# dsh-memory-pg 安装与配置（0.2.2）

一份「换台机器 / 换个 profile 也能照着抄」的说明。

> 英文简介与 8 个工具的说明在 [`README.md`](README.md)（npm 页面上显示的那份）；
> **安装、配置、报错排查只在本文件**，避免两处各写一份再慢慢漂移。

三种典型场景：

| 场景 | `target` | 用哪张库 | 需要 SSH 到服务器吗 |
|---|---|---|---|
| A. 单机自用 | `local` | 只有本机 PostgreSQL | 不需要 |
| B. 记忆都在服务器 | `server` | 只有服务器那一张（自动 SSH 隧道） | 需要 |
| C. 本机为主 + 与服务器对齐 | `both` | 读写本机，靠同步脚本抹平 | 同步时才需要 |

---

## 0. 前置条件

**DSH 侧**

- dsh `0.1.1-rc.2` 或更新（profile 机制 + `agent/pre-step` 钩子）；已在 `0.2.2-rc.1`（会话格式 v4）上实测
- 有 pnpm（profile 的 `package.json` 是 pnpm 工作区）
- **不需要** `@deepseek-ai/*` 任何包：0.2.2 起插件零运行时依赖（0.1.x 有投影就注册、没有就走旧路径）

**Python 侧**（插件会 spawn 一个常驻 `python -m app.sidecar`）

- Python ≥ 3.10
- 记忆库项目目录（下面叫 `<memory-project>`），**必须**含 `app/` 与 `.pylibs/`
- 依赖：`psycopg[binary]`、`pgvector`、ONNX 向量模型（见该项目的 `requirements.txt`）
- `target=server/both` 另需 **`paramiko`**（同步脚本与 SSH 隧道都用它）

> 依赖装在项目内的 `.pylibs/` 是最省事的做法：插件已经把 `<memory-project>/.pylibs`
> 自动塞进子进程的 `PYTHONPATH`。装在别处也行，用 `pythonPath` 指过去。
>
> ⚠️ Windows 上 `python` 可能是应用商店的占位 stub。**建议 `pythonExe` 写绝对路径**：
> `C:\Users\<你>\AppData\Local\Python\pythoncore-3.14-64\python.exe`

**数据库侧**

- PostgreSQL 13+ 且装了 `pgvector`（记忆库 schema 见该项目的 `sql/`）
- 若库在别的机器：**默认只监听 `127.0.0.1`** 是好事，别为了连它去开公网端口 ——
  用 `target=server` 的 SSH 隧道，或者把板子架在隧道上（见下）

---

## 1. 装

### 方式一：从源码目录（开发/自用，推荐）

```bash
cd ~/.dsh/profiles/<profile-name>          # Windows: C:\Users\<你>\.dsh\profiles\<name>
pnpm add "file:/绝对路径/到/memory-project/plugin"
```

### 方式二：从 tgz 包（拷到别人机器上）

```bash
cd ~/.dsh/profiles/<profile-name>
pnpm add "file:/绝对路径/dsh-memory-pg-0.2.2.tgz"
```

### 方式三：从 npm（如果发布过）

```bash
cd ~/.dsh/profiles/<profile-name>
pnpm add dsh-memory-pg
```

### 装完还要两件事（都要，缺一不生效）

**① 把 bundle 加进 profile 的 `package.json`**

```json
{
  "dsh": {
    "profile": {
      "bundles": ["@deepseek-ai/dsh-base", "...", "dsh-memory-pg"]
    }
  }
}
```

`pnpm add` 只装包，**不会**自动加 bundle。

**② 在 profile 的 `cordis.patch.yml` 里覆盖 `id: memory-pg` 的 config**

```yaml
- id: memory-pg
  config:
    projectRoot: '/绝对/路径/到/memory-project'
    userId: owner
    pythonExe: python
    tools: true
    autoContext: true
    target: local
```

> ⚠️ **这里必须用 `- id: memory-pg ...` 覆盖，不能再写 `- insert:`**。
> 插件自带的 `cordis.patch.yml` 已经 `insert` 过这一行了，用户层再 insert 一次会报
> `duplicate loader entry id: memory-pg`，dsh 直接起不来。
> 另外补丁是**整块替换 config**（不合并），所以需要的键要**写全**。

改完重启 dsh。

---

## 2. 三个场景的完整配置块（直接抄）

### A. 只用本机库（`target: local`）

```yaml
- id: memory-pg
  config:
    projectRoot: '/path/to/memory-project'
    userId: owner
    pythonExe: python
    dsn: 'postgresql://mem:mem@127.0.0.1:5432/mem'   # 留空则用 MEM_PG_DSN / 项目默认
    tools: true
    autoContext: true
    contextBudget: 900
    factLimit: 10
    includeTasks: true
    refreshSteps: 0
    target: local
```

不需要 SSH、不需要 paramiko、不需要同步脚本。

### B. 只用服务器那张库（`target: server`）

