/**
 * 记忆库 sidecar 的 Node 侧管理：spawn 一个常驻的 `python -m app.sidecar`，
 * 用 JSON-lines 跟它对话。
 *
 * 为什么常驻而不是每次 spawn：
 *   一次冷启动 ≈ 950ms（Python + psycopg + ONNX 模型 + SCRAM 握手），
 *   而常驻之后每次调用只要 10~50ms。工具会被模型频繁调用，这个差别是可用与不可用的差别。
 *
 * 生命周期：
 *   · 第一次 request() 时懒启动（装好插件但没用到记忆的会话不付这个钱）
 *   · 子进程死了 → 挂起的请求全部 reject，下次 request() 自动重启（带退避）
 *   · dispose() 发 shutdown 并等待，超时则 kill
 *
 * 失败语义：
 *   记忆不可用**不能**让整个会话挂掉。所有错误都变成工具结果里的错误文本，
 *   由模型决定下一步 —— 这也是这个项目对"增强层"的一贯取向（见 app/graph.py）。
 */

import { spawn } from "node:child_process";
import path from "node:path";

const DEFAULT_TIMEOUT_MS = 30_000;
const MAX_STDERR_LINES = 40;

export class MemorySidecar {
  /**
   * @param {object} options
   * @param {string} options.projectRoot 记忆库项目目录（含 app/ 与 .pylibs/）
   * @param {string} [options.pythonExe] python 可执行文件，默认 "python"
   * @param {string} [options.dsn] PostgreSQL DSN；不给则用子进程环境里的 MEM_PG_DSN
   * @param {string} [options.userId] 记忆归属用户
   * @param {number} [options.timeoutMs]
   * @param {(line: string) => void} [options.log] 子进程 stderr 的去处
   */
  constructor(options) {
    this.projectRoot = path.resolve(options.projectRoot);
    this.pythonExe = options.pythonExe || "python";
    this.dsn = options.dsn;
    this.userId = options.userId || "owner";
    this.timeoutMs = options.timeoutMs || DEFAULT_TIMEOUT_MS;
    this.log = options.log || ((line) => console.error(`[memory] ${line}`));

    this.child = null;
    this.pending = new Map();
    this.nextId = 1;
    this.buffer = "";
    this.stderrTail = [];
    this.restartCount = 0;
    this.disposed = false;
  }

  /** 子进程环境：零磁盘写入 + UTF-8 + 项目内 .pylibs。 */
  _env() {
    const sep = process.platform === "win32" ? ";" : ":";
    const env = {
      ...process.env,
      PYTHONPATH: [path.join(this.projectRoot, ".pylibs"), this.projectRoot].join(sep),
      PYTHONIOENCODING: "utf-8",
      PYTHONDONTWRITEBYTECODE: "1", // 见 mem.cmd 里的长注释：保证对磁盘零写入
      MEM_USER_ID: this.userId,
    };
    if (this.dsn) env.MEM_PG_DSN = this.dsn;
    return env;
  }

  _ensureChild() {
    if (this.child && this.child.exitCode === null) return this.child;
    if (this.disposed) throw new Error("memory sidecar 已关闭");

    // 重启太快说明是配置问题（python 不在 PATH、依赖没装），别无限循环
    if (this.restartCount >= 3 && this._lastExitAt && Date.now() - this._lastExitAt < 5000) {
      throw new Error(
        `memory sidecar 连续启动失败 ${this.restartCount} 次：${this.stderrTail.slice(-5).join(" | ")}`,
      );
    }

    this.buffer = "";
    const child = spawn(this.pythonExe, ["-m", "app.sidecar"], {
      cwd: this.projectRoot,
      env: this._env(),
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    this.child = child;

    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => this._onStdout(chunk));

    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      for (const line of String(chunk).split(/\r?\n/)) {
        if (!line.trim()) continue;
        this.stderrTail.push(line);
        if (this.stderrTail.length > MAX_STDERR_LINES) this.stderrTail.shift();
        this.log(line);
      }
    });

    child.on("error", (err) => {
      this._failAll(new Error(`memory sidecar 启动失败：${err.message}`));
      this._lastExitAt = Date.now();
      this.restartCount += 1;
      this.child = null;
    });

    child.on("exit", (code, signal) => {
      const detail = this.stderrTail.slice(-3).join(" | ");
      this._failAll(
        new Error(
          `memory sidecar 退出（code=${code}, signal=${signal}）${detail ? "：" + detail : ""}`,
        ),
      );
      this._lastExitAt = Date.now();
      if (code !== 0) this.restartCount += 1;
      this.child = null;
    });

    return child;
  }

  _onStdout(chunk) {
    this.buffer += chunk;
    let nl;
    while ((nl = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, nl).trim();
      this.buffer = this.buffer.slice(nl + 1);
      if (!line) continue;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch {
        this.log(`无法解析的响应行: ${line.slice(0, 200)}`);
        continue;
      }
      const waiter = this.pending.get(msg.id);
      if (!waiter) continue;
      this.pending.delete(msg.id);
      clearTimeout(waiter.timer);
      if (msg.ok) waiter.resolve(msg.result);
      else waiter.reject(sidecarError(msg.error));
    }
  }

  _failAll(error) {
    for (const [, waiter] of this.pending) {
      clearTimeout(waiter.timer);
      waiter.reject(error);
    }
    this.pending.clear();
  }

  /**
   * 发一个请求。
   * @param {string} op
   * @param {object} [args]
   * @param {number} [timeoutMs]
   * @returns {Promise<object>}
   */
  request(op, args = {}, timeoutMs) {
    const child = this._ensureChild();
    const id = this.nextId++;
    const payload = Buffer.from(JSON.stringify({ id, op, args }) + "\n", "utf8");

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`memory sidecar 超时（${op} > ${timeoutMs || this.timeoutMs}ms）`));
      }, timeoutMs || this.timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      child.stdin.write(payload, (err) => {
        if (!err) return;
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new Error(`写入 sidecar 失败：${err.message}`));
      });
    });
  }

  /** 优雅关闭；不抛错（关不掉就 kill）。 */
  async dispose() {
    this.disposed = true;
    const child = this.child;
    if (!child || child.exitCode !== null) return;
    try {
      await this.request("shutdown", {}, 2000);
    } catch {
      /* 已经在退出路径上，忽略 */
    }
    await new Promise((resolve) => {
      const timer = setTimeout(() => {
        try {
          child.kill();
        } catch {
          /* ignore */
        }
        resolve();
      }, 2000);
      child.once("exit", () => {
        clearTimeout(timer);
        resolve();
      });
    });
    this.child = null;
  }
}

function sidecarError(error) {
  if (!error) return new Error("memory sidecar 返回了未知错误");
  const err = new Error(error.message || String(error));
  err.name = error.type || "SidecarError";
  if (error.hint) err.hint = error.hint;
  return err;
}
