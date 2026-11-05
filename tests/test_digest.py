"""关键词提炼的单测。零依赖：python tests/test_digest.py

这里测的是**抽词行为**，不是"能不能跑"：

  · 不发明词：每个关键词都必须真的在原文里出现过（记忆系统里最忌讳"看起来像关键词的幻觉"）
  · 子串抑制：pgvector 不能被拆成 pgvecto/gvector 一起霸榜
  · 停用词：的/了/是/在 这类词在任何时段都高频，作为关键词等于没信息
  · tf-idf：同样词频下，出现在所有文档里的词权重必须低于只出现在少数文档里的词
  · 时段标签：ISO 周必须从周一起算，跨年周（2026-12-28）的年份归属要正确
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.digest import (  # noqa: E402
    build_df,
    bucket_label,
    default_range,
    extract_keywords,
    tokenize,
)


class TestTokenize(unittest.TestCase):
    def test_latin_words(self) -> None:
        toks = tokenize("用 pgvector 和 PostgreSQL 18.6 做检索")
        self.assertIn("pgvector", toks)
        self.assertIn("postgresql", toks)     # 全小写

    def test_latin_stopwords_dropped(self) -> None:
        toks = tokenize("the and of for with")
        self.assertEqual([t for t in toks if t in {"the", "and", "of"}], [])

    def test_cjk_bigrams_present(self) -> None:
        toks = tokenize("记忆库")
        self.assertIn("记忆", toks)
        self.assertIn("忆库", toks)

    def test_cjk_ngram_bounded_by_function_char(self) -> None:
        # "库的" 首字是实词、尾字是虚词 → 属于跨词边界碎片，应被丢掉
        self.assertNotIn("库的", tokenize("记忆库的设计"))

    def test_pure_digits_dropped(self) -> None:
        self.assertEqual([t for t in tokenize("12345 678") if t.isdigit()], [])

    def test_empty(self) -> None:
        self.assertEqual(tokenize(""), [])
        self.assertEqual(tokenize(None or ""), [])

    def test_every_cjk_token_is_substring(self) -> None:
        """不发明词：抽出的每个中文词项都必须是原文的子串。"""
        src = "本机 PostgreSQL 的 timezone 设为 Asia/Shanghai，psycopg3 不认这个时区名"
        for tok in tokenize(src):
            if tok.isascii():
                self.assertIn(tok, src.lower())
            else:
                self.assertIn(tok, src)


class TestSubstringSuppression(unittest.TestCase):
    def test_longer_wins(self) -> None:
        docs = ["pgvector pgvector pgvector", "pgvector 安装", "pgvector 编译"]
        terms = [t.term for t in extract_keywords(docs, top_n=20, min_count=1)]
        self.assertIn("pgvector", terms)
        # 被 pgvector 包含的碎片不该同时出现
        self.assertNotIn("gvector", terms)
        self.assertNotIn("pgvecto", terms)

    def test_frequency_wins(self) -> None:
        # 高频长词吃掉自己：'记忆库' 出现 5 次，'记忆' 被它包含 → 只留 '记忆库'
        docs = ["记忆库"] * 5 + ["记忆库设计"]
        terms = [t.term for t in extract_keywords(docs, top_n=20, min_count=1)]
        self.assertIn("记忆库", terms)
        self.assertNotIn("记忆", terms, "被更高频长词包含的碎片应被抑制")


class TestExtractKeywords(unittest.TestCase):
    def test_top_n_limit(self) -> None:
        docs = [f"关键词{i}号 内容{i}" for i in range(20)]
        self.assertLessEqual(len(extract_keywords(docs, top_n=5, min_count=1)), 5)

    def test_min_count_filters_singletons(self) -> None:
        docs = ["数据库连接池", "数据库索引", "缓存命中率", "无关的一句话"]
        terms = {t.term for t in extract_keywords(docs, top_n=20, min_count=2)}
        self.assertIn("数据库", terms)        # 出现 2 次；且抑制了碎片 '数据'
        self.assertNotIn("缓存", terms)       # 只出现 1 次

    def test_min_count_relaxed_for_tiny_bucket(self) -> None:
        # 只有 1 条记忆时不该把关键词全过滤掉
        terms = extract_keywords(["唯一的一条记忆"], top_n=10, min_count=2)
        self.assertTrue(terms)

    def test_idf_scales_weight_by_document_frequency(self) -> None:
        """tf-idf 的机制：同一份文档，有无 idf 的权重比必须等于 idf 本身。"""
        import math

        docs = ["记忆 pgvector", "记忆 别的内容甲", "记忆 别的内容乙", "记忆 别的内容丙"]
        df = build_df(docs)
        plain = {t.term: t for t in extract_keywords(docs, top_n=30, min_count=1)}
        weighted = {
            t.term: t
            for t in extract_keywords(docs, top_n=30, min_count=1,
                                      corpus_df=df, corpus_size=len(docs))
        }
        for term, t in weighted.items():
            idf = math.log(1.0 + len(docs) / (1.0 + df.get(term, 1)))
            self.assertAlmostEqual(t.weight, plain[term].weight * idf, places=6,
                                   msg=f"{term} 的权重不是 count×idf")
        # 高频词（出现在 4/4 篇）的 idf 必须低于罕见词（1/4 篇）
        self.assertLess(
            math.log(1 + 4 / (1 + df["记忆"])),
            math.log(1 + 4 / (1 + df["pgvector"])),
        )

    def test_no_corpus_df_equals_raw_count(self) -> None:
        docs = ["甲甲 乙乙", "甲甲 丙丙"]
        terms = {t.term: t for t in extract_keywords(docs, top_n=20, min_count=1)}
        # 无 idf 时 weight == count
        for t in terms.values():
            self.assertAlmostEqual(t.weight, float(t.count))

    def test_empty_docs(self) -> None:
        self.assertEqual(extract_keywords([], top_n=10), [])
        self.assertEqual(extract_keywords([""], top_n=10), [])


class TestBucketLabel(unittest.TestCase):
    def test_day(self) -> None:
        self.assertEqual(bucket_label("day", date(2026, 10, 25)), "2026-10-25")

    def test_week_is_iso(self) -> None:
        # 2026-10-19 是周一 → ISO W43
        self.assertEqual(bucket_label("week", date(2026, 10, 19)), "2026-W43")

    def test_cross_year_week(self) -> None:
        # ISO 周跨年：2021-01-01 是周五，归属 2020-W53（不是 2021-W01）
        self.assertEqual(bucket_label("week", date(2021, 1, 1)), "2020-W53")
        # 2026 有 53 个 ISO 周：2026-12-28（周一）的周四是 12-31，仍属 2026
        self.assertEqual(bucket_label("week", date(2026, 12, 28)), "2026-W53")

    def test_month_year(self) -> None:
        self.assertEqual(bucket_label("month", date(2026, 10, 1)), "2026-10")
        self.assertEqual(bucket_label("year", date(2026, 1, 1)), "2026")


class TestDefaultRange(unittest.TestCase):
    def test_ranges_are_ordered_and_cover_today(self) -> None:
        today = date(2026, 10, 25)
        for period in ("day", "week", "month", "year"):
            start, end = default_range(period, today)
            self.assertLess(start, end, period)
            self.assertLessEqual(start, today, period)
            self.assertGreater(end, today, period)

    def test_year_range_is_wider_than_month(self) -> None:
        today = date(2026, 10, 25)
        m = default_range("month", today)
        y = default_range("year", today)
        self.assertGreater((y[1] - y[0]).days, (m[1] - m[0]).days)


if __name__ == "__main__":
    unittest.main(verbosity=2)
