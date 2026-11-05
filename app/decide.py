"""写入裁决：模型只提议，规则做裁决。

四态（来自设计稿的语义，不是 ADD/UPDATE/DELETE/NONE 的换皮）：

    accept  全新内容，正常写入
    merge   复述或强化已有条目，**不新建**，抬高该条目的置信度/重要性
    defer   疑似矛盾，落库但标记待裁决（不立即合并，也不丢弃）
    drop    低信息熵 / 无后续价值，直接丢弃

注意 merge 和 "取代" 不是一件事：merge 只调整治理信号，不动内容；
真正的内容替换发生在**矛盾被裁决之后**（resolve_conflict → supersede）。

这个模块是纯函数，没有 IO、没有数据库、没有网络。原因很实际：写入路径的核心
规则如果散落在 LLM 调用、事务、重试逻辑之间，就永远测不起来，而这条路径错了的
表现是"用户的偏好就是没存进去"，静默且难以察觉。

三条原则：
  1. 失败关闭：模型给了不认识的东西，一律 drop，绝不猜。
  2. 模型给的 memory_id 只当"提名"，必须能在候选集里查到才认；new_content 只当
     建议，默认不落库（落库用抽取阶段已校验过的原文）。
  3. 冲突不物理删除；矛盾先生成 defer 边，裁决后才取代。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Sequence

from .config import Policy

Action = Literal["accept", "merge", "defer", "drop"]
ACTIONS: frozenset[str] = frozenset({"accept", "merge", "defer", "drop"})


# ---------------------------------------------------------------------------
# 数据形状
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Fact:
    """抽取阶段产出的一条待写入记忆单元。

    kind 决定 TTL 与写入门槛：raw_turn / episode / summary / longterm。
    记忆单元按"事件"切分，不按 token 数切分 —— 一个单元对应一个有始有终的事件。
    """

    user_id: str
    content: str
    confidence: float
    source: str  # conversation / tool / human / import
    event_time: datetime
    kind: str = "episode"
    event_time_source: str = "inferred"
    summary: str | None = None
    importance: float = 0.5
    tags: tuple[str, ...] = ()
    # kind='longterm' 时才有：这条要落到 facts 表（按主体键精确取用），
    # 而不是 episodes 表（按语义召回）。决策一在类型层面就分开了。
    fact_key: str | None = None
    value: object = None


@dataclass(frozen=True, slots=True)
class Candidate:
    """检索出来的相似旧记忆。裁决只看内容与治理字段，不看分数。"""

    id: str
    content: str
    confidence: float
    source: str
    importance: float = 0.5
    use_count: int = 0
    distance: float | None = None


@dataclass(frozen=True, slots=True)
class Proposal:
    """模型给的原始建议，已解析但**尚未**被信任。"""

    action: str
    memory_id: str | None = None
    new_content: str | None = None
    reason: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Decision:
    """最终裁决结果。store 只执行这个，不再看模型。"""

    action: Action
    content: str
    target_id: str | None = None
    reason: str = ""
    needs_review: bool = False
    model_action: str | None = None
    downgraded: bool = False
    # merge 时给治理信号用的增量
    confidence_delta: float = 0.0
    importance_delta: float = 0.0


# ---------------------------------------------------------------------------
# 归一化：只用于"是不是同一件事"的比较，不用于改写落库内容
# ---------------------------------------------------------------------------
_WS = re.compile(r"\s+")
_PUNCT = re.compile(
    r"[\s\u3000-\u303f\uff00-\uff65!-/:-@\[-`{-~，。、；：？！“”‘’（）《》…—]+"
)


def normalize(text: str) -> str:
    """小写 + 去标点空白 + NFKC 折叠，只为了判等。"""
    s = unicodedata.normalize("NFKC", text or "").lower()
    return _PUNCT.sub("", _WS.sub(" ", s))


# ---------------------------------------------------------------------------
# 裁决
# ---------------------------------------------------------------------------
def arbitrate(
    proposal: Proposal,
    fact: Fact,
    candidates: Sequence[Candidate],
    policy: Policy | None = None,
) -> Decision:
    """把模型的建议压成一条确定性的动作。

    判定顺序即优先级，每条拒绝路径都写明 reason，便于线上按 reason 聚合看
    到底是哪条护栏在起作用（这个直方图比召回质量指标更早发现问题）。
    """
    pol = policy or Policy()
    model_action = (proposal.action or "").strip().lower()

    # R0 不认识的动词 —— 模型跑飞了，别猜
    if model_action not in ACTIONS:
        return Decision(
            action="drop",
            content=fact.content,
            reason="invalid_action",
            needs_review=True,
            model_action=model_action or None,
        )

    # R1 模型自己认为没价值
    if model_action == "drop":
        return Decision(
            action="drop", content=fact.content, reason="model_drop", model_action="drop"
        )

    # 写入门槛按类型区分：长期层最严，情景事件宽松
    min_conf = pol.min_write_confidence(fact.kind)
    if fact.confidence < min_conf:
        return Decision(
            action="drop",
            content=fact.content,
            reason=f"below_write_threshold({fact.kind})",
            needs_review=False,
            model_action=model_action,
        )

    # R2 accept —— 全新内容
    if model_action == "accept":
        dup = _find_exact_duplicate(fact.content, candidates)
        if dup is not None:
            # 内容逐字相同：没必要新建，转为强化（也顺手做了队列重试的幂等）
            return _merge(dup, fact, pol, reason="accept_deduped_to_merge", model_action="accept")
        return Decision(
            action="accept", content=fact.content, reason="model_accept", model_action="accept"
        )

    # R3 merge / defer 都需要一个可验证的目标
    target = _lookup(proposal.memory_id, candidates)
    if target is None:
        # 模型编了一个不存在的 id —— 幻觉最常见的形状。内容本身可能仍然有价值，
        # 所以不丢，转为 accept 但标记待复核（宁可多一条待看，不可静默丢信息）。
        return Decision(
            action="accept",
            content=fact.content,
            reason="unknown_target",
            needs_review=True,
            model_action=model_action,
            downgraded=True,
        )

    # 人工写入的记忆不接受模型的自动改动：既不抬置信度，也不被取代
    if target.source in pol.protected_sources:
        return Decision(
            action="drop",
            content=fact.content,
            target_id=target.id,
            reason="protected_source",
            needs_review=True,
            model_action=model_action,
        )

    if model_action == "merge":
        # 弱证据不许抬高强记忆：0.95 的旧条目不能被 0.6 的推断"强化"到 1.0
        if target.confidence - fact.confidence >= pol.confidence_gap:
            return Decision(
                action="drop",
                content=fact.content,
                target_id=target.id,
                reason="merge_confidence_gap",
                needs_review=True,
                model_action="merge",
            )
        return _merge(target, fact, pol, reason="model_merge", model_action="merge")

    # model_action == "defer"
    if normalize(target.content) == normalize(fact.content):
        # 内容相同就没有矛盾可挂起，直接强化
        return _merge(target, fact, pol, reason="defer_no_conflict", model_action="defer")
    return Decision(
        action="defer",
        content=fact.content,
        target_id=target.id,
        reason="model_defer",
        needs_review=True,
        model_action="defer",
    )


def _merge(
    target: Candidate, fact: Fact, pol: Policy, *, reason: str, model_action: str
) -> Decision:
    """merge 的落库动作：不新建行，只抬置信度/重要性。"""
    # 复述的价值和"新信息量"成反比：完全相同的复述只给最小增量
    same = normalize(target.content) == normalize(fact.content)
    conf_delta = pol.merge_confidence_boost * (0.5 if same else 1.0)
    imp_delta = pol.merge_importance_boost * (0.5 if same else 1.0)
    return Decision(
        action="merge",
        content=fact.content,
        target_id=target.id,
        reason=reason,
        model_action=model_action,
        confidence_delta=conf_delta,
        importance_delta=imp_delta,
    )


# ---------------------------------------------------------------------------
# 解析与查找
# ---------------------------------------------------------------------------
def _lookup(memory_id: str | None, candidates: Sequence[Candidate]) -> Candidate | None:
    """按 id 精确查找。找不到就是找不到 —— 不用相似度兜底猜一个。"""
    if not memory_id:
        return None
    wanted = str(memory_id).strip().lower()
    for c in candidates:
        if str(c.id).strip().lower() == wanted:
            return c
    return None


def _find_exact_duplicate(
    content: str, candidates: Sequence[Candidate]
) -> Candidate | None:
    key = normalize(content)
    if not key:
        return None
    for c in candidates:
        if normalize(c.content) == key:
            return c
    return None


def parse_proposal(payload: object) -> Proposal:
    """把模型返回值解析成 Proposal。任何形状错误都退化成 drop。

    模型一定会给你意外的东西：null、数组、带 markdown 围栏的字符串、大小写混写、
    多余字段、以及旧版词汇（ADD/UPDATE/DELETE/NONE）。解析器是防线，不是格式化工具。
    """
    if not isinstance(payload, dict):
        return Proposal(action="drop", reason="not_an_object", raw={})

    action = payload.get("action") or payload.get("state")
    if isinstance(action, str):
        action = action.strip().lower()
        # 兼容旧词汇，免得换了提示词之后整条管线静默瘫痪
        action = _LEGACY_ACTIONS.get(action, action)
    else:
        action = "drop"

    memory_id = payload.get("memory_id")
    memory_id = str(memory_id).strip() if memory_id not in (None, "") else None

    new_content = payload.get("new_content")
    new_content = new_content.strip() if isinstance(new_content, str) else None

    reason = payload.get("reason")
    reason = reason.strip()[:200] if isinstance(reason, str) else None

    if action not in ACTIONS:
        return Proposal(action="drop", reason="invalid_action", raw=payload)

    return Proposal(
        action=action,
        memory_id=memory_id,
        new_content=new_content or None,
        reason=reason,
        raw=payload,
    )


# 旧词汇 → 新四态。ADD/UPDATE 都归 accept（"要写入一条新的/替换的"），
# NONE 归 drop，DELETE 归 defer（矛盾需要裁决，不能直接删）。
_LEGACY_ACTIONS = {
    "add": "accept",
    "update": "accept",
    "delete": "defer",
    "none": "drop",
}
