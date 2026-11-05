-- =============================================================================
-- 可执行的语义自检（不是语法检查，是行为检查）
--   psql "$PG_URL" -v ON_ERROR_STOP=1 -f sql/99_selftest.sql
--
-- 覆盖那些"写错了也不报错、只是慢慢变脏"的地方：
--   失效链、事实唯一性、外键延迟、SKIP LOCKED 领取、租约回收、死信、
--   RRF 双路贡献、衰减下限、未来时间不被放大、失效行不再召回。
--
-- 全部是断言，不是"看着像对"。结束后自动清理（user_id 前缀 selftest:）。
-- =============================================================================
\set ON_ERROR_STOP on
\pset pager off

BEGIN;

-- ---------------------------------------------------------------------------
-- 工具：造一条 one-hot 向量（维度跟随 schema，不写死）
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    d int;
BEGIN
    SELECT atttypmod INTO d
    FROM pg_attribute
    WHERE attrelid = 'episodes'::regclass AND attname = 'embedding';
    IF d IS NULL OR d <= 0 THEN
        RAISE EXCEPTION '读不到 episodes.embedding 的维度（typmod=%）', d;
    END IF;
    PERFORM set_config('mem.selftest_dim', d::text, true);
    RAISE NOTICE '[setup] embedding 维度 = %', d;
END $$;

CREATE OR REPLACE FUNCTION pg_temp.onehot(pos int) RETURNS vector AS $fn$
    SELECT ('[' || string_agg(CASE WHEN i = pos THEN '1' ELSE '0' END, ',' ORDER BY i) || ']')::vector
    FROM generate_series(0, current_setting('mem.selftest_dim')::int - 1) i
$fn$ LANGUAGE sql STABLE;

-- 清理历史残留
DELETE FROM episodes    WHERE user_id LIKE 'selftest:%';
DELETE FROM facts       WHERE user_id LIKE 'selftest:%';
DELETE FROM memory_jobs WHERE user_id LIKE 'selftest:%';

-- ---------------------------------------------------------------------------
-- [1] 失效链：旧行不被删，只是被打上 valid_until + superseded_by
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    old_id uuid;
    new_id uuid;
    live   int;
    u      text := 'selftest:supersede';
BEGIN
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model,
                          event_time, source, confidence)
    VALUES (u, '用户时区为 Asia/Tokyo', '时区 Tokyo', pg_temp.onehot(0), 'selftest',
            now() - interval '10 days', 'conversation', 0.8)
    RETURNING id INTO old_id;

    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model,
                          event_time, source, confidence)
    VALUES (u, '用户时区为 Asia/Shanghai', '时区 Shanghai', pg_temp.onehot(1), 'selftest',
            now(), 'conversation', 0.9)
    RETURNING id INTO new_id;

    UPDATE episodes SET valid_until = now(), superseded_by = new_id
    WHERE id = old_id AND valid_until IS NULL;

    IF NOT FOUND THEN
        RAISE EXCEPTION '[1] 失效语句没有命中旧行';
    END IF;

    IF (SELECT superseded_by FROM episodes WHERE id = old_id) IS DISTINCT FROM new_id THEN
        RAISE EXCEPTION '[1] superseded_by 没有指向新行';
    END IF;

    IF (SELECT valid_until FROM episodes WHERE id = old_id) IS NULL THEN
        RAISE EXCEPTION '[1] valid_until 没有被闭合（NULL 表示仍然有效）';
    END IF;

    IF (SELECT count(*) FROM episodes WHERE user_id = u) <> 2 THEN
        RAISE EXCEPTION '[1] 历史行丢失了（失效不是删除）';
    END IF;

    SELECT count(*) INTO live FROM episodes WHERE user_id = u AND valid_until IS NULL;
    IF live <> 1 THEN
        RAISE EXCEPTION '[1] 当前有效行应为 1，实际 %', live;
    END IF;

    -- 并发兜底：第二次失效必须命中 0 行
    UPDATE episodes SET valid_until = now(), superseded_by = new_id
    WHERE id = old_id AND valid_until IS NULL;
    IF FOUND THEN
        RAISE EXCEPTION '[1] 重复失效没有被 valid_until IS NULL 挡住（并发兜底失效）';
    END IF;

    RAISE NOTICE '[1] PASS 失效链：历史保留 / 当前唯一 / 重复失效被挡';
END $$;

-- ---------------------------------------------------------------------------
-- [2] 事实表：每个 (user_id, key) 只能有一条当前值
--     注意：新行 id 必须**预先**生成并在两处复用。写一个随机 uuid 上去就完事，
--     悬空的 superseded_by 只会在 COMMIT 时才被延迟外键抓到 —— 清理语句一跑，
--     这条错误就消失了，测试会假通过。这是延迟外键的代价，得记着。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    u text := 'selftest:facts';
    v_new uuid := gen_random_uuid();
    blocked boolean := false;
BEGIN
    INSERT INTO facts (user_id, key, value, confidence, source)
    VALUES (u, 'timezone', '"Asia/Tokyo"'::jsonb, 0.8, 'conversation');

    -- 再插一条当前值必须被部分唯一索引挡住
    BEGIN
        INSERT INTO facts (user_id, key, value, confidence, source)
        VALUES (u, 'timezone', '"Asia/Shanghai"'::jsonb, 0.9, 'conversation');
    EXCEPTION WHEN unique_violation THEN
        blocked := true;
    END;

    IF NOT blocked THEN
        RAISE EXCEPTION '[2] 同一 key 出现了两条当前值 —— 部分唯一索引没生效';
    END IF;

    -- 正确姿势：先失效旧行（指向即将插入的新行），再插同一个 id 的新行。
    -- 这个顺序只有在 superseded_by 是延迟外键时才成立，顺带验证了修正 #7。
    UPDATE facts
    SET valid_until = now(), superseded_by = v_new
    WHERE user_id = u AND key = 'timezone' AND valid_until IS NULL;

    INSERT INTO facts (id, user_id, key, value, confidence, source)
    VALUES (v_new, u, 'timezone', '"Asia/Shanghai"'::jsonb, 0.9, 'human');

    IF (SELECT count(*) FROM facts
        WHERE user_id = u AND key = 'timezone' AND valid_until IS NULL) <> 1 THEN
        RAISE EXCEPTION '[2] 失效 + 插入之后当前值不唯一';
    END IF;

    IF (SELECT count(*) FROM facts WHERE user_id = u AND key = 'timezone') <> 2 THEN
        RAISE EXCEPTION '[2] 历史事实没有保留';
    END IF;

    -- 旧行确实指向了新行，链没断
    IF NOT EXISTS (
        SELECT 1 FROM facts old JOIN facts new ON new.id = old.superseded_by
        WHERE old.user_id = u AND old.key = 'timezone' AND new.value = '"Asia/Shanghai"'::jsonb
    ) THEN
        RAISE EXCEPTION '[2] 旧行的 superseded_by 没有指向新行（延迟外键会等到 COMMIT 才报，别让它溜过去）';
    END IF;

    RAISE NOTICE '[2] PASS 事实表：当前值唯一 / 历史保留 / 失效链闭合';
END $$;

-- ---------------------------------------------------------------------------
-- [3] 外键延迟：facts 必须"先失效、后插入"，这只有在 superseded_by 延迟校验时成立
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    bad text;
BEGIN
    SELECT conname INTO bad
    FROM pg_constraint
    WHERE conrelid IN ('episodes'::regclass, 'facts'::regclass)
      AND contype = 'f'
      AND NOT condeferrable;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION '[3] 外键 % 不是 DEFERRABLE，facts 的写序会失效', bad;
    END IF;
    RAISE NOTICE '[3] PASS superseded_by 外键均为延迟校验';
END $$;

-- ---------------------------------------------------------------------------
-- [4] 队列：在途去重 / SKIP LOCKED 领取 / 租约回收 / 死信
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    u text := 'selftest:queue';
    n int;
    claimed record;
    claimed_n int := 0;
    statuses text;
BEGIN
    INSERT INTO memory_jobs (user_id, job_type, payload, dedup_key)
    SELECT u, 'consolidate', jsonb_build_object('i', i), u || ':dedup:' || i
    FROM generate_series(1, 3) i;

    -- 在途去重：同一个 dedup_key 再入队应被吞掉
    INSERT INTO memory_jobs (user_id, job_type, payload, dedup_key)
    VALUES (u, 'consolidate', '{}'::jsonb, u || ':dedup:1')
    ON CONFLICT DO NOTHING;

    SELECT count(*) INTO n FROM memory_jobs WHERE user_id = u;
    IF n <> 3 THEN
        RAISE EXCEPTION '[4] 在途去重失效，期望 3 条实际 %', n;
    END IF;

    -- 领取 2 条
    FOR claimed IN
        WITH next_batch AS (
            SELECT id FROM memory_jobs
            WHERE user_id = u AND status = 'pending' AND run_at <= now()
            ORDER BY run_at, id
            FOR UPDATE SKIP LOCKED
            LIMIT 2
        )
        UPDATE memory_jobs j
        SET status = 'processing', locked_at = now(), locked_by = 'selftest-w1',
            attempts = j.attempts + 1, updated_at = now()
        FROM next_batch b
        WHERE j.id = b.id
        RETURNING j.id, j.attempts
    LOOP
        claimed_n := claimed_n + 1;
        IF claimed.attempts <> 1 THEN
            RAISE EXCEPTION '[4] 领取时 attempts 应该 +1';
        END IF;
    END LOOP;

    IF claimed_n <> 2 THEN
        RAISE EXCEPTION '[4] 期望领取 2 条，实际 %', claimed_n;
    END IF;

    SELECT string_agg(DISTINCT status, ',') INTO statuses FROM memory_jobs WHERE user_id = u;
    IF statuses <> 'pending,processing' THEN
        RAISE EXCEPTION '[4] 领取后状态分布异常: %', statuses;
    END IF;

    -- 租约过期回收：worker 崩掉的场景
    UPDATE memory_jobs SET locked_at = now() - interval '1 hour'
    WHERE user_id = u AND status = 'processing';

    PERFORM reap_stuck_jobs(300);

    SELECT count(*) INTO n FROM memory_jobs WHERE user_id = u AND status = 'pending';
    IF n <> 3 THEN
        RAISE EXCEPTION '[4] 回收后应全部回到 pending，实际 %', n;
    END IF;

    -- 死信：把一条的 attempts 打到上限再回收
    UPDATE memory_jobs
    SET status = 'processing', locked_at = now() - interval '1 hour', attempts = max_attempts
    WHERE id = (SELECT min(id) FROM memory_jobs WHERE user_id = u);

    PERFORM reap_stuck_jobs(300);

    SELECT count(*) INTO n FROM memory_jobs WHERE user_id = u AND status = 'dead';
    IF n <> 1 THEN
        RAISE EXCEPTION '[4] 用尽重试的任务应进死信，实际 dead=%', n;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM memory_jobs_dead WHERE user_id = u) THEN
        RAISE EXCEPTION '[4] 死信视图看不到这条任务';
    END IF;

    RAISE NOTICE '[4] PASS 队列：去重 / 领取 / 租约回收 / 死信';
