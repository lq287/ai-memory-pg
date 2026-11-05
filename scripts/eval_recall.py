"""召回评估：只用纯向量排序（不掺 RRF/衰减），检验 embedder 的检索质量。

    python scripts/eval_recall.py                 # 默认配置
    python scripts/eval_recall.py --prefix        # 查询侧加 bge 指令前缀
    python scripts/eval_recall.py --compare       # 两种都跑，对比 Top-1/Top-3

为什么需要专门的评估：
    "召回看起来还行"是不可证伪的。不锚定"我期望命中哪一条"，就发现不了
    "同义改写排第一、真正相关的排第六"这种问题 —— 而它在上线后表现为
    "AI 老是答非所问"，但没人能说清是哪一环坏了。

指标：Top-1 / Top-3 命中率（命中 = 期望记忆出现在前 N 名）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for lib in (ROOT / ".pylibs",):
    if lib.exists():
        sys.path.insert(0, str(lib))

DEFAULT_DSN = "postgresql://mem:mem@127.0.0.1:5432/mem"
DEFAULT_USER = os.environ.get("MEM_USER_ID", "owner")

# (查询, 期望命中的记忆里的特征串)
CASES: list[tuple[str, str, str]] = [
    ("我不想让你把我的界面弄没了", "Get-Process node", "别杀宿主"),
    ("工具把整个界面搞崩了", "Get-Process node", "别杀宿主(改写)"),
    ("编译扩展总是报错怎么办", "MSVC", "pgvector 编译"),
    ("怎么在 Windows 上编这个数据库插件", "MSVC", "编译(改写)"),
    ("下载大文件一直断", "codeload", "下载断流"),
    ("从 github 拉代码总失败", "codeload", "github(改写)"),
    ("我的脚本中文乱码了", "UTF-8", "编码 BOM"),
    ("写 ps1 文件要注意什么", "UTF-8", "编码(改写)"),
    ("winget 装东西老超时", "msstore", "winget 源"),
    ("安装程序要等很久才有反应", "setup.exe", "setup.exe --wait"),
    ("psycopg 报时区警告", "timezone", "psycopg 时区"),
    ("向量索引用的什么参数", "m=8", "HNSW 参数"),
]

PREFIX = "为这个句子生成表示以用于检索相关文章："


def cos(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def rank(embedder, rows, query: str, use_prefix: bool):
    q = (PREFIX + query) if use_prefix else query
    qv = embedder.embed_one(q)
    scored = [(cos(qv, r["embedding_vec"]), r) for r in rows]
    scored.sort(key=lambda t: -t[0])
    return scored


def evaluate(embedder, rows, use_prefix: bool, verbose: bool) -> tuple[int, int]:
    top1 = top3 = 0
    for query, needle, label in CASES:
        scored = rank(embedder, rows, query, use_prefix)
        pos = next(
            (i + 1 for i, (_s, r) in enumerate(scored) if needle.lower() in r["content"].lower()),
            None,
        )
        if pos == 1:
            top1 += 1
        if pos and pos <= 3:
            top3 += 1
        if verbose:
            mark = "✓" if pos and pos <= 3 else "✗"
            top = scored[0][1]["content"][:36]
            print(f"  {mark} [{label:<16}] 期望在第 {pos} 名  top1={top}")
    return top1, top3


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="eval_recall.py")
    p.add_argument("--dsn", default=os.environ.get("MEM_PG_DSN") or DEFAULT_DSN)
    p.add_argument("--user", default=DEFAULT_USER)
    p.add_argument("--prefix", action="store_true", help="查询侧加指令前缀")
    p.add_argument("--compare", action="store_true", help="两种都跑并对比")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    import psycopg
    from psycopg.rows import dict_row

    from app.config import Settings
    from app.worker import build_deps

    settings = Settings.from_env()
    # 对齐库里的模型：不这么做，默认值 hash-768 会让这份评估测的是替身
    # 而不是真实向量 —— 输出 "Top-1 = 0/12" 这种既正确又毫无意义的结论。
    from app.embedding import align_to_database

    settings = align_to_database(settings, dsn=args.dsn)
    embedder, _llm = build_deps(settings)
    print(f"embedder : {embedder.model} dim={embedder.dim}")

    with psycopg.connect(args.dsn, row_factory=dict_row) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, content, embedding::text AS et FROM episodes "
            "WHERE user_id=%s AND valid_until IS NULL AND embedding IS NOT NULL",
            (args.user,),
        )
        raw = cur.fetchall()
        conn.commit()

    # 把 pgvector 的文本形式解析成 float 列表（避免额外依赖）
    rows = []
    for r in raw:
        vec = [float(x) for x in r["et"].strip("[]").split(",")]
        rows.append({"id": r["id"], "content": r["content"], "embedding_vec": vec})
    print(f"语料     : {len(rows)} 条记忆, {len(CASES)} 条测试查询")
    if not rows:
        print("没有可用记忆，先跑 mem.py add")
        return 1

    if args.compare:
        for label, use in (("无前缀", False), ("有前缀", True)):
            print(f"\n--- {label} ---")
            t1, t3 = evaluate(embedder, rows, use, args.verbose)
            print(f"  Top-1 = {t1}/{len(CASES)}   Top-3 = {t3}/{len(CASES)}")
        return 0

    t1, t3 = evaluate(embedder, rows, args.prefix, True)
    print(f"\nTop-1 = {t1}/{len(CASES)}   Top-3 = {t3}/{len(CASES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