```yaml
- id: memory-pg
  config:
    projectRoot: '/path/to/memory-project'
    userId: owner
    pythonExe: python
    tools: true
    autoContext: true
    target: server
    serverHost: '203.0.113.10'            # 不给就用环境变量 DSH_SSH_HOST
    serverPort: 22
    serverUser: root                       # 不给就用 DSH_SSH_USER
    serverKeyFile: '/path/to/id_rsa'       # 不给则自动找 syncScript 旁的 run/sync/ssh/id_rsa
    serverPassword: ''                     # 没有可用私钥时回退（或用 DSH_SSH_PASS）
    serverDsn: 'postgresql://mem:mem@127.0.0.1:5432/mem'   # 服务器上那张库的 DSN
    tunnelPort: 15432                      # 本地监听端口，冲突就换一个
    pythonPath: '/path/to/pylibs-that-has-paramiko'
```

行为：第一次记忆调用时懒加载 `lib/pg_tunnel.py`（paramiko）拉起
`127.0.0.1:<tunnelPort> → <serverHost>:127.0.0.1:5432` 的转发，sidecar 的 DSN 被改写成
隧道地址；**本机那张库完全不会被碰**。插件卸载/重启即关闭隧道。

> 为什么不用 `ssh.exe`：Windows 的 OpenSSH 客户端会因私钥 ACL 太宽松**直接拒用**
> （`Permissions for 'id_rsa' are too open`），无人值守场景改不动 ACL。
> paramiko 没这个问题。Linux 上两者都行，但统一用 paramiko 更省心。

### C. 本机为主 + 与服务器同步（`target: both`）

```yaml
- id: memory-pg
  config:
    projectRoot: '/path/to/memory-project'
    userId: owner
    pythonExe: python
    dsn: 'postgresql://mem:mem@127.0.0.1:5432/mem'
    tools: true
    autoContext: true
    target: both
    serverHost: '203.0.113.10'
    serverUser: root
    serverKeyFile: '/path/to/id_rsa'
    syncScript: '/path/to/shell/proactive/tools/sync_mem.py'
    syncAuto: false                        # true = 每次写操作后自动同步
    syncTimeoutMs: 600000
    pythonPath: '/path/to/pylibs-that-has-paramiko'
```

`both` **不是**两个库同时双写 —— 一个事实来源（本机）+ 同步脚本，这样两边不会分叉成
两段历史。抹平靠 `memory_sync` 工具（`status` / `pull` / `push` / `both`）。

同步脚本自己的凭据来源（优先级从高到低）：环境变量 `DSH_SSH_*` → 脚本旁的
`run/sync/target.json` + `run/sync/ssh/id_rsa`。插件会把配置里的 `serverHost/User/Password`
转成这几个环境变量传下去。

---

## 3. 打一张 tgz 带到别的机器

```bash
cd /path/to/memory-project/plugin
pnpm pack            # 产出 dsh-memory-pg-0.2.2.tgz
tar tzf dsh-memory-pg-0.2.2.tgz    # 确认 lib/ 下 5 个文件都在
```

包内应含：`lib/index.js`、`lib/sidecar.js`、`lib/tunnel.js`、`lib/sync.js`、
`lib/pg_tunnel.py`、`cordis.patch.yml`。**少了后三个说明 `package.json` 的 `files`
白名单没更新** —— 装到新机器上会报「找不到 ./tunnel.js」。

---

## 4. 验证（不重启也能先查配置）

```bash
# ① 配置有没有被正确合成（应该看到 "# == dsh-memory-pg, patched by ..."）
dsh --profile <name> --dump-config | grep -A6 'dsh-memory-pg'

# ② 引擎通不通（不经过 DSH）
cd /path/to/memory-project
printf '%s\n' '{"id":1,"op":"health"}' '{"id":2,"op":"shutdown"}' | python -m app.sidecar

# ③ 插件级（起一个真实会话）
dsh --profile <name> "调用 memory_health，把结果原文贴出来，不要解释"
```

`memory_health` 的第一行会写清楚**在用哪张库**：

- `库：本机库｜…`
- `库：服务器 203.0.113.10 上的库（隧道 127.0.0.1:15432）｜隧道：127.0.0.1:15432 → 127.0.0.1:5432（经 root@…:22，本次拉起）｜…`

---

## 5. 切换 / 关掉 / 回滚

- **换库**：只改 `target` 一个字段，重启 dsh。三个值随时可切。
- **暂时不用记忆**：`tools: false` + `autoContext: false`（包还装着，不注册工具也不注入）。
- **彻底卸掉**：
  1. `package.json` 的 `bundles` 里删掉 `dsh-memory-pg`
  2. `cordis.patch.yml` 里删掉那一整段（或整个文件写回 `[]`）
  3. `pnpm remove dsh-memory-pg`
  4. 重启 dsh

---

## 6. 常见报错对照

