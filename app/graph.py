"""图扩展层（第三路召回）的 Python 侧。

这一层回答的是"关系型问题"：
    "这个客户所属集团下其他子公司是怎么处理的？"
向量和词法都会答错，因为问题里没有出现在记忆里的字面词；能答对的是关系。

设计取向（与 sql/06_graph.sql 一致）：
  · **不造实体**。实体只有两个来源：显式声明（`mem.py graph add` / 写记忆时 `--entity`），
    或从已有记忆里认出**库里已知**的实体名（`auto_link`）。绝不做 NER 猜新实体 ——
    猜错的实体比没有实体更糟：它会把不相关的记忆用"关系"粘在一起。
  · **不删关系**。relations 也走双时态（valid_until IS NULL = 当前有效），
    和 episodes 一套口径，历史可回溯。
  · **可选、可关**。召回默认开启，但用户没有图数据时它就是空集 ——
    RRF 里第三路贡献 0，行为与两路召回完全一致。要有图数据才谈得上"变好"。
  · **不拖死写入**。挂图是 best-effort：图操作失败只记日志，绝不让一次记忆写入失败。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Sequence

if TYPE_CHECKING:  # 只为类型；运行时不 import，避免 store ↔ graph 循环
    from .store import Store

log = logging.getLogger("memory.graph")

# 谓词白名单在 SQL 里是 CHECK (predicate ~ '^[a-z_]{2,32}$')，这里再挡一层，
# 只是为了给出比 PG 报错更好读的提示。
KNOWN_LABELS = ("person", "org", "project", "system", "place", "concept")


@dataclass
class GraphParams:
    """图路径的召回参数。"""

    enabled: bool = True
    # 2 跳足够覆盖"同一集团下的兄弟公司"这类问题；3 跳开始把一切连到一切
    depth: int = 2
    limit: int = 30
    # 关闭时 recall 完全不多花一次查询
    @classmethod
    def off(cls) -> "GraphParams":
        return cls(enabled=False)


@dataclass
class GraphTrace:
    """图路径干了什么 —— 召回结果要能自证，不能只是"分数变了"。"""

    seeds: list[str] = field(default_factory=list)
    episodes: int = 0
    via: list[dict[str, Any]] = field(default_factory=list)

    @property
    def used(self) -> bool:
        return bool(self.seeds and self.episodes)

    def describe(self) -> str:
        if not self.seeds:
            return "图: 未命中种子实体（跳过）"
        if not self.episodes:
            return f"图: 种子 {self.seeds} 无可达记忆"
        lines = [
            f"图: 种子 {self.seeds} → {self.episodes} 条记忆（深度 ≤ "
            f"{max((v['depth'] for v in self.via), default=0)}）"
        ]
        for v in self.via[:5]:
            lines.append(f"    d{v['depth']} via {v['via_entity']}")
        return "\n".join(lines)


class GraphMemory:
    """实体 / 关系 / 记忆挂载 的读写入口。"""

    def __init__(self, store: "Store"):
        self.store = store

    # --- 写入 ---------------------------------------------------------------
    def upsert_entity(
        self,
        user_id: str,
        display: str,
        label: str = "concept",
        aliases: Sequence[str] = (),
    ) -> str:
        if label not in KNOWN_LABELS:
            raise ValueError(f"label 必须是 {KNOWN_LABELS} 之一，收到 {label!r}")
        row = self.store._read(
            "SELECT graph_upsert_entity(%s::text, %s::text, %s::text, %s::text[]) AS id",
            (user_id, display, label, list(aliases) or None),
        )
        return str(row[0]["id"])

    def relate(
        self,
        user_id: str,
        src: str,
        predicate: str,
        dst: str,
        confidence: float = 0.8,
        source_episode: str | None = None,
    ) -> str:
        pred = predicate.strip().lower()
        if not pred.replace("_", "").isalnum() or not (2 <= len(pred) <= 32):
            raise ValueError(f"谓词只能用 a-z0-9_、长度 2..32，收到 {predicate!r}")
        if src.strip().lower() == dst.strip().lower():
            raise ValueError("自环关系没有意义：src 与 dst 是同一个实体")
        row = self.store._read(
            "SELECT graph_relate(%s::text, %s::text, %s::text, %s::text,"
            " %s::real, %s::uuid) AS id",
            (user_id, src, pred, dst, float(confidence), source_episode),
        )
        return str(row[0]["id"])

    def link_episode(self, episode_id: str, names: Iterable[str]) -> int:
        """把一条记忆挂到若干**已存在**的实体上。返回实际新增的边数。"""
        names = [n for n in (str(x).strip() for x in names) if n]
        if not names:
            return 0
        row = self.store._read(
            "SELECT graph_link_episode(%s::uuid, %s::text[]) AS n",
            (episode_id, names),
        )
        return int(row[0]["n"])

    def auto_link(self, episode_id: str, user_id: str, text: str) -> int:
        """从记忆正文里认出**库里已有**的实体并挂上。best-effort，永不抛。"""
        try:
            found = self.entities_in_text(user_id, text)
            if not found:
                return 0
            return self.link_episode(episode_id, [e["display"] for e in found])
        except Exception as exc:  # 图挂了不能连累记忆写入
            log.warning("auto_link 失败（已忽略）: %s", exc)
            return 0

    # --- 查询 ---------------------------------------------------------------
    def entities_in_text(self, user_id: str, text: str) -> list[dict]:
        if not text or not text.strip():
            return []
        return self.store._read(
            "SELECT * FROM graph_entities_in_text(%s::text, %s::text)",
            (user_id, text),
        )

    def neighbors(
        self, user_id: str, seeds: Sequence[str], depth: int = 2, limit: int = 200
    ) -> list[dict]:
        if not seeds:
            return []
        return self.store._read(
            "SELECT * FROM graph_neighbors(%s::text, %s::text[], %s::int, %s::int)",
            (user_id, list(seeds), int(depth), int(limit)),
        )

    def episodes(
        self, user_id: str, seeds: Sequence[str], depth: int = 2, limit: int = 50
    ) -> list[dict]:
        if not seeds:
            return []
        return self.store._read(
            "SELECT * FROM graph_episodes(%s::text, %s::text[], %s::int, %s::int)",
            (user_id, list(seeds), int(depth), int(limit)),
        )

    def stats(self, user_id: str) -> dict:
        row = self.store._read(
            "SELECT * FROM graph_stats(%s::text)", (user_id,)
        )
        return row[0] if row else {"entities": 0, "relations_live": 0, "linked_episodes": 0}

    def entities(self, user_id: str, limit: int = 50) -> list[dict]:
        return self.store._read(
            """SELECT id, name, display, label, aliases FROM entities
               WHERE user_id = %s ORDER BY label, name LIMIT %s""",
            (user_id, limit),
        )

    def relations(self, user_id: str, limit: int = 50) -> list[dict]:
        return self.store._read(
            """SELECT r.id, s.display AS src, r.predicate, d.display AS dst,
                      r.confidence, r.valid_from, r.valid_until
               FROM relations r
               JOIN entities s ON s.id = r.src_id
               JOIN entities d ON d.id = r.dst_id
               WHERE r.user_id = %s AND r.valid_until IS NULL
               ORDER BY s.display, r.predicate, d.display LIMIT %s""",
            (user_id, limit),
        )

    # --- 召回路径 -----------------------------------------------------------
    def seeds_for_query(self, user_id: str, query: str) -> list[str]:
        """查询里出现了**已知实体**才当种子。没有就不开图路。"""
        return [e["display"] for e in self.entities_in_text(user_id, query)]

    def route(
        self, user_id: str, query: str, params: GraphParams | None = None
    ) -> tuple[list[str], GraphTrace]:
        """给 recall 用的第三路：返回 (记忆 id 列表, 轨迹)。

        id 的顺序即图排序（深度优先、同深度按时间新→旧），
        它直接变成 recall_episodes 里的 rk_g。
        """
        p = params or GraphParams()
        trace = GraphTrace()
        if not p.enabled or not query.strip():
            return [], trace

        seeds = self.seeds_for_query(user_id, query)
        trace.seeds = seeds
        if not seeds:
            return [], trace

        hits = self.episodes(user_id, seeds, p.depth, p.limit)
        trace.episodes = len(hits)
        trace.via = [
            {"depth": h["depth"], "via_entity": h["via_entity"], "id": str(h["episode_id"])}
            for h in hits
        ]
        return [str(h["episode_id"]) for h in hits], trace
