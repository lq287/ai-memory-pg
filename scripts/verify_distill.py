"""distill 提炼层的专项验证。

    python scripts/verify_distill.py [-v]

守的是这条链路的三个致命点：
  · **不会静默丢记忆**：够老的 raw_turn 一定出现在候选里，提炼后不再重复
  · **血缘必须可追**：摘要 → 原文、原文 → 摘要，双向都能查
  · **两条后端等价可用**：heuristic 全自动；agent 走 plan/apply 人写回

另外验一个容易被忽略的点：提炼产物是**一等公民** —— 它写进 episodes(kind='summary')，
有向量、能被正常召回、TTL 是 3650 天而不是 30 天。
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for lib in (ROOT / ".pylibs",):
    if lib.exists():
        sys.path.insert(0, str(lib))

DEFAULT_DSN = "postgresql://mem:mem@127.0.0.1:5432/mem"

from app import distill  # noqa: E402
from app.config import RecallParams, Settings  # noqa: E402
from app.decide import Decision, Fact, arbitrate, parse_proposal  # noqa: E402
from app.config import Policy  # noqa: E402
from app.embedding import build_embedder  # noqa: E402
from app.store import Store  # noqa: E402
from app.workmem import WorkMemory  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []
VERBOSE = False


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, detail))
    if VERBOSE or not ok:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("MEM_PG_DSN") or DEFAULT_DSN)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    VERBOSE = args.verbose

    user = f"ds-{uuid.uuid4().hex[:8]}"
    print(f"DSN  : {args.dsn}")
    print(f"user : {user}")
    try:
        run(args.dsn, user, keep=args.keep)
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


def _seed(store: Store, user: str, turns: list[tuple[str, int, str]]) -> list[str]:
    """直接落 raw_turn（绕过裁决，测试要的是"库里已有的原始轮次"）。"""
    ids = []
    for content, age_days, ref in turns:
        fact = Fact(
            user_id=user, content=content, confidence=0.9, source="conversation",
            event_time=datetime.now(timezone.utc) - timedelta(days=age_days),
            kind="raw_turn",
        )
        vec = store.embedder.embed([content])[0]
        d = arbitrate(parse_proposal({"action": "accept"}), fact, [], Policy())
        res = store.apply(d, fact, vec, source_ref=f"{user}:{ref}")
        ids.append(res["id"])
    return ids


def run(dsn: str, user: str, *, keep: bool) -> None:
    with Store(dsn, build_embedder(Settings.from_env())) as store:
        # ---- 1. 候选筛选 -------------------------------------------------
        print("\n> 候选筛选")
        raws = _seed(store, user, [
            ("用户要求发布改走 GitHub Actions，删掉 scp 到 10.0.0.5", 12, "a"),
            ("用户说 staging 端口 55433，生产 5432", 11, "b"),
            ("刚发生的一条，还不该被提炼", 1, "fresh"),
        ])
        cands = distill.fetch_candidates(store, user, min_age_days=7)
        check("够老的 raw_turn 进入候选", len(cands) == 2, f"{len(cands)}")
        check("当天的不进入候选", all(c.id != raws[2] for c in cands))
        st = distill.pending_stats(store, user, min_age_days=7)
        check("pending 统计正确", st["pending"] == 2, str(st))
        check("oldest_days 合理", float(st["oldest_days"]) >= 10, str(st["oldest_days"]))

        # ---- 2. heuristic 全自动 -----------------------------------------
        print("\n> heuristic 后端（零依赖）")
        res = distill.distill_run(store, user, backend="heuristic", min_age_days=7, batch_size=10)
        check("产出了 1 条摘要", len(res) == 1, f"{len(res)}")
        r = res[0]
        check("覆盖 2 条原文", r.linked == 2, f"linked={r.linked}")
        check("backend 标记正确", r.backend == "heuristic")
        # 抽取式不得编造：每条要点都能在原文里找到
        joined = " ".join(c.content for c in cands)
        bullets = [ln[2:] for ln in r.text.splitlines() if ln.startswith("- ")]
        check("要点非空", len(bullets) >= 1, f"{len(bullets)}")
        check("抽取式不编造（要点均出自原文）",
              all(b[:20] in joined for b in bullets), str(bullets[:2]))

        # 提炼产物是一等公民
        row = store._read(
            "SELECT kind, expires_at, ingestion_days AS days FROM ("
            "  SELECT kind, expires_at,"
            "         EXTRACT(EPOCH FROM (expires_at - ingested_at))/86400 AS ingestion_days"
            "  FROM episodes WHERE id=%s) s", (r.summary_id,))[0]
        check("落库类型是 summary", row["kind"] == "summary", row["kind"])
        check("TTL 是 3650 天（不是 raw 的 30）", abs(float(row["days"]) - 3650) < 2, str(row["days"]))
        hits = store.recall(user, "部署 端口 约定", RecallParams(limit=5, include_pending=True))[0]
        check("摘要能被正常召回", any(str(h["id"]) == r.summary_id for h in hits),
              f"hits={len(hits)}")

        # ---- 3. 幂等 -----------------------------------------------------
        print("\n> 幂等")
        again = distill.distill_run(store, user, backend="heuristic", min_age_days=7)
        check("再跑不重复提炼", again == [], f"{len(again)}")
        n_summary = store._read(
            "SELECT count(*) AS n FROM episodes WHERE user_id=%s AND kind='summary'", (user,)
        )[0]["n"]
        check("库里仍只有 1 条摘要", n_summary == 1, f"{n_summary}")

        # ---- 4. 血缘双向 -------------------------------------------------
        print("\n> 血缘")
        srcs = store._read("SELECT * FROM distill_sources(%s)", (r.summary_id,))
        check("摘要 → 原文 可查", len(srcs) == 2, f"{len(srcs)}")
        back = store._read("SELECT * FROM distill_of_raw(%s)", (raws[0],))
        check("原文 → 摘要 可查", len(back) == 1, f"{len(back)}")
        check("反向血缘指向同一条摘要",
              str(back[0]["summary_id"]) == r.summary_id if back else False)

        # ---- 5. close_raw ------------------------------------------------
        print("\n> close_raw")
        closed = store._write("SELECT distill_close_raw(%s) AS n",
                             ([raws[0], raws[1], r.summary_id],))["n"]
        check("只闭合 raw_turn（不吃 summary）", closed == 2, f"n={closed}")
        check("summary 仍然有效",
              store._read("SELECT valid_until FROM episodes WHERE id=%s",
                          (r.summary_id,))[0]["valid_until"] is None)

        # ---- 6. agent 后端：plan / apply ---------------------------------
        print("\n> agent 后端（plan + apply）")
        user2 = user + "-agent"
        _seed(store, user2, [
            ("用户周三说：发布脚本那条 scp 删掉，改走 Actions", 10, "x"),
            ("用户补充：secret 用 STAGING_DB_URL，别硬编码", 10, "y"),
        ])
        plan = distill.distill_plan(store, user2, min_age_days=7, batch_size=10)
        check("plan 建了任务", plan.get("created") is True and plan.get("task_id"))
        check("plan 建了工作集并 pin 了原文", plan.get("count") == 2)
        wm = WorkMemory(store)
        items = wm.items(plan["set_id"])
        check("工作集条目数 = 候选数", len(items) == 2, f"{len(items)}")
        check("装配文本包含原文", "scp" in plan["assembly"] or "Actions" in plan["assembly"])

        # Agent 写摘要 → apply
        body = ("- 发布流程改走 GitHub Actions，删除手工 scp 步骤\n"
                "- Actions secret 使用 STAGING_DB_URL，禁止硬编码")
        out = distill.distill_apply_from_task(
            store, plan["task_id"], body, summary="部署改 Actions"
        )
        check("apply 落库成功", out.summary_id is not None)
        check("apply 挂上 2 条血缘", out.linked == 2, f"{out.linked}")
        check("apply 后 backend=agent", out.backend == "agent")
        task, _ev = wm.get_task(plan["task_id"])
        check("提炼任务已关闭", task["status"] == "done", task["status"])
        pend = distill.pending_stats(store, user2, min_age_days=7)
        check("agent 路径后不再有积压", pend["pending"] == 0, str(pend))

        # ---- 7. 回归：不越界 --------------------------------------------
        print("\n> 边界")
        check("raw_ids 为空应报错",
              _raises(lambda: distill.distill_apply(store, user2, [], "x")))
        check("正文为空应报错",
              _raises(lambda: distill.distill_apply(store, user2, ["0" * 36], "   ")))
        check("backend=agent 不能走自动路径",
              _raises(lambda: distill.distill_run(store, user2, backend="agent")))
        check("未知 backend 报错",
              _raises(lambda: distill.distill_run(store, user2, backend="nope")))

        # ---- 清理 --------------------------------------------------------
        if not keep:
            for u in (user, user2):
                store.conn.execute("DELETE FROM tasks WHERE user_id=%s", (u,))
                store.conn.execute("DELETE FROM episodes WHERE user_id=%s", (u,))
            store.conn.commit()
            check("清理完成",
                  not distill.fetch_candidates(store, user, min_age_days=7))
        else:
            print(f"\n（--keep：数据保留在 {user} / {user2}）")


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
