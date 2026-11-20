# Setting up the MCP server

chist's MCP server implements **MCP 2026-07-28** (verified: it negotiates
`protocol_version = 2026-07-28`). It is read-only and fully stateless.

## Prerequisites

The official MCP Python SDK requires Python 3.10 or newer. macOS ships 3.9 as its
system Python, so provide 3.12+ separately.

```bash
cd /path/to/chist-server
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python mcp      # verified with mcp 2.0.0
```

The ingest side (`python3 -m chist ingest`) keeps running on the system Python 3.9 with
no dependencies. This virtualenv is only for the MCP server.

## Running it

```bash
# stdio (started as a child process by each tool; the usual choice)
.venv/bin/python -m chist.mcp_server

# streamable HTTP (use this when running in Docker)
.venv/bin/python -m chist.mcp_server --transport streamable-http --host 0.0.0.0 --port 8848
```

Select the database with `CHIST_DB` or `--db`. The default is
`~/.local/share/chist/history.db`.

## Registering with each tool

Replace `<REPO>` with the path to your clone.

### Claude Code

```bash
claude mcp add chist -- <REPO>/.venv/bin/python -m chist.mcp_server
# when running over HTTP
claude mcp add --transport http chist http://127.0.0.1:8848/mcp
```

### Codex

```bash
codex mcp add chist -- <REPO>/.venv/bin/python -m chist.mcp_server
# when running over HTTP (--url points at streamable HTTP)
codex mcp add chist --url http://127.0.0.1:8848/mcp
```

### Cursor

`~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "chist": {
      "command": "<REPO>/.venv/bin/python",
      "args": ["-m", "chist.mcp_server"]
    }
  }
}
```

For HTTP use `{"url": "http://127.0.0.1:8848/mcp"}` — the same shape Cursor's other
remote entries use.

### Antigravity

`~/.gemini/config/mcp_config.json` (a symlink from
`~/.gemini/antigravity/mcp_config.json`). That file is currently empty and remote MCP
support is unconfirmed, so try the stdio form first.

```json
{
  "mcpServers": {
    "chist": {
      "command": "<REPO>/.venv/bin/python",
      "args": ["-m", "chist.mcp_server"]
    }
  }
}
```

## Tools provided

| Tool | Purpose | Default / cap |
|---|---|---|
| `search_history(query, source?, cwd?, role?, since?, until?, host?, limit, target?)` | full-text search, returns excerpts | limit 20 / 100 |
| `list_sessions(cwd?, source?, since?, host?, limit)` | list sessions | limit 20 / 100 |
| `get_session(session_id, source?, offset?, limit?, thinking?)` | conversation body | limit 50 / 100, 40,000 chars per response |

`search_history` answers from the search database where the server keeps one (terms of
two or more characters are indexed; text is masked before indexing). Pass
`target="record"` to use the index inside the master database instead (terms under three
characters are left out; text as stored). The result's `target` field says which one
answered. `list_sessions` and `get_session` always read the master database.

All three declare `read_only_hint=True`, `destructive_hint=False` and
`idempotent_hint=True`, and return a structured schema defined with TypedDict.

**There is deliberately no prune/delete tool over MCP.** Deletion stays in the CLI: the
read-only declaration above is what lets clients auto-approve these calls, and a
mistaken deletion cannot be undone.

## What 2026-07-28 changes

| Spec item | Implementation |
|---|---|
| Sessions removed, stateless | All three tools are side-effect-free reads; HTTP runs with `stateless_http=True` |
| `server/discover` (MUST) | Implemented by the SDK; exposed to modern peers only |
| `resultType` required | The SDK sets `complete` |
| `ttlMs` / `cacheScope` | `tools/list` and `server/discover` declare 1 hour and `public` (the tool list is static) |
| Roots deprecated | Follows the recommended migration: cwd arrives as a tool argument |
| Sampling / Logging deprecated | Unused; logs go to stderr |
| HTTP+SSE deprecated | Only streamable HTTP is offered; there is no `--transport sse` |

## Security

Chat history can contain credentials.

- All three tools are **read-only**; no writing tool is exposed
- Output is masked by default (`sk-`, `ghp_`, `AKIA`, `AIza`, `Bearer`, JWTs, PEM keys),
  covering both message bodies and session titles
- `get_session` requires a range and truncates at 40,000 characters per response
- The database file is `600`

**Exposing this over HTTP needs more than the above.** Restrict it to a private network
(Tailscale, WireGuard, or a reverse proxy on a LAN) and require a bearer token. See
[`deploy.md`](deploy.md) for the proxy configuration used by the Docker deployment.

## Known caveats

- **Client-side version support is unverified.** The spec was published 2026-07-28 and
  SDK 2.0.0 carries `LATEST_PROTOCOL_VERSION = 2026-07-28` with
  `DEFAULT_NEGOTIATED_VERSION = 2025-03-26` for backwards compatibility. Some clients
  may still speak the older version; the SDK handles both, so this should be harmless.
- **`server/discover` is invisible to legacy peers.** Calling it over raw JSON-RPC
  without a protocol version in `_meta` returns `-32601 Method not found`, as specified.
- **Searching your own history adds that search to the history.** Ingest is idempotent so
  nothing breaks, but it does add noise.