END $$;

-- ---------------------------------------------------------------------------
-- [5] 检索：向量路与关键词路必须各自有独占命中
--     （原草稿的 kw 分支缺外层 ORDER BY + 用了全长 similarity，会静默返回 0 行）
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    u     text := 'selftest:recall';
    qv    vector := pg_temp.onehot(0);
    q     text := '时区';
    legacy int;
    fixed  int;
    total  int;
    vec_only int;
    kw_only  int;
    ancient  record;
    expected_decay float8;
    actual_decay   float8;
BEGIN
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source)
    VALUES
      (u, '用户喜欢用深色主题的编辑器', '深色主题', pg_temp.onehot(0), 'selftest', now(), 'conversation'),
      (u, '用户的时区设置需要每年复查一次', '时区复查', pg_temp.onehot(7), 'selftest', now() - interval '1 day', 'conversation'),
      (u, '用户最早的时区是 UTC+9', '旧时区', pg_temp.onehot(0), 'selftest', now() - interval '5 years', 'conversation');

    -- 5.1 原稿写法 vs 现写法：默认阈值下（不调任何 GUC）中文短查询的差别
    --     原稿 content % q  —— similarity 是全长对称比较，且中文短查询的三元组
    --                          带空格填充，实际恒为 0 行
    --     现写法              —— 精确子串 OR word_similarity
    SELECT count(*) INTO legacy FROM episodes
    WHERE user_id = u AND valid_until IS NULL AND content % q;
    SELECT count(*) INTO fixed FROM episodes
    WHERE user_id = u AND valid_until IS NULL
      AND (strpos(content, q) > 0 OR q <% content);

    IF fixed < 1 THEN
        RAISE EXCEPTION '[5] 关键词路一条都没召回（legacy=%, fixed=%）', legacy, fixed;
    END IF;
    RAISE NOTICE '[5.1] 关键词路（默认阈值）：content %% q = % 条；现写法 = % 条', legacy, fixed;

    -- 关键：vec_k 必须小于总行数，否则每条都会被向量路覆盖，
    -- "关键词路独占命中" 就永远为 0，测不出单路假象。
    SELECT count(*) INTO total FROM recall_episodes(
        qv, u, q, p_vec_k => 2, p_kw_k => 10, p_limit => 10);
    IF total < 2 THEN
        RAISE EXCEPTION '[5] 混合召回只返回 % 条，期望 >= 2', total;
    END IF;

    SELECT count(*) INTO vec_only FROM recall_episodes(
        qv, u, q, p_vec_k => 2, p_kw_k => 10, p_limit => 10)
    WHERE rk_v IS NOT NULL AND rk_k IS NULL;

    SELECT count(*) INTO kw_only FROM recall_episodes(
        qv, u, q, p_vec_k => 2, p_kw_k => 10, p_limit => 10)
    WHERE rk_k IS NOT NULL AND rk_v IS NULL;

    IF vec_only < 1 THEN
        RAISE EXCEPTION '[5] 向量路没有独占命中，RRF 实际只在跑单路';
    END IF;
    IF kw_only < 1 THEN
        RAISE EXCEPTION '[5] 关键词路没有独占命中，RRF 实际只在跑单路';
    END IF;

    -- 高斯衰减：把分数还原成衰减系数，和公式逐项对一遍
    --   score = rrf × 高斯(age) × (1 + freq·log(1+use_count)) × (1 - w + w·importance)
    -- 这里把 importance_weight 显式设为 0，隔离出纯衰减项再比对。
    -- 老记忆不会被"归零"：5 年后权重仍在 0.5 附近，这正是免罚期+长尺度的意义。
    SELECT * INTO ancient FROM recall_episodes(
        qv, u, q, p_vec_k => 2, p_kw_k => 10, p_limit => 10,
        p_importance_weight => 0.0) r
    WHERE r.event_time < now() - interval '1 year'
    LIMIT 1;

    IF ancient.id IS NULL THEN
        RAISE EXCEPTION '[5] 五年前的记忆完全没被召回';
    END IF;

    expected_decay := power(0.5::float8,
        power(GREATEST(ancient.age_days - 180.0, 0.0) / 1825.0, 2));
    actual_decay := ancient.score / NULLIF(ancient.rrf_score, 0);

    IF abs(actual_decay - expected_decay) > 1e-6 THEN
        RAISE EXCEPTION '[5] 高斯衰减与公式不符：实测 % 期望 %（age=%d）',
            actual_decay, expected_decay, ancient.age_days;
    END IF;
    IF actual_decay < 0.3 THEN
        RAISE EXCEPTION '[5] 一年前的记忆权重被压到 % —— 免罚期/尺度没生效', actual_decay;
    END IF;

    RAISE NOTICE '[5] PASS 检索：向量独占 % 条 / 关键词独占 % 条 / 老记忆 age=%d天 decay=%',
        vec_only, kw_only, ancient.age_days, actual_decay;
END $$;

-- ---------------------------------------------------------------------------
-- [6] 未来事件不被时间项放大
--     （原草稿 now() - event_time 为负，指数衰减变成指数放大）
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    u text := 'selftest:future';
    r record;
BEGIN
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source)
    VALUES (u, '用户下个月要去成都出差', '成都出差', pg_temp.onehot(0), 'selftest',
            now() + interval '30 days', 'conversation');

    SELECT * INTO r FROM recall_episodes(
        pg_temp.onehot(0), u, '成都出差', p_vec_k => 10, p_kw_k => 10, p_limit => 5) LIMIT 1;

    IF r.id IS NULL THEN
        RAISE EXCEPTION '[6] 未来的日程没有被召回';
    END IF;
    IF r.age_days <> 0 THEN
        RAISE EXCEPTION '[6] 未来事件的 age_days 应该钳到 0，实际 %', r.age_days;
    END IF;
    IF r.score > r.rrf_score + 1e-9 THEN
        RAISE EXCEPTION '[6] 未来事件被衰减项放大了：score=% rrf=%', r.score, r.rrf_score;
    END IF;

    RAISE NOTICE '[6] PASS 未来事件不被放大（age_days=%）', r.age_days;
END $$;

-- ---------------------------------------------------------------------------
-- [7] 两条失效路径都不能再被召回
--     (a) 有接替者：valid_until + superseded_by 一起写（UPDATE 动作）
--     (b) 无接替者：只写 valid_until（DELETE 动作）—— 这条曾经是 bug：
--         判据还停在 superseded_by IS NULL 时，DELETE 掉的行会继续被召回。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    u text := 'selftest:recall';
    v_new uuid;
    hits int;
BEGIN
    -- (a) 有接替者的失效
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source)
    VALUES (u, '用户偏好浅色主题的编辑器', '浅色主题', pg_temp.onehot(0), 'selftest', now(), 'conversation')
    RETURNING id INTO v_new;

    UPDATE episodes
    SET valid_until = now(), superseded_by = v_new
    WHERE user_id = u AND content LIKE '%深色主题%' AND valid_until IS NULL;

    IF NOT FOUND THEN
        RAISE EXCEPTION '[7] 没有找到要失效的行';
    END IF;

    SELECT count(*) INTO hits FROM recall_episodes(
        pg_temp.onehot(0), u, '深色主题', p_vec_k => 2, p_kw_k => 10, p_limit => 10)
    WHERE snippet LIKE '%深色主题%';
    IF hits <> 0 THEN
        RAISE EXCEPTION '[7a] 已失效（有接替者）的记忆仍被召回（% 条）', hits;
    END IF;

    -- (b) 无接替者的失效：只写 valid_until，superseded_by 保持 NULL
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source)
    VALUES (u, '用户的工位在 3 楼靠窗', '工位', pg_temp.onehot(3), 'selftest', now(), 'conversation');

    UPDATE episodes
    SET valid_until = now(), review_state = 'rejected'
    WHERE user_id = u AND content LIKE '%工位%' AND valid_until IS NULL;

    IF NOT FOUND THEN
        RAISE EXCEPTION '[7] 没有找到要作废的行';
    END IF;

    IF EXISTS (
        SELECT 1 FROM episodes
        WHERE user_id = u AND content LIKE '%工位%' AND valid_until IS NULL
    ) THEN
        RAISE EXCEPTION '[7b] 只写 valid_until 的作废没有生效';
    END IF;

    SELECT count(*) INTO hits FROM recall_episodes(
        pg_temp.onehot(3), u, '工位', p_vec_k => 2, p_kw_k => 10, p_limit => 10)
    WHERE snippet LIKE '%工位%';
    IF hits <> 0 THEN
        RAISE EXCEPTION '[7b] 已作废（无接替者）的记忆仍被召回（% 条）', hits;
    END IF;

    RAISE NOTICE '[7] PASS 两条失效路径都不再参与召回（有/无接替者）';
END $$;

-- ---------------------------------------------------------------------------
-- [8] 分类型 TTL：写入就带过期时间，查询层过滤（不做定时清理作业）
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    u text := 'selftest:ttl';
    raw_row   record;
    sum_row   record;
    lt_row    record;
    expired   record;
    hits int;
