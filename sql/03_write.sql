-- =============================================================================
-- 写入层：四态裁决的落库动作 + 冲突裁决 + 软删除 + 队列（含回收与死信）
-- 运行前提：sql/01_schema.sql 已执行。
--
-- 四态与落库动作的对应关系（决策二：模型只提议，规则做裁决）：
--
--   accept  → 01_insert_episode            新行写入
--   merge   → 02_reinforce_episode         不新建，抬高既有行的置信度/重要性
--   defer   → 03_insert_deferred_episode   新行以 review_state='pending' 落库（默认不召回）
--             + 04_open_conflict           在旧行与新行之间建 open 的边
--   drop    → 什么都不做
--
-- 矛盾**被裁决之后**才走取代：05_supersede_episode（旧行闭合、superseded_by 指向新行）。
-- 取代不删除 —— 旧行永远留在表里，retrieval 只是过滤掉被闭合的版本，
-- 于是"当时为什么那样认为"这个问题事后仍然可答。
--
-- 所有 INSERT 都用**无目标**的 ON CONFLICT DO NOTHING：目标是部分唯一索引时，
-- 带推断子句的写法对不同 PG 版本对「索引谓词必须被 WHERE 蕴含」的判定很挑剔，
-- 无目标版本一律可靠 —— 反正我们也确实想让任何唯一约束冲突都变成幂等跳过。
-- =============================================================================
\set ON_ERROR_STOP on

-- 同一会话里重复执行会报 "prepared statement already exists"：先 DEALLOCATE ALL;

-- ---------------------------------------------------------------------------
-- 01 accept —— 写入一条新记忆
-- expires_at 留空，由 01_schema.sql 里的触发器按 kind 从 ttl_interval() 算出，
-- 任何写入路径都不可能忘记算 TTL。
-- ---------------------------------------------------------------------------
PREPARE insert_episode(
    text, text, text, text, vector, text, timestamptz, text, text, text,
    real, real, text, text[]
) AS
INSERT INTO episodes (
    user_id, kind, content, summary, embedding, embedding_model,
    event_time, event_time_source, source, source_ref,
    confidence, importance, review_state, tags
) VALUES (
    $1, $2, $3, $4, $5::vector, $6,
    $7, $8, $9, $10,
    $11, $12, $13, $14
)
ON CONFLICT DO NOTHING
RETURNING id, expires_at, lifecycle_state;

-- ---------------------------------------------------------------------------
-- 02 merge —— 复述/强化：不新建行，只抬高既有条目的置信度与重要性
-- 必须带 valid_until IS NULL：并发的另一次写入可能已经把它闭合了，
-- 那时 rowcount=0 就是明确的"目标已失效"，而不是静默改历史行。
-- 上限一律 LEAST(1.0, ...)，避免反复复述把置信度顶到 1.0 之后再也无法区分强弱。
-- ---------------------------------------------------------------------------
PREPARE reinforce_episode(uuid, real, real) AS
UPDATE episodes
SET confidence = LEAST(1.0, confidence + $2),
    importance = LEAST(1.0, importance + $3),
    use_count  = use_count + 1,
    last_used_at = now()
WHERE id = $1
  AND valid_until IS NULL
RETURNING id, confidence, importance, use_count;

-- ---------------------------------------------------------------------------
-- 03 defer —— 疑似矛盾：落库但标记待裁决
-- review_state='pending' 的行默认不参与召回（见 02_retrieval.sql 的 $12 开关），
-- 这样"新值"不会在裁决前抢走"旧值"的位置，信息也没有丢。
-- ---------------------------------------------------------------------------
PREPARE insert_deferred_episode(
    text, text, text, text, vector, text, timestamptz, text, text, text,
    real, real, text[]
) AS
INSERT INTO episodes (
    user_id, kind, content, summary, embedding, embedding_model,
    event_time, event_time_source, source, source_ref,
    confidence, importance, review_state, tags
) VALUES (
    $1, $2, $3, $4, $5::vector, $6,
    $7, $8, $9, $10,
    $11, $12, 'pending', $13
)
ON CONFLICT DO NOTHING
RETURNING id, expires_at;

-- 04 冲突边
PREPARE open_conflict(uuid, text, uuid, uuid, text, jsonb) AS
INSERT INTO memory_conflicts (id, user_id, existing_id, incoming_id, reason, signal)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT DO NOTHING
RETURNING id;

