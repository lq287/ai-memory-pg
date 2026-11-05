"""本地 ONNX 向量化：不联网、不需要 API key、不依赖 torch。

用途：`MEM_EMBED_MODEL` 设为 `onnx:<模型目录>` 时启用。默认模型
`Xenova/bge-base-zh-v1.5`（int8 量化，98 MB，768 维，中文专精）——
维度正好等于 `episodes.embedding vector(768)`，换上去不需要改列。

依赖只有三个（都有 cp314 wheel）：onnxruntime / tokenizers / numpy。

几个容易踩的点，写在这里省一次调试：
  1. **池化方式按模型不同**。bge 系列用 CLS（第一个 token），e5 系列用 mean。
     选错了照样出向量，但相似度会整体失真 —— 不报错，只是越来越不准。
  2. **必须 L2 归一化**。我们用的是余弦距离（`<=>`），未归一化时不同长度的文本
     分数不可比。
  3. **bge-zh-v1.5 不需要 query 指令前缀**（v1.0 才需要）。真要加，用
     `MEM_EMBED_QUERY_PREFIX` 显式配置，别偷偷加 —— 加了之后文档侧向量也得同口径。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence


def _l2(vec: list[float]) -> list[float]:
    norm = sum(v * v for v in vec) ** 0.5
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


class OnnxEmbedder:
    """基于 onnxruntime 的句向量模型。接口与 HashEmbedder / OpenAIEmbedder 一致。

    构造时只读文件，不做推理；onnxruntime 的会话是线程安全的，可复用。
    """

    def __init__(
        self,
        model_dir: str | Path,
        model: str | None = None,
        dim: int = 768,
        pooling: str | None = None,
        query_prefix: str = "",
        max_length: int = 512,
        intra_threads: int | None = None,
    ) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.dir = Path(model_dir)
        # 标签默认取**模型目录名**，而不是写死一个字符串。
        # 踩过的坑：这里曾经默认 "bge-base-zh"，而实际模型目录是
        # "bge-base-zh-v1.5"，于是没显式设 MEM_ONNX_MODEL_NAME 时
        # episodes.embedding_model 就被写成错的标签（同库出现两种标签）。
        # 那一列的职责就是标记向量空间，标签不可信等于这道防线失效。
        self.model = model or self.dir.name
        self.dim = dim
        self.query_prefix = query_prefix
        self.max_length = max_length

        onnx_path = self.dir / "model_quantized.onnx"
        if not onnx_path.exists():
            alt = self.dir / "model.onnx"
            if not alt.exists():
                raise FileNotFoundError(
                    f"{self.dir} 里没有 model_quantized.onnx 或 model.onnx"
                )
            onnx_path = alt

        tok_path = self.dir / "tokenizer.json"
        if not tok_path.exists():
            raise FileNotFoundError(f"{self.dir} 里没有 tokenizer.json（要用 fast tokenizer）")
        self.tokenizer = Tokenizer.from_file(str(tok_path))
        # tokenizers 默认不截断/不填充；这里显式开截断。
        self.tokenizer.enable_truncation(max_length=max_length)
        # PAD id 必须从 tokenizer 里取，不能写死 0：
        # XLM-R（bge-zh）的 <s>=0 / <pad>=1 / </s>=2，写死 0 等于往序列里塞 <s>，
        # 会把 CLS 向量带偏（实测 padding 会让同一句话的向量漂移 ~3e-2）。
        pad_id = self.tokenizer.token_to_id("<pad>")
        if pad_id is None:
            pad_id = 1
        self.tokenizer.enable_padding(pad_id=pad_id, pad_token="<pad>")

        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if intra_threads:
            opts.intra_op_num_threads = intra_threads
        self.session = ort.InferenceSession(str(onnx_path), opts, providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self.session.get_inputs()}
        self._output = self.session.get_outputs()[0].name

        # 池化：显式 > 读 1_Pooling/config.json > 按模型名猜
        self.pooling = pooling or self._read_pooling() or (
            "mean" if "e5" in self.model.lower() else "cls"
        )
        if self.pooling not in ("cls", "mean"):
            raise ValueError(f"不支持的 pooling: {self.pooling}（只支持 cls / mean）")

    # -- 配置 ---------------------------------------------------------------
    def _read_pooling(self) -> str | None:
        p = self.dir / "1_Pooling" / "config.json"
        if not p.exists():
            return None
        import json

        cfg = json.loads(p.read_text(encoding="utf-8"))
        if cfg.get("pooling_mode_cls_token"):
            return "cls"
        if cfg.get("pooling_mode_mean_tokens"):
            return "mean"
        return None

    # -- 推理 ---------------------------------------------------------------
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """逐条推理，**不按批拼长度**。

        为什么放弃批处理换确定性：拼批要做 padding，而补位 token 会让 CLS 向量
        带一点偏移（int8 量化下实测 ~3e-2）。同一个句子在不同批次里算出的向量
        不一样，索引里就会出现"同一文本两个向量"，重算时还会整体漂移。
        记忆库的量级（万级以内）用不上批处理，确定性更重要。
        bge-base-zh 量化版单句约 10~20 ms，1 万条约 3 分钟。
        """
        return [self._embed_one_uncached(t) for t in texts]

    def _embed_one_uncached(self, text: str) -> list[float]:
        enc = self.tokenizer.encode(text or " ")
        feeds = {
            "input_ids": [enc.ids],
            "attention_mask": [enc.attention_mask],
        }
        if "token_type_ids" in self._inputs:
            feeds["token_type_ids"] = [enc.type_ids]

        hidden = self.session.run([self._output], feeds)[0]  # [1, seq, hidden]
        rows = hidden[0]
        if self.pooling == "cls":
            vec = list(map(float, rows[0]))
        else:
            mask = enc.attention_mask
            n = max(1, sum(mask))
            acc = [0.0] * len(rows[0])
            for j, m in enumerate(mask):
                if m:
                    for k, v in enumerate(rows[j]):
                        acc[k] += float(v)
            vec = [v / n for v in acc]
        return _l2(vec)

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def embed_query(self, text: str) -> list[float]:
        """查询侧。默认与文档侧同口径；需要前缀时由 query_prefix 控制。"""
        return self.embed_one((self.query_prefix + text) if self.query_prefix else text)


def build_onnx_embedder(settings) -> OnnxEmbedder:
    """从 Settings 造 OnnxEmbedder：MEM_EMBED_MODEL=onnx:<dir> 触发。"""
    raw = settings.embed_model
    if raw.startswith("onnx:"):
        model_dir = raw.split(":", 1)[1]
    else:
        model_dir = os.environ.get(
            "MEM_ONNX_MODEL_DIR",
            str(Path(__file__).resolve().parent.parent / "vendor" / "models" / "bge-base-zh-v1.5"),
        )
    return OnnxEmbedder(
        model_dir=model_dir,
        # 不显式指定时用目录名当标签（见 __init__ 里的注释：写死字符串会撒谎）
        model=os.environ.get("MEM_ONNX_MODEL_NAME") or None,
        dim=settings.embed_dim,
        pooling=os.environ.get("MEM_ONNX_POOLING") or None,
        query_prefix=os.environ.get("MEM_EMBED_QUERY_PREFIX", ""),
    )