BEGIN
    INSERT INTO episodes (user_id, kind, content, summary, embedding, embedding_model, event_time, source)
    VALUES (u, 'raw_turn', '用户问了一句日志在哪', '日志位置', pg_temp.onehot(0), 'selftest', now(), 'conversation')
    RETURNING id, expires_at INTO raw_row;

    INSERT INTO episodes (user_id, kind, content, summary, embedding, embedding_model, event_time, source)
    VALUES (u, 'summary', '用户偏好汇总：深色主题 + PowerShell', '偏好汇总', pg_temp.onehot(1), 'selftest', now(), 'conversation')
    RETURNING id, expires_at INTO sum_row;

    INSERT INTO episodes (user_id, kind, content, summary, embedding, embedding_model, event_time, source)
    VALUES (u, 'longterm', '用户的时区长期为 Asia/Shanghai', '时区长期', pg_temp.onehot(2), 'selftest', now(), 'conversation')
    RETURNING id, expires_at INTO lt_row;

    -- 触发器必须按 kind 把 expires_at 算对
    IF raw_row.expires_at IS NULL OR
       abs(EXTRACT(EPOCH FROM (raw_row.expires_at - now())) - 30 * 86400) > 120 THEN
        RAISE EXCEPTION '[8] raw_turn 的 TTL 不是 30 天：%', raw_row.expires_at;
    END IF;
    IF sum_row.expires_at IS NULL OR
       abs(EXTRACT(EPOCH FROM (sum_row.expires_at - now())) - 3650 * 86400) > 120 THEN
        RAISE EXCEPTION '[8] summary 的 TTL 不是 3650 天：%', sum_row.expires_at;
    END IF;
    IF lt_row.expires_at IS NOT NULL THEN
        RAISE EXCEPTION '[8] longterm 不该按时间过期，却拿到 %', lt_row.expires_at;
    END IF;

    -- 直接把一条的 expires_at 拨到过去，模拟"已过期但没人清理"
    UPDATE episodes SET expires_at = now() - interval '1 day'
    WHERE user_id = u AND kind = 'raw_turn'
    RETURNING id INTO expired;

    SELECT count(*) INTO hits FROM recall_episodes(
        pg_temp.onehot(0), u, '日志', p_vec_k => 10, p_kw_k => 10, p_limit => 10)
    WHERE id = (SELECT id FROM episodes WHERE kind = 'raw_turn' AND user_id = u);
    IF hits <> 0 THEN
        RAISE EXCEPTION '[8] 已过期（expires_at 已过）的记忆仍被召回';
    END IF;

    -- 未过期的两条还在
    SELECT count(*) INTO hits FROM recall_episodes(
        pg_temp.onehot(1), u, '偏好', p_vec_k => 10, p_kw_k => 10, p_limit => 10);
    IF hits < 1 THEN
        RAISE EXCEPTION '[8] 未过期的记忆被 TTL 误伤';
    END IF;

    RAISE NOTICE '[8] PASS TTL：触发器按 kind 计算 / 过期行查询层过滤 / 长期层不过期';
END $$;

-- ---------------------------------------------------------------------------
-- [9] merge：不新建行，只抬治理信号；目标失效时 rowcount=0
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    u text := 'selftest:merge';
    v_id uuid;
    before_conf real;
    after_conf real;
    n_before int;
    n_after int;
    affected int;
BEGIN
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source, confidence, importance)
    VALUES (u, '用户偏好深色主题', '深色主题', pg_temp.onehot(0), 'selftest', now(), 'conversation', 0.60, 0.50)
    RETURNING id, confidence INTO v_id, before_conf;

    SELECT count(*) INTO n_before FROM episodes WHERE user_id = u;

    -- 与 store.py 里 SQL_REINFORCE 等价：只抬信号，不新建
    UPDATE episodes
    SET confidence = LEAST(1.0, confidence + 0.05),
        importance = LEAST(1.0, importance + 0.02),
        use_count  = use_count + 1,
        last_used_at = now()
    WHERE id = v_id AND valid_until IS NULL;

    SELECT count(*) INTO n_after FROM episodes WHERE user_id = u;
    SELECT confidence INTO after_conf FROM episodes WHERE id = v_id;

    IF n_after <> n_before THEN
        RAISE EXCEPTION '[9] merge 新建了行（% -> %）', n_before, n_after;
    END IF;
    IF after_conf <= before_conf THEN
        RAISE EXCEPTION '[9] merge 没有抬高置信度（% -> %）', before_conf, after_conf;
    END IF;

    -- 已闭合的行不许再被 merge（并发兜底）
    UPDATE episodes SET valid_until = now() WHERE id = v_id;
    UPDATE episodes SET confidence = LEAST(1.0, confidence + 0.05)
    WHERE id = v_id AND valid_until IS NULL;
    GET DIAGNOSTICS affected = ROW_COUNT;
    IF affected <> 0 THEN
        RAISE EXCEPTION '[9] merge 改到了已失效的行';
    END IF;

    RAISE NOTICE '[9] PASS merge：不新建行 / 置信度 % -> % / 失效行不可改',
        before_conf, after_conf;
END $$;

-- ---------------------------------------------------------------------------
-- [10] defer：落库但待裁决，裁决前不召回；resolve_conflict 后才取代
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    u text := 'selftest:defer';
    old_id uuid;
    new_id uuid;
    conflict_id uuid;
    hits int;
    resolved record;
    old_row record;
BEGIN
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source)
    VALUES (u, '用户的时区是 Asia/Tokyo', 'Tokyo', pg_temp.onehot(0), 'selftest', now(), 'conversation')
    RETURNING id INTO old_id;

    -- defer 的第一步：新行以 pending 落库
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source, review_state)
    VALUES (u, '用户的时区是 Asia/Shanghai', 'Shanghai', pg_temp.onehot(0), 'selftest', now(), 'conversation', 'pending')
    RETURNING id INTO new_id;

    -- 第二步：建 open 的冲突边
    INSERT INTO memory_conflicts (user_id, existing_id, incoming_id, reason, signal)
    VALUES (u, old_id, new_id, 'selftest', '{"src":"99_selftest"}'::jsonb)
    RETURNING id INTO conflict_id;

    -- 裁决前：新行不该被召回（旧行仍代表"当前为真"）
    SELECT count(*) INTO hits FROM recall_episodes(
        pg_temp.onehot(0), u, '时区', p_vec_k => 10, p_kw_k => 10, p_limit => 10)
    WHERE id = new_id;
    IF hits <> 0 THEN
        RAISE EXCEPTION '[10] 待裁决的行被召回了 —— 新值在裁决前抢走了旧值的位置';
    END IF;

    -- 必须先能在 pending_conflicts 里被看见（否则永远没人裁决）
    IF NOT EXISTS (SELECT 1 FROM memory_conflicts WHERE id = conflict_id AND status = 'open') THEN
        RAISE EXCEPTION '[10] 冲突边没有建出来';
    END IF;

    -- 裁决为 superseded：旧行闭合、血缘指向新行、新行转正
    SELECT * INTO resolved FROM resolve_conflict(conflict_id, 'superseded', 'selftest');
    IF resolved.resolution <> 'superseded' THEN
        RAISE EXCEPTION '[10] resolve_conflict 返回异常: %', resolved.resolution;
    END IF;

    SELECT id, valid_until, superseded_by INTO old_row FROM episodes WHERE id = old_id;
    IF old_row.valid_until IS NULL THEN
        RAISE EXCEPTION '[10] 裁决后旧行没有闭合';
    END IF;
    IF old_row.superseded_by IS DISTINCT FROM new_id THEN
        RAISE EXCEPTION '[10] 裁决后血缘没有指向新行';
    END IF;
    IF (SELECT review_state FROM episodes WHERE id = new_id) <> 'none' THEN
        RAISE EXCEPTION '[10] 裁决后新行仍是 pending';
    END IF;

    -- 现在新行应该被召回，旧行不该
    SELECT count(*) INTO hits FROM recall_episodes(
        pg_temp.onehot(0), u, '时区', p_vec_k => 10, p_kw_k => 10, p_limit => 10)
    WHERE id = new_id;
    IF hits <> 1 THEN
        RAISE EXCEPTION '[10] 裁决后新行没有转正召回';
    END IF;
    SELECT count(*) INTO hits FROM recall_episodes(
        pg_temp.onehot(0), u, '时区', p_vec_k => 10, p_kw_k => 10, p_limit => 10)
    WHERE id = old_id;
    IF hits <> 0 THEN
        RAISE EXCEPTION '[10] 裁决后旧行仍在召回';
    END IF;

    -- 重复裁决必须报错，不能静默再改一次
    BEGIN
        PERFORM resolve_conflict(conflict_id, 'kept_old', 'selftest');
        RAISE EXCEPTION '[10] 重复裁决没有被拒绝';
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM LIKE '%重复裁决没有被拒绝%' THEN RAISE; END IF;
        -- 预期：'冲突 ... 已经处理过'
    END;

    RAISE NOTICE '[10] PASS defer：待裁决不召回 / 裁决后取代 / 血缘完整 / 重复裁决被拒';
END $$;

-- ---------------------------------------------------------------------------
-- [11] 高斯衰减曲线：免罚期内几乎不衰减，offset+scale 处半衰
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    d_180  float8;
    d_365  float8;
    d_730  float8;
    d_2005 float8;
BEGIN
    d_180  := power(0.5::float8, power(GREATEST(180.0  - 180.0, 0.0) / 1825.0, 2));
    d_365  := power(0.5::float8, power(GREATEST(365.0  - 180.0, 0.0) / 1825.0, 2));
    d_730  := power(0.5::float8, power(GREATEST(730.0  - 180.0, 0.0) / 1825.0, 2));
    d_2005 := power(0.5::float8, power(GREATEST(2005.0 - 180.0, 0.0) / 1825.0, 2));

    IF abs(d_180 - 1.0) > 1e-9 THEN
        RAISE EXCEPTION '[11] 免罚期内不该衰减，得到 %', d_180;
    END IF;
    IF d_365 < 0.99 THEN
        RAISE EXCEPTION '[11] 一年前的权重被压到 %（应接近 1）', d_365;
    END IF;
    IF abs(d_2005 - 0.5) > 1e-6 THEN
        RAISE EXCEPTION '[11] offset+scale 处应为 0.5，得到 %', d_2005;
    END IF;

    RAISE NOTICE '[11] PASS 高斯曲线：180d=% 365d=% 730d=% 2005d=%',
        d_180, d_365, d_730, d_2005;
