"""向量化。

接口只要求 embed(texts) -> list[list[float]]，维度必须等于 episodes.embedding 的维度。

这里自带一个 HashEmbedder：确定性、零依赖、不需要任何模型或 API key。
它**不是**语义向量，只用于本地跑通管线、写单测、压 SQL。生产环境换成
OpenAIEmbedder / OllamaEmbedder 之类即可，schema 不用动 —— 但必须：

  1. 换模型 = 换向量空间 = 必须全量重算，不要新旧混用。
     episodes.embedding_model 就是为这次重算准备的特征列：
         UPDATE episodes SET ... WHERE embedding_model <> '<new model>';
  2. 降维（1536 -> 768）要走 Matryoshka 截断**并重新 L2 归一化**，
     直接截断会让余弦相似度整体偏移。
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, Sequence


class Embedder(Protocol):
    model: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


# ---------------------------------------------------------------------------
# 零依赖替身：子词哈希 + L2 归一化
# ---------------------------------------------------------------------------
_WORD = re.compile(r"[a-z0-9_]+")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _tokens(text: str) -> list[str]:
    """英文按词、中文按单字 + 相邻二字组、标点符号单独成 token。

    中文二字组是关键：单字区分度太低，整句又不重合，二字组正好对上
    "用户的说法换一层皮" 这个实际场景。
    """
    s = (text or "").lower()
    out = _WORD.findall(s)
    cjk = _CJK.findall(s)
    out.extend(f"c:{c}" for c in cjk)
    out.extend(f"b:{a}{b}" for a, b in zip(cjk, cjk[1:]))
    return out


def _bucket(token: str, dim: int, salt: int = 0) -> int:
    h = hashlib.blake2b(f"{salt}:{token}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big") % dim


class HashEmbedder:
    """确定性替身。同样的文本永远给同样的向量，所以单测可以断言排序。"""

    def __init__(self, dim: int = 768, model: str = "hash-768") -> None:
        self.dim = dim
        self.model = model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]

    def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _tokens(text):
            # 两次哈希做符号位，降低桶冲突带来的系统性偏移
            idx = _bucket(tok, self.dim, 0)
            sign = 1.0 if _bucket(tok, self.dim, 1) % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            # 空文本也要给一条合法向量，否则 HNSW 里就是 NULL distance
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]


# ---------------------------------------------------------------------------
# 生产替身：OpenAI 兼容 /embeddings（零依赖，只用标准库）
# ---------------------------------------------------------------------------
class OpenAIEmbedder:
    """任何 OpenAI 兼容端点都能用：OpenAI、DeepSeek、vLLM、Ollama、Xinference…"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "text-embedding-3-small",
        dim: int = 1536,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.timeout = timeout

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        import json
        import urllib.request

        body = json.dumps({"model": self.model, "input": list(texts)}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read())
        rows = sorted(payload["data"], key=lambda d: d["index"])
        vectors = [r["embedding"] for r in rows]
        for v in vectors:
            if len(v) != self.dim:
                raise ValueError(
                    f"维度不一致：期望 {self.dim}，实际 {len(v)}。"
                    "改维度必须同步改 episodes.embedding 并全量重算。"
                )
        return vectors


def to_pgvector_literal(vec: Sequence[float]) -> str:
    """没有 pgvector-python 适配器时，用它把向量写成 SQL 字面量。"""
    return "[" + ",".join(f"{v:.7g}" for v in vec) + "]"


