-- =============================================================================
-- distill 提炼层：把原始轮次（raw_turn, 30d TTL）压成派生的 summary（3650d TTL）
--
-- 设计稿原话："原始轮次 30 天 TTL，过期后仅保留提炼结果"。
-- 也就是说，如果不做提炼，30 天后用户那句话就彻底没了 —— 这是"记忆系统悄悄丢东西"
-- 的典型形态：不报错、不告警，只是某天开始查不到。
--
-- 三条语义约定：
--   1. 提炼产物是一等公民：写进 episodes(kind='summary')，有向量、能被召回。
--   2. **血缘必须记**：distill_links 记录"这条摘要来自哪些原始轮次"。
--      于是"当时具体怎么说的"仍然可追，而摘要本身轻量。
--   3. 原始轮次**不因为被提炼就删**：让它按自己的 TTL 自然过期；
--      要提前回收空间才用 close_raw=true（仍然只是闭合区间，不是物理删除）。
-- =============================================================================
\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- 1. 血缘表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS distill_links (
    summary_id UUID        NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    raw_id     UUID        NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (summary_id, raw_id)
);

CREATE INDEX IF NOT EXISTS idx_distill_raw ON distill_links (raw_id);

COMMENT ON TABLE distill_links IS
'提炼血缘：summary 由哪些 raw_turn 压成；raw 侧可反查"我这条被提炼到哪去了"';

-- raw_turn 的扫描索引（提炼按事件时间顺序取最老的批次）
CREATE INDEX IF NOT EXISTS idx_ep_raw_live
    ON episodes (user_id, event_time)
    WHERE kind = 'raw_turn' AND valid_until IS NULL AND review_state <> 'pending';

-- ---------------------------------------------------------------------------
-- 2. 候选：够老、还活着、且没被提炼过的原始轮次
--
-- 为什么要"够老"（默认 7 天）：刚发生的对话上下文还在，立刻压成一句摘要会丢细节；
-- 隔几天再提炼，正好是"哪些值得留"的信息已经稳定的时候。
-- 为什么要排除已被提炼的：靠 distill_links 反查，避免同一批反复压成多条摘要。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION distill_candidates(
    p_user_id       TEXT,
    p_min_age_days  FLOAT8 DEFAULT 7,
    p_limit         INT    DEFAULT 50
) RETURNS TABLE (
    id uuid, content text, summary text, event_time timestamptz,
    ingested_at timestamptz, expires_at timestamptz, age_days float8
)
LANGUAGE sql STABLE AS $fn$
    SELECT e.id, e.content, e.summary, e.event_time, e.ingested_at, e.expires_at,
           GREATEST(EXTRACT(EPOCH FROM (now() - e.event_time))::float8, 0.0) / 86400.0 AS age_days
    FROM episodes e
    WHERE e.user_id = p_user_id
      AND e.kind = 'raw_turn'
      AND e.valid_until IS NULL
      AND e.review_state <> 'pending'
      AND (e.expires_at IS NULL OR e.expires_at > now())   -- 已过期的没意义
      AND EXTRACT(EPOCH FROM (now() - e.event_time)) / 86400.0 >= p_min_age_days
      AND NOT EXISTS (SELECT 1 FROM distill_links l WHERE l.raw_id = e.id)
    ORDER BY e.event_time
    LIMIT p_limit
$fn$;

-- 待提炼统计（运维看板用）
CREATE OR REPLACE FUNCTION distill_pending(
    p_user_id TEXT, p_min_age_days FLOAT8 DEFAULT 7
) RETURNS TABLE (pending bigint, oldest_days float8, expiring_7d bigint)
LANGUAGE sql STABLE AS $fn$
    WITH c AS (
        SELECT * FROM distill_candidates(p_user_id, p_min_age_days, 100000)
    )
    SELECT count(*),
           COALESCE(max(age_days), 0),
           count(*) FILTER (WHERE expires_at IS NOT NULL
                              AND expires_at <= now() + interval '7 days')
    FROM c
$fn$;

-- ---------------------------------------------------------------------------
-- 3. 落血缘
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION distill_link(
    p_summary_id UUID,
    p_raw_ids    UUID[]
) RETURNS INT LANGUAGE plpgsql AS $fn$
DECLARE
    n INT;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM episodes WHERE id = p_summary_id AND kind = 'summary'
    ) THEN
        RAISE EXCEPTION 'distill_link: % 不是 kind=summary 的记忆', p_summary_id;
    END IF;

    IF p_raw_ids IS NULL OR array_length(p_raw_ids, 1) IS NULL THEN
        RAISE EXCEPTION 'distill_link: 原始轮次列表为空';
    END IF;

    INSERT INTO distill_links (summary_id, raw_id)
    SELECT p_summary_id, unnest(p_raw_ids)
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $fn$;

-- 反查：这条原始轮次被提炼到了哪些摘要
CREATE OR REPLACE FUNCTION distill_of_raw(p_raw_id UUID)
RETURNS TABLE (summary_id uuid, content text, created_at timestamptz)
LANGUAGE sql STABLE AS $fn$
    SELECT e.id, e.content, l.created_at
    FROM distill_links l JOIN episodes e ON e.id = l.summary_id
    WHERE l.raw_id = p_raw_id
    ORDER BY l.created_at DESC
$fn$;

-- 某条摘要覆盖了哪些原始轮次
CREATE OR REPLACE FUNCTION distill_sources(p_summary_id UUID)
RETURNS TABLE (raw_id uuid, content text, event_time timestamptz, age_days float8)
LANGUAGE sql STABLE AS $fn$
    SELECT e.id, e.content, e.event_time,
           GREATEST(EXTRACT(EPOCH FROM (now() - e.event_time))::float8, 0.0) / 86400.0
    FROM distill_links l JOIN episodes e ON e.id = l.raw_id
    WHERE l.summary_id = p_summary_id
    ORDER BY e.event_time
$fn$;

-- ---------------------------------------------------------------------------
-- 4. 收尾：可选的"提前闭合原始轮次"
-- 默认**不动** —— 让 raw_turn 按自己的 TTL 过期。
-- 只有确认摘要质量够好、想尽快回收空间时才调用。
-- 注意：闭合只影响召回，行仍在（血缘仍可追）。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION distill_close_raw(p_raw_ids UUID[]) RETURNS INT
LANGUAGE plpgsql AS $fn$
DECLARE
    n INT;
BEGIN
    UPDATE episodes
    SET valid_until = now()
    WHERE id = ANY(p_raw_ids)
      AND kind = 'raw_turn'
      AND valid_until IS NULL;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $fn$;

DO $$
BEGIN
    RAISE NOTICE 'distill ok：distill_links / distill_candidates / distill_link / distill_sources';
END $$;
