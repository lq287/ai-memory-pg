/**
 * 插件自检：不启动 DSH，直接验 sidecar 通道与插件的参数转发。
 *
 *   node test/smoke.mjs [projectRoot]
 *
 * 为什么要有它：插件最脆弱的地方不是"能不能加载"，而是
 *   ① 子进程能不能起来（PATH / PYTHONPATH / .pylibs 装没装）
 *   ② 请求/失效/重启的边界（进程死了要能自愈，不能让会话卡住）
 *   ③ 工具参数有没有被正确转发到 Python 端（拼错一个键，模型就静默拿不到数据）
 * 这三件事都能在没有 DSH 的情况下测掉。
 *
 * 退出码：0 全过；1 有失败。
 */

import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { MemorySidecar } from "../lib/sidecar.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = process.argv[2] || path.resolve(here, "..", "..");

let pass = 0;
const fail = [];

function check(name, ok, detail = "") {
  if (ok) {
    pass += 1;
    console.log(`  [PASS] ${name}`);
  } else {
    fail.push(name);
    console.log(`  [FAIL] ${name}  ${detail}`);
  }
}

const log = (line) => {
  if (process.env.SMOKE_VERBOSE) console.error(`    · ${line}`);
};

const sidecar = new MemorySidecar({ projectRoot, userId: "owner", log });

try {
  console.log(`\n=== dsh-memory-pg 自检 (projectRoot=${projectRoot}) ===\n`);

  console.log("[1] 通道");
  const ping = await sidecar.request("ping", {});
  check("ping 返回 pong + 子进程 pid", ping?.ok === true && typeof ping.pid === "number", JSON.stringify(ping));
  check("embedder 名字非空", typeof ping.embedder === "string" && ping.embedder.length > 0, ping.embedder);

  console.log("\n[2] 只读操作");
  const health = await sidecar.request("health", {});
  check("health 有规模与健康度", typeof health.episodes_live === "number" && !!health.quality, JSON.stringify(health).slice(0, 120));
  const ctx = await sidecar.request("context", { max_chars: 400 });
  check("context 返回文本或显式 empty", typeof ctx.text === "string" && typeof ctx.empty === "boolean");
  check("context 遵守字符预算", ctx.text.length <= 420, `len=${ctx.text.length}`);

  console.log("\n[3] 召回");
  const recall = await sidecar.request("recall", { query: "记忆库", limit: 2 });
  check("recall 返回 hits 数组", Array.isArray(recall.hits), JSON.stringify(recall).slice(0, 120));
  check("hits 每条含 id/score/paths", recall.hits.every((h) => h.id && typeof h.score === "number" && /^[vkg-]{3}$/.test(h.paths)));

  console.log("\n[4] 参数转发（拼错键会静默返回空，所以逐个验）");
  const facts = await sidecar.request("fact_get", { keys: ["mem.cli.entry"] });
  check("fact_get 按键命中", Array.isArray(facts.facts) && facts.facts.length === 1, JSON.stringify(facts).slice(0, 120));
  const digests = await sidecar.request("digest_show", { limit: 2 });
  check("digest_show 返回数组", Array.isArray(digests.digests));

  console.log("\n[5] 错误处理");
  let caught = null;
  try {
    await sidecar.request("definitely_not_an_op", {});
  } catch (err) {
    caught = err;
  }
  check("未知操作抛错而不是挂住", !!caught && /未知操作/.test(caught.message), String(caught));

  console.log("\n[6] 子进程死了能自愈");
  const pid = sidecar.child?.pid;
  check("拿到当前子进程 pid", typeof pid === "number", String(pid));
  sidecar.child.kill();
  await new Promise((r) => setTimeout(r, 400));
  const ping2 = await sidecar.request("ping", {});
  check("杀掉后下一次请求自动重启", ping2?.ok === true && ping2.pid !== pid, `${pid} -> ${ping2?.pid}`);
} catch (err) {
  fail.push(`未捕获异常: ${err?.message || err}`);
  console.log(`\n[FAIL] 未捕获异常: ${err?.stack || err}`);
} finally {
  await sidecar.dispose();
}

console.log("\n" + "=".repeat(52));
if (fail.length) {
  console.log(` 失败 ${fail.length} 项 / 通过 ${pass} 项`);
  for (const f of fail) console.log(`   · ${f}`);
  process.exit(1);
}
console.log(` 全部通过：${pass} 项断言`);
process.exit(0);
