#!/usr/bin/env node
/**
 * 修复 dsh 会话日志里"升级到会话格式 v4 之后读不动"的历史行。
 *
 * 背景（2026-11-10 实测）：dsh 0.2.x 把会话格式升到 v4，v4 的准入校验要求
 * 每条消息的 source 是"producer-owned"（`dsh-session-format-v3-to-v4` 里的
 * `source()`）：kind 必须是非空字符串，且不能是字面量 "plugin"。
 *
 * 但升级前由第一方插件（tool-jobs / compact / user-approval /
 * @deepseek-ai/dsh-system-prompt …）写下的行，source 形状是旧的
 * `{ kind: "plugin", plugin: "<插件名>" }`。于是一打开那个会话就抛：
 *
 *     format v4 message requires a producer-owned source kind
 *
 * 这跟装了什么第三方插件无关 —— 是日志本身不合法。本脚本把旧行改写成
 * v4 认的形状（`{ kind: "<producer kind>" }`，其余字段原样保留），
 * 让老会话还能继续开。
 *
 * 用法：
 *   node scripts/repair_session_v4.mjs                      # 只体检（默认 dry-run）
 *   node scripts/repair_session_v4.mjs --session <id>       # 只体检某个会话
 *   node scripts/repair_session_v4.mjs --apply              # 真改（先自动 .bak 备份）
 *
 * ⚠️ 必须在**关掉对应会话**之后跑（服务进程会继续往日志尾部追加帧，
 *    边写边改会让偏移对不上）。改完重启 dsh，再打开那个会话。
 */

import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { zstdDecompressSync, zstdCompressSync } from "node:zlib";

const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);

/**
 * 官方 v3→v4 迁移用的两张表（抄自 dsh-session-format-v3-to-v4）：
 * 少数第一方插件在 v4 里换了名字，其余第一方插件保持同名。
 * 认不出来的第三方插件走 `plugin:<name>`，同样满足"非空且不是 plugin"。
 */
const RENAMED_PRODUCERS = {
  compact: "compact-basic",
  "tool-jobs": "tool-jobs",
  "@deepseek-ai/dsh-system-prompt": "runtime-context",
};
const SAME_NAME_PRODUCERS = new Set([
  "agent-instructions",
  "session-reference",
  "team-message",
  "goal",
  "skill-invocation",
  "skill-catalog",
  "coordinator",
  "subagent-report",
  "subagent-settled",
  "webhook",
  "agent-message",
  "model-selection",
  "plan-mode",
  "time-context",
  "tmux-context",
  "user-approval",
  "repeat-tool-reminder",
  "tool-cordis",
  "cordis-host-runner",
  "tool-goal",
  "tool-jobs",
  "hooks-codex",
  "hooks-claude-code",
  "schedule",
  "dsh-session-title-llm",
]);

/** 旧 `plugin` 名 → v4 的 producer kind。 */
function producerKind(plugin, role) {
  if (plugin === "@deepseek-ai/dsh-system-prompt" && role === "system") return "system-prompt";
  if (Object.hasOwn(RENAMED_PRODUCERS, plugin)) return RENAMED_PRODUCERS[plugin];
  if (SAME_NAME_PRODUCERS.has(plugin)) return plugin;
  return `plugin:${plugin}`;
}

/** 多帧 zstd 按 magic 切开；每帧单独解压，解不动的帧原样保留（尾部可能写了半帧）。 */
function readFrames(buf) {
  const offsets = [];
  for (let i = 0; i + 4 <= buf.length; i++) {
    if (buf.compare(MAGIC, 0, 4, i, i + 4) === 0) offsets.push(i);
  }
  const frames = [];
  for (let k = 0; k < offsets.length; k++) {
    const start = offsets[k];
    const end = k + 1 < offsets.length ? offsets[k + 1] : buf.length;
    const seg = buf.subarray(start, end);
    let text = null;
    try {
      text = zstdDecompressSync(seg).toString("utf8");
    } catch {
      text = null;
    }
    frames.push({ raw: seg, text });
  }
  return frames;
}

/** 把一行事件里所有"消息槽位"的 source 改写成 v4 形状；返回改了几处。 */
function rewriteRow(row) {
  let changed = 0;
  const fix = (message, role) => {
    if (!message || typeof message !== "object") return;
    const src = message.source;
    if (!src || typeof src !== "object" || src.kind !== "plugin") return;
    const plugin = typeof src.plugin === "string" ? src.plugin : "";
    if (!plugin) return;
    const { plugin: _drop, ...rest } = src;
    rest.kind = producerKind(plugin, role);
    message.source = rest;
    changed++;
  };

  if (row.type === "user/message") fix(row.data, "user");
  else if (row.type === "system/message" || row.type === "developer/message") fix(row.data?.message, "system");
  else if (row.type === "assistant/message") fix(row.data?.message, "assistant");
  else if (row.type === "tool/result") fix(row.data?.message, "tool");
  else if (row.type === "agent/inbox/spliced" && Array.isArray(row.data?.inserted)) {
    for (const m of row.data.inserted) fix(m, "user");
  } else if (row.type === "session/title-llm-request" && Array.isArray(row.data?.messages)) {
    for (const m of row.data.messages) fix(m, "user");
  }
  return changed;
}

