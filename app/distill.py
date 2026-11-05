"""distill 提炼：把原始轮次压成派生摘要，并留下血缘。

三种后端，同一条管线：

  HeuristicDistiller  抽取式、零依赖、确定性。默认后端 —— 不需要 LLM 端点。
  AgentDistiller      把一批原始轮次变成一个**待办任务**（tasks + working_set），
                      由 Agent（我）读书写摘要；写回时用 distill_apply()。
                      没有 API key 也能拿到接近 LLM 的质量。
  LLMDistiller        配了 MEM_LLM_BASE_URL 时用，质量最好（提示词见 llm.py）。

为什么默认不是 LLM：
  提炼跑在后台、按用户批量进行，一旦调用失败就是"记忆静默丢失"。
  抽取式后端不会失败，且结果可复现；LLM 后端是升级项，不是前提。

提炼产物是**一等公民**：写进 episodes(kind='summary')，带向量、能被召回，
并通过 distill_links 记录"由哪些原始轮次压成"。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol, Sequence

from .decide import Fact, normalize

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger(__name__)

# 提炼产物按 summary 类型入库：TTL 3650 天，比 raw_turn 的 30 天长寿
SUMMARY_KIND = "summary"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RawTurn:
    id: str
    content: str
    summary: str | None
    event_time: datetime
    age_days: float


@dataclass(frozen=True, slots=True)
class DistillResult:
    summary_id: str | None
    linked: int
    text: str
    backend: str
    sources: tuple[str, ...]


class Distiller(Protocol):
    name: str

    def summarize(self, turns: Sequence[RawTurn], *, limit_chars: int = 600) -> tuple[str, str]: ...


# ---------------------------------------------------------------------------
# 后端 A：抽取式启发式（默认）
# ---------------------------------------------------------------------------
_SENT = re.compile(r"[。！？!?\n；;]+")
_NUMBER = re.compile(r"\d")
_CJK = re.compile(r"[\u3400-\u9fff]")


def _signal_score(text: str) -> float:
    """一句话"值不值得留"的启发式打分。

    依据是"信息量"，不是"重要性"判断：
      · 含数字/ID/路径/版本 → 具体、可核对，最该留
      · 含中文 → 中文信息密度高，同字符数更值钱
      · 长度适中最好：太短没信息，太长是啰嗦
      · 含因果/否定/决策词 → 通常是结论
    """
    if not text:
        return 0.0
    n = len(text)
    score = 0.0
    if _NUMBER.search(text):
        score += 2.0
    cjk = len(_CJK.findall(text))
    score += min(cjk / 12.0, 1.5)
    # 长度：20~80 字最理想
    if 20 <= n <= 80:
        score += 1.0
    elif n < 8:
        score -= 1.5
    elif n > 200:
        score -= 0.5
    if re.search(r"(因为|所以|于是|结果|决定|改成|不要|必须|避免|失败|报错|约定|以后)", text):
        score += 1.2
    if re.search(r"(http|C:|D:|E:|\\|/|_\w+|\.\w{2,4}\b)", text):
        score += 0.8
    return score


class HeuristicDistiller:
    """抽取式：从一批原始轮次里挑出信息量最高的几句话，压成要点。

    刻意不做"改写" —— 抽取不会编造，代价是读起来不如人写的好。
    这正是它适合当默认后端的原因：**不会产生幻觉**。
    """

    name = "heuristic"

    def summarize(self, turns: Sequence[RawTurn], *, limit_chars: int = 600) -> tuple[str, str]:
        if not turns:
            return "", ""

        # 1. 切句 + 去重（同一句在多轮里出现只留一次）
        seen: set[str] = set()
        scored: list[tuple[float, int, str]] = []
        for idx, t in enumerate(turns):
            for raw in _SENT.split(t.content or ""):
                s = (raw or "").strip()
                if len(s) < 4:
                    continue
                key = normalize(s)
                if not key or key in seen:
                    continue
                seen.add(key)
                # 越新的轮次略微加分（同分时新内容优先）
                recency = idx / max(1, len(turns))
                scored.append((_signal_score(s) + recency * 0.5, idx, s))

        scored.sort(key=lambda x: (-x[0], x[1]))

        # 2. 贪心装进字数上限，保持原始时间顺序输出
        picked: list[tuple[int, str]] = []
        used = 0
        for _score, idx, s in scored:
            cost = len(s) + 3  # "- " + 换行
            if used + cost > limit_chars and picked:
                continue
            if used + cost > limit_chars:
                s = s[: max(8, limit_chars - used - 3)]
                cost = len(s) + 3
            picked.append((idx, s))
            used += cost
            if used >= limit_chars:
                break

        picked.sort(key=lambda x: x[0])
        body = "\n".join(f"- {s}" for _i, s in picked)

        # 3. 一句话摘要：取时间跨度 + 第一条要点
        first = turns[0].event_time
        last = turns[-1].event_time
        span = (last - first).days
        head = f"{first:%Y-%m-%d} 起的 {len(turns)} 轮原始记录"
        if span > 0:
            head += f"（跨度 {span} 天）"
        summary = f"{head}：{picked[0][1][:80]}" if picked else head
        return body, summary


# ---------------------------------------------------------------------------
# 后端 C：LLM（需要端点；带**接地校验**防幻觉）
# ---------------------------------------------------------------------------
DISTILL_SYSTEM = """你是一个记忆提炼器。把一批原始对话轮次压成要点摘要。

