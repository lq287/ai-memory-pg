/**
 * 用 Python/paramiko 拉一条 SSH 本地端口转发，把「服务器上的 PostgreSQL」
 * 变成 127.0.0.1:<tunnelPort>，这样 sidecar 用普通 DSN 就能连过去，
 * 不需要改记忆库项目里的任何代码。
 *
 * 为什么不直接调 ssh.exe：见 ./pg_tunnel.py 顶部的注释 —— Windows 的 OpenSSH
 * 客户端会因私钥 ACL 太宽松直接拒绝使用（"Permissions are too open"），
 * 而 paramiko 这条路在这个项目里已经跑通了（同步脚本、计划任务都用它）。
 *
 * 失败语义：ensure() 抛错，调用方把错误变成工具文本 —— 记忆是增强层，
 * 连不上不能让会话挂掉。
 */

import { spawn } from "node:child_process";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT = path.join(HERE, "pg_tunnel.py");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 端口能不能连上（连上就算通，用于判断隧道是否就绪）。 */
export function probePort(port, host = "127.0.0.1", timeoutMs = 400) {
  return new Promise((resolve) => {
    const sock = net.connect({ port, host });
    let settled = false;
    const done = (ok) => {
      if (settled) return;
      settled = true;
      try {
        sock.destroy();
      } catch {
        /* ignore */
      }
      resolve(ok);
    };
    sock.setTimeout(timeoutMs);
    sock.once("connect", () => done(true));
    sock.once("timeout", () => done(false));
    sock.once("error", () => done(false));
  });
}

export class PgTunnel {
  /**
   * @param {object} opts
   * @param {string} opts.host 服务器地址
   * @param {number} [opts.port] SSH 端口
   * @param {string} [opts.user]
   * @param {string} [opts.keyFile] 私钥
   * @param {string} [opts.password] 没有私钥时的回退
   * @param {number} [opts.localPort] 本地监听端口（0 = 系统分配）
   * @param {string} [opts.remoteHost]
   * @param {number} [opts.remotePort]
   * @param {string} [opts.pythonExe]
   * @param {string[]} [opts.pylibs] 额外的 PYTHONPATH 目录（paramiko 可能不在系统 python 里）
   * @param {number} [opts.timeoutMs] 等隧道就绪的上限
   * @param {(line: string) => void} [opts.log]
   */
  constructor(opts = {}) {
    this.host = opts.host || "";
    this.port = opts.port || 22;
    this.user = opts.user || "root";
    this.keyFile = opts.keyFile || "";
    this.password = opts.password || "";
    this.localPort = Number(opts.localPort ?? 15432);
    this.remoteHost = opts.remoteHost || "127.0.0.1";
    this.remotePort = opts.remotePort || 5432;
    this.pythonExe = opts.pythonExe || "python";
    this.pylibs = Array.isArray(opts.pylibs) ? opts.pylibs.filter(Boolean) : [];
    this.timeoutMs = opts.timeoutMs || 15000;
    this.log = opts.log || ((line) => console.error(`[memory-tunnel] ${line}`));

    this.child = null;
    this.stderrTail = [];
    this._readyPort = 0;
    this._external = false; // 端口本来就有东西在听（可能是上一次进程留下的隧道）
    this._starting = null;
  }

  /** 隧道就绪后的本地端口；没就绪时抛错。 */
  get readyPort() {
    if (!this._readyPort) throw new Error("SSH 隧道还没就绪");
    return this._readyPort;
  }

  get describe() {
    if (!this._readyPort) return `未建立（目标 ${this.user}@${this.host}:${this.port} → ${this.remoteHost}:${this.remotePort}）`;
    const how = this._external ? "复用已有监听" : "本次拉起";
    return `127.0.0.1:${this._readyPort} → ${this.remoteHost}:${this.remotePort}（经 ${this.user}@${this.host}:${this.port}，${how}）`;
  }

  /** 确保隧道可用；并发调用共享同一次启动。 */
  async ensure() {
    if (this._readyPort && (await probePort(this._readyPort))) return this._readyPort;
    if (this._starting) return this._starting;
    this._starting = this._ensureOnce().finally(() => {
      this._starting = null;
    });
    return this._starting;
  }

  async _ensureOnce() {
    if (!this.host) throw new Error("没有配置 serverHost（target=server/both 时必须给）");

    // 端口已经有人在听 → 当成现成隧道用（可能是上一次 dsh 进程没清掉的）
    if (this.localPort && (await probePort(this.localPort))) {
      this._readyPort = this.localPort;
      this._external = true;
      this.log(`本地端口 ${this.localPort} 已有监听，直接复用`);
      return this._readyPort;
    }

    const args = [SCRIPT, "--host", this.host, "--port", String(this.port),
                  "--user", this.user,
                  "--local-port", String(this.localPort),
                  "--remote-host", this.remoteHost,
                  "--remote-port", String(this.remotePort)];
    if (this.keyFile) args.push("--key", this.keyFile);
    if (this.password) args.push("--password", this.password);

    this.stderrTail = [];
    const sep = process.platform === "win32" ? ";" : ":";
    const env = { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONDONTWRITEBYTECODE: "1" };
    if (this.pylibs.length) {
      env.PYTHONPATH = [...this.pylibs, env.PYTHONPATH || ""].filter(Boolean).join(sep);
    }
    const child = spawn(this.pythonExe, args, {
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true, // 别在桌面上闪黑框
      env,
    });
    this.child = child;

    let firstLine;
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      for (const line of String(chunk).split(/\r?\n/)) {
        if (!line.trim()) continue;
        if (!firstLine) {
          try {
            const info = JSON.parse(line);
            if (info?.ok) {
              this._readyPort = Number(info.local_port) || this.localPort;
              firstLine = info;
            }
          } catch {
            this.log(`隧道脚本输出：${line.slice(0, 200)}`);
          }
        }
      }
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      for (const line of String(chunk).split(/\r?\n/)) {
        if (!line.trim()) continue;
        this.stderrTail.push(line);
        if (this.stderrTail.length > 20) this.stderrTail.shift();
        this.log(line);
      }
    });
    child.on("exit", (code) => {
      if (this._readyPort && code !== 0) this.log(`隧道进程退出 code=${code}`);
      this._readyPort = 0;
      this.child = null;
    });

    const t0 = Date.now();
    while (Date.now() - t0 < this.timeoutMs) {
      if (firstLine) return this._readyPort;
      const guess = this.localPort;
      if (guess && (await probePort(guess))) {
        this._readyPort = guess;
        return this._readyPort;
      }
      await sleep(250);
    }
    const detail = this.stderrTail.slice(-4).join(" | ");
    throw new Error(`SSH 隧道在 ${this.timeoutMs}ms 内没起来${detail ? "：" + detail : ""}`);
  }

  dispose() {
    const child = this.child;
    this.child = null;
    this._readyPort = 0;
    if (!child) return;
    try {
      child.kill();
    } catch {
      /* ignore */
    }
  }
}
