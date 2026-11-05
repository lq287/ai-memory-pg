-- =============================================================================
-- 检索层：先召回、再融合、后塑形
--   ① 多路并行召回（向量 + 词法）
--   ② RRF 融合（对分数尺度不敏感，适合异构召回源）
--   ③ 复合打分（高斯时间衰减 + 对数使用频率）
--   ④ 渐进披露（只返回 summary 层，全文按 id 再取）
--   ⑤ 预算封顶（应用层 retrieve.py 按 token 截断，SQL 只负责给分）
--
-- 运行前提：sql/01_schema.sql 已执行。
--
-- 参数位（占位符 → 应用侧绑定）：
--   $1  query embedding      ::vector  —— 必须显式 ::vector，否则驱动传 text 时解析不出 <=>
--   $2  user_id              ::text
--   $3  query text           ::text
--   $4  vec_k                向量召回条数        50
--   $5  kw_k                 关键词召回条数      50
--   $6  rrf_k                RRF 平滑常数        60
--   $7  decay_offset_days    衰减"免罚期"        180
--   $8  decay_scale_days     衰减尺度            1825
--   $9  decay                衰减系数            0.5
--   $10 freq_weight          频率加成权重        0.2
--   $11 limit                最终返回条数        1~2 简单 / 5~10 复杂
--   $12 include_pending      是否包含待裁决行    false
--
-- 关于时间衰减，这里和上一版不同，值得说清楚：
--   上一版用「指数半衰期 90 天」：90 天后权重 0.5，一年后 0.06 —— 等于把老案例打死。
--   现在用「高斯 + 免罚期」：
--       score = decay ^ ((max(0, age_days - offset_days) / scale_days) ^ 2)
--   offset=180d / scale=1825d / decay=0.5 时：
--       半年   → 0.9999   （几乎不动）
--       1 年   → 0.993
--       2 年   → 0.939
--       5.5 年 → 0.5      （offset + scale 处正好半衰）
--      10 年   → 0.075
--   这是记忆的自然消退：近期经历不被额外加权，但也不会因为"老"就被判死。
--   要更激进的近期偏好，把 offset/scale 一起调小（scale=90, offset=0 时 90 天即 0.5）。
--
-- 相对最初那版草稿的修订（每处都会让召回静默变差）：
--   1. $1 显式 ::vector
--   2. kw 分支补 ORDER BY —— 原版只有窗口函数里的 ORDER BY，LIMIT 会返回任意行
--   3. 关键词判据改「子串包含 OR word_similarity」—— 见本节末尾的长注释
--   4. row_number() 代替 rank() —— 名次无并列，RRF 分布稳定
--   5. 未来时间的 age 钳零 —— 否则 event_time > now() 会得到负年龄
--   6. 当前有效只判 valid_until IS NULL（superseded_by 退回纯血缘指针）
--   7. 加 TTL 过滤与待裁决过滤（分类型保留策略见 01_schema.sql）
-- =============================================================================
\set ON_ERROR_STOP on

-- 同一会话里重复执行会报 "prepared statement already exists"：先 DEALLOCATE ALL;
-- 这些 PREPARE 只服务于 psql 里手跑验证，应用侧用下面的函数。

