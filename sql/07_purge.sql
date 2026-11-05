-- =============================================================================
-- 生命周期收尾：用户级物理删除（GDPR / 用户主动清除）
--
-- 这是整个系统里**唯一**允许真删的路径。其余一切（冲突、遗忘、过期）都是软失效。
-- 所以它必须难被误调用：
--   · p_confirm 必须与 p_user_id 完全一致（防"手滑传空/传错"）
--   · 一次调用删完所有派生数据（否则会留下孤儿：工作集、任务、血缘、冲突边）
--   · 返回各表删除行数，便于审计与回执
--
-- 注意执行顺序：先断子表引用，再删 episodes/facts。
-- episodes 的自引用外键是 DEFERRABLE INITIALLY DEFERRED，同一条语句里父子同删也安全，
-- 但跨表顺序仍需注意（distill_links / relations 都指向 episodes）。
-- =============================================================================
\set ON_ERROR_STOP on

CREATE OR REPLACE FUNCTION purge_user(
    p_user_id TEXT,
    p_confirm TEXT
) RETURNS TABLE (
    episodes_deleted    BIGINT,
    facts_deleted       BIGINT,
    links_deleted       BIGINT,
    relations_deleted   BIGINT,
    entities_deleted    BIGINT,
    tasks_deleted       BIGINT,
    sets_deleted        BIGINT,
    jobs_deleted        BIGINT,
    conflicts_deleted   BIGINT
)
LANGUAGE plpgsql AS $fn$
DECLARE
    v_ep BIGINT; v_fact BIGINT; v_links BIGINT; v_rel BIGINT; v_ent BIGINT;
    v_task BIGINT; v_set BIGINT; v_job BIGINT; v_conf BIGINT;
    v_ent_tbl BOOLEAN; v_rel_tbl BOOLEAN;
BEGIN
    IF p_confirm IS NULL OR p_confirm IS DISTINCT FROM p_user_id THEN
        RAISE EXCEPTION 'purge_user 需要二次确认：第二个参数必须与 user_id 完全一致（收到 user=%, confirm=%）',
            p_user_id, p_confirm;
    END IF;

    v_ent_tbl := to_regclass('public.entities') IS NOT NULL;
    v_rel_tbl := to_regclass('public.relations') IS NOT NULL;

    -- 1) 血缘（指向 episodes，必须先断）
    DELETE FROM distill_links
    WHERE summary_id IN (SELECT id FROM episodes WHERE user_id = p_user_id)
       OR raw_id     IN (SELECT id FROM episodes WHERE user_id = p_user_id);
    GET DIAGNOSTICS v_links = ROW_COUNT;

    -- 2) 图：关系 → 实体（若图层存在）
    IF v_rel_tbl THEN
        EXECUTE 'DELETE FROM relations WHERE user_id = $1' USING p_user_id;
        GET DIAGNOSTICS v_rel = ROW_COUNT;
    ELSE
        v_rel := 0;
    END IF;
    IF v_ent_tbl THEN
        EXECUTE 'DELETE FROM episode_entities WHERE episode_id IN (SELECT id FROM episodes WHERE user_id = $1)'
            USING p_user_id;
        EXECUTE 'DELETE FROM entities WHERE user_id = $1' USING p_user_id;
        GET DIAGNOSTICS v_ent = ROW_COUNT;
    ELSE
        v_ent := 0;
    END IF;

    -- 3) 任务与工作集（working_sets 挂 tasks，CASCADE 会带走 items）
    DELETE FROM working_sets WHERE user_id = p_user_id;
    GET DIAGNOSTICS v_set = ROW_COUNT;
    DELETE FROM tasks WHERE user_id = p_user_id;   -- CASCADE: task_events
    GET DIAGNOSTICS v_task = ROW_COUNT;

    -- 4) 队列与冲突边
    DELETE FROM memory_jobs WHERE user_id = p_user_id;
    GET DIAGNOSTICS v_job = ROW_COUNT;
    DELETE FROM memory_conflicts WHERE user_id = p_user_id;
    GET DIAGNOSTICS v_conf = ROW_COUNT;

    -- 5) 记忆本体（episodes 的自引用外键延迟到语句末校验，一条语句删完是安全的）
    DELETE FROM episodes WHERE user_id = p_user_id;
    GET DIAGNOSTICS v_ep = ROW_COUNT;
    DELETE FROM facts WHERE user_id = p_user_id;
    GET DIAGNOSTICS v_fact = ROW_COUNT;

    RETURN QUERY SELECT v_ep, v_fact, v_links, v_rel, v_ent, v_task, v_set, v_job, v_conf;
END $fn$;

COMMENT ON FUNCTION purge_user IS
'用户级物理删除（唯一真删路径）。第二参数必须与 user_id 相同作为二次确认。';

-- 试运行：只统计将要删多少，不删
CREATE OR REPLACE FUNCTION purge_user_dryrun(p_user_id TEXT)
RETURNS TABLE (table_name TEXT, rows_to_delete BIGINT)
LANGUAGE sql STABLE AS $fn$
    SELECT 'episodes',  count(*) FROM episodes         WHERE user_id = p_user_id
    UNION ALL SELECT 'facts',     count(*) FROM facts          WHERE user_id = p_user_id
    UNION ALL SELECT 'tasks',     count(*) FROM tasks          WHERE user_id = p_user_id
    UNION ALL SELECT 'working_sets', count(*) FROM working_sets WHERE user_id = p_user_id
    UNION ALL SELECT 'memory_jobs',  count(*) FROM memory_jobs  WHERE user_id = p_user_id
    UNION ALL SELECT 'memory_conflicts', count(*) FROM memory_conflicts WHERE user_id = p_user_id
    UNION ALL SELECT 'distill_links', count(*) FROM distill_links
        WHERE summary_id IN (SELECT id FROM episodes WHERE user_id = p_user_id)
           OR raw_id     IN (SELECT id FROM episodes WHERE user_id = p_user_id)
    ORDER BY 1
$fn$;

DO $$
BEGIN
    RAISE NOTICE 'purge ok：purge_user（需二次确认）/ purge_user_dryrun';
END $$;
