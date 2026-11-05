"""工作记忆层 + 任务状态层的全流程验证。

    python scripts/verify_workmem.py [-v]

覆盖的是"这两层凭什么独立存在"的那些性质：
  · 状态机：非法迁移被拒、终态不可变、failed 必须带 error
  · 事件流：每次推进都留痕，能回答"当时怎么走到这一步"
  · 快照语义：pin 之后底层记忆被取代，工作集内容**不变**
  · 装配：按 position 顺序、预算硬截断、同一输入必然同一输出（可复现）
  · 生命周期：工作集过期即删（与记忆的"取代不删除"相反）
  · 级联：删任务连带清掉它的工作集与事件
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for lib in (ROOT / ".pylibs",):
    if lib.exists():
        sys.path.insert(0, str(lib))

DEFAULT_DSN = "postgresql://mem:mem@127.0.0.1:5432/mem"

from app.config import Settings  # noqa: E402
from app.decide import Fact  # noqa: E402
from app.embedding import build_embedder  # noqa: E402
from app.store import Store  # noqa: E402
from app.workmem import WorkMemory, estimate_tokens  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []
VERBOSE = False


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, detail))
    if VERBOSE or not ok:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def raises(fn) -> bool:
    try:
        fn()
    except Exception:
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("MEM_PG_DSN") or DEFAULT_DSN)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--keep", action="store_true", help="不清理测试数据")
    args = ap.parse_args(argv)
    VERBOSE = args.verbose

    settings = Settings.from_env()
    embedder = build_embedder(settings)
    user = f"wm-{uuid.uuid4().hex[:8]}"
    print(f"DSN  : {args.dsn}")
    print(f"user : {user}")

    try:
        run(args.dsn, embedder, user, keep=args.keep)
    except Exception:
        print("\n未捕获异常：")
        traceback.print_exc()
        RESULTS.append((False, "脚本未跑完", "见 traceback"))

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    print("\n" + "─" * 70)
    for ok, name, detail in RESULTS:
        if not ok:
            print(f"  FAIL  {name}  {detail}")
    print(f"结果：{passed}/{len(RESULTS)} 项通过")
    return 0 if passed == len(RESULTS) else 1


def run(dsn: str, embedder, user: str, *, keep: bool) -> None:
    with Store(dsn, embedder) as store:
        wm = WorkMemory(store)

        # ---- 1. 状态机 ---------------------------------------------------
        print("\n> 任务状态机")
        t = wm.start_task(user, "research", "状态机验证", {"origin": "verify"})
        tid = str(t["id"])
        check("task_start 初始为 pending", t["status"] == "pending", t["status"])
        check("初始无 started_at", t["started_at"] is None)

        t = wm.update_task(tid, status="running", step="collect", step_index=1, progress=0.25)
        check("pending -> running 允许", t["status"] == "running")
        check("running 自动填 started_at", t["started_at"] is not None)
        check("step/progress 落库", t["step"] == "collect" and abs(float(t["progress"]) - 0.25) < 1e-6)

        t = wm.update_task(tid, step="analyze", step_index=2, progress=0.6)
        check("同状态内改 step 允许（自环）", t["status"] == "running" and t["step"] == "analyze")

        t = wm.update_task(tid, status="blocked")
        check("running -> blocked 允许", t["status"] == "blocked")
        t = wm.update_task(tid, status="running")
        check("blocked -> running 允许", t["status"] == "running")

        check("非法迁移 pending->done 被拒",
              raises(lambda: wm.update_task(str(wm.start_task(user, "x", "非法迁移")["id"]),
                                             status="done")))

        done = wm.finish_task(tid, "done", {"answer": 42})
        check("running -> done 允许", done["status"] == "done")
        check("终态自动填 finished_at", done["finished_at"] is not None)
        check("done -> running 被拒", raises(lambda: wm.update_task(tid, status="running")))

        # failed 必须带 error（CHECK 约束）
        t2 = wm.start_task(user, "x", "失败路径")
        t2 = wm.update_task(str(t2["id"]), status="running")
        check("failed 不带 error 被拒",
              raises(lambda: wm.update_task(str(t2["id"]), status="failed")))
        t2 = wm.update_task(str(t2["id"]), status="failed", error="boom")
        check("failed 带 error 允许", t2["status"] == "failed" and t2["error"] == "boom")

        # ---- 2. 事件流 ---------------------------------------------------
        print("\n> 任务事件流")
        task, events = wm.get_task(tid, events=20)
        kinds = [e["event"] for e in events]
        check("有 created 事件", "created" in kinds, str(kinds))
        check("有 step 事件", "step" in kinds)
        check("事件数 >= 5", len(events) >= 5, f"{len(events)}")
        check("事件按时间正序", [e["at"] for e in events] == sorted(e["at"] for e in events))

        # ---- 3. 快照语义（核心）-----------------------------------------
        print("\n> 工作记忆：pin 快照")
        fact = Fact(user_id=user, content="用户偏好深色主题",
                    confidence=0.9, source="human",
                    event_time=datetime.now(timezone.utc))
        vec = embedder.embed([fact.content])[0]
        from app.decide import arbitrate, parse_proposal
        from app.config import Policy
        d = arbitrate(parse_proposal({"action": "accept"}), fact, [], Policy())
        res = store.apply(d, fact, vec, source_ref=f"{user}:wm1")
        ep_id = res["id"]

        task3 = wm.start_task(user, "impl", "快照验证")
        ws = wm.open_set(user, task_id=str(task3["id"]), note="快照", token_budget=800)
        sid = str(ws["id"])
        item = wm.pin_memory(sid, ep_id)
        check("pin_memory 成功", item is not None and item["kind"] == "pinned_memory")
        before = item["content"]

        # 底层记忆被取代：pinned 的条目内容必须不变
        store.conn.execute(
            "UPDATE episodes SET valid_until=now(), review_state='rejected' WHERE id=%s",
            (ep_id,),
        )
        store.conn.commit()
        items = wm.items(sid)
        check("底层记忆失效后，工作集内容不变", items[0]["content"] == before,
              f"{before[:24]}...")
        check("底层记忆确实已失效",
              store._read("SELECT valid_until FROM episodes WHERE id=%s", (ep_id,))[0]["valid_until"] is not None)
        check("ref_id 保留溯源", str(items[0]["ref_id"]) == ep_id)

        # ---- 4. 装配：顺序 / 预算 / 可复现 --------------------------------
        print("\n> 工作记忆：装配")
        wm.pin_task_state(sid, str(task3["id"]))
        wm.pin_note(sid, "第二条：先验证再交付")
        # 故意写得很长，超过 800 的默认预算，才能验证截断路径
        wm.pin_note(sid, "第三条：这段故意写长，用来触发预算截断。" * 60)

        items = wm.items(sid)
        check("position 单调递增", [i["position"] for i in items] == sorted(i["position"] for i in items),
              str([i["position"] for i in items]))

        asm1 = wm.render(sid)
        asm2 = wm.render(sid)
        check("两次装配结果完全一致（可复现）", asm1.text == asm2.text and asm1.used_items == asm2.used_items)
        check("超出预算时丢条目", asm1.dropped >= 1, f"dropped={asm1.dropped} tokens={asm1.tokens}")
        check("装配结果在预算内", asm1.tokens <= int(ws["token_budget"]), f"tokens={asm1.tokens}/{ws['token_budget']}")
        check("至少保留一条", len(asm1.used_items) >= 1, f"used={len(asm1.used_items)}")

        # 紧预算：前三条合计约 48 token，所以要把预算压到 30 才真正触发逐条淘汰
        tight = wm.render(sid, budget=30)
        check("紧预算仍保留至少 1 条", len(tight.used_items) >= 1, f"used={len(tight.used_items)}")
        check("紧预算装得更少",
              tight.tokens < asm1.tokens or len(tight.used_items) < len(asm1.used_items),
              f"tokens {tight.tokens} vs {asm1.tokens}, used {len(tight.used_items)} vs {len(asm1.used_items)}")
        check("紧预算不超预算（允许首条截断的少量溢出）",
              tight.tokens <= 30 + 24, f"tokens={tight.tokens}")

        # 顺序由 position 决定，与插入时间无关
        wm.pin_note(sid, "这条插到最前面", position=0)
        items = wm.items(sid)
        check("显式 position=0 排到最前", items[0]["content"] == "这条插到最前面")
        check("新首位进入装配", wm.render(sid).text.splitlines()[1] == "这条插到最前面")

        # ---- 5. 一个任务一份工作集 ---------------------------------------
        print("\n> 工作集唯一性与清空")
        ws2 = wm.open_set(user, task_id=str(task3["id"]), note="重复开")
        check("同一 task 复用同一工作集", str(ws2["id"]) == sid, f"{str(ws2['id'])[:8]} vs {sid[:8]}")
        n = wm.clear(sid)
        check("clear 返回删除条数", n >= 4, f"n={n}")
        check("清空后 items 为空", wm.items(sid) == [])

        # ---- 6. 生命周期与时序 -------------------------------------------
        print("\n> 生命周期")
        tmp = wm.open_set(user, session_id="s-1", note="短 TTL", ttl_hours=0.0001)
        tmp_id = str(tmp["id"])
        store.conn.execute("UPDATE working_sets SET expires_at = now() - interval '1 minute' WHERE id=%s",
                           (tmp_id,))
        store.conn.commit()
        deleted, _expired = wm.expire()
        check("过期工作集被删除", wm.get_set(tmp_id) is None, f"sets_deleted={deleted}")
        check("未过期的不受影响", wm.get_set(sid) is not None)

        # ---- 7. 级联 -----------------------------------------------------
        print("\n> 级联删除")
        store.conn.execute("DELETE FROM tasks WHERE id=%s", (tid,))
        store.conn.commit()
        check("任务删除后 get_task 返回 None", wm.get_task(tid) is None)
        left = store._read("SELECT count(*) AS n FROM task_events WHERE task_id=%s", (tid,))[0]["n"]
        check("任务事件随任务级联删除", left == 0, f"left={left}")

        store.conn.execute("DELETE FROM tasks WHERE id=%s", (str(task3["id"]),))
        store.conn.commit()
        check("工作集随任务级联删除", wm.get_set(sid) is None)

        # ---- 8. 列表 -----------------------------------------------------
        print("\n> 进行中任务")
        a = wm.start_task(user, "a", "活任务A")
        b = wm.start_task(user, "b", "活任务B")
        wm.update_task(str(b["id"]), status="running")
        finished = wm.start_task(user, "c", "已完任务")
        wm.update_task(str(finished["id"]), status="running")
        wm.finish_task(str(finished["id"]), "done", {"ok": 1})
        live = wm.live_tasks(user)
        ids = {str(t["id"]) for t in live}
        check("live 只含非终态", str(a["id"]) in ids and str(b["id"]) in ids
              and str(finished["id"]) not in ids, f"live={len(live)}")
        allt = wm.all_tasks(user)
        check("all 含终态", str(finished["id"]) in {str(t["id"]) for t in allt})

        # ---- 清理 --------------------------------------------------------
        if not keep:
            store.conn.execute("DELETE FROM tasks WHERE user_id=%s", (user,))
            store.conn.execute("DELETE FROM working_sets WHERE user_id=%s", (user,))
            store.conn.execute("DELETE FROM episodes WHERE user_id=%s", (user,))
            store.conn.commit()
            check("清理完成", not wm.all_tasks(user))
        else:
            print(f"\n（--keep：数据保留在 user={user}）")


if __name__ == "__main__":
    raise SystemExit(main())