END $$;

-- ---------------------------------------------------------------------------
-- [12] 剩下的两条裁决路径 + 软删除/恢复
--   kept_old —— 旧行胜：新行标 rejected 并闭合（留在表里可追溯）
--   merged   —— 合并：旧行闭合、新行转正并吸收两条的治理信号上限
--   forget/restore —— 软删除 + tombstone，可恢复
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    u text := 'selftest:resolve2';
    old1 uuid; new1 uuid; c1 uuid;
    old2 uuid; new2 uuid; c2 uuid;
    r record;
    hits int;
BEGIN
    -- ---- kept_old ----
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source, confidence)
    VALUES (u, '用户的时区是 Asia/Tokyo', 'Tokyo', pg_temp.onehot(0), 'selftest', now(), 'conversation', 0.9)
    RETURNING id INTO old1;
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source, review_state)
    VALUES (u, '用户的时区可能是 UTC+9', 'UTC+9', pg_temp.onehot(0), 'selftest', now(), 'conversation', 'pending')
    RETURNING id INTO new1;
    INSERT INTO memory_conflicts (user_id, existing_id, incoming_id, reason)
    VALUES (u, old1, new1, 'kept_old_case') RETURNING id INTO c1;

    SELECT * INTO r FROM resolve_conflict(c1, 'kept_old', 'selftest');
    IF (SELECT valid_until FROM episodes WHERE id = old1) IS NOT NULL THEN
        RAISE EXCEPTION '[12] kept_old 不该闭合旧行';
    END IF;
    IF (SELECT review_state FROM episodes WHERE id = new1) <> 'rejected' THEN
        RAISE EXCEPTION '[12] kept_old 没有把新行标成 rejected';
    END IF;
    IF (SELECT valid_until FROM episodes WHERE id = new1) IS NULL THEN
        RAISE EXCEPTION '[12] kept_old 的新行仍然有效（应闭合，避免它继续被召回）';
    END IF;
    -- 新行仍然留在表里（可追溯），只是不参与召回
    IF NOT EXISTS (SELECT 1 FROM episodes WHERE id = new1) THEN
        RAISE EXCEPTION '[12] kept_old 把新行物理删掉了';
    END IF;

    -- ---- merged ----
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source, confidence, importance)
    VALUES (u, '用户偏好深色主题', '深色', pg_temp.onehot(1), 'selftest', now(), 'conversation', 0.70, 0.90)
    RETURNING id INTO old2;
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source, review_state, confidence, importance)
    VALUES (u, '用户偏好深色主题（夜间也保持）', '深色 夜间', pg_temp.onehot(1), 'selftest', now(), 'conversation', 'pending', 0.80, 0.40)
    RETURNING id INTO new2;
    INSERT INTO memory_conflicts (user_id, existing_id, incoming_id, reason)
    VALUES (u, old2, new2, 'merged_case') RETURNING id INTO c2;

    SELECT * INTO r FROM resolve_conflict(c2, 'merged', 'selftest');
    IF (SELECT superseded_by FROM episodes WHERE id = old2) IS DISTINCT FROM new2 THEN
        RAISE EXCEPTION '[12] merged 没有把旧行的血缘指向新行';
    END IF;
    IF (SELECT review_state FROM episodes WHERE id = new2) <> 'none' THEN
        RAISE EXCEPTION '[12] merged 后新行仍是 pending';
    END IF;
    -- 合并要吸收两条的治理信号上限：confidence 取 max(0.70,0.80)，importance 取 max(0.90,0.40)
    IF (SELECT confidence FROM episodes WHERE id = new2) < 0.80 - 1e-6 THEN
        RAISE EXCEPTION '[12] merged 后新行置信度没有吸收旧值';
    END IF;
    IF (SELECT importance FROM episodes WHERE id = new2) < 0.90 - 1e-6 THEN
        RAISE EXCEPTION '[12] merged 后新行重要性没有吸收旧值';
    END IF;

    -- ---- 软删除 / 恢复 ----
    IF NOT forget_episode(new2, '用户要求忘掉') THEN
        RAISE EXCEPTION '[12] forget_episode 没有影响任何行';
    END IF;
    SELECT count(*) INTO hits FROM recall_episodes(
        pg_temp.onehot(1), u, '深色主题', p_vec_k => 10, p_kw_k => 10, p_limit => 10)
    WHERE id = new2;
    IF hits <> 0 THEN
        RAISE EXCEPTION '[12] 遗忘之后仍被召回';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM episodes WHERE id = new2) THEN
        RAISE EXCEPTION '[12] 遗忘做成了物理删除（应该留 tombstone）';
    END IF;
    IF position('遗忘' in COALESCE((SELECT summary FROM episodes WHERE id = new2), '')) = 0 THEN
        RAISE EXCEPTION '[12] 遗忘原因没有留在行上';
    END IF;

    IF NOT restore_episode(new2) THEN
        RAISE EXCEPTION '[12] restore_episode 没有恢复成功';
    END IF;
    SELECT count(*) INTO hits FROM recall_episodes(
        pg_temp.onehot(1), u, '深色主题', p_vec_k => 10, p_kw_k => 10, p_limit => 10)
    WHERE id = new2;
    IF hits <> 1 THEN
        RAISE EXCEPTION '[12] 恢复之后没有被召回';
    END IF;

    -- resolve_conflict_for：按 (user_id, existing_id) 找 open 的边并裁决
    -- 这条路径曾经因 OUT 参数与列名撞车而报 AmbiguousColumn，建函数时不报、
    -- 只在调用时炸 —— 所以必须在自检里真的调一次。
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source)
    VALUES (u, '用户偏好浅色背景', '浅色', pg_temp.onehot(2), 'selftest', now(), 'conversation')
    RETURNING id INTO old1;
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source, review_state)
    VALUES (u, '用户偏好浅色背景（白天）', '浅色 白天', pg_temp.onehot(2), 'selftest', now(), 'conversation', 'pending')
    RETURNING id INTO new1;
    INSERT INTO memory_conflicts (user_id, existing_id, incoming_id, reason)
    VALUES (u, old1, new1, 'by_existing') RETURNING id INTO c1;

    SELECT * INTO r FROM resolve_conflict_for(u, old1, 'superseded', 'selftest');
    IF r.resolution <> 'superseded' THEN
        RAISE EXCEPTION '[12] resolve_conflict_for 没有生效（%）', r.resolution;
    END IF;
    IF r.existing_id IS DISTINCT FROM old1 OR r.incoming_id IS DISTINCT FROM new1 THEN
        RAISE EXCEPTION '[12] resolve_conflict_for 返回的 id 不对';
    END IF;

    RAISE NOTICE '[12] PASS kept_old / merged / 软删除 / 恢复 / resolve_conflict_for';
END $$;

-- ---------------------------------------------------------------------------
-- [13] 使用反馈与生命周期刷新：不是主链路，但漏了会让衰减口径失效
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    u text := 'selftest:usage';
    v_id uuid;
    f_id uuid;
    aged_id uuid;
    before_use int;
    after_use int;
    after_ts timestamptz;
    refreshed bigint;
    lc text;
    purged int;
BEGIN
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source)
    VALUES (u, '用户的键盘是 HHKB', 'HHKB', pg_temp.onehot(0), 'selftest', now(), 'conversation')
    RETURNING id INTO v_id, before_use;

    SELECT use_count INTO before_use FROM episodes WHERE id = v_id;

    -- 等价于 bump_usage(uuid[])
    UPDATE episodes SET use_count = use_count + 1, last_used_at = now()
    WHERE id = ANY(ARRAY[v_id]) RETURNING use_count, last_used_at INTO after_use, after_ts;

    IF after_use <> before_use + 1 THEN
        RAISE EXCEPTION '[13] use_count 没有 +1（% -> %）', before_use, after_use;
    END IF;
    IF after_ts IS NULL THEN
        RAISE EXCEPTION '[13] last_used_at 没有写入 —— 事实/记忆的衰减口径会失效';
    END IF;

    -- 长期事实的 bump 口径（facts 按 last_used_at 衰减，所以这个字段是必需的）
    INSERT INTO facts (user_id, key, value, confidence, source)
    VALUES (u, 'keyboard', '"HHKB"', 0.9, 'human') RETURNING id INTO f_id;
    UPDATE facts SET use_count = use_count + 1, last_used_at = now()
    WHERE user_id = u AND valid_until IS NULL AND key = ANY(ARRAY['keyboard']);
    IF NOT FOUND THEN
        RAISE EXCEPTION '[13] facts 的 bump 没命中当前值';
    END IF;
    IF (SELECT last_used_at FROM facts WHERE id = f_id) IS NULL THEN
        RAISE EXCEPTION '[13] facts.last_used_at 没有写入';
    END IF;

    -- refresh_lifecycle：把一条 80 天前写入的 episode 刷成 faded
    INSERT INTO episodes (user_id, kind, content, summary, embedding, embedding_model,
                          event_time, ingested_at, source)
    VALUES (u, 'episode', '八十天前的一次评审', '老评审', pg_temp.onehot(2), 'selftest',
            now() - interval '80 days', now() - interval '80 days', 'conversation')
    RETURNING id INTO aged_id;

    SELECT lifecycle_state INTO lc FROM episodes WHERE id = aged_id;
    IF lc <> 'fresh' THEN
        RAISE EXCEPTION '[13] 未刷新前不该是 %', lc;
    END IF;

    SELECT updated INTO refreshed FROM refresh_lifecycle();

    SELECT lifecycle_state INTO lc FROM episodes WHERE id = aged_id;
    IF lc <> 'faded' THEN
        RAISE EXCEPTION '[13] refresh_lifecycle 之后应为 faded，实际 %', lc;
    END IF;

    -- 队列清理只删 done，不碰 dead（死信要留给人看）
    INSERT INTO memory_jobs (user_id, job_type, payload, status, finished_at)
    VALUES (u, 'consolidate', '{}'::jsonb, 'done', now() - interval '30 days'),
           (u, 'consolidate', '{}'::jsonb, 'dead', now() - interval '30 days');
    DELETE FROM memory_jobs
    WHERE user_id = u AND status = 'done' AND finished_at < now() - interval '7 days';
    GET DIAGNOSTICS purged = ROW_COUNT;
    IF purged <> 1 THEN
        RAISE EXCEPTION '[13] 清理应删 1 条 done，实际 %', purged;
    END IF;
    IF (SELECT count(*) FROM memory_jobs WHERE user_id = u AND status = 'dead') <> 1 THEN
        RAISE EXCEPTION '[13] 清理误删了死信';
    END IF;

    RAISE NOTICE '[13] PASS 使用反馈 / 生命周期刷新(updated=%) / 队列清理不碰死信', refreshed;