/** v4 准入规则的本地镜像（kind：非空字符串、且不是 "plugin"）。 */
function violatesV4(message) {
  const src = message?.source;
  return !src || typeof src !== "object" || typeof src.kind !== "string" || src.kind.length === 0 || src.kind === "plugin";
}

/** 收集某个事件行里所有消息对象，用于校验。 */
function collectMessages(row) {
  if (row.type === "user/message") return [row.data];
  if (row.type === "system/message" || row.type === "developer/message" || row.type === "assistant/message" || row.type === "tool/result") return [row.data?.message];
  if (row.type === "agent/inbox/spliced") return Array.isArray(row.data?.inserted) ? row.data.inserted : [];
  if (row.type === "session/title-llm-request") return Array.isArray(row.data?.messages) ? row.data.messages : [];
  return [];
}

function main() {
  const argv = process.argv.slice(2);
  const apply = argv.includes("--apply");
  const onlySession = argv.includes("--session") ? argv[argv.indexOf("--session") + 1] : "";
  const home = process.env.DSH_HOME || path.join(os.homedir(), ".dsh");
  const sessionsRoot = path.join(home, "sessions");

  if (!fs.existsSync(sessionsRoot)) {
    console.error(`找不到会话目录：${sessionsRoot}`);
    process.exit(1);
  }

  const logs = [];
  for (const proj of fs.readdirSync(sessionsRoot)) {
    const dir = path.join(sessionsRoot, proj);
    if (!fs.statSync(dir).isDirectory()) continue;
    for (const sess of fs.readdirSync(dir)) {
      if (onlySession && !sess.includes(onlySession)) continue;
      const file = path.join(dir, sess, "session.jsonl.zstd");
      if (fs.existsSync(file)) logs.push({ proj, sess, file });
    }
  }
  if (!logs.length) {
    console.error("没有找到 session.jsonl.zstd");
    process.exit(1);
  }

  let totalBad = 0;
  let totalFixed = 0;
  for (const { proj, sess, file } of logs) {
    const buf = fs.readFileSync(file);
    const frames = readFrames(buf);
    let bad = 0;
    let badRows = 0;
    let lineCount = 0;
    const rewritten = [];
    for (const frame of frames) {
      if (frame.text === null) {
        rewritten.push(frame.raw); // 解不动的帧原样保留
        continue;
      }
      const lines = frame.text.split("\n").filter(Boolean);
      lineCount += lines.length;
      const outLines = [];
      let touched = false;
      for (const line of lines) {
        let row;
        try {
          row = JSON.parse(line);
        } catch {
          outLines.push(line); // 解析不了的原样保留
          continue;
        }
        const badHere = collectMessages(row).filter(violatesV4).length;
        if (badHere) {
          bad += badHere;
          badRows++;
          const n = rewriteRow(row);
          totalFixed += n;
          touched = true;
        }
        outLines.push(touched || badHere ? JSON.stringify(row) : line);
      }
      const payload = outLines.join("\n") + (outLines.length ? "\n" : "");
      // 逐帧重编码：帧结构不变，读取端的增量追加语义也不变
      rewritten.push(touched ? zstdCompressSync(Buffer.from(payload, "utf8")) : frame.raw);
    }
    totalBad += bad;
    const tag = bad ? "BAD " : "OK  ";
    console.log(`${tag}${proj}\\${sess.slice(0, 20)}…  帧 ${frames.length} 行 ${lineCount} 坏消息 ${bad}（${badRows} 行）`);
    if (!bad || !apply) continue;

    const outBuf = Buffer.concat(rewritten);
    const bak = `${file}.bak-${Date.now()}`;
    fs.copyFileSync(file, bak);
    fs.writeFileSync(file, outBuf);
    // 复查：重读一遍，坏消息必须归零、行数必须一致
    const after = readFrames(fs.readFileSync(file));
    let remain = 0;
    let afterLines = 0;
    for (const fr of after) {
      if (fr.text === null) continue;
      for (const line of fr.text.split("\n").filter(Boolean)) {
        afterLines++;
        try {
          remain += collectMessages(JSON.parse(line)).filter(violatesV4).length;
        } catch {
          /* 原样保留的行 */
        }
      }
    }
    console.log(
      `     已改写 → ${path.basename(file)}（备份 ${path.basename(bak)}；复查：剩余坏消息 ${remain}，行数 ${lineCount} → ${afterLines}）`,
    );
  }

  console.log(`\n合计：坏消息 ${totalBad} 条${apply ? `，已改写 ${totalFixed} 处` : "（dry-run，未改动；加 --apply 生效）"}`);
  if (totalBad && !apply) console.log("提示：跑 --apply 之前请先关掉要修的那个会话（服务进程会继续追加，边写边改会错位）。");
}

main();
