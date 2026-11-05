-- =============================================================================
-- 图扩展层（阶段四）：实体 + 关系 + 多跳遍历，作为混合召回的**第三路**
--
-- 设计稿把图放在最后："只有当关系型问题（'这个客户所属集团下其他子公司怎么处理的'）
-- 在真实请求中反复出现、且向量明显答不对时，再加图扩展。"
--
-- 这里**没有引入 Apache AGE**，原因是权衡而非偷懒：
--   · AGE 要在 Windows 上用 flex/bison + PostgreSQL 解析器钩子编译，成本远高于 pgvector；
--   · 本项目的图查询形状很窄 —— "从若干种子实体出发，沿关系走 N 跳，取关联记忆"，
--     PostgreSQL 的递归 CTE 完全够用，且不需要第二套查询语言；
--   · 少一个扩展 = 少一个版本绑定（AGE 与 PG 大版本强耦合）。
-- 将来真要 Cypher 风格的开放式图查询，再把这一层换到 AGE —— 表结构不用动。
--
-- 语义与其它层保持一致：
--   · relations 也是双时态（valid_until IS NULL = 当前有效），旧关系不物理删；
--   · 遍历只走"当前有效"的关系；
--   · episode_entities 把记忆挂到实体上，于是"实体 → 记忆"是直接索引，不需要扫描。
-- =============================================================================
\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- 1. 实体
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entities (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    TEXT        NOT NULL,
    name       TEXT        NOT NULL CHECK (length(name) BETWEEN 1 AND 120),  -- 规范化名（小写、去空格）
    display    TEXT        NOT NULL,                                         -- 原样显示
    label      TEXT        NOT NULL DEFAULT 'concept'
                           CHECK (label IN ('person','org','project','system','place','concept')),
    aliases    TEXT[]      NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_name ON entities (user_id, name);
CREATE INDEX IF NOT EXISTS idx_entity_label ON entities (user_id, label);

COMMENT ON COLUMN entities.name IS '规范化名：小写、去首尾空格、压缩内部空白；匹配一律用它';

-- ---------------------------------------------------------------------------
-- 2. 关系（双时态，旧关系不删）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS relations (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        TEXT        NOT NULL,
    src_id         UUID        NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    dst_id         UUID        NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    predicate      TEXT        NOT NULL CHECK (predicate ~ '^[a-z_]{2,32}$'),
    confidence     REAL        NOT NULL DEFAULT 0.8 CHECK (confidence BETWEEN 0 AND 1),
    source_episode UUID        REFERENCES episodes(id) ON DELETE SET NULL,
    valid_from     TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until    TIMESTAMPTZ,                        -- NULL = 当前有效
    superseded_by  UUID        REFERENCES relations(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT rel_no_self_loop CHECK (src_id <> dst_id)
);

-- 同一 (源, 谓词, 目标) 只能有一条当前有效关系
CREATE UNIQUE INDEX IF NOT EXISTS uq_rel_live
    ON relations (src_id, dst_id, predicate) WHERE valid_until IS NULL;
CREATE INDEX IF NOT EXISTS idx_rel_src ON relations (src_id) WHERE valid_until IS NULL;
CREATE INDEX IF NOT EXISTS idx_rel_dst ON relations (dst_id) WHERE valid_until IS NULL;

COMMENT ON COLUMN relations.predicate IS '小写下划线谓词：belongs_to / works_at / depends_on / part_of / owns';

-- ---------------------------------------------------------------------------
-- 3. 记忆 ↔ 实体
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS episode_entities (
    episode_id UUID        NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    entity_id  UUID        NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (episode_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_epent_entity ON episode_entities (entity_id);

-- ---------------------------------------------------------------------------
-- 4. 写入：实体 / 关系 / 挂记忆
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION graph_norm(p_text TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $fn$
    SELECT lower(btrim(regexp_replace(COALESCE(p_text, ''), '\s+', ' ', 'g')))
$fn$;

CREATE OR REPLACE FUNCTION graph_upsert_entity(
    p_user_id TEXT,
    p_display TEXT,
    p_label   TEXT DEFAULT 'concept',
    p_aliases TEXT[] DEFAULT '{}'
) RETURNS UUID LANGUAGE plpgsql AS $fn$
DECLARE
    v_name TEXT := graph_norm(p_display);
    v_id   UUID;
BEGIN
    IF v_name = '' THEN
        RAISE EXCEPTION 'graph_upsert_entity: 实体名为空';
    END IF;

    INSERT INTO entities (user_id, name, display, label, aliases)
    VALUES (p_user_id, v_name, btrim(p_display), p_label, COALESCE(p_aliases, '{}'))
    ON CONFLICT (user_id, name) DO UPDATE
        SET updated_at = now(),
            -- 标签从 concept 升级成具体类型；别名取并集
            label = CASE WHEN entities.label = 'concept' THEN EXCLUDED.label ELSE entities.label END,
            aliases = ARRAY(SELECT DISTINCT unnest(entities.aliases || EXCLUDED.aliases))
    RETURNING id INTO v_id;

    RETURN v_id;
END $fn$;

CREATE OR REPLACE FUNCTION graph_relate(
    p_user_id  TEXT,
    p_src      TEXT,
    p_predicate TEXT,
    p_dst      TEXT,
    p_confidence REAL DEFAULT 0.8,
    p_source_episode UUID DEFAULT NULL
) RETURNS UUID LANGUAGE plpgsql AS $fn$
DECLARE
    v_src UUID; v_dst UUID; v_id UUID;
BEGIN
    v_src := graph_upsert_entity(p_user_id, p_src);
    v_dst := graph_upsert_entity(p_user_id, p_dst);

    INSERT INTO relations (user_id, src_id, dst_id, predicate, confidence, source_episode)
    VALUES (p_user_id, v_src, v_dst, lower(p_predicate), p_confidence, p_source_episode)
    ON CONFLICT DO NOTHING
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        -- 已存在当前有效关系：只更新置信度（取较高者）
        UPDATE relations
        SET confidence = GREATEST(confidence, p_confidence)
        WHERE src_id = v_src AND dst_id = v_dst
          AND predicate = lower(p_predicate) AND valid_until IS NULL
        RETURNING id INTO v_id;
    END IF;

    RETURN v_id;
END $fn$;

CREATE OR REPLACE FUNCTION graph_link_episode(
    p_episode_id UUID,
    p_entity_names TEXT[]
) RETURNS INT LANGUAGE plpgsql AS $fn$
DECLARE
    v_user TEXT;
    n INT;
BEGIN
    SELECT user_id INTO v_user FROM episodes WHERE id = p_episode_id;
    IF v_user IS NULL THEN
        RAISE EXCEPTION 'graph_link_episode: 记忆 % 不存在', p_episode_id;
    END IF;

    INSERT INTO episode_entities (episode_id, entity_id)
    SELECT p_episode_id, e.id
    FROM entities e
    WHERE e.user_id = v_user AND e.name = ANY(SELECT graph_norm(x) FROM unnest(p_entity_names) x)
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $fn$;

-- ---------------------------------------------------------------------------
-- 5. 查询：多跳遍历
--
-- 无向遍历（关系两个方向都算），带访问集合防环，depth 上限兜底。
-- 返回每个可达实体的**最短深度**（DISTINCT ON 取 depth 最小）。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION graph_neighbors(
    p_user_id TEXT,
    p_seed_names TEXT[],
    p_max_depth INT DEFAULT 2,
    p_limit INT DEFAULT 200
) RETURNS TABLE (entity_id uuid, name text, display text, label text, depth int, path text[])
LANGUAGE sql STABLE AS $fn$
    WITH RECURSIVE seeds AS (
        SELECT e.id, e.name, e.display, e.label, 0 AS depth,
               ARRAY[e.id] AS visited,
               ARRAY[e.display] AS path
        FROM entities e
        WHERE e.user_id = p_user_id
          AND e.name = ANY(SELECT graph_norm(x) FROM unnest(p_seed_names) x)

        UNION ALL

        SELECT n.id, n.name, n.display, n.label, s.depth + 1,
               s.visited || n.id,
               s.path || n.display
        FROM seeds s
        JOIN relations r
          ON r.valid_until IS NULL
         AND (r.src_id = s.id OR r.dst_id = s.id)
        JOIN entities n
          ON n.id = CASE WHEN r.src_id = s.id THEN r.dst_id ELSE r.src_id END
        WHERE s.depth < p_max_depth
          AND NOT (n.id = ANY(s.visited))
    ),
    best AS (
        SELECT DISTINCT ON (id) id, name, display, label, depth, path
        FROM seeds
        ORDER BY id, depth
    )
    SELECT id, name, display, label, depth, path
    FROM best
    ORDER BY depth, display
    LIMIT p_limit
$fn$;

-- 从种子实体出发，取可达实体上挂着的记忆（这是第三路召回的输入）
--
-- via_entity 必须是**最近那一跳**的实体：先按 (记忆, 深度) 取最短深度，
-- 再在该深度里取一个显示名。用 min(display) 会给出"depth=0 却标着 depth=2 的实体"
-- 这种自相矛盾的标签，排查时非常误导。
CREATE OR REPLACE FUNCTION graph_episodes(
    p_user_id TEXT,
    p_seed_names TEXT[],
    p_max_depth INT DEFAULT 2,
    p_limit INT DEFAULT 50
) RETURNS TABLE (episode_id uuid, depth int, via_entity text)
LANGUAGE sql STABLE AS $fn$
    WITH nb AS (
        SELECT * FROM graph_neighbors(p_user_id, p_seed_names, p_max_depth, 500)
    ),
    hit AS (
        SELECT ee.episode_id, nb.depth, nb.display, e.event_time
        FROM nb
        JOIN episode_entities ee ON ee.entity_id = nb.entity_id
        JOIN episodes e ON e.id = ee.episode_id
        WHERE e.user_id = p_user_id
          AND e.valid_until IS NULL
          AND e.review_state <> 'pending'
          AND (e.expires_at IS NULL OR e.expires_at > now())
    ),
    best AS (
        SELECT DISTINCT ON (episode_id)
               episode_id, depth, display AS via_entity, event_time
        FROM hit
        ORDER BY episode_id, depth, display
    )
    SELECT episode_id, depth, via_entity
    FROM best
    ORDER BY depth, event_time DESC
    LIMIT p_limit
$fn$;

-- 从一段文本里认出**已知实体**（最长优先，避免"阿里"吃掉"阿里巴巴"）
-- 这不是 NER：只匹配库里已有的实体名/别名，所以不会凭空造实体。
CREATE OR REPLACE FUNCTION graph_entities_in_text(
    p_user_id TEXT,
    p_text TEXT
) RETURNS TABLE (entity_id uuid, name text, display text, label text)
LANGUAGE sql STABLE AS $fn$
    WITH t AS (SELECT graph_norm(p_text) AS s)
    SELECT e.id, e.name, e.display, e.label
    FROM entities e, t
    WHERE e.user_id = p_user_id
      AND (
          position(e.name IN t.s) > 0
          OR EXISTS (
              SELECT 1 FROM unnest(e.aliases) a
              WHERE a <> '' AND position(graph_norm(a) IN t.s) > 0
          )
      )
    ORDER BY length(e.name) DESC
    LIMIT 20
$fn$;

-- 统计
CREATE OR REPLACE FUNCTION graph_stats(p_user_id TEXT)
RETURNS TABLE (entities bigint, relations_live bigint, linked_episodes bigint)
LANGUAGE sql STABLE AS $fn$
    SELECT
        (SELECT count(*) FROM entities WHERE user_id = p_user_id),
        (SELECT count(*) FROM relations WHERE user_id = p_user_id AND valid_until IS NULL),
        (SELECT count(DISTINCT ee.episode_id) FROM episode_entities ee
         JOIN episodes e ON e.id = ee.episode_id WHERE e.user_id = p_user_id)
$fn$;

DO $$
BEGIN
    RAISE NOTICE 'graph ok：entities / relations / episode_entities / graph_neighbors / graph_episodes';
END $$;