-- ---------------------------------------------------------------------------
-- 1. 主检索：情景记忆的混合召回
--    这份 PREPARE 是"可直接抄进应用"的权威 SQL 文本；recall_episodes() 函数体
--    逐字复用它，参数位也一致，避免两份 SQL 各自漂移。
-- ---------------------------------------------------------------------------
PREPARE recall_episodes_raw(
    vector, text, text, int, int, float8, float8, float8, float8, float8,
    int, boolean, float8, text[], uuid[]
) AS
WITH vec AS MATERIALIZED (
    SELECT id, row_number() OVER (ORDER BY dist) AS rk_v
    FROM (
        SELECT id, embedding <=> $1::vector AS dist
        FROM episodes
        WHERE user_id = $2
          AND valid_until IS NULL
          AND (expires_at IS NULL OR expires_at > now())
          AND ($12 OR review_state <> 'pending')
          AND embedding IS NOT NULL
        ORDER BY embedding <=> $1::vector
        LIMIT $4
    ) s
),
kw AS MATERIALIZED (
    SELECT id, row_number() OVER (ORDER BY is_hit DESC, sim DESC) AS rk_k
    FROM (
        SELECT id,
               strpos(content, $3) > 0      AS is_hit,   -- 精确子串（中文靠它）
               word_similarity($3, content) AS sim      -- 英文/长查询靠它
        FROM episodes
        WHERE user_id = $2
          AND valid_until IS NULL
          AND (expires_at IS NULL OR expires_at > now())
          AND ($12 OR review_state <> 'pending')
          AND $3 <> ''
          AND (strpos(content, $3) > 0 OR $3 <% content)
        ORDER BY is_hit DESC, sim DESC
        LIMIT $5
    ) s
),
gr AS MATERIALIZED (
    -- 图路：数组下标就是名次（$15 为空则空集）
    SELECT id, ord AS rk_g
    FROM unnest(COALESCE($15::uuid[], '{}'::uuid[])) WITH ORDINALITY AS t(id, ord)
),
rrf AS (
    SELECT id,
           COALESCE(1.0 / ($6 + rk_v), 0.0)
         + COALESCE(1.0 / ($6 + rk_k), 0.0)
         + COALESCE(1.0 / ($6 + rk_g), 0.0) AS s,
           rk_v, rk_k, rk_g
    FROM vec
    FULL OUTER JOIN kw USING (id)
    FULL OUTER JOIN gr USING (id)
),
scored AS (
    SELECT e.id,
           e.summary,
           COALESCE(e.summary, left(e.content, 200)) AS snippet,
           e.event_time,
           e.importance,
           e.confidence,
           e.source,
           e.expires_at,
           e.use_count,
           e.tags,                       -- 标签过滤需要（&& 走 idx_ep_tags 的 GIN）
           r.rk_v,
           r.rk_k,
           r.rk_g,
           r.s AS rrf_score,
           -- age 单位天；未来事件钳到 0
           GREATEST(EXTRACT(EPOCH FROM (now() - e.event_time))::float8, 0.0) / 86400.0 AS age_days
    FROM rrf r
    JOIN episodes e ON e.id = r.id
)
SELECT id, summary, snippet, event_time, importance, confidence, source, expires_at,
       rk_v, rk_k, rk_g, rrf_score, age_days,
       rrf_score
         * power($9, power(GREATEST(age_days - $7, 0.0) / $8, 2))        -- 高斯 + 免罚期
         * (1.0 + $10 * log(1 + use_count))                              -- 对数频率加成
         * (1.0 - $13 + $13 * importance)                                -- 重要性权重
       AS score
FROM scored
WHERE ($14::text[] IS NULL OR tags && $14)                               -- 标签过滤（&& 走 GIN）
ORDER BY score DESC
LIMIT $11;

-- 调用示例（psql 里手跑）：
--   BEGIN;
--   SET LOCAL hnsw.ef_search = 100;   -- 必须先设，理由见下一节
--   EXECUTE recall_episodes_raw(
--       (SELECT embedding FROM episodes WHERE user_id = 'u_1' LIMIT 1),
--       'u_1', '时区', 50, 50, 60, 180, 1825, 0.5, 0.2, 8, false, 0.5, NULL, NULL);
--   COMMIT;
--
-- 最后一组参数：重要性权重 0.5、标签过滤 NULL、图扩展 NULL（= 只用两路）。
-- 要开第三路，把 NULL 换成 graph_episodes('u_1', ARRAY['阿里云'], 2, 50) 的 id 列数组：
--   SELECT array_agg(episode_id ORDER BY depth) FROM graph_episodes('u_1', ARRAY['阿里云']);
--
-- 更省事的方式是用下面的函数（内部已经把 ef_search 设好了）：
--   SELECT * FROM recall_episodes((SELECT embedding FROM episodes LIMIT 1), 'u_1', '时区');

