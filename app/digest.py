"""日/周/月/年 关键词提炼（rollup digest）。

人和"逐条记忆"的检索方式不同：人经常按**时段**回忆 ——
"上周主要在忙什么""上个月有哪些事"。逐条向量召回答不了这种问题，
而且 `raw_turn` 30 天就过期，过一个月连原料都没了。

所以这一层把某个时间桶里的记忆压成「关键词 + 代表事项」，
落成一条**可召回的 summary 记忆**（kind='summary'，TTL 3650 天）。

三条取向：

1. **不发明词。** 关键词全部来自原文的 n-gram，代表事项由
   `HeuristicDistiller` 抽取原句 —— 和提炼层一个原则：宁愿读起来糙，
   也不要出现"原文没有的概念"。
2. **按 event_time 分桶**（见 sql/08_digest.sql）。补录旧记忆会落到它应在的时段。
3. **摘要不进原料。** 年摘要不会再去摘日摘要的文本，否则关键词自我强化。

抽词算法（无分词器可用时的务实做法）：
    中文 n-gram（2~4 字）+ 拉丁词 → 去停用词 → 子串抑制 → tf-idf 排序
子串抑制是关键：不抑制时 "pgvector / pgvecto / gvector" 会一起冒出来霸榜，
抑制规则是"被更高频的更长 n-gram 包含则丢弃"，这样留下的总是更完整的那一个。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Iterable, Sequence

from .distill import HeuristicDistiller, RawTurn

if TYPE_CHECKING:  # 只为类型，避免循环 import
    from .store import Store

PERIODS = ("day", "week", "month", "year")
# 保留标签带命名空间：普通记忆也可能被用户/Agent 打上 "digest" 这类标签，
# 一旦撞车就会被误当成摘要而排出原料 —— 记忆静默消失，且不报错。
DIGEST_TAG = "sys:digest"

# 单字/双字的虚词、代词、连词：它们在任何时段的文档里都高频，作为关键词毫无信息量。
_STOP = set(
    "的 了 是 在 和 与 把 被 也 都 就 不 很 有 我 你 他 她 它 们 个 这 那 上 下 中 对 为 以 及 或 而 但 如果 "
    "因为 所以 然后 现在 可以 需要 应该 已经 还是 就是 没有 一个 一些 这个 那个 我们 你们 他们 "
    "什么 怎么 如何 为什么 时候 东西 事情 问题 部分 情况 地方 方法 方式 时候 "
    "了 着 过 地 得 又 再 才 只 更 最 太 很 挺 会 能 要 想 让 给 从 到 于 就 还 又 并 且 "
    "the a an and or but if then of to in on at for with from by is are was were be been "
    "this that these those it its as not no yes you we they he she i".split()
)

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9_+#.-]{1,30}")
_DIGITS = re.compile(r"^\d+$")


# ---------------------------------------------------------------------------
# 抽词
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Term:
    term: str
    count: int
    weight: float

    def as_dict(self) -> dict:
        return {"term": self.term, "count": self.count, "weight": round(self.weight, 4)}


def tokenize(text: str) -> list[str]:
    """把一段文本切成候选词项。中文按 2~4 字 n-gram，英文按词。"""
    if not text:
        return []
    out: list[str] = []

    for w in _LATIN.findall(text):
        lw = w.lower().strip(".-")
        if len(lw) >= 2 and lw not in _STOP and not _DIGITS.match(lw):
            out.append(lw)

    for run in _CJK_RUN.findall(text):
        for n in (2, 3, 4):
            for i in range(len(run) - n + 1):
                gram = run[i : i + n]
                if gram in _STOP:
                    continue
                # 首尾是虚词的 n-gram 基本是跨词边界切出来的碎片
                if gram[0] in _STOP or gram[-1] in _STOP:
                    continue
                out.append(gram)
    return out


def _suppress_substrings(counts: dict[str, int]) -> dict[str, int]:
    """子串抑制：被更高频（或同频但更长）的词包含时丢弃自己。

    不做的后果：搜 "pgvector" 会同时留下 pgvecto / gvector / pgvect / ector…
    全部进关键词列表，看起来像乱码，人读不了。
    """
    terms = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    kept: list[tuple[str, int]] = []
    for term, cnt in terms:
        if any(term != k and term in k for k, _ in kept):
            continue
        kept.append((term, cnt))
    return dict(kept)


def extract_keywords(
    docs: Sequence[str],
    top_n: int = 12,
    *,
    min_count: int = 2,
    corpus_df: dict[str, int] | None = None,
    corpus_size: int | None = None,
) -> list[Term]:
    """从一个桶的文档里抽关键词。

    `corpus_df` / `corpus_size`：跨桶的文档频率。传了就按 tf-idf 加权
    （"这一期特别突出"），不传就纯词频（单桶孤立使用时的退化行为）。
    """
    counts: dict[str, int] = {}
    for doc in docs:
        for tok in tokenize(doc):
            counts[tok] = counts.get(tok, 0) + 1

    counts = _suppress_substrings(counts)

    # 只出现一次的词多半是偶然，除非桶里只有一两条记忆
    if len(docs) >= 3:
        counts = {k: v for k, v in counts.items() if v >= min_count}

    out: list[Term] = []
    for term, cnt in counts.items():
        if corpus_df is not None and corpus_size:
            df = corpus_df.get(term, 1)
            # 平滑 idf，避免 df=0 时爆炸；对数保证高频不会无限膨胀
            idf = math.log(1.0 + corpus_size / (1.0 + df))
        else:
            idf = 1.0
        out.append(Term(term=term, count=cnt, weight=cnt * idf))

    out.sort(key=lambda t: (-t.weight, -len(t.term), t.term))
    return out[:top_n]


def build_df(docs: Sequence[str]) -> dict[str, int]:
    """文档频率：每个词项出现在多少条文档里（tf-idf 的分母）。"""
    df: dict[str, int] = {}
    for doc in docs:
        for tok in set(tokenize(doc)):
            df[tok] = df.get(tok, 0) + 1
    return df


# ---------------------------------------------------------------------------
# 时段标签
# ---------------------------------------------------------------------------
def bucket_label(period: str, bucket_start: date) -> str:
    if period == "day":
        return bucket_start.isoformat()
    if period == "week":
        iso = bucket_start.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if period == "month":
        return f"{bucket_start:%Y-%m}"
    return f"{bucket_start:%Y}"


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------
@dataclass
class DigestResult:
    period: str
    bucket_start: date
    bucket_end: date
    label: str
    episode_count: int
    keywords: tuple[str, ...]
    digest_id: str | None
    digest_episode_id: str | None
    content: str
    skipped_reason: str | None = None


UPDATE_DIGEST_EPISODE = """
UPDATE episodes
   SET content = %(content)s,
       summary = %(summary)s,
       embedding = %(vec)s::vector,
       embedding_model = %(model)s,
       tags = %(tags)s::text[]
 WHERE id = %(id)s
