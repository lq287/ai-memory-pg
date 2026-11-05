-- =============================================================================
-- AI 记忆层 · 单库 Schema（PostgreSQL 15+ / pgvector 0.8+ / pg_trgm）
-- 目标：一个库装下情景记忆、长期事实、冲突待裁决、写入队列（含死信）。
-- 运行：psql "$PG_URL" -f sql/01_schema.sql
-- 幂等：全部 IF NOT EXISTS / OR REPLACE / DROP TRIGGER IF EXISTS，可重复执行。
-- 注意：IF NOT EXISTS 只保证"表在就不报错"，不会帮你改已存在表的结构 —— 改字段写迁移。
-- =============================================================================
\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- 0. 扩展
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- 1. 分类型保留策略（TTL）
--
-- 不用一把统一的 TTL 管所有记忆：原始轮次密度最低、过期最快；派生摘要的寿命
-- 远长于原材料。这张策略表是**唯一**的 TTL 真源，写入时由触发器落到 expires_at。
--
-- 过期用 expires_at 在**查询层过滤**，不做定时清理作业 —— 清理作业的窗口期内
-- 过期记录还是会被召回，而且一旦作业挂了没人知道。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ttl_interval(p_kind text) RETURNS interval
LANGUAGE sql IMMUTABLE AS $fn$
    SELECT CASE p_kind
             WHEN 'raw_turn'  THEN interval '30 days'    -- 原始轮次：短命
             WHEN 'episode'   THEN interval '90 days'    -- 情景事件：留够近案例
             WHEN 'summary'   THEN interval '3650 days'  -- 派生摘要：长期
             ELSE NULL                                   -- 长期层：不按时间过期
           END
$fn$;

COMMENT ON FUNCTION ttl_interval IS
'分类型保留策略的真源。返回 NULL = 不按时间过期（靠 superseded_by 与衰减权重管理）。';

-- ---------------------------------------------------------------------------
-- 2. 情景记忆 episodes
--
-- 双时态三条时间线，各管一件事：
--   event_time    —— 现实世界何时发生（抽取 LLM 给，给不出就回落并标记来源）
--   ingested_at   —— 系统何时知道（= 插入时刻）；TTL 从这一刻算
--   valid_from/until —— 这条认知在系统里何时为真（失效即区间闭合）
--
-- 唯一"当前有效"判据 = valid_until IS NULL（区间未闭合）。
-- superseded_by 只是血缘指针（谁接替了它），不参与任何"是否有效"的判断。
-- 为什么不用 'infinity' / '9999-12-31' 哨兵，见下方 COMMENT 前的长注释。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS episodes (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           TEXT        NOT NULL,

    -- 记忆单元按"事件"切分，不按 token 切分。kind 决定 TTL 与检索权重。
    kind              TEXT        NOT NULL DEFAULT 'episode'
                                  CHECK (kind IN ('raw_turn','episode','summary','longterm')),

    content           TEXT        NOT NULL CHECK (length(content) > 0),
    summary           TEXT,                          -- 50~100 token，渐进披露第一层
    embedding         vector(768),                   -- 维度必须与 embedder 一致
    embedding_model   TEXT        NOT NULL DEFAULT 'unknown',

    -- 双时态
    event_time        TIMESTAMPTZ NOT NULL,
    event_time_source TEXT        NOT NULL DEFAULT 'ingest'
                                  CHECK (event_time_source IN ('extracted','inferred','ingest')),
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_from        TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until       TIMESTAMPTZ,                -- NULL = 未失效（当前有效）
    superseded_by     UUID        REFERENCES episodes(id),   -- 延迟约束，见第 6 节

    -- 保留期：由触发器按 kind 从 ttl_interval() 算出；NULL = 不按时间过期
    expires_at        TIMESTAMPTZ,

    -- 治理
    source            TEXT        NOT NULL
                                  CHECK (source IN ('conversation','tool','human','import')),
    source_ref        TEXT,                          -- 幂等键
    confidence        REAL        NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
    importance        REAL        NOT NULL DEFAULT 0.5 CHECK (importance BETWEEN 0 AND 1),
    use_count         INT         NOT NULL DEFAULT 0 CHECK (use_count >= 0),
    last_used_at      TIMESTAMPTZ,
    -- fresh / aging / faded / expired：派生自 kind + ingested_at，**不参与检索**，
    -- 只用于运维看板与告警；真相永远由 expires_at 与查询层过滤决定。
    lifecycle_state   TEXT        NOT NULL DEFAULT 'fresh'
                                  CHECK (lifecycle_state IN ('fresh','aging','faded','expired')),
    -- 待裁决标记：defer 动作写入的行 review_state='pending'，默认不参与召回
    review_state      TEXT        NOT NULL DEFAULT 'none'
                                  CHECK (review_state IN ('none','pending','rejected')),
    tags              TEXT[]      NOT NULL DEFAULT '{}'
);

