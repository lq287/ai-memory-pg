// 本地 SQL 验证夹具：在 PGlite（真实 PostgreSQL 编译成 WASM）里按顺序执行
// sql/01_schema.sql → 02_retrieval.sql → 03_write.sql → 99_selftest.sql，
// 然后跑一组"取证查询"，把关键数值打印出来。
//
// 为什么用这个而不是 Docker：不需要管理员权限、不需要后台服务、不占端口，
// 而且 PGlite 内置 pgvector 与 pg_trgm —— 恰好是这套 schema 依赖的两个扩展。
// 它验证的是 SQL 的真实语义（约束、plpgsql 函数、RRF 算法、衰减公式），
// 但不能替代生产环境的压测、执行计划调优和 pgvector 版本差异确认。
//
// 为什么还要"取证"：PGlite 不把 RAISE NOTICE 透出来（results 里没有 messages 字段），
// 所以 99_selftest.sql 的 PASS 只能靠"不抛异常"来体现。为了看得见数字，
// 这里补一段自己算、自己打印的诊断查询。
//
//   npm run check
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const { PGlite } = await import("@electric-sql/pglite");

// ---------------------------------------------------------------------------
// 扩展：必须在 CREATE EXTENSION 之前登记
// pgvector 从 0.3 起拆成独立包；pg_trgm 仍在主包的 contrib 里
// 两种导出形状都要认：工厂函数，或 { name, setup } 对象
// ---------------------------------------------------------------------------
const extensions = {};
for (const [key, spec] of [
  ["vector", "@electric-sql/pglite-pgvector"],
  ["pg_trgm", "@electric-sql/pglite/contrib/pg_trgm"],
]) {
  try {
    const mod = await import(spec);
    const factory = mod[key] ?? mod.default?.[key] ?? mod.default;
    const usable =
      typeof factory === "function" ||
      (factory &&
        typeof factory.setup === "function" &&
        typeof factory.name === "string");
    if (usable) extensions[key] = factory;
    else console.warn(`[warn] ${spec} 没有导出可用的扩展工厂，跳过`);
  } catch (err) {
    console.warn(`[warn] 加载 ${spec} 失败：${err.message}`);
  }
}

// psql 的元命令（\set / \pset / \echo）不是 SQL，PGlite 会解析失败 —— 剔除。
// 只影响 psql 的显示与错误处理策略，不影响任何一条 SQL 语句本身。
const stripPsqlMeta = (sql) =>
  sql
    .split(/\r?\n/)
    .filter((line) => !/^\s*\\/.test(line))
    .join("\n");

const line = (ch = "─") => console.log(ch.repeat(78));

// ---------------------------------------------------------------------------
// 阶段一：按顺序执行四份 SQL
// ---------------------------------------------------------------------------
const db = await new PGlite({ extensions });

const meta = await db.query(
  `SELECT version() AS version,
          (SELECT extversion FROM pg_extension WHERE extname = 'vector')  AS vector,
          (SELECT extversion FROM pg_extension WHERE extname = 'pg_trgm') AS pg_trgm`
);
line("═");
console.log("宿主：", meta.rows[0].version.split(" on ")[0]);
line("═");

const FILES = [
  "sql/01_schema.sql",
  "sql/02_retrieval.sql",
  "sql/03_write.sql",
  "sql/04_workmem.sql",
  "sql/05_distill.sql",
  "sql/06_graph.sql",
  "sql/07_purge.sql",
  "sql/08_digest.sql",
  "sql/99_selftest.sql",
];

let failures = 0;

for (const file of FILES) {
  const sql = stripPsqlMeta(readFileSync(resolve(ROOT, file), "utf8"));
  const started = Date.now();
  process.stdout.write(`\n▶ ${file} ... `);
  try {
    await db.exec(sql);
    console.log(`OK (${Date.now() - started} ms)`);
  } catch (err) {
    failures += 1;
    console.log("FAILED");
    console.error(`\n  ✗ ${file}: ${err.message}\n`);
    break; // 后面的脚本依赖前面的对象，继续跑只会产生噪音
  }
}

if (failures) {
  line("═");
  console.log(`结果：失败（${failures} 个文件）`);
  await db.close();
  process.exit(1);
}

// ---------------------------------------------------------------------------
// 阶段二：取证
// ---------------------------------------------------------------------------
const dim = (
  await db.query(
    `SELECT atttypmod AS d FROM pg_attribute
     WHERE attrelid = 'episodes'::regclass AND attname = 'embedding'`
  )
).rows[0].d;

