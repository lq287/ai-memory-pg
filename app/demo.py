"""端到端演示：入队 → 异步写入（四态裁决）→ 冲突裁决 → 混合召回 → 预算封顶。

    python -m app.demo            # 需要可连的 PG（npm run serve 或 docker compose up -d）

不需要任何 API key：默认用 HashEmbedder + HeuristicLLM 两个替身，
所以这条命令验证的是**管线**（队列、事务、四态落库、TTL、冲突边、检索装配），
而不是模型效果。
"""
from __future__ import annotations

import logging
import uuid

from .config import Policy, RecallParams, Settings, plan_recall
from .retrieve import assemble, expand, recall
from .store import Store
from .worker import Worker, build_deps

# 每次跑用一个新的 user_id：上一次的任务会留在队列里（processing / dead），
# 而 dedup_key 的在途去重会挡住同名重放 —— 那正是它该干的事，但演示要能重复跑。
USER = f"demo-{uuid.uuid4().hex[:8]}"

# 四段"对话"，刻意覆盖四态：
#   1) 长期事实（走 facts 表，按 subject key 取）
#   2) 同一 key 的新值（取代 → 旧行闭合，历史保留）
#   3) 一条情景事件（accept）
#   4) 与 3) 矛盾的复述（defer → 建冲突边 → 之后裁决）
CONVERSATIONS = [
    (1, "我的时区是 Asia/Tokyo。"),
    (2, "我的时区改成了 Asia/Shanghai。"),
    (3, "我比较喜欢在杭州开会，上周的产品评审会就在那边。"),
    (4, "我不喜欢在杭州开会，那边的评审会效率很低。"),
    (5, "我比较喜欢在杭州开会，上周的产品评审会就在那边。"),  # 复述 → merge
]

QUERIES = [
    "我的时区是什么",           # 简单查询 → 少召回
    "上次那个评审会在哪开的",    # 复杂查询 → 多召回
]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s %(message)s")
    settings = Settings.from_env()
    embedder, llm = build_deps(settings)

    with Store(settings.dsn, embedder) as store:
        print(f"user_id = {USER}")

        print("=" * 74)
        print("0. 回收上次进程崩溃留下的任务（租约过期 → 回到 pending）")
        print(f"   reclaimed/deadlettered = {store.reap(settings.lease_seconds)}")

        print("=" * 74)
        print("1. 入队（fire-and-forget：对话回复先返回，写入丢进队列）")
        for idx, text in CONVERSATIONS:
            job_id = store.enqueue(
                USER,
                "consolidate",
                {"text": text, "source": "conversation"},
                dedup_key=f"{USER}:demo:{idx}",
            )
            print(f"   enqueued job={job_id}  {text[:38]}…")

        print("=" * 74)
        print("2. 消费队列（每轮：抽取 → 近邻候选 → 四态建议 → 规则裁决 → 落库）")
        # max_dist 放宽到 0.95：HashEmbedder 不是语义向量，0.35（真模型阈值）会筛空候选，
        # 那样 defer/merge 永远走不到。真模型请用默认值。
        worker = Worker(
            store, llm, embedder, settings, Policy(), candidate_max_dist=0.95
        )
        processed = 0
        for _ in range(6):
            n = worker.run_once()
            if n == 0:
                break
            processed += n
        print(f"   处理了 {processed} 个 job")

        print("=" * 74)
        print("3. 情景记忆：kind / TTL / 评审状态 / 失效链")
        rows = store.list_episodes(USER)
        if not rows:
            print("   （没有记忆 —— 检查上面的 worker 日志）")
        for r in rows:
            state = "当前" if r["valid_until"] is None else "已失效"
            ttl = r["expires_at"].strftime("%Y-%m-%d") if r["expires_at"] else "不按时间过期"
            print(
                f"   [{state}][{r['kind']:<8}][review={r['review_state']:<7}] "
                f"ttl={ttl:<12} use={r['use_count']}  {r['content'][:34]}"
            )

        print("=" * 74)
        print("4. defer 的冲突边：落库但标待裁决，裁决前不参与召回")
        conflicts = store.pending_conflicts(USER)
        if not conflicts:
            print("   （没有待裁决冲突）")
        for c in conflicts:
            print(f"   旧: {c['existing_content'][:34]}")
            print(f"   新: {c['incoming_content'][:34]}  ({c['reason']})")
            resolved = store.resolve_conflict(
                str(c["id"]), "superseded", resolved_by="demo"
            )
            print(f"   → 裁决: superseded（旧行闭合，血缘指向新行）{resolved.get('resolution')}")

        print("=" * 74)
        print("5. 长期事实：stats 走 facts 表，衰减按 last_used_at 不按 event_time")
        facts = store.recall_facts(USER)
        for f in facts:
            print(
                f"   {f['key']:<10} = {f['value']!r:<18} conf={f['confidence']:.2f} "
                f"age={f['age_days']:.0f}d score={f['score']:.3f}"
            )
        if not facts:
            print("   （没有长期事实 —— 检查门槛：longterm 需要 conf >= 0.80）")

        print("=" * 74)
        print("6. 混合召回 + 预算封顶（事实层优先，情景层按分数截断）")
        for q in QUERIES:
            params = plan_recall(q)
            block, hits, _facts = assemble(store, USER, q, params)
            print(f"\n   Q: {q}   (limit={params.limit}, ef_search={params.resolved_ef_search()}, "
                  f"budget={params.token_budget})")
            for h in hits:
                print(
                    f"     score={h.score:.6f} 路径={h.recall_paths} "
                    f"rk_v={h.rank_vec} rk_k={h.rank_kw} | {h.summary or h.snippet}"
                )
            print("   --- 实际注入 ---")
            for line in block.splitlines():
                print(f"   {line}")

        print("=" * 74)
        print("7. 渐进披露第二层：按 id 拉全文（只有 summary 不够用时才做这一步）")
        top = recall(store, USER, "评审会", RecallParams(limit=2))
        if top:
            full = expand(store, [top[0].id])
            for cid, content in full.items():
                print(f"   {cid[:8]}…: {content[:60]}")
        else:
            print("   （无命中）")

        print("=" * 74)
        print("8. 运维视角：待裁决 / 已过期仍在有效行 / 缺向量 / 队列")
        print(f"   {store.health(USER)}")
        for s in store.stats():
            print(f"   {s['status']:<12} jobs={s['jobs']:<4} overdue={s['overdue']}")

    print(f"\n完成。清理演示数据（user_id = {USER}）：")
    print(f"  DELETE FROM memory_conflicts WHERE user_id = '{USER}';")
    print(f"  DELETE FROM episodes         WHERE user_id = '{USER}';")
    print(f"  DELETE FROM facts            WHERE user_id = '{USER}';")
    print(f"  DELETE FROM memory_jobs      WHERE user_id = '{USER}';")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
