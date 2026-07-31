# chist

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](#)
[![MCP](https://img.shields.io/badge/MCP-2026--07--28-green.svg)](#mcp-server)

日本語版は [README.ja.md](README.ja.md)。

Unified full-text search across your **Claude Code**, **Codex**, **Cursor CLI**, and
**Antigravity CLI** chat history. Ingests every transcript into one SQLite database
and lets you search it from a **web UI**, an **MCP server**, or the **command line**.

Ingestion and search run on the Python 3.9 standard library — **zero dependencies**.
Only the MCP server needs an external package.

```bash
python3 -m chist ingest              # incremental import
python3 -m chist search 'trigram'    # full-text search
python3 -m chist show <ext_id>       # read a conversation
```

## Why

Chat history is scattered: each CLI keeps its own format in its own directory, and none
of them can see the others. When you already solved something months ago in a different
tool, you can't find it. chist normalizes all four into one schema and one index, so a
single query reaches everything — and `project_key` (derived from the git remote) groups
the same project across different machines and different tools.

## Features

- **Four sources, one index** — JSONL for Claude Code / Codex / Cursor CLI, and raw
  protobuf-in-SQLite for Antigravity CLI (decoded without a `.proto` definition)
- **Incremental ingest** — unchanged files are skipped by mtime+size; changed ones resume
  from a byte offset, verified against a hash of the first 4 KB
- **CJK-capable search** — FTS5 with the `trigram` tokenizer, so Japanese substrings match
  (the default `unicode61` tokenizer cannot)
- **Web UI** — Svelte SPA with search, session browsing, and statistics
- **MCP server** — lets an agent query its own history (`search_history`, `list_sessions`,
  `get_session`)
- **Multi-machine aggregation** — each machine ingests locally and pushes deltas to a
  central server over HTTP
- **Secret masking** — credentials are masked on display, and always before storage on the
  aggregation server

## Measured performance

macOS, 1,034 files / ~1.86 GB of transcripts:

| Metric | Value |
|---|---|
| Initial ingest (all files) | **14.9 s** |
| Second run (no changes) | < 0.1 s |
| Database size | 130 MB (including the FTS index) |
| Search latency | ~0.05 s |

| source | messages | sessions |
|---|---:|---:|
| claude-code | 49,263 | 769 |
| codex | 1,488 | 10 |
| cursor-cli | 410 | 9 |
| antigravity-cli | 225 | 3 |

## Quick start (local, no Docker)

```bash
git clone https://github.com/you/chist-server.git
cd chist-server
python3 -m chist ingest
python3 -m chist search 'docker compose'
```

The database defaults to `~/.local/share/chist/history.db` (mode `600`). Override it with
`--db` or `CHIST_DB`.

### Options

```
search  -s/--source  filter by source (repeatable)
        --host       filter by machine (repeatable)
        -c/--cwd     substring match on the project path
        -r/--role    user | assistant | system | tool
        --since / --until   ISO 8601 (e.g. 2026-07-01)
        -n/--limit   default 20
        --raw        do not mask secrets
show    --thinking   include thinking blocks
ingest  --limit N    cap the number of files (for smoke tests)
prune   --before / --after  time range (ISO 8601)
        -s/--source  by source
        --session    by session (ext_id or id)
        --match      substring of the body (to undo a bad import)
        --dry-run    report counts without deleting
        -y/--yes     skip confirmation (required non-interactively)
        --vacuum     reclaim file size after deleting
```

## Pruning history

Ingest is append-only, so the database only grows. Measured at 3.8–5.9 KB per message
(the trigram index alone is ~1.9× the body), which is roughly 150–300 MB per year in
daily use. Capacity is rarely the issue — the real need is **removing something
imported by mistake, or a conversation you don't want kept**.

```bash
chist prune --before 2026-01-01 --dry-run     # check the count first
chist prune --before 2026-01-01 --yes --vacuum
chist prune --match 'password' --dry-run      # undo a bad import
chist prune --session <ext_id> --yes
```

Conditions combine with AND. **With no condition it does nothing** — deleting
everything is deliberately not offered (remove the database file instead).

On the aggregation server, run the same CLI inside the container:

```bash
docker exec chist-server python -m chist prune --before 2026-01-01 --dry-run
```

Notes:

- Sources without timestamps (Cursor CLI) are still covered by time ranges — the
  session's end time is used as a fallback, so nothing is silently skipped
- Without `--vacuum` the file does not shrink (space is only reused). VACUUM rewrites
  the database, so it needs free space equal to its size
- Sessions left with no body are removed too (`--keep-sessions` keeps them). Empty
  sessions outside the delete set are never touched
- **If the original log files still exist and you clear `ingest_state`, a re-ingest
  brings the messages back.** Delete the source files to remove them for good
- **No prune tool is exposed over MCP** — the tools are declared `read_only_hint`, and
  a mistaken deletion cannot be undone, so deletion stays in the CLI

## Server deployment

For multiple machines, run the aggregation server in Docker behind an existing reverse
proxy. Each machine ingests locally and pushes only the delta.

```bash
cp .env.example .env      # CHIST_TOKEN, CHIST_HOST, CHIST_BASICAUTH, …
docker compose up -d --build
```

```bash
# on each machine (still dependency-free)
export CHIST_TOKEN=<same token>
python3 -m chist ingest
python3 -m chist push https://chist.example.com
```

The compose file reads every environment-specific value from `.env`, publishes no host
port, and joins the proxy's network directly. The Svelte UI is built in a
`node:22-alpine` stage and baked into the image, so **the server does not need Node
installed**. See [`docs/deploy.md`](docs/deploy.md).

## Web UI

Three tabs — search, sessions, statistics. Opening a URL with `?q=<query>` runs that
search immediately, and `#search` / `#sessions` / `#stats` select the tab, so results are
shareable as links. Light/dark themes follow the OS setting by default.

**Filtering by machine** is available from the web UI, the CLI (`--host`), and MCP.
In the web UI the machine filter and column appear only when two or more machines have
pushed — with a single machine they stay hidden. The list of machines is read from the
`hosts` table at runtime, so adding a machine needs no configuration.

## MCP server

```bash
claude mcp add --transport http chist https://chist.example.com/mcp
```

Implements **MCP 2026-07-28** over stdio or streamable HTTP, read-only and stateless.
Tools: `search_history`, `list_sessions`, `get_session`. See
[`docs/mcp-setup.md`](docs/mcp-setup.md).

For stdio use, the SDK requires Python 3.10+:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python mcp
.venv/bin/python -m chist.mcp_server
```

Only `chist/mcp_server.py` imports `mcp`. Ingest, search, and the CLI stay
dependency-free on the system Python.

## Security model

**Never expose the app directly.** Two route groups cannot authenticate themselves:

| Path | Why | Protection |
|---|---|---|
| `/mcp` | routed by the MCP SDK, bypasses the app's bearer check | proxy forwardAuth → `/auth` |
| `/` `/api/*` | browsers cannot send a bearer token | proxy basicauth |
| `/ingest` `/stats` `/auth` | — | bearer token in the app |
| `/health` | — | unauthenticated by design (health checks) |

The shipped `docker-compose.yml` wires this up as three Traefik routers with an IP
allowlist on each. The server **refuses to start** without `CHIST_TOKEN`, so there is no
path to running it unauthenticated.

### Secret masking

The local database stores raw text and masks on display (`--raw` disables it). The
aggregation server masks **before storing**, so `push --raw` still cannot put live
credentials into the server database.

Detected: known prefixes (`sk-`, `ghp_`, `AKIA`, `AIza`, `Bearer`, JWTs, PEM private
keys), labelled assignments (`TOKEN=…`, `password: …`), and credential-bearing commands
(`curl -u user:pass`, `htpasswd -nbB user pass`).

**A bare random string cannot be masked.** With no label and no prefix there is nothing to
key on, and entropy-based detection produces too many false positives to be usable. Write
secrets with a label (`password: …`) and they will be masked.

**Masking is best-effort, not a guarantee.** It always covers message bodies and session
titles (titles matter: Cursor CLI and Antigravity derive them from the first user
message, so a prompt like "deploy with TOKEN=…" would otherwise leak through the title).
It still has limits:

- bare random strings, as above
- `cwd` / `git_branch` / `project_key` are **not** masked — filtering and cross-machine
  project grouping depend on them. Keep secrets out of paths
- the local database stores bodies **verbatim** and masks on display

**Treat the database file itself as a secret.** The local database is `0600`; the server
keeps it inside a docker volume. Back it up on the same assumption. "Masking exists, so
the database is safe" does not hold.

## Notes and limitations

- **Queries shorter than 3 characters fall back to LIKE.** The `trigram` tokenizer cannot
  MATCH fewer than 3 characters; the CLI and UI switch automatically and say so.
- **`tool_call` / `tool_result` are stored but not indexed.** Large diffs and file dumps
  drown out the signal. They are over 70% of all rows.
- **Resumed sessions keep their original `uuid`.** Claude Code copies prior history into a
  new file on resume, so `UNIQUE (source, ext_id, part)` keeps only one copy. Search never
  shows duplicates; `show <resumed-id>` may be missing the earlier part.
- **Cursor CLI has no timestamps or model names.** `ts` is NULL and session spans fall back
  to file mtime.
- **Codex `reasoning` is usually encrypted.** Only plaintext `summary` blocks are kept.
- **Antigravity CLI is decoded without a `.proto`.** Field numbers were identified from
  real data, so a format change could silently drop messages — watch `chist stats` for
  unexpected drops.

## Documentation

| File | Contents |
|---|---|
| [`docs/deploy.md`](docs/deploy.md) | Server deployment and multi-machine aggregation |
| [`docs/mcp-setup.md`](docs/mcp-setup.md) | Registering the MCP server with each tool |
| [`db/schema.sql`](db/schema.sql) | Schema of record, with design notes |

## License

MIT — see [LICENSE](LICENSE).