await db.exec(`
CREATE OR REPLACE FUNCTION pg_temp.onehot(pos int, dim int) RETURNS vector AS $fn$
    SELECT ('[' || string_agg(CASE WHEN i = pos THEN '1' ELSE '0' END, ',' ORDER BY i) || ']')::vector
    FROM generate_series(0, dim - 1) i
$fn$ LANGUAGE sql IMMUTABLE;
`);

// --- 取证 1：pg_trgm 到底怎么切中文 -----------------------------------------
line("═");
console.log("① pg_trgm 对中文短查询到底做了什么（这是关键词路能不能用的前提）");
line();
const trgm = (
  await db.query(`
    SELECT show_trgm('时区')     AS cn,
           show_trgm('时间安排')  AS cn4,
           show_trgm('timezone') AS en
  `)
).rows[0];
for (const [k, v] of Object.entries(trgm)) console.log(`  show_trgm(${k}): ${v.join(" ")}`);

const sims = (
  await db.query(`
    SELECT
      similarity('时区', '用户的时区是上海')                          AS cn_full,
      word_similarity('时区', '用户的时区是上海')                      AS cn_word,
      strict_word_similarity('时区', '用户的时区是上海')               AS cn_strict,
      similarity('timezone', 'my timezone setting needs a yearly review')        AS en_full,
      word_similarity('timezone', 'my timezone setting needs a yearly review')   AS en_word
  `)
).rows[0];
line();
console.log("  查询 '时区' vs 记忆 '用户的时区是上海'");
console.log(`    similarity=${sims.cn_full.toFixed(3)}  word_similarity=${sims.cn_word.toFixed(3)}  strict=${sims.cn_strict.toFixed(3)}`);
console.log("  查询 'timezone' vs 长英文记忆");
console.log(`    similarity=${sims.en_full.toFixed(3)}  word_similarity=${sims.en_word.toFixed(3)}`);
console.log("  => 中文短查询的两个 similarity 都是 0，%，<% 都召回不到；英文靠 <% 就能命中");

// --- 取证 2：新旧关键词判据在同一批数据上的条数 -----------------------------
line("═");
console.log("② 新旧关键词判据的实际召回条数（中文/英文各一批）");
line();
await db.exec(`
INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source)
VALUES
  ('ev:cn', '用户的时区是 Asia/Shanghai', '时区上海', pg_temp.onehot(0, ${dim}), 'ev', now(), 'conversation'),
  ('ev:cn', '用户每周五下午做周报',       '周报',     pg_temp.onehot(1, ${dim}), 'ev', now(), 'conversation'),
  ('ev:cn', '用户偏好深色主题',           '深色主题', pg_temp.onehot(2, ${dim}), 'ev', now(), 'conversation'),
  ('ev:en', 'my timezone is Asia/Shanghai and I set it manually', 'timezone', pg_temp.onehot(3, ${dim}), 'ev', now(), 'conversation'),
  ('ev:en', 'I write the weekly report on Friday afternoons',      'weekly report', pg_temp.onehot(4, ${dim}), 'ev', now(), 'conversation');
`);

for (const [user, q] of [
  ["ev:cn", "时区"],
  ["ev:cn", "周报"],
  ["ev:en", "timezone"],
]) {
  const r = (
    await db.query(
      `SELECT
         count(*) FILTER (WHERE content % $2)                        AS old_pct,
         count(*) FILTER (WHERE $2 <% content)                       AS old_wordop,
         count(*) FILTER (WHERE strpos(content, $2) > 0)             AS substr,
         count(*) FILTER (WHERE strpos(content, $2) > 0 OR $2 <% content) AS new_pred
       FROM episodes WHERE user_id = $1 AND valid_until IS NULL`,
      [user, q]
    )
  ).rows[0];
  console.log(
    `  [${user}] 查询="${q}"  content%q=${r.old_pct}  q<%content=${r.old_wordop}` +
      `  strpos=${r.substr}  新判据=${r.new_pred}`
  );
}

// --- 取证 3：RRF 的两路贡献与衰减下限 --------------------------------------
line("═");
console.log("③ 混合召回：两路各自的名次、RRF 分、衰减后的最终分");
line();
const rec = (
  await db.query(
    `SELECT summary, snippet, rk_v, rk_k, rrf_score, age_days, score
     FROM recall_episodes(pg_temp.onehot(0, ${dim}), 'ev:cn', '时区',
                          p_vec_k => 2, p_kw_k => 5, p_limit => 5)`
  )
).rows;
console.log("  summary".padEnd(16) + "rk_v".padEnd(6) + "rk_k".padEnd(6) + "rrf".padEnd(10) + "age(d)".padEnd(9) + "score");
for (const r of rec) {
  console.log(
    "  " +
      String(r.summary ?? r.snippet).padEnd(14) +
      String(r.rk_v ?? "-").padEnd(6) +
      String(r.rk_k ?? "-").padEnd(6) +
      r.rrf_score.toFixed(6).padEnd(10) +
      r.age_days.toFixed(0).padEnd(9) +
      r.score.toFixed(6)
  );
}

// 高斯衰减曲线：offset=180d 内不衰减，offset+scale 处半衰
const decay = (
  await db.query(`
    WITH cfg AS (SELECT 180.0::float8 AS off, 1825.0::float8 AS scl, 0.5::float8 AS dec),
         ages AS (SELECT unnest(ARRAY[0, 90, 180, 365, 730, 2005, 3650]) AS age)
    SELECT age,
           round(power(dec, power(GREATEST(age - off, 0.0) / scl, 2))::numeric, 4) AS factor
    FROM cfg, ages ORDER BY age
  `)
).rows;
line();
console.log("  高斯衰减系数（offset=180d / scale=1825d / decay=0.5）：");
console.log("    " + decay.map((r) => `${r.age}d=${r.factor}`).join("  "));
console.log("  => 半年内几乎不动；5.5 年才到 0.5。对比上一版的「指数半衰期 90 天」：");
console.log("     90d=0.50  365d=0.06  3650d=0.0001 —— 老案例会被直接判死");

const fut = (
  await db.query(`
    SELECT GREATEST(EXTRACT(EPOCH FROM (now() - (now() + interval '30 days')))::float8, 0.0)
             / 86400.0 AS age_days_clamped
  `)
).rows[0];
console.log(`  未来 30 天的日程：age_days 被钳到 ${fut.age_days_clamped}（不钳就是 -30，高斯项会把未来放大）`);

// --- 取证 4：失效链 --------------------------------------------------------
line("═");
console.log("④ 失效链：旧行留在表里，只是被闭合 + 指向接替者");
line();
await db.exec(`
INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source)
VALUES ('ev:chain', '用户时区为 Asia/Tokyo', 'T', pg_temp.onehot(5, ${dim}), 'ev', now() - interval '10 days', 'conversation');
INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source)
VALUES ('ev:chain', '用户时区为 Asia/Shanghai', 'S', pg_temp.onehot(6, ${dim}), 'ev', now(), 'conversation');

UPDATE episodes
SET valid_until = now(),
    superseded_by = (SELECT id FROM episodes WHERE user_id = 'ev:chain' AND summary = 'S')
WHERE user_id = 'ev:chain' AND summary = 'T' AND valid_until IS NULL;
`);
const chain = (
  await db.query(
    `SELECT summary, content,
            (valid_until IS NULL) AS is_current,
            (valid_until IS NULL) AS open_ended,
            superseded_by IS NOT NULL AS has_successor
     FROM episodes WHERE user_id = 'ev:chain' ORDER BY ingested_at`
  )
).rows;
for (const r of chain) {
  console.log(
    `  ${r.summary}  ${r.is_current ? "当前" : "已失效"}  ` +
      `valid_until=${r.open_ended ? "NULL(未失效)" : "已闭合"}  有接替者=${r.has_successor}`
  );
}
const hist = (
  await db.query(
    `SELECT count(*)::int AS rows_total,
            count(*) FILTER (WHERE valid_until IS NULL)::int AS rows_current
     FROM episodes WHERE user_id = 'ev:chain'`
  )
).rows[0];
console.log(`  物理行数=${hist.rows_total}  当前有效=${hist.rows_current}  => 历史没丢，只是不参与召回`);

// --- 取证 5：事实表唯一性 --------------------------------------------------
line("═");
console.log("⑤ 事实表：同一个 key 只能有一条当前值（靠部分唯一索引强制）");
line();
await db.exec(`
INSERT INTO facts (user_id, key, value, confidence, source)
VALUES ('ev:fact', 'timezone', '"Asia/Tokyo"', 0.8, 'conversation');
`);
// 新行 id 先算出来，两处复用：悬空的 superseded_by 只会在 COMMIT 被延迟外键抓到
const newFactId = randomUUID();
let blocked = false;
try {
  await db.exec(`
    INSERT INTO facts (user_id, key, value, confidence, source)
    VALUES ('ev:fact', 'timezone', '"Asia/Shanghai"', 0.9, 'conversation');
  `);
} catch (err) {
  blocked = /unique|duplicate|重复/i.test(`${err.message} ${err.code ?? ""}`);
  console.log(`  第二条当前值被拒绝：${blocked ? "是（unique_violation）" : err.message}`);
}
if (!blocked) {
  failures += 1;
  console.log("  ✗ 部分唯一索引没生效");
} else {
  await db.exec(`
    UPDATE facts SET valid_until = now(), superseded_by = '${newFactId}'::uuid
    WHERE user_id = 'ev:fact' AND key = 'timezone' AND valid_until IS NULL;
    INSERT INTO facts (id, user_id, key, value, confidence, source)
    VALUES ('${newFactId}'::uuid, 'ev:fact', 'timezone', '"Asia/Shanghai"', 0.9, 'human');
  `);
  const f = (
    await db.query(`
      SELECT count(*)::int AS total,
             count(*) FILTER (WHERE valid_until IS NULL)::int AS current,
             (SELECT value FROM facts WHERE user_id='ev:fact' AND key='timezone' AND valid_until IS NULL) AS now_value
      FROM facts WHERE user_id = 'ev:fact' AND key = 'timezone'
    `)
  ).rows[0];
  console.log(`  先失效后插入：共 ${f.total} 行，当前 ${f.current} 行，当前值=${JSON.stringify(f.now_value)}`);
}

// --- 取证 6：分类型 TTL 与生命周期 ----------------------------------------
line("═");
console.log("⑥ 分类型 TTL：写入时由触发器按 kind 落到 expires_at，查询层过滤");
line();
const ttl = (
  await db.query(`
    SELECT ttl_interval('raw_turn')  AS raw_turn,
           ttl_interval('episode')   AS episode,
           ttl_interval('summary')   AS summary,
           ttl_interval('longterm')  AS longterm
  `)
).rows[0];
console.log(
  `  策略：raw_turn=${ttl.raw_turn}  episode=${ttl.episode}  ` +
    `summary=${ttl.summary}  longterm=${ttl.longterm ?? "不按时间过期"}`
);

await db.exec(`
INSERT INTO episodes (user_id, kind, content, summary, embedding, embedding_model, event_time, source)
VALUES ('ev:ttl', 'raw_turn', '用户问了一句日志在哪', '日志', pg_temp.onehot(0, ${dim}), 'ev', now(), 'conversation'),
       ('ev:ttl', 'episode',  '上周在杭州开了评审会',  '评审会', pg_temp.onehot(1, ${dim}), 'ev', now(), 'conversation'),
       ('ev:ttl', 'summary',  '偏好汇总：深色主题',    '汇总', pg_temp.onehot(2, ${dim}), 'ev', now(), 'conversation');
`);
const rows = (
  await db.query(`
    SELECT kind, expires_at,
           CASE WHEN expires_at IS NULL THEN NULL
                ELSE round(EXTRACT(EPOCH FROM (expires_at - ingested_at)) / 86400)::int END AS ttl_days,
           lifecycle_of(kind, ingested_at) AS lifecycle
    FROM episodes WHERE user_id = 'ev:ttl' ORDER BY kind
  `)
).rows;
for (const r of rows) {
  console.log(`  ${r.kind.padEnd(9)} ttl_days=${String(r.ttl_days ?? "-").padEnd(6)} lifecycle=${r.lifecycle}`);
}
// 生命周期是派生的：同样是 episode，80 天前是 faded，100 天前是 expired
const lc = (
  await db.query(`
    SELECT lifecycle_of('episode', now() - interval '10 days')  AS d10,
           lifecycle_of('episode', now() - interval '60 days')  AS d60,
           lifecycle_of('episode', now() - interval '80 days')  AS d80,
           lifecycle_of('episode', now() - interval '100 days') AS d100
  `)
).rows[0];
console.log(`  派生生命周期（kind=episode, TTL 90 天）：10d=${lc.d10} 60d=${lc.d60} 80d=${lc.d80} 100d=${lc.d100}`);

// 过期不等于删除：把一行的 expires_at 拨到过去，它立刻从召回里消失，但行还在
await db.exec(`
UPDATE episodes SET expires_at = now() - interval '1 day'
WHERE user_id = 'ev:ttl' AND kind = 'raw_turn';
`);
const gone = (
  await db.query(`
    SELECT (SELECT count(*)::int FROM episodes WHERE user_id='ev:ttl') AS rows_total,
           (SELECT count(*)::int FROM recall_episodes(
                pg_temp.onehot(0, ${dim}), 'ev:ttl', '日志', p_vec_k => 10, p_kw_k => 10, p_limit => 10)) AS recalled
  `)
).rows[0];
console.log(`  拨过期一条：物理行数=${gone.rows_total}，其中被召回=${gone.recalled} => 过期只影响召回，不删数据`);

// --- 取证 7：defer / 冲突裁决 --------------------------------------------
line("═");
console.log("⑦ defer：待裁决先挂起（不召回），裁决后才取代");
line();
await db.exec(`
INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source)
VALUES ('ev:defer', '用户的时区是 Asia/Tokyo', 'Tokyo', pg_temp.onehot(0, ${dim}), 'ev', now(), 'conversation');

INSERT INTO episodes (user_id, content, summary, embedding, embedding_model, event_time, source, review_state)
VALUES ('ev:defer', '用户的时区是 Asia/Shanghai', 'Shanghai', pg_temp.onehot(0, ${dim}), 'ev', now(), 'conversation', 'pending');
`);
const before = (
  await db.query(
    `SELECT count(*)::int AS recalled FROM recall_episodes(
        pg_temp.onehot(0, ${dim}), 'ev:defer', '时区', p_vec_k => 10, p_kw_k => 10, p_limit => 10)`
  )
).rows[0];
console.log(`  裁决前召回条数=${before.recalled}（pending 行被过滤，只剩下旧值）`);

await db.exec(`
INSERT INTO memory_conflicts (user_id, existing_id, incoming_id, reason, signal)
SELECT 'ev:defer',
       (SELECT id FROM episodes WHERE user_id='ev:defer' AND content LIKE '%Tokyo%'),
       (SELECT id FROM episodes WHERE user_id='ev:defer' AND content LIKE '%Shanghai%'),
       'value_conflict', '{"src":"sqlcheck"}'::jsonb;
`);
const conflict = (
  await db.query(
    `SELECT id, existing_id, incoming_id FROM memory_conflicts WHERE user_id = 'ev:defer' AND status = 'open'`
  )
).rows[0];
const resolved = (
  await db.query(`SELECT * FROM resolve_conflict($1, 'superseded', 'sqlcheck')`, [conflict.id])
).rows[0];
const after = (
  await db.query(
    `SELECT count(*)::int AS recalled FROM recall_episodes(
        pg_temp.onehot(0, ${dim}), 'ev:defer', '时区', p_vec_k => 10, p_kw_k => 10, p_limit => 10)`
  )
).rows[0];
const state = (
  await db.query(
    `SELECT (SELECT count(*)::int FROM episodes WHERE id = $1 AND valid_until IS NULL) AS old_open,
            (SELECT review_state FROM episodes WHERE id = $2) AS new_review,
            (SELECT status || '/' || resolution FROM memory_conflicts WHERE id = $3) AS edge`,
    [conflict.existing_id, conflict.incoming_id, conflict.id]
  )
).rows[0];
console.log(`  裁决后召回条数=${after.recalled}（新值转正、旧值退出）`);
console.log(
  `  旧行仍有效=${state.old_open === 1}  新行评审状态=${state.new_review}  冲突边=${state.edge}`
);
console.log("  => 旧行留在表里（血缘可追），只是不再参与召回");

// --- 清理 ----------------------------------------------------------------
await db.exec(`
DELETE FROM memory_conflicts WHERE user_id LIKE 'ev:%';
DELETE FROM episodes WHERE user_id LIKE 'ev:%';
DELETE FROM facts    WHERE user_id LIKE 'ev:%';
`);

// --- 汇总：对象清单 -------------------------------------------------------
line("═");
const ext = (await db.query(`SELECT extname, extversion FROM pg_extension ORDER BY 1`)).rows;
const objects = (
  await db.query(`
    SELECT c.relname AS name,
           CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' ELSE 'index' END AS kind
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r','v','i') AND c.relname NOT LIKE 'pg_%'
    ORDER BY kind DESC, name`)
).rows;
const procs = (
  await db.query(`
    SELECT p.proname AS name FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public' AND p.prokind = 'f'
      -- 排除扩展自带的函数，只看本项目自己定义的
      AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
    ORDER BY 1`)
).rows.map((r) => r.name);

console.log("扩展：" + ext.map((r) => `${r.extname} ${r.extversion}`).join("  "));
console.log("自定义函数：" + procs.join(", "));
console.log("对象：" + objects.map((r) => `${r.kind}:${r.name}`).join("  "));
line("═");
if (failures) {
  console.log(`结果：失败 ${failures} 处`);
  process.exitCode = 1;
} else {
  console.log("结果：8 份 SQL 全部执行通过 + 7 组取证；99_selftest.sql 的 19 组断言一条不成立就会抛异常");
}
line("═");

await db.close();
