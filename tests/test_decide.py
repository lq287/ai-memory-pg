"""裁决逻辑的单测。零依赖：python tests/test_decide.py

这里测的是**护栏**，不是效果。四态（accept/merge/defer/drop）每一条都对应一次
线上事故的形状：
  - 模型编 id / 编动作        → 幻觉落库
  - 弱证据抬高强记忆          → 记忆越用越错
  - 矛盾被直接删掉            → 历史丢失，无法回溯
  - 长期事实门槛失守          → 脏配置污染所有后续任务
  - 重试导致重复写入          → 同一件事记了五遍
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Policy  # noqa: E402
from app.decide import (  # noqa: E402
    Candidate,
    Fact,
    Proposal,
    arbitrate,
    normalize,
    parse_proposal,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def fact(
    content="用户时区为 Asia/Shanghai", confidence=0.9, source="conversation", kind="episode"
) -> Fact:
    return Fact(
        user_id="u_1",
        content=content,
        confidence=confidence,
        source=source,
        event_time=NOW,
        kind=kind,
    )


def cand(
    id="11111111-1111-1111-1111-111111111111",
    content="用户时区为 Asia/Tokyo",
    confidence=0.8,
    source="conversation",
) -> Candidate:
    return Candidate(id=id, content=content, confidence=confidence, source=source)


class TestParseProposal(unittest.TestCase):
    def test_unknown_action_becomes_drop(self):
        self.assertEqual(parse_proposal({"action": "MERGE_ALL"}).action, "drop")

    def test_case_folded(self):
        self.assertEqual(parse_proposal({"action": "MERGE"}).action, "merge")

    def test_non_dict(self):
        self.assertEqual(parse_proposal(["accept"]).action, "drop")

    def test_empty_memory_id_is_none(self):
        self.assertIsNone(parse_proposal({"action": "merge", "memory_id": ""}).memory_id)

    def test_legacy_vocabulary_is_mapped(self):
        """换了提示词之后旧词汇不该让整条管线静默瘫痪。"""
        self.assertEqual(parse_proposal({"action": "ADD"}).action, "accept")
        self.assertEqual(parse_proposal({"action": "UPDATE"}).action, "accept")
        self.assertEqual(parse_proposal({"action": "NONE"}).action, "drop")
        self.assertEqual(parse_proposal({"action": "DELETE"}).action, "defer")

    def test_state_alias(self):
        self.assertEqual(parse_proposal({"state": "defer"}).action, "defer")


class TestArbitrate(unittest.TestCase):
    def test_invalid_action_fails_closed(self):
        d = arbitrate(Proposal(action="upsert"), fact(), [cand()])
        self.assertEqual(d.action, "drop")
        self.assertEqual(d.reason, "invalid_action")
        self.assertTrue(d.needs_review)

    def test_model_drop_passthrough(self):
        d = arbitrate(Proposal(action="drop"), fact(), [cand()])
        self.assertEqual(d.action, "drop")
        self.assertFalse(d.needs_review)

    def test_accept_with_no_candidates(self):
        d = arbitrate(Proposal(action="accept"), fact(), [])
        self.assertEqual(d.action, "accept")

    def test_accept_when_genuinely_new(self):
        d = arbitrate(
            Proposal(action="accept"),
            fact("用户偏好深色主题"),
            [cand(content="用户时区为 Asia/Tokyo")],
        )
        self.assertEqual(d.action, "accept")

    def test_accept_deduped_to_merge(self):
        """内容逐字相同就不新建 —— 顺手把队列重试的幂等也做了。"""
        same = "用户时区为 Asia/Shanghai"
        d = arbitrate(
            Proposal(action="accept"),
            fact("用户时区为   Asia/Shanghai。"),
            [cand(id="abc", content=same)],
        )
        self.assertEqual(d.action, "merge")
        self.assertEqual(d.reason, "accept_deduped_to_merge")
        self.assertEqual(d.target_id, "abc")
        self.assertGreater(d.confidence_delta, 0)

    def test_valid_merge(self):
        d = arbitrate(
            Proposal(action="merge", memory_id="abc"),
            fact("用户时区为 Asia/Shanghai"),
            [cand(id="abc", content="用户时区为 Asia/Tokyo")],
        )
        self.assertEqual(d.action, "merge")
        self.assertEqual(d.target_id, "abc")
        self.assertAlmostEqual(d.confidence_delta, Policy().merge_confidence_boost)

    def test_restated_merge_gets_smaller_boost(self):
        """逐字复述的信息量最低，增量减半，避免反复复述把置信度顶满。"""
        d = arbitrate(
            Proposal(action="merge", memory_id="abc"),
            fact("用户时区为Asia/Tokyo"),
            [cand(id="abc", content="用户时区为 Asia/Tokyo")],
        )
        self.assertEqual(d.action, "merge")
        self.assertAlmostEqual(d.confidence_delta, Policy().merge_confidence_boost * 0.5)

    def test_hallucinated_memory_id_falls_back_to_accept(self):
        """模型编一个不存在的 id —— 幻觉最常见的形状。内容仍然有价值，不丢。"""
        d = arbitrate(
            Proposal(action="merge", memory_id="99999999-9999-9999-9999-999999999999"),
            fact("用户偏好深色主题"),
            [cand()],
        )
        self.assertEqual(d.action, "accept")
        self.assertEqual(d.reason, "unknown_target")
        self.assertTrue(d.needs_review)
        self.assertTrue(d.downgraded)

    def test_merge_without_memory_id(self):
        d = arbitrate(Proposal(action="merge", memory_id=None), fact(), [cand()])
        self.assertEqual(d.action, "accept")
        self.assertEqual(d.reason, "unknown_target")

    def test_human_source_is_protected(self):
        """人工写入的记忆不接受模型的自动改动 —— merge 和 defer 都不行。"""
        for action in ("merge", "defer"):
            with self.subTest(action=action):
                d = arbitrate(
                    Proposal(action=action, memory_id="abc"),
                    fact(confidence=0.9),
                    [cand(id="abc", confidence=0.3, source="human")],
                )
                self.assertEqual(d.action, "drop")
                self.assertEqual(d.reason, "protected_source")
                self.assertTrue(d.needs_review)

    def test_merge_confidence_gap_blocks_reinforcement(self):
        """0.95 的旧记忆不该被 0.60 的推断"强化"到更高。"""
        d = arbitrate(
            Proposal(action="merge", memory_id="abc"),
            fact(confidence=0.60),
            [cand(id="abc", confidence=0.95)],
        )
        self.assertEqual(d.action, "drop")
        self.assertEqual(d.reason, "merge_confidence_gap")

    def test_valid_defer(self):
        d = arbitrate(
            Proposal(action="defer", memory_id="abc"),
            fact("用户时区为 Asia/Shanghai", confidence=0.9),
            [cand(id="abc", content="用户时区为 Asia/Tokyo", confidence=0.8)],
        )
        self.assertEqual(d.action, "defer")
        self.assertEqual(d.target_id, "abc")
        self.assertTrue(d.needs_review)

    def test_defer_with_identical_content_becomes_merge(self):
        """内容一样就没有矛盾可挂起。"""
        d = arbitrate(
            Proposal(action="defer", memory_id="abc"),
            fact("用户时区为Asia/Tokyo"),
            [cand(id="abc", content="用户时区为 Asia/Tokyo")],
        )
        self.assertEqual(d.action, "merge")
        self.assertEqual(d.reason, "defer_no_conflict")

    def test_defer_hallucinated_target(self):
        d = arbitrate(
            Proposal(action="defer", memory_id="deadbeef-not-a-real-id"),
            fact("用户时区为 Asia/Shanghai"),
            [cand()],
        )
        self.assertEqual(d.action, "accept")
        self.assertEqual(d.reason, "unknown_target")

    def test_longterm_threshold_is_strict(self):
        """长期事实门槛最严：低置信度候选不得进入长期层。"""
        d = arbitrate(
            Proposal(action="accept"),
            fact("用户时区为 Asia/Shanghai", confidence=0.5, kind="longterm"),
            [],
        )
        self.assertEqual(d.action, "drop")
        self.assertIn("below_write_threshold(longterm)", d.reason)

    def test_longterm_passes_above_threshold(self):
        d = arbitrate(
            Proposal(action="accept"),
            fact("用户时区为 Asia/Shanghai", confidence=0.85, kind="longterm"),
            [],
        )
        self.assertEqual(d.action, "accept")

    def test_episode_threshold_is_loose(self):
        """情景事件可以宽松：0.4 的推断也值得留一条。"""
        d = arbitrate(
            Proposal(action="accept"),
            fact("上周在杭州开了评审会", confidence=0.40, kind="episode"),
            [],
        )
        self.assertEqual(d.action, "accept")

    def test_raw_turn_threshold_loosest(self):
        d = arbitrate(
            Proposal(action="accept"),
            fact("用户问了一次部署流程", confidence=0.25, kind="raw_turn"),
            [],
        )
        self.assertEqual(d.action, "accept")

    def test_llm_rewrite_not_trusted_by_default(self):
        """默认落库用抽取原文，不落模型的自由改写。"""
        original = "用户时区为 Asia/Shanghai"
        d = arbitrate(
            Proposal(action="merge", memory_id="abc", new_content="用户的时区曾经是上海"),
            fact(original),
            [cand(id="abc", content="用户时区为 Asia/Tokyo")],
        )
        self.assertEqual(d.content, original)


class TestNormalize(unittest.TestCase):
    def test_punctuation_and_space_folded(self):
        self.assertEqual(
            normalize("用户时区为 Asia/Shanghai。"), normalize("用户时区为asia/shanghai")
        )

    def test_fullwidth_folded(self):
        self.assertEqual(normalize("ＡＢＣ"), normalize("abc"))

    def test_distinct_content_stays_distinct(self):
        self.assertNotEqual(normalize("时区东京"), normalize("时区上海"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
