"""长驻 sidecar：给 DSH 插件用的 JSON-lines 服务（stdin/stdout）。

为什么不是"插件每次 spawn 一次 `python scripts/mem.py`"：
  · 一次进程启动 ≈ 950ms（Python 90ms + psycopg 180ms + ONNX 模型 450ms + SCRAM 握手）
  · 插件是 **host 进程内**运行的（不受沙箱限制），可以养一个常驻子进程：
    启动付一次钱，之后每次调用只是 10~50ms 的准备 + 一次 SQL/推理。
  · 更重要的：只有一处实现。四态裁决、向量化、分桶逻辑都在 app/ 里，
    插件不重写它们（这个项目已经因为"两处各自实现"翻过两次车）。

协议（每行一个 JSON，UTF-8）：
  请求  {"id": <任意>, "op": "<操作>", "args": {...}}
  成功  {"id": ..., "ok": true,  "result": {...}}
  失败  {"id": ..., "ok": false, "error": {"type": "...", "message": "...", "hint": "..."}}

约定：
  · **stdout 只写协议行**，日志一律走 stderr —— 否则插件端解析会炸。
  · 单个请求失败不退出（把错误回给调用方），只有 stdin 关闭或致命初始化失败才退出。
  · 不写 .pyc（见下面 dont_write_bytecode）：这样从受限沙箱里也不产生任何磁盘写入。

跑法（插件会自动这么做）：
  cd <project>; python -m app.sidecar
"""

from __future__ import annotations

import json
import os
import sys
import traceback

# 与 scripts/mem.py 同一条约定：零磁盘写入。必须在 import app.* 之前。
sys.dont_write_bytecode = True

from .config import Settings  # noqa: E402
from .embedding import LazyEmbedder, align_to_database  # noqa: E402
from .facade import Memory  # noqa: E402
from .store import Store  # noqa: E402

DEFAULT_DSN = "postgresql://mem:mem@127.0.0.1:5432/mem"


def _log(msg: str) -> None:
    print(f"[memory-sidecar] {msg}", file=sys.stderr, flush=True)


def _build_memory(user_id: str) -> Memory:
    settings = Settings.from_env()
    if not os.environ.get("MEM_PG_DSN"):
        os.environ["MEM_PG_DSN"] = DEFAULT_DSN
    dsn = os.environ["MEM_PG_DSN"]
    store = Store(dsn, LazyEmbedder(settings)).connect()
    resolved = align_to_database(settings, conn=store.conn)
    if resolved is not settings:
        store.embedder = LazyEmbedder(resolved)
    return Memory(store, user_id)


# ---------------------------------------------------------------------------
# 操作表：名字 → (处理函数, 是否需要参数)
# ---------------------------------------------------------------------------
def _ops(mem: Memory) -> dict:
    return {
        "ping": lambda a: {
            "ok": True,
            "user": mem.user,
            "embedder": mem.store.embedder.model,
            "pid": os.getpid(),
        },
        "health": lambda a: mem.health(),
        "context": lambda a: mem.context(
            max_chars=int(a.get("max_chars", 1200)),
            fact_limit=int(a.get("fact_limit", 12)),
            with_tasks=bool(a.get("with_tasks", True)),
            with_conflicts=bool(a.get("with_conflicts", False)),
        ),
        "recall": lambda a: mem.recall(
            a["query"],
            limit=int(a.get("limit", 5)),
            tags=a.get("tags"),
            include_full=bool(a.get("full", False)),
            use_graph=bool(a.get("graph", True)),
            mark_used=bool(a.get("mark_used", False)),
            min_score=float(a.get("min_score", 0.0)),
        ),
        "add": lambda a: mem.add(
            a["text"],
            kind=a.get("kind", "episode"),
            confidence=float(a.get("confidence", 0.9)),
            importance=float(a.get("importance", 0.6)),
            source=a.get("source", "conversation"),
            summary=a.get("summary"),
            tags=a.get("tags"),
            entities=a.get("entities"),
            action=a.get("action", "accept"),
            target=a.get("target"),
        ),
        "fact_get": lambda a: mem.fact_get(a.get("keys"), limit=int(a.get("limit", 50))),
        "fact_set": lambda a: mem.fact_set(
            a["key"], a.get("value"),
            confidence=float(a.get("confidence", 0.9)),
            importance=float(a.get("importance", 0.6)),
            source=a.get("source", "human"),
        ),
        "digest_build": lambda a: mem.digest_build(
            a.get("period", "week"),
            from_date=a.get("from"),
            to_date=a.get("to"),
            top=int(a.get("top", 12)),
            min_count=int(a.get("min_count", 2)),
        ),
        "digest_show": lambda a: mem.digest_show(a.get("period"), limit=int(a.get("limit", 10))),
        "graph_route": lambda a: mem.graph_route(
            a["query"], depth=int(a.get("depth", 2)), limit=int(a.get("limit", 30))
        ),
        "list": lambda a: mem.list_recent(limit=int(a.get("limit", 20))),
        "forget": lambda a: mem.forget(a["id"], a.get("reason")),
        "restore": lambda a: mem.restore(a["id"]),
    }


def _reply(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    user_id = os.environ.get("MEM_USER_ID", "owner")
    try:
        mem = _build_memory(user_id)
    except Exception as exc:
        _log(f"初始化失败: {type(exc).__name__}: {exc}")
        _reply(
            {
                "id": 0,
                "ok": False,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "hint": "检查 MEM_PG_DSN 与 PostgreSQL 是否在跑；python -m app.sidecar 需要先 pip 装 .pylibs",
                },
            }
        )
        return 1

    ops = _ops(mem)
    _log(f"ready pid={os.getpid()} user={user_id} dsn={os.environ.get('MEM_PG_DSN')}")

    for raw in sys.stdin:
        # 防御 BOM / NUL：某些客户端（PowerShell 管道、Windows 编辑器）会在首行带 BOM，
        # 直接 json.loads 会抛 "Unexpected UTF-8 BOM"，让插件端看起来像随机故障。
        line = raw.replace("\ufeff", "").replace("\x00", "").strip()
        if not line:
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            op = req.get("op")
            args = req.get("args") or {}
            if op == "shutdown":
                _reply({"id": req_id, "ok": True, "result": {"bye": True}})
                break
            fn = ops.get(op)
            if fn is None:
                raise KeyError(f"未知操作 {op!r}；可用: {', '.join(sorted(ops))}")
            _reply({"id": req_id, "ok": True, "result": fn(args)})
        except Exception as exc:  # 单请求失败不能拖垮整个进程
            tb = traceback.format_exc(limit=3)
            _log(f"请求失败: {type(exc).__name__}: {exc}")
            _reply(
                {
                    "id": req_id,
                    "ok": False,
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "trace": tb if os.environ.get("MEM_SIDECAR_DEBUG") else None,
                    },
                }
            )

    _log("stdin closed, exiting")
    try:
        mem.store.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
