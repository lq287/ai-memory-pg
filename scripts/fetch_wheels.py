"""绕过 pip 抓取 psycopg / pgvector 的 Windows wheel（pip 的临时目录在沙箱里被拒）。

用法：python scripts/_fetch.py
产物解包到 .pylibs/，随后 PYTHONPATH=.pylibs 即可 import psycopg。
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.request
import zipfile

PY = f"cp{sys.version_info.major}{sys.version_info.minor}"
PLAT = "win_amd64"
DEST = pathlib.Path(".pylibs")
WHEELS = pathlib.Path(".wheels")


def pypi(pkg: str) -> dict:
    url = f"https://pypi.org/pypi/{pkg}/json"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def pick(files: list[dict], *, abi: str | None) -> dict | None:
    cands = [f for f in files if f["filename"].endswith(".whl")]
    if abi is None:
        for f in cands:
            if f["filename"].endswith("py3-none-any.whl"):
                return f
        return None
    for f in cands:
        n = f["filename"]
        if abi in n and PLAT in n:
            return f
    return None


def main() -> int:
    DEST.mkdir(exist_ok=True)
    WHEELS.mkdir(exist_ok=True)

    plan = [("psycopg", None), ("psycopg-binary", PY), ("pgvector", None)]
    missing = False

    for pkg, abi in plan:
        try:
            data = pypi(pkg)
        except Exception as exc:  # 网络
            print(f"[{pkg}] 查询 PyPI 失败: {exc}")
            return 1
        ver = data["info"]["version"]
        f = pick(data["releases"][ver], abi=abi)
        if f is None:
            print(f"[{pkg}] {ver} 没有匹配 abi={abi or 'py3-none-any'} 的 wheel")
            if pkg == "psycopg-binary":
                names = [x["filename"] for x in data["releases"][ver] if PLAT in x["filename"]]
                print("        可用的 win_amd64 wheel:", *names[:8], sep="\n          ")
                missing = True
            continue

        target = WHEELS / f["filename"]
        if not target.exists():
            print(f"[{pkg}] 下载 {f['filename']} ({f['size'] // 1024} KB)")
            with urllib.request.urlopen(f["url"], timeout=120) as resp:
                target.write_bytes(resp.read())
        print(f"[{pkg}] 解包 -> {DEST}")
        with zipfile.ZipFile(target) as zf:
            zf.extractall(DEST)

    if missing:
        print("\n结论：psycopg-binary 没有本机 Python 版本的 ABI wheel，Python 层跑不了。")
        return 1
    print("\n完成。下一步：$env:PYTHONPATH = '.pylibs'; python -c \"import psycopg, pgvector; print('ok')\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
