"""工作记忆层 + 任务状态层（Python 侧）。

这两层与"记忆检索"的区别必须守住：
  · 任务状态：按 id 直查，不搜索。
  · 工作记忆：为当前任务**装配**一段上下文 —— 装配是显式的、有序的、可复现的，
    不是"拿个 query 去召回一批"。

三条实现约定：
  1. **pin 时快照**：条目内容是拷贝，不是指向 episodes 的活引用。底层记忆之后
     被取代/过期/软删除，本任务读到的上下文不会跟着变（否则任务不可复现）。
  2. **顺序显式**：position 由调用方决定，默认追加。工作记忆从不重排。
  3. **预算硬截断**：按 position 顺序累加 token，超预算即停；第一条永远保留
     （哪怕它本身超预算，也截断文本塞进去）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Sequence

if TYPE_CHECKING:  # 只用于类型标注，避免循环导入
    from .store import Store

log = logging.getLogger(__name__)

KINDS = ("pinned_memory", "fact", "note", "task_state", "tool_output")
TERMINAL = ("done", "failed", "cancelled")


def estimate_tokens(text: str) -> int:
    """与 retrieve.estimate_tokens 同一口径（CJK 1 字 ≈ 1 token，其余 4 字符 ≈ 1）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u3400" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + (other + 3) // 4


@dataclass(frozen=True, slots=True)
class Assembly:
    """装配结果。used 记下了真正进了预算的条目，方便回写/审计。"""

    text: str
    used_items: tuple[str, ...]
    dropped: int
    tokens: int


