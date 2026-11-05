"""两次 LLM 调用：抽取记忆单元、给出四态建议。

重要：模型输出永远只是**建议**。抽取结果要过形状校验，状态建议要过
decide.arbitrate 的确定性护栏。这里负责的是"把模型的自由发挥收敛成 schema
内合法的东西"，不是"相信模型"。

两个替身：
  HeuristicLLM     零依赖、不联网、确定性 —— 本地跑通和单测用
  OpenAICompatLLM  任何 OpenAI 兼容端点（OpenAI / DeepSeek / vLLM / Ollama）
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Protocol, Sequence

from .decide import Candidate, Fact, Proposal, parse_proposal

# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------
EXTRACT_SYSTEM = """你是一个记忆抽取器。从对话中抽取"值得跨会话记住"的单元。

两条硬约束：
- 记忆单元按**事件**切分，不按 token 切分。一个单元对应一个有始有终的事件，
  或一条完整的事实。不要把一次交互切成碎片。
- 长期事实（偏好/配置/主体属性）与情景事件分开标注：事实带 fact_key，
  情景事件不带。

抽取标准：
- 只抽用户本人的稳定偏好、身份、环境、约定、长期目标，以及值得回顾的事件与决策。
- 不抽：临时情绪、当次任务的中间状态、礼貌用语、对 AI 的指令本身。
- 一句话一条，第三人称陈述，保留具体值（时区、版本、路径、阈值）。
- 不确定是否长期有效的，confidence 给 0.4 以下。

严格输出 JSON，不要任何解释文字、不要 markdown 围栏：
{"items": [{
  "kind": "episode",
  "content": "用户上月把发布流程从手工改为 CI 触发",
  "summary": "发布流程改 CI",
  "event_time": "2025-03-01T10:00:00+08:00",
  "confidence": 0.8,
  "importance": 0.6,
  "tags": ["release"]
}]}

kind 取值：raw_turn / episode / summary / longterm。
若 kind=longterm，必须带 fact_key（小写、点分隔，如 "timezone"、"plan.tier"）
和 value（JSON 值），例如：
  {"kind":"longterm","content":"用户时区为 Asia/Shanghai","fact_key":"timezone",
   "value":"Asia/Shanghai","confidence":0.9,...}

event_time：用户明确提到时间点时才给 ISO8601，否则一律 null —— 不要猜时间。"""

JUDGE_SYSTEM = """你是一个记忆管理助手。给定一条候选记忆和若干已有记忆，
选择唯一动作：

- accept  全新内容，正常写入
- merge   复述或强化已有条目，不新建，只提升该条目的置信度/重要性
- defer   疑似矛盾（两条描述同一件事但对不上），落库但标记待裁决 —— 不合并也不丢弃
- drop    低信息熵、无后续价值

硬约束：
1. memory_id 只能从候选列表的 id 中选，**不得编造**；merge/defer 必须给。
2. 只有确实指向同一条记忆时才选 merge/defer，仅仅话题相关请选 accept。
3. 拿不准就选 accept —— 建一条新的代价，远小于挂一条待裁决或改错一条旧的。
4. new_content 是可选的建议措辞，可以省略。

