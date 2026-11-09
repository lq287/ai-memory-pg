# -*- coding: utf-8 -*-
"""给「直接用服务器上那张记忆库」提供本地端口转发（SSH 隧道）。

为什么用 Python + paramiko，而不是直接调 ssh.exe：
  · 密钥鉴权在这个项目里已经由 paramiko 走通（同步脚本、计划任务都靠它）。
  · **Windows 的 OpenSSH 客户端会因私钥 ACL 太宽松直接拒绝使用**
    （"Permissions for 'id_rsa' are too open"），无人值守场景下几乎没法让人现场改 ACL。
  · 两侧都能跑：目标已经在本机时（target=local）这个脚本根本不会被拉起。

用法
----
    python pg_tunnel.py --host 1.2.3.4 --user root --key C:/path/id_rsa \
                        [--password xxx] [--local-port 15432] \
                        [--remote-host 127.0.0.1] [--remote-port 5432]

stdout 第一行打印 {"ok": true, "local_port": N} 表示可以连了，之后进程常驻转发。
进程退出（kill / 父进程关掉管道）即关闭隧道。
"""
from __future__ import annotations

import argparse
import json
import select
import socketserver
import sys
import traceback

# 注意：故意用 3.6 兼容的写法（不用 PEP 604 的 X | Y），
# 服务器上是 Python 3.11，本机 3.14，两边都要能跑。


class _Forward(socketserver.BaseRequestHandler):
    """一条本地连接 ↔ 一条 SSH direct-tcpip 通道，双向搬运。"""

    transport = None  # 由 main 注入

    def handle(self) -> None:
        try:
            peer = self.request.getpeername()
        except OSError:
            peer = ("?", 0)
        try:
            chan = self.transport.open_channel(
                "direct-tcpip",
                (self.server.remote_host, self.server.remote_port),  # type: ignore[attr-defined]
                peer,
            )
        except Exception as exc:  # noqa: BLE001
            print("[tunnel] 打开通道失败：%s" % exc, file=sys.stderr, flush=True)
            return
        if chan is None:
            print("[tunnel] 打开通道失败：服务器拒绝", file=sys.stderr, flush=True)
            return
        try:
            while True:
                r, _, _ = select.select([self.request, chan], [], [])
                if self.request in r:
                    data = self.request.recv(16384)
                    if not data:
                        break
                    chan.sendall(data)
                if chan in r:
                    data = chan.recv(16384)
                    if not data:
                        break
                    self.request.sendall(data)
        except Exception:  # noqa: BLE001
            pass
        finally:
            for sock in (chan, self.request):
                try:
                    sock.close()
                except Exception:  # noqa: BLE001
                    pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    remote_host = "127.0.0.1"
    remote_port = 5432


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SSH 本地端口转发（给远程 PostgreSQL 用）")
    ap.add_argument("--host", required=True, help="服务器地址")
    ap.add_argument("--port", type=int, default=22, help="SSH 端口")
    ap.add_argument("--user", default="root")
    ap.add_argument("--key", help="私钥文件（优先）")
    ap.add_argument("--password", help="没有可用私钥时的回退")
    ap.add_argument("--local-port", type=int, default=15432,
                    help="本地监听端口；0 = 让系统分配")
    ap.add_argument("--remote-host", default="127.0.0.1")
    ap.add_argument("--remote-port", type=int, default=5432)
    a = ap.parse_args(argv)

    try:
        import paramiko
    except ImportError:
        print("缺 paramiko：pip install paramiko", file=sys.stderr, flush=True)
        return 2

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {"look_for_keys": False, "allow_agent": False}
    if a.key:
        kw["key_filename"] = a.key
    if a.password:
        kw["password"] = a.password
    try:
        client.connect(a.host, port=a.port, username=a.user, timeout=20,
                       banner_timeout=30, auth_timeout=30, **kw)
    except Exception as exc:  # noqa: BLE001
        print("[tunnel] SSH 连接失败：%s: %s" % (type(exc).__name__, exc),
              file=sys.stderr, flush=True)
        return 3

    transport = client.get_transport()
    if transport is None or not transport.is_active():
        print("[tunnel] SSH 连接没有活跃 transport", file=sys.stderr, flush=True)
        return 3

    try:
        srv = _Server(("127.0.0.1", a.local_port), _Forward)
    except OSError as exc:
        print("[tunnel] 本地端口 %s 监听失败：%s" % (a.local_port, exc),
              file=sys.stderr, flush=True)
        return 4
    srv.remote_host = a.remote_host
    srv.remote_port = a.remote_port
    _Forward.transport = transport

    port = srv.server_address[1]
    print(json.dumps({"ok": True, "local_port": port,
                      "remote": "%s:%s" % (a.remote_host, a.remote_port),
                      "via": "%s@%s:%s" % (a.user, a.host, a.port)}), flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 5
    finally:
        try:
            srv.server_close()
        except Exception:  # noqa: BLE001
            pass
        try:
            transport.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
