# dsh-memory-pg

PostgreSQL-backed long-term memory as a DeepSeek Harness plugin: **native tools** plus a
**per-turn memory preamble**, so memory enters the conversation on its own instead of only
when someone remembers to go query it.

* recall = vector ⊕ keyword ⊕ relation-graph, fused with RRF, then Gaussian age decay × log
  frequency × importance, then a token budget
* writes go through four-state arbitration (`accept` / `merge` / `defer` / `drop`) —
  restating something reinforces it instead of duplicating it; contradictions wait for review
  instead of silently overwriting
* day / week / month / year keyword digests, so "what was I busy with last month" is answerable
* every memory is bitemporal (`valid_until`), nothing is hard-deleted by arbitration

## What you get

| Tool | Purpose |
|---|---|
| `memory_recall` | three-path recall over memories (summary layer by default, `full: true` for text) |
| `memory_add` | write a memory through the arbitration pipeline |
| `memory_fact_get` | exact key lookup for stable facts (timezone, editor, answer style…) |
| `memory_fact_set` | write/overwrite a stable fact (old value goes to history) |
| `memory_context` | "what should I know right now" preamble (facts + recent digests + live tasks) |
| `memory_digest` | build/read day/week/month/year keyword digests |
| `memory_health` | connectivity, embedder, scale, health counters, graph stats |
| `memory_sync` | run the two-way sync script (`status` / `pull` / `push` / `both`) |

Plus `autoContext` (default **on**): before each turn’s first step the plugin injects one
plugin-sourced message containing the preamble, budgeted by `contextBudget` characters.
Memory being unavailable **never** breaks the request — injection is skipped and tools return
an error *text*.

## Requirements

This plugin is a thin native front-end; the memory engine stays in one place.

* **Node ≥ 20** (provided by DSH)
* **Python 3.11+** with the memory project's dependencies installed in `<projectRoot>/.pylibs`
  (`psycopg`, `pgvector`, `onnxruntime`, `tokenizers`, `numpy`)
* **PostgreSQL 14+ with pgvector** and the memory project's schema loaded
  (`psql -f sql/01_schema.sql … 08_digest.sql`)

> The plugin does not re-implement recall, arbitration, embeddings or digests in JavaScript.
> It spawns a long-lived `python -m app.sidecar` and talks JSON-lines to it. One cold start
> (~1 s: Python + psycopg + ONNX model + SCRAM) then ~10–50 ms per call. Two implementations
> would drift; this project has already been bitten by exactly that twice.

## Install

```bash
# 1. install the package into your profile
dsh plugin --profile web add dsh-memory-pg

# 2. register the bundle (edit ~/.dsh/profiles/web/package.json)
#      "dsh": { "profile": { "bundles": [ ..., "dsh-memory-pg" ] } }

# 3. point it at your memory project — edit the config in the same profile,
#    or overlay a patch (see below)

# 4. restart dsh
```

Minimal patch overlay (equivalent to what the bundle ships, but explicit):

```yaml
- insert:
    - id: memory-pg
      name: dsh-memory-pg
      config:
        projectRoot: 'D:\DSH proj\AI记忆'   # must contain app/ and .pylibs/
        userId: owner
```

```bash
dsh --profile web --patch ./memory.patch.yml
```

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `projectRoot` | — (**required**) | memory project dir (contains `app/`, `.pylibs/`) |
| `userId` | `owner` | whose memories |
| `dsn` | env `MEM_PG_DSN` | PostgreSQL DSN |
| `pythonExe` | `python` | interpreter to spawn |
| `tools` | `true` | register the seven tools |
| `autoContext` | `true` | inject the preamble every turn |
| `contextBudget` | `900` | characters of preamble (≈ tokens) |
| `factLimit` | `10` | facts included in the preamble |
| `includeTasks` | `true` | include live tasks in the preamble |
| `refreshSteps` | `0` | re-sample the preamble every N steps (0 = once per turn) |
| `timeoutMs` | `30000` | per-call timeout |
| `target` | `local` | which database to use: `local` / `server` / `both` |
| `serverHost` | env `DSH_SSH_HOST` | server holding the second database (`target=server`) |
| `serverPort` | `22` | SSH port for the tunnel |
| `serverUser` | env `DSH_SSH_USER` | SSH user |
| `serverKeyFile` | auto (`run/sync/ssh/id_rsa`) | private key for the tunnel |
| `serverPassword` | env `DSH_SSH_PASS` | fallback when no usable key |
| `serverDsn` | `postgresql://mem:mem@127.0.0.1:5432/mem` | DSN of the server database (inside the tunnel) |
| `tunnelPort` | `15432` | local port the tunnel listens on |
| `syncScript` | — | path to `sync_mem.py` (enables `memory_sync`) |
| `syncAuto` | `false` | sync after every write (`target=both` only) |
| `syncTimeoutMs` | `600000` | per-sync timeout |
| `pythonPath` | — | extra `PYTHONPATH` (OS separator), when `paramiko` is not in the system python |