END $$;

-- ---------------------------------------------------------------------------
-- [14] 复合打分的两个乘数：importance 权重 与 tags 过滤
--     这两个曾经"字段建好了但没进查询"，是最难发现的一类缺口 ——
--     不报错、只是重要性高的记忆没被优待、按标签筛不出来。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    u text := 'selftest:score';
    qv vector := pg_temp.onehot(0);
    high_id uuid;
    low_id uuid;
    tagged_id uuid;
    plain_id uuid;
    r record;
    hits int;
BEGIN
    -- 两条文本与向量都相同，只有 importance 不同
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model,
                          event_time, source, importance, tags)
    VALUES (u, '用户偏好深色主题（重要）', '重要', qv, 'selftest', now(), 'conversation', 1.0, ARRAY['pref','ui'])
    RETURNING id INTO high_id;

    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model,
                          event_time, source, importance, tags)
    VALUES (u, '用户偏好深色主题（次要）', '次要', qv, 'selftest', now(), 'conversation', 0.0, ARRAY['misc'])
    RETURNING id INTO low_id;

    -- importance_weight = 1 时：乘数 = importance，差 1.0 倍 vs 0.0 倍
    SELECT * INTO r FROM recall_episodes(
        qv, u, '深色主题', p_vec_k => 10, p_kw_k => 10, p_limit => 10,
        p_importance_weight => 1.0) x
    WHERE x.id = high_id;
    IF r.id IS NULL THEN
        RAISE EXCEPTION '[14] 重要行没被召回';
    END IF;
    IF r.score <= 0 THEN
        RAISE EXCEPTION '[14] importance=1 时分数应为正，实际 %', r.score;
    END IF;

    -- importance=0 且权重=1 → 乘数为 0 → 分数为 0（排在最后）
    SELECT * INTO r FROM recall_episodes(
        qv, u, '深色主题', p_vec_k => 10, p_kw_k => 10, p_limit => 10,
        p_importance_weight => 1.0) x
    WHERE x.id = low_id;
    IF r.score <> 0 THEN
        RAISE EXCEPTION '[14] importance=0、权重=1 时分数应为 0，实际 %', r.score;
    END IF;

    -- 权重=0 时 importance 完全不参与：两条的"乘数比值"应相同。
    -- 注意不能直接比 score —— 两行内容相同但 RRF 名次不同（1/61 vs 1/62），
    -- score 本来就该有差；要隔离乘数就得比 score / rrf_score。
    DECLARE
        f_high float8;
        f_low  float8;
    BEGIN
        SELECT x.score / NULLIF(x.rrf_score, 0) INTO f_high FROM recall_episodes(
            qv, u, '深色主题', p_vec_k => 10, p_kw_k => 10, p_limit => 10,
            p_importance_weight => 0.0) x WHERE x.id = high_id;
        SELECT x.score / NULLIF(x.rrf_score, 0) INTO f_low FROM recall_episodes(
            qv, u, '深色主题', p_vec_k => 10, p_kw_k => 10, p_limit => 10,
            p_importance_weight => 0.0) x WHERE x.id = low_id;
        IF abs(coalesce(f_high,0) - coalesce(f_low,0)) > 1e-9 THEN
            RAISE EXCEPTION '[14] 权重=0 时 importance 仍在影响乘数（% vs %）', f_high, f_low;
        END IF;
    END;

    -- 权重=1 时，乘数比值的差应等于 importance 的差（1.0 - 0.0）
    DECLARE
        g_high float8;
        g_low  float8;
    BEGIN
        SELECT x.score / NULLIF(x.rrf_score, 0) INTO g_high FROM recall_episodes(
            qv, u, '深色主题', p_vec_k => 10, p_kw_k => 10, p_limit => 10,
            p_importance_weight => 1.0) x WHERE x.id = high_id;
        SELECT x.score / NULLIF(x.rrf_score, 0) INTO g_low FROM recall_episodes(
            qv, u, '深色主题', p_vec_k => 10, p_kw_k => 10, p_limit => 10,
            p_importance_weight => 1.0) x WHERE x.id = low_id;
        -- 两行的 age/use_count 都相同，所以乘数差应等于 (1-w+w*1) vs (1-w+w*0) = 1 vs 0
        IF abs(coalesce(g_high,0) - coalesce(g_low,0)) < 1e-6 THEN
            RAISE EXCEPTION '[14] 权重=1 时 importance 没拉开差距（% vs %）', g_high, g_low;
        END IF;
    END;

    -- tags 过滤：只取带 'ui' 的
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model,
                          event_time, source, tags)
    VALUES (u, '用户把界面改成浅色', '浅色', qv, 'selftest', now(), 'conversation', ARRAY['ui'])
    RETURNING id INTO tagged_id;
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model,
                          event_time, source, tags)
    VALUES (u, '用户把数据库换成 PG18', 'PG18', qv, 'selftest', now(), 'conversation', ARRAY['db'])
    RETURNING id INTO plain_id;

    SELECT count(*) INTO hits FROM recall_episodes(
        qv, u, '用户', p_vec_k => 10, p_kw_k => 10, p_limit => 20,
        p_tags => ARRAY['ui']);
    IF hits <> 2 THEN
        RAISE EXCEPTION '[14] tags 过滤应命中 2 条（pref+ui、ui），实际 %', hits;
    END IF;

    SELECT count(*) INTO hits FROM recall_episodes(
        qv, u, '用户', p_vec_k => 10, p_kw_k => 10, p_limit => 20,
        p_tags => ARRAY['db']);
    IF hits <> 1 THEN
        RAISE EXCEPTION '[14] tags=db 应命中 1 条，实际 %', hits;
    END IF;

    -- 不传 tags = 不过滤
    SELECT count(*) INTO hits FROM recall_episodes(
        qv, u, '用户', p_vec_k => 10, p_kw_k => 10, p_limit => 20);
    IF hits < 4 THEN
        RAISE EXCEPTION '[14] 不传 tags 时不该过滤，实际 % 条', hits;
    END IF;

    RAISE NOTICE '[14] PASS 复合打分：importance 权重可控 / tags 过滤生效 / 空标签不过滤';
END $$;

-- ---------------------------------------------------------------------------
-- [15] 任务状态层 + 工作记忆层
--     这两层独立于记忆检索：任务按键直查、工作集是"装配"而非"召回"。
--     这里守的是它们自己的不变量：状态机、事件流、快照、级联。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    u text := 'selftest:workmem';
    t tasks;
    ws working_sets;
    ep_id uuid;
    wi working_items;
    n int;
    got jsonb;
BEGIN
    -- ---- 状态机 ----
    SELECT * INTO t FROM task_start(u, 'research', '自检任务');
    IF t.status <> 'pending' OR t.started_at IS NOT NULL THEN
        RAISE EXCEPTION '[15] 新任务应为 pending 且无 started_at';
    END IF;

    SELECT * INTO t FROM task_update(t.id, 'running', 'step-a', 1, 0.4::real);
    IF t.status <> 'running' OR t.step <> 'step-a' OR t.started_at IS NULL THEN
        RAISE EXCEPTION '[15] pending->running 没有正确落 step/started_at';
    END IF;

    BEGIN
        PERFORM task_update(t.id, 'pending');
        RAISE EXCEPTION '[15] running->pending 竟然被允许';
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM LIKE '%竟然被允许%' THEN RAISE; END IF;
    END;

    BEGIN
        PERFORM task_update(t.id, 'failed');
        RAISE EXCEPTION '[15] failed 不带 error 竟然通过';
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM LIKE '%竟然通过%' THEN RAISE; END IF;
    END;

    SELECT * INTO t FROM task_update(t.id, 'done', NULL, NULL, 1.0::real, '{"ok":1}'::jsonb);
    IF t.status <> 'done' OR t.finished_at IS NULL THEN
        RAISE EXCEPTION '[15] running->done 没有正确闭合';
    END IF;

    BEGIN
        PERFORM task_update(t.id, 'running');
        RAISE EXCEPTION '[15] 终态回退竟然被允许';
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM LIKE '%竟然被允许%' THEN RAISE; END IF;
    END;

    -- ---- 事件流 ----
    SELECT events INTO got FROM (SELECT * FROM task_summary(t.id, 50)) s;
    IF jsonb_array_length(got) < 3 THEN
        RAISE EXCEPTION '[15] 事件流条数不足（%）', jsonb_array_length(got);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(got) e WHERE e->>'event' = 'created') THEN
        RAISE EXCEPTION '[15] 事件流里没有 created';
    END IF;

    -- ---- 工作集：一个任务一份、快照稳定 ----
    INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source)
    VALUES (u, '用户偏好深色主题', '深色主题', pg_temp.onehot(0), 'selftest', now(), 'conversation')
    RETURNING id INTO ep_id;

    SELECT * INTO t FROM task_start(u, 'impl', '工作集任务');
    SELECT * INTO ws FROM working_open(u, t.id, 'sess-1', '自检', 200);
    SELECT * INTO ws FROM working_open(u, t.id, 'sess-1', '自检', 200);
    SELECT count(*) INTO n FROM working_sets WHERE task_id = t.id;
    IF n <> 1 THEN
        RAISE EXCEPTION '[15] 同一 task 应只有一份工作集，实际 %', n;
    END IF;

    SELECT * INTO wi FROM working_pin(ws.id, 'pinned_memory', '[快照] 用户偏好深色主题', 12, ep_id);
    UPDATE episodes SET valid_until = now() WHERE id = ep_id;
    IF (SELECT content FROM working_items WHERE id = wi.id) <> '[快照] 用户偏好深色主题' THEN
        RAISE EXCEPTION '[15] 底层记忆失效后工作集内容被改动了';
    END IF;
    IF (SELECT ref_id FROM working_items WHERE id = wi.id) IS DISTINCT FROM ep_id THEN
        RAISE EXCEPTION '[15] ref_id 溯源丢失';
    END IF;

    PERFORM working_pin(ws.id, 'note', '插到最前', 4, NULL, 0);
    IF (SELECT content FROM working_items WHERE set_id = ws.id
        ORDER BY position, created_at LIMIT 1) <> '插到最前' THEN
        RAISE EXCEPTION '[15] 显式 position=0 没有排到最前';
    END IF;

    n := working_clear(ws.id);
    IF n < 2 THEN
        RAISE EXCEPTION '[15] working_clear 应删除 >= 2 条，实际 %', n;
    END IF;

    -- ---- 生命周期：过期即删（与记忆的"取代不删除"相反）----
    SELECT * INTO ws FROM working_open(u, NULL, 'sess-exp', '过期测试', 100);
    UPDATE working_sets SET expires_at = now() - interval '1 second' WHERE id = ws.id;
    PERFORM working_expire();
    IF EXISTS (SELECT 1 FROM working_sets WHERE id = ws.id) THEN
        RAISE EXCEPTION '[15] 过期工作集没有被删除';
    END IF;

    -- ---- 级联 ----
    DELETE FROM tasks WHERE id = t.id;
    IF EXISTS (SELECT 1 FROM task_events WHERE task_id = t.id) THEN
        RAISE EXCEPTION '[15] 任务事件没有随任务级联删除';
    END IF;
    IF EXISTS (SELECT 1 FROM working_sets WHERE task_id = t.id) THEN
        RAISE EXCEPTION '[15] 工作集没有随任务级联删除';
    END IF;

    RAISE NOTICE '[15] PASS 任务状态机 / 事件流 / 快照稳定 / position / 过期即删 / 级联';
