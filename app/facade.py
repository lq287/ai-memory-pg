"""记忆库的门面（facade）：把"一次记忆操作"打包成一个方法。

存在的理由：现在有两个前端要调同一套逻辑 ——
  · `scripts/mem.py`（命令行，人用）
  · `app/sidecar.py`（长驻 JSON-lines 服务，DSH 插件用）
两边如果各自拼参数、各自决定默认值，就会漂移成两套语义（这个项目里已经因为
"两处各自对齐 embedding 模型"翻过车）。所以：**参数默认值与返回结构只在这里定义一次**，
CLI 和插件都调这里。

设计约束：
  · 所有方法只做一件事，返回**可直接 JSON 化**的 dict（插件要把它塞进工具结果）。
  · 不打印、不 sys.exit —— 调用方决定怎么呈现。
  · 写操作的幂等/护栏仍由 SQL 与 app.store 负责，这里不重复实现。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

from .config import FactsParams, Policy, RecallParams
from .decide import Fact, arbitrate, parse_proposal
from .digest import PERIODS, DigestBuilder, default_range
from .store import Store


def _jsonable(value: Any) -> Any:
    """把 date/datetime/Decimal/UUID 之类转成 JSON 友好的类型。

    这个函数存在的理由很具体：sidecar 的响应必须能被 `json.dumps` 序列化，
    否则整个请求会变成一条 "Object of type UUID is not JSON serializable" 的错误 ——
    插件端看起来像随机故障。所以**未知对象一律退化成 str**，宁可丢结构也不丢响应。
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, uuid.UUID):
        return str(value)
    try:
        import decimal

        if isinstance(value, decimal.Decimal):
            return float(value)
    except Exception:  # pragma: no cover
        pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if not isinstance(value, (str, int, float, bool, bytes, type(None))):
        return str(value)
    return value


def _rows(rows: Iterable[dict]) -> list[dict]:
    return [{k: _jsonable(v) for k, v in r.items()} for r in rows]


@dataclass
class AddResult:
    action: str
    reason: str
    id: str | None = None
    applied: bool = False
    expires_at: str | None = None