COMMENT ON COLUMN episodes.valid_until   IS 'NULL = 未失效（这是"当前有效"的唯一判据）';
COMMENT ON COLUMN episodes.expires_at    IS 'TTL 截止时刻，由 kind 经触发器算出；检索层过滤';
COMMENT ON COLUMN episodes.review_state  IS 'pending = 疑似矛盾待裁决，默认不召回';
COMMENT ON COLUMN episodes.summary       IS '默认返回层，控制 token 成本';
COMMENT ON COLUMN episodes.source_ref    IS '幂等键，队列重试时防重复落库';
COMMENT ON COLUMN episodes.lifecycle_state IS '派生展示字段，不参与检索（真源是 expires_at）';

-- "当前有效"的判据改过两次，留个记录：
--   原稿：  valid_until > now() AND superseded_by IS NULL      —— 两个真相源
--   我一度：valid_until NOT NULL DEFAULT 'infinity'            —— 更干净，但见下
--   现在：  valid_until IS NULL                                —— 唯一判据
-- 1) 'infinity' 语义最漂亮，但 psycopg 3 的 timestamptz loader 直接拒绝：
--        DataError: timestamp too large (after year 10K): 'infinity'
--    JDBC 默认也一样 —— 任何 SELECT 到 valid_until 的查询都会在应用层炸。
-- 2) 换 '9999-12-31' 哨兵同样不行：会话时区 UTC+8 下就是 10000-01-01，照样溢出。
-- 3) 更要紧的：只要"未失效"有值域，"旧行失效但无接替者"（软删除）就写不了
--    superseded_by；判据一旦是 superseded_by IS NULL，被作废的记忆会继续被召回。
-- NULL 一并解决三件事：区间未闭合即 NULL（时间旅行的标准写法）、客户端天然安全、
-- 判据只剩一个字段。

-- 幂等：同一 (user_id, source, source_ref) 只允许一条
CREATE UNIQUE INDEX IF NOT EXISTS uq_ep_source_ref
    ON episodes (user_id, source, source_ref)
    WHERE source_ref IS NOT NULL;

-- 向量索引：只覆盖"当前有效且非待裁决"的行。
-- m=8 从默认 16 下调，索引体积近半；ef_construction=64 保持默认。
CREATE INDEX IF NOT EXISTS idx_ep_hnsw
    ON episodes USING hnsw (embedding vector_cosine_ops)
    WITH (m = 8, ef_construction = 64)
    WHERE valid_until IS NULL AND review_state <> 'pending';

-- 关键词索引（trgm 走 GIN）
CREATE INDEX IF NOT EXISTS idx_ep_trgm
    ON episodes USING gin (content gin_trgm_ops)
    WHERE valid_until IS NULL AND review_state <> 'pending';

-- 主查询骨架
CREATE INDEX IF NOT EXISTS idx_ep_user_live
    ON episodes (user_id, event_time DESC)
    WHERE valid_until IS NULL AND review_state <> 'pending';

-- TTL 扫描 / 待裁决巡检
CREATE INDEX IF NOT EXISTS idx_ep_expires
    ON episodes (expires_at) WHERE expires_at IS NOT NULL AND valid_until IS NULL;
CREATE INDEX IF NOT EXISTS idx_ep_pending_review
    ON episodes (user_id, ingested_at DESC) WHERE review_state = 'pending';

CREATE INDEX IF NOT EXISTS idx_ep_tags ON episodes USING gin (tags);

-- TTL 触发器：任何写入路径都不会忘记算过期时间
CREATE OR REPLACE FUNCTION episodes_apply_ttl() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.expires_at IS NULL THEN
        NEW.expires_at := NEW.ingested_at + ttl_interval(NEW.kind);
    END IF;
    RETURN NEW;
END $fn$;

