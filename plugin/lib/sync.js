/**
 * 调记忆库的双向同步脚本（自己那条 `proactive/tools/sync_mem.py`），
 * 把输出的尾巴交给模型看。
 *
 * 为什么要走脚本而不是插件自己写 SQL：同步的语义（哪些表、什么顺序、
 * 自然键冲突怎么裁决、server wins）全在那个脚本里，而且它两侧跑同一份代码
 * （每次 pull 前会把自己 SFTP 到服务器）。插件再实现一遍必然两套语义。
 *
 * 子进程一律 windowsHide —— 2026-11-08 的教训：pythonw 起的子进程（psql.exe）
 * 会被 Windows 分配一个新的控制台窗口，每 30 分钟在桌面上闪一下。
 */

import { spawn } from "node:child_process";

/** 同步模式 → 脚本参数。both 就是不带方向参数（默认双向）。 */
const MODE_FLAG = {
  status: ["--status"],
  pull: ["--pull"],
  push: ["--push"],
  both: [],
};

export const SYNC_MODES = Object.keys(MODE_FLAG);

/**
 * 跑一次同步。
 *
 * @param {object} opts
 * @param {string} opts.script sync_mem.py 的绝对路径
 * @param {string} [opts.pythonExe]
 * @param {string} [opts.mode] status | pull | push | both
 * @param {boolean} [opts.dryRun]
 * @param {Record<string,string>} [opts.env] 额外环境变量（DSH_SSH_* / MEM_PG_DSN 等）
 * @param {string} [opts.cwd]
 * @param {number} [opts.timeoutMs]
 * @returns {Promise<{ok:boolean, code:number|null, out:string, tail:string, secs:number}>}
 */
export function runSync(opts = {}) {
  const script = opts.script;
  if (!script) return Promise.reject(new Error("没有配置 syncScript（sync_mem.py 的绝对路径）"));
  const mode = opts.mode && MODE_FLAG[opts.mode] ? opts.mode : "both";
  const args = [script, ...MODE_FLAG[mode]];
  if (opts.dryRun) args.push("--dry-run");
  const timeoutMs = opts.timeoutMs || 600000;
  const t0 = Date.now();

  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(opts.pythonExe || "python", args, {
        cwd: opts.cwd || undefined,
        env: { ...process.env, ...(opts.env || {}), PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
      });
    } catch (err) {
      resolve({ ok: false, code: null, out: `启动同步脚本失败：${err.message}`, tail: "", secs: 0 });
      return;
    }

    let out = "";
    const cap = (chunk) => {
      out += chunk;
      if (out.length > 200000) out = out.slice(-100000);
    };
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", cap);
    child.stderr.on("data", cap);

    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        /* ignore */
      }
    }, timeoutMs);

    child.on("error", (err) => {
      clearTimeout(timer);
      resolve({ ok: false, code: null, out: `同步脚本启动失败：${err.message}`, tail: "", secs: 0 });
    });

    child.on("exit", (code) => {
      clearTimeout(timer);
      const lines = out.split(/\r?\n/).filter((l) => l.trim());
      resolve({
        ok: code === 0 && !/❌|失败|有错误/.test(out),
        code,
        out,
        tail: lines.slice(-8).join("\n"),
        secs: Math.round((Date.now() - t0) / 100) / 10,
      });
    });
  });
}
