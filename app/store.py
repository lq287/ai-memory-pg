"""Postgres 读写层。

SQL 与 sql/*.sql 一一对应，那边是权威版本（带注释解释为什么这么写），
这里是可调用的副本。改动请两边同步 —— 这是本项目唯一允许的重复。

依赖：psycopg>=3.1、pgvector>=0.3（只为注册 list[float] -> vector 的适配器）。

两条从真库踩出来的铁律，写在这里免得后人重踩：
  1. 向量参数必须显式 ::vector。psycopg 把 list[float] 编成 double precision[]，
     没有 cast 时 `embedding <=> $1` 直接 UndefinedFunction。
  2. 任何语句失败后连接会停在 aborted 事务里，之后**所有**语句都报
     InFailedSqlTransaction —— 连"记录这次失败"都会失败。所以读写一律走
     _read()/_write()，异常必回滚。
"""
from __future__ import annotations

import contextlib
import json
import logging
import uuid
from typing import Any, Iterator, Sequence

from .config import FactsParams, RecallParams
from .decide import Candidate, Decision, Fact
from .embedding import Embedder

log = logging.getLogger(__name__)

# --- 队列 -------------------------------------------------------------------
SQL_CLAIM = """
WITH next_batch AS (
    SELECT id FROM memory_jobs
    WHERE status = 'pending' AND run_at <= now()
    ORDER BY run_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT %s
)
UPDATE memory_jobs j
SET status = 'processing', locked_at = now(), locked_by = %s,
    attempts = j.attempts + 1, updated_at = now()
FROM next_batch b
WHERE j.id = b.id
RETURNING j.id, j.user_id, j.job_type, j.payload, j.attempts, j.max_attempts
"""

SQL_ACK = """
UPDATE memory_jobs
SET status = 'done', finished_at = now(), updated_at = now(),
    locked_at = NULL, locked_by = NULL, last_error = NULL
WHERE id = %s AND status = 'processing'
RETURNING id
"""

SQL_FAIL = """
UPDATE memory_jobs
SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
    run_at = CASE WHEN attempts >= max_attempts
                  THEN run_at
                  ELSE now() + make_interval(secs => LEAST(300, (5 * (2 ^ attempts))::int))
             END,
    finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END,
    locked_at = NULL, locked_by = NULL,
    last_error = left(%s, 2000), updated_at = now()
WHERE id = %s
RETURNING id, status, attempts
"""

SQL_STATS = """
SELECT status, count(*) AS jobs,
       count(*) FILTER (WHERE status = 'pending' AND run_at < now() - interval '5 min') AS overdue
FROM memory_jobs GROUP BY status ORDER BY status
"""

# --- 写入 -------------------------------------------------------------------
# expires_at 故意留空：触发器按 kind 从 ttl_interval() 算出，任何路径都不会漏
SQL_INSERT_EPISODE = """
INSERT INTO episodes (
    user_id, kind, content, summary, embedding, embedding_model,
    event_time, event_time_source, source, source_ref,
    confidence, importance, review_state, tags
) VALUES (
    %(user_id)s, %(kind)s, %(content)s, %(summary)s, %(embedding)s::vector,
    %(embedding_model)s, %(event_time)s, %(event_time_source)s, %(source)s,
    %(source_ref)s, %(confidence)s, %(importance)s, %(review_state)s, %(tags)s
)
ON CONFLICT DO NOTHING
RETURNING id, expires_at, lifecycle_state
"""

SQL_REINFORCE = """
UPDATE episodes
SET confidence = LEAST(1.0, confidence + %s),
    importance = LEAST(1.0, importance + %s),
    use_count  = use_count + 1,
    last_used_at = now()
WHERE id = %s AND valid_until IS NULL
RETURNING id, confidence, importance, use_count
"""

SQL_SUPERSEDE_EPISODE = """
UPDATE episodes
SET valid_until = now(), superseded_by = %s
WHERE id = %s AND valid_until IS NULL
RETURNING id
"""