-- ---------------------------------------------------------------------------
-- 05 supersede —— 旧行闭合 + 指向新行（取代而非删除）
-- 带 AND valid_until IS NULL 是并发兜底：LLM 裁决在事务外跑（几百 ms~几秒），
-- 期间目标行可能已被别的 worker 闭合；rowcount=0 就是"输了竞态"。
-- ---------------------------------------------------------------------------
PREPARE supersede_episode(uuid, uuid) AS
UPDATE episodes
SET valid_until = now(),
    superseded_by = $2
WHERE id = $1
  AND valid_until IS NULL
RETURNING id;

-- ---------------------------------------------------------------------------
-- 06 冲突裁决：把 open 的 defer 落定
--   superseded —— 新行胜：旧行闭合（取代）
--   kept_old   —— 旧行胜：新行标 rejected（留在表里，可追溯，不再召回）
--   merged     —— 两条合并：旧行闭合，新行吸收旧行的置信度后存活
-- 一个函数做完，保证"边状态"与"行状态"不会只改一半。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION resolve_conflict(
    p_conflict_id uuid,
    p_resolution   text,               -- superseded / kept_old / merged
    p_resolved_by  text DEFAULT 'rule'
) RETURNS TABLE (conflict_id uuid, existing_id uuid, incoming_id uuid, resolution text)
LANGUAGE plpgsql AS $$
DECLARE
    c memory_conflicts;
BEGIN
    IF p_resolution NOT IN ('superseded','kept_old','merged') THEN
        RAISE EXCEPTION '非法裁决 %，只接受 superseded / kept_old / merged', p_resolution;
    END IF;

    SELECT * INTO c FROM memory_conflicts WHERE id = p_conflict_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION '冲突 % 不存在', p_conflict_id;
    END IF;
    IF c.status <> 'open' THEN
        RAISE EXCEPTION '冲突 % 已经处理过（status=%）', p_conflict_id, c.status;
    END IF;

    IF p_resolution IN ('superseded','merged') THEN
        -- 旧行闭合、血缘指向新行
        UPDATE episodes
        SET valid_until = now(), superseded_by = c.incoming_id
        WHERE id = c.existing_id AND valid_until IS NULL;

        -- 新行转入正常状态（若旧行已被人抢先闭合，新行仍然存活，信息不丢）
        UPDATE episodes
        SET review_state = 'none'
        WHERE id = c.incoming_id;
    ELSE
        UPDATE episodes
        SET review_state = 'rejected', valid_until = now()
        WHERE id = c.incoming_id;
    END IF;

    IF p_resolution = 'merged' THEN
        -- 合并：新行吸收旧行的治理信号，避免"合并之后反而更不确定"
        UPDATE episodes new
        SET confidence = LEAST(1.0, GREATEST(new.confidence, old.confidence)),
            importance = LEAST(1.0, GREATEST(new.importance, old.importance))
        FROM episodes old
        WHERE new.id = c.incoming_id AND old.id = c.existing_id;
    END IF;

    UPDATE memory_conflicts
    SET status = 'resolved', resolution = p_resolution,
        resolved_at = now(), resolved_by = p_resolved_by
    WHERE id = p_conflict_id;

    RETURN QUERY SELECT c.id, c.existing_id, c.incoming_id, p_resolution;
END $$;

-- 便捷版：按 (user_id, existing_id) 找那条 open 的边并裁决
--
-- 注意所有列引用都必须带别名。RETURNS TABLE 里的 existing_id 会和
-- memory_conflicts.existing_id 撞名，不限定表别名就是
-- "column reference "existing_id" is ambiguous" —— 而且这个错只在**调用时**才出现，
-- 建函数时不会报（plpgsql 是运行时解析 SQL 的）。
CREATE OR REPLACE FUNCTION resolve_conflict_for(
    p_user_id text, p_existing_id uuid, p_resolution text, p_resolved_by text DEFAULT 'rule'
) RETURNS TABLE (conflict_id uuid, existing_id uuid, incoming_id uuid, resolution text)
LANGUAGE plpgsql AS $$
DECLARE
    v_id uuid;
BEGIN
    SELECT c.id INTO v_id
    FROM memory_conflicts c
    WHERE c.user_id = p_user_id
      AND c.existing_id = p_existing_id
      AND c.status = 'open'
    ORDER BY c.created_at DESC
    LIMIT 1;

    IF v_id IS NULL THEN
        RAISE EXCEPTION '没有待裁决的冲突（user=%, existing=%）', p_user_id, p_existing_id;
    END IF;
    RETURN QUERY SELECT * FROM resolve_conflict(v_id, p_resolution, p_resolved_by);