DROP TRIGGER IF EXISTS trg_episodes_ttl ON episodes;
CREATE TRIGGER trg_episodes_ttl
    BEFORE INSERT OR UPDATE OF kind, ingested_at ON episodes
    FOR EACH ROW EXECUTE FUNCTION episodes_apply_ttl();

-- 生命周期派生：只用于展示/告警，不是检索依据
CREATE OR REPLACE FUNCTION lifecycle_of(
    p_kind text, p_ingested_at timestamptz, p_at timestamptz DEFAULT now()
) RETURNS text LANGUAGE sql STABLE AS $fn$
    SELECT CASE
        WHEN ttl_interval(p_kind) IS NULL THEN 'fresh'
        WHEN p_at >= p_ingested_at + ttl_interval(p_kind)                    THEN 'expired'
        WHEN p_at >= p_ingested_at + ttl_interval(p_kind) * 0.75             THEN 'faded'
        WHEN p_at >= p_ingested_at + ttl_interval(p_kind) * 0.5              THEN 'aging'
        ELSE 'fresh'
    END
$fn$;

CREATE OR REPLACE FUNCTION refresh_lifecycle() RETURNS TABLE (updated bigint)
LANGUAGE plpgsql AS $fn$
DECLARE
    v_count bigint;
BEGIN
    UPDATE episodes e
    SET lifecycle_state = lifecycle_of(e.kind, e.ingested_at)
    WHERE e.lifecycle_state <> lifecycle_of(e.kind, e.ingested_at);
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN QUERY SELECT v_count;
END $fn$;