END $$;

-- ---------------------------------------------------------------------------
-- [16] distill 提炼：候选筛选 / 血缘 / 幂等 / 只动 raw_turn
--     核心风险是"30 天 TTL 到期后原始轮次没了、却没提炼过" ——
--     那等于记忆被静默删除。这里守的就是这条防线。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    u text := 'selftest:distill';
    r1 uuid; r2 uuid; r3 uuid; fresh uuid;
    sum_id uuid;
    n bigint;
    st record;
BEGIN
    -- 两条 10 天前 + 一条 1 天前的 raw_turn
    INSERT INTO episodes (user_id, kind, content, summary, embedding, embedding_model, event_time, source)
    VALUES (u, 'raw_turn', '用户要求发布改走 GitHub Actions', 'CI', pg_temp.onehot(0), 'selftest', now() - interval '10 days', 'conversation')
    RETURNING id INTO r1;
    INSERT INTO episodes (user_id, kind, content, summary, embedding, embedding_model, event_time, source)
    VALUES (u, 'raw_turn', '用户说 staging 端口 55433', '端口', pg_temp.onehot(1), 'selftest', now() - interval '10 days', 'conversation')
    RETURNING id INTO r2;
    INSERT INTO episodes (user_id, kind, content, summary, embedding, embedding_model, event_time, source)
    VALUES (u, 'raw_turn', '刚发生的一条，还不该被提炼', '新', pg_temp.onehot(2), 'selftest', now() - interval '1 day', 'conversation')
    RETURNING id INTO fresh;

    -- 候选：只有够老的两条
    SELECT count(*) INTO n FROM distill_candidates(u, 7, 100);
    IF n <> 2 THEN
        RAISE EXCEPTION '[16] 候选应为 2 条（7 天前的），实际 %', n;
    END IF;

    SELECT * INTO st FROM distill_pending(u, 7);
    IF st.pending <> 2 OR st.oldest_days < 9 THEN
        RAISE EXCEPTION '[16] distill_pending 统计不对: %', st;
    END IF;

    -- 摘要必须是 kind='summary' 才能挂血缘
    INSERT INTO episodes (user_id, kind, content, summary, embedding, embedding_model, event_time, source)
    VALUES (u, 'summary', '部署与端口约定汇总', '汇总', pg_temp.onehot(3), 'selftest', now(), 'tool')
    RETURNING id INTO sum_id;

    BEGIN
        PERFORM distill_link(r1, ARRAY[r1]);  -- r1 是 raw_turn，不是 summary
        RAISE EXCEPTION '[16] 用 raw_turn 当 summary 竟然通过了';
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM LIKE '%竟然通过%' THEN RAISE; END IF;
    END;

    SELECT distill_link(sum_id, ARRAY[r1, r2]) INTO n;
    IF n <> 2 THEN
        RAISE EXCEPTION '[16] 应挂 2 条血缘，实际 %', n;
    END IF;

    -- 幂等：重复挂不报错也不增加
    SELECT distill_link(sum_id, ARRAY[r1, r2]) INTO n;
    IF n <> 0 THEN
        RAISE EXCEPTION '[16] 重复挂血缘应插入 0 行，实际 %', n;
    END IF;

    -- 提炼后候选减少
    SELECT count(*) INTO n FROM distill_candidates(u, 7, 100);
    IF n <> 0 THEN
        RAISE EXCEPTION '[16] 已提炼的原始轮次仍在候选里（%）', n;
    END IF;

    -- 双向血缘
    SELECT count(*) INTO n FROM distill_sources(sum_id);
    IF n <> 2 THEN
        RAISE EXCEPTION '[16] distill_sources 应返回 2 条，实际 %', n;
    END IF;
    SELECT count(*) INTO n FROM distill_of_raw(r1);
    IF n <> 1 THEN
        RAISE EXCEPTION '[16] distill_of_raw 应返回 1 条，实际 %', n;
    END IF;

    -- 再插一条「未提炼的旧记录」，用来验证 close_raw 的排除效果
    INSERT INTO episodes (user_id, kind, content, summary, embedding, embedding_model, event_time, source)
    VALUES (u, 'raw_turn', '另一条未提炼的旧记录', '旧', pg_temp.onehot(4), 'selftest',
            now() - interval '11 days', 'conversation')
    RETURNING id INTO r3;

    SELECT count(*) INTO n FROM distill_candidates(u, 7, 100);
    IF n <> 1 THEN
        RAISE EXCEPTION '[16] 未提炼的老记录应出现在候选里，实际 %', n;
    END IF;

    -- close_raw 只动 raw_turn，不动 summary
    SELECT distill_close_raw(ARRAY[r1, r2, r3, sum_id]) INTO n;
    IF n <> 3 THEN
        RAISE EXCEPTION '[16] close_raw 应只影响 3 条 raw_turn，实际 %', n;
    END IF;
    IF (SELECT valid_until FROM episodes WHERE id = sum_id) IS NOT NULL THEN
        RAISE EXCEPTION '[16] close_raw 误伤了 summary';
    END IF;
    -- 闭合后行仍在（血缘可追）
    IF NOT EXISTS (SELECT 1 FROM episodes WHERE id = r1) THEN
        RAISE EXCEPTION '[16] close_raw 做成了物理删除';
    END IF;

    -- 闭合的 raw_turn 不再进入候选（valid_until 判据）
    SELECT count(*) INTO n FROM distill_candidates(u, 7, 100);
    IF n <> 0 THEN
        RAISE EXCEPTION '[16] 已闭合的 raw_turn 仍进入候选（%）', n;
    END IF;

    RAISE NOTICE '[16] PASS distill：候选筛选 / 只认 summary / 幂等 / 双向血缘 / close_raw 不越界';
END $$;

-- ===========================================================================
-- [17] 图扩展：实体归并 / 多跳 / 防环 / 只走当前有效关系 / 第三路进 RRF
-- ===========================================================================
DO $$
DECLARE
    u    TEXT := 'selftest:graph';
    e1   UUID;
    ep_a UUID; ep_b UUID; ep_c UUID;
    first_ep UUID;
    n    INT;
    lbl  TEXT;
    conf REAL;