END $$;

-- ---------------------------------------------------------------------------
-- 07 软删除 / 恢复（遗忘）
-- 遗忘 = 闭合区间 + review_state='rejected'（tombstone 留在表里，可恢复）。
-- 物理删除只发生在用户级 purge —— 那是唯一允许真删的路径。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION forget_episode(p_id uuid, p_reason text DEFAULT NULL)
RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
    UPDATE episodes
    SET valid_until = now(), review_state = 'rejected',
        summary = CASE WHEN p_reason IS NULL THEN summary
                       ELSE left(COALESCE(summary, content), 190) || ' [遗忘: ' || p_reason || ']' END
    WHERE id = p_id AND valid_until IS NULL;
    RETURN FOUND;
END $$;

CREATE OR REPLACE FUNCTION restore_episode(p_id uuid)
RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
    UPDATE episodes
    SET valid_until = NULL, superseded_by = NULL, review_state = 'none'
    WHERE id = p_id AND valid_until IS NOT NULL;
    RETURN FOUND;
END $$;

-- ---------------------------------------------------------------------------
-- 08 长期事实：先失效旧行，再插新行
--
-- 顺序**必须**是"先失效、后插入"：部分唯一索引是立即检查的，新旧两行同时
-- valid_until IS NULL 会立刻撞唯一约束。新行 id 由应用侧预先生成（uuid4），
-- 才能让延迟外键在提交时解析 —— 写成 gen_random_uuid() 的话悬空引用只会在
-- COMMIT 报错，而清理语句一跑这个错误还会消失。
-- ---------------------------------------------------------------------------
PREPARE supersede_fact(uuid, text, text) AS
UPDATE facts
SET valid_until = now(),
    superseded_by = $1          -- 预先生成的新行 id
WHERE user_id = $2
  AND key = $3
  AND valid_until IS NULL
RETURNING id;

PREPARE insert_fact(uuid, text, text, jsonb, real, real, text) AS
INSERT INTO facts (id, user_id, key, value, confidence, importance, source)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT DO NOTHING
RETURNING id;

-- ---------------------------------------------------------------------------
-- 09 队列：入队 / 领取 / 确认 / 失败 / 回收
-- ---------------------------------------------------------------------------

-- 9.1 入队（dedup_key 在途去重：同一片段不会并发处理两次）
PREPARE enqueue_job(text, text, jsonb, text) AS
INSERT INTO memory_jobs (user_id, job_type, payload, dedup_key)
VALUES ($1, $2, $3, $4)
ON CONFLICT DO NOTHING
RETURNING id;

-- 9.2 领取：FOR UPDATE SKIP LOCKED 是唯一正确姿势
-- SQL 里没有 "UPDATE ... LIMIT" —— 它不合法，必须走 CTE 子查询。
PREPARE claim_jobs(int, text) AS
WITH next_batch AS (
    SELECT id
    FROM memory_jobs
    WHERE status = 'pending'
      AND run_at <= now()
    ORDER BY run_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT $1
)
UPDATE memory_jobs j
SET status     = 'processing',
    locked_at  = now(),
    locked_by  = $2,
    attempts   = j.attempts + 1,
    updated_at = now()
FROM next_batch b
WHERE j.id = b.id
RETURNING j.id, j.user_id, j.job_type, j.payload, j.attempts, j.max_attempts;

-- 9.3 成功
PREPARE ack_job(bigint) AS
UPDATE memory_jobs
SET status = 'done', finished_at = now(), updated_at = now(),
    locked_at = NULL, locked_by = NULL, last_error = NULL
WHERE id = $1 AND status = 'processing'
RETURNING id;

-- 9.4 失败：指数退避，超过 max_attempts 进死信
PREPARE fail_job(bigint, text) AS
UPDATE memory_jobs
SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
    run_at = CASE WHEN attempts >= max_attempts
                  THEN run_at
                  ELSE now() + make_interval(secs => LEAST(300, (5 * (2 ^ attempts))::int))
             END,
    finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END,
    locked_at = NULL, locked_by = NULL,
    last_error = left($2, 2000), updated_at = now()
WHERE id = $1
RETURNING id, status, attempts;

