"""重算已有记忆的向量（换 embedding 模型后必做）。

    python scripts/reembed.py --check                 # 只比对维度/模型，不改数据
    python scripts/reembed.py                         # 重算所有向量
    python scripts/reembed.py --migrate               # 维度不一致时先改列再重算
    python scripts/reembed.py --only-outdated         # 只重算 embedding_model 对不上的行
    python scripts/reembed.py --batch 32

为什么要单独有这个脚本：换 embedding 模型 = 换向量空间。新旧向量混在一列里，
余弦距离就没有可比性了，召回会静默变差（不报错、只是越来越不准）。
`episodes.embedding_model` 列就是为这件事准备的探针：
    SELECT embedding_model, count(*) FROM episodes GROUP BY 1;

维度处理：
    episodes.embedding 是 vector(768)。换到别的维度必须改列 + 重建 HNSW 索引，
    --migrate 会自动做（DROP INDEX → ALTER COLUMN → CREATE INDEX），且整件事在一个事务里。
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

from app.config import Settings  # noqa: E402
from app.embedding import HashEmbedder, to_pgvector_literal  # noqa: E402
from app.worker import build_deps  # noqa: E402


def build_embedder(settings: Settings):
    """只造 embedder，不造 LLM（重算向量不需要模型推理）。"""
    if settings.embed_model.startswith("hash"):
        return HashEmbedder(dim=settings.embed_dim)
    if settings.embed_model.startswith("onnx"):
        from app.onnx_embed import build_onnx_embedder

        return build_onnx_embedder(settings)
    from app.embedding import OpenAIEmbedder

    base = settings.llm_base_url.rstrip("/")
    if base.endswith("/embeddings"):
        base = base[: -len("/embeddings")]
    return OpenAIEmbedder(
        base_url=base,
        api_key=settings.llm_api_key,
        model=settings.embed_model,
        dim=settings.embed_dim,
    )


HNSW_INDEX = """
CREATE INDEX idx_ep_hnsw
    ON episodes USING hnsw (embedding vector_cosine_ops)
    WITH (m = 8, ef_construction = 64)
    WHERE valid_until IS NULL AND review_state <> 'pending'
