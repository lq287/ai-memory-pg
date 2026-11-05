"""应用侧取记忆的唯一入口。

原则：
  1. 默认只给 summary。全文要显式要 —— 这是控制 token 成本最直接的一刀。
  2. 召回复用数据库里的 recall_episodes() / recall_facts()，不在应用层重排一遍。
     两处排序逻辑各写一遍，早晚会不一致。
  3. **预算封顶在这里执行**：按复合分数排序后逐条累加，超过 token 预算就截断。
     没有这一层，遇到"上次那个事"这类模糊提问会把预算全吃光。
  4. 检索不写库。use_count 走 bump_usage() 异步批量刷。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from .config import FactsParams, RecallParams, plan_recall
from .store import Store

log = logging.getLogger(__name__)

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def estimate_tokens(text: str) -> int:
    """粗估 token 数，够用来做预算截断即可。

    中日韩字符基本 1 字 ≈ 1 token；拉丁文大约 4 字符 ≈ 1 token。
    要精确就换成真 tokenizer，但那会把一条热路径变成依赖入口。
    """
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


@dataclass(frozen=True, slots=True)
class Hit:
    id: str
    summary: str
    snippet: str
    event_time: datetime
    source: str
    confidence: float
    score: float
    rank_vec: int | None
    rank_kw: int | None
    expires_at: datetime | None

    @property
    def recall_paths(self) -> str:
        """哪几路召回了它。调试路由问题时第一眼要看的东西。"""
        return ("v" if self.rank_vec else "-") + ("k" if self.rank_kw else "-")


@dataclass(frozen=True, slots=True)
class Rendered:
    """一次装配的结果，带着"用了哪几条"以便回写 use_count。"""

    text: str
    used_ids: tuple[str, ...]
    dropped: int


def recall(
    store: Store,
    user_id: str,
    query: str,
    params: RecallParams | None = None,
) -> list[Hit]:
    """混合召回，返回 summary 层。"""
    p = params or plan_recall(query)
    rows, _vec = store.recall(user_id, query, p)
    hits = [
        Hit(
            id=str(r["id"]),
            summary=r["summary"] or "",
            snippet=r["snippet"] or "",
            event_time=r["event_time"],
            source=r["source"],
            confidence=float(r["confidence"]),
            score=float(r["score"]),
            rank_vec=int(r["rk_v"]) if r["rk_v"] is not None else None,
            rank_kw=int(r["rk_k"]) if r["rk_k"] is not None else None,
            expires_at=r["expires_at"],
        )
        for r in rows
    ]
    log.debug(
        "recall user=%s q=%r limit=%s -> %s 条 (%s)",
        user_id,
        query[:40],
        p.limit,
        len(hits),
        ",".join(h.recall_paths for h in hits),
    )
    return hits


def expand(store: Store, ids: Sequence[str]) -> dict[str, str]:
    """渐进披露第二层：按 id 拉全文。ids 为空就不查。"""
    if not ids:
        return {}
    return {str(r["id"]): r["content"] for r in store.expand(list(ids))}


def render_memories(hits: Sequence[Hit], budget: int = 800) -> Rendered:
    """把命中装配成注入 prompt 的一段文本，并按 token 预算截断。

    格式刻意做成"一行一条 + 日期"：模型对时间信息的利用程度和它的摆放位置
    强相关，把日期放在最前面比放在句尾更容易被引用。
    """
    if not hits:
        return Rendered("", (), 0)

    header = "# 相关记忆（可能不完整，按需引用，不要编造）"
    used_tokens = estimate_tokens(header)
    lines = [header]
    used_ids: list[str] = []
    dropped = 0

    for h in hits:
        day = h.event_time.strftime("%Y-%m-%d") if h.event_time else "日期未知"
        line = f"- [{day}] {h.summary or h.snippet}"
        cost = estimate_tokens(line)
        if used_ids and used_tokens + cost > budget:
            dropped += 1
            continue   # 按分数顺序截断：便宜的留，超预算的丢
        if not used_ids and used_tokens + cost > budget:
            # 第一条就超预算：截断文本硬塞进去，总比什么都不给强
            keep = max(8, budget - used_tokens - 8)
            line = line[:keep] + "…"
            cost = estimate_tokens(line)
            dropped += 1
        lines.append(line)
        used_ids.append(h.id)
        used_tokens += cost

    return Rendered("\n".join(lines), tuple(used_ids), dropped)


def render_facts(facts: Sequence[dict], budget: int = 300) -> Rendered:
    """长期事实的装配。事实层不做语义排序，只按 score（置信度 × 使用衰减）。"""
    if not facts:
        return Rendered("", (), 0)

    header = "# 已知事实（按主体键取用，优先级高于推测）"
    used_tokens = estimate_tokens(header)
    lines = [header]
    keys: list[str] = []
    dropped = 0

    for f in facts:
        value = f["value"]
        rendered_value = (
            value if isinstance(value, str) else repr(value)
        )
        line = f"- {f['key']} = {rendered_value}"
        cost = estimate_tokens(line)
        if used_tokens + cost > budget and keys:
            dropped += 1
            continue
        lines.append(line)
        keys.append(f["key"])
        used_tokens += cost

    return Rendered("\n".join(lines), tuple(keys), dropped)


def recall_facts(
    store: Store, user_id: str, keys: Sequence[str] | None = None,
    params: FactsParams | None = None,
) -> list[dict]:
    return store.recall_facts(user_id, keys, params)


def assemble(
    store: Store,
    user_id: str,
    query: str,
    params: RecallParams | None = None,
) -> tuple[str, list[Hit], list[dict]]:
    """一步到位：事实层 + 情景层，各自预算封顶，拼成一段。

    事实层排在前面：它是"按主体键精确取用"的确定性知识，应当优先于
    "按语义相似度模糊召回"的情景片段。
    """
    p = params or plan_recall(query)
    facts = recall_facts(store, user_id)
    fact_block = render_facts(facts, budget=min(300, p.token_budget // 3))
    hits = recall(store, user_id, query, p)
    mem_block = render_memories(hits, budget=max(200, p.token_budget - 300))

    parts = [b for b in (fact_block.text, mem_block.text) if b]
    return "\n\n".join(parts), hits, facts