BEGIN
    -- 规范化归并：大小写/空格不同视为同一实体
    e1 := graph_upsert_entity(u, '  阿里云  ', 'org');
    e1 := graph_upsert_entity(u, '阿里云', 'concept');        -- 同一条，label 不该被降级
    SELECT count(*) INTO n FROM entities WHERE user_id = u;
    IF n <> 1 THEN
        RAISE EXCEPTION '[17] 实体未归并（应 1 条，实际 %）', n;
    END IF;
    SELECT label INTO lbl FROM entities WHERE user_id = u;
    IF lbl <> 'org' THEN
        RAISE EXCEPTION '[17] label 被 concept 覆盖了（现在 %）', lbl;
    END IF;

    -- 别名不新增实体
    PERFORM graph_upsert_entity(u, '阿里云', 'org', ARRAY['Aliyun', '阿里云公司']);
    SELECT count(*) INTO n FROM entities WHERE user_id = u;
    IF n <> 1 THEN
        RAISE EXCEPTION '[17] 加别名竟新增实体（%）', n;
    END IF;

    PERFORM graph_upsert_entity(u, '某集团', 'org');
    PERFORM graph_relate(u, '阿里云', 'belongs_to', '某集团', 0.9);
    -- 幂等：同一条边再来一次不新增，置信度取较高者
    PERFORM graph_relate(u, '阿里云', 'belongs_to', '某集团', 0.95);
    SELECT count(*) INTO n FROM relations WHERE user_id = u AND valid_until IS NULL;
    IF n <> 1 THEN
        RAISE EXCEPTION '[17] 关系不幂等（应为 1 条，实际 %）', n;
    END IF;
    SELECT confidence INTO conf FROM relations WHERE user_id = u;
    IF conf::float8 < 0.94 THEN
        RAISE EXCEPTION '[17] 重复关系未取较高置信度（%）', conf;
    END IF;

    -- 自环必须被拒
    BEGIN
        PERFORM graph_relate(u, '阿里云', 'belongs_to', '阿里云');
        RAISE EXCEPTION '[17] 自环关系竟然被接受';
    EXCEPTION WHEN check_violation THEN NULL;   -- CHECK rel_no_self_loop
    END;

    -- 链：阿里云 → 某集团 → 杭州机房
    PERFORM graph_relate(u, '某集团', 'owns', '杭州机房');
    SELECT count(*) INTO n FROM graph_neighbors(u, ARRAY['阿里云'], 2);
    IF n <> 3 THEN
        RAISE EXCEPTION '[17] 2 跳应达 3 个实体，实际 %', n;
    END IF;
    SELECT count(*) INTO n FROM graph_neighbors(u, ARRAY['阿里云'], 1);
    IF n <> 2 THEN
        RAISE EXCEPTION '[17] 1 跳应为 2 个实体，实际 %', n;
    END IF;

    -- 环：杭州机房 → 阿里云，递归不能爆
    PERFORM graph_relate(u, '杭州机房', 'serves', '阿里云');
    SELECT count(*) INTO n FROM graph_neighbors(u, ARRAY['阿里云'], 5);
    IF n <> 3 THEN
        RAISE EXCEPTION '[17] 有环时遍历应仍是 3 个实体，实际 %', n;
    END IF;

    -- 记忆挂载
    INSERT INTO episodes (user_id, kind, content, summary, event_time, source)
    VALUES (u, 'episode', '阿里云用的是杭州机房', '阿里云/杭州机房', now(), 'tool')
    RETURNING id INTO ep_a;
    INSERT INTO episodes (user_id, kind, content, summary, event_time, source)
    VALUES (u, 'episode', '某集团统一走内网审计', '集团审计', now(), 'tool')
    RETURNING id INTO ep_b;
    INSERT INTO episodes (user_id, kind, content, summary, event_time, source)
    VALUES (u, 'episode', '本次发布窗口改到 22:00', '发布窗口', now(), 'tool')
    RETURNING id INTO ep_c;

    SELECT graph_link_episode(ep_a, ARRAY['阿里云']) INTO n;
    IF n <> 1 THEN RAISE EXCEPTION '[17] 挂载应为 1 条边，实际 %', n; END IF;
    PERFORM graph_link_episode(ep_b, ARRAY['某集团']);
    -- 挂不存在的实体：不报错、不新增
    SELECT graph_link_episode(ep_b, ARRAY['根本不存在的实体']) INTO n;
    IF n <> 0 THEN RAISE EXCEPTION '[17] 未知实体不该挂上（%）', n; END IF;

    -- 第三路只拿得到"图可达"的记忆
    SELECT count(*) INTO n FROM graph_episodes(u, ARRAY['阿里云'], 2);
    IF n <> 2 THEN
        RAISE EXCEPTION '[17] 图可达记忆应为 2 条，实际 %', n;
    END IF;
    IF EXISTS (SELECT 1 FROM graph_episodes(u, ARRAY['阿里云'], 2) WHERE episode_id = ep_c) THEN
        RAISE EXCEPTION '[17] 无关记忆被图路带出来了';
    END IF;

    -- 深度优先：阿里云(0) 那条排在 某集团(1) 那条前面
    SELECT episode_id INTO first_ep FROM graph_episodes(u, ARRAY['阿里云'], 2)
     ORDER BY depth LIMIT 1;
    IF first_ep <> ep_a THEN
        RAISE EXCEPTION '[17] 图路排序错误：最近的应是 ep_a';
    END IF;

    -- via_entity 必须是**最近那一跳**的实体（否则 depth=0 却标着 depth=2 的实体，误导排查）
    SELECT via_entity INTO lbl FROM graph_episodes(u, ARRAY['阿里云'], 2) WHERE episode_id = ep_a;
    IF lbl <> '阿里云' THEN
        RAISE EXCEPTION '[17] ep_a 的 via_entity 应是种子阿里云，实际 %', lbl;
    END IF;
    SELECT via_entity INTO lbl FROM graph_episodes(u, ARRAY['阿里云'], 2) WHERE episode_id = ep_b;
    IF lbl <> '某集团' THEN
        RAISE EXCEPTION '[17] ep_b 的 via_entity 应是某集团，实际 %', lbl;
    END IF;

    -- 文本里认实体：认别名
    SELECT count(*) INTO n FROM graph_entities_in_text(u, '问问阿里云公司那边');
    IF n <> 1 THEN
        RAISE EXCEPTION '[17] 别名未识别（%）', n;
    END IF;

    -- 关闭的关系不参与遍历。
    -- 注意这里是**无向**遍历：只关 belongs_to 仍能经 serves（杭州机房→阿里云）绕回来，
    -- 所以必须全关；全关之后只剩种子自己。
    UPDATE relations SET valid_until = now() WHERE user_id = u AND valid_until IS NULL;
    SELECT count(*) INTO n FROM graph_neighbors(u, ARRAY['阿里云'], 2);
    IF n <> 1 THEN
        RAISE EXCEPTION '[17] 关闭的关系仍被走了（可达 % / 应只有种子 1 个）', n;
    END IF;
    UPDATE relations SET valid_until = NULL WHERE user_id = u;

    -- 第三路真的进 RRF。
    -- 查询词 '阿里云' 只出现在 ep_a 正文里，所以两路只能拿到 ep_a；
    -- ep_b（"某集团统一走内网审计"）正文里没有"阿里云"三个字 ——
    -- 只有沿关系 阿里云 → 某集团 走一跳才够得着。这就是图路存在的全部理由。
    SELECT count(*) INTO n FROM recall_episodes(
        NULL::vector, u, '阿里云', 0, 50, 60, 180, 1825, 0.5, 0.2, 10,
        NULL, false, 0.5, NULL, NULL);
    IF n <> 1 THEN
        RAISE EXCEPTION '[17] 两路应只召回 1 条（ep_a），实际 %', n;
    END IF;
    IF EXISTS (
        SELECT 1 FROM recall_episodes(
            NULL::vector, u, '阿里云', 0, 50, 60, 180, 1825, 0.5, 0.2, 10,
            NULL, false, 0.5, NULL, NULL) r
        WHERE r.id = ep_b) THEN
        RAISE EXCEPTION '[17] 两路竟然够到了 ep_b（词法不该命中它）';
    END IF;

    SELECT count(*) INTO n FROM recall_episodes(
        NULL::vector, u, '阿里云', 0, 50, 60, 180, 1825, 0.5, 0.2, 10,
        NULL, false, 0.5, NULL,
        (SELECT array_agg(episode_id ORDER BY depth) FROM graph_episodes(u, ARRAY['阿里云'], 2)));
    IF n <> 2 THEN
        RAISE EXCEPTION '[17] 三路应召回 2 条，实际 %', n;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM recall_episodes(
            NULL::vector, u, '阿里云', 0, 50, 60, 180, 1825, 0.5, 0.2, 10,
            NULL, false, 0.5, NULL,
            (SELECT array_agg(episode_id ORDER BY depth)
               FROM graph_episodes(u, ARRAY['阿里云'], 2))) r
        WHERE r.id = ep_b AND r.rk_g IS NOT NULL AND r.rk_k IS NULL) THEN
        RAISE EXCEPTION '[17] ep_b 应由图路单独带出（rk_g 有值、rk_k 为空）';
    END IF;

    -- 空图数组 = 回到两路，结果必须完全一致（数量与成员都不变）
    SELECT count(*) INTO n FROM recall_episodes(
        NULL::vector, u, '阿里云', 0, 50, 60, 180, 1825, 0.5, 0.2, 10,
        NULL, false, 0.5, NULL, ARRAY[]::uuid[]);
    IF n <> 1 THEN
        RAISE EXCEPTION '[17] 空图数组不该改变结果（% / 应 1）', n;
    END IF;

    RAISE NOTICE '[17] PASS 图扩展：归并 / 幂等 / 防环 / 只走当前有效 / 第三路进 RRF';
END $$;

-- ===========================================================================
-- [18] 整用户清除：二次确认 / dryrun 对账 / 跨层干净 / 不误伤他人
-- ===========================================================================
DO $$
DECLARE
    u   TEXT := 'selftest:purge';
    o   TEXT := 'selftest:purge_keep';
    eid UUID;
    sid UUID;
    n   INT;
    left_over INT;
    kept INT;