-- ---------------------------------------------------------------------------
-- 2. ef_search：必须按查询设，不要写死在配置文件里
--
-- HNSW + user_id 过滤最大的坑：HNSW 是图近似搜索，先在图上取 ef_search 个候选，
-- **然后**才应用 WHERE。过滤掉 99% 之后可能一条都不剩 —— 库里有数据、查询不报错、
-- 就是召回 0。三种解法，按顺序采用：
--   a) pgvector >= 0.8：开 hnsw.iterative_scan，不足时继续扫
--   b) ef_search >= max(4 * limit, vec_k)
--   c) 单用户记忆量小（< 数万行）时直接放弃 HNSW，用精确扫描
-- ---------------------------------------------------------------------------
-- ⚠️ 换签名前必须显式 DROP 旧函数。
-- CREATE OR REPLACE FUNCTION 只替换**参数表完全相同**的函数；参数表一变就是新建重载，
-- 于是 recall_episodes(...) 变成歧义调用，COMMENT ON FUNCTION 也会报"函数名不是唯一的"。
-- 这正是"改函数签名"在生产上最常见的翻车方式，这里把上一版的签名都列出来删掉。
DROP FUNCTION IF EXISTS recall_episodes(
    vector, text, text, int, int, float8, float8, float8, float8, float8,
    int, int, boolean);
DROP FUNCTION IF EXISTS recall_episodes(
    vector, text, text, int, int, float8, float8, float8, float8, float8,
    int, int, boolean, float8, text[]);