"""


def column_dim(cur) -> int | None:
    """读 episodes.embedding 的类型修饰（即 vector(N) 里的 N）。"""
    cur.execute(
        """SELECT atttypmod AS d FROM pg_attribute
           WHERE attrelid = 'episodes'::regclass AND attname = 'embedding'"""
    )
    row = cur.fetchone()
    if not row:
        return None
    # row_factory 是 dict_row，必须按列名取；用 row[0] 会 TypeError
    return row["d"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="reembed.py", description="重算记忆向量")
    p.add_argument("--dsn", default=os.environ.get("MEM_PG_DSN") or DEFAULT_DSN)
    p.add_argument("--check", action="store_true", help="只检查，不改数据")
    p.add_argument("--migrate", action="store_true", help="维度不一致时改列 + 重建索引")
    p.add_argument("--only-outdated", action="store_true", help="只重算模型对不上的行")
    p.add_argument("--batch", type=int, default=32)
    p.add_argument("--limit", type=int, default=0, help="限制处理条数（0 = 全部）")
    p.add_argument(
        "--allow-standin",
        dest="allow_standin",
        action="store_true",
        help="允许用零依赖替身(hash)覆盖真实模型的向量（默认禁止，见下方护栏）",
    )
    args = p.parse_args(argv)

    import psycopg
    from psycopg.rows import dict_row

    settings = Settings.from_env()

    with psycopg.connect(args.dsn, row_factory=dict_row) as conn:
        cur = conn.cursor()
        dim = column_dim(cur)
        conn.commit()
        print(f"DSN       : {args.dsn}")
        print(f"列维度    : vector({dim})")

        cur.execute(
            "SELECT coalesce(embedding_model,'<null>') AS m, count(*) AS n "
            "FROM episodes GROUP BY 1 ORDER BY 2 DESC"
        )
        dist = cur.fetchall()
        conn.commit()
        print("现有分布  : " + (", ".join(f"{r['m']}={r['n']}" for r in dist) or "（空表）"))

        # 没显式指定模型时，**按库里在用的模型对齐**（规则在 app.embedding 里只写一次）。
        # 不这么做的话，Settings 的默认值 hash-768 会把真实向量全部覆盖掉。
        from app.embedding import align_to_database

        aligned = align_to_database(settings, conn=conn)
        if aligned is not settings:
            print(f"自动对齐  : 库里在用的模型不是替身 → MEM_EMBED_MODEL=onnx（可用环境变量覆盖）")
            settings = aligned

        embedder = build_embedder(settings)
        print(f"embedder  : model={embedder.model}  dim={embedder.dim}")

        # ---------------------------------------------------------------
        # 护栏：不许用零依赖替身覆盖真实模型的向量。
        #
        # 这条护栏是踩出来的：本脚本一度不读库里的 embedding_model，
        # `MEM_EMBED_MODEL` 没设时 Settings 默认 hash-768，于是
        # `--only-outdated` 把 43 条 bge 向量**全部**改写成哈希向量 ——
        # 库里那列"向量空间标记"因此变成一句谎话，召回静默变差。
        # 哈希向量只配用于单测和链路验证，永远不该覆盖真实语义向量。
        # ---------------------------------------------------------------
        real_rows = sum(r["n"] for r in dist if not str(r["m"]).startswith("hash"))
        if embedder.model.startswith("hash") and real_rows and not args.allow_standin:
            print(
                f"\n[拒绝] 当前 embedder 是零依赖替身（{embedder.model}），"
                f"而库里有 {real_rows} 行真实模型向量。\n"
                "       这种情况 99% 是忘了设 MEM_EMBED_MODEL，不是真的想降级。\n"
                "       换模型请显式指定，例如：\n"
                "         $env:MEM_EMBED_MODEL='onnx'; python scripts/reembed.py\n"
                "       确实要降级成替身（仅测试库）再加 --allow-standin。"
            )
            return 5

        mismatch = dim != embedder.dim
        if mismatch:
            print(f"[!] 维度不一致：列 {dim} vs embedder {embedder.dim}")
            if not args.migrate:
                print("    加 --migrate 会自动改列并重建 HNSW 索引；否则先退出。")
                return 2
        if args.check:
            print("（--check 模式，不做修改）")
            return 0 if not mismatch else 2

        if mismatch:
            print(f"  迁移：ALTER COLUMN embedding TYPE vector({embedder.dim})")
            cur.execute("DROP INDEX IF EXISTS idx_ep_hnsw")
            cur.execute(
                f"ALTER TABLE episodes ALTER COLUMN embedding TYPE vector({embedder.dim}) "
                f"USING NULL"          # 旧向量与新区间不可比，直接清空后重算
            )
            cur.execute(HNSW_INDEX)
            conn.commit()
            print("  [OK] 列已改、索引已重建（旧向量已清空）")

        where = "TRUE"
        params: list = []
        if args.only_outdated:
            where = "(embedding IS NULL OR embedding_model <> %s)"
            params.append(embedder.model)
        cur.execute(f"SELECT id, content FROM episodes WHERE {where} ORDER BY ingested_at", params)
        rows = cur.fetchall()
        conn.commit()
        if args.limit:
            rows = rows[: args.limit]
        print(f"待重算    : {len(rows)} 行")

        done = 0
        for i in range(0, len(rows), args.batch):
            chunk = rows[i : i + args.batch]
            vectors = embedder.embed([r["content"] for r in chunk])
            for r, vec in zip(chunk, vectors):
                if len(vec) != embedder.dim:
                    print(f"  [FAIL] {r['id'][:8]} 返回维度 {len(vec)} != {embedder.dim}")
                    return 3
                cur.execute(
                    "UPDATE episodes SET embedding = %s::vector, embedding_model = %s WHERE id = %s",
                    (to_pgvector_literal(vec), embedder.model, r["id"]),
                )
            conn.commit()
            done += len(chunk)
            print(f"  {done}/{len(rows)}")

        cur.execute("SELECT count(*) AS n FROM episodes WHERE embedding IS NULL")
        left = cur.fetchone()["n"]
        conn.commit()
        print(f"完成      : 重算 {done} 行，仍缺向量 {left} 行")
        return 0 if left == 0 else 4


if __name__ == "__main__":
    raise SystemExit(main())
