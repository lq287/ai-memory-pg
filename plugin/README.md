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
| `memory_health` | connectivity, embedder, scale, health counters, **and which database is active** |
| `memory_sync` | two-way sync with the server database (`status` / `pull` / `push` / `both`) |

Plus `autoContext` (default **on**): before each turn's first step the plugin injects one
plugin-sourced message containing the preamble, budgeted by `contextBudget` characters.
Memory being unavailable **never** breaks the request — injection is skipped and tools return
an error *text*.

## Requirements

* **Node ≥ 20** (provided by DSH)
* **Python 3.11+** with the memory project's dependencies in `<projectRoot>/.pylibs`
  (`psycopg`, `pgvector`, `onnxruntime`, `tokenizers`, `numpy`; `paramiko` for `target=server|both`)
* **PostgreSQL 14+ with pgvector**, memory-project schema loaded

## Install

```bash
dsh plugin --profile <name> add dsh-memory-pg   # 1. add the package
# 2. add "dsh-memory-pg" to that profile's dsh.profile.bundles
# 3. override id memory-pg's config there (projectRoot is required — there is no sane default)
# 4. restart dsh
```

**All details live in [`INSTALL.md`](INSTALL.md)** (Chinese): three ready-to-paste config
blocks for `target: local | server | both`, the full field table, the tgz-for-another-machine
checklist, a troubleshooting table, and how to switch or remove the plugin.

Only a handful of keys matter to most people:

| Key | Default | Meaning |
|---|---|---|
| `projectRoot` | — (**required**) | memory project dir (contains `app/`, `.pylibs/`) |
| `userId` | `owner` | whose memories |
| `dsn` | env `MEM_PG_DSN` | local PostgreSQL DSN |
| `target` | `local` | `local` = local DB only · `server` = server DB via SSH tunnel (local untouched) · `both` = local DB, kept in step by `memory_sync` |

```bash
dsh --profile <name> --dump-config | grep -A6 'dsh-memory-pg'   # confirm the config composed
cd <projectRoot> && printf '%s\n' '{"id":1,"op":"health"}' '{"id":2,"op":"shutdown"}' \
  | python -m app.sidecar                                        # confirm the engine answers
```

## Design notes

* **Thin front-end, one implementation.** The plugin does not re-implement recall,
  arbitration, embeddings or digests in JavaScript. It spawns a long-lived
  `python -m app.sidecar` and talks JSON-lines to it: one cold start (~1 s: Python + psycopg +
  ONNX model + SCRAM), then ~10–50 ms per call. Two implementations would drift — this project
  has been bitten by exactly that twice.
* **Failure direction differs by layer, on purpose.** Graph expansion degrades silently (a
  missing edge just means one recall path contributes nothing); the LLM distiller fails loudly
  (a hallucinated port number would be recalled as fact forever). The memory *tools* here never
  throw — they return text starting with `…失败：`, so a broken database degrades a session
  instead of killing it.
* **`server` mode never touches the local database.** The sidecar's DSN is rewritten to
  `127.0.0.1:<tunnelPort>` and `lib/pg_tunnel.py` (paramiko) opens the forward lazily. paramiko
  rather than `ssh.exe` because the Windows OpenSSH client refuses a private key whose ACL is
  too permissive, which is unfixable in an unattended setup.
* **`both` is not two live connections.** One source of truth (local) plus the sync script, so
  the two databases cannot drift into two histories.
* **`dsn` / `userId` / `target` are read at spawn**, so changing them needs a host restart.
* **The sidecar writes no files** (`PYTHONDONTWRITEBYTECODE=1`): on Windows the DSH sandbox is a
  `WRITE_RESTRICTED` token, and a stray `__pycache__` write outside the session workspace would
  be denied.

## License

MIT — see [LICENSE](LICENSE). The engine lives at
[github.com/lq287/ai-memory-pg](https://github.com/lq287/ai-memory-pg).