| 现象 | 原因 | 处理 |
|---|---|---|
| `duplicate loader entry id: memory-pg` | 用户层写了 `- insert:` | 改成 `- id: memory-pg` + 完整 config |
| 工具全是「…失败：` 前缀文本 | sidecar 起不来 / 连不上库 | 先跑第 4 节的 ②，看 python 与 DSN |
| `缺 paramiko` | 系统 python 里没有 paramiko | 装它，或用 `pythonPath` 指向含 paramiko 的目录 |
| `SSH 隧道在 …ms 内没起来` | 服务器不可达 / 私钥不对 / 端口占用 | 手工试 `python lib/pg_tunnel.py --host ... --key ...`，它会打印真实错误 |
| sidecar 连续启动失败 3 次 | `projectRoot` 指错、`pythonExe` 是占位 stub | 用绝对路径，并确认该目录有 `app/` 与 `.pylibs/` |
| `memory_sync` 报「没有配置 syncScript」 | `target=both` 但没给脚本路径 | 补 `syncScript`，或把 target 改回 `local` |
| 每轮都报 `format v4 message requires a producer-owned source kind` | 插件 ≤0.2.0 的注入消息把 `source.kind` 写成了 `"plugin"`；会话格式 v4 要求它是插件自己的名字，并且要求插件注册 `sessionProjections` 投影 | 升级到 **0.2.2+** |
| 升级 dsh 后**工具全没了**，启动日志只有一句 `patch: entry "memory-pg" not found` | profile 规范化把 `dsh-memory-pg` 从 `dsh.profile.bundles` 里剔掉了（它还在 `dependencies` 里，所以看着像装了）。bundles 里没有它 ⇒ 它的补丁层不应用 ⇒ 插件从未加载，而且**不报错** | 把 `"dsh-memory-pg"` 加回 `profiles/<名字>/package.json` 的 `dsh.profile.bundles`，然后重启 |
| 启动日志有 `memory-pg (dsh-memory-pg): Error: cannot find package '@deepseek-ai/...'` | ≤0.2.1 有 peer 依赖，而 dsh 初始化 profile 用的是 `autoInstallPeers: false`，peer 根本不会被安装 | 用 **0.2.2+**（已改成零运行时依赖，不 import 任何 `@deepseek-ai/*`） |
| 改完代码但行为没变 | pnpm 对 `file:` 依赖会用已有副本 | 在 profile 目录跑 `pnpm install --force`（或先删 `node_modules/<包名>`） |
| 桌面上每轮闪黑框 | 子进程（`psql.exe` 等）被分配了新控制台 | 相关 spawn 必须 `windowsHide: true` / `CREATE_NO_WINDOW` |

> 想在启动日志里确认插件到底有没有生效，看这一行（0.2.2 起直接写 stderr）：
> `[memory-pg] memory-pg ready: v0.2.2 target=… tools=on autoContext=on projection=registered|legacy-host`
> `projection=registered` 表示新版要求的会话投影已注册；`legacy-host` 表示宿主是 0.1.x（不需要投影）。

---

## 7. 字段速查

| 键 | 默认 | 说明 |
|---|---|---|
| `projectRoot` | —（必填） | 记忆库项目目录（含 `app/`、`.pylibs/`） |
| `userId` | `owner` | 记忆归属用户 |
| `dsn` | env `MEM_PG_DSN` | 本机库 DSN |
| `pythonExe` | `python` | 建议写绝对路径 |
| `pythonPath` | — | 额外 `PYTHONPATH`（OS 分隔符），paramiko 不在系统 python 时用 |
| `tools` | `true` | 是否注册 8 个工具 |
| `autoContext` | `true` | 是否每轮注入记忆前言 |
| `contextBudget` | `900` | 前言字符预算 |
| `factLimit` | `10` | 前言带多少条事实 |
| `includeTasks` | `true` | 前言是否带进行中任务 |
| `refreshSteps` | `0` | 每 N 步重采样前言（0 = 一轮一次） |
| `timeoutMs` | `30000` | 单次 sidecar 调用超时 |
| `target` | `local` | `local` / `server` / `both` |
| `serverHost` | env `DSH_SSH_HOST` | 服务器地址 |
| `serverPort` | `22` | SSH 端口 |
| `serverUser` | env `DSH_SSH_USER` | SSH 用户 |
| `serverKeyFile` | 自动找 | 隧道私钥 |
| `serverPassword` | env `DSH_SSH_PASS` | 无可用私钥时回退 |
| `serverDsn` | `postgresql://mem:mem@127.0.0.1:5432/mem` | 服务器上那张库的 DSN |
| `tunnelPort` | `15432` | 本地隧道端口 |
| `syncScript` | — | `sync_mem.py` 路径（给了才有 `memory_sync`） |
| `syncAuto` | `false` | 写后自动同步（`both` 才有意义） |
| `syncTimeoutMs` | `600000` | 单次同步超时 |

## 8. 工具一览

| 工具 | 用途 |
|---|---|
| `memory_recall` | 向量 ⊕ 关键词 ⊕ 关系图三路召回 |
| `memory_add` | 走四态裁决写入一条记忆 |
| `memory_fact_get` | 按键精确取长期事实 |
| `memory_fact_set` | 写/覆盖长期事实（旧值进历史） |
| `memory_context` | 「现在该知道什么」前言 |
| `memory_digest` | 日/周/月/年关键词摘要（可重建） |
| `memory_health` | 体检 + **打印当前在用哪张库** |
| `memory_sync` | 本机 ↔ 服务器双向同步（`status`/`pull`/`push`/`both`） |