CREATE OR REPLACE FUNCTION recall_episodes(
    p_query_vec         vector,
    p_user_id           text,
    p_query_text        text,
    p_vec_k             int     DEFAULT 50,
    p_kw_k              int     DEFAULT 50,
    p_rrf_k             float8  DEFAULT 60,
    p_decay_offset_days float8  DEFAULT 180,
    p_decay_scale_days  float8  DEFAULT 1825,
    p_decay             float8  DEFAULT 0.5,
    p_freq_weight       float8  DEFAULT 0.2,
    p_limit             int     DEFAULT 8,
    p_ef_search         int     DEFAULT NULL,
    p_include_pending   boolean DEFAULT false,
    -- 重要性权重：0 = 不参与，1 = 完全按 importance 缩放。
    -- 默认 0.5 → 乘数落在 [0.5, 1.0]，最多 2 倍差距，不会让"重要但无关"的东西霸榜。
    p_importance_weight float8  DEFAULT 0.5,
    -- 标签过滤：NULL = 不过滤；给定则要求 tags 与之有交集（&& 走 GIN 索引）
    p_tags              text[]  DEFAULT NULL,
    -- 第三路：图扩展。
    -- 刻意收**已算好的 UUID 数组**，而不是在这里递归遍历：
    --   1) recall_episodes 因此不依赖 entities/relations 表，sql/02 单独加载也能跑；
    --   2) 图的种子（哪些实体）属于应用层的判断，不该埋在 SQL 里；
    --   3) 遍历是重查询，放在 RRF 之前可以按需开关、单独加缓存。
    -- 数组顺序即图排序（array position = rank），由 graph_episodes() 的 depth/时间排序给出。
    -- NULL/空数组 = 不参与，与两路召回行为完全一致（RRF 不做归一化，缺一路不补偿）。
    p_graph_episodes    uuid[]  DEFAULT NULL
) RETURNS TABLE (
    id uuid, summary text, snippet text, event_time timestamptz,
    importance real, confidence real, source text, expires_at timestamptz,
    rk_v bigint, rk_k bigint, rk_g bigint,
    rrf_score float8, age_days float8, score float8
)
-- VOLATILE：函数里调了 set_config（改 GUC 是有副作用的），标 STABLE 就是撒谎
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    v_ef int := COALESCE(p_ef_search, GREATEST(4 * p_limit, p_vec_k));
BEGIN
    -- set_config(..., is_local => true)：只在当前事务/语句内生效，
    -- 不会污染连接池里的下一条查询（session 级 SET 的经典事故）
    PERFORM set_config('hnsw.ef_search', v_ef::text, true);

    BEGIN
        PERFORM set_config('hnsw.iterative_scan', 'relaxed_order', true);
        PERFORM set_config('hnsw.max_scan_tuples', '20000', true);
    EXCEPTION WHEN OTHERS THEN
        NULL;   -- pgvector < 0.8 没有这些 GUC
    END;

    RETURN QUERY EXECUTE $q$
        WITH vec AS MATERIALIZED (
            SELECT id, row_number() OVER (ORDER BY dist) AS rk_v
            FROM (
                SELECT id, embedding <=> $1::vector AS dist
                FROM episodes
                WHERE user_id = $2
                  AND valid_until IS NULL
                  AND (expires_at IS NULL OR expires_at > now())
                  AND ($12 OR review_state <> 'pending')
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> $1::vector
                LIMIT $4
            ) s
        ),
        kw AS MATERIALIZED (
            SELECT id, row_number() OVER (ORDER BY is_hit DESC, sim DESC) AS rk_k
            FROM (
                SELECT id,
                       strpos(content, $3) > 0      AS is_hit,
                       word_similarity($3, content) AS sim
                FROM episodes
                WHERE user_id = $2
                  AND valid_until IS NULL
                  AND (expires_at IS NULL OR expires_at > now())
                  AND ($12 OR review_state <> 'pending')
                  AND $3 <> ''
                  AND (strpos(content, $3) > 0 OR $3 <% content)
                ORDER BY is_hit DESC, sim DESC
                LIMIT $5
            ) s
        ),
        gr AS MATERIALIZED (
            -- 图路：数组下标就是名次。COALESCE 保证 NULL 时是空集（不是 NULL 行）
            SELECT id, ord AS rk_g
            FROM unnest(COALESCE($15::uuid[], '{}'::uuid[])) WITH ORDINALITY AS t(id, ord)
        ),
        rrf AS (
            -- 三路 FULL OUTER JOIN：任一路命中即入候选。
            -- 没有命中某一路的，那一项的 rank 为 NULL，1.0/(k+NULL) = NULL，COALESCE 成 0。
            SELECT id,
                   COALESCE(1.0 / ($6 + rk_v), 0.0)
                 + COALESCE(1.0 / ($6 + rk_k), 0.0)
                 + COALESCE(1.0 / ($6 + rk_g), 0.0) AS s,
                   rk_v, rk_k, rk_g
            FROM vec
            FULL OUTER JOIN kw USING (id)
            FULL OUTER JOIN gr USING (id)
        ),
        scored AS (
            SELECT e.id, e.summary,
                   COALESCE(e.summary, left(e.content, 200)) AS snippet,
                   e.event_time, e.importance, e.confidence, e.source,
                   e.expires_at, e.use_count, e.tags,
                   r.rk_v, r.rk_k, r.rk_g, r.s AS rrf_score,
                   GREATEST(EXTRACT(EPOCH FROM (now() - e.event_time))::float8, 0.0) / 86400.0 AS age_days
            FROM rrf r JOIN episodes e ON e.id = r.id
        )
        SELECT id, summary, snippet, event_time, importance, confidence, source, expires_at,
               rk_v, rk_k, rk_g, rrf_score, age_days,
               rrf_score * power($9, power(GREATEST(age_days - $7, 0.0) / $8, 2))
                         * (1.0 + $10 * log(1 + use_count))
                         * (1.0 - $13 + $13 * importance) AS score
        FROM scored
        WHERE ($14::text[] IS NULL OR tags && $14)
        ORDER BY score DESC
        LIMIT $11
    $q$
    USING p_query_vec, p_user_id, p_query_text, p_vec_k, p_kw_k, p_rrf_k,
          p_decay_offset_days, p_decay_scale_days, p_decay, p_freq_weight,
          p_limit, p_include_pending, p_importance_weight, p_tags, p_graph_episodes;
