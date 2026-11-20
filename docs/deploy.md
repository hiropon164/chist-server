# Deploying the aggregation server (multi-machine)

Each machine ingests locally, sends normalised data to the aggregation server, and every
tool then queries the server's MCP endpoint. The assumption is that only a private
network can reach it, published through an existing **Traefik**.

```
┌─ Mac / PC (agent) ─────────┐         ┌─ Docker host ─────────────────────────┐
│ chist ingest → chist push  │  HTTPS  │ Traefik :443 (letsencrypt)            │
│   zero deps / Python 3.9   │────────▶│   Host(`chist.example.com`)           │
│                            │         │   ipallowlist → forwardAuth(/auth)    │
│ Claude Code / Codex /      │   MCP   │              │                        │
│ Cursor / Antigravity       │────────▶│              ▼                        │
└────────────────────────────┘         │ chist-server :8848 (no host port)     │
                                        │   POST /ingest   POST /mcp            │
                                        │   GET  /health   /stats   /auth       │
                                        │   SQLite (volume: chist-data)         │
                                        └───────────────────────────────────────┘
```

No host port is opened. The container joins the same docker network as Traefik, which
connects to port 8848 directly.

**Ingest must happen on the agent side.** Chat logs record neither the hostname nor the
git remote, and `git -C <cwd> remote get-url origin` only works on the machine that has
the repository.

## Server side

Put the repository on the Docker host and build it there. No image registry is needed,
and the arm64 / amd64 difference is absorbed at build time.

```bash
cd /srv/chist-server
cp .env.example .env
vi .env          # fill in CHIST_TOKEN

docker compose up -d --build
docker compose logs -f chist
```

What goes in `.env` (see `.env.example` for the annotated version):

```
CHIST_TOKEN=<output of openssl rand -hex 32>
CHIST_HOST=chist.example.com
CHIST_ALLOW=192.168.1.0/24
CHIST_BASICAUTH=<output of htpasswd -nbB>
CHIST_NETWORK=traefik
CHIST_ENTRYPOINT=websecure
CHIST_CERTRESOLVER=letsencrypt
```

`docker-compose.yml` reads every environment-specific value from `.env`, so moving to a
different host never means editing the compose file.

Check it:

```bash
curl -s -H "Authorization: Bearer $CHIST_TOKEN" https://chist.example.com/health
# {"ok":true,"messages":0}
```

Distribute the same token to every agent. **The server refuses to start without it.**

### What to get right in Traefik

**1. Match the network name.** Find the network your existing Traefik is on with
`docker network ls` and put it in `CHIST_NETWORK` (compose usually prefixes the project
name, giving something like `<project>_traefik`). The compose file only joins it with
`external: true`, so Traefik's own configuration is untouched. Everything is done with
labels, so Traefik does not need restarting.

**2. Do not attach a buffering middleware.** MCP's streamable HTTP returns the POST
response as a stream. Traefik does not buffer by default, so it passes through — but a
`buffering` middleware will make the response never arrive.

**3. Name resolution.** `CHIST_HOST` has to resolve from every machine. Add an A record
to your router's DNS, or write the Docker host's IP into each machine's `/etc/hosts`
(`C:\Windows\System32\drivers\etc\hosts` on Windows).

Where wildcard DNS points at a public IP, LAN traffic can come back through hairpin NAT
and arrive as the router's **external** address. In that case **`CHIST_ALLOW` must
include that public IP too**, or requests from the LAN get a 403.

**4. Always protect `/mcp` with forwardAuth (important).** `/ingest` and `/stats` verify
the bearer token inside the app, but `/mcp` is routed by the MCP SDK and never reaches
that check. Passing it straight through means **the whole history is readable with no
token** (measured: `tools/list` returns 200). The compose labels point forwardAuth at
`/auth`.

```yaml
- traefik.http.routers.chist-agent.middlewares=chist-allow,chist-auth
- traefik.http.middlewares.chist-auth.forwardauth.address=http://chist-server:8848/auth
```

