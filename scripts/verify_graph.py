"""图扩展层（第三路召回）的独立验证。

跑法：
    $env:PYTHONPATH="$PWD/.pylibs;$PWD"
    python scripts/verify_graph.py

它验的是**行为**，不是"函数能跑通"：
  1. 实体规范化归并、别名不新增实体、label 不被降级
  2. 关系幂等、自环被拒、置信度取高
  3. 多跳遍历、深度上限、成环不死循环、只走当前有效关系
  4. 记忆挂载：未知实体不挂、已挂幂等
  5. best-effort：图操作失败不能拖死记忆写入
  6. 第三路真的改变召回：图独有记忆被带出来（rk_g 有值 / rk_k 为空）
  7. 关闭图路后结果与两路完全一致
  8. 文本认实体：最长优先 + 别名
  9. 整用户清除：二次确认 / dryrun 对账 / 跨层干净 / 不误伤他人

所有测试用户以 zz_graph_ 开头，结束时清干净。
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import RecallParams, Settings  # noqa: E402
from app.embedding import build_embedder  # noqa: E402
from app.graph import GraphMemory, GraphParams  # noqa: E402
from app.store import Store  # noqa: E402

DSN = os.environ.get("MEM_PG_DSN", "postgresql://mem:mem@127.0.0.1:5432/mem")

_ok = 0
_fail: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _ok
    if cond:
        _ok += 1
        print(f"  [PASS] {name}")
    else:
        _fail.append(name)
        print(f"  [FAIL] {name}  {detail}")


def expect_raise(name: str, fn, exc=Exception) -> None:
    try:
        fn()
    except exc:
        check(name, True)
        return
    except Exception as e:  # noqa: BLE001
        check(name, False, f"抛了 {type(e).__name__}: {e}")
        return
    check(name, False, "没有抛异常")


def seed_episode(store: Store, user: str, content: str, summary: str, importance=0.5) -> str:
    row = store._write(
        """INSERT INTO episodes (user_id, kind, content, summary, event_time, source, importance)
           VALUES (%s, 'episode', %s, %s, %s, 'tool', %s) RETURNING id""",
        (user, content, summary, datetime.now(timezone.utc), importance),
    )
    return str(row["id"])


def cleanup(store: Store, like: str = "zz_graph_%") -> None:
    with store.tx() as cur:
        cur.execute("DELETE FROM distill_links WHERE summary_id IN "
                    f"(SELECT id FROM episodes WHERE user_id LIKE '{like}')")
        cur.execute("DELETE FROM episode_entities WHERE episode_id IN "
                    f"(SELECT id FROM episodes WHERE user_id LIKE '{like}')")
        # working_items / task_events 自己没有 user_id，只能经父表找
        cur.execute("DELETE FROM working_items WHERE set_id IN "
                    f"(SELECT id FROM working_sets WHERE user_id LIKE '{like}')")
        cur.execute("DELETE FROM task_events WHERE task_id IN "
                    f"(SELECT id FROM tasks WHERE user_id LIKE '{like}')")
        for t in ("relations", "entities", "working_sets",
                  "tasks", "memory_conflicts", "memory_jobs"):
            cur.execute(f"DELETE FROM {t} WHERE user_id LIKE %s", (like,))
        cur.execute("DELETE FROM episodes WHERE user_id LIKE %s", (like,))
        cur.execute("DELETE FROM facts WHERE user_id LIKE %s", (like,))


def main() -> int:
    user = f"zz_graph_{uuid.uuid4().hex[:8]}"
    other = f"zz_graph_keep_{uuid.uuid4().hex[:6]}"
    print(f"\n=== 图扩展验证 (user={user}) ===")

    with Store(DSN, build_embedder(Settings.from_env())).connect() as store:
        g = GraphMemory(store)
        cleanup(store)

        # --- 1. 实体归并 ---------------------------------------------------
        print("\n[1] 实体规范化与归并")
        e1 = g.upsert_entity(user, "  阿里云  ", "org")
        e2 = g.upsert_entity(user, "阿里云", "concept")
        check("大小写/空白归一为同一实体", e1 == e2, f"{e1} != {e2}")
        check("只有一条实体记录", g.stats(user)["entities"] == 1)
        check("label 不被 concept 降级",
              g.entities(user)[0]["label"] == "org", str(g.entities(user)))
        g.upsert_entity(user, "阿里云", "org", ["Aliyun", "阿里云公司"])
        check("加别名不新增实体", g.stats(user)["entities"] == 1)
        al = g.entities(user)[0]["aliases"]
        check("别名落库", "Aliyun" in al and "阿里云公司" in al, str(al))
        expect_raise("非法 label 被拒", lambda: g.upsert_entity(user, "x", "spaceship"), ValueError)

        # --- 2. 关系 -------------------------------------------------------
        print("\n[2] 关系：幂等 / 自环 / 置信度")
        g.upsert_entity(user, "某集团", "org")
        g.relate(user, "阿里云", "belongs_to", "某集团", 0.9)
        g.relate(user, "阿里云", "belongs_to", "某集团", 0.95)
        rels = g.relations(user)
        check("同一关系只有一条", len(rels) == 1, f"{len(rels)} 条")
        check("置信度取较高者", abs(rels[0]["confidence"] - 0.95) < 1e-6,
              str(rels[0]["confidence"]))
        expect_raise("自环关系被拒", lambda: g.relate(user, "阿里云", "x_y", "阿里云"), Exception)
        expect_raise("非法谓词被拒",
                     lambda: g.relate(user, "阿里云", "B-A-D!", "某集团"), ValueError)

        # --- 3. 遍历 -------------------------------------------------------
        print("\n[3] 多跳遍历")
        g.relate(user, "某集团", "owns", "杭州机房", 0.7)
        nb2 = g.neighbors(user, ["阿里云"], depth=2)
        check("2 跳到达 3 个实体", len(nb2) == 3, f"{len(nb2)} 个")
        check("深度 0 是种子", [r for r in nb2 if r["depth"] == 0][0]["display"] == "阿里云")
        check("深度 2 是杭州机房",
              [r for r in nb2 if r["depth"] == 2][0]["display"] == "杭州机房")
        check("1 跳只有 2 个实体", len(g.neighbors(user, ["阿里云"], depth=1)) == 2)
        check("不存在的种子返回空", g.neighbors(user, ["不存在的东西"], depth=2) == [])

        # 反向遍历（无向）
        back = g.neighbors(user, ["杭州机房"], depth=2)
        check("从末端也能反向走到源头",
              any(r["display"] == "阿里云" for r in back), str([r["display"] for r in back]))

        # 成环：杭州机房 → 阿里云
        g.relate(user, "杭州机房", "serves", "阿里云")
        cyclic = g.neighbors(user, ["阿里云"], depth=5)
        check("成环不会死循环（仍是 3 个）", len(cyclic) == 3, f"{len(cyclic)} 个")

        # --- 4. 挂载 -------------------------------------------------------
        print("\n[4] 记忆挂载")
        ep_a = seed_episode(store, user, "阿里云用的是杭州机房", "阿里云/杭州机房", 0.7)
        ep_b = seed_episode(store, user, "某集团统一走内网审计", "集团审计", 0.7)
        ep_c = seed_episode(store, user, "本次发布窗口改到 22:00", "发布窗口", 0.3)
        check("挂到已有实体成功", g.link_episode(ep_a, ["阿里云"]) == 1)
        check("重复挂载不新增", g.link_episode(ep_a, ["阿里云"]) == 0)
        check("未知实体不挂载（也不报错）", g.link_episode(ep_a, ["根本没有这个实体"]) == 0)
        g.link_episode(ep_b, ["某集团"])
        hits = g.episodes(user, ["阿里云"], depth=2)
        ids = {str(h["episode_id"]) for h in hits}
        check("图可达 2 条记忆", len(hits) == 2, f"{len(hits)} 条")
        check("无关记忆不在图路上", ep_c not in ids)
        check("种子记忆 depth=0",
              [h for h in hits if str(h["episode_id"]) == ep_a][0]["depth"] == 0)
        # via_entity 必须对应最近那一跳（早期实现用 min(display)，会给出
        # "depth=0 却标着 depth=2 的实体"这种自相矛盾的标签）
        check("depth=0 的 via_entity 是种子本身",
              [h for h in hits if str(h["episode_id"]) == ep_a][0]["via_entity"] == "阿里云",
              str([(h["depth"], h["via_entity"]) for h in hits]))
        check("depth=1 的 via_entity 是某集团",
              [h for h in hits if str(h["episode_id"]) == ep_b][0]["via_entity"] == "某集团",
              str([(h["depth"], h["via_entity"]) for h in hits]))

        # --- 5. best-effort -------------------------------------------------
        print("\n[5] 图失败不能拖死记忆写入")
        broken = GraphMemory(store)
        original = broken.entities_in_text
        broken.entities_in_text = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        got = broken.auto_link(ep_a, user, "阿里云")
        check("auto_link 吞掉异常返回 0", got == 0)
        broken.entities_in_text = original

        # --- 6. 第三路进召回 ------------------------------------------------
        print("\n[6] 第三路真的改变召回")
        p2 = RecallParams(vec_k=0, kw_k=50, limit=10, enable_graph=False)
        p3 = RecallParams(vec_k=0, kw_k=50, limit=10, enable_graph=True)
        rows2, _, t2 = store.recall(user, "阿里云", p2, with_trace=True)
        rows3, _, t3 = store.recall(user, "阿里云", p3, with_trace=True)
        check("关闭图路时轨迹为空", t2.seeds == [] and not t2.used, f"seeds={t2.seeds}")
        check("开启图路时找到种子实体", t3.seeds == ["阿里云"], str(t3.seeds))
        check("两路只召回 1 条", len(rows2) == 1, f"{len(rows2)} 条")
        check("三路召回 2 条", len(rows3) == 2, f"{len(rows3)} 条")
        by_id3 = {str(r["id"]): r for r in rows3}
        check("图独有那条被带出来", ep_b in by_id3, str(list(by_id3)))
        if ep_b in by_id3:
            r = by_id3[ep_b]
            check("它是图路单独贡献的（rk_g 有值、rk_k 为空）",
                  r["rk_g"] is not None and r["rk_k"] is None,
                  f"rk_g={r['rk_g']} rk_k={r['rk_k']}")
        check("三路的成员严格包含两路", {str(r['id']) for r in rows2} <= set(by_id3))

        # --- 7. 关掉就等于两路 ----------------------------------------------
        print("\n[7] 关闭图路 = 两路（成员一致）")
        off = GraphParams.off()
        ids_off, trace_off = g.route(user, "阿里云", off)
        check("off 时 route 返回空", ids_off == [] and trace_off.seeds == [])
        routed, trace_on = g.route(user, "阿里云", GraphParams(depth=2, limit=30))
        check("on 时 route 有 id", len(routed) == 2, f"{len(routed)} 个")
        check("trace.describe 可读", "种子" in trace_on.describe())
        # 查询里没有已知实体 → 空集，不该报错
        none_ids, none_trace = g.route(user, "今天天气怎么样", GraphParams())
        check("查询无已知实体时静默跳过", none_ids == [] and none_trace.seeds == [])

        # --- 8. 文本认实体 --------------------------------------------------
        print("\n[8] 文本认实体")
        found = g.entities_in_text(user, "问问阿里云公司那边的情况")
        check("认得出别名", len(found) == 1 and found[0]["display"] == "阿里云",
              str([f["display"] for f in found]))
        check("不存在的实体认不出", g.entities_in_text(user, "完全无关的一句话") == [])
        check("空文本返回空", g.entities_in_text(user, "   ") == [])
        g.upsert_entity(user, "阿里", "org")   # 短名
        found2 = g.entities_in_text(user, "阿里巴巴和阿里云")
        check("长名优先（阿里云 排在 阿里 前面）",
              found2[0]["display"] == "阿里云", str([f["display"] for f in found2]))

        # --- 9. auto_link 走通 ----------------------------------------------
        print("\n[9] 写入即挂图（auto_link）")
        ep_d = seed_episode(store, user, "阿里云又说了一遍杭州机房的事", "复述", 0.4)
        n = g.auto_link(ep_d, user, "阿里云又说了一遍杭州机房的事")
        # 这句话里有 阿里云 / 杭州机房 / 阿里 三个已知实体，所以边数 > 1
        check("auto_link 挂上了边", n >= 1, f"{n} 条")
        check("其中包含 阿里云",
              g.link_episode(ep_d, ["阿里云"]) == 0)   # 第二次挂同一条应为 0
        check("图路又能多看到这条",
              any(str(h["episode_id"]) == ep_d for h in g.episodes(user, ["阿里云"], 2)))

        # --- 10. 整用户清除 --------------------------------------------------
        print("\n[10] 整用户清除")
        store.set_fact(user, "graph.test", {"v": 1}, confidence=0.9, source="tool")
        with store.tx() as cur:
            cur.execute("INSERT INTO episodes (user_id, kind, content, summary, event_time, source) "
                        "VALUES (%s,'episode','旁观者记忆','旁观',now(),'tool')", (other,))
            cur.execute("INSERT INTO facts (user_id, key, value, source) "
                        "VALUES (%s,'keep.k','\"v\"','tool')", (other,))
        g.upsert_entity(other, "旁观实体")

        plan = store.purge_dryrun(user)
        check("dryrun 给出清单", plan.get("episodes", 0) >= 4, str(plan))
        expect_raise("确认串不一致被拒", lambda: store._read(
            "SELECT * FROM purge_user(%s::text, %s::text)", (user, "wrong")), Exception)
        check("被拒后数据还在", store.purge_dryrun(user)["episodes"] >= 4)

        counts = store.purge_user(user)
        check("purge 返回计数", int(counts.get("episodes_deleted", 0)) >= 4, str(counts))
        check("关系和实体也一并删了",
              int(counts.get("relations_deleted", 0)) >= 1
              and int(counts.get("entities_deleted", 0)) >= 1, str(counts))
        after = store.purge_dryrun(user)
        check("清除后各表归零", all(v == 0 for v in after.values()), str(after))
        check("不存在的关系也清了",
              store._read("SELECT count(*) AS n FROM relations WHERE user_id=%s", (user,))[0]["n"] == 0)
        check("旁观者记忆完好",
              store._read("SELECT count(*) AS n FROM episodes WHERE user_id=%s",
                          (other,))[0]["n"] == 1)
        check("旁观者实体完好", g.stats(other)["entities"] == 1)

        # zz_graph_keep_* 也匹配 zz_graph_%，一次清完
        cleanup(store)

    print("\n" + "=" * 52)
    if _fail:
        print(f" 失败 {len(_fail)} 项 / 通过 {_ok} 项")
        for f in _fail:
            print(f"   · {f}")
        return 1
    print(f" 全部通过：{_ok} 项断言")
    print("=" * 52)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
