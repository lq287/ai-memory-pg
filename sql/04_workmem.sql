-- =============================================================================
-- 工作记忆层 + 任务状态层
--
-- 设计稿把这两层单独列出来，理由很实际：它们的访问模式**不是检索**。
--   · 任务状态（tasks）      ：按 task_id / user_id 直查，关心 step/status/result
--   · 工作记忆（working_*）  ：为当前任务**装配**一段上下文（显式 pin，不搜索）
--
-- 为什么不塞进 episodes：
--   情景记忆是"可能有用、按语义找"；工作记忆是"就是这些、按顺序给"。
--   混在一起会让每次装配都变成一次召回，既要排序又要预算，还不可复现。
--
-- 为什么落在 PG 而不是 Redis（设计稿推荐运行时状态）：
--   本项目的既定前提是单 PG。而"任务跑到一半进程崩了，重启后还能接着装配同一段上下文"
--   这件事，纯内存做不到、PG 天然做到。代价是多几次 IO —— 装配本来就不是热路径。
--
-- 一条关键语义：**pin 时快照内容，不引用活数据**。
--   底层记忆（episodes）可能被取代、过期、软删除，但一个任务的工作集必须在任务
--   生命周期内保持稳定 —— 否则"同一个任务两次读到不同上下文"会让行为不可复现。
--   所以 working_items.content 存的是快照，ref_id 只是溯源用的软引用（不建外键）。
-- =============================================================================
\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- 1. 任务状态
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      TEXT        NOT NULL,
    kind         TEXT        NOT NULL CHECK (length(kind) BETWEEN 1 AND 40),
    title        TEXT        NOT NULL CHECK (length(title) > 0),
    status       TEXT        NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending','running','blocked','done','failed','cancelled')),
    step         TEXT,                          -- 当前步骤名（状态机由应用定义，这里只管一致性）
    step_index   INT         NOT NULL DEFAULT 0 CHECK (step_index >= 0),
    progress     REAL        NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
    result       JSONB,                         -- 产出（自由结构）
    error        TEXT,
    metadata     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    -- 任务状态也会过期：超过 retention 不再参与"进行中任务"的装配，但仍保留供审计
    expires_at   TIMESTAMPTZ,

    -- 终态必须带 finished_at；非终态不许带
    CONSTRAINT task_terminal_consistency CHECK (
        (status IN ('done','failed','cancelled')) = (finished_at IS NOT NULL)
    ),
    -- failed 必须留 error，便于排查
    CONSTRAINT task_failed_has_error CHECK (status <> 'failed' OR error IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_tasks_user_live
    ON tasks (user_id, updated_at DESC) WHERE status IN ('pending','running','blocked');
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_expires
    ON tasks (expires_at) WHERE expires_at IS NOT NULL;

COMMENT ON TABLE tasks IS '工作流进度：按 task_id/user_id 直查，不参与语义检索';
COMMENT ON COLUMN tasks.step IS '当前步骤名；step_index 用于排序/回退，便于断点续跑';
COMMENT ON COLUMN tasks.expires_at IS '保留期；过期后不再出现在"进行中任务"视图，但行仍保留';

-- 迁移（状态机）守卫：非法迁移直接报错，而不是悄悄写进去
-- 允许的迁移：
--   pending   -> running / blocked / cancelled
--   running   -> running(自环，改 step) / done / failed / blocked / cancelled
--   blocked   -> running / failed / cancelled
--   终态      -> 任何（禁止，只能重开新任务）
CREATE OR REPLACE FUNCTION tasks_guard_transition() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.status = OLD.status THEN
        NEW.updated_at := now();
        RETURN NEW;
    END IF;

    IF OLD.status IN ('done','failed','cancelled') THEN
        RAISE EXCEPTION '任务 % 已是终态 %，不能改成 %（要续做请新开任务）',
            OLD.id, OLD.status, NEW.status;
    END IF;

    IF NOT (
        (OLD.status = 'pending' AND NEW.status IN ('running','blocked','cancelled')) OR
        (OLD.status = 'running' AND NEW.status IN ('done','failed','blocked','cancelled')) OR
        (OLD.status = 'blocked' AND NEW.status IN ('running','failed','cancelled'))
    ) THEN
        RAISE EXCEPTION '非法状态迁移 % -> %（任务 %）', OLD.status, NEW.status, OLD.id;
    END IF;

    NEW.updated_at := now();
    IF NEW.status = 'running' AND NEW.started_at IS NULL THEN
        NEW.started_at := now();
    END IF;
    IF NEW.status IN ('done','failed','cancelled') THEN
        NEW.finished_at := COALESCE(NEW.finished_at, now());
    END IF;
    RETURN NEW;
END $fn$;

DROP TRIGGER IF EXISTS trg_tasks_guard ON tasks;
CREATE TRIGGER trg_tasks_guard
    BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION tasks_guard_transition();

-- ---------------------------------------------------------------------------
-- 2. 任务事件日志（append-only）
-- 与"取代不删除"一致：任务怎么一步步走到现在的，事后要能回答。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_events (
    id         BIGSERIAL PRIMARY KEY,
    task_id    UUID        NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq        INT         NOT NULL DEFAULT 0,
    event      TEXT        NOT NULL CHECK (event IN ('created','status','step','note')),
    status     TEXT,
    step       TEXT,
    payload    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_task_events_task
    ON task_events (task_id, id);

COMMENT ON TABLE task_events IS '任务事件流：只追加，用于回溯"当时怎么做的"';

-- ---------------------------------------------------------------------------
-- 3. 工作记忆：一个任务 / 一次会话的显式上下文
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS working_sets (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      TEXT        NOT NULL,
    task_id      UUID        REFERENCES tasks(id) ON DELETE CASCADE,
    session_id   TEXT,                          -- 宿主会话标识（可空）
    note         TEXT,                          -- 这组上下文的用途
    token_budget INT         NOT NULL DEFAULT 1500 CHECK (token_budget > 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 工作记忆是易失的：默认 24 小时。别让它变成第二份语料库
    expires_at   TIMESTAMPTZ NOT NULL DEFAULT now() + interval '24 hours'
);

CREATE INDEX IF NOT EXISTS idx_ws_user ON working_sets (user_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ws_task
    ON working_sets (task_id) WHERE task_id IS NOT NULL;   -- 一个任务一份工作集
CREATE INDEX IF NOT EXISTS idx_ws_expires ON working_sets (expires_at);

CREATE TABLE IF NOT EXISTS working_items (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    set_id     UUID        NOT NULL REFERENCES working_sets(id) ON DELETE CASCADE,
    kind       TEXT        NOT NULL
                           CHECK (kind IN ('pinned_memory','fact','note','task_state','tool_output')),
    ref_id     UUID,                            -- 溯源用软引用（episodes/facts 的 id），不建外键
    content    TEXT        NOT NULL CHECK (length(content) > 0),   -- ★ pin 时的快照
    tokens     INT         NOT NULL DEFAULT 0 CHECK (tokens >= 0),
    position   INT         NOT NULL DEFAULT 0,  -- 装配顺序由调用方显式决定
    metadata   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wi_set ON working_items (set_id, position, created_at);

COMMENT ON TABLE working_items IS '工作记忆条目：内容是 pin 时的快照，底层记忆后续变化不影响本任务';
COMMENT ON COLUMN working_items.ref_id IS '溯源软引用，不建外键：底层记忆可能已被取代/软删除';

-- ---------------------------------------------------------------------------
-- 4. 操作函数
-- ---------------------------------------------------------------------------

-- 开任务
CREATE OR REPLACE FUNCTION task_start(
    p_user_id TEXT, p_kind TEXT, p_title TEXT,
    p_metadata JSONB DEFAULT '{}'::jsonb,
    p_retention INTERVAL DEFAULT NULL
) RETURNS tasks LANGUAGE plpgsql AS $fn$
DECLARE
    t tasks;
BEGIN
    INSERT INTO tasks (user_id, kind, title, status, metadata, expires_at)
    VALUES (p_user_id, p_kind, p_title, 'pending', COALESCE(p_metadata, '{}'::jsonb),
            CASE WHEN p_retention IS NULL THEN NULL ELSE now() + p_retention END)
    RETURNING * INTO t;

    INSERT INTO task_events (task_id, event, status, payload)
    VALUES (t.id, 'created', 'pending', jsonb_build_object('kind', p_kind, 'title', p_title));
    RETURN t;
END $fn$;

-- 推进任务：status / step / progress / result / error 一次更新，落一条事件
CREATE OR REPLACE FUNCTION task_update(
    p_task_id UUID,
    p_status  TEXT DEFAULT NULL,
    p_step    TEXT DEFAULT NULL,
    p_step_index INT DEFAULT NULL,
    p_progress REAL DEFAULT NULL,
    p_result  JSONB DEFAULT NULL,
    p_error   TEXT DEFAULT NULL
) RETURNS tasks LANGUAGE plpgsql AS $fn$
DECLARE
    t tasks;
    v_event TEXT;
BEGIN
    UPDATE tasks
    SET status     = COALESCE(p_status, status),
        step       = COALESCE(p_step, step),
        step_index = COALESCE(p_step_index, step_index),
        progress   = COALESCE(p_progress, progress),
        result     = COALESCE(p_result, result),
        error      = COALESCE(p_error, error)
    WHERE id = p_task_id
    RETURNING * INTO t;

    IF NOT FOUND THEN
        RAISE EXCEPTION '任务 % 不存在', p_task_id;
    END IF;

    v_event := CASE WHEN p_step IS NOT NULL THEN 'step' ELSE 'status' END;
    INSERT INTO task_events (task_id, event, status, step, payload)
    VALUES (t.id, v_event, t.status, t.step,
            jsonb_strip_nulls(jsonb_build_object(
                'progress', p_progress,
                'result', p_result,
                'error', p_error,
                'step_index', p_step_index)));
    RETURN t;
END $fn$;

-- 取任务 + 最近事件（按键直查，不做任何检索）
--
-- 返回 jsonb 而不是 `tasks` 复合类型，原因很实际：psycopg 默认不认识自定义复合类型，
-- 会把整行变成一个 `(uuid,text,...)` 字符串，Python 侧 t['title'] 直接 TypeError。
-- to_jsonb 是通用出口，任何驱动都能解析。
DROP FUNCTION IF EXISTS task_summary(UUID, INT);
CREATE OR REPLACE FUNCTION task_summary(p_task_id UUID, p_events INT DEFAULT 10)
RETURNS TABLE (task jsonb, events jsonb) LANGUAGE sql STABLE AS $fn$
    SELECT to_jsonb(t),
           COALESCE((
               SELECT jsonb_agg(jsonb_build_object(
                          'event', e.event, 'status', e.status, 'step', e.step,
                          'payload', e.payload, 'at', e.created_at) ORDER BY e.id)
               FROM (
                   SELECT * FROM task_events WHERE task_id = t.id
                   ORDER BY id DESC LIMIT p_events
               ) e
           ), '[]'::jsonb)
    FROM tasks t WHERE t.id = p_task_id
$fn$;

-- 进行中的任务（按最近更新）
CREATE OR REPLACE FUNCTION tasks_live(p_user_id TEXT, p_limit INT DEFAULT 20)
RETURNS SETOF tasks LANGUAGE sql STABLE AS $fn$
    SELECT * FROM tasks
    WHERE user_id = p_user_id
      AND status IN ('pending','running','blocked')
      AND (expires_at IS NULL OR expires_at > now())
    ORDER BY updated_at DESC
    LIMIT p_limit
$fn$;

-- 开工作集（一个任务一份；已经存在就复用）
CREATE OR REPLACE FUNCTION working_open(
    p_user_id TEXT,
    p_task_id UUID DEFAULT NULL,
    p_session_id TEXT DEFAULT NULL,
    p_note TEXT DEFAULT NULL,
    p_token_budget INT DEFAULT 1500,
    p_ttl INTERVAL DEFAULT NULL
) RETURNS working_sets LANGUAGE plpgsql AS $fn$
DECLARE
    ws working_sets;
BEGIN
    IF p_task_id IS NOT NULL THEN
        SELECT * INTO ws FROM working_sets WHERE task_id = p_task_id;
        IF FOUND THEN
            UPDATE working_sets
            SET note = COALESCE(p_note, note),
                token_budget = p_token_budget,
                updated_at = now()
            WHERE id = ws.id
            RETURNING * INTO ws;
            RETURN ws;
        END IF;
    END IF;

    INSERT INTO working_sets (user_id, task_id, session_id, note, token_budget, expires_at)
    VALUES (p_user_id, p_task_id, p_session_id, p_note, p_token_budget,
            now() + COALESCE(p_ttl, interval '24 hours'))
    RETURNING * INTO ws;
    RETURN ws;
END $fn$;

-- 往工作集里 pin 一条（内容快照 + 显式顺序）
CREATE OR REPLACE FUNCTION working_pin(
    p_set_id UUID,
    p_kind   TEXT,
    p_content TEXT,
    p_tokens INT DEFAULT 0,
    p_ref_id UUID DEFAULT NULL,
    p_position INT DEFAULT NULL,
    p_metadata JSONB DEFAULT '{}'::jsonb
) RETURNS working_items LANGUAGE plpgsql AS $fn$
DECLARE
    wi working_items;
    v_pos INT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM working_sets WHERE id = p_set_id) THEN
        RAISE EXCEPTION '工作集 % 不存在', p_set_id;
    END IF;

    v_pos := p_position;
    IF v_pos IS NULL THEN
        SELECT COALESCE(MAX(position), 0) + 1 INTO v_pos
        FROM working_items WHERE set_id = p_set_id;
    END IF;

    INSERT INTO working_items (set_id, kind, ref_id, content, tokens, position, metadata)
    VALUES (p_set_id, p_kind, p_ref_id, p_content, GREATEST(p_tokens, 0), v_pos,
            COALESCE(p_metadata, '{}'::jsonb))
    RETURNING * INTO wi;

    UPDATE working_sets SET updated_at = now() WHERE id = p_set_id;
    RETURN wi;
END $fn$;

-- 清空工作集（换任务/换话题时用；保留工作集本身）
CREATE OR REPLACE FUNCTION working_clear(p_set_id UUID) RETURNS INT
LANGUAGE plpgsql AS $fn$
DECLARE n INT;
BEGIN
    DELETE FROM working_items WHERE set_id = p_set_id;
    GET DIAGNOSTICS n = ROW_COUNT;
    UPDATE working_sets SET updated_at = now() WHERE id = p_set_id;
    RETURN n;
END $fn$;

-- 过期清理：工作记忆是易失的，过期即删（与 actors 记忆的"取代不删除"相反 ——
-- 这里本来就只是临时的上下文缓存，留着只会变成第二份脏语料）
CREATE OR REPLACE FUNCTION working_expire() RETURNS TABLE (sets_deleted BIGINT, tasks_expired BIGINT)
LANGUAGE plpgsql AS $fn$
DECLARE
    v_sets BIGINT;
    v_tasks BIGINT;
BEGIN
    DELETE FROM working_sets WHERE expires_at <= now();
    GET DIAGNOSTICS v_sets = ROW_COUNT;

    -- 任务只清 expires_at（让它们不再出现在"进行中"），行保留供审计
    UPDATE tasks
    SET expires_at = now()
    WHERE expires_at IS NOT NULL AND expires_at > now() AND status IN ('done','failed','cancelled');
    GET DIAGNOSTICS v_tasks = ROW_COUNT;

    RETURN QUERY SELECT v_sets, v_tasks;
END $fn$;

-- ---------------------------------------------------------------------------
-- 5. 一句话校验
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    RAISE NOTICE 'workmem ok：tasks / task_events / working_sets / working_items';
END $$;
