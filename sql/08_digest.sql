-- =============================================================================
-- 日/周/月/年 关键词提炼（rollup digest）
--
-- 解决的问题：情景记忆是"一条条的"，但人回忆时是按**时段**回忆的 ——
-- "上周主要在忙什么""上个月有哪些事"。逐条召回答不了这种问题，
-- 而且 raw_turn 30 天就过期，过了一个月连原料都没了。
--
-- 所以这里做一层**时段关键词摘要**：把某个时间桶里的记忆压成
-- 「关键词 + 代表事项」，落成一条可召回的 summary 记忆。
--
-- 三个关键判断（都不是随手定的）：
--
-- 1. **按 event_time 分桶，不按 ingested_at。**
--    "去年十月发生了什么"问的是事情何时发生，不是何时入库。
--    补录一条 2019 年的记忆，它就该出现在 2019 年的年度摘要里。
--
-- 2. **半开区间 [start, end)。**
--    日/周/月/年边界最常见的 bug 就是"月末最后一天算给谁"：
--    闭区间会让相邻两桶都含边界日，年度汇总凭空多算。
--
-- 3. **提炼结果本身不能再进原料。**
--    摘要记忆也是一条 episode，若不排除，年摘要会把日摘要的文本再摘一遍，
--    关键词被自身污染（自我强化的噪声）。判据是 tags 里带保留标签 'sys:digest'（带命名空间，避免和用户自己的 'digest' 标签撞车）。
--
-- 抽词（分词/停用词/IDF）放在 Python 侧（app/digest.py）：
-- SQL 里做不好中文分词，硬做只会得到一堆单字。这里只负责
--   桶边界 → 取原料 → 存结果，三件事都是纯 SQL 擅长的。
-- =============================================================================
\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- 1. 摘要表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_digests (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       TEXT        NOT NULL,
    period        TEXT        NOT NULL CHECK (period IN ('day','week','month','year')),
    bucket_start  DATE        NOT NULL,   -- 含
    bucket_end    DATE        NOT NULL,   -- 不含（半开）
    -- 结构化词项：保留 count 与 weight，便于后续按权重排或做"这个词在本期变多了"
    terms         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    -- 扁平关键词数组：&& 走 GIN，可以"找提到 pgvector 的那几周"
    keywords      TEXT[]      NOT NULL DEFAULT '{}',
    episode_count INT         NOT NULL DEFAULT 0,
    summary       TEXT        NOT NULL DEFAULT '',
    content       TEXT        NOT NULL DEFAULT '',
    -- 对应的可召回记忆行（app/digest.py 写入；删除摘要记忆时置空不级联）
    digest_episode_id UUID    REFERENCES episodes(id) ON DELETE SET NULL,
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT digest_bucket_order CHECK (bucket_end > bucket_start)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_digest_bucket
    ON memory_digests (user_id, period, bucket_start);
CREATE INDEX IF NOT EXISTS idx_digest_keywords
    ON memory_digests USING gin (keywords);
CREATE INDEX IF NOT EXISTS idx_digest_recent
    ON memory_digests (user_id, period, bucket_start DESC);

COMMENT ON COLUMN memory_digests.terms IS
    'JSON [{term,count,weight}]；weight = count × idf(跨桶)，即"这一期特别突出"的词';

-- ---------------------------------------------------------------------------
-- 2. 桶边界
-- ---------------------------------------------------------------------------
-- DATE_TRUNC 依赖会话时区（'周'在 ISO 里从周一开始），所以是 STABLE 不是 IMMUTABLE
CREATE OR REPLACE FUNCTION digest_bucket(p_ts TIMESTAMPTZ, p_period TEXT)
RETURNS DATE LANGUAGE sql STABLE AS $fn$
    SELECT date_trunc(p_period, p_ts)::date
$fn$;

CREATE OR REPLACE FUNCTION digest_bucket_end(p_start DATE, p_period TEXT)
RETURNS DATE LANGUAGE sql IMMUTABLE AS $fn$
    SELECT (p_start + CASE p_period
        WHEN 'day'   THEN interval '1 day'
        WHEN 'week'  THEN interval '1 week'
        WHEN 'month' THEN interval '1 month'
        WHEN 'year'  THEN interval '1 year'
        ELSE NULL
    END)::date
$fn$;

-- 枚举区间内的桶（含空桶：空桶也要能被"重建"覆盖，否则删不干净旧结果）
CREATE OR REPLACE FUNCTION digest_buckets(
    p_user_id TEXT,
    p_period  TEXT,
    p_from    DATE,
    p_to      DATE
) RETURNS TABLE (bucket_start date, bucket_end date, episode_count bigint)
LANGUAGE sql STABLE AS $fn$
    WITH step AS (
        SELECT CASE p_period
            WHEN 'day'   THEN interval '1 day'
            WHEN 'week'  THEN interval '1 week'
            WHEN 'month' THEN interval '1 month'
            WHEN 'year'  THEN interval '1 year'
        END AS s
    ),
    starts AS (
        SELECT g AS bucket_start
        FROM step, generate_series(
            date_trunc(p_period, p_from::timestamp),
            (p_to - interval '1 day')::timestamp,
            step.s
        ) g
    )
    SELECT s.bucket_start::date,
           digest_bucket_end(s.bucket_start::date, p_period) AS bucket_end,
           (SELECT count(*)
              FROM episodes e
             WHERE e.user_id = p_user_id
               AND e.valid_until IS NULL
               AND e.review_state <> 'pending'
               AND (e.expires_at IS NULL OR e.expires_at > now())
               AND NOT (e.tags && ARRAY['sys:digest'])          -- 摘要不进原料（见文件头第 3 点）
               AND digest_bucket(e.event_time, p_period) = s.bucket_start::date) AS episode_count
    FROM starts s
    WHERE s.bucket_start::date < p_to
    ORDER BY 1
$fn$;

-- 取一个桶的原料（正文 + 摘要 + 重要性），给 Python 抽词用
CREATE OR REPLACE FUNCTION digest_sources(
    p_user_id TEXT,
    p_period  TEXT,
    p_bucket  DATE
) RETURNS TABLE (id uuid, event_time timestamptz, content text, summary text, importance real)
LANGUAGE sql STABLE AS $fn$
    SELECT e.id, e.event_time, e.content, e.summary, e.importance
    FROM episodes e
    WHERE e.user_id = p_user_id
      AND e.valid_until IS NULL
      AND e.review_state <> 'pending'
      AND (e.expires_at IS NULL OR e.expires_at > now())
      AND NOT (e.tags && ARRAY['sys:digest'])
      AND digest_bucket(e.event_time, p_period) = p_bucket
    ORDER BY e.event_time
$fn$;

-- ---------------------------------------------------------------------------
-- 3. 写入（幂等：同一 user/period/bucket 只有一条）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION digest_upsert(
    p_user_id TEXT,
    p_period  TEXT,
    p_bucket_start DATE,
    p_bucket_end   DATE,
    p_terms   JSONB,
    p_keywords TEXT[],
    p_episode_count INT,
    p_summary TEXT,
    p_content TEXT,
    p_digest_episode_id UUID DEFAULT NULL
) RETURNS UUID LANGUAGE plpgsql AS $fn$
DECLARE
    v_id UUID;
BEGIN
    INSERT INTO memory_digests (
        user_id, period, bucket_start, bucket_end, terms, keywords,
        episode_count, summary, content, digest_episode_id
    ) VALUES (
        p_user_id, p_period, p_bucket_start, p_bucket_end,
        COALESCE(p_terms, '[]'::jsonb), COALESCE(p_keywords, '{}'),
        COALESCE(p_episode_count, 0), COALESCE(p_summary, ''),
        COALESCE(p_content, ''), p_digest_episode_id
    )
    ON CONFLICT (user_id, period, bucket_start) DO UPDATE
        SET bucket_end   = EXCLUDED.bucket_end,
            terms        = EXCLUDED.terms,
            keywords     = EXCLUDED.keywords,
            episode_count= EXCLUDED.episode_count,
            summary      = EXCLUDED.summary,
            content      = EXCLUDED.content,
            -- 新摘要记忆存在才更新指针；传 NULL 表示"沿用旧的"
            digest_episode_id = COALESCE(EXCLUDED.digest_episode_id,
                                         memory_digests.digest_episode_id),
            generated_at = now()
    RETURNING id INTO v_id;

    RETURN v_id;
END $fn$;

-- 最近若干期的摘要（CLI show 用）
-- ⚠️ 改返回列必须显式 DROP：CREATE OR REPLACE 只替换参数表相同**且返回类型相同**的函数，
-- 返回 TABLE(...) 多一列就是换返回类型，直接报 "请先使用 DROP FUNCTION"。
DROP FUNCTION IF EXISTS digest_recent(text, text, integer);
CREATE OR REPLACE FUNCTION digest_recent(
    p_user_id TEXT,
    p_period  TEXT DEFAULT NULL,
    p_limit   INT  DEFAULT 10
) RETURNS TABLE (
    period text, bucket_start date, bucket_end date, episode_count int,
    keywords text[], terms jsonb, summary text, digest_episode_id uuid, generated_at timestamptz
)
LANGUAGE sql STABLE AS $fn$
    SELECT d.period, d.bucket_start, d.bucket_end, d.episode_count,
           d.keywords, d.terms, d.summary, d.digest_episode_id, d.generated_at
    FROM memory_digests d
    WHERE d.user_id = p_user_id
      AND (p_period IS NULL OR d.period = p_period)
    ORDER BY d.bucket_start DESC, d.period
    LIMIT p_limit
$fn$;

-- 统计：每期多少条、覆盖到哪
CREATE OR REPLACE FUNCTION digest_stats(p_user_id TEXT)
RETURNS TABLE (period text, buckets bigint, episodes bigint,
               first_bucket date, last_bucket date, keywords bigint)
LANGUAGE sql STABLE AS $fn$
    SELECT d.period, count(*), sum(d.episode_count)::bigint,
           min(d.bucket_start), max(d.bucket_start),
           sum(cardinality(d.keywords))::bigint
    FROM memory_digests d
    WHERE d.user_id = p_user_id
    GROUP BY d.period
    ORDER BY CASE d.period WHEN 'day' THEN 1 WHEN 'week' THEN 2
                           WHEN 'month' THEN 3 ELSE 4 END
$fn$;

DO $$
BEGIN
    RAISE NOTICE 'digest ok：memory_digests / digest_bucket / digest_buckets / digest_sources / digest_upsert / digest_recent / digest_stats';
END $$;