-- ---------------------------------------------------------------------------
-- 3. 长期事实 facts —— 键值表，不走向量
--
-- 与情景记忆分离存储（决策一）：访问模式是"按主体键精确取用"，不是语义召回。
-- 原版 PK (user_id, key, valid_from) 且 valid_from DEFAULT now()：
-- now() 是事务开始时间，同一事务写同一 key 两次直接主键冲突；而且这个 PK
-- 允许同一个 key 同时存在多条"当前值"，与表的语义正好相反。
-- 改成：代理主键 + 部分唯一索引，把"每个 key 只有一个当前值"变成数据库不变量。
--
-- use_count / last_used_at 是必需的：长期事实按 last_used_at 衰减，
-- 不按 event_time（事实本身不会"变老"，衰减的是"近期没被需要"）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS facts (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       TEXT        NOT NULL,
    key           TEXT        NOT NULL CHECK (key ~ '^[a-z0-9_.-]{1,64}$'),
    value         JSONB       NOT NULL,
    confidence    REAL        NOT NULL DEFAULT 0.6 CHECK (confidence BETWEEN 0 AND 1),
    importance    REAL        NOT NULL DEFAULT 0.6 CHECK (importance BETWEEN 0 AND 1),
    source        TEXT        NOT NULL
                              CHECK (source IN ('conversation','tool','human','import')),
    use_count     INT         NOT NULL DEFAULT 0 CHECK (use_count >= 0),
    last_used_at  TIMESTAMPTZ,
    valid_from    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),  -- 不是 now()
    valid_until   TIMESTAMPTZ,                                     -- NULL = 当前值
    superseded_by UUID        REFERENCES facts(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 核心不变量：每个 (user_id, key) 最多一条当前值
CREATE UNIQUE INDEX IF NOT EXISTS uq_facts_live
    ON facts (user_id, key)
    WHERE valid_until IS NULL;

CREATE INDEX IF NOT EXISTS idx_facts_history
    ON facts (user_id, key, valid_from DESC);

-- ---------------------------------------------------------------------------
-- 4. 冲突待裁决（defer 动作的落地形式）
--
-- "疑似矛盾"既不合并也不丢弃：新行以 review_state='pending' 落库（默认不召回），
-- 同时在旧行与新行之间建一条 open 的边。裁决之后走 resolve_conflict()：
--   superseded —— 新行胜，旧行闭合（取代而非删除）
--   kept_old   —— 旧行胜，新行标 rejected（仍留在表里可追溯）
--   merged     —— 两条合并，旧行闭合、新行存活
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_conflicts (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      TEXT        NOT NULL,
    existing_id  UUID        NOT NULL REFERENCES episodes(id),
    incoming_id  UUID        NOT NULL REFERENCES episodes(id),
    reason       TEXT,
    signal       JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- LLM 给的依据，便于回溯
    status       TEXT        NOT NULL DEFAULT 'open'
                             CHECK (status IN ('open','resolved','dismissed')),
    resolution   TEXT        CHECK (resolution IN ('superseded','kept_old','merged')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at  TIMESTAMPTZ,
    resolved_by  TEXT,
    CONSTRAINT conflict_resolved_has_resolution
        CHECK (status <> 'resolved' OR resolution IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_conflict_open
    ON memory_conflicts (existing_id, incoming_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_conflict_open_user
    ON memory_conflicts (user_id, created_at DESC) WHERE status = 'open';

COMMENT ON TABLE memory_conflicts IS
'defer 动作落的边：疑似矛盾先挂起，等规则或人工裁决；不合并、不丢弃。';

-- ---------------------------------------------------------------------------
-- 5. 写入队列 memory_jobs —— 用 PG 自己当 broker，不引 Kafka
--
-- SKIP LOCKED 队列的三个必备件，缺一个就会静默丢数据：
--   a) status + locked_at   —— worker 崩了能被回收（否则任务永久卡在 processing）
--   b) attempts/max_attempts + dead —— 失败有上限，进死信而不是无限重试
--   c) dedup_key 部分唯一   —— 在途任务去重，同一片段不并发处理两次
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_jobs (
    id            BIGSERIAL PRIMARY KEY,
    user_id       TEXT        NOT NULL,
    job_type      TEXT        NOT NULL DEFAULT 'consolidate'
                              CHECK (job_type IN ('consolidate','distill','reembed','purge')),
    payload       JSONB       NOT NULL,
    dedup_key     TEXT,
    status        TEXT        NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','processing','done','dead')),
    attempts      INT         NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts  INT         NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    run_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at     TIMESTAMPTZ,
    locked_by     TEXT,
    last_error    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_inflight
    ON memory_jobs (dedup_key)
    WHERE dedup_key IS NOT NULL AND status IN ('pending','processing');

CREATE INDEX IF NOT EXISTS idx_jobs_claim
    ON memory_jobs (run_at, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_jobs_lease
    ON memory_jobs (locked_at) WHERE status = 'processing';

COMMENT ON COLUMN memory_jobs.dedup_key IS '在途去重键，如 f"{user_id}:{source}:{source_ref}"';

CREATE OR REPLACE VIEW memory_jobs_dead AS
SELECT id, user_id, job_type, attempts, max_attempts, last_error, updated_at, payload
FROM memory_jobs
WHERE status = 'dead';

-- ---------------------------------------------------------------------------
-- 6. 延迟外键
--
-- superseded_by 指向"新行"。facts 有部分唯一索引，必须先让旧行退出"当前"，
-- 再插新行 —— 此时 superseded_by 指向还没插入的行，所以两个外键都改为延迟检查。
-- 好处：写入顺序不再有隐含依赖，骨架不用小心排语句。
-- 代价：悬空引用只在 COMMIT 报错 —— 所以"预生成 id 并在两处复用"是硬要求。
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    ALTER TABLE episodes DROP CONSTRAINT IF EXISTS episodes_superseded_by_fkey;
    ALTER TABLE episodes ADD CONSTRAINT episodes_superseded_by_fkey
        FOREIGN KEY (superseded_by) REFERENCES episodes(id) DEFERRABLE INITIALLY DEFERRED;

    ALTER TABLE facts DROP CONSTRAINT IF EXISTS facts_superseded_by_fkey;
    ALTER TABLE facts ADD CONSTRAINT facts_superseded_by_fkey
        FOREIGN KEY (superseded_by) REFERENCES facts(id) DEFERRABLE INITIALLY DEFERRED;
END $$;

-- ---------------------------------------------------------------------------
-- 7. 一句话校验
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v text;
    d int;
BEGIN
    SELECT extversion INTO v FROM pg_extension WHERE extname = 'vector';
    IF v IS NULL THEN
        RAISE EXCEPTION 'pgvector 未安装';
    END IF;

    SELECT atttypmod INTO d
    FROM pg_attribute
    WHERE attrelid = 'episodes'::regclass AND attname = 'embedding';

    RAISE NOTICE 'pgvector %, episodes.embedding 维度 typmod=%', v, d;
    RAISE NOTICE 'schema ok：episodes / facts / memory_conflicts / memory_jobs';
END $$;