class Memory:
    """一个已连接的记忆库门面。生命周期由调用方管（CLI 一次一进程，sidecar 常驻）。"""

    def __init__(self, store: Store, user_id: str = "owner") -> None:
        self.store = store
        self.user = user_id

    # ------------------------------------------------------------------ 读
    def health(self) -> dict:
        h = self.store.health(self.user)
        graph = {}
        try:
            from .graph import GraphMemory

            graph = GraphMemory(self.store).stats(self.user)
        except Exception:
            pass
        facts = self.store._read(
            "SELECT count(*) AS n FROM facts WHERE user_id=%s AND valid_until IS NULL",
            (self.user,),
        )[0]["n"]
        eps = self.store._read(
            "SELECT count(*) AS n FROM episodes WHERE user_id=%s AND valid_until IS NULL",
            (self.user,),
        )[0]["n"]
        return {
            "user": self.user,
            "episodes_live": int(eps),
            "facts_live": int(facts),
            "quality": h,
            "graph": {k: int(v) for k, v in (graph or {}).items() if isinstance(v, (int, float))},
            "embedder": self.store.embedder.model,
        }

    def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        tags: Sequence[str] | None = None,
        include_full: bool = False,
        use_graph: bool = True,
        mark_used: bool = False,
        min_score: float = 0.0,
    ) -> dict:
        """三路召回。返回 summary 层；include_full 才取全文（渐进披露第二层）。"""
        params = RecallParams(
            limit=max(1, int(limit)),
            tags=tuple(tags) if tags else None,
            enable_graph=bool(use_graph),
        )
        rows, _vec, trace = self.store.recall(self.user, query, params, with_trace=True)
        hits = []
        for r in rows:
            score = float(r["score"])
            if score < min_score:
                continue
            paths = ("v" if r["rk_v"] else "-") + ("k" if r["rk_k"] else "-") + (
                "g" if r["rk_g"] else "-"
            )
            hits.append(
                {
                    "id": str(r["id"]),
                    "score": round(score, 6),
                    "paths": paths,
                    "summary": r["summary"] or r["snippet"],
                    "event_time": _jsonable(r["event_time"]),
                    "importance": float(r["importance"]),
                    "confidence": float(r["confidence"]),
                }
            )
        if include_full and hits:
            full = {str(x["id"]): x["content"] for x in self.store.expand([h["id"] for h in hits])}
            for h in hits:
                h["content"] = full.get(h["id"], "")
        if mark_used and hits:
            self.store.bump_usage([h["id"] for h in hits])
        return {
            "query": query,
            "hits": hits,
            "graph": {
                "seeds": trace.seeds if trace else [],
                "episodes": trace.episodes if trace else 0,
            },
        }

    def context(
        self,
        *,
        max_chars: int = 1200,
        fact_limit: int = 12,
        with_tasks: bool = True,
        with_conflicts: bool = False,
    ) -> dict:
        """会话前言：事实 + 近期时段摘要 + 进行中任务（+ 待裁决冲突）。"""
        out: list[str] = []

        facts = self.store.recall_facts(self.user, None, FactsParams(limit=fact_limit))
        if facts:
            out.append("[事实]")
            for f in facts:
                val = json.dumps(_jsonable(f["value"]), ensure_ascii=False)
                out.append(f"- {f['key']} = {val}")

        dg = DigestBuilder(self.store)
        for period in ("week", "day"):
            rows = [r for r in dg.recent(self.user, period, 2) if r["episode_count"]]
            if rows:
                out.append(f"[{period}]")
                for r in rows:
                    out.append(f"- {r['bucket_start']} {r['summary'][:100]}")

        if with_tasks:
            tasks = self.store._read(
                "SELECT * FROM tasks_live(%s::text, %s::int)", (self.user, 5)
            )
            if tasks:
                out.append("[进行中]")
                for t in tasks:
                    out.append(f"- {t['kind']}/{t['status']} {t['title']} ({t['progress']:.0%})")

        if with_conflicts:
            n = self.store._read(
                "SELECT count(*) AS n FROM memory_conflicts "
                "WHERE user_id=%s AND status='open'",
                (self.user,),
            )[0]["n"]
            if n:
                out.append(f"[待裁决] {n} 条冲突")

        text = "\n".join(out)
        truncated = False
        if max_chars and len(text) > max_chars:
            text = text[:max_chars].rsplit("\n", 1)[0] + "\n…（已截断）"
            truncated = True
        return {"text": text, "empty": not bool(text.strip()), "truncated": truncated}

    def fact_get(self, keys: Sequence[str] | None = None, *, limit: int = 50) -> dict:
        rows = self.store.recall_facts(self.user, list(keys) if keys else None,
                                       FactsParams(limit=limit))
        return {"facts": _rows(rows)}

    def list_recent(self, *, limit: int = 20) -> dict:
        rows = self.store.list_episodes(self.user, limit)
        return {"episodes": _rows(rows)}

    def digest_show(self, period: str | None = None, *, limit: int = 10) -> dict:
        rows = DigestBuilder(self.store).recent(self.user, period, limit)
        return {"digests": _rows(rows)}

    def graph_route(self, query: str, *, depth: int = 2, limit: int = 30) -> dict:
        from .graph import GraphMemory, GraphParams

        g = GraphMemory(self.store)
        ids, trace = g.route(
            self.user, query, GraphParams(depth=depth, limit=limit)
        )
        picks = []
        if ids:
            for r in self.store.expand(ids):
                picks.append(
                    {"id": str(r["id"]), "summary": r["summary"], "content": (r["content"] or "")[:300]}
                )
        return {"trace": trace.describe(), "seeds": trace.seeds, "episodes": picks}

    # ------------------------------------------------------------------ 写
    def add(
        self,
        text: str,
        *,
        kind: str = "episode",
        confidence: float = 0.9,
        importance: float = 0.6,
        source: str = "conversation",
        summary: str | None = None,
        tags: Sequence[str] | None = None,
        entities: Sequence[str] | None = None,
        action: str = "accept",
        target: str | None = None,
        max_dist: float = 0.35,
    ) -> dict:
        """写一条记忆：走完整四态裁决链路（不是裸 INSERT）。"""
        fact = Fact(
            user_id=self.user,
            content=text,
            confidence=float(confidence),
            source=source,
            event_time=datetime.now(timezone.utc),
            kind=kind,
            event_time_source="ingest",
            summary=(summary or text)[:120],
            importance=float(importance),
            tags=tuple(tags or ()),
        )
        vec = self.store.embedder.embed([fact.content])[0]
        cands = self.store.conflict_candidates(self.user, vec, k=5, max_dist=max_dist)
        proposal = parse_proposal({"action": action, "memory_id": target})
        decision = arbitrate(proposal, fact, cands, Policy())
        result = self.store.apply(
            decision, fact, vec,
            source_ref=f"plugin:{uuid.uuid4().hex[:12]}",
            entities=tuple(entities or ()),
        )
        return {
            "action": decision.action,
            "reason": decision.reason,
            "applied": bool(result.get("applied")),
            "id": str(result.get("id")) if result.get("id") else None,
            "cascade": result.get("reason"),
        }

    def fact_set(
        self,
        key: str,
        value: Any,
        *,
        confidence: float = 0.9,
        importance: float = 0.6,
        source: str = "human",
    ) -> dict:
        fid = self.store.set_fact(
            self.user, key.strip().lower(), value,
            float(confidence), source, importance=float(importance),
        )
        return {"id": str(fid) if fid else None, "key": key, "value": _jsonable(value)}

    def digest_build(
        self,
        period: str = "week",
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        top: int = 12,
        min_count: int = 2,
        periods: Sequence[str] | None = None,
    ) -> dict:
        if period != "all" and period not in PERIODS:
            raise ValueError(f"period 必须是 {PERIODS} 之一或 'all'")
        b = DigestBuilder(self.store)
        targets = list(periods) if periods else (
            list(PERIODS) if period == "all" else [period]
        )
        built = []
        for p in targets:
            if from_date:
                start = date.fromisoformat(from_date)
                end = (date.fromisoformat(to_date) + __import__("datetime").timedelta(days=1)) \
                    if to_date else start + __import__("datetime").timedelta(days=1)
            else:
                start, end = default_range(p)
            for r in b.build_range(self.user, p, start, end, top_n=int(top),
                                   min_count=int(min_count)):
                if r.skipped_reason:
                    continue
                built.append(
                    {
                        "period": r.period,
                        "label": r.label,
                        "bucket_start": r.bucket_start.isoformat(),
                        "episode_count": r.episode_count,
                        "keywords": list(r.keywords),
                    }
                )
        return {"built": built, "count": len(built)}

    def forget(self, episode_id: str, reason: str | None = None) -> dict:
        return {"ok": bool(self.store.forget(episode_id, reason))}

    def restore(self, episode_id: str) -> dict:
        return {"ok": bool(self.store.restore(episode_id))}


__all__ = ["Memory", "AddResult", "PERIODS"]