class LazyEmbedder:
    """延迟构建的 Embedder 代理。

    为什么需要它：ONNX 模型的 session 初始化约 450ms，而 `doctor` / `list` /
    `fact get` / `health` / `graph stats` / `purge` 这些**只读**命令根本不需要向量。
    CLI 每次都是新进程，这 450ms 会一百次地白付。

    代理对调用方完全透明：读到 `.model` / `.dim` 或调用 `.embed()` 时才真正构建，
    所以第一次写记忆仍然会付这笔钱，而查一下事实不会。
    """

    def __init__(self, settings, factory=None) -> None:
        self._settings = settings
        self._factory = factory or build_embedder
        self._inner: Embedder | None = None

    def _get(self) -> "Embedder":
        if self._inner is None:
            self._inner = self._factory(self._settings)
        return self._inner

    @property
    def built(self) -> bool:
        return self._inner is not None

    @property
    def model(self) -> str:
        return self._get().model

    @property
    def dim(self) -> int:
        return self._get().dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self._get().embed(texts)


def align_to_database(settings, conn=None, dsn: str | None = None):
    """把 settings 的 embed_model 对齐到**库里实际在用的模型**。

    为什么必须在工厂旁边：库里已经有一列 `episodes.embedding_model` 标记向量空间，
    而 Settings 的默认值是 `hash-768`（零依赖替身）。于是**任何**忘记设
    `MEM_EMBED_MODEL` 的脚本都会用替身去算/去比真实向量 —— 不报错，只是结果全错。
    这个坑踩了两次：`mem.py`（写入口）和 `scripts/reembed.py`（把 43 条真实向量
    覆盖成了哈希向量）。所以对齐规则只写一次，所有脚本都调它。

    规则：
      · 显式设了 MEM_EMBED_MODEL 就不动（用户说了算，哪怕和库里不一致 —— 那正是换模型的场景）
      · 否则取库里出现次数最多的 embedding_model；非 hash 的真实模型 → 用本地 onnx
      · 库是空的 / 查不到 → 原样返回
    """
    import dataclasses
    import os

    if os.environ.get("MEM_EMBED_MODEL"):
        return settings

    def _query(c) -> str | None:
        row = c.execute(
            "SELECT embedding_model AS m, count(*) AS n FROM episodes "
            "WHERE embedding_model IS NOT NULL AND embedding_model <> 'unknown' "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return row["m"] if isinstance(row, dict) else row[0]

    try:
        if conn is not None:
            model = _query(conn)
        elif dsn:
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(dsn, row_factory=dict_row) as c:
                model = _query(c)
        else:
            return settings
    except Exception:
        return settings

    if model and not str(model).startswith("hash") and model != settings.embed_model:
        # 本地能提供的真实模型只有 onnx；具体标签由 onnx 层按模型目录名给出
        return dataclasses.replace(settings, embed_model="onnx")
    return settings


def build_embedder(settings, *, allow_fallback: bool = True) -> "Embedder":
    """**唯一**的 embedder 工厂。worker / reembed / mem.py 都必须走这里。

    为什么强调唯一：不同入口各自 new 一个 embedder，就会出现
    "命令行写进去的是 hash 向量、worker 写进去的是 bge 向量" ——
    两套向量空间混在同一列，余弦距离失去可比性，召回静默变差且不报错。
    （这个坑真踩过：mem.py 早期写死了 HashEmbedder。）

    MEM_EMBED_MODEL 取值：
        hash-*     零依赖确定性替身（仅跑通链路/单测，不是语义向量）
        onnx[:dir] 本地 ONNX 模型（默认 vendor/models/bge-base-zh-v1.5，768 维）
        其它       当作 OpenAI 兼容 /embeddings 的模型名
    """
    raw = (settings.embed_model or "").strip()

    if raw.startswith("hash"):
        return HashEmbedder(dim=settings.embed_dim)

    if raw.startswith("onnx"):
        from .onnx_embed import build_onnx_embedder

        return build_onnx_embedder(settings)

    if not settings.llm_base_url:
        if not allow_fallback:
            raise RuntimeError(f"MEM_EMBED_MODEL={raw} 需要 MEM_LLM_BASE_URL，但没配置")
        return HashEmbedder(dim=settings.embed_dim)

    return OpenAIEmbedder(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=raw,
        dim=settings.embed_dim,
    )
