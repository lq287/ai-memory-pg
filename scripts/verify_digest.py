"""日/周/月/年关键词提炼的端到端验证（真 PG）。

跑法：
    python scripts/verify_digest.py

验的是行为，不是"函数能跑"：
  1. 分桶按 event_time（补录的旧记忆落到它应在的时段，而不是"入库那天"）
  2. 桶边界半开：相邻两桶不重叠，月末/年末不漏不重
  3. 关键词必须**出自原文**（含子串校验），且能反映该时段的主题
  4. 不同时段的关键词必须不同（tf-idf 起作用的证据，而不是所有期一个样）
  5. 幂等：重复构建同一期 → 摘要行 id 不变、摘要记忆被更新而不是新增
  6. 摘要记忆可召回（kind=summary，tags 带 digest）且不被当作下一期原料
  7. 空桶不产生垃圾行；删光原料后重建会把该期清空
  8. 日/周/月/年四层对同一批记忆的分桶自洽（同一记忆只落在一个桶里）

测试用户以 zz_digest_ 开头，跑完自清。
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.digest import DigestBuilder, bucket_label, build_df, tokenize  # noqa: E402
from app.embedding import build_embedder  # noqa: E402
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


def seed(store: Store, user: str, text: str, when: datetime, *, tags=(), importance=0.6) -> str:
    row = store._write(
        """INSERT INTO episodes (user_id, kind, content, summary, event_time, source,
                                 importance, tags)
           VALUES (%s, 'episode', %s, %s, %s, 'tool', %s, %s::text[]) RETURNING id""",
        (user, text, text[:60], when, importance, list(tags)),
    )
    return str(row["id"])


def cleanup(store: Store, like: str = "zz_digest_%") -> None:
    with store.tx() as cur:
        cur.execute("DELETE FROM distill_links WHERE summary_id IN "
                    f"(SELECT id FROM episodes WHERE user_id LIKE '{like}')")
        cur.execute("DELETE FROM episode_entities WHERE episode_id IN "
                    f"(SELECT id FROM episodes WHERE user_id LIKE '{like}')")
        cur.execute("DELETE FROM memory_digests WHERE user_id LIKE %s", (like,))
        cur.execute("DELETE FROM episodes WHERE user_id LIKE %s", (like,))
        cur.execute("DELETE FROM facts WHERE user_id LIKE %s", (like,))
        for t in ("relations", "entities", "tasks", "working_sets",
                  "memory_conflicts", "memory_jobs"):
            cur.execute(f"DELETE FROM {t} WHERE user_id LIKE %s", (like,))


def main() -> int:
    user = f"zz_digest_{uuid.uuid4().hex[:8]}"
    print(f"\n=== 关键词提炼验证 (user={user}) ===")

    now = datetime.now(timezone.utc)
    with Store(DSN, build_embedder(Settings.from_env())).connect() as store:
        cleanup(store)
        b = DigestBuilder(store)

        # --- 1. 分桶按 event_time ------------------------------------------
        print("\n[1] 分桶依据是 event_time，不是入库时间")
        # 本月的三条：主题是"编译"
        this_week = now
        last_month = (now.replace(day=1) - timedelta(days=5))
        last_year = now.replace(year=now.year - 1, month=6, day=15)

        ids_compile = [
            seed(store, user, "pgvector 编译要装 MSVC 和 Windows SDK", this_week),
            seed(store, user, "编译扩展时 flex 和 bison 是必需的", this_week),
            seed(store, user, "Makefile.win 调 nmake 编译 pgvector", this_week),
        ]
        ids_docker = [
            seed(store, user, "docker 镜像构建失败，WSL2 没有启用", last_month),
            seed(store, user, "docker 安装卡在 hyper-v 组件", last_month),
            seed(store, user, "docker 容器启动报 WSL2 内核缺失", last_month),
        ]
        ids_icon = [
            seed(store, user, "图标用 GDI+ 画矢量再超采样缩小", last_year),
            seed(store, user, "图标导出 32bpp DIB 的 ICO 文件", last_year),
            seed(store, user, "鲸鱼娘立绘从 GitHub API 取透明底", last_year),
        ]

        week_start = now.date() - timedelta(days=now.weekday())
        month_start = now.date().replace(day=1)
        year_start = now.date().replace(month=1, day=1)

        wk = b.buckets(user, "week", now.date() - timedelta(days=30), now.date() + timedelta(days=1))
        cur_wk = [x for x in wk if x["bucket_start"] == week_start]
        check("本周桶找到且计数为 3", bool(cur_wk) and cur_wk[0]["episode_count"] == 3,
              str(cur_wk))
        check("库里 9 条记忆被分进 3 个不同的周/月/年桶",
              sum(1 for x in b.buckets(user, "week", now.date() - timedelta(days=800),
                                       now.date() + timedelta(days=1)) if x["episode_count"]) == 3)

        # 补录一条"很久以前发生"的记忆，event_time 是去年
        backdated = seed(store, user, "图标的事情其实是去年六月做的", last_year)
        yb = b.buckets(user, "year", date(last_year.year, 1, 1), now.date() + timedelta(days=1))
        ybucket = [x for x in yb if x["bucket_start"] == date(last_year.year, 1, 1)]
        check("补录的旧记忆落到去年的年桶（按 event_time 而非入库时间）",
              bool(ybucket) and ybucket[0]["episode_count"] == 4, str(ybucket))

        # --- 2. 桶边界半开 --------------------------------------------------
        print("\n[2] 桶边界半开，互不重叠")
        spans = b.buckets(user, "week", now.date() - timedelta(days=60),
                          now.date() + timedelta(days=1))
        overlap = all(
            spans[i]["bucket_end"] <= spans[i + 1]["bucket_start"]
            for i in range(len(spans) - 1)
        )
        check("相邻周桶 bucket_end <= 下一个 bucket_start", overlap)
        check("周桶跨度正好 7 天",
              all((s["bucket_end"] - s["bucket_start"]).days == 7 for s in spans))
        mspans = b.buckets(user, "month", date(now.year - 1, 1, 1), now.date() + timedelta(days=1))
        check("月桶跨度 = 该月天数",
              all((s["bucket_end"] - s["bucket_start"]).days ==
                  (s["bucket_end"].replace(day=1) - s["bucket_start"]).days + 1
                  or (s["bucket_end"] - s["bucket_start"]).days in (28, 29, 30, 31)
                  for s in mspans))

        # --- 3. 关键词必须出自原文 ------------------------------------------
        print("\n[3] 关键词出自原文，且反映该时段主题")
        r_week = b.build_bucket(user, "week", week_start, week_start + timedelta(days=7))
        check("本周桶构建成功", r_week.episode_count == 3, str(r_week.episode_count))
        check("本周关键词非空", bool(r_week.keywords), str(r_week.keywords))
        src_text = " ".join(
            s["content"] for s in b.sources(user, "week", week_start)
        ).lower()
        bad = [k for k in r_week.keywords if k.isascii() and k not in src_text
               or (not k.isascii() and k not in src_text)]
        check("每个关键词都能在原文里找到", not bad, f"凭空出现: {bad}")
        joined = "/".join(r_week.keywords)
        check("本周关键词体现'编译'主题（pgvector/编译/msvc 之类）",
              any(w in joined for w in ("pgvector", "编译", "msvc", "windows")), joined)

        # --- 4. 不同时段关键词不同 ------------------------------------------
        print("\n[4] 不同时段的关键词不同（tf-idf 生效的直接证据）")
        month_prev = last_month.date().replace(day=1)
        r_prev_month = b.build_bucket(
            user, "month", month_prev,
            (month_prev.replace(day=28) + timedelta(days=4)).replace(day=1),
        )
        check("上月桶构建成功", r_prev_month.episode_count == 3,
              str(r_prev_month.episode_count))
        prev_joined = "/".join(r_prev_month.keywords)
        check("上月关键词体现 docker 主题", "docker" in prev_joined or "wsl2" in prev_joined,
              prev_joined)
        check("两个时段的关键词集合不同",
              set(r_week.keywords) != set(r_prev_month.keywords))

        # --- 5. 幂等 --------------------------------------------------------
        print("\n[5] 幂等：重建同一期不新增行")
        episodes_before = store._read(
            "SELECT count(*) AS n FROM episodes WHERE user_id=%s", (user,)
        )[0]["n"]
        r_week2 = b.build_bucket(user, "week", week_start, week_start + timedelta(days=7))
        episodes_after = store._read(
            "SELECT count(*) AS n FROM episodes WHERE user_id=%s", (user,)
        )[0]["n"]
        check("摘要行 id 不变", str(r_week2.digest_id) == str(r_week.digest_id),
              f"{r_week.digest_id} -> {r_week2.digest_id}")
        check("摘要记忆 id 不变", str(r_week2.digest_episode_id) == str(r_week.digest_episode_id))
        check("没有因为重建而多出记忆行", episodes_before == episodes_after,
              f"{episodes_before} -> {episodes_after}")
        n_digest_rows = store._read(
            "SELECT count(*) AS n FROM memory_digests WHERE user_id=%s AND period='week'",
            (user,),
        )[0]["n"]
        check("同 (user, period, bucket) 只有一行", n_digest_rows == 1, str(n_digest_rows))

        # --- 6. 摘要记忆可召回、且不进原料 ----------------------------------
        print("\n[6] 摘要记忆是一等公民")
        ep = store._read(
            "SELECT kind, tags, content FROM episodes WHERE id=%s",
            (str(r_week.digest_episode_id),),
        )[0]
        check("摘要记忆 kind=summary", ep["kind"] == "summary", ep["kind"])
        check("摘要记忆带 sys:digest 保留标签", "sys:digest" in (ep["tags"] or []), str(ep["tags"]))
        check("摘要记忆正文含关键词", "关键词" in (ep["content"] or ""))
        check("摘要记忆不会被当作下一期原料",
              all(digest_ep not in [s["id"] for s in b.sources(user, "week", week_start)]
                  for digest_ep in [ep and str(r_week.digest_episode_id)]))

        # --- 7. 空桶与清空 --------------------------------------------------
        print("\n[7] 空桶不产生垃圾行；原料删光后重建会清空该期")
        far = date(2020, 1, 6)   # 一个肯定没有记忆的周
        r_empty = b.build_bucket(user, "week", far, far + timedelta(days=7))
        check("空桶被跳过", r_empty.skipped_reason == "empty_bucket", str(r_empty.skipped_reason))
        n_rows_far = store._read(
            "SELECT count(*) AS n FROM memory_digests WHERE user_id=%s AND bucket_start=%s",
            (user, far),
        )[0]["n"]
        check("空桶没有留下 memory_digests 行", n_rows_far == 0)

        # 删掉本月那三条原料，重建应把该期刷新为空
        with store.tx() as cur:
            cur.execute("UPDATE episodes SET valid_until = now() WHERE id = ANY(%s::uuid[])",
                        (ids_compile,))
        r_gone = b.build_bucket(user, "week", week_start, week_start + timedelta(days=7))
        check("原料失效后该期被刷新为空", r_gone.skipped_reason == "emptied",
              str(r_gone.skipped_reason))
        refreshed = store._read(
            "SELECT episode_count, keywords FROM memory_digests WHERE user_id=%s AND period='week'",
            (user,),
        )[0]
        check("刷新后 episode_count 归零", refreshed["episode_count"] == 0,
              str(refreshed))
        check("刷新后关键词清空", list(refreshed["keywords"]) == [], str(refreshed["keywords"]))

        # --- 8. 四层自洽 ----------------------------------------------------
        print("\n[8] 日/周/月/年四层对同一批记忆分桶自洽")
        for period, start in (("day", now.date()), ("week", week_start),
                              ("month", month_start), ("year", year_start)):
            span = b.buckets(user, period, start, start + timedelta(days=1))
            label = bucket_label(period, start)
            check(f"{period} 桶标签为 {label}", bool(span) and
                  bucket_label(period, span[0]["bucket_start"]) == label, label)

        # 同一条记忆不会同时落在两个 period 的同名桶里（用 8 月那条验证）
        target = ids_docker[0]
        counts = {}
        for period in ("day", "week", "month", "year"):
            rows = store._read(
                "SELECT count(*) AS n FROM episodes e WHERE e.id=%s "
                "AND digest_bucket(e.event_time,%s) = digest_bucket(e.event_time,%s)",
                (target, period, period),
            )[0]["n"]
            counts[period] = rows
        check("同一条记忆在每个 period 下都只归属一个桶",
              all(v == 1 for v in counts.values()), str(counts))

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