`/auth` looks only at the Authorization header and returns 200 or 401. Traefik's health
check bypasses middleware, so `/health` is never blocked by it.

**5. Guard the web UI with basicauth.** `/` and `/api/*` are used from a browser, which
cannot send a bearer token, so they get their own router with basicauth. Generate the
hash with:

```bash
docker run --rm httpd:2-alpine htpasswd -nbB <user> <pass>
```

Paste the output into `CHIST_BASICAUTH` in `.env` (no `$` escaping is needed there).

That makes three routers. Priorities are set explicitly so overlapping paths cannot be
mixed up.

| Router | Matches | Authentication | priority |
|---|---|---|---|
| `chist-health` | `/health` | none | 300 |
| `chist-agent` | `/ingest` `/mcp` `/stats` | forwardAuth (bearer) | 200 |
| `chist-web` | everything else (`/`, `/api/*`) | basicauth | 100 |

**6. Restrict who can reach it.** List the allowed source CIDRs in `CHIST_ALLOW`.

```yaml
- traefik.http.middlewares.chist-allow.ipallowlist.sourcerange=${CHIST_ALLOW}
```

`ipallowlist` is the Traefik v3 spelling (`ipwhitelist` from v2 is deprecated).

**7. TLS.** Serve on `websecure` (:443) with `certresolver=letsencrypt`. The bearer
token travels in the clear over plain HTTP, so do not run it that way.

### Deleting history and disk usage

Ingest only appends, so the database grows. Measured at 3.8–5.9 KB per message (the
trigram index alone is about 1.9× the body), which works out to roughly 150–300 MB per
year in daily use. Capacity is rarely the problem, but there is a command for removing
mistaken imports or conversations you would rather not keep.

```bash
docker exec chist-server python -m chist prune --before 2026-01-01 --dry-run
docker exec chist-server python -m chist prune --before 2026-01-01 --yes --vacuum
```

Nothing happens unless at least one condition (`--before` / `--after` / `--source` /
`--session` / `--match`) is given, and the file only shrinks with `--vacuum`.

Note that a backup script archiving the whole volume will copy the entire database every
run, so its efficiency drops as the database grows. Consider pruning once it reaches the
gigabyte range.

### The search database

The compose file sets `CHIST_SEARCH_DB=/data/search.db`. That makes the server keep a
second file beside the record: a copy of the searchable text with a trigram index and a
bigram index, which is what searches (MCP, CLI in the container, web UI) answer from by
default. It is derived -- delete it and `chist reindex --rebuild` brings it back -- and
it roughly doubles the space the volume needs (measured: 248 MB record + 256 MB copy at
78k messages).

```bash
docker exec chist-server python -m chist reindex            # catch up
docker exec chist-server python -m chist reindex --check    # compare with the record; exit 1 if they differ
docker exec chist-server python -m chist reindex --rebuild  # throw away and refill (one transaction)
```

What it buys: two-character terms are indexed (the record's own index drops any term
under three characters, which silently widens the search), and text is masked with the
*current* rules as it is copied. Change a masking rule, restart the container, and the
copy is rebuilt in the background; searches keep seeing the old copy until it commits.

The record's own masking is applied once, when a row is stored. To bring rows stored
under older rules up to date -- this overwrites them and cannot be undone:

```bash
docker exec chist-server python -m chist remask          # count only
docker exec chist-server python -m chist remask --yes
```

`tools/search_check.py` runs `reindex --check` from the Docker host and sends a Gotify
message when the two disagree *or when the check could not be run*. Put it on a daily
timer; it appends one line per run to `~/.local/state/chist/search-check.log`.

Remove the `CHIST_SEARCH_DB` line to go back to the single-file behaviour.

### Where the database lives

Use the named volume (`chist-data`). **Never put SQLite on NFS or SMB** — its locking
breaks.

```bash
# backup
docker run --rm -v chist-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/chist-$(date +%Y%m%d).tar.gz -C /data .
```

