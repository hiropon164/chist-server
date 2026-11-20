"""Aggregation server: the ingest API and MCP in one process.

Runs on the Docker host. Agents on each machine POST normalised data with
`chist push`, and tools reach MCP on `/mcp` at the same port.

  POST /ingest   ingest from an agent (bearer token required)
  GET  /health   liveness check (unauthenticated)
  GET  /stats    aggregates (bearer token required)
  GET  /auth     verification only, for the proxy's forwardAuth (bearer token required)
  POST /mcp      MCP 2026-07-28 (streamable HTTP)
  GET  /         web UI (the Svelte SPA as a single HTML file)
  GET  /api/*    read-only API for the web UI

Tool definitions are shared with chist.mcp_server.

**Never expose this app on its own.**
`/mcp` is routed by the MCP SDK, so it does not pass through `_authorized` here.
`/` and `/api/*` are meant for browsers, which cannot send a bearer token. Both are
protected at the reverse proxy instead (see the labels in docker-compose.yml).

  /health              unauthenticated (health checks)
  /ingest /stats /mcp  forwardAuth -> /auth verifies the bearer token
  / and /api/*         basicauth
  everything           restricted to the LAN by an IP allowlist
"""
import argparse
import hmac
import json
import os
import pathlib
import sys

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from . import db, search, searchdb
from .mask import mask
from .mcp_server import server

TOKEN = None
_conn = None

# Per-request cap. The default batch of 500 messages fits with room to spare.
MAX_BODY = 64 * 1024 * 1024


def _db():
    global _conn
    if _conn is None:
        _conn = db.connect(os.environ.get('CHIST_DB'))
    return _conn


def _authorized(request: Request) -> bool:
    # No 'allow when no token is set' escape hatch — main refuses to start instead.
    if not TOKEN:
        return False
    auth = request.headers.get('authorization', '')
    if not auth.startswith('Bearer '):
        return False
    # Constant-time compare so the token cannot be guessed from timing
    return hmac.compare_digest(auth[7:], TOKEN)


def _items(payload, key):
    """Pull an array field out of the payload. Malformed input yields [], never raises."""
    v = payload.get(key)
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, dict)]


@server.custom_route('/health', methods=['GET'])
async def health(request: Request):
    try:
        n = _db().execute('SELECT COUNT(*) FROM messages').fetchone()[0]
    except Exception as e:
        return JSONResponse({'ok': False, 'error': str(e)}, status_code=500)
    return JSONResponse({'ok': True, 'messages': n})