-- 9.5 回收：worker 被 OOM 杀掉时任务会永久卡在 processing。
-- 没有这一步，SKIP LOCKED 队列就是一条会静默丢数据的水管。
CREATE OR REPLACE FUNCTION reap_stuck_jobs(
    p_lease_seconds int DEFAULT 300,
    p_batch         int DEFAULT 500
) RETURNS TABLE (reclaimed bigint, deadlettered bigint)
LANGUAGE plpgsql AS $$
DECLARE
    v_cutoff timestamptz := now() - make_interval(secs => p_lease_seconds);
    v_reclaim bigint;
    v_dead    bigint;
BEGIN
    WITH t AS (
        SELECT id FROM memory_jobs
        WHERE status = 'processing' AND locked_at < v_cutoff AND attempts < max_attempts
        ORDER BY locked_at FOR UPDATE SKIP LOCKED LIMIT p_batch
    )
    UPDATE memory_jobs j
    SET status = 'pending', locked_at = NULL, locked_by = NULL,
        run_at = now(), updated_at = now(),
        last_error = COALESCE(j.last_error, 'lease expired')
    FROM t WHERE j.id = t.id;
    GET DIAGNOSTICS v_reclaim = ROW_COUNT;

    WITH t AS (
        SELECT id FROM memory_jobs
        WHERE status = 'processing' AND locked_at < v_cutoff AND attempts >= max_attempts
        ORDER BY locked_at FOR UPDATE SKIP LOCKED LIMIT p_batch
    )
    UPDATE memory_jobs j
    SET status = 'dead', locked_at = NULL, locked_by = NULL,
        finished_at = now(), updated_at = now(),
        last_error = COALESCE(j.last_error, 'lease expired, out of retries')
    FROM t WHERE j.id = t.id;
    GET DIAGNOSTICS v_dead = ROW_COUNT;

    RETURN QUERY SELECT v_reclaim, v_dead;
END $$;

-- ---------------------------------------------------------------------------
-- 10 可观测性：这三条查询决定你能不能在 5 分钟内发现问题
-- ---------------------------------------------------------------------------
PREPARE queue_stats AS
SELECT status, count(*) AS jobs, min(created_at) AS oldest,
       max(attempts) AS max_attempts_used,
       count(*) FILTER (WHERE status = 'pending' AND run_at < now() - interval '5 min') AS overdue
FROM memory_jobs GROUP BY status;

PREPARE dead_letter_check(int) AS
SELECT id, user_id, job_type, attempts, last_error, updated_at
FROM memory_jobs
WHERE status = 'dead' AND updated_at > now() - interval '1 hour'
ORDER BY updated_at DESC LIMIT $1;

PREPARE backlog_age AS
SELECT COALESCE(EXTRACT(EPOCH FROM (now() - min(run_at))), 0)::int AS oldest_pending_seconds,
       count(*) AS pending
FROM memory_jobs WHERE status = 'pending';

PREPARE purge_finished_jobs(interval) AS
DELETE FROM memory_jobs WHERE status = 'done' AND finished_at < now() - $1;

-- 记忆卫生总览：待裁决、已过期未清、无向量的行
PREPARE memory_health(text) AS
SELECT
  (SELECT count(*) FROM episodes WHERE user_id = $1 AND review_state = 'pending')      AS pending_review,
  (SELECT count(*) FROM episodes WHERE user_id = $1 AND valid_until IS NULL
      AND expires_at IS NOT NULL AND expires_at <= now())                              AS expired_live,
  (SELECT count(*) FROM episodes WHERE user_id = $1 AND valid_until IS NULL
      AND embedding IS NULL)                                                           AS missing_embedding,
  (SELECT count(*) FROM memory_conflicts WHERE user_id = $1 AND status = 'open')       AS open_conflicts;

-- ---------------------------------------------------------------------------
-- 11 用户级物理删除（GDPR / 用户主动清除）
-- 唯一允许真删的路径。self-FK 是延迟的且在语句末统一校验，父子同删是安全的 ——
-- 前提是这几条 DELETE 在**同一个**事务里。
--   BEGIN;
--   DELETE FROM memory_conflicts WHERE user_id = $1;
--   DELETE FROM episodes    WHERE user_id = $1;
--   DELETE FROM facts       WHERE user_id = $1;
--   DELETE FROM memory_jobs WHERE user_id = $1;
--   COMMIT;
-- ---------------------------------------------------------------------------