## Choosing which database (v0.2.0)

`target` decides where every tool **and** the automatic preamble read and write:

| `target` | reads/writes | other database |
|---|---|---|
| `local` (default) | the local PostgreSQL | untouched |
| `server` | the server's PostgreSQL, via an SSH local port forward | the local one is untouched |
| `both` | the local PostgreSQL | kept in step by `memory_sync` / `syncAuto` |

```yaml
- id: memory-pg
  config:
    projectRoot: 'D:\DSH proj\AI记忆'
    userId: owner
    pythonExe: python
    tools: true
    autoContext: true
    target: server                       # local | server | both
    serverHost: '203.0.113.10'
    serverUser: root
    serverKeyFile: 'C:\path\id_rsa'    # omitted → auto-detect next to syncScript
    serverDsn: 'postgresql://mem:mem@127.0.0.1:5432/mem'
    tunnelPort: 15432
    syncScript: 'D:\proj\shell\proactive\tools\sync_mem.py'
    syncAuto: false
```

Notes:

* **`server` mode never touches the local database.** The sidecar's DSN is rewritten to
  `127.0.0.1:<tunnelPort>`, and `lib/pg_tunnel.py` (paramiko) opens the SSH forward lazily on the
  first memory call. The tunnel is torn down with the plugin.
* Why paramiko instead of `ssh.exe`: the Windows OpenSSH client refuses a private key whose ACL is
  too permissive (`Permissions for 'id_rsa' are too open`), which is impossible to fix in an
  unattended setup. `paramiko` is already the path the sync script uses.
* `both` is **not** two live connections. One source of truth (local) + the sync script, so the
  two databases cannot drift into two different histories. Use the `memory_sync` tool
  (`status` / `pull` / `push` / `both`) or turn on `syncAuto`.
* `memory_health` prints the active target and, in `server` mode, the tunnel description — that is
  the quickest way to confirm which database you are actually talking to.

## Verifying locally

The sidecar speaks plain JSON-lines, so you can test the engine without DSH:

```bash
cd <projectRoot>
printf '%s\n' \
  '{"id":1,"op":"ping"}' \
  '{"id":2,"op":"health"}' \
  '{"id":3,"op":"recall","args":{"query":"timezone","limit":2}}' \
  '{"id":4,"op":"shutdown"}' | python -m app.sidecar
```

A full DSH-level check (tools + auto-injection, no web UI needed):

```bash
dsh --profile <test-profile> "call memory_health once and report it verbatim"
dsh --profile <test-profile> "Do NOT call any tool. List the long-term memory fact keys present in this request."
```

## Design notes

* **Failure direction differs by layer, on purpose.** Graph expansion degrades silently
  (a missing edge just means one recall path contributes nothing); the LLM distiller fails
  loudly (a hallucinated port number would be recalled as fact forever). Memory *tools* here
  never throw — they return text starting with `…失败：`, so a broken database degrades a
  session instead of killing it.
* **`dsn`/`userId` are read at spawn**, so changing them needs a restart of the host.
* **The sidecar writes no files** (`PYTHONDONTWRITEBYTECODE=1`): on Windows the DSH sandbox is
  a `WRITE_RESTRICTED` token, and a stray `__pycache__` write outside the session workspace
  would be denied.

## License

MIT