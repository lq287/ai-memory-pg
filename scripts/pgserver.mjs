// 把 PGlite 当"真的 Postgres 服务"用：PGlite + pglite-socket 在本机 127.0.0.1:55432
// 上开一个 PG wire 协议端口，于是 psycopg / psql / 本项目的 app/*.py 可以照常连。
//
//   node scripts/pgserver.mjs            # 前台常驻，Ctrl+C 退出
//   node scripts/pgserver.mjs --no-schema  # 只起服务，不预装 schema
//
// 这是**开发/验证用**的服务器：内存库、单进程、没有认证、性能与真正的 PG 无可比性。
// 生产请用 docker-compose.yml 里那个 pgvector/pgvector 镜像。
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const PORT = Number(process.env.MEM_PGLITE_PORT ?? 55432);
const HOST = process.env.MEM_PGLITE_HOST ?? "127.0.0.1";
const withSchema = !process.argv.includes("--no-schema");

const { PGlite } = await import("@electric-sql/pglite");
const { PGLiteSocketServer } = await import("@electric-sql/pglite-socket");

const extensions = {};
for (const [key, spec] of [
  ["vector", "@electric-sql/pglite-pgvector"],
  ["pg_trgm", "@electric-sql/pglite/contrib/pg_trgm"],
]) {
  const mod = await import(spec);
  const factory = mod[key] ?? mod.default?.[key] ?? mod.default;
  if (typeof factory === "function" || factory?.setup) extensions[key] = factory;
}

const db = new PGlite({ extensions });

const stripPsqlMeta = (sql) =>
  sql
    .split(/\r?\n/)
    .filter((line) => !/^\s*\\/.test(line))
    .join("\n");

if (withSchema) {
  // 顺序有依赖：06_graph 依赖 episodes；07_purge 用 to_regclass 兜底，放最后
  for (const f of [
    "sql/01_schema.sql",
    "sql/02_retrieval.sql",
    "sql/03_write.sql",
    "sql/04_workmem.sql",
    "sql/05_distill.sql",
    "sql/06_graph.sql",
    "sql/07_purge.sql",
    "sql/08_digest.sql",
  ]) {
    process.stdout.write(`▶ 装载 ${f} ... `);
    await db.exec(stripPsqlMeta(readFileSync(resolve(ROOT, f), "utf8")));
    console.log("OK");
  }
}

const version = (await db.query("SELECT version() AS v")).rows[0].v.split(" on ")[0];
const server = new PGLiteSocketServer({ db, port: PORT, host: HOST, maxConnections: 5 });
await server.start();

console.log(`
────────────────────────────────────────────────────────────────
可用：${version} （PGlite，内存库，仅开发/验证用）
监听：postgresql://postgres@${HOST}:${PORT}/postgres

  PowerShell:
    $env:MEM_PG_DSN = "postgresql://postgres@${HOST}:${PORT}/postgres"
    python -m app.demo
    python -m app.worker --once

  psql:
    psql "postgresql://postgres@${HOST}:${PORT}/postgres"

Ctrl+C 退出（数据随之丢失）
────────────────────────────────────────────────────────────────`);

const shutdown = async () => {
  console.log("\n关闭中…");
  await server.stop();
  await db.close();
  process.exit(0);
};
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