SQL_OPEN_CONFLICT = """
INSERT INTO memory_conflicts (id, user_id, existing_id, incoming_id, reason, signal)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
RETURNING id
"""

# 裁决候选：纯相似度，不带衰减/频率。阈值 ≈ 余弦相似度 0.65 以上
SQL_CONFLICT_CANDIDATES = """
SELECT id, content, confidence, importance, source, use_count,
       embedding <=> %(vec)s::vector AS dist
FROM episodes
WHERE user_id = %(user_id)s
  AND valid_until IS NULL
  AND embedding IS NOT NULL
  AND (embedding <=> %(vec)s::vector) < %(max_dist)s
ORDER BY embedding <=> %(vec)s::vector
LIMIT %(k)s
"""

# --- 读取 -------------------------------------------------------------------
SQL_RECALL = """
SELECT * FROM recall_episodes(
    %(vec)s::vector, %(user_id)s, %(query)s,
    %(vec_k)s, %(kw_k)s, %(rrf_k)s,
    %(decay_offset)s, %(decay_scale)s, %(decay)s,
    %(freq)s, %(limit)s, %(ef)s, %(include_pending)s,
    %(imp_weight)s, %(tags)s::text[], %(graph_ids)s::uuid[]
)
"""

SQL_RECALL_FACTS = """
SELECT * FROM recall_facts(
    %(user_id)s, %(keys)s, %(limit)s,
    %(decay_offset)s, %(decay_scale)s, %(decay)s
)
"""

SQL_EXPAND = """
SELECT id, content, summary, event_time, kind, source, confidence
FROM episodes WHERE id = ANY(%s)
"""

SQL_BUMP = """
UPDATE episodes
SET use_count = use_count + 1, last_used_at = now()
WHERE id = ANY(%s)
RETURNING id, use_count
"""

SQL_BUMP_FACTS = """
UPDATE facts
SET use_count = use_count + 1, last_used_at = now()
WHERE user_id = %s AND valid_until IS NULL AND key = ANY(%s)
RETURNING key, use_count
"""

SQL_SUPERSEDE_FACT = """
UPDATE facts
SET valid_until = now(), superseded_by = %s
WHERE user_id = %s AND key = %s AND valid_until IS NULL
RETURNING id
"""

SQL_INSERT_FACT = """
INSERT INTO facts (id, user_id, key, value, confidence, importance, source)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
RETURNING id
"""

SQL_PENDING_CONFLICTS = """
SELECT c.id, c.created_at, c.reason, c.signal,
       old.id AS existing_id, old.content AS existing_content,
       old.confidence AS existing_confidence,
       new.id AS incoming_id, new.content AS incoming_content,
       new.confidence AS incoming_confidence
FROM memory_conflicts c
JOIN episodes old ON old.id = c.existing_id
JOIN episodes new ON new.id = c.incoming_id
WHERE c.status = 'open' AND (%s::text IS NULL OR c.user_id = %s)
ORDER BY c.created_at DESC
LIMIT %s
"""

SQL_HEALTH = """
SELECT
  (SELECT count(*) FROM episodes WHERE user_id = %s AND review_state = 'pending')     AS pending_review,
  (SELECT count(*) FROM episodes WHERE user_id = %s AND valid_until IS NULL
      AND expires_at IS NOT NULL AND expires_at <= now())                             AS expired_live,
  (SELECT count(*) FROM episodes WHERE user_id = %s AND valid_until IS NULL
      AND embedding IS NULL)                                                          AS missing_embedding,
  (SELECT count(*) FROM memory_conflicts WHERE user_id = %s AND status = 'open')      AS open_conflicts
"""