END $$;

COMMENT ON FUNCTION recall_episodes(
    vector, text, text, int, int, float8, float8, float8, float8, float8,
    int, int, boolean, float8, text[], uuid[]
) IS
'混合召回：向量 ⊕ 词法 ⊕ 图 → RRF(k=60) → 高斯衰减(offset/scale/decay) × 对数频率 × 重要性权重；可带标签过滤与图扩展。返回 summary 层。';

-- ---------------------------------------------------------------------------
-- 3. 长期事实检索：按 subject key 精确取，不走向量
--
-- 注意衰减口径与情景记忆**不同**：事实按 last_used_at 衰减，不按 event_time。
-- 事实本身不会"变老"，衰减的是"近期没被需要"；混用会系统性误伤高频有价值的旧事实。
-- 因此 facts 表根本没有 event_time 列 —— 类型系统层面就不给你写错的机会。
-- ---------------------------------------------------------------------------
PREPARE recall_facts_raw(text, text[], int, float8, float8, float8) AS
SELECT f.key, f.value, f.confidence, f.importance, f.source, f.valid_from, f.last_used_at,
       GREATEST(EXTRACT(EPOCH FROM (now() - COALESCE(f.last_used_at, f.valid_from)))::float8,
                0.0) / 86400.0 AS age_days,
       f.importance
         * power($6, power(
             GREATEST(
                 GREATEST(EXTRACT(EPOCH FROM (now() - COALESCE(f.last_used_at, f.valid_from)))::float8,
                          0.0) / 86400.0 - $4,
                 0.0) / $5, 2)) AS score
FROM facts f
WHERE f.user_id = $1
  AND f.valid_until IS NULL
  AND ($2::text[] IS NULL OR f.key = ANY($2))
ORDER BY score DESC, f.key
LIMIT $3;

CREATE OR REPLACE FUNCTION recall_facts(
    p_user_id           text,
    p_keys              text[]  DEFAULT NULL,
    p_limit             int     DEFAULT 20,
    p_decay_offset_days float8  DEFAULT 30,
    p_decay_scale_days  float8  DEFAULT 365,
    p_decay             float8  DEFAULT 0.5
) RETURNS TABLE (
    key text, value jsonb, confidence real, importance real, source text,
    valid_from timestamptz, last_used_at timestamptz, age_days float8, score float8
)
LANGUAGE sql STABLE AS $fn$
    SELECT f.key, f.value, f.confidence, f.importance, f.source, f.valid_from, f.last_used_at,
           GREATEST(EXTRACT(EPOCH FROM (now() - COALESCE(f.last_used_at, f.valid_from)))::float8,
                    0.0) / 86400.0 AS age_days,
           f.importance
             * power(p_decay, power(
                 GREATEST(
                     GREATEST(EXTRACT(EPOCH FROM (now() - COALESCE(f.last_used_at, f.valid_from)))::float8,
                              0.0) / 86400.0 - p_decay_offset_days,
                     0.0) / p_decay_scale_days, 2)) AS score
    FROM facts f
    WHERE f.user_id = p_user_id
      AND f.valid_until IS NULL
      AND (p_keys IS NULL OR f.key = ANY(p_keys))
    ORDER BY score DESC, f.key
    LIMIT p_limit
$fn$;

COMMENT ON FUNCTION recall_facts IS
'长期事实按时序取用：score = importance × 高斯(last_used_at 年龄)。不走向量，不按 event_time。';

-- ---------------------------------------------------------------------------
-- 4. 渐进披露第二层：按 id 拉全文
-- 只有 summary 不足以回答时才调用 —— 这一步是 token 成本最直接的开关。
-- ---------------------------------------------------------------------------
PREPARE fetch_contents(uuid[]) AS
SELECT id, content, summary, event_time, source, confidence
FROM episodes
WHERE id = ANY($1);