要求：
- 只保留长期有价值的信息：决定、约定、具体值（端口/路径/版本/ID）、结论、待办。
- 丢弃：寒暄、重复表述、一次性的中间状态。
- 用中文、短句、以 "- " 开头的列表，3~6 条，总长不超过 {limit} 字。
- **禁止编造**：不要加入原文里没有的数字、路径、名称、结论。没有就少写一条。

只输出要点列表本身，不要前言、不要标题、不要 markdown 围栏。"""

_NUM_TOKEN = re.compile(r"\d+(?:\.\d+)?")

# 行首的"列表标记"：圆点、星号、以及 1. / 1) / 1、 / ① 这类序号。
# 必须在接地校验**之前**剥掉 —— 否则模型正常输出的 "1. xxx" 会被当成
# "原文没有的数字"而整条拒绝（这是一个真实存在的误杀：模型很爱加序号）。
# 注意模式要求序号后面紧跟 . ) 、 或圈号，所以 "5432 是端口" 不会被误伤。
_LINE_MARK = re.compile(r"^\s*(?:[-*•·]|\d{1,2}[.)、]\s|[①-⑳])\s*")


def _grounded(summary: str, sources: str) -> bool:
    """接地校验：摘要里出现的每个数字，都必须在原文里出现过。

    这条检查很粗糙，但专治最危险的一类幻觉 —— 编出一个不存在的端口/版本/阈值。
    记忆系统里"编造的精确值"比"含糊的概括"危害大得多：前者会被当成事实反复召回。
    """
    src_nums = set(_NUM_TOKEN.findall(sources))
    for tok in _NUM_TOKEN.findall(summary):
        if tok not in src_nums:
            return False
    return True


class LLMDistiller:
    """提示词驱动的提炼。质量最好，但会失败 —— 所以只在配了端点时启用。"""

    name = "llm"

    def __init__(self, llm, *, max_retries: int = 2) -> None:
        self.llm = llm
        self.max_retries = max_retries

    def summarize(self, turns: Sequence[RawTurn], *, limit_chars: int = 600) -> tuple[str, str]:
        if not turns:
            return "", ""

        joined = "\n".join(f"[{t.event_time:%Y-%m-%d}] {t.content}" for t in turns)
        system = DISTILL_SYSTEM.replace("{limit}", str(limit_chars))
        last_err: Exception | None = None

        for attempt in range(self.max_retries + 1):
            raw = self.llm.chat_text(system, joined)
            body = (raw or "").strip()
            # 去掉模型爱加的围栏
            body = re.sub(r"^```[\w-]*\s*|\s*```$", "", body).strip()
            lines = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
            # 统一成 "- " 列表，同时剥掉行首标记（含序号，理由见 _LINE_MARK）
            items = [s for s in (_LINE_MARK.sub("", ln, count=1).strip() for ln in lines) if s]
            bullets = [f"- {s}" for s in items]
            body = "\n".join(bullets)

            if not body:
                last_err = ValueError("LLM 返回空摘要")
                continue
            if len(body) > limit_chars * 2:
                body = body[: limit_chars * 2]
            if not _grounded(body, joined):
                last_err = ValueError("摘要出现原文没有的数字（疑似幻觉），拒绝落库")
                log.warning("distill(llm) 接地校验失败，重试 %s/%s", attempt + 1, self.max_retries)
                continue

            first = turns[0].event_time
            head = f"{first:%Y-%m-%d} 起的 {len(turns)} 轮记录：{items[0][:60]}" if items else ""
            return body, head[:200]

        # 重试仍不接地：抛错让队列重试/进死信，绝不静默落一条可疑摘要
        raise RuntimeError(f"LLM 提炼失败（{last_err}）")


# ---------------------------------------------------------------------------
# 后端 B：Agent-in-the-loop

# ---------------------------------------------------------------------------
class AgentDistiller:
    """把提炼变成**可交给 Agent 的任务**：不自动产出摘要，只排队等写回。

    流程（CLI 对应 mem.py distill plan / apply）：
      1. plan()  → 建一个 kind='distill' 的 task，把候选原始轮次放进去；
      2. Agent（我）读任务、写摘要；
      3. apply() → 写 summary 记忆 + 落血缘 + 关任务。
    好处：不需要 API key，质量接近 LLM；代价是需要人在环里。
    """

    name = "agent"

    def summarize(self, turns: Sequence[RawTurn], *, limit_chars: int = 600) -> tuple[str, str]:
        raise NotImplementedError(
            "AgentDistiller 不自动产出摘要：用 distill_plan() 建任务，写好后再 distill_apply()"
        )


# ---------------------------------------------------------------------------
# 管线
# ---------------------------------------------------------------------------
def _parse_embedding(text: str) -> list[float]:
    return [float(x) for x in text.strip("[]").split(",")]


def fetch_candidates(
    store: "Store", user_id: str, *, min_age_days: float = 7, limit: int = 50
) -> list[RawTurn]:
    rows = store._read(
        "SELECT * FROM distill_candidates(%s, %s, %s)", (user_id, min_age_days, limit)
    )
    return [
        RawTurn(
            id=str(r["id"]),
            content=r["content"],
            summary=r["summary"],
            event_time=r["event_time"],
            age_days=float(r["age_days"]),
        )
        for r in rows
    ]


def pending_stats(store: "Store", user_id: str, *, min_age_days: float = 7) -> dict:
    rows = store._read("SELECT * FROM distill_pending(%s, %s)", (user_id, min_age_days))
    return rows[0] if rows else {"pending": 0, "oldest_days": 0.0, "expiring_7d": 0}


def distill_apply(
    store: "Store",
    user_id: str,
    raw_ids: Sequence[str],
    body: str,
    *,
    summary: str | None = None,
    backend: str = "agent",
    source: str = "tool",
    confidence: float = 0.8,
    importance: float = 0.6,
    close_raw: bool = False,
) -> DistillResult:
    """把一个批次的原始轮次写成一条 summary 记忆 + 血缘。"""
    if not raw_ids:
        raise ValueError("raw_ids 为空")
    if not body.strip():
        raise ValueError("摘要正文为空")

    # 幂等键：同一批原始轮次只产出一条摘要
    digest = hashlib.sha1(
        ("|".join(sorted(normalize(x) for x in raw_ids))).encode()
    ).hexdigest()[:16]
    source_ref = f"distill:{backend}:{digest}"

    head = (summary or body.splitlines()[0])[:200]
    vec = store.embedder.embed([body])[0]

    fact = Fact(
        user_id=user_id,
        content=body,
        confidence=confidence,
        source=source,
        event_time=datetime.now(timezone.utc),
        kind=SUMMARY_KIND,
        event_time_source="ingest",
        summary=head,
        importance=importance,
        tags=("distilled", backend),
    )
    # 直接走 accept：摘要不参与四态裁决（它不对应"新事实"，而是聚合）
    from .decide import Decision

    decision = Decision(action="accept", content=body, reason="distill")
    res = store.apply(decision, fact, vec, source_ref=source_ref)

    if not res.get("applied"):
        log.info("distill 命中幂等（%s），跳过落库", source_ref)
        return DistillResult(None, 0, body, backend, tuple(raw_ids))

    summary_id = str(res["id"])
    row = store._write("SELECT distill_link(%s, %s) AS n", (summary_id, list(raw_ids)))
    linked = int(row["n"]) if row else 0

    if close_raw:
        store._write("SELECT distill_close_raw(%s) AS n", (list(raw_ids),))

    log.info("distill 完成 summary=%s linked=%s backend=%s", summary_id[:8], linked, backend)
    return DistillResult(summary_id, linked, body, backend, tuple(raw_ids))


def distill_run(
    store: "Store",
    user_id: str,
    *,
    backend: str = "heuristic",
    min_age_days: float = 7,
    batch_size: int = 20,
    max_batches: int = 5,
    close_raw: bool = False,
    limit_chars: int = 600,
) -> list[DistillResult]:
    """自动提炼：按批取候选 → 用一个 Distiller 压成摘要 → 落库。

    AgentDistiller 会在这里被拒绝 —— 它必须走 plan/apply 两步。
    """
    distiller: Distiller
    if backend == "heuristic":
        distiller = HeuristicDistiller()
    elif backend == "agent":
        raise ValueError("backend=agent 需要人工写回：用 distill_plan() + distill_apply()")
    elif backend == "llm":
        # 只有配了端点才允许；没配就明确报错，别假装成功
        from .config import Settings

        settings = Settings.from_env()
        if not settings.llm_base_url:
            raise ValueError("backend=llm 需要 MEM_LLM_BASE_URL（当前未配置）")
        from .llm import OpenAICompatLLM

        distiller = LLMDistiller(
            OpenAICompatLLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
        )
    else:
        raise ValueError(f"未知 backend: {backend}")

    out: list[DistillResult] = []
    for _ in range(max_batches):
        turns = fetch_candidates(store, user_id, min_age_days=min_age_days, limit=batch_size)
        if not turns:
            break
        body, head = distiller.summarize(turns, limit_chars=limit_chars)
        if not body.strip():
            log.info("distill：本批无可提炼内容，跳过")
            break
        out.append(
            distill_apply(
                store, user_id, [t.id for t in turns], body,
                summary=head, backend=distiller.name, close_raw=close_raw,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Agent-in-the-loop：plan
# ---------------------------------------------------------------------------
def distill_plan(
    store: "Store",
    user_id: str,
    *,
    min_age_days: float = 7,
    batch_size: int = 20,
    token_budget: int = 3000,
) -> dict:
    """建一个提炼任务 + 工作集，把候选原始轮次 pin 进去，等 Agent 写摘要。

    复用任务层与工作记忆层：任务记录"提炼进度"，工作集承载"这批要压的原文"。
    """
    from .workmem import WorkMemory

    wm = WorkMemory(store)
    turns = fetch_candidates(store, user_id, min_age_days=min_age_days, limit=batch_size)
    if not turns:
        return {"created": False, "reason": "no_candidates", "count": 0}

    task = wm.start_task(
        user_id, "distill", f"提炼 {len(turns)} 条原始轮次",
        metadata={"min_age_days": min_age_days, "raw_ids": [t.id for t in turns]},
    )
    tid = str(task["id"])
    wm.update_task(tid, status="running", step="read", step_index=1, progress=0.1)

    ws = wm.open_set(user_id, task_id=tid, note="提炼工作集", token_budget=token_budget)
    sid = str(ws["id"])
    for t in turns:
        wm.pin(sid, "tool_output",
               f"[{t.event_time:%Y-%m-%d}, {t.age_days:.0f}天前] {t.content}",
               ref_id=t.id)
    wm.update_task(tid, progress=0.3)

    return {
        "created": True,
        "task_id": tid,
        "set_id": sid,
        "count": len(turns),
        "raw_ids": [t.id for t in turns],
        "assembly": wm.render(sid).text,
    }


def distill_apply_from_task(
    store: "Store",
    task_id: str,
    body: str,
    *,
    summary: str | None = None,
    close_raw: bool = False,
) -> DistillResult:
    """把 Agent 写好的摘要写回，并关闭对应的提炼任务。"""
    from .workmem import WorkMemory

    wm = WorkMemory(store)
    got = wm.get_task(task_id, events=5)
    if not got:
        raise ValueError(f"任务 {task_id} 不存在")
    task, _events = got
    meta = task.get("metadata") or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    raw_ids = meta.get("raw_ids") or []
    if not raw_ids:
        raise ValueError(f"任务 {task_id} 没有关联原始轮次")

    result = distill_apply(
        store, task["user_id"], raw_ids, body,
        summary=summary, backend="agent", close_raw=close_raw,
    )
    wm.finish_task(task_id, "done", {"summary_id": result.summary_id,
                                     "linked": result.linked})
    return result


__all__ = [
    "RawTurn", "DistillResult", "Distiller",
    "HeuristicDistiller", "AgentDistiller", "LLMDistiller",
    "fetch_candidates", "pending_stats",
    "distill_run", "distill_apply",
    "distill_plan", "distill_apply_from_task",
]