严格输出 JSON，不要任何解释文字、不要 markdown 围栏：
{"action": "merge", "memory_id": "…", "new_content": "…", "reason": "一句话理由"}"""


class LLM(Protocol):
    def extract(self, *, user_id: str, text: str, source: str) -> list[Fact]: ...

    def judge(self, *, fact: Fact, candidates: Sequence[Candidate]) -> Proposal: ...


# ---------------------------------------------------------------------------
# 输出清洗：模型一定会带围栏、带前言、带尾随逗号
# ---------------------------------------------------------------------------
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def loads_strict(raw: str) -> object:
    """从一段可能很脏的文本里抠出第一个 JSON 值。失败抛 ValueError。"""
    if not isinstance(raw, str):
        raise ValueError("LLM 返回值不是字符串")
    s = _FENCE.sub("", raw).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(s)):
            if s[i] == opener:
                depth += 1
            elif s[i] == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start : i + 1])
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"JSON 解析失败: {exc}") from exc
    raise ValueError("找不到 JSON")


def _clip(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _as_float(value: object, default: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return max(lo, min(hi, f))


def _parse_event_time(value: object) -> tuple[datetime, str]:
    """时间点解析。给不出就是给不出 —— 回落到 now() 并显式标记来源。

    让 NOT NULL 字段靠"猜一个时间"来填，是记忆系统里最容易埋下的假精确：
    之后所有衰减分都会基于这个错误时间算。
    """
    if isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt, "extracted"
        except ValueError:
            pass
    return datetime.now(timezone.utc), "inferred"


_KINDS = {"raw_turn", "episode", "summary", "longterm"}
_FACT_KEY = re.compile(r"^[a-z0-9_.-]{1,64}$")


def parse_facts(payload: object, *, user_id: str, source: str) -> list[Fact]:
    """把抽取结果收敛成合法 Fact 列表。形状不对的整条丢掉，不做修补。"""
    if isinstance(payload, dict):
        rows = payload.get("items") or payload.get("facts")
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = None
    if not isinstance(rows, list):
        raise ValueError("抽取结果缺少 items 数组")

    out: list[Fact] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        content = row.get("content")
        if not isinstance(content, str) or len(content.strip()) < 2:
            continue

        kind = row.get("kind")
        kind = kind.strip().lower() if isinstance(kind, str) else "episode"
        if kind not in _KINDS:
            kind = "episode"

        # 长期事实必须带合法的 fact_key，否则降级成情景事件（宁可存成事件，
        # 也不能用脏 key 污染 facts 表的唯一性不变量）
        fact_key = row.get("fact_key")
        if isinstance(fact_key, str):
            fact_key = fact_key.strip().lower()
            fact_key = fact_key if _FACT_KEY.match(fact_key) else None
        else:
            fact_key = None
        if kind == "longterm" and fact_key is None:
            kind = "episode"

        event_time, source_of_time = _parse_event_time(row.get("event_time"))
        tags = row.get("tags")
        tags = (
            tuple(str(t) for t in tags if isinstance(t, (str, int)))
            if isinstance(tags, list)
            else ()
        )
        out.append(
            Fact(
                user_id=user_id,
                content=_clip(content, 600),
                confidence=_as_float(row.get("confidence"), 0.5),
                source=source,
                event_time=event_time,
                kind=kind,
                event_time_source=source_of_time,
                summary=(
                    _clip(row["summary"], 200)
                    if isinstance(row.get("summary"), str)
                    else None
                ),
                importance=_as_float(row.get("importance"), 0.5),
                tags=tags[:8],
                fact_key=fact_key,
                value=row.get("value"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# 替身 A：零依赖规则版
# ---------------------------------------------------------------------------
_PREF_PATTERNS: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"(我的?)?(时区|所在地|城市|语言|编辑器|终端|系统|版本|路径|端口)"), 0.85),
    (re.compile(r"我(?:比较)?(?:喜欢|偏好|习惯|常用|讨厌|不喜欢|不想)"), 0.8),
    (re.compile(r"我(?:叫|是|在.{1,12}(?:工作|居住))"), 0.85),
    (re.compile(r"(以后|今后|默认|一律|每次都|记住)"), 0.75),
    (re.compile(r"\bI (?:prefer|like|use|live in|work at|am)\b", re.IGNORECASE), 0.8),
    (re.compile(r"\bmy (?:name|timezone|time zone|editor|email) is\b", re.IGNORECASE), 0.9),
    (re.compile(r"\b(?:always|never|from now on|remember)\b", re.IGNORECASE), 0.7),
)
# 长期事实的关键词 → fact_key（替身只认这几个，生产靠模型给）
_FACT_KEYS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"时区|timezone|time zone", re.IGNORECASE), "timezone"),
    (re.compile(r"套餐|plan|订阅", re.IGNORECASE), "plan.tier"),
    (re.compile(r"货币|币种|currency", re.IGNORECASE), "currency"),
)
# 从句子里抠出"值"（替身偷懒版：真模型直接给 value 字段）
_FACT_VALUES: dict[str, re.Pattern[str]] = {
    "timezone": re.compile(r"\b([A-Z][A-Za-z]+/[A-Za-z_]+|UTC[+-]\d{1,2})\b"),
    "currency": re.compile(r"\b([A-Z]{3})\b"),
}
_SENT_SPLIT = re.compile(r"[。！？\n；;!?]+|(?<=\.)\s+")
_NEGATION = re.compile(r"(不|别|没有|never|not|don't|do not)")


class HeuristicLLM:
    """确定性规则替身：能跑通全链路，但不具备真正的语义理解。

    它的存在让 `python -m app.worker` 不需要任何 API key 就能验证
    队列、事务、四态落库、TTL、冲突边是否工作正常 —— 这些才是最容易写错的部分。
    """

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, *, user_id: str, text: str, source: str) -> list[Fact]:
        self.calls += 1
        facts: list[Fact] = []
        for raw in _SENT_SPLIT.split(text or ""):
            sent = raw.strip()
            if len(sent) < 4:
                continue
            best = 0.0
            for pattern, weight in _PREF_PATTERNS:
                if pattern.search(sent):
                    best = max(best, weight)
            if best == 0.0:
                continue

            fact_key = None
            for pattern, key in _FACT_KEYS:
                if pattern.search(sent):
                    fact_key = key
                    break

            value = sent
            if fact_key:
                matcher = _FACT_VALUES.get(fact_key)
                found = matcher.search(sent) if matcher else None
                value = found.group(1) if found else sent

            facts.append(
                Fact(
                    user_id=user_id,
                    content=_clip(sent, 600),
                    confidence=best,
                    source=source,
                    event_time=datetime.now(timezone.utc),
                    kind="longterm" if fact_key else "episode",
                    event_time_source="ingest",
                    summary=_clip(sent, 80),
                    importance=min(1.0, best + 0.1),
                    tags=("heuristic",),
                    fact_key=fact_key,
                    value=value if fact_key else None,
                )
            )
        return facts

    def judge(self, *, fact: Fact, candidates: Sequence[Candidate]) -> Proposal:
        self.calls += 1
        if not candidates:
            return Proposal(action="accept", reason="no_candidates")

        new_key = _shingles(fact.content)
        best: Candidate | None = None
        best_overlap = 0.0
        for c in candidates:
            overlap = _jaccard(new_key, _shingles(c.content))
            if overlap > best_overlap:
                best, best_overlap = c, overlap

        if best is None or best_overlap < 0.30:
            return Proposal(action="accept", reason="low_overlap")

        same = _strip(fact.content) == _strip(best.content)
        if same:
            return Proposal(action="merge", memory_id=best.id, reason="restated")

        contradicts = bool(_NEGATION.search(fact.content)) != bool(
            _NEGATION.search(best.content)
        )
        if best_overlap >= 0.55 or contradicts:
            return Proposal(
                action="defer",
                memory_id=best.id,
                new_content=fact.content,
                reason=f"overlap={best_overlap:.2f}",
            )
        return Proposal(action="accept", reason=f"overlap={best_overlap:.2f}")


def _strip(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", (text or "").lower())


def _shingles(text: str) -> set[str]:
    s = _strip(text)
    return {s[i : i + 2] for i in range(max(0, len(s) - 1))} or {s}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# 替身 B：OpenAI 兼容端点（标准库实现，无 SDK 依赖）
# ---------------------------------------------------------------------------
class OpenAICompatLLM:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "gpt-4o-mini",
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries

    def chat_text(self, system: str, user: str) -> str:
        """要一段纯文本回复（不是 JSON）。distill 提炼摘要用。"""
        import time

        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._chat(system, user)
            except Exception as exc:
                last = exc
                time.sleep(min(2**attempt, 4))
        raise RuntimeError(f"LLM 调用失败: {last}")

    def _chat_json(self, system: str, user: str) -> object:
        import time

        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return loads_strict(self._chat(system, user))
            except Exception as exc:  # 网络、限流、JSON 坏了，都重试
                last = exc
                time.sleep(min(2**attempt, 4))
        raise RuntimeError(f"LLM 调用失败: {last}")

    def _chat(self, system: str, user: str) -> str:
        import urllib.request

        body = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read())
        return payload["choices"][0]["message"]["content"]

    def extract(self, *, user_id: str, text: str, source: str) -> list[Fact]:
        payload = self._chat_json(EXTRACT_SYSTEM, _clip(text, 6000))
        return parse_facts(payload, user_id=user_id, source=source)

    def judge(self, *, fact: Fact, candidates: Sequence[Candidate]) -> Proposal:
        if not candidates:
            return Proposal(action="accept", reason="no_candidates")

        lines = [
            "候选记忆：",
            f"- kind: {fact.kind} | confidence: {fact.confidence:.2f} | source: {fact.source}",
            f"- content: {_clip(fact.content, 500)}",
            "",
            "已有记忆：",
        ]
        for c in candidates:
            lines.append(
                f"- id: {c.id} | source: {c.source} | confidence: {c.confidence:.2f} "
                f"| use_count: {c.use_count} | content: {_clip(c.content, 400)}"
            )
        payload = self._chat_json(JUDGE_SYSTEM, "\n".join(lines))
        return parse_proposal(payload)
