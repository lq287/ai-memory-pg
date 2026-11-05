"""集中参数。

所有旋钮只在这里定义一次，SQL 与 Python 侧都从这里取，避免出现
"配置里写了 ef_search=40、函数里默认 100" 这种查不出来的事故。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class RecallParams:
    """一次召回的参数。默认值来自设计稿标定过的参考配置。"""

    # 召回宽度
    vec_k: int = 50
    kw_k: int = 50
    # 融合
    rrf_k: float = 60.0
    # 打分：高斯时间衰减（offset=180d 内不衰减；offset+scale 处降到 decay 倍）
    decay_offset_days: float = 180.0
    decay_scale_days: float = 1825.0
    decay: float = 0.5
    # 使用频率：1 + w * log10(1 + use_count)，log10 保证高频条目不会无限膨胀
    freq_weight: float = 0.2
    # 返回
    limit: int = 8
    # 重要性权重：0 = 不参与打分，1 = 完全按 importance 缩放。
    #
    # 默认值从 0.5 调到 0.25，是**实测标定**出来的，不是偏好：
    # RRF 的分数带极窄 —— 1/(60+名次)，rank1 与 rank3 只差 3.2%、与 rank4 差 4.7%。
    # 而乘数 (1-w+w·importance) 在 w=0.5 时对 importance 0.6 vs 0.8 就有 12.5% 的摆幅：
    # 一个"我手写时给了 0.8"的技术笔记能稳定压过 rank1 的正确记忆。
    # 校正原则：**乘数摆幅必须小于相邻名次差**，否则它不是微调而是主导。
    # w=0.25 时同样的 0.2 差距只有 5% 摆幅，落在 RRF 的一个名次台阶内。
    importance_weight: float = 0.25
    # 标签过滤（NULL/空 = 不过滤），走 idx_ep_tags 的 GIN
    tags: tuple[str, ...] | None = None
    # 待裁决的 defer 行默认不注入（它们还没被裁定为真）
    include_pending: bool = False
    # HNSW 查询期参数（不写死在配置里，每次查询计算）
    ef_search: int | None = None
    # 每次注入记忆的 token 硬上限（预算封顶，由 retrieve.render 执行截断）
    token_budget: int = 800
    # 第三路：图扩展。默认开，但用户没有任何实体/关系时它是空集 ——
    # 那时 RRF 第三路贡献 0，行为与两路召唤完全一致。要有图数据才谈得上"变好"。
    enable_graph: bool = True
    # 2 跳足够覆盖"同集团兄弟公司"；3 跳开始把一切连到一切
    graph_depth: int = 2
    graph_limit: int = 30

    def resolved_ef_search(self) -> int:
        """ef_search 必须 >= 召回宽度，否则 HNSW 图都没走完就停了。

        经验公式 4 * limit，下限取 vec_k。配合 SQL 里的 hnsw.iterative_scan
        （pgvector >= 0.8）应对 user_id 过滤导致的候选塌方。
        """
        if self.ef_search is not None:
            return max(self.ef_search, self.limit)
        return max(4 * self.limit, self.vec_k)


@dataclass(frozen=True, slots=True)
class FactsParams:
    """长期事实的取用参数。衰减口径与情景记忆不同：按 last_used_at。"""

    limit: int = 20
    decay_offset_days: float = 30.0
    decay_scale_days: float = 365.0
    decay: float = 0.5
    token_budget: int = 300


# 复杂度启发式：这些词出现时通常是"回顾/对比/多跳"类问题，需要更多上下文。
_COMPLEX_HINTS = (
    "为什么", "怎么", "如何", "对比", "比较", "区别", "差异",
    "上次", "之前", "那次", "当时", "以前", "历史",
    "哪些", "分别", "都有", "所有", "总结", "梳理",
)
_SIMPLE_LIMIT = 2
_COMPLEX_LIMIT = 8


def plan_recall(query: str, base: RecallParams | None = None) -> RecallParams:
    """按查询复杂度决定召回条数与预算。

    这是个**占位启发式**，不是调优结论：先用它把 1~2 条 vs 5~10 条的差别跑出来，
    攒够真实查询后再用日志里的 rk_v/rk_k/score 反过来定阈值。
    唯一确定的是：不要所有查询都返回同样多。
    """
    p = base or RecallParams()
    q = (query or "").strip()
    complex_ = len(q) >= 16 or any(h in q for h in _COMPLEX_HINTS)
    if complex_:
        return replace(p, limit=_COMPLEX_LIMIT, token_budget=max(p.token_budget, 1200))
    return replace(p, limit=_SIMPLE_LIMIT, token_budget=min(p.token_budget, 400))


@dataclass(frozen=True, slots=True)
class Policy:
    """写入裁决的确定性护栏。见 decide.py 的判定表。"""

    # 这些来源的记忆不接受模型的自动改动（人工写入 = 用户自己说的）
    protected_sources: tuple[str, ...] = ("human",)
    # 弱证据不许抬高/取代强记忆的阈值
    confidence_gap: float = 0.15
    # 按记忆类型区分的写入门槛：长期层最严，情景事件宽松
    write_threshold_episode: float = 0.35
    write_threshold_longterm: float = 0.80
    write_threshold_raw_turn: float = 0.20
    write_threshold_summary: float = 0.50
    # merge 时的治理信号增量（有上限 LEAST(1.0, ...) 兜底）
    merge_confidence_boost: float = 0.05
    merge_importance_boost: float = 0.02
    # 是否信任模型重写的 new_content。默认关：落库用抽取阶段的原文
    trust_llm_rewrite: bool = False

    def min_write_confidence(self, kind: str) -> float:
        return {
            "raw_turn": self.write_threshold_raw_turn,
            "episode": self.write_threshold_episode,
            "summary": self.write_threshold_summary,
            "longterm": self.write_threshold_longterm,
        }.get(kind, self.write_threshold_episode)


@dataclass(frozen=True, slots=True)
class Settings:
    dsn: str
    embed_model: str
    embed_dim: int
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    worker_id: str
    batch_size: int
    lease_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            dsn=os.environ.get(
                "MEM_PG_DSN", "postgresql://mem:mem@127.0.0.1:55432/mem"
            ),
            embed_model=os.environ.get("MEM_EMBED_MODEL", "hash-768"),
            embed_dim=int(os.environ.get("MEM_EMBED_DIM", "768")),
            llm_base_url=os.environ.get("MEM_LLM_BASE_URL", ""),
            llm_api_key=os.environ.get("MEM_LLM_API_KEY", ""),
            llm_model=os.environ.get("MEM_LLM_MODEL", "gpt-4o-mini"),
            worker_id=os.environ.get("MEM_WORKER_ID", f"w-{os.getpid()}"),
            batch_size=int(os.environ.get("MEM_BATCH_SIZE", "10")),
            lease_seconds=int(os.environ.get("MEM_LEASE_SECONDS", "300")),
        )