-- ---------------------------------------------------------------------------
-- 5. 使用反馈：use_count 回写
-- 检索路径里直接 UPDATE 会把只读查询变成写事务（MVCC 膨胀 + autovacuum 压力）。
-- 做法：检索只返回 id，后台异步批量刷。情景记忆与长期事实各一条。
-- ---------------------------------------------------------------------------
PREPARE bump_usage(uuid[]) AS
UPDATE episodes
SET use_count = use_count + 1, last_used_at = now()
WHERE id = ANY($1)
RETURNING id, use_count;

PREPARE bump_fact_usage(text, text[]) AS
UPDATE facts
SET use_count = use_count + 1, last_used_at = now()
WHERE user_id = $1 AND valid_until IS NULL AND key = ANY($2)
RETURNING key, use_count;

-- ---------------------------------------------------------------------------
-- 6. 写入裁决用的"相似候选"（不是给回答用的）
-- 纯相似度、不带衰减/频率：裁决关心的是"是不是同一件事"，不是"该不该现在被想起"。
-- ---------------------------------------------------------------------------
PREPARE conflict_candidates(vector, text, int, float8) AS
SELECT e.id, e.content, e.summary, e.confidence, e.importance, e.source, e.use_count,
       e.valid_until, e.event_time, e.embedding <=> $1::vector AS dist
FROM episodes e
WHERE e.user_id = $2
  AND e.valid_until IS NULL
  AND e.embedding IS NOT NULL
  AND (e.embedding <=> $1::vector) < $4
ORDER BY e.embedding <=> $1::vector
LIMIT $3;                                   -- 3~5 条足够，多了 LLM 反而选错

PREPARE exact_duplicate(text, text) AS
SELECT id, content
FROM episodes
WHERE user_id = $1 AND valid_until IS NULL AND content = $2;

-- ---------------------------------------------------------------------------
-- 7. 待裁决冲突的巡检（告警 / 人工处理入口）
-- ---------------------------------------------------------------------------
PREPARE pending_conflicts(text, int) AS
SELECT c.id, c.created_at, c.reason, c.signal,
       old.content AS existing_content, old.confidence AS existing_confidence,
       new.content AS incoming_content, new.confidence AS incoming_confidence
FROM memory_conflicts c
JOIN episodes old ON old.id = c.existing_id
JOIN episodes new ON new.id = c.incoming_id
WHERE c.status = 'open' AND ($1::text IS NULL OR c.user_id = $1)
ORDER BY c.created_at DESC
LIMIT $2;

-- ---------------------------------------------------------------------------
-- 8. 可选：不想用 trgm 时的词法路径
--
-- 严格说 pg_trgm 不是 BM25：它没有词频饱和、没有文档长度归一、没有 IDF，
-- 只是三元组集合相似度。要真 BM25 得上 ParadeDB 的 pg_search 扩展。
-- 中文另有一条路：分词之后走 tsvector（需 zhparser 或 pg_bigm）：
--
--   CREATE INDEX ... USING gin (to_tsvector('simple', content));
--   ... WHERE to_tsvector('simple', content) @@ plainto_tsquery('simple', $3)
--   ... ORDER BY ts_rank_cd(to_tsvector('simple', content), plainto_tsquery('simple', $3)) DESC
--
-- 关于为什么中文必须靠 strpos：pg_trgm 会给字符串**两端补空格**再造三元组，
-- '时区' -> '  时',' 时区','时区 '，其中两个带空格；而长文本「…的时区是…」里
-- 只有 '的时区'。于是 similarity 与 word_similarity 对中文短查询**双双为 0**
-- （实测：similarity=0.000 word_similarity=0.000），%，<% 都召回不到。
-- 英文则相反：word_similarity('timezone', 长句) = 1.000 —— <% 正是为它设计的。
-- 所以判据是两条 OR 起来：strpos（中文/短查询，零歧义）+ <%（英文/长查询，可走 GIN）。
-- 单用户记忆量到万级后，中文词法路要换 pg_bigm 或 zhparser + tsvector。
-- ---------------------------------------------------------------------------
