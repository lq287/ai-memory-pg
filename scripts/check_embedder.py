"""验证本地 ONNX embedder：维度、归一化、语义区分度。"""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for lib in (ROOT / ".pylibs",):
    if lib.exists():
        sys.path.insert(0, str(lib))

from app.onnx_embed import OnnxEmbedder

MODEL_DIR = ROOT / "vendor" / "models" / "bge-base-zh-v1.5"


def cos(a, b):
    return sum(x * y for x, y in zip(a, b))  # 已 L2 归一化，点积即余弦


def main() -> int:
    print(f"model dir : {MODEL_DIR}")
    e = OnnxEmbedder(model_dir=MODEL_DIR)
    print(f"pooling   : {e.pooling}")
    print(f"inputs    : {sorted(e._inputs)}")

    pairs = [
        ("同义改写", "我的时区是 Asia/Shanghai", "用户所在的时区为 Asia/Shanghai"),
        ("同主题不同事", "我的时区是 Asia/Shanghai", "上周在杭州开了产品评审会"),
        ("字面不重合但语义近", "winget 安装总是失败", "命令行装包工具下载超时怎么处理"),
        ("完全无关", "我的时区是 Asia/Shanghai", "Python 的列表推导式怎么写"),
    ]

    v0 = e.embed_one(pairs[0][1])
    print(f"\ndim       : {len(v0)}   norm = {math.sqrt(sum(x*x for x in v0)):.6f}")
    if len(v0) != e.dim:
        print(f"[FAIL] 维度 {len(v0)} != 期望 {e.dim}")
        return 3

    print()
    prev = None
    for name, a, b in pairs:
        s = cos(e.embed_one(a), e.embed_one(b))
        print(f"  {name:<18} cos={s:+.4f}")
        if name == "同义改写":
            prev = s
        if name == "完全无关" and prev is not None and s >= prev:
            print("[FAIL] 无关句对的相似度不低于同义改写句对")
            return 4

    # 批量与单条必须一致（padding/mask 处理正确性）
    batch = e.embed(["我的时区是 Asia/Shanghai", "上周在杭州开了产品评审会"])
    single = [e.embed_one("我的时区是 Asia/Shanghai"), e.embed_one("上周在杭州开了产品评审会")]
    drift = max(abs(x - y) for a, b in zip(batch, single) for x, y in zip(a, b))
    print(f"\n批量 vs 单条 最大偏差 = {drift:.2e}")
    if drift > 1e-4:
        print("[FAIL] 批量推理与单条结果不一致（padding 掩码有问题）")
        return 5

    print("\n[OK] embedder 可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