class WorkMemory:
    """绑定到一个 Store（共用同一条连接与事务工具）。"""

    def __init__(self, store: "Store") -> None:
        self.store = store

    # -- 任务状态 -----------------------------------------------------------
    def start_task(
        self,
        user_id: str,
        kind: str,
        title: str,
        metadata: dict | None = None,
        retention_hours: float | None = None,
    ) -> dict:
        row = self.store._write(
            # 两个坑叠在一起，都踩过：
            # (1) 必须 `SELECT * FROM f(...)`，不能 `SELECT (f(...)).*` ——
            #     后者会按输出列**重复求值**（tasks 有 16 列就执行 16 次），
            #     表现为"调一次 start_task 却多了 16 个任务、事件重复十几条"。
            # (2) 显式 cast：psycopg 把 Python int 编成 int2、float 编成 float8，
            #     而参数类型是 int4 / real，float8 -> float4 不是隐式转换。
            "SELECT * FROM task_start(%s::text, %s::text, %s::text, %s::jsonb, %s::interval)",
            (
                user_id,
                kind,
                title,
                json.dumps(metadata or {}, ensure_ascii=False),
                f"{retention_hours} hours" if retention_hours else None,
            ),
        )
        log.info("task_start %s kind=%s", str(row["id"])[:8], kind)
        return row

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        step: str | None = None,
        step_index: int | None = None,
        progress: float | None = None,
        result: Any = None,
        error: str | None = None,
    ) -> dict:
        row = self.store._write(
            "SELECT * FROM task_update(%s::uuid, %s::text, %s::text, %s::int, %s::real, "\
            "%s::jsonb, %s::text)",
            (
                task_id,
                status,
                step,
                step_index,
                progress,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                error,
            ),
        )
        return row

    def finish_task(self, task_id: str, status: str = "done", result: Any = None,
                    error: str | None = None) -> dict:
        if status not in TERMINAL:
            raise ValueError(f"finish_task 的 status 必须是 {TERMINAL} 之一")
        # 只有 done 才代表"跑完了"，progress 才推到 1.0。
        # failed / cancelled 保留真实进度 —— 否则看板会把"失败在第 40%"显示成 100%。
        progress = 1.0 if status == "done" else None
        return self.update_task(task_id, status=status, progress=progress,
                                result=result, error=error)

    def get_task(self, task_id: str, events: int = 10) -> tuple[dict, list[dict]] | None:
        row = self.store._read("SELECT * FROM task_summary(%s, %s)", (task_id, events))
        if not row:
            return None
        task = row[0]["task"]
        ev = row[0]["events"]
        return task, (ev if isinstance(ev, list) else json.loads(ev))

    def live_tasks(self, user_id: str, limit: int = 20) -> list[dict]:
        return self.store._read("SELECT * FROM tasks_live(%s, %s)", (user_id, limit))

    def all_tasks(self, user_id: str, limit: int = 20) -> list[dict]:
        return self.store._read(
            "SELECT * FROM tasks WHERE user_id=%s ORDER BY updated_at DESC LIMIT %s",
            (user_id, limit),
        )

    # -- 工作集 -------------------------------------------------------------
    def open_set(
        self,
        user_id: str,
        task_id: str | None = None,
        session_id: str | None = None,
        note: str | None = None,
        token_budget: int = 1500,
        ttl_hours: float | None = None,
    ) -> dict:
        return self.store._write(
            "SELECT * FROM working_open(%s::text, %s::uuid, %s::text, %s::text, "\
            "%s::int, %s::interval)",
            (user_id, task_id, session_id, note, token_budget,
             f"{ttl_hours} hours" if ttl_hours else None),
        )

    def get_set(self, set_id: str) -> dict | None:
        rows = self.store._read("SELECT * FROM working_sets WHERE id=%s", (set_id,))
        return rows[0] if rows else None

    def items(self, set_id: str) -> list[dict]:
        return self.store._read(
            "SELECT * FROM working_items WHERE set_id=%s ORDER BY position, created_at",
            (set_id,),
        )

    def pin(
        self,
        set_id: str,
        kind: str,
        content: str,
        *,
        ref_id: str | None = None,
        tokens: int | None = None,
        position: int | None = None,
        metadata: dict | None = None,
    ) -> dict:
        if kind not in KINDS:
            raise ValueError(f"kind 必须是 {KINDS} 之一，收到 {kind!r}")
        return self.store._write(
            "SELECT * FROM working_pin(%s::uuid, %s::text, %s::text, %s::int, "\
            "%s::uuid, %s::int, %s::jsonb)",
            (
                set_id,
                kind,
                content,
                tokens if tokens is not None else estimate_tokens(content),
                ref_id,
                position,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )

    def pin_note(self, set_id: str, content: str, **kw) -> dict:
        return self.pin(set_id, "note", content, **kw)

    def pin_memory(self, set_id: str, episode_id: str, *, with_content: bool = False) -> dict | None:
        """把一条情景记忆 pin 进工作集（**快照**当前 summary/content）。"""
        rows = self.store._read(
            "SELECT id, summary, content, event_time FROM episodes WHERE id=%s",
            (episode_id,),
        )
        if not rows:
            log.warning("pin_memory: episode %s 不存在", episode_id)
            return None
        ep = rows[0]
        day = ep["event_time"].strftime("%Y-%m-%d") if ep["event_time"] else "日期未知"
        body = ep["content"] if with_content else (ep["summary"] or ep["content"][:200])
        return self.pin(
            set_id, "pinned_memory", f"[{day}] {body}",
            ref_id=str(ep["id"]), metadata={"summary": ep["summary"]},
        )

    def pin_fact(self, set_id: str, user_id: str, key: str) -> dict | None:
        """把一条长期事实 pin 进工作集（按键精确取，快照 value）。"""
        rows = self.store._read(
            "SELECT key, value FROM facts WHERE user_id=%s AND key=%s AND valid_until IS NULL",
            (user_id, key),
        )
        if not rows:
            log.warning("pin_fact: 事实 %s 不存在", key)
            return None
        f = rows[0]
        value = f["value"] if isinstance(f["value"], str) else json.dumps(f["value"], ensure_ascii=False)
        return self.pin(set_id, "fact", f"- {f['key']} = {value}",
                        metadata={"key": f["key"]})

    def pin_task_state(self, set_id: str, task_id: str) -> dict | None:
        """把任务当前状态作为一行上下文 pin 进去（让模型知道"现在进行到哪"）。"""
        task = self.store._read(
            "SELECT kind, title, status, step, step_index, progress FROM tasks WHERE id=%s",
            (task_id,),
        )
        if not task:
            return None
        t = task[0]
        line = (
            f"任务「{t['title']}」({t['kind']}): status={t['status']}"
            f" step={t['step'] or '-'} ({t['step_index']})"
            f" progress={float(t['progress']):.0%}"
        )
        return self.pin(set_id, "task_state", line, ref_id=None,
                        metadata={"task_id": str(task_id)})

    def clear(self, set_id: str) -> int:
        row = self.store._write("SELECT working_clear(%s::uuid) AS n", (set_id,))
        return int(row["n"]) if row else 0

    def expire(self) -> tuple[int, int]:
        row = self.store._write("SELECT * FROM working_expire()")
        return (int(row["sets_deleted"]), int(row["tasks_expired"])) if row else (0, 0)

    # -- 装配（不检索）------------------------------------------------------
    def render(self, set_id: str, *, budget: int | None = None,
               header: str | None = None) -> Assembly:
        """按 position 顺序装配成一段文本，超出 token 预算即截断。

        这是"读工作记忆"的**唯一**入口 —— 它不接收 query，永远返回同样的结果，
        所以同一任务里两次装配必然一致（可复现）。
        """
        ws = self.get_set(set_id)
        if ws is None:
            return Assembly("", (), 0, 0)
        limit = budget or int(ws["token_budget"])
        items = self.items(set_id)
        if not items:
            return Assembly("", (), 0, 0)

        head = header or "# 当前任务上下文"
        lines = [head]
        used_tokens = estimate_tokens(head)
        used: list[str] = []
        dropped = 0

        for it in items:
            line = it["content"]
            cost = int(it["tokens"]) or estimate_tokens(line)
            if used_tokens + cost > limit and used:
                dropped += 1
                continue
            if not used and used_tokens + cost > limit:
                # 第一条就超预算：截断硬塞，总比什么都不给强
                keep = max(16, limit - used_tokens - 8)
                line = line[:keep] + "…"
                cost = estimate_tokens(line)
                dropped += 1
            lines.append(line)
            used.append(str(it["id"]))
            used_tokens += cost

        return Assembly("\n".join(lines), tuple(used), dropped, used_tokens)

    def render_task(self, task_id: str, *, budget: int | None = None) -> Assembly:
        """便捷：直接装配某个任务的工作集。"""
        rows = self.store._read(
            "SELECT id FROM working_sets WHERE task_id=%s", (task_id,)
        )
        if not rows:
            return Assembly("", (), 0, 0)
        return self.render(str(rows[0]["id"]), budget=budget)


__all__ = ["WorkMemory", "Assembly", "estimate_tokens", "KINDS", "TERMINAL"]