`search.db` can be left out of backups: it is rebuilt from `history.db`.

## Agent side

On each machine. **The MCP SDK is not needed**; any Python 3.9 or newer works.

```bash
export CHIST_TOKEN=<the same token as the server>

python3 -m chist ingest                             # local ingest
python3 -m chist push https://chist.example.com     # send the delta
```

To run it periodically from cron or launchd, just put those two lines in order. Both are
idempotent.

### What gets sent

By default only `message` / `thinking` / `artifact`. `tool_call` and `tool_result` make
up about 70% of all rows and are not in the FTS index, so they are skipped. Pass
`--include-tools` to include them.

Measured on one Mac:

| | rows | database |
|---|---:|---:|
| local database (everything, including tools) | 51,386 | 125 MB |
| sent to the server | 13,631 | 50 MB |

The initial push of 13,631 rows took about **6.6 s** (26 batches). After that only the
delta moves — a few hundred KB per day.

### Secret masking

The agent masks before sending. Local-only use could get by with masking at display
time, but the risk of collecting live credentials on a server several machines can reach
is greater. The receiving side (`/ingest`) masks again before storing, so even
`--raw` cannot put live credentials into the server database.

## Connecting each tool over MCP

```bash
claude mcp add --transport http chist https://chist.example.com/mcp
codex  mcp add chist --url https://chist.example.com/mcp
```

For Cursor, put `{"chist": {"url": "https://chist.example.com/mcp"}}` in
`~/.cursor/mcp.json`. Antigravity's remote MCP support is unconfirmed — see
[`mcp-setup.md`](mcp-setup.md).

2026-07-28 removed protocol-level sessions, so the server runs with
`stateless_http=True`. That also leaves request routing free if it is ever put behind a
load balancer.

## Grouping projects across machines

`sessions.project_key` is what ties the same project together across machines.

```
with a git remote : github.com/you/example-project   ← matches across machines
without one       : local:example-app                ← approximated from the basename
```

On the Mac measured above, only 2 of 33 distinct cwds had a resolvable git remote. The
rest fall back to `local:<basename>`, which means **two machines with a same-named
directory are treated as the same project**. Set a git remote on anything that must stay
distinct.

```bash
curl -s -H "Authorization: Bearer $CHIST_TOKEN" https://chist.example.com/stats
```

## Authentication and exposure

- `/ingest`, `/stats` and `/auth` require a bearer token. Only `/health` is
  unauthenticated (for health checks)
- **`/mcp`, `/` and `/api/*` are not authenticated inside the app** — the first because
  it is routed by the MCP SDK, the others because browsers cannot send a bearer token.
  They are covered by forwardAuth and basicauth respectively. Never expose the app
  without the proxy
- Without `CHIST_TOKEN` the server **refuses to start** (exit 2). There is no path to
  running it unauthenticated
- No host port is published, so Traefik is the only way in, further narrowed by the IP
  allowlist
- The container runs as non-root (uid 10001)
- The receiving side masks with `mask()` before storing, covering message bodies and
  session titles alike
- **Reaching this from the internet needs more.** Add a rate-limiting middleware
  (`ratelimit`) at minimum. Chat history is among the most sensitive data you hold; a
  token alone is not enough for a public-facing deployment

## Verified behaviour

Measured with the server and an agent wired together directly:

| Check | Result |
|---|---|
| `/ingest` without authentication | 401 |
| initial push | 13,631 rows accepted / 6.6 s |
| second push (no delta) | 0 rows sent |
| `--reset` full resend | 12,631 rows sent / **0 accepted** (`UNIQUE (source, ext_id, part)` absorbs duplicates) |
| MCP against the server database | `protocol_version = 2026-07-28`, search works |
| `project_key` aggregation | e.g. 374 rows under `github.com/you/example-project` |

The `messages` count from `list_sessions` is lower than `chist sessions` reports
locally, because tool rows are not sent. That is by design.
