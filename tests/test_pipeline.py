"""不连数据库的端到端测试：抽取 → 候选 → 四态建议 → 确定性裁决 → 预算装配。

用 HashEmbedder + HeuristicLLM 两个替身，所以 `python tests/test_pipeline.py`
在任何机器上都能跑。它验证的不是"效果好不好"，而是"管线接得上、退化路径不炸"。
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import FactsParams, Policy, RecallParams, plan_recall  # noqa: E402
from app.decide import Candidate, arbitrate  # noqa: E402
from app.embedding import HashEmbedder, to_pgvector_literal  # noqa: E402
from app.llm import HeuristicLLM, loads_strict, parse_facts  # noqa: E402
from app.retrieve import Hit, Rendered, estimate_tokens, render_facts, render_memories  # noqa: E402
from datetime import datetime, timezone  # noqa: E402


def cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 1.0 - dot / (na * nb)


class TestHashEmbedder(unittest.TestCase):
    def setUp(self) -> None:
        self.emb = HashEmbedder(dim=768)

    def test_deterministic(self):
        self.assertEqual(
            self.emb.embed(["用户时区为 Asia/Shanghai"]),
            self.emb.embed(["用户时区为 Asia/Shanghai"]),
        )

    def test_dim_and_unit_norm(self):
        v = self.emb.embed(["hello 世界"])[0]
        self.assertEqual(len(v), 768)
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in v)), 1.0, places=6)

    def test_empty_text_is_valid_vector(self):
        v = self.emb.embed([""])[0]
        self.assertEqual(len(v), 768)
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in v)), 1.0, places=6)

    def test_related_closer_than_unrelated(self):
        q = self.emb.embed(["我的时区是 Asia/Shanghai"])[0]
        near = self.emb.embed(["我的时区改成了 Asia/Shanghai"])[0]
        far = self.emb.embed(["我上周在杭州参加了产品评审会"])[0]
        self.assertLess(cosine_distance(q, near), cosine_distance(q, far))

    def test_pgvector_literal_is_parseable(self):
        self.assertEqual(to_pgvector_literal([0.5, -0.25, 0.0]), "[0.5,-0.25,0]")


class TestParseFacts(unittest.TestCase):
    def test_bad_rows_dropped(self):
        payload = {
            "items": [
                {"content": "用户时区为 Asia/Shanghai", "confidence": 0.9},
                {"content": "", "confidence": 0.9},
                {"summary": "没有 content"},
                "不是一个对象",
                {"content": "  x  "},
            ]
        }
        facts = parse_facts(payload, user_id="u_1", source="conversation")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].content, "用户时区为 Asia/Shanghai")

    def test_legacy_facts_key_still_accepted(self):
        facts = parse_facts(
            {"facts": [{"content": "用户偏好深色主题"}]}, user_id="u_1", source="tool"
        )
        self.assertEqual(len(facts), 1)

    def test_missing_event_time_is_inferred_not_guessed(self):
        facts = parse_facts(
            {"items": [{"content": "用户偏好深色主题"}]}, user_id="u_1", source="tool"
        )
        self.assertEqual(facts[0].event_time_source, "inferred")

    def test_event_time_parsed_when_given(self):
        facts = parse_facts(
            {"items": [{"content": "用户 3 月 1 日入职", "event_time": "2025-03-01T10:00:00+08:00"}]},
            user_id="u_1",
            source="conversation",
        )
        self.assertEqual(facts[0].event_time_source, "extracted")
        self.assertEqual(facts[0].event_time.year, 2025)

    def test_confidence_clamped(self):
        facts = parse_facts(
            {"items": [{"content": "用户偏好深色主题", "confidence": 4.2}]},
            user_id="u_1",
            source="tool",
        )
        self.assertEqual(facts[0].confidence, 1.0)

    def test_invalid_kind_falls_back_to_episode(self):
        facts = parse_facts(
            {"items": [{"content": "用户偏好深色主题", "kind": "forever"}]},
            user_id="u_1",
            source="tool",
        )
        self.assertEqual(facts[0].kind, "episode")

    def test_longterm_without_fact_key_downgrades(self):
        """脏 key 绝不能污染 facts 表的唯一性不变量 —— 宁可降级成情景事件。"""
        facts = parse_facts(
            {"items": [{"content": "用户时区为上海", "kind": "longterm", "value": "Asia/Shanghai"}]},
            user_id="u_1",
            source="tool",
        )
        self.assertEqual(facts[0].kind, "episode")
        self.assertIsNone(facts[0].fact_key)

    def test_longterm_with_bad_fact_key_downgrades(self):
        facts = parse_facts(
            {"items": [{"content": "用户时区为上海", "kind": "longterm", "fact_key": "Time Zone!"}]},
            user_id="u_1",
            source="tool",
        )
        self.assertEqual(facts[0].kind, "episode")

    def test_longterm_with_valid_key_kept(self):
        facts = parse_facts(
            {"items": [
                {"content": "用户时区为上海", "kind": "longterm",
                 "fact_key": "timezone", "value": "Asia/Shanghai"}
            ]},
            user_id="u_1",
            source="tool",
        )
        self.assertEqual(facts[0].kind, "longterm")
        self.assertEqual(facts[0].fact_key, "timezone")
        self.assertEqual(facts[0].value, "Asia/Shanghai")

    def test_missing_items_raises(self):
        with self.assertRaises(ValueError):
            parse_facts({"oops": True}, user_id="u_1", source="tool")


class TestLoadsStrict(unittest.TestCase):
    def test_markdown_fence_stripped(self):
        self.assertEqual(loads_strict('```json\n{"action": "accept"}\n```'), {"action": "accept"})

    def test_json_after_preamble(self):
        self.assertEqual(loads_strict('好的：\n{"action": "drop"}'), {"action": "drop"})

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            loads_strict("我不知道")


class TestOfflinePipeline(unittest.TestCase):
    """抽取 → 相似候选 → 建议 → 裁决，全链路（不打数据库）。"""

    def setUp(self) -> None:
        self.emb = HashEmbedder(dim=768)
        self.llm = HeuristicLLM()
        self.policy = Policy()

    def _candidates(self, existing: list[tuple[str, str]], text: str) -> list[Candidate]:
        """把给定文本当作"库里的旧记忆"构造候选集。

        这里不做距离过滤：替身的哈希向量只用来排序，真实系统里由 SQL 的
        余弦阈值筛，测试要验证的是"给定候选集，裁决对不对"。
        """
        vec = self.emb.embed([text])[0]
        rows = []
        for cid, content in existing:
            dist = cosine_distance(vec, self.emb.embed([content])[0])
            rows.append(
                Candidate(id=cid, content=content, confidence=0.8, source="conversation", distance=dist)
            )
        return sorted(rows, key=lambda c: c.distance or 1.0)

    def test_restatement_merges(self):
        text = "我比较喜欢在杭州开会。"
        facts = self.llm.extract(user_id="u_1", text=text, source="conversation")
        self.assertTrue(facts)
        cands = self._candidates([("old-id", "我比较喜欢在杭州开会。")], facts[0].content)
        self.assertTrue(cands)
        decision = arbitrate(self.llm.judge(fact=facts[0], candidates=cands), facts[0], cands, self.policy)
        self.assertEqual(decision.action, "merge")
        self.assertEqual(decision.target_id, "old-id")

    def test_contradiction_defers(self):
        text = "我不喜欢在杭州开会。"
        facts = self.llm.extract(user_id="u_1", text=text, source="conversation")
        self.assertTrue(facts)
        cands = self._candidates([("old-id", "我比较喜欢在杭州开会，上周的产品评审会就在那边。")],
                                 facts[0].content)
        proposal = self.llm.judge(fact=facts[0], candidates=cands)
        decision = arbitrate(proposal, facts[0], cands, self.policy)
        self.assertIn(decision.action, {"defer", "accept"})
        if decision.action == "defer":
            self.assertEqual(decision.target_id, "old-id")
            self.assertTrue(decision.needs_review)

    def test_longterm_routing(self):
        facts = self.llm.extract(user_id="u_1", text="我的时区是 Asia/Tokyo。", source="conversation")
        self.assertTrue(facts)
        self.assertEqual(facts[0].kind, "longterm")
        self.assertEqual(facts[0].fact_key, "timezone")

    def test_chitchat_extracts_nothing(self):
        self.assertEqual(
            self.llm.extract(user_id="u_1", text="你好呀，今天天气不错，谢谢！", source="conversation"),
            [],
        )

    def test_pipeline_is_stable_across_runs(self):
        text = "我的时区是 Asia/Shanghai。"
        first = [(f.content, f.confidence, f.kind) for f in
                 self.llm.extract(user_id="u_1", text=text, source="conversation")]
        second = [(f.content, f.confidence, f.kind) for f in
                  self.llm.extract(user_id="u_1", text=text, source="conversation")]
        self.assertEqual(first, second)


class TestPlanRecall(unittest.TestCase):
    def test_simple_query_small_limit(self):
        p = plan_recall("时区")
        self.assertLessEqual(p.limit, 2)
        self.assertLessEqual(p.token_budget, 400)

    def test_complex_query_larger_limit(self):
        p = plan_recall("上次我们讨论的那个部署方案为什么失败了")
        self.assertGreaterEqual(p.limit, 5)
        self.assertGreaterEqual(p.token_budget, 1200)

    def test_ef_search_dominates_limit(self):
        p = RecallParams(limit=10, vec_k=50)
        self.assertGreaterEqual(p.resolved_ef_search(), p.vec_k)

    def test_default_decay_config(self):
        """高斯衰减的参考配置：180 天内不衰减，offset+scale 处半衰。"""
        p = RecallParams()
        self.assertEqual(p.decay_offset_days, 180)
        self.assertEqual(p.decay_scale_days, 1825)
        self.assertEqual(p.decay, 0.5)
        self.assertEqual(p.freq_weight, 0.2)

    def test_facts_decay_uses_its_own_window(self):
        """长期事实的免罚期/尺度与情景记忆不同（按 last_used_at 衰减）。"""
        f = FactsParams()
        self.assertNotEqual(f.decay_offset_days, RecallParams().decay_offset_days)
        self.assertNotEqual(f.decay_scale_days, RecallParams().decay_scale_days)


class TestBudget(unittest.TestCase):
    def _hit(self, i: int, text: str, score: float) -> Hit:
        return Hit(
            id=f"id{i}",
            summary=text,
            snippet=text,
            event_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source="conversation",
            confidence=0.8,
            score=score,
            rank_vec=i,
            rank_kw=None,
            expires_at=None,
        )

    def test_estimate_tokens_cjk(self):
        self.assertEqual(estimate_tokens("时区上海"), 4)
        self.assertEqual(estimate_tokens(""), 0)
        self.assertGreater(estimate_tokens("a" * 40), 0)

    def test_empty_hits(self):
        r = render_memories([], 100)
        self.assertEqual(r.text, "")
        self.assertEqual(r.used_ids, ())

    def test_budget_truncates_tail(self):
        hits = [self._hit(i, "记忆内容" * 20, 1.0 - i * 0.1) for i in range(10)]
        r = render_memories(hits, budget=60)
        self.assertLess(len(r.used_ids), len(hits))
        self.assertGreaterEqual(len(r.used_ids), 1)
        self.assertGreater(r.dropped, 0)
        # 保留的一定是按分数排序靠前的那些
        self.assertEqual(r.used_ids[0], "id0")

    def test_first_hit_never_dropped_entirely(self):
        """第一条就超预算时截断文本硬塞，总比什么都不给强。"""
        hits = [self._hit(0, "很长的记忆" * 200, 1.0)]
        r = render_memories(hits, budget=40)
        self.assertEqual(len(r.used_ids), 1)
        self.assertIn("…", r.text)

    def test_facts_render_respects_budget(self):
        facts = [{"key": f"k{i}", "value": "v" * 20} for i in range(10)]
        r = render_facts(facts, budget=40)
        self.assertLess(len(r.used_ids), len(facts))
        self.assertGreaterEqual(len(r.used_ids), 1)


class TestHeuristicDistiller(unittest.TestCase):
    """抽取式提炼的**不变量**：不编造、可复现、有上限、会去重。

    这些性质比"摘要写得好不好"更值得测 —— 自动提炼跑在后台，
    一旦开始编造或不可复现，污染的是整条记忆链。
    """

    def setUp(self) -> None:
        from app.distill import HeuristicDistiller

        self.d = HeuristicDistiller()

    def _turn(self, i, content, age=10.0):
        from app.distill import RawTurn
        from datetime import datetime, timedelta, timezone

        return RawTurn(
            id=f"id{i}", content=content, summary=None,
            event_time=datetime.now(timezone.utc) - timedelta(days=age),
            age_days=age,
        )

    def test_empty_input(self):
        body, summary = self.d.summarize([])
        self.assertEqual(body, "")
        self.assertEqual(summary, "")

    def test_extractive_never_invents(self):
        turns = [
            self._turn(0, "用户要求发布改走 GitHub Actions。"),
            self._turn(1, "staging 端口是 55433，生产 5432。"),
        ]
        body, _ = self.d.summarize(turns)
        joined = " ".join(t.content for t in turns)
        bullet_texts = [ln[2:] for ln in body.splitlines() if ln.startswith("- ")]
        self.assertTrue(bullet_texts)
        for b in bullet_texts:
            self.assertIn(b[:15], joined, f"要点不是原文片段：{b}")

    def test_dedup_identical_sentences(self):
        turns = [
            self._turn(1, "端口是 55433。"),
            self._turn(2, "端口是 55433。"),
        ]
        body, _ = self.d.summarize(turns)
        self.assertEqual(body.count("55433"), 1, body)

    def test_respects_char_limit(self):
        turns = [self._turn(i, f"第{i}条记录：端口 {5000 + i}，环境 staging。") for i in range(20)]
        body, _ = self.d.summarize(turns, limit_chars=80)
        self.assertLessEqual(len(body), 80 + 8, f"len={len(body)}")

    def test_deterministic(self):
        turns = [self._turn(i, f"第{i}条：决定改成方案 {i}，因为原来的失败。") for i in range(5)]
        self.assertEqual(self.d.summarize(turns), self.d.summarize(turns))

    def test_prefers_concrete_over_fluff(self):
        turns = [
            self._turn(0, "好的。"),
            self._turn(1, "发布脚本要删掉 scp 到 10.0.0.5 那行，改走 Actions。"),
        ]
        body, _ = self.d.summarize(turns, limit_chars=60)
        self.assertIn("scp", body)
        self.assertNotIn("好的", body)

    def test_summary_nonempty_and_short(self):
        turns = [self._turn(0, "用户决定把评审流程固定在每周五。")]
        _body, summary = self.d.summarize(turns)
        self.assertTrue(summary)
        self.assertLessEqual(len(summary), 120)

    def test_agent_backend_refuses_auto_summarize(self):
        from app.distill import AgentDistiller

        with self.assertRaises(NotImplementedError):
            AgentDistiller().summarize([self._turn(0, "x y z")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