@server.custom_route('/auth', methods=['GET', 'POST'])
async def auth(request: Request):
    """For the proxy's forwardAuth: inspect the header and return 200 or 401.

    `/mcp` is created by the MCP SDK and is not a route of this module, so having the
    proxy call this endpoint is what protects the whole surface.
    """
    if not _authorized(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    return JSONResponse({'ok': True})


@server.custom_route('/ingest', methods=['POST'])
async def ingest(request: Request):
    if not _authorized(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)

    declared = request.headers.get('content-length')
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        return JSONResponse({'error': 'payload too large'}, status_code=413)
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return JSONResponse({'error': 'payload too large'}, status_code=413)
    try:
        payload = json.loads(raw)
    except ValueError as e:
        return JSONResponse({'error': 'invalid json: %s' % e}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({'error': 'payload must be an object'}, status_code=400)

    conn = _db()
    host = (payload.get('host') or {}).get('host')
    os_name = (payload.get('host') or {}).get('os')
    if host:
        conn.execute(
            'INSERT INTO hosts (host, os, last_seen) VALUES (?,?,datetime("now")) '
            'ON CONFLICT(host) DO UPDATE SET os=excluded.os, last_seen=excluded.last_seen',
            (host, os_name))

    # Settle sessions first. Never clobber a non-NULL value on an existing row.
    sid = {}
    for s in _items(payload, 'sessions'):
        if not s.get('source') or not s.get('ext_id'):
            continue
        conn.execute(
            'INSERT INTO sessions (source, ext_id, title, cwd, git_branch, host,'
            ' project_key, started_at, ended_at) VALUES (?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(source, ext_id) DO UPDATE SET '
            '  title       = COALESCE(excluded.title, sessions.title),'
            '  cwd         = COALESCE(sessions.cwd, excluded.cwd),'
            '  git_branch  = COALESCE(excluded.git_branch, sessions.git_branch),'
            '  host        = COALESCE(sessions.host, excluded.host),'
            '  project_key = COALESCE(excluded.project_key, sessions.project_key),'
            '  started_at  = MIN(COALESCE(excluded.started_at, sessions.started_at),'
            '                    COALESCE(sessions.started_at, excluded.started_at)),'
            '  ended_at    = MAX(COALESCE(excluded.ended_at, sessions.ended_at),'
            '                    COALESCE(sessions.ended_at, excluded.ended_at))',
            # Some sources derive the title from the first user message, so it needs the
            # same masking as the body.
            (s.get('source'), s.get('ext_id'), mask(s.get('title')), s.get('cwd'),
             s.get('git_branch'), host, s.get('project_key'),
             s.get('started_at'), s.get('ended_at')))
        row = conn.execute('SELECT id FROM sessions WHERE source=? AND ext_id=?',
                           (s.get('source'), s.get('ext_id'))).fetchone()
        if row:
            sid[(s.get('source'), s.get('ext_id'))] = row['id']

    rows = []
    skipped = 0
    for m in _items(payload, 'messages'):
        if not m.get('source') or not m.get('ext_id'):
            skipped += 1
            continue
        key = (m.get('source'), m.get('session_ext_id'))
        if key not in sid:
            row = conn.execute('SELECT id FROM sessions WHERE source=? AND ext_id=?',
                               key).fetchone()
            if not row:
                skipped += 1
                continue
            sid[key] = row['id']
        rows.append((sid[key], m.get('source'), m.get('cwd'), m.get('ext_id'),
                     m.get('part') or 0, m.get('seq'), m.get('role'),
                     m.get('kind') or 'message', m.get('ts'), m.get('model'),
                     m.get('provider'),
                     # The sender masks in push too, but --raw bypasses that. Always mask
                     # again here, before anything is stored.
                     mask(m.get('text') or ''), None, None))

    accepted = 0
    if rows:
        cur = conn.executemany(
            'INSERT OR IGNORE INTO messages '
            '(session_id, source, cwd, ext_id, part, seq, role, kind, ts, model,'
            ' provider, text, src_path, src_pos) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            rows)
        accepted = max(cur.rowcount, 0)

    if sid:
        conn.execute(
            'UPDATE sessions SET msg_count = ('
            '  SELECT COUNT(*) FROM messages WHERE messages.session_id = sessions.id'
            ') WHERE id IN (%s)' % ','.join('?' * len(sid)), list(sid.values()))
    conn.commit()

    # After the commit, and never able to fail the request: the record is what
    # the agent was told is stored. See searchdb.after_write.
    if accepted:
        searchdb.after_write(conn, log=lambda m: sys.stderr.write(m + '\n'))

    return JSONResponse({'ok': True, 'received': len(rows), 'accepted': accepted,
                         'duplicates': len(rows) - accepted, 'skipped': skipped})


def _stats_payload(conn, projects=20):
    out = {'hosts': [], 'sources': [], 'projects': [], 'totals': {}}
    for r in conn.execute('SELECT host, os, last_seen FROM hosts ORDER BY host'):
        out['hosts'].append(dict(r))
    for r in conn.execute(
            'SELECT source, COUNT(*) n, MIN(ts) first, MAX(ts) last '
            'FROM messages GROUP BY source ORDER BY n DESC'):
        out['sources'].append(dict(r))
    for r in conn.execute(
            'SELECT s.project_key, COUNT(m.id) n, COUNT(DISTINCT s.host) hosts,'
            '       COUNT(DISTINCT s.source) tools FROM sessions s '
            'JOIN messages m ON m.session_id = s.id '
            'WHERE s.project_key IS NOT NULL GROUP BY s.project_key '
            'ORDER BY n DESC LIMIT ?', (projects,)):
        out['projects'].append(dict(r))
    row = conn.execute(
        'SELECT (SELECT COUNT(*) FROM messages) messages,'
        '       (SELECT COUNT(*) FROM sessions) sessions,'
        '       (SELECT COUNT(*) FROM hosts) hosts').fetchone()
    out['totals'] = dict(row)
    return out


@server.custom_route('/stats', methods=['GET'])
async def stats(request: Request):
    if not _authorized(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    return JSONResponse(_stats_payload(_db()))


# ------------------------------------------------------------------ Web UI
# Everything below assumes basicauth at the proxy; no bearer token is required here.

WEB_INDEX = pathlib.Path(__file__).resolve().parent.parent / 'web' / 'index.html'
_index_cache = None


def _str(request, name, default=None):
    v = request.query_params.get(name)
    return v if v else default


def _int(request, name, default, lo=1, hi=200):
    try:
        return max(lo, min(int(request.query_params.get(name)), hi))
    except (TypeError, ValueError):
        return default


@server.custom_route('/', methods=['GET'])
async def index(request: Request):
    """Serve the SPA. The build output is a single HTML file, so one route suffices."""
    global _index_cache
    if _index_cache is None:
        if not WEB_INDEX.exists():
            return JSONResponse({'error': 'web UI is not bundled in this image'}, status_code=404)
        _index_cache = WEB_INDEX.read_text(encoding='utf-8')
    return HTMLResponse(_index_cache)


@server.custom_route('/api/search', methods=['GET'])
async def api_search(request: Request):
    q = _str(request, 'q', '')
    if not q:
        return JSONResponse({'mode': '', 'count': 0, 'hits': [],
                             'note': 'Enter a search term.'})
    sources = [s for s in request.query_params.getlist('source') if s]
    hosts = [h for h in request.query_params.getlist('host') if h]
    try:
        target, engine, fell_back = search.resolve_target(_str(request, 'target'))
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    rows, mode, dropped = search.search(
        _db(), q, source=sources or None, cwd=_str(request, 'cwd'),
        role=_str(request, 'role'), since=_str(request, 'since'),
        until=_str(request, 'until'), limit=_int(request, 'limit', 30),
        masked=True, host=hosts or None, engine=engine)
    hits = [{'id': r['id'], 'source': r['source'], 'role': r['role'], 'ts': r['ts'],
             'model': r['model'], 'cwd': r['cwd'], 'session_title': r['title'],
             'session_id': r['session_id'], 'excerpt': r['hit']} for r in rows]
    notes = [fell_back] if fell_back else []
    if dropped:
        notes.append('Left out of the index search: %s (under %d characters).'
                     % (', '.join(repr(t) for t in dropped), search.floor(engine)))
    if mode == 'like':
        notes.append('Every term is under %d characters, so the longest one was '
                     'matched as a substring.' % search.floor(engine))
    if not hits:
        notes.append('No matches. Try dropping short terms, or a filter.')
    note = ' '.join(notes)
    return JSONResponse({'mode': mode, 'target': target, 'count': len(hits),
                         'hits': hits, 'note': note})


@server.custom_route('/api/sessions', methods=['GET'])
async def api_sessions(request: Request):
    sources = [s for s in request.query_params.getlist('source') if s]
    hosts = [h for h in request.query_params.getlist('host') if h]
    rows = search.sessions(
        _db(), cwd=_str(request, 'cwd'), source=sources or None,
        since=_str(request, 'since'), limit=_int(request, 'limit', 50),
        host=hosts or None)
    return JSONResponse({'count': len(rows), 'sessions': [
        {'id': r['ext_id'], 'source': r['source'], 'title': r['title'], 'cwd': r['cwd'],
         'git_branch': r['git_branch'], 'host': r['host'], 'project_key': r['project_key'],
         'started_at': r['started_at'], 'ended_at': r['ended_at'],
         'messages': r['msg_count']} for r in rows]})


@server.custom_route('/api/session', methods=['GET'])
async def api_session(request: Request):
    """Return a conversation body via ?id=<ext_id>. Taken from the query, not the path."""
    ext_id = _str(request, 'id')
    if not ext_id:
        return JSONResponse({'error': 'id is required'}, status_code=400)
    kinds = ['message'] + (['thinking'] if _str(request, 'thinking') else [])
    # ext_id is only unique together with source, so use source when it is supplied.
    sess, msgs = search.show(_db(), ext_id, masked=True, kinds=kinds,
                             source=_str(request, 'source'))
    if not sess:
        return JSONResponse({'error': 'session not found'}, status_code=404)
    offset = _int(request, 'offset', 0, lo=0, hi=10 ** 7)
    limit = _int(request, 'limit', 200, hi=1000)
    window = msgs[offset:offset + limit]
    return JSONResponse({
        'session': {'id': sess['ext_id'], 'source': sess['source'], 'title': sess['title'],
                    'cwd': sess['cwd'], 'git_branch': sess['git_branch'],
                    'host': sess['host'], 'project_key': sess['project_key'],
                    'started_at': sess['started_at'], 'ended_at': sess['ended_at'],
                    'messages': sess['msg_count']},
        'total': len(msgs), 'offset': offset, 'returned': len(window),
        'has_more': offset + len(window) < len(msgs),
        # The same ext_id also exists in another source; re-call with source to pin it.
        'ambiguous': bool(sess.get('ambiguous')),
        'messages': [{'role': m['role'], 'kind': m['kind'], 'ts': m['ts'],
                      'model': m['model'], 'text': m['text']} for m in window],
    })


@server.custom_route('/api/stats', methods=['GET'])
async def api_stats(request: Request):
    return JSONResponse(_stats_payload(_db(), projects=_int(request, 'projects', 20)))


def main(argv=None):
    global TOKEN
    p = argparse.ArgumentParser(prog='chist-server', description='chist aggregation server')
    p.add_argument('--host', default='0.0.0.0')
    p.add_argument('--port', type=int, default=8848)
    p.add_argument('--db', help='defaults to CHIST_DB or ~/.local/share/chist/history.db')
    p.add_argument('--token', help='defaults to the CHIST_TOKEN environment variable')
    args = p.parse_args(argv)

    if args.db:
        os.environ['CHIST_DB'] = args.db
    TOKEN = args.token or os.environ.get('CHIST_TOKEN')
    if not TOKEN:
        sys.stderr.write(
            'CHIST_TOKEN is not set. Refusing to start rather than serve history '
            'unauthenticated.\n'
            '  Generate one with: openssl rand -hex 32\n')
        return 2

    _db()  # create or migrate the schema before serving
    if searchdb.enabled():
        # In the background so the server answers at once: a rebuild takes
        # minutes, and until it commits searches see the copy as it was.
        import threading
        threading.Thread(
            target=searchdb.ensure_current,
            args=(db.db_path(os.environ.get('CHIST_DB')),),
            kwargs={'log': lambda m: sys.stderr.write(m + '\n')},
            name='searchdb-ensure-current', daemon=True).start()
    sys.stderr.write('chist-server http://%s:%d  (MCP: /mcp, ingest: /ingest)\n'
                     % (args.host, args.port))
    # 2026-07-28 removed protocol-level sessions, so run stateless
    server.run(transport='streamable-http', host=args.host, port=args.port,
               stateless_http=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