class Store:
    """单连接、单线程。worker 循环本身就是串行的，不需要连接池。

    注意 psycopg 连接不是线程安全的：要并发消费就每个线程一个 Store。
    """

    def __init__(self, dsn: str, embedder: Embedder) -> None:
        self.dsn = dsn
        self.embedder = embedder
        self._conn: Any = None

    # -- 生命周期 -----------------------------------------------------------
    def connect(self) -> "Store":
        import os

        import psycopg
        from pgvector.psycopg import register_vector
        from psycopg.rows import dict_row

        # 服务端预处理语句：psycopg3 默认执行 5 次后自动 PREPARE（真的省解析开销）。
        # 但 PGlite 的 wire 服务端模拟层不隔离会话级语句名，第二个连接会撞上
        # `prepared statement "_pg3_0" already exists`。开发/验证走 PGlite 时用
        # MEM_PG_NO_PREPARE=1 关掉；连真 PostgreSQL 时保持默认（不要为了兼容性
        # 牺牲生产性能）。
        no_prepare = os.environ.get("MEM_PG_NO_PREPARE", "").strip() in ("1", "true", "yes")
        self._conn = psycopg.connect(
            self.dsn,
            row_factory=dict_row,
            autocommit=False,
            prepare_threshold=None if no_prepare else 5,
        )
        register_vector(self._conn)
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Store":
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def conn(self) -> Any:
        if self._conn is None:
            raise RuntimeError("Store 未连接：先 connect() 或用 with Store(...) as s")
        return self._conn

    @contextlib.contextmanager
    def tx(self) -> Iterator[Any]:
        with self.conn.transaction():
            yield self.conn

    # -- 统一的事务收尾 -----------------------------------------------------
    def _reset(self) -> None:
        try:
            self.conn.rollback()
        except Exception:
            pass

    def _read(self, sql: str, params: Any = None) -> list[dict]:
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except Exception:
            self._reset()
            raise
        self.conn.commit()  # 释放快照，别留着 idle-in-transaction
        return rows

    def _write(self, sql: str, params: Any = None) -> dict | None:
        try:
            row = self.conn.execute(sql, params).fetchone()
        except Exception:
            self._reset()
            raise
        self.conn.commit()
        return row

    # -- 队列 ---------------------------------------------------------------
    def enqueue(
        self,
        user_id: str,
        job_type: str,
        payload: dict,
        dedup_key: str | None = None,
    ) -> int | None:
        """入队。返回 None 表示在途去重命中了（同一片段已在队列里）。"""
        row = self._write(
            """INSERT INTO memory_jobs (user_id, job_type, payload, dedup_key)
               VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id""",
            (user_id, job_type, json.dumps(payload, ensure_ascii=False), dedup_key),
        )
        return row["id"] if row else None

    def claim(self, limit: int, worker_id: str) -> list[dict]:
        return self._read(SQL_CLAIM, (limit, worker_id))

    def ack(self, job_id: int) -> None:
        self._write(SQL_ACK, (job_id,))

    def fail(self, job_id: int, error: str) -> str:
        # 先清掉上游可能留下的 aborted 事务，否则连"记一次失败"都做不到
        self._reset()
        row = self._write(SQL_FAIL, (error, job_id))
        return row["status"] if row else "unknown"

    def reap(self, lease_seconds: int = 300, batch: int = 500) -> tuple[int, int]:
        row = self._write("SELECT * FROM reap_stuck_jobs(%s, %s)", (lease_seconds, batch))
        return (row["reclaimed"], row["deadlettered"]) if row else (0, 0)

    def stats(self) -> list[dict]:
        return self._read(SQL_STATS)

    def health(self, user_id: str) -> dict:
        return self._read(SQL_HEALTH, (user_id,) * 4)[0]

    # -- 写入：四态裁决的执行 -----------------------------------------------
    def conflict_candidates(
        self, user_id: str, embedding: Sequence[float], k: int = 5, max_dist: float = 0.35
    ) -> list[Candidate]:
        rows = self._read(
            SQL_CONFLICT_CANDIDATES,
            {"vec": list(embedding), "user_id": user_id, "max_dist": max_dist, "k": k},
        )
        return [
            Candidate(
                id=str(r["id"]),
                content=r["content"],
                confidence=float(r["confidence"]),
                importance=float(r["importance"]),
                source=r["source"],
                use_count=int(r["use_count"]),
                distance=float(r["dist"]) if r["dist"] is not None else None,
            )
            for r in rows
        ]

    def apply(
        self,
        decision: Decision,
        fact: Fact,
        embedding: Sequence[float],
        source_ref: str | None = None,
        entities: Sequence[str] = (),
    ) -> dict:
        """执行四态裁决。整个过程一个事务，按用户加事务级 advisory lock。

        为什么还要 advisory lock：两条消息几乎同时到达时，两个 worker 会各自
        查到"候选为空"然后各插一条等价记忆。锁的粒度是用户，持锁时间只有几条
        语句（LLM 已经在外面跑完了），代价可以忽略。

        `entities`：调用方显式声明的实体（`mem.py add --entity ...`）。
        落库之后 best-effort 挂到图上；图失败不影响这次写入。
        """
        if decision.action == "drop":
            return {"applied": False, "action": "drop", "reason": decision.reason}

        if decision.action == "merge":
            result = self._apply_merge(decision)
        elif decision.action == "defer":
            result = self._apply_defer(decision, fact, embedding, source_ref)
        else:
            result = self._apply_accept(decision, fact, embedding, source_ref)

        self._link_graph(result, fact, decision, entities)
        return result

    def _link_graph(
        self,
        result: dict,
        fact: Fact,
        decision: Decision,
        entities: Sequence[str],
    ) -> None:
        """把刚落库的记忆挂到实体上。**永不抛**：图是增强，不是写入的前置条件。"""
        if not result.get("applied") or not result.get("id"):
            return
        try:
            from .graph import GraphMemory

            g = GraphMemory(self)
            declared = [e for e in (str(x).strip() for x in entities) if e]
            if declared:
                # 显式声明的实体允许新建（这是"用户说它是实体"，不是猜）
                for name in declared:
                    g.upsert_entity(fact.user_id, name)
                g.link_episode(result["id"], declared)
            # 再补一轮"正文里出现的已知实体"
            g.auto_link(result["id"], fact.user_id, decision.content or fact.content)
        except Exception as exc:  # noqa: BLE001
            log.warning("图挂载失败（已忽略，记忆已写入 %s）: %s", result.get("id"), exc)

    def _apply_accept(
        self, decision: Decision, fact: Fact, embedding: Sequence[float], source_ref: str | None
    ) -> dict:
        with self.conn.transaction():
            self.conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (fact.user_id,)
            )
            row = self.conn.execute(
                SQL_INSERT_EPISODE,
                {
                    "user_id": fact.user_id,
                    "kind": fact.kind,
                    "content": decision.content,
                    "summary": fact.summary,
                    "embedding": list(embedding),
                    "embedding_model": self.embedder.model,
                    "event_time": fact.event_time,
                    "event_time_source": fact.event_time_source,
                    "source": fact.source,
                    "source_ref": source_ref,
                    "confidence": fact.confidence,
                    "importance": fact.importance,
                    "review_state": "pending" if decision.needs_review else "none",
                    "tags": list(fact.tags),
                },
            ).fetchone()
            if row is None:
                # source_ref 幂等命中：队列重试时走到这里，什么都不做
                return {"applied": False, "action": "accept", "reason": "duplicate_source_ref"}
            return {
                "applied": True,
                "action": "accept",
                "id": str(row["id"]),
                "expires_at": row["expires_at"],
                "reason": decision.reason,
            }

    def _apply_merge(self, decision: Decision) -> dict:
        row = self._write(
            SQL_REINFORCE,
            (decision.confidence_delta, decision.importance_delta, decision.target_id),
        )
        if row is None:
            # 目标已被别的写入者闭合：信息不丢，降级成新写入
            log.info("merge 目标已失效（%s），降级为 accept", decision.target_id)
            return {"applied": False, "action": "merge", "reason": "target_closed"}
        return {
            "applied": True,
            "action": "merge",
            "id": str(row["id"]),
            "confidence": row["confidence"],
            "importance": row["importance"],
            "reason": decision.reason,
        }

    def _apply_defer(
        self, decision: Decision, fact: Fact, embedding: Sequence[float], source_ref: str | None
    ) -> dict:
        conflict_id = uuid.uuid4()
        with self.conn.transaction():
            self.conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (fact.user_id,)
            )
            row = self.conn.execute(
                SQL_INSERT_EPISODE,
                {
                    "user_id": fact.user_id,
                    "kind": fact.kind,
                    "content": decision.content,
                    "summary": fact.summary,
                    "embedding": list(embedding),
                    "embedding_model": self.embedder.model,
                    "event_time": fact.event_time,
                    "event_time_source": fact.event_time_source,
                    "source": fact.source,
                    "source_ref": source_ref,
                    "confidence": fact.confidence,
                    "importance": fact.importance,
                    "review_state": "pending",   # 裁决前不参与召回
                    "tags": list(fact.tags),
                },
            ).fetchone()
            if row is None:
                return {"applied": False, "action": "defer", "reason": "duplicate_source_ref"}

            edge = self.conn.execute(
                SQL_OPEN_CONFLICT,
                (
                    conflict_id,
                    fact.user_id,
                    decision.target_id,
                    row["id"],
                    decision.reason,
                    json.dumps(
                        {"model_action": decision.model_action, "content": fact.content[:500]},
                        ensure_ascii=False,
                    ),
                ),
            ).fetchone()
            return {
                "applied": True,
                "action": "defer",
                "id": str(row["id"]),
                "conflict_id": str(edge["id"]) if edge else None,
                "existing_id": str(decision.target_id),
                "reason": decision.reason,
            }

    def resolve_conflict(
        self, conflict_id: str, resolution: str, resolved_by: str = "rule"
    ) -> dict:
        """裁决一条 defer 边：superseded / kept_old / merged。"""
        row = self._write(
            "SELECT * FROM resolve_conflict(%s, %s, %s)",
            (conflict_id, resolution, resolved_by),
        )
        return dict(row) if row else {}

    def resolve_conflict_for(
        self, user_id: str, existing_id: str, resolution: str, resolved_by: str = "rule"
    ) -> dict:
        row = self._write(
            "SELECT * FROM resolve_conflict_for(%s, %s, %s, %s)",
            (user_id, existing_id, resolution, resolved_by),
        )
        return dict(row) if row else {}

    def pending_conflicts(self, user_id: str | None = None, limit: int = 20) -> list[dict]:
        return self._read(SQL_PENDING_CONFLICTS, (user_id, user_id, limit))

    def forget(self, episode_id: str, reason: str | None = None) -> bool:
        row = self._write("SELECT forget_episode(%s, %s) AS ok", (episode_id, reason))
        return bool(row and row["ok"])

    def restore(self, episode_id: str) -> bool:
        row = self._write("SELECT restore_episode(%s) AS ok", (episode_id,))
        return bool(row and row["ok"])

    # -- 长期事实 -----------------------------------------------------------
    def recall_facts(
        self,
        user_id: str,
        keys: Sequence[str] | None = None,
        params: FactsParams | None = None,
    ) -> list[dict]:
        p = params or FactsParams()
        return self._read(
            SQL_RECALL_FACTS,
            {
                "user_id": user_id,
                "keys": list(keys) if keys else None,
                "limit": p.limit,
                "decay_offset": p.decay_offset_days,
                "decay_scale": p.decay_scale_days,
                "decay": p.decay,
            },
        )

    def set_fact(
        self,
        user_id: str,
        key: str,
        value: Any,
        confidence: float,
        source: str,
        importance: float = 0.6,
    ) -> str | None:
        """写一条事实：先失效旧值，再插同一个 id 的新值（顺序不能反）。"""
        new_id = uuid.uuid4()
        try:
            with self.conn.transaction():
                self.conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"{user_id}:{key}",),
                )
                self.conn.execute(SQL_SUPERSEDE_FACT, (new_id, user_id, key))
                row = self.conn.execute(
                    SQL_INSERT_FACT,
                    (
                        new_id,
                        user_id,
                        key,
                        json.dumps(value, ensure_ascii=False),
                        confidence,
                        importance,
                        source,
                    ),
                ).fetchone()
                return str(row["id"]) if row else None
        except Exception:
            self._reset()
            raise

    def bump_fact_usage(self, user_id: str, keys: Sequence[str]) -> int:
        if not keys:
            return 0
        return len(self._read(SQL_BUMP_FACTS, (user_id, list(keys))))

    # -- 情景记忆读取 -------------------------------------------------------
    def recall(
        self,
        user_id: str,
        query: str,
        params: RecallParams | None = None,
        with_trace: bool = False,
    ):
        """混合召回。返回 (命中列表, 查询向量)；`with_trace=True` 时再带一份图轨迹。

        向量顺手带出去给调用方复用（写冲突检测、重算 embed 都用得着）。
        """
        p = params or RecallParams()

        # 第三路：图。种子来自"查询里出现的已知实体"；没有种子就是空列表，
        # recall_episodes 里第三路贡献 0 —— 与两路召回完全等价。
        graph_ids: list[str] = []
        trace = None
        if p.enable_graph:
            from .graph import GraphMemory, GraphParams

            graph_ids, trace = GraphMemory(self).route(
                user_id, query, GraphParams(depth=p.graph_depth, limit=p.graph_limit)
            )

        vec = self.embedder.embed([query])[0]
        rows = self._read(
            SQL_RECALL,
            {
                "vec": list(vec),
                "user_id": user_id,
                "query": query,
                "vec_k": p.vec_k,
                "kw_k": p.kw_k,
                "rrf_k": p.rrf_k,
                "decay_offset": p.decay_offset_days,
                "decay_scale": p.decay_scale_days,
                "decay": p.decay,
                "freq": p.freq_weight,
                "limit": p.limit,
                "ef": p.resolved_ef_search(),
                "include_pending": p.include_pending,
                "imp_weight": p.importance_weight,
                "tags": list(p.tags) if p.tags else None,
                "graph_ids": graph_ids or None,
            },
        )
        if with_trace:
            if trace is None:          # 关掉图路时也给一个空轨迹，调用方不必判空
                from .graph import GraphTrace

                trace = GraphTrace()
            return rows, vec, trace
        return rows, vec

    def expand(self, ids: Sequence[str]) -> list[dict]:
        """渐进披露第二层：拿全文。只在 summary 不够用时调用。"""
        if not ids:
            return []
        return self._read(SQL_EXPAND, (list(ids),))

    def bump_usage(self, ids: Sequence[str]) -> int:
        """使用反馈。检索路径里不要直接写，攒一批后台刷。"""
        if not ids:
            return 0
        return len(self._read(SQL_BUMP, (list(ids),)))

    def purge_dryrun(self, user_id: str) -> dict:
        """先看会删掉什么。purge_user 是对整个用户的物理删除，必须先给清单。"""
        rows = self._read("SELECT * FROM purge_user_dryrun(%s::text)", (user_id,))
        return {r["table_name"]: int(r["rows_to_delete"]) for r in rows}

    def purge_user(self, user_id: str) -> dict:
        """整用户硬删。p_confirm 传 user_id 本身 —— SQL 里要求两者一致才执行。"""
        rows = self._read(
            "SELECT * FROM purge_user(%s::text, %s::text)", (user_id, user_id)
        )
        return rows[0] if rows else {}

    def list_episodes(self, user_id: str, limit: int = 20) -> list[dict]:
        """看某个用户的记忆、TTL 与失效链（排查/演示用）。"""
        return self._read(
            """SELECT id, kind, content, summary, event_time, ingested_at,
                      valid_until, superseded_by, expires_at, lifecycle_state,
                      review_state, use_count
               FROM episodes WHERE user_id = %s
               ORDER BY ingested_at, id
               LIMIT %s""",
            (user_id, limit),
        )