BEGIN
    INSERT INTO episodes (user_id, kind, content, summary, event_time, source)
    VALUES (u, 'raw_turn', '要被清掉的原始轮次', '待清', now(), 'conversation')
    RETURNING id INTO eid;
    INSERT INTO episodes (user_id, kind, content, summary, event_time, source)
    VALUES (u, 'summary', '由原始轮次提炼出的摘要', '摘要', now(), 'tool')
    RETURNING id INTO sid;
    INSERT INTO episodes (user_id, kind, content, summary, event_time, source)
    VALUES (o, 'episode', '旁观者的记忆', '旁观', now(), 'tool');
    INSERT INTO facts (user_id, key, value, source) VALUES (u, 'purge.k', '"v"', 'human');
    INSERT INTO facts (user_id, key, value, source) VALUES (o, 'purge.k', '"v"', 'human');
    PERFORM task_start(u, 'ops', '待清任务');
    PERFORM working_open(u, NULL, NULL, '待清工作集');
    PERFORM graph_upsert_entity(u, '待清实体', 'concept');
    PERFORM graph_upsert_entity(o, '旁观实体', 'concept');
    PERFORM graph_relate(u, '待清实体', 'related_to', '待清实体二');
    PERFORM graph_link_episode(eid, ARRAY['待清实体']);
    PERFORM distill_link(sid, ARRAY[eid]);
    INSERT INTO memory_jobs (user_id, job_type, payload) VALUES (u, 'distill', '{}'::jsonb);

    -- 1) 确认串不一致 → 必须拒绝。用 SQLSTATE 精确断言：P0001 且消息可辨。
    BEGIN
        PERFORM purge_user(u, 'wrong');
        RAISE EXCEPTION '[18] 确认串不一致竟然执行了';
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM LIKE '[18]%' THEN RAISE; END IF;
    END;
    SELECT count(*) INTO n FROM episodes WHERE user_id = u;
    IF n <> 2 THEN
        RAISE EXCEPTION '[18] 被拒绝的调用竟然删了数据（剩 % / 应 2）', n;
    END IF;

    -- 2) dryrun 对账
    SELECT r.rows_to_delete INTO n FROM purge_user_dryrun(u) r WHERE r.table_name = 'episodes';
    IF n <> 2 THEN RAISE EXCEPTION '[18] dryrun episodes 应为 2，实际 %', n; END IF;
    SELECT r.rows_to_delete INTO n FROM purge_user_dryrun(u) r WHERE r.table_name = 'facts';
    IF n <> 1 THEN RAISE EXCEPTION '[18] dryrun facts 应为 1，实际 %', n; END IF;
    SELECT r.rows_to_delete INTO n FROM purge_user_dryrun(u) r WHERE r.table_name = 'distill_links';
    IF n <> 1 THEN RAISE EXCEPTION '[18] dryrun distill_links 应为 1，实际 %', n; END IF;

    -- 3) 真删
    PERFORM purge_user(u, u);

    -- 4) 目标用户跨层全清空
    SELECT (SELECT count(*) FROM episodes      WHERE user_id = u)
         + (SELECT count(*) FROM facts         WHERE user_id = u)
         + (SELECT count(*) FROM entities      WHERE user_id = u)
         + (SELECT count(*) FROM relations     WHERE user_id = u)
         + (SELECT count(*) FROM tasks         WHERE user_id = u)
         + (SELECT count(*) FROM working_sets  WHERE user_id = u)
         + (SELECT count(*) FROM memory_jobs   WHERE user_id = u)
         + (SELECT count(*) FROM distill_links WHERE summary_id = sid OR raw_id = eid)
      INTO left_over;
    IF left_over <> 0 THEN
        RAISE EXCEPTION '[18] 清除后仍有 % 行残留', left_over;
    END IF;

    -- 5) 旁观者完好
    SELECT (SELECT count(*) FROM episodes WHERE user_id = o)
         + (SELECT count(*) FROM facts    WHERE user_id = o)
         + (SELECT count(*) FROM entities WHERE user_id = o) INTO kept;
    IF kept <> 3 THEN
        RAISE EXCEPTION '[18] 误伤了旁观用户（剩 % / 应 3）', kept;
    END IF;

    RAISE NOTICE '[18] PASS 清除：二次确认 / dryrun 对账 / 跨层干净 / 不误伤他人';
END $$;

-- ===========================================================================
-- [19] 关键词提炼：分桶按 event_time / 半开区间 / 幂等 / 摘要不进原料
-- ===========================================================================
DO $$
DECLARE
    u     TEXT := 'selftest:digest';
    ws    DATE;
    ws2   DATE;
    n     INT;
    id1   UUID;
    id2   UUID;
    rec   RECORD;
    ep1   UUID;
BEGIN
    -- 1) 桶边界：日/周/月/年各自的起点与半开终点
    IF digest_bucket(TIMESTAMPTZ '2026-10-25 13:00+08', 'day') <> DATE '2026-10-25' THEN
        RAISE EXCEPTION '[19] 日桶起点错';
    END IF;
    -- 2026-10-25 是周日，ISO 周应从 10-19（周一）开始
    IF digest_bucket(TIMESTAMPTZ '2026-10-25 13:00+08', 'week') <> DATE '2026-10-19' THEN
        RAISE EXCEPTION '[19] 周桶起点不是周一：%',
            digest_bucket(TIMESTAMPTZ '2026-10-25 13:00+08', 'week');
    END IF;
    IF digest_bucket(TIMESTAMPTZ '2026-10-25 13:00+08', 'month') <> DATE '2026-10-01' THEN
        RAISE EXCEPTION '[19] 月桶起点错';
    END IF;
    IF digest_bucket(TIMESTAMPTZ '2026-10-25 13:00+08', 'year') <> DATE '2026-01-01' THEN
        RAISE EXCEPTION '[19] 年桶起点错';
    END IF;
    IF digest_bucket_end(DATE '2026-10-19', 'week') <> DATE '2026-10-26' THEN
        RAISE EXCEPTION '[19] 周桶终点应为下周一（半开）';
    END IF;
    -- 2 月跨年边界：12-01 + 1 month 必须落到次年 01-01
    IF digest_bucket_end(DATE '2026-12-01', 'month') <> DATE '2027-01-01' THEN
        RAISE EXCEPTION '[19] 月桶跨年边界错：%',
            digest_bucket_end(DATE '2026-12-01', 'month');
    END IF;
    -- 闰年 2 月是 29 天
    IF digest_bucket_end(DATE '2028-02-01', 'month') <> DATE '2028-03-01' THEN
        RAISE EXCEPTION '[19] 闰年 2 月边界错';
    END IF;
    IF digest_bucket_end(DATE '2026-01-01', 'year') <> DATE '2027-01-01' THEN
        RAISE EXCEPTION '[19] 年桶终点错';
    END IF;

    -- 2) 枚举桶：按 event_time 归属，且相邻桶不重叠
    ws := digest_bucket(now(), 'week');
    INSERT INTO episodes (user_id, kind, content, summary, event_time, source)
    VALUES (u, 'episode', '本周的一条记忆', '本周', now(), 'tool')
    RETURNING id INTO ep1;
    INSERT INTO episodes (user_id, kind, content, summary, event_time, source)
    VALUES (u, 'episode', '十天前的一条记忆', '十天前', now() - interval '10 days', 'tool');

    SELECT count(*) INTO n
      FROM digest_buckets(u, 'week', (ws - interval '21 days')::date, (ws + interval '1 day')::date)
     WHERE episode_count > 0;
    IF n <> 2 THEN
        RAISE EXCEPTION '[19] 应有两个非空周桶（本周 + 十天前），实际 %', n;
    END IF;

    -- 3) digest_sources 只取该桶、且排除 digest 自己的摘要
    SELECT count(*) INTO n FROM digest_sources(u, 'week', ws);
    IF n <> 1 THEN
        RAISE EXCEPTION '[19] 本周桶应只有 1 条原料，实际 %', n;
    END IF;

    -- 4) upsert 幂等：同一 (user, period, bucket) 只有一行，内容被覆盖
    SELECT digest_upsert(u, 'week', ws, digest_bucket_end(ws, 'week'),
                         '[{"term":"甲","count":3,"weight":3}]'::jsonb,
                         ARRAY['甲'], 1, 'first', 'content-1', NULL) INTO id1;
    SELECT digest_upsert(u, 'week', ws, digest_bucket_end(ws, 'week'),
                         '[{"term":"乙","count":5,"weight":5}]'::jsonb,
                         ARRAY['乙'], 2, 'second', 'content-2', NULL) INTO id2;
    IF id1 <> id2 THEN
        RAISE EXCEPTION '[19] upsert 产生了新行（% vs %）', id1, id2;
    END IF;
    SELECT * INTO rec FROM memory_digests WHERE user_id = u AND period = 'week'
      AND bucket_start = ws;
    IF rec.episode_count <> 2 OR rec.summary <> 'second' OR rec.keywords <> ARRAY['乙'] THEN
        RAISE EXCEPTION '[19] upsert 没有覆盖旧内容：% / % / %',
            rec.episode_count, rec.summary, rec.keywords;
    END IF;

    -- 5) digest_episode_id 传 NULL 时沿用旧值（避免一次空重建把指针抹掉）
    SELECT digest_upsert(u, 'week', ws, digest_bucket_end(ws, 'week'),
                         '[]'::jsonb, ARRAY[]::text[], 0, 'third', 'content-3', ep1) INTO id1;
    SELECT digest_upsert(u, 'week', ws, digest_bucket_end(ws, 'week'),
                         '[]'::jsonb, ARRAY[]::text[], 0, 'fourth', 'content-4', NULL) INTO id2;
    SELECT * INTO rec FROM memory_digests WHERE user_id = u AND period = 'week'
      AND bucket_start = ws;
    IF rec.digest_episode_id IS DISTINCT FROM ep1 THEN
        RAISE EXCEPTION '[19] 传 NULL 时不该抹掉 digest_episode_id（现在 %）', rec.digest_episode_id;
    END IF;

    -- 6) 摘要记忆若带 digest 标签，就不再进原料（防止年摘要把日摘要再摘一遍）
    UPDATE episodes SET tags = ARRAY['sys:digest'] WHERE id = ep1;
    SELECT count(*) INTO n FROM digest_sources(u, 'week', ws);
    IF n <> 0 THEN
        RAISE EXCEPTION '[19] 摘要记忆没有被排除出原料（剩 %）', n;
    END IF;
    UPDATE episodes SET tags = ARRAY[]::text[] WHERE id = ep1;

    -- 7) digest_stats 能看到各期覆盖
    SELECT count(*) INTO n FROM digest_stats(u) WHERE period = 'week' AND buckets >= 1;
    IF n <> 1 THEN
        RAISE EXCEPTION '[19] digest_stats 没有统计到周摘要';
    END IF;

    RAISE NOTICE '[19] PASS 关键词提炼：桶边界（ISO 周/跨年/闰月）/ 按 event_time 归属 / upsert 幂等 / 摘要不进原料';
END $$;

-- 清理
DELETE FROM memory_digests WHERE user_id LIKE 'selftest:%';
DELETE FROM memory_conflicts WHERE user_id LIKE 'selftest:%';
DELETE FROM episodes    WHERE user_id LIKE 'selftest:%';
DELETE FROM facts       WHERE user_id LIKE 'selftest:%';
DELETE FROM memory_jobs WHERE user_id LIKE 'selftest:%';
DELETE FROM relations   WHERE user_id LIKE 'selftest:%';
DELETE FROM entities    WHERE user_id LIKE 'selftest:%';
DELETE FROM tasks       WHERE user_id LIKE 'selftest:%';
DELETE FROM working_sets WHERE user_id LIKE 'selftest:%';

COMMIT;

\echo ''
\echo '========================================='
\echo ' 全部自检通过（19 组，均为行为断言）'
\echo '========================================='
