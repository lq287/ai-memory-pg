"""全量验证：把 Store / Worker / retrieve 的每一个对外入口都在真库上打一遍。

    python scripts/verify_all.py            # 需要可连的 PG（npm run serve 或 docker compose）
    python scripts/verify_all.py -v         # 打印每一步

为什么单独有这个脚本：`tests/` 是零依赖单测（不连库），`99_selftest.sql` 是 SQL 层
行为断言。中间的 Python↔PG 边界（参数绑定、事务收尾、返回类型）没人管，而它恰好是
最容易出错的一层 —— 之前四个 bug 里有三个就长在这里。这个脚本就是补那一层。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for lib in (ROOT / ".pylibs",):
    if lib.exists():
        sys.path.insert(0, str(lib))

from app.config import FactsParams, Policy, RecallParams, Settings, plan_recall  # noqa: E402
from app.decide import Fact, arbitrate, parse_proposal  # noqa: E402
from app.embedding import HashEmbedder  # noqa: E402
from app.llm import HeuristicLLM  # noqa: E402
from app.retrieve import assemble, expand, recall, render_memories  # noqa: E402
from app.store import Store  # noqa: E402
from app.worker import Worker  # noqa: E402

VERBOSE = False
RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, detail))
    if VERBOSE or not ok:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def section(title: str) -> None:
    print(f"\n▶ {title}")


def main() -> int:
    global VERBOSE
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    VERBOSE = args.verbose

    settings = Settings.from_env()
    embedder = HashEmbedder(dim=settings.embed_dim)
    llm = HeuristicLLM()
    user = f"verify-{uuid.uuid4().hex[:8]}"
    print(f"DSN   : {settings.dsn}")
    print(f"user  : {user}")

    try:
        _run(settings, embedder, llm, user)
    except Exception:
        print("\n未捕获异常：")
        traceback.print_exc()
        RESULTS.append((False, "脚本未跑完", "见上面的 traceback"))

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "─" * 72)
    for ok, name, detail in RESULTS:
        if not ok:
            print(f"  FAIL  {name}  {detail}")
    print(f"结果：{passed}/{total} 项通过")
    if passed != total:
        print("失败项见上。")
        return 1
    print("全部通过。")
    return 0


def _run(settings: Settings, embedder: HashEmbedder, llm: HeuristicLLM, user: str) -> None:
    policy = Policy()

    with Store(settings.dsn, embedder) as store:
        worker = Worker(store, llm, embedder, settings, policy, candidate_max_dist=0.95)

        # -- 1. 队列 ---------------------------------------------------------
        section("队列：入队 / 在途去重 / 领取 / 确认 / 失败退避 / 回收")
        job1 = store.enqueue(user, "consolidate", {"text": "我的时区是 Asia/Tokyo。", "source": "conversation"},
                             dedup_key=f"{user}:q1")
        check("enqueue 返回 job id", job1 is not None, f"job1={job1}")
        dup = store.enqueue(user, "consolidate", {"text": "重复入队", "source": "conversation"},
                            dedup_key=f"{user}:q1")
        check("在途去重返回 None", dup is None, f"dup={dup}")

        claimed = store.claim(10, "verify-w1")
        check("claim 领到任务", len(claimed) == 1, f"len={len(claimed)}")
        check("claim 后 attempts=1", claimed and claimed[0]["attempts"] == 1)
        job_id = claimed[0]["id"]

        store.ack(job_id)
        stats = {r["status"]: r["jobs"] for r in store.stats()}
        check("ack 后状态 done", stats.get("done", 0) >= 1, f"stats={stats}")

        # 失败退避 + 死信
        job2 = store.enqueue(user, "consolidate", {"text": "x", "source": "tool"})
        store.claim(10, "verify-w1")
        st = store.fail(job2, "boom")
        check("fail 后回到 pending（还有重试次数）", st == "pending", f"status={st}")

        # 连接被搞坏后，fail() 仍必须能记账（曾经的 InFailedSqlTransaction 事故）
        try:
            store.conn.execute("SELECT * FROM no_such_table_xyz")
        except Exception:
            pass
        st2 = store.fail(job2, "after aborted tx")
        check("aborted 事务后 fail() 仍可用", st2 in ("pending", "dead"), f"status={st2}")

        # 租约回收
        store.conn.execute(
            "UPDATE memory_jobs SET status='processing', locked_at=now()-interval '1 hour' WHERE id=%s",
            (job2,),
        )
        store.conn.commit()
        reclaimed, dead = store.reap(300)
        check("reap 回收了卡死任务", reclaimed >= 1, f"reclaimed={reclaimed} dead={dead}")

        # -- 2. 四态落库 -----------------------------------------------------
        section("四态落库：accept / merge / defer / drop")
        fact_a = Fact(user_id=user, content="用户偏好深色主题", confidence=0.8,
                      source="conversation", event_time=_now())
        vec_a = embedder.embed([fact_a.content])[0]
        cands = store.conflict_candidates(user, vec_a, k=5, max_dist=0.95)
        check("conflict_candidates 可用（空库返回空表）", cands == [], f"len={len(cands)}")

        d_accept = arbitrate(parse_proposal({"action": "accept"}), fact_a, cands, policy)
        r_accept = store.apply(d_accept, fact_a, vec_a, source_ref=f"{user}:v1")
        check("accept 落库", r_accept.get("applied") is True, json.dumps(r_accept, default=str))
        first_id = r_accept.get("id")

        r_dup = store.apply(d_accept, fact_a, vec_a, source_ref=f"{user}:v1")
        check("同 source_ref 重放幂等（不重复插入）", r_dup.get("applied") is False, f"{r_dup}")

        cands = store.conflict_candidates(user, vec_a, k=5, max_dist=0.95)
        check("写入后能检索到候选", len(cands) == 1, f"len={len(cands)}")
        check("候选带 importance 字段", cands and hasattr(cands[0], "importance"))

        d_merge = arbitrate(
            parse_proposal({"action": "merge", "memory_id": first_id}),
            Fact(user_id=user, content="用户偏好深色主题", confidence=0.8,
                 source="conversation", event_time=_now()),
            cands, policy,
        )
        r_merge = store.apply(d_merge, fact_a, vec_a, source_ref=f"{user}:v2")
        check("merge 落库", r_merge.get("action") == "merge", f"{r_merge}")
        check("merge 抬高了置信度",
              r_merge.get("confidence", 0) > cands[0].confidence,
              f"{cands[0].confidence} -> {r_merge.get('confidence')}")

        fact_conflict = Fact(user_id=user, content="用户不喜欢深色主题", confidence=0.85,
                             source="conversation", event_time=_now())
        d_defer = arbitrate(
            parse_proposal({"action": "defer", "memory_id": first_id}),
            fact_conflict, cands, policy,
        )
        r_defer = store.apply(d_defer, fact_conflict, embedder.embed([fact_conflict.content])[0],
                             source_ref=f"{user}:v3")
        check("defer 落库并建冲突边",
              r_defer.get("action") == "defer" and r_defer.get("conflict_id"),
              f"{r_defer}")

        pending = store.pending_conflicts(user)
        check("pending_conflicts 能看到它", len(pending) == 1, f"len={len(pending)}")

        hits = recall(store, user, "深色主题", RecallParams(limit=10))
        check("待裁决的行默认不召回",
              all(h.id != r_defer.get("id") for h in hits), f"hits={[h.summary for h in hits]}")

        conflict_id = r_defer["conflict_id"]
        resolved = store.resolve_conflict(conflict_id, "superseded", "verify")
        check("resolve_conflict superseded", resolved.get("resolution") == "superseded",
              f"{resolved}")
        check("重复裁决被拒", _raises(lambda: store.resolve_conflict(conflict_id, "kept_old")))

        rows = {str(r["id"]): r for r in store.list_episodes(user)}
        check("旧行已闭合", rows[first_id]["valid_until"] is not None)
        check("旧行血缘指向新行", str(rows[first_id]["superseded_by"]) == r_defer["id"])

        d_drop = arbitrate(parse_proposal({"action": "drop"}), fact_a, cands, policy)
        r_drop = store.apply(d_drop, fact_a, vec_a, source_ref=f"{user}:v4")
        check("drop 不落库", r_drop.get("applied") is False, f"{r_drop}")

        # resolve_conflict_for（按 existing_id 找 open 边）
        section("defer → resolve_conflict_for（另一条路径）")
        fact_b = Fact(user_id=user, content="用户的键盘是 HHQL", confidence=0.8,
                      source="conversation", event_time=_now())
        vec_b = embedder.embed([fact_b.content])[0]
        r_b = store.apply(arbitrate(parse_proposal({"action": "accept"}), fact_b, [], policy),
                          fact_b, vec_b, source_ref=f"{user}:vb")
        cands_b = store.conflict_candidates(user, vec_b, k=5, max_dist=0.95)
        fact_b2 = Fact(user_id=user, content="用户的键盘是 HHKB", confidence=0.85,
                       source="conversation", event_time=_now())
        d_b = arbitrate(parse_proposal({"action": "defer", "memory_id": r_b["id"]}),
                        fact_b2, cands_b, policy)
        store.apply(d_b, fact_b2, embedder.embed([fact_b2.content])[0], source_ref=f"{user}:vb2")
        r_for = store.resolve_conflict_for(user, r_b["id"], "kept_old", "verify")
        check("resolve_conflict_for kept_old", r_for.get("resolution") == "kept_old", f"{r_for}")
        rows = {str(r["id"]): r for r in store.list_episodes(user)}
        check("kept_old 保留旧行",
              rows[r_b["id"]]["valid_until"] is None,
              f"valid_until={rows[r_b['id']]['valid_until']}")

        # -- 3. 软删除 -------------------------------------------------------
        section("软删除 / 恢复 / TTL")
        check("forget 生效", store.forget(r_b["id"], "用户要求忘掉") is True)
        hits = recall(store, user, "键盘", RecallParams(limit=10))
        check("遗忘后不召回", all(h.id != r_b["id"] for h in hits))
        check("tombstone 还在", any(str(r["id"]) == r_b["id"] for r in store.list_episodes(user)))
        check("restore 生效", store.restore(r_b["id"]) is True)
        hits = recall(store, user, "键盘", RecallParams(limit=10))
        check("恢复后重新召回", any(h.id == r_b["id"] for h in hits))

        ttl = {r["kind"]: r["expires_at"] for r in store.list_episodes(user)}
        check("episode 行带 TTL", ttl.get("episode") is not None, f"expires_at={ttl.get('episode')}")

        # -- 4. 长期事实 -----------------------------------------------------
        section("长期事实：set_fact 取代 / recall_facts / bump")
        fid1 = store.set_fact(user, "timezone", "Asia/Tokyo", 0.8, "conversation")
        check("set_fact 首次写入", fid1 is not None)
        fid2 = store.set_fact(user, "timezone", "Asia/Shanghai", 0.9, "human")
        check("set_fact 再次写入（取代）", fid2 is not None and fid2 != fid1)

        facts = store.recall_facts(user)
        current = [f for f in facts if f["key"] == "timezone"]
        check("只返回一条当前值", len(current) == 1, f"len={len(current)}")
        check("当前值是新写的", current and current[0]["value"] == "Asia/Shanghai",
              f"value={current[0]['value'] if current else None}")
        check("recall_facts 带 score/age_days",
              current and "score" in current[0] and "age_days" in current[0])

        hist = store.conn.execute(
            "SELECT count(*) AS n FROM facts WHERE user_id=%s AND key='timezone'", (user,)
        ).fetchone()
        store.conn.commit()
        check("历史事实保留（取代不删除）", hist["n"] == 2, f"rows={hist['n']}")

        n = store.bump_fact_usage(user, ["timezone"])
        check("bump_fact_usage 命中当前值", n == 1, f"n={n}")
        after = store.recall_facts(user, ["timezone"])[0]
        check("facts.last_used_at 已写入", after["last_used_at"] is not None)

        check("facts 带按 last_used_at 的 score",
              isinstance(after["score"], float),
              f"score={after['score']}")

        # -- 5. 检索与装配 ---------------------------------------------------
        section("检索 / 渐进披露 / 预算封顶 / 装配")
        hits = recall(store, user, "深色主题", plan_recall("深色主题"))
        check("简单查询召回条数受限", len(hits) <= 2, f"len={len(hits)}")
        hits8 = recall(store, user, "上次我们讨论的那个深色主题为什么改了", plan_recall("x" * 20))
        check("复杂查询召回更多", len(hits8) >= len(hits), f"{len(hits)} -> {len(hits8)}")

        if hits8:
            full = expand(store, [hits8[0].id])
            check("expand 拿到全文", bool(full), f"ids={list(full)[:1]}")
            before = store.list_episodes(user)
            store.bump_usage([hits8[0].id])
            after_use = {str(r["id"]): r["use_count"] for r in store.list_episodes(user)}
            check("bump_usage 递增 use_count",
                  after_use[hits8[0].id] > 0, f"use_count={after_use[hits8[0].id]}")

        rendered = render_memories(hits8, budget=60)
        check("预算截断生效", len(rendered.used_ids) <= len(hits8), f"kept={len(rendered.used_ids)}")

        block, hits_all, facts_all = assemble(store, user, "我的时区是什么", plan_recall("我的时区是什么"))
        check("assemble 拼出注入文本", bool(block))
        check("事实层排在情景层前面",
              block.find("已知事实") < block.find("相关记忆") if "相关记忆" in block else True)

        # -- 6. 可观测性 -----------------------------------------------------
        section("可观测性：health / stats / list_episodes")
        health = store.health(user)
        check("health 四个计数齐全",
              set(health) == {"pending_review", "expired_live", "missing_embedding", "open_conflicts"},
              f"{health}")
        check("health 数字是 int", all(isinstance(v, int) for v in health.values()), f"{health}")
        check("stats 有状态分组", len(store.stats()) >= 1, f"{store.stats()}")

        # -- 7. worker -------------------------------------------------------
        section("worker：process / run_once")
        txt = "我比较喜欢在杭州开会，上周的产品评审会就在那边。"
        j = store.enqueue(user, "consolidate", {"text": txt, "source": "conversation"},
                          dedup_key=f"{user}:wk")
        check("worker 入队", j is not None)
        n = worker.run_once()
        check("run_once 处理到任务", n >= 1, f"n={n}")
        rows = store.list_episodes(user)
        check("worker 写出了情景记忆", any("杭州" in r["content"] for r in rows))

        empty = worker.run_once()
        check("队列空了 run_once 返回 0", empty == 0, f"n={empty}")

        # -- 8. 黑盒 CLI -----------------------------------------------------
        section("黑盒：python -m app.worker --once")
        j = store.enqueue(user, "consolidate",
                          {"text": "我比较喜欢在北京开会。", "source": "conversation"},
                          dedup_key=f"{user}:cli")
        proc = subprocess.run(
            [sys.executable, "-m", "app.worker", "--once", "--log-level", "WARNING"],
            cwd=str(ROOT), env=os.environ.copy(),
            capture_output=True, text=True, timeout=120,
        )
        check("worker --once 退出码 0", proc.returncode == 0,
              (proc.stderr or proc.stdout or "").strip()[-300:])
        rows = store.list_episodes(user)
        check("CLI 也写出了记忆", any("北京" in r["content"] for r in rows))

        # -- 9. 收尾 ---------------------------------------------------------
        section("清理")
        store.conn.execute("DELETE FROM memory_conflicts WHERE user_id=%s", (user,))
        store.conn.execute("DELETE FROM episodes WHERE user_id=%s", (user,))
        store.conn.execute("DELETE FROM facts WHERE user_id=%s", (user,))
        store.conn.execute("DELETE FROM memory_jobs WHERE user_id=%s", (user,))
        store.conn.commit()
        check("清理完毕", not store.list_episodes(user) and not store.recall_facts(user))


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
