"""记忆库命令行：把"记忆"真正写进 PostgreSQL，而不是写进文件。

    python scripts/mem.py doctor                       # 体检：连库/扩展/表/规模
    python scripts/mem.py init                         # 装载 sql/01..03（幂等）
    python scripts/mem.py add "用户偏好深色主题"        # 写一条情景记忆
    python scripts/mem.py recall "时区"                # 混合召回（向量 ⊕ 词法 → RRF → 高斯衰减）
    python scripts/mem.py fact set timezone Asia/Shanghai
    python scripts/mem.py fact get [key]
    python scripts/mem.py conflicts                    # 待裁决冲突
    python scripts/mem.py forget <id> | restore <id>
    python scripts/mem.py health

为什么单独有这个：`app/` 是给服务端用的库；这个脚本是给"人/Agent 直接读写记忆"
用的入口，一条命令就能存取，不必起 worker、不必写代码。

DSN 优先级：MEM_PG_DSN > 本地 mem 角色（postgresql://mem:mem@127.0.0.1:5432/mem）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 不写 .pyc —— 让本脚本对磁盘零写入。
#
# ⚠️ 这不是"从别的工作区存记忆要授权"的真正原因（那个是 .pylibs 的 NTFS ACL 坏了，
#    见 scripts/fix-pylibs-acl.ps1：沙箱内 pip 装出来的包继承了受限 token 的默认
#    DACL，只带 SYSTEM/Administrators/OWNER RIGHTS，没有 EVERYONE，于是别的工作区
#    的会话连读都读不了 → 拒绝 → 申请提权）。
#
# 这一条仍然值得留：Windows 沙箱是 WRITE_RESTRICTED token，工作区外的**写**一律被拒，
# 而刷新 app/__pycache__ 正是一次工作区外写入。关掉字节码后没有可拒的东西。
#
# 必须在 import app.* 之前设置：模块一旦编译过就晚了。
# mem.cmd 里也设了同名环境变量，覆盖直接 `python scripts\mem.py` 的调用。
# ---------------------------------------------------------------------------
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for lib in (ROOT / ".pylibs",):
    if lib.exists():
        sys.path.insert(0, str(lib))

DEFAULT_DSN = "postgresql://mem:mem@127.0.0.1:5432/mem"

from app.config import Policy, RecallParams  # noqa: E402
from app.decide import Candidate, Fact, arbitrate, parse_proposal  # noqa: E402
from app.store import Store  # noqa: E402

DEFAULT_USER = os.environ.get("MEM_USER_ID", "owner")


def _split_tags(raw) -> tuple[str, ...] | None:
    """--tags 同时接受逗号与空格分隔：`--tags a,b` 和 `--tags a b` 等价。

    argparse 的 nargs="*" 只按空格分词，所以 `--tags a,b` 会进来一个
    "a,b" 字符串 —— 这里统一拆开，避免"标签写进去了但过滤不出来"。
    """
    if not raw:
        return None
    out: list[str] = []
    for grp in raw:
        for part in str(grp).split(","):
            p = part.strip()
            if p:
                out.append(p)
    return tuple(dict.fromkeys(out)) or None


def get_dsn() -> str:
    return os.environ.get("MEM_PG_DSN") or DEFAULT_DSN


def open_store() -> Store:
    """统一走 app.embedding.build_embedder，避免和 worker 用不同的向量空间。

    两处性能处理，都是 CLI 每次新进程才有的成本：

    1. **LazyEmbedder**：只读命令（doctor/list/fact get/health/graph stats/purge/...）
       不会为一次 SQL 白付 450ms 的 ONNX 模型加载，真正 embed 时才构建。
    2. **复用连接对齐模型**：`_match_db_embedder` 原本自己再开一条连接查
       `embedding_model`，在白付一次 SCRAM 握手（~200ms）之后这条连接就关了。
       现在先开 Store，再用**同一条连接**查；LazyEmbedder 尚未构建，换掉是安全的。
    """
    from app.config import Settings
    from app.embedding import LazyEmbedder, align_to_database

    settings = Settings.from_env()
    store = Store(get_dsn(), LazyEmbedder(settings)).connect()
    # 没显式指定模型时对齐到库里在用的模型（规则只在 app.embedding 里写一次）
    resolved = align_to_database(settings, conn=store.conn)
    if resolved is not settings:
        store.embedder = LazyEmbedder(resolved)
    return store


def _match_db_embedder(settings, conn=None):
    """兼容薄壳：真正的规则在 app.embedding.align_to_database（只写一次）。"""
    from app.embedding import align_to_database

    return align_to_database(settings, conn=conn or get_dsn())


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"DSN: {get_dsn()}")
    with open_store() as store:
        row = store.conn.execute("SELECT version() AS v").fetchone()
        print(f"  PostgreSQL : {row['v'].split(' on ')[0]}")
        store.conn.commit()
        exts = store.conn.execute(
            "SELECT extname, extversion FROM pg_extension "
            "WHERE extname IN ('vector','pg_trgm','plpgsql') ORDER BY 1"
        ).fetchall()
        store.conn.commit()
        print("  扩展       : " + ", ".join(f"{e['extname']} {e['extversion']}" for e in exts))
        names = {"vector", "pg_trgm"}
        have = {e["extname"] for e in exts}
        missing = names - have
        if missing:
            print(f"  [缺扩展] {', '.join(sorted(missing))} —— 需超级用户执行 CREATE EXTENSION")
        tables = store.conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1"
        ).fetchall()
        store.conn.commit()
        print("  表         : " + (", ".join(t["tablename"] for t in tables) or "（空，先跑 init）"))
        if {"episodes", "facts"} <= {t["tablename"] for t in tables}:
            n_ep = store.conn.execute(
                "SELECT count(*) AS n FROM episodes WHERE user_id=%s", (args.user,)
            ).fetchone()["n"]
            n_fact = store.conn.execute(
                "SELECT count(*) AS n FROM facts WHERE user_id=%s", (args.user,)
            ).fetchone()["n"]
            store.conn.commit()
            print(f"  记忆规模   : episodes={n_ep}  facts={n_fact}  (user={args.user})")
            h = store.health(args.user)
            print(f"  健康度     : {h}")
            if "entities" in {t["tablename"] for t in tables}:
                g = store.conn.execute(
                    "SELECT * FROM graph_stats(%s::text)", (args.user,)
                ).fetchone()
                store.conn.commit()
                print(
                    f"  图层       : entities={g['entities']}  "
                    f"relations={g['relations_live']}  "
                    f"linked_episodes={g['linked_episodes']}"
                )
        return 0 if not missing else 2


def _strip_psql(sql: str) -> str:
    return "\n".join(l for l in sql.splitlines() if not l.lstrip().startswith("\\"))


def cmd_init(args: argparse.Namespace) -> int:
    files = [
        "01_schema.sql", "02_retrieval.sql", "03_write.sql",
        "04_workmem.sql", "05_distill.sql", "06_graph.sql", "07_purge.sql",
    ]
    with open_store() as store:
        for f in files:
            sql = _strip_psql((ROOT / "sql" / f).read_text(encoding="utf-8"))
            try:
                store.conn.execute(sql)
                store.conn.commit()
                print(f"  [OK] {f}")
            except Exception as exc:
                store.conn.rollback()
                msg = str(exc).splitlines()[0]
                print(f"  [FAIL] {f}: {msg}")
                if "permission denied to create extension" in msg:
                    print("         扩展要超级用户建：psql -U postgres -d mem -c 'CREATE EXTENSION vector;'")
                return 1
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    """写一条记忆。走完整裁决链路（accept/merge/defer/drop），不是裸 INSERT。"""
    fact = Fact(
        user_id=args.user,
        content=args.text,
        confidence=args.confidence,
        source=args.source,
        event_time=datetime.now(timezone.utc),
        kind=args.kind,
        event_time_source="ingest",
        summary=(args.summary or args.text)[:120],
        importance=args.importance,
        tags=_split_tags(args.tags) or (),
    )
    with open_store() as store:
        vec = store.embedder.embed([fact.content])[0]
        cands = store.conflict_candidates(args.user, vec, k=5, max_dist=args.max_dist)
        proposal = parse_proposal({"action": args.action, "memory_id": args.target})
        decision = arbitrate(proposal, fact, cands, Policy())
        result = store.apply(
            decision, fact, vec,
            source_ref=f"cli:{uuid.uuid4().hex[:12]}",
            entities=_split_tags(args.entity) or (),
        )
        print(
            f"  {decision.action:<7} reason={decision.reason:<28} "
            f"id={str(result.get('id') or '-')[:8]} cascade={result.get('reason', '-')}"
        )
        if decision.action == "drop":
            print(f"  （未写入：{decision.reason}）")
            return 3
        return 0


def cmd_recall(args: argparse.Namespace) -> int:
    with open_store() as store:
        params = RecallParams(
            limit=args.limit,
            include_pending=args.include_pending,
            importance_weight=args.importance_weight,
            tags=_split_tags(args.tags),
            enable_graph=not args.no_graph,
        )
        rows, _, trace = store.recall(args.user, args.query, params, with_trace=True)
        if not rows:
            print("  （无命中）")
        for r in rows:
            paths = (
                ("v" if r["rk_v"] else "-")
                + ("k" if r["rk_k"] else "-")
                + ("g" if r["rk_g"] else "-")
            )
            print(
                f"  score={r['score']:.6f} 路径={paths} "
                f"rk_v={r['rk_v']} rk_k={r['rk_k']} rk_g={r['rk_g']} "
                f"| {r['summary'] or r['snippet']}"
            )
        if trace is not None:
            print(f"  {trace.describe()}")
        if args.full and rows:
            full = store.expand([str(r["id"]) for r in rows])
            print("\n  --- 全文 ---")
            for cid, content in full.items():
                print(f"  {cid[:8]}: {content[:200]}")
        # 使用反馈：**默认不刷**，要显式 --mark-used。
        #
        # 这是踩出来的：以前每次 recall 都无条件 bump use_count，于是
        # "被召回过的" 就等于 "被用过的"。调试时反复召回同几条，
        # 它们的 use_count 一路涨到 15，而 1+0.2·log10(1+n) 的跨度
        # 远大于 RRF 的分数带（1/(60+名次) 只差几个百分点）——
        # 结果是"我测过的记忆"永久占据榜首，真实需要被埋掉。
        # 语义上 "retrieved" ≠ "used"：只有调用方确认这条记忆真的
        # 影响了输出，才该记一次使用。
        if args.mark_used and rows:
            store.bump_usage([str(r["id"]) for r in rows])
            print(f"  （已记使用反馈：{len(rows)} 条 use_count +1）")
        return 0


def cmd_fact(args: argparse.Namespace) -> int:
    with open_store() as store:
        if args.op == "set":
            if not args.key or args.value is None:
                print("  用法: mem.py fact set <key> <value>")
                return 1
            raw = args.value
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = raw
            fid = store.set_fact(
                args.user, args.key.lower(), value, args.confidence, args.source,
                importance=args.importance,
            )
            print(f"  [OK] {args.key} = {json.dumps(value, ensure_ascii=False)}  (id={str(fid)[:8]})")
            return 0
        rows = store.recall_facts(args.user, [args.key] if args.key else None)
        if not rows:
            print("  （没有事实）")
            return 0
        for f in rows:
            print(
                f"  {f['key']:<14} = {json.dumps(f['value'], ensure_ascii=False):<24} "
                f"conf={f['confidence']:.2f} src={f['source']} "
                f"age={f['age_days']:.0f}d score={f['score']:.3f}"
            )
        return 0


def cmd_conflicts(args: argparse.Namespace) -> int:
    with open_store() as store:
        rows = store.pending_conflicts(args.user if args.user != "all" else None, args.limit)
        if not rows:
            print("  （没有待裁决冲突）")
        for c in rows:
            print(f"  [{str(c['id'])[:8]}] 旧: {c['existing_content'][:60]}")
            print(f"            新: {c['incoming_content'][:60]}  ({c['reason']})")
            if args.resolve:
                r = store.resolve_conflict(str(c["id"]), args.resolve, "cli")
                print(f"            → {r.get('resolution')}")
        return 0


def cmd_forget(args: argparse.Namespace) -> int:
    with open_store() as store:
        if args.op == "forget":
            ok = store.forget(args.id, args.reason)
            print(f"  forget({args.id[:8]}) -> {ok}")
        else:
            ok = store.restore(args.id)
            print(f"  restore({args.id[:8]}) -> {ok}")
        return 0 if ok else 1


def cmd_health(args: argparse.Namespace) -> int:
    with open_store() as store:
        print(f"  健康度 : {store.health(args.user)}")
        for s in store.stats():
            print(f"  队列   : {s['status']:<12} jobs={s['jobs']:<4} overdue={s['overdue']}")
        return 0


# ---------------------------------------------------------------------------
# 任务状态层 / 工作记忆层
# ---------------------------------------------------------------------------
def _wm(store):
    from app.workmem import WorkMemory

    return WorkMemory(store)


def cmd_task(args: argparse.Namespace) -> int:
    with open_store() as store:
        wm = _wm(store)
        if args.op == "start":
            t = wm.start_task(
                args.user, args.kind, args.title,
                metadata=json.loads(args.meta) if args.meta else None,
                retention_hours=args.retention_hours,
            )
            print(f"  [OK] task {t['id']}  {t['kind']}  {t['title']}  status={t['status']}")
            return 0
        if args.op == "update":
            t = wm.update_task(
                args.id, status=args.status, step=args.step,
                step_index=args.step_index,
                progress=args.progress,
                result=json.loads(args.result) if args.result else None,
                error=args.error,
            )
            print(f"  [OK] {str(t['id'])[:8]} status={t['status']} step={t['step']} "
                  f"progress={float(t['progress']):.0%}")
            return 0
        if args.op in ("done", "fail", "cancel"):
            status = {"done": "done", "fail": "failed", "cancel": "cancelled"}[args.op]
            t = wm.finish_task(
                args.id, status,
                json.loads(args.result) if args.result else None,
                args.error,
            )
            print(f"  [OK] {str(t['id'])[:8]} -> {t['status']}")
            return 0
        if args.op == "get":
            got = wm.get_task(args.id, args.events)
            if not got:
                print("  （任务不存在）")
                return 1
            t, events = got
            print(f"  task     : {t['title']}  ({t['kind']})")
            print(f"  status   : {t['status']}   step={t['step']} ({t['step_index']})  "
                  f"progress={float(t['progress']):.0%}")
            print(f"  created  : {t['created_at']}   updated: {t['updated_at']}")
            if t["result"] is not None:
                print(f"  result   : {json.dumps(t['result'], ensure_ascii=False)[:200]}")
            if t["error"]:
                print(f"  error    : {t['error'][:200]}")
            print("  events:")
            for e in events:
                # jsonb 里的时间戳是字符串（jsonb_agg 出来的），不能直接当 datetime 格式化
                at = e.get("at")
                if isinstance(at, str):
                    at_s = at[:16].replace("T", " ")
                elif at is not None:
                    at_s = f"{at:%m-%d %H:%M}"
                else:
                    at_s = "-"
                print(f"    {at_s}  {e['event']:<8} {e['status'] or '-':<10} {e['step'] or ''}")
            return 0
        # list
        rows = wm.all_tasks(args.user, args.limit) if args.all else wm.live_tasks(args.user, args.limit)
        if not rows:
            print("  （没有任务）")
        for t in rows:
            print(f"  [{t['status']:<9}] {str(t['id'])[:8]} {t['kind']:<10} "
                  f"step={t['step'] or '-':<12} {float(t['progress']):>3.0%}  {t['title'][:36]}")
        return 0


def cmd_work(args: argparse.Namespace) -> int:
    # 子命令名就是 op：open / pin-note / pin-memory / pin-fact / pin-task / show / render / clear / expire
    op = (args.op or "").strip()
    if op.startswith("pin-"):
        kind = op[4:]
        op = "pin"
    else:
        kind = getattr(args, "kind", None)

    with open_store() as store:
        wm = _wm(store)
        if op == "open":
            ws = wm.open_set(args.user, task_id=args.task, session_id=args.session,
                             note=args.note, token_budget=args.budget,
                             ttl_hours=args.ttl_hours)
            print(f"  [OK] working_set {ws['id']}  budget={ws['token_budget']}  "
                  f"expires={ws['expires_at']:%m-%d %H:%M}")
            return 0
        if op == "pin":
            if kind == "memory":
                it = wm.pin_memory(args.set, args.value, with_content=args.full)
            elif kind == "fact":
                it = wm.pin_fact(args.set, args.user, args.value)
            elif kind == "task":
                it = wm.pin_task_state(args.set, args.value)
            else:
                it = wm.pin_note(args.set, args.value)
            if it is None:
                print("  [FAIL] 目标不存在")
                return 1
            print(f"  [OK] pinned kind={it['kind']} position={it['position']} "
                  f"tokens={it['tokens']}  {it['content'][:56]}")
            return 0
        if args.op == "show":
            ws = wm.get_set(args.set)
            if not ws:
                print("  （工作集不存在）")
                return 1
            print(f"  set      : {ws['id']}  note={ws['note']}  budget={ws['token_budget']}")
            print(f"  expires  : {ws['expires_at']}")
            for it in wm.items(args.set):
                print(f"    [{it['position']:>2}] {it['kind']:<14} {it['tokens']:>4}t  {it['content'][:60]}")
            return 0
        if args.op == "render":
            asm = wm.render(args.set, budget=args.budget)
            if not asm.text:
                print("  （空工作集）")
                return 1
            print(asm.text)
            print(f"\n  [stats] tokens={asm.tokens} used={len(asm.used_items)} dropped={asm.dropped}")
            return 0
        if args.op == "clear":
            print(f"  [OK] 删除 {wm.clear(args.set)} 条")
            return 0
        # expire
        sets_deleted, tasks_expired = wm.expire()
        print(f"  [OK] 过期工作集删除={sets_deleted}  任务标记={tasks_expired}")
        return 0


# ---------------------------------------------------------------------------
# distill 提炼
# ---------------------------------------------------------------------------
def cmd_distill(args: argparse.Namespace) -> int:
    from app import distill

    with open_store() as store:
        if args.op == "status":
            st = distill.pending_stats(store, args.user, min_age_days=args.min_age_days)
            print(f"  待提炼 : {st['pending']} 条  最老 {float(st['oldest_days']):.0f} 天  "
                  f"7 天内过期 {st['expiring_7d']} 条")
            rows = distill.fetch_candidates(store, args.user,
                                           min_age_days=args.min_age_days, limit=args.limit)
            for r in rows:
                print(f"    {r.id[:8]} {r.event_time:%Y-%m-%d} ({r.age_days:.0f}d) {r.content[:60]}")
            return 0

        if args.op == "run":
            res = distill.distill_run(
                store, args.user, backend=args.backend,
                min_age_days=args.min_age_days, batch_size=args.batch_size,
                max_batches=args.max_batches, close_raw=args.close_raw,
                limit_chars=args.limit_chars,
            )
            if not res:
                print("  （没有可提炼的原始轮次）")
                return 0
            for r in res:
                print(f"  [OK] summary={str(r.summary_id)[:8]} 覆盖 {r.linked} 条  backend={r.backend}")
                for line in r.text.splitlines():
                    print(f"    {line}")
            return 0

        if args.op == "plan":
            plan = distill.distill_plan(
                store, args.user, min_age_days=args.min_age_days,
                batch_size=args.batch_size, token_budget=args.token_budget,
            )
            if not plan.get("created"):
                print(f"  （没有候选：{plan.get('reason')}）")
                return 0
            print(f"  [OK] task={plan['task_id']}  working_set={plan['set_id']}  "
                  f"待压 {plan['count']} 条")
            print("  raw_ids: " + ",".join(plan["raw_ids"]))
            print()
            print(plan["assembly"])
            print()
            print("  下一步：读上面原文，然后")
            print(f"    python scripts/mem.py distill apply {plan['task_id']} --file 摘要.md")
            return 0

        # apply
        if args.file:
            body = Path(args.file).read_text(encoding="utf-8")
        elif args.text:
            body = args.text
        else:
            print("  用法: distill apply <task_id> --text '...' | --file 摘要.md")
            return 1
        res = distill.distill_apply_from_task(
            store, args.task, body, summary=args.summary, close_raw=args.close_raw
        )
        print(f"  [OK] summary={str(res.summary_id)[:8]} 覆盖 {res.linked} 条原文  "
              f"backend={res.backend}")
        return 0
def cmd_graph(args: argparse.Namespace) -> int:
    """实体 / 关系 / 图召回自检。"""
    from app.graph import GraphMemory, GraphParams

    with open_store() as store:
        g = GraphMemory(store)
        if args.op == "add":
            if not args.name:
                print("  用法: mem.py graph add <显示名> [--label org] [--alias 别名 ...]")
                return 1
            eid = g.upsert_entity(args.user, args.name, args.label, args.alias or ())
            print(f"  [OK] entity {args.name} ({args.label}) id={eid[:8]}")
            return 0

        if args.op == "relate":
            if not (args.src and args.pred and args.dst):
                print("  用法: mem.py graph relate <源> <谓词> <目标> [--confidence 0.9]")
                return 1
            rid = g.relate(args.user, args.src, args.pred, args.dst, args.confidence)
            print(f"  [OK] {args.src} --{args.pred}--> {args.dst} id={rid[:8]}")
            return 0

        if args.op == "link":
            if not (args.id and args.entity):
                print("  用法: mem.py graph link <记忆id> --entity <实体名> ...")
                return 1
            n = g.link_episode(args.id, args.entity)
            print(f"  [OK] 挂上 {n} 条边（实体需已存在，用 graph add 建）")
            return 0

        if args.op == "backfill":
            # 一次性把**存量**记忆挂到已存在的实体上（新写入时是自动挂的）
            rows = store._read(
                """SELECT e.id, e.content FROM episodes e
                   WHERE e.user_id = %s AND e.valid_until IS NULL
                     AND NOT EXISTS (SELECT 1 FROM episode_entities x WHERE x.episode_id = e.id)
                   ORDER BY e.event_time DESC LIMIT %s""",
                (args.user, args.limit),
            )
            touched = edges = 0
            for r in rows:
                n = g.auto_link(str(r["id"]), args.user, r["content"] or "")
                if n:
                    touched += 1
                    edges += n
            print(f"  [OK] 扫了 {len(rows)} 条未挂载记忆，{touched} 条挂上了 {edges} 条边")
            print(f"  {g.stats(args.user)}")
            return 0

        if args.op == "stats":
            print(f"  {g.stats(args.user)}")
            for e in g.entities(args.user, args.limit):
                al = f" 别名={list(e['aliases'])}" if e["aliases"] else ""
                print(f"  · [{e['label']:<8}] {e['display']}{al}")
            for r in g.relations(args.user, args.limit):
                print(f"  → {r['src']} --{r['predicate']}--> {r['dst']}  conf={r['confidence']:.2f}")
            return 0

        # neighbors / route：从查询或指定实出发走几跳
        if args.op == "neighbors":
            seeds = args.seed or ([args.query] if args.query else [])
            rows = g.neighbors(args.user, seeds, args.depth)
            if not rows:
                print(f"  （从 {seeds} 没有可达实体；实体名需与 graph add 一致）")
                return 0
            for r in rows:
                print(f"  d{r['depth']} [{' > '.join(r['path'])}] ({r['label']})")
            return 0

        # route：完整走一遍第三路，看召回会拿到哪些记忆
        ids, trace = g.route(
            args.user, args.query or "", GraphParams(depth=args.depth, limit=args.limit)
        )
        print(f"  {trace.describe()}")
        if ids:
            full = store.expand(ids)
            for r in full:
                print(f"  · {str(r['id'])[:8]} {r['content'][:60]}")
        return 0


def cmd_purge(args: argparse.Namespace) -> int:
    """整用户清除（GDPR 式硬删）。必须二次确认。"""
    with open_store() as store:
        if not args.yes:
            plan = store.purge_dryrun(args.user)
            print(f"  将删除（user={args.user}）：")
            for k, v in plan.items():
                if isinstance(v, int):
                    print(f"    {k:<18} {v}")
            print(f"  确认执行请加 --yes（会真的物理删除，且无法恢复）")
            return 0
        counts = store.purge_user(args.user)
        print(f"  [OK] user={args.user} 已清除：")
        for k, v in counts.items():
            if isinstance(v, int):
                print(f"    {k:<18} {v}")
        return 0


def cmd_digest(args: argparse.Namespace) -> int:
    """日/周/月/年关键词提炼。"""
    from datetime import date as _date

    from app.digest import PERIODS, DigestBuilder, bucket_label, default_range

    with open_store() as store:
        b = DigestBuilder(store)

        if args.op == "build":
            periods = list(PERIODS) if args.period == "all" else [args.period]
            if args.from_date:
                start = _date.fromisoformat(args.from_date)
                end = (_date.fromisoformat(args.to_date) + timedelta(days=1)) if args.to_date \
                    else (start + timedelta(days=1))
            else:
                start = end = None

            total_new = 0
            for period in periods:
                s, e = (start, end) if start else default_range(period)
                results = b.build_range(
                    args.user, period, s, e,
                    top_n=args.top, min_count=args.min_count,
                    include_empty=args.include_empty,
                )
                built = [r for r in results if not r.skipped_reason]
                print(f"  {period:<5} {s}~{e - timedelta(days=1)}  "
                      f"桶 {len(results)} 个，非空 {len(built)} 个")
                for r in built:
                    kws = "/".join(r.keywords[:6])
                    print(f"    [{r.label}] {r.episode_count:>3} 条  {kws}")
                total_new += len(built)
            print(f"  [OK] 共写入/更新 {total_new} 期摘要")
            return 0

        if args.op == "show":
            rows = b.recent(args.user, None if args.period == "all" else args.period, args.limit)
            if not rows:
                print("  （还没有摘要，先跑 mem.py digest build --period week）")
                return 0
            for r in rows:
                print(f"  [{r['period']:<5}] {r['bucket_start']}~{r['bucket_end']}  "
                      f"{r['episode_count']} 条  {r['summary'][:70]}")
                if args.full:
                    print("        " + (r["keywords"] or []).__str__())
            return 0

        if args.op == "keywords":
            rows = b.recent(args.user, None if args.period == "all" else args.period, args.limit)
            agg: dict[str, float] = {}
            for r in rows:
                terms = r.get("terms") or []
                if isinstance(terms, str):
                    terms = json.loads(terms)
                for i, t in enumerate(terms):
                    if not isinstance(t, dict):
                        continue
                    # 跨期汇总：权重相加，并给"最近一期"更高权重
                    agg[t["term"]] = agg.get(t["term"], 0.0) + float(t.get("weight") or 0)
            if not agg:
                print("  （没有关键词，先跑 mem.py digest build）")
                return 0
            top = sorted(agg.items(), key=lambda kv: -kv[1])[: args.limit]
            print("  " + "  ".join(f"{k}({v:.0f})" for k, v in top))
            return 0

        if args.op == "stats":
            rows = b.stats(args.user)
            if not rows:
                print("  （还没有摘要）")
            for r in rows:
                print(f"  {r['period']:<5} 桶={r['buckets']:<4} 覆盖记忆={r['episodes']:<5} "
                      f"关键词数={r['keywords']:<5} {r['first_bucket']} ~ {r['last_bucket']}")
            return 0

        print("  用法: mem.py digest build|show|keywords|stats")
        return 1


def cmd_context(args: argparse.Namespace) -> int:
    """组装一段可直接塞进提示词的记忆前言（会话开头用）。

    为什么需要这个：记忆库本身是 pull 的 —— 没有任何东西会自动把它带进对话。
    这个命令把"现在该知道什么"压成一段有预算上限的文本，
    人和 Agent 都只要一条命令就能拿到，不必分别调 fact/recall/digest。
    """
    from app.config import FactsParams
    from app.digest import DigestBuilder
    from app.retrieve import recall as do_recall

    out: list[str] = []
    with open_store() as store:
        # 1) 长期事实（按键直查，最稳的一层）
        facts = store.recall_facts(args.user, None, FactsParams(limit=args.fact_limit))
        if facts:
            out.append("[事实]")
            for f in facts:
                out.append(f"- {f['key']} = {json.dumps(f['value'], ensure_ascii=False)}")

        # 2) 最近的时段摘要（日/周）——比逐条记忆更适合"我在忙什么"
        dg = DigestBuilder(store)
        for period in ("week", "day"):
            rows = dg.recent(args.user, period, 2)
            live = [r for r in rows if r["episode_count"]]
            if live:
                out.append(f"[{period}]")
                for r in live:
                    out.append(f"- {r['bucket_start']} {r['summary'][:100]}")

        # 3) 进行中的任务（状态层，不是检索层）
        live_tasks = store._read("SELECT * FROM tasks_live(%s::text, %s::int)",
                                 (args.user, 5)) if args.with_tasks else []
        if live_tasks:
            out.append("[进行中]")
            for t in live_tasks:
                out.append(f"- {t['kind']}/{t['status']} {t['title']} ({t['progress']:.0%})")

        # 4) 需要人裁决的冲突（不裁决就是脏数据）
        if args.with_conflicts:
            n = store._read(
                "SELECT count(*) AS n FROM memory_conflicts WHERE user_id=%s AND status='open'",
                (args.user,),
            )[0]["n"]
            if n:
                out.append(f"[待裁决] {n} 条冲突，用 mem.py conflicts 看")

        text = "\n".join(out)
        if args.max_chars and len(text) > args.max_chars:
            text = text[: args.max_chars].rsplit("\n", 1)[0] + "\n…（已截断）"
        if not text:
            print("  （记忆库里还没有可作为前言的条目）")
            return 0
        print(text)
        return 0


def cmd_list(args: argparse.Namespace) -> int:
    with open_store() as store:
        rows = store.list_episodes(args.user, args.limit)
        if not rows:
            print("  （没有记忆）")
        for r in rows:
            state = "当前" if r["valid_until"] is None else "已失效"
            ttl = r["expires_at"].strftime("%Y-%m-%d") if r["expires_at"] else "不过期"
            print(
                f"  [{state}][{r['kind']:<8}][review={r['review_state']:<7}] ttl={ttl:<11} "
                f"use={r['use_count']} {str(r['id'])[:8]} {r['content'][:48]}"
            )
        return 0


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mem.py", description="把记忆写进 PostgreSQL")
    p.add_argument("--user", default=DEFAULT_USER, help="记忆归属（默认 owner / MEM_USER_ID）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("doctor", help="体检")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("init", help="装载 sql/01..07（幂等，可重复跑）")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("add", help="写一条情景记忆")
    sp.add_argument("text")
    sp.add_argument("--kind", default="episode",
                    choices=["raw_turn", "episode", "summary", "longterm"])
    sp.add_argument("--confidence", type=float, default=0.9)
    sp.add_argument("--importance", type=float, default=0.6)
    sp.add_argument("--source", default="human",
                    choices=["conversation", "tool", "human", "import"])
    sp.add_argument("--summary", default=None)
    sp.add_argument("--tags", nargs="*", default=None)
    sp.add_argument("--action", default="accept",
                    choices=["accept", "merge", "defer", "drop"])
    sp.add_argument("--target", default=None, help="merge/defer 的目标 memory_id")
    sp.add_argument("--max-dist", dest="max_dist", type=float, default=0.35,
                    help="候选余弦距离阈值（HashEmbedder 建议 0.95）")
    sp.add_argument("--entity", nargs="*", default=None,
                    help="这条记忆涉及的实体名（会建实体并挂图）")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("recall", help="混合召回")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=5)
    sp.add_argument("--include-pending", action="store_true")
    sp.add_argument("--full", action="store_true", help="打印全文（渐进披露第二层）")
    sp.add_argument("--tags", nargs="*", default=None, help="标签过滤（要求 tags 有交集）")
    sp.add_argument("--importance-weight", dest="importance_weight", type=float,
                    default=0.25,
                    help="重要性权重 0~1（0=不参与打分，默认 0.25；调大会压过 RRF 名次差）")
    sp.add_argument("--no-graph", dest="no_graph", action="store_true",
                    help="关掉第三路（图扩展）")
    sp.add_argument("--mark-used", dest="mark_used", action="store_true",
                    help="把本次命中记为一次真实使用（use_count+1）；默认不记")
    sp.set_defaults(func=cmd_recall)

    sp = sub.add_parser("list", help="列出记忆")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("graph", help="实体/关系（第三路召回）")
    gsub = sp.add_subparsers(dest="op", required=True)
    g = gsub.add_parser("add", help="建实体")
    g.add_argument("name")
    g.add_argument("--label", default="concept",
                   choices=["person", "org", "project", "system", "place", "concept"])
    g.add_argument("--alias", nargs="*", default=None)
    g.set_defaults(func=cmd_graph)
    g = gsub.add_parser("relate", help="建关系（自动建两端实体）")
    g.add_argument("src")
    g.add_argument("pred", help="小写下划线谓词，如 belongs_to")
    g.add_argument("dst")
    g.add_argument("--confidence", type=float, default=0.8)
    g.set_defaults(func=cmd_graph)
    g = gsub.add_parser("link", help="把一条记忆挂到已有实体")
    g.add_argument("id")
    g.add_argument("--entity", nargs="*", required=True)
    g.set_defaults(func=cmd_graph)
    g = gsub.add_parser("stats", help="实体/关系清单")
    g.add_argument("--limit", type=int, default=50)
    g.set_defaults(func=cmd_graph)
    g = gsub.add_parser("backfill", help="把存量记忆挂到已有实体（一次性）")
    g.add_argument("--limit", type=int, default=200)
    g.set_defaults(func=cmd_graph)
    g = gsub.add_parser("neighbors", help="从实体出发走 N 跳")
    g.add_argument("--seed", nargs="*", default=None)
    g.add_argument("--query", default=None, help="或用一句查询（取其中已知实体）")
    g.add_argument("--depth", type=int, default=2)
    g.set_defaults(func=cmd_graph)
    g = gsub.add_parser("route", help="看第三路会给召回哪些记忆")
    g.add_argument("query")
    g.add_argument("--depth", type=int, default=2)
    g.add_argument("--limit", type=int, default=30)
    g.set_defaults(func=cmd_graph)

    sp = sub.add_parser("purge", help="整用户物理清除（不可恢复）")
    sp.add_argument("--yes", action="store_true", help="真的执行（默认只打印清单）")
    sp.set_defaults(func=cmd_purge)

    sp = sub.add_parser("digest", help="日/周/月/年关键词提炼")
    dsub = sp.add_subparsers(dest="op", required=True)
    d = dsub.add_parser("build", help="构建时段摘要（幂等，可重复跑）")
    d.add_argument("--period", default="week",
                   choices=["day", "week", "month", "year", "all"])
    d.add_argument("--from", dest="from_date", default=None, help="起始日期 YYYY-MM-DD")
    d.add_argument("--to", dest="to_date", default=None, help="结束日期（含）")
    d.add_argument("--top", type=int, default=12, help="每期保留几个关键词")
    d.add_argument("--min-count", dest="min_count", type=int, default=2,
                   help="词项最少出现次数（记忆少于 3 条时自动放宽）")
    d.add_argument("--include-empty", dest="include_empty", action="store_true",
                   help="空桶也建（默认跳过，不产生垃圾行）")
    d.set_defaults(func=cmd_digest)
    d = dsub.add_parser("show", help="近几期摘要")
    d.add_argument("--period", default="all",
                   choices=["day", "week", "month", "year", "all"])
    d.add_argument("--limit", type=int, default=10)
    d.add_argument("--full", action="store_true", help="打印关键词列表")
    d.set_defaults(func=cmd_digest)
    d = dsub.add_parser("keywords", help="跨期关键词排行")
    d.add_argument("--period", default="all",
                   choices=["day", "week", "month", "year", "all"])
    d.add_argument("--limit", type=int, default=15)
    d.set_defaults(func=cmd_digest)
    d = dsub.add_parser("stats", help="每期覆盖情况")
    d.set_defaults(func=cmd_digest)

    sp = sub.add_parser("context", help="会话前言：事实 + 近期摘要 + 进行中任务")
    sp.add_argument("--fact-limit", dest="fact_limit", type=int, default=12)
    sp.add_argument("--no-tasks", dest="with_tasks", action="store_false")
    sp.add_argument("--with-conflicts", dest="with_conflicts", action="store_true")
    sp.add_argument("--max-chars", dest="max_chars", type=int, default=1200)
    sp.set_defaults(func=cmd_context)

    sp = sub.add_parser("fact", help="长期事实")
    sp.add_argument("op", choices=["set", "get"])
    sp.add_argument("key", nargs="?", default=None)
    sp.add_argument("value", nargs="?", default=None)
    sp.add_argument("--confidence", type=float, default=0.9)
    sp.add_argument("--importance", type=float, default=0.6)
    sp.add_argument("--source", default="human",
                    choices=["conversation", "tool", "human", "import"])
    sp.set_defaults(func=cmd_fact)

    sp = sub.add_parser("conflicts", help="待裁决冲突")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--resolve", choices=["superseded", "kept_old", "merged"], default=None)
    sp.set_defaults(func=cmd_conflicts)

    sp = sub.add_parser("forget", help="软删除")
    sp.add_argument("id")
    sp.add_argument("--reason", default=None)
    sp.set_defaults(func=cmd_forget, op="forget")

    sp = sub.add_parser("restore", help="恢复")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_forget, op="restore")

    sp = sub.add_parser("health", help="健康度与队列")
    sp.set_defaults(func=cmd_health)

    # ---- distill 提炼 ----
    sp = sub.add_parser("distill", help="把 raw_turn 压成 summary（保留血缘）")
    dsub = sp.add_subparsers(dest="op", required=True)

    d = dsub.add_parser("status", help="看待提炼积压")
    d.add_argument("--min-age-days", dest="min_age_days", type=float, default=7)
    d.add_argument("--limit", type=int, default=10)
    d.set_defaults(func=cmd_distill, backend=None, batch_size=20, max_batches=5,
                   close_raw=False, limit_chars=600, token_budget=3000, task=None,
                   text=None, file=None, summary=None)

    d = dsub.add_parser("run", help="自动提炼（heuristic 后端）")
    d.add_argument("--backend", default="heuristic", choices=["heuristic"])
    d.add_argument("--min-age-days", dest="min_age_days", type=float, default=7)
    d.add_argument("--batch-size", dest="batch_size", type=int, default=20)
    d.add_argument("--max-batches", dest="max_batches", type=int, default=5)
    d.add_argument("--limit-chars", dest="limit_chars", type=int, default=600)
    d.add_argument("--close-raw", dest="close_raw", action="store_true",
                   help="提炼后提前闭合原始轮次（默认让它们按 TTL 自然过期）")
    d.set_defaults(func=cmd_distill, limit=10, token_budget=3000, task=None,
                   text=None, file=None, summary=None)

    d = dsub.add_parser("plan", help="建提炼任务 + 工作集，交给人/Agent 写摘要")
    d.add_argument("--min-age-days", dest="min_age_days", type=float, default=7)
    d.add_argument("--batch-size", dest="batch_size", type=int, default=20)
    d.add_argument("--token-budget", dest="token_budget", type=int, default=3000)
    d.set_defaults(func=cmd_distill, backend=None, max_batches=5, close_raw=False,
                   limit_chars=600, limit=10, task=None, text=None, file=None,
                   summary=None)

    d = dsub.add_parser("apply", help="把写好的摘要写回（关闭对应任务）")
    d.add_argument("task")
    d.add_argument("--text", default=None)
    d.add_argument("--file", default=None)
    d.add_argument("--summary", default=None, help="一句话摘要（默认取正文首行）")
    d.add_argument("--close-raw", dest="close_raw", action="store_true")
    d.set_defaults(func=cmd_distill, backend=None, min_age_days=7, batch_size=20,
                   max_batches=5, limit_chars=600, limit=10, token_budget=3000)
    # ---- 任务状态层 ----
    sp = sub.add_parser("task", help="任务状态（按键直查，不检索）")
    tsub = sp.add_subparsers(dest="op", required=True)

    t = tsub.add_parser("start", help="开任务")
    t.add_argument("--kind", required=True, help="任务类型，如 research / deploy / review")
    t.add_argument("--title", required=True)
    t.add_argument("--meta", default=None, help="JSON 元数据")
    t.add_argument("--retention-hours", dest="retention_hours", type=float, default=None)
    t.set_defaults(func=cmd_task)
    t.set_defaults(events=10, limit=20, all=False, id=None, status=None, step=None,
                   step_index=None, progress=None, result=None, error=None)

    t = tsub.add_parser("update", help="推进任务")
    t.add_argument("id")
    t.add_argument("--status", default=None,
                   choices=["pending", "running", "blocked", "done", "failed", "cancelled"])
    t.add_argument("--step", default=None)
    t.add_argument("--step-index", dest="step_index", type=int, default=None)
    t.add_argument("--progress", type=float, default=None, help="0~1")
    t.add_argument("--result", default=None, help="JSON")
    t.add_argument("--error", default=None)
    t.set_defaults(func=cmd_task, events=10, limit=20, all=False, kind=None, title=None,
                   meta=None, retention_hours=None)

    for op, hlp in (("done", "标记完成"), ("fail", "标记失败（需 --error）"), ("cancel", "取消")):
        t = tsub.add_parser(op, help=hlp)
        t.add_argument("id")
        t.add_argument("--result", default=None, help="JSON")
        t.add_argument("--error", default=None)
        t.set_defaults(func=cmd_task, op=op, events=10, limit=20, all=False, kind=None,
                       title=None, meta=None, retention_hours=None, status=None, step=None,
                       step_index=None, progress=None)

    t = tsub.add_parser("get", help="看任务 + 事件流")
    t.add_argument("id")
    t.add_argument("--events", type=int, default=10)
    t.set_defaults(func=cmd_task, limit=20, all=False, kind=None, title=None, meta=None,
                   retention_hours=None, status=None, step=None, step_index=None,
                   progress=None, result=None, error=None)

    t = tsub.add_parser("list", help="列出任务")
    t.add_argument("--all", action="store_true", help="含终态")
    t.add_argument("--limit", type=int, default=20)
    t.set_defaults(func=cmd_task, events=10, kind=None, title=None, meta=None,
                   retention_hours=None, id=None, status=None, step=None, step_index=None,
                   progress=None, result=None, error=None)

    # ---- 工作记忆层 ----
    sp = sub.add_parser("work", help="工作记忆（显式装配，不检索）")
    wsub = sp.add_subparsers(dest="op", required=True)

    w = wsub.add_parser("open", help="开工作集（同一 task 复用）")
    w.add_argument("--task", default=None)
    w.add_argument("--session", default=None)
    w.add_argument("--note", default=None)
    w.add_argument("--budget", type=int, default=1500, help="token 预算")
    w.add_argument("--ttl-hours", dest="ttl_hours", type=float, default=None)
    w.set_defaults(func=cmd_work, kind=None, value=None, set=None, full=False)

    for kind, hlp in (("note", "pin 一段自由文本"), ("memory", "pin 一条情景记忆（快照）"),
                      ("fact", "pin 一条长期事实（按键取）"), ("task", "pin 任务当前状态")):
        w = wsub.add_parser(f"pin-{kind}", help=hlp)
        w.add_argument("set")
        w.add_argument("value", help="note=文本 / memory=episode id / fact=key / task=task id")
        w.add_argument("--full", action="store_true", help="memory: 用 content 而不是 summary")
        w.set_defaults(func=cmd_work, kind=kind, budget=None, ttl_hours=None,
                       task=None, session=None, note=None)

    w = wsub.add_parser("show", help="看工作集条目")
    w.add_argument("set")
    w.set_defaults(func=cmd_work, kind=None, value=None, budget=None, ttl_hours=None,
                   task=None, session=None, note=None, full=False)

    w = wsub.add_parser("render", help="按预算装配成文本")
    w.add_argument("set")
    w.add_argument("--budget", type=int, default=None)
    w.set_defaults(func=cmd_work, kind=None, value=None, ttl_hours=None, task=None,
                   session=None, note=None, full=False)

    w = wsub.add_parser("clear", help="清空条目（保留工作集）")
    w.add_argument("set")
    w.set_defaults(func=cmd_work, kind=None, value=None, budget=None, ttl_hours=None,
                   task=None, session=None, note=None, full=False)

    w = wsub.add_parser("expire", help="删除过期工作集")
    w.set_defaults(func=cmd_work, kind=None, value=None, set=None, budget=None,
                   ttl_hours=None, task=None, session=None, note=None, full=False)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