RETURNING id
"""


class DigestBuilder:
    """把时间桶压成关键词摘要。幂等：同一 (user, period, bucket) 反复构建结果一致。"""

    def __init__(self, store: "Store") -> None:
        self.store = store

    # -- 读 ------------------------------------------------------------------
    def buckets(self, user_id: str, period: str, start: date, end: date) -> list[dict]:
        return self.store._read(
            "SELECT * FROM digest_buckets(%s::text, %s::text, %s::date, %s::date)",
            (user_id, period, start, end),
        )

    def sources(self, user_id: str, period: str, bucket: date) -> list[dict]:
        return self.store._read(
            "SELECT * FROM digest_sources(%s::text, %s::text, %s::date)",
            (user_id, period, bucket),
        )

    def recent(self, user_id: str, period: str | None = None, limit: int = 10) -> list[dict]:
        return self.store._read(
            "SELECT * FROM digest_recent(%s::text, %s::text, %s::int)",
            (user_id, period, limit),
        )

    def stats(self, user_id: str) -> list[dict]:
        return self.store._read("SELECT * FROM digest_stats(%s::text)", (user_id,))

    # -- 写 ------------------------------------------------------------------
    def _render(self, label: str, start: date, end: date, n: int,
                terms: Sequence[Term], sources: Sequence[dict]) -> tuple[str, str]:
        """摘要正文只用原文片段，不生成任何新表述。"""
        kws = "、".join(t.term for t in terms[:8]) or "（无显著关键词）"
        span = f"{start}~{end - timedelta(days=1)}"
        head = f"{label}（{span}，{n} 条记忆）关键词：{kws}"

        # 代表事项：复用提炼层的抽取式摘要，保证每句都能在原文里找到
        turns = [
            RawTurn(
                id=str(s["id"]),
                content=s["content"] or "",
                summary=s["summary"],
                event_time=s["event_time"],
                age_days=0.0,
            )
            for s in sources
        ]
        body, _ = HeuristicDistiller().summarize(turns, limit_chars=320) if turns else ("", "")
        content = head if not body else f"{head}\n代表事项：\n{body}"
        return content, head

    def build_bucket(
        self,
        user_id: str,
        period: str,
        bucket_start: date,
        bucket_end: date,
        *,
        top_n: int = 12,
        corpus_df: dict[str, int] | None = None,
        corpus_size: int | None = None,
        min_count: int = 2,
    ) -> DigestResult:
        label = bucket_label(period, bucket_start)
        sources = self.sources(user_id, period, bucket_start)
        docs = [(s["content"] or "") for s in sources]

        if not sources:
            # 空桶：只有已经建过摘要的才刷新成空，避免给空时段留垃圾行
            existing = self.store._read(
                """SELECT id FROM memory_digests
                    WHERE user_id = %s AND period = %s AND bucket_start = %s""",
                (user_id, period, bucket_start),
            )
            if not existing:
                return DigestResult(period, bucket_start, bucket_end, label, 0, (), None, None,
                                    "", skipped_reason="empty_bucket")
            self.store._write(
                "SELECT digest_upsert(%s,%s,%s,%s,%s::jsonb,%s::text[],%s,%s,%s,%s::uuid)",
                (user_id, period, bucket_start, bucket_end, "[]", [], 0, "", "", None),
            )
            return DigestResult(period, bucket_start, bucket_end, label, 0, (), None, None,
                                "", skipped_reason="emptied")

        terms = extract_keywords(
            docs, top_n, min_count=min_count,
            corpus_df=corpus_df, corpus_size=corpus_size,
        )
        content, summary = self._render(label, bucket_start, bucket_end, len(sources), terms, sources)
        keywords = [t.term for t in terms]
        terms_json = json.dumps([t.as_dict() for t in terms], ensure_ascii=False)

        # 1) 先落/更新"可召回的摘要记忆"，拿到 id
        ep_id = self._upsert_digest_episode(user_id, period, bucket_start, content, summary)

        # 2) 再落结构化摘要行（指针指向那条记忆）
        row = self._write_digest(
            user_id, period, bucket_start, bucket_end, terms_json, keywords,
            len(sources), summary, content, ep_id,
        )
        return DigestResult(
            period, bucket_start, bucket_end, label, len(sources),
            tuple(keywords), str(row) if row else None, ep_id, content,
        )

    def _upsert_digest_episode(
        self, user_id: str, period: str, bucket_start: date, content: str, summary: str
    ) -> str | None:
        """摘要记忆本身也是一条 episode（kind='summary'），所以能被正常召回。"""
        source_ref = f"digest:{period}:{bucket_start.isoformat()}"
        existing = self.store._read(
            """SELECT id FROM episodes
                WHERE user_id = %s AND source_ref = %s AND valid_until IS NULL LIMIT 1""",
            (user_id, source_ref),
        )
        vec = self.store.embedder.embed([content])[0]
        if existing:
            self.store._write(
                UPDATE_DIGEST_EPISODE,
                {
                    "content": content,
                    "summary": summary[:200],
                    "vec": list(vec),
                    "model": self.store.embedder.model,
                    "tags": [DIGEST_TAG, period, bucket_label(period, bucket_start)],
                    "id": str(existing[0]["id"]),
                },
            )
            return str(existing[0]["id"])

        from .decide import Decision, Fact
        from .distill import SUMMARY_KIND

        fact = Fact(
            user_id=user_id,
            content=content,
            confidence=0.9,
            source="tool",
            event_time=_bucket_midpoint(bucket_start),
            kind=SUMMARY_KIND,
            event_time_source="inferred",
            summary=summary[:200],
            importance=0.6,
            tags=(DIGEST_TAG, period, bucket_label(period, bucket_start)),
        )
        res = self.store.apply(
            Decision(action="accept", content=content, reason="digest"),
            fact, vec, source_ref=source_ref,
        )
        return str(res["id"]) if res.get("applied") else None

    def _write_digest(
        self, user_id: str, period: str, bucket_start: date, bucket_end: date,
        terms_json: str, keywords: Sequence[str], n: int,
        summary: str, content: str, ep_id: str | None,
    ):
        row = self.store._read(
            "SELECT digest_upsert(%s::text,%s::text,%s::date,%s::date,%s::jsonb,"
            "%s::text[],%s::int,%s::text,%s::text,%s::uuid) AS id",
            (user_id, period, bucket_start, bucket_end, terms_json, list(keywords),
             n, summary, content, ep_id),
        )
        return row[0]["id"] if row else None

    # -- 批量 ----------------------------------------------------------------
    def build_range(
        self,
        user_id: str,
        period: str,
        start: date,
        end: date,
        *,
        top_n: int = 12,
        min_count: int = 2,
        include_empty: bool = False,
    ) -> list[DigestResult]:
        """构建 [start, end) 内所有桶。

        tf-idf 的语料是**整段区间**的所有文档，这样同一批桶之间的
        "谁更突出"才有可比性（单桶各算各的 idf 会得到互不可比的权重）。
        """
        if period not in PERIODS:
            raise ValueError(f"period 必须是 {PERIODS} 之一，收到 {period!r}")

        spans = self.buckets(user_id, period, start, end)
        # 先把所有原料拉一次，供全局 df 计算
        docs_by_bucket: dict[date, list[str]] = {}
        all_docs: list[str] = []
        for b in spans:
            srcs = self.sources(user_id, period, b["bucket_start"])
            docs = [(s["content"] or "") for s in srcs]
            docs_by_bucket[b["bucket_start"]] = docs
            all_docs.extend(docs)

        df = build_df(all_docs)
        n_docs = len(all_docs)

        results: list[DigestResult] = []
        for b in spans:
            if not docs_by_bucket[b["bucket_start"]] and not include_empty:
                results.append(DigestResult(
                    period, b["bucket_start"], b["bucket_end"],
                    bucket_label(period, b["bucket_start"]), 0, (), None, None,
                    "", skipped_reason="empty_bucket",
                ))
                continue
            results.append(self.build_bucket(
                user_id, period, b["bucket_start"], b["bucket_end"],
                top_n=top_n, corpus_df=df, corpus_size=n_docs, min_count=min_count,
            ))
        return results


def _bucket_midpoint(bucket_start: date):
    """摘要记忆的 event_time 取桶中点。

    为什么不是桶起点：日摘要落在当天 00:00 会让它看起来比当天的记忆都"老"，
    在高斯衰减里被轻微惩罚；取中点更接近"这一段时间"的直觉。
    """
    from datetime import datetime, time, timezone

    return datetime.combine(bucket_start, time(12, 0), tzinfo=timezone.utc)


def default_range(period: str, today: date | None = None) -> tuple[date, date]:
    """CLI 默认区间：够看到"最近几期"就行。"""
    today = today or date.today()
    if period == "day":
        return today - timedelta(days=13), today + timedelta(days=1)
    if period == "week":
        return today - timedelta(days=7 * 7), today + timedelta(days=1)
    if period == "month":
        return date(today.year - 1, today.month, 1), today + timedelta(days=1)
    return date(today.year - 5, 1, 1), today + timedelta(days=1)


__all__ = [
    "PERIODS", "DIGEST_TAG", "Term", "DigestResult", "DigestBuilder",
    "tokenize", "extract_keywords", "build_df", "bucket_label", "default_range",
]
