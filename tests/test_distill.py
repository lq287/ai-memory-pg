"""提炼后端的单测。零依赖、零网络：python tests/test_distill.py

这里测的核心是**LLM 后端的接地校验**：记忆系统里最危险的幻觉不是"说得含糊"，
而是"编出一个精确值"（端口、版本、阈值、路径）—— 它会被当成事实反复召回，
而且事后无法与真实的精确值区分。所以 `_grounded()` 宁可错杀：

  · 摘要里出现的每个数字都必须在原文里出现过；
  · 校验不过就**重试**，重试还不过就**抛错**（进死信），绝不静默落一条可疑摘要。

顺带覆盖后端选择：heuristic 能跑、agent 必须拒绝、llm 没端点必须报错。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.distill import (  # noqa: E402
    DISTILL_SYSTEM,
    AgentDistiller,
    HeuristicDistiller,
    LLMDistiller,
    RawTurn,
    _grounded,
)

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def turns(*texts: str) -> list[RawTurn]:
    return [
        RawTurn(
            id=f"id-{i}",
            content=t,
            summary=None,
            event_time=NOW - timedelta(days=10 - i),
            age_days=float(10 - i),
        )
        for i, t in enumerate(texts)
    ]


class FakeLLM:
    """假 LLM：按脚本返回，记录调用次数与最后一次提示词。"""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.last_system = ""
        self.last_user = ""

    def chat_text(self, system: str, user: str) -> str:
        self.calls += 1
        self.last_system = system
        self.last_user = user
        idx = min(self.calls - 1, len(self.replies) - 1)
        return self.replies[idx]


class TestGrounding(unittest.TestCase):
    """接地校验：数字必须在原文里出现。"""

    def test_numbers_present(self) -> None:
        self.assertTrue(_grounded("端口是 5432", "数据库跑在 5432 端口"))

    def test_invented_number_rejected(self) -> None:
        # 原文说 5432，摘要编了个 5433 —— 最典型的危险幻觉
        self.assertFalse(_grounded("端口是 5433", "数据库跑在 5432 端口"))

    def test_no_numbers_is_grounded(self) -> None:
        self.assertTrue(_grounded("把配置改到新位置", "把配置改到新位置"))

    def test_decimal_token(self) -> None:
        self.assertTrue(_grounded("阈值 0.35", "余弦距离阈值 0.35"))
        self.assertFalse(_grounded("阈值 0.45", "余弦距离阈值 0.35"))

    def test_empty_summary(self) -> None:
        self.assertTrue(_grounded("", "任何原文"))

    def test_wrong_year_rejected(self) -> None:
        # 日期也是数字：编错版本年份同样拦住
        self.assertFalse(_grounded("升级到 PostgreSQL 19", "升级到 PostgreSQL 18"))


class TestLLMDistiller(unittest.TestCase):
    """用假 LLM 验证：清洗、重试、拒绝落库。"""

    def test_normal_path(self) -> None:
        llm = FakeLLM("- 数据库端口是 5432\n- 记忆一律入库，不写文件")
        d = LLMDistiller(llm)
        body, head = d.summarize(turns("数据库端口是 5432", "记忆一律入库，不写文件"))
        self.assertEqual(llm.calls, 1)
        self.assertIn("- 数据库端口是 5432", body)
        self.assertEqual(body.count("\n"), 1)
        self.assertTrue(head)
        self.assertLessEqual(len(head), 200)

    def test_system_prompt_has_limit_substituted(self) -> None:
        llm = FakeLLM("- 只有一条")
        LLMDistiller(llm).summarize(turns("只有一条"), limit_chars=123)
        # 提示词里的 {limit} 必须被替换掉，否则模型会照抄占位符
        self.assertNotIn("{limit}", llm.last_system)
        self.assertIn("123", llm.last_system)
        self.assertIn("禁止编造", DISTILL_SYSTEM)

    def test_code_fence_stripped(self) -> None:
        llm = FakeLLM("```markdown\n- 端口 5432\n```")
        body, _ = LLMDistiller(llm).summarize(turns("端口 5432"))
        self.assertNotIn("```", body)
        self.assertIn("- 端口 5432", body)

    def test_bullets_normalized(self) -> None:
        # 模型爱用 • * 1. 之类的符号，统一成 "- "
        llm = FakeLLM("• 第一点\n* 第二点\n1. 第三点")
        body, _ = LLMDistiller(llm).summarize(turns("第一点", "第二点", "第三点"))
        for line in body.splitlines():
            self.assertTrue(line.startswith("- "), f"未规范化: {line!r}")
        self.assertEqual(len(body.splitlines()), 3)

    def test_numbered_list_not_mistaken_for_hallucination(self) -> None:
        """序号 "1." 是标记，不是数据：必须剥掉再接地校验，否则整条被误杀。"""
        llm = FakeLLM("1. 端口是 5432\n2. 路径是 D:/mem")
        body, _ = LLMDistiller(llm).summarize(turns("端口是 5432", "路径是 D:/mem"))
        self.assertEqual(llm.calls, 1, "带序号的正常输出不该触发重试")
        self.assertIn("- 端口是 5432", body)
        self.assertNotIn("1.", body)

    def test_content_digits_survive_normalization(self) -> None:
        """以数字开头但不是序号的内容不能被吃掉。"""
        llm = FakeLLM("5432 是当前端口\n2026 年改成新方案")
        body, _ = LLMDistiller(llm).summarize(turns("5432 是当前端口", "2026 年改成新方案"))
        self.assertIn("5432 是当前端口", body)
        self.assertIn("2026 年改成新方案", body)

    def test_hallucination_retried_then_succeeds(self) -> None:
        # 第一次编了原文没有的数字 → 重试；第二次接地 → 采用
        llm = FakeLLM("- 端口是 9999", "- 端口是 5432")
        body, _ = LLMDistiller(llm).summarize(turns("端口是 5432"))
        self.assertEqual(llm.calls, 2)
        self.assertIn("5432", body)
        self.assertNotIn("9999", body)

    def test_hallucination_exhausted_raises(self) -> None:
        # 一直编 → 抛错（队列重试/进死信），绝不静默落库
        llm = FakeLLM("- 端口是 9999")
        with self.assertRaises(RuntimeError):
            LLMDistiller(llm, max_retries=1).summarize(turns("端口是 5432"))
        self.assertEqual(llm.calls, 2)   # 1 次 + 1 次重试

    def test_empty_reply_retried(self) -> None:
        llm = FakeLLM("   ", "- 有内容的一条")
        body, _ = LLMDistiller(llm).summarize(turns("有内容的一条"))
        self.assertEqual(llm.calls, 2)
        self.assertIn("有内容", body)

    def test_empty_turns(self) -> None:
        llm = FakeLLM("- 不该被调用")
        body, head = LLMDistiller(llm).summarize([])
        self.assertEqual((body, head), ("", ""))
        self.assertEqual(llm.calls, 0)   # 空输入不该浪费一次请求

    def test_too_long_truncated(self) -> None:
        long_line = "- " + "很长的一条" * 400
        llm = FakeLLM(long_line)
        body, _ = LLMDistiller(llm).summarize(turns("很长的一条"), limit_chars=100)
        self.assertLessEqual(len(body), 200)   # limit_chars * 2

    def test_sources_carry_dates(self) -> None:
        llm = FakeLLM("- 一条")
        LLMDistiller(llm).summarize(turns("甲", "乙"))
        # 原文送进去时带日期，模型才能判断"多久以前"
        self.assertIn("2026-02-19", llm.last_user)
        self.assertIn("甲", llm.last_user)


class TestBackendSelection(unittest.TestCase):
    """后端必须能被明确选择，也必须在不可用时明确失败。"""

    def test_heuristic_still_works(self) -> None:
        body, head = HeuristicDistiller().summarize(
            turns("数据库端口是 5432，以后不要再用 55432。", "好的")
        )
        self.assertTrue(body.startswith("- "))
        self.assertIn("5432", body)
        self.assertTrue(head)

    def test_heuristic_never_invents(self) -> None:
        # 抽取式：输出的每一句都必须能在原文里找到
        src = "端口改成 5432，因为 55432 被占用了。另外记得重跑迁移脚本。"
        body, _ = HeuristicDistiller().summarize(turns(src))
        for line in body.splitlines():
            self.assertIn(line[2:].strip(), src)

    def test_agent_refuses_to_summarize(self) -> None:
        with self.assertRaises(NotImplementedError):
            AgentDistiller().summarize(turns("随便"))

    def test_llm_without_endpoint_fails_loudly(self) -> None:
        # distill_run 在没配端点时必须报错，而不是静默退化成 heuristic
        import os

        from app.distill import distill_run

        old = os.environ.pop("MEM_LLM_BASE_URL", None)
        try:
            with self.assertRaises(ValueError) as ctx:
                distill_run(None, "u_1", backend="llm")   # type: ignore[arg-type]
            self.assertIn("MEM_LLM_BASE_URL", str(ctx.exception))
        finally:
            if old is not None:
                os.environ["MEM_LLM_BASE_URL"] = old

    def test_unknown_backend_rejected(self) -> None:
        from app.distill import distill_run

        with self.assertRaises(ValueError):
            distill_run(None, "u_1", backend="magic")   # type: ignore[arg-type]


class TestLimits(unittest.TestCase):
    """长度上限与空输入。"""

    def test_heuristic_respects_limit(self) -> None:
        ts = turns(*[f"第 {i} 条：这是一句比较长的中文记录，用来测试上限。" for i in range(30)])
        body, _ = HeuristicDistiller().summarize(ts, limit_chars=100)
        self.assertLessEqual(len(body), 130)   # 允许一条的溢出余量

    def test_heuristic_empty(self) -> None:
        self.assertEqual(HeuristicDistiller().summarize([]), ("", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
