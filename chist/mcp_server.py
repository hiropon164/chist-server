"""MCP server for chist.

Implements MCP 2026-07-28. This is the only module that depends on the official
Python SDK (`mcp` 2.x); it is a thin wrapper over `chist.search` / `chist.db`.
The ingest side (`chist ingest`) stays dependency-free.

  stdio            python -m chist.mcp_server
  streamable HTTP  python -m chist.mcp_server --transport streamable-http --port 8848

2026-07-28 dropped the `initialize` handshake and protocol-level sessions, so this
server is fully stateless: read-only, with no state carried between requests.
"""
import argparse
import os
import sys
from typing import List, Optional, TypedDict

from mcp.server import CacheHint, MCPServer
from mcp.types import ToolAnnotations

from . import db, search


class Hit(TypedDict):
    """One message matched by a search."""
    id: int
    source: str
    role: str
    ts: Optional[str]
    model: Optional[str]
    cwd: Optional[str]
    session_title: Optional[str]
    session_id: Optional[str]   # ext_id, ready to pass straight to get_session
    excerpt: str


class SearchResult(TypedDict):
    mode: str           # 'fts' (full-text index) or 'like' (substring fallback)
    count: int
    hits: List[Hit]
    note: str           # extra remarks, empty when none (NotRequired makes the SDK
                        # insert default=None, which then fails validation)


class SessionInfo(TypedDict):
    id: str
    source: str
    title: Optional[str]
    cwd: Optional[str]
    git_branch: Optional[str]
    host: Optional[str]      # which machine the conversation happened on
    started_at: Optional[str]
    ended_at: Optional[str]
    messages: int


class SessionList(TypedDict):
    count: int
    sessions: List[SessionInfo]


class Turn(TypedDict):
    role: str
    kind: str
    ts: Optional[str]
    model: Optional[str]
    text: str


class SessionBody(TypedDict):
    session: SessionInfo
    total: int
    offset: int
    returned: int
    has_more: bool
    messages: List[Turn]

# The tool list is static, so cache it generously and let shared intermediaries
# cache it too ('public').
CACHE_HINTS = {
    'tools/list': CacheHint(ttl_ms=3_600_000, scope='public'),
    'server/discover': CacheHint(ttl_ms=3_600_000, scope='public'),
}

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=False)

# Caps so a single response cannot fill the model's context window.
MAX_LIMIT = 100
MAX_BODY_CHARS = 40_000

server = MCPServer(
    name='chist',
    title='CLI chat history search',
    version='0.1.0',
    instructions=(
        'Search past chat history from Claude Code, Codex, Cursor CLI and Antigravity CLI. '
        'Use it to answer "how did I solve this before?" or "what was discussed in this '
        'project?". Start with search_history to locate something, then read the full '
        'conversation with get_session. Secrets are masked in the results by default.'
    ),
    cache_hints=CACHE_HINTS,
)

_conn = None


def _db():
    global _conn
    if _conn is None:
        _conn = db.connect(os.environ.get('CHIST_DB'), readonly=True)
    return _conn


def _clip(n, default, hi=MAX_LIMIT):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, hi))


@server.tool(
    name='search_history',
    description=(
        'Full-text search over past chat history, returning an excerpt around each match. '
        'Queries of 3+ characters use the full-text index; shorter ones fall back to a '
        'substring search automatically. Pass part of a project path as cwd to restrict '
        'the search to that project.'
    ),
    annotations=READ_ONLY,
)
def search_history(
    query: str,
    source: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    role: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    host: Optional[List[str]] = None,
    limit: int = 20,
) -> SearchResult:
    """
    Args:
        query: the search term.
        source: restrict to these sources: claude-code / codex / cursor-cli / antigravity-cli.
        cwd: substring match on the project path.
        role: user / assistant / system / tool.
        since: at or after this time (ISO 8601, e.g. 2026-07-01).
        until: at or before this time.
        host: restrict to conversations held on these machines.
        limit: maximum results (default 20, capped at 100).
    """
    rows, mode = search.search(
        _db(), query, source=source, cwd=cwd, role=role, since=since, until=until,
        limit=_clip(limit, 20), masked=True, host=host)
    hits = []
    for r in rows:
        hits.append({
            'id': r['id'],
            'source': r['source'],
            'role': r['role'],
            'ts': r['ts'],
            'model': r['model'],
            'cwd': r['cwd'],
            'session_title': r['title'],
            'session_id': r['session_id'],
            'excerpt': r['hit'].replace('\x02', '**').replace('\x03', '**'),
        })
    notes = []
    if mode == 'like':
        notes.append('The query is under 3 characters, so a substring search was used '
                     'instead of the full-text index.')
    if not hits:
        notes.append('No matches. Try a shorter term or drop the cwd filter.')
    return {'mode': mode, 'count': len(hits), 'hits': hits, 'note': ' '.join(notes)}


@server.tool(
    name='list_sessions',
    description='List conversation sessions, newest first. Can be filtered by project, '
                'machine or time range.',
    annotations=READ_ONLY,
)
def list_sessions(
    cwd: Optional[str] = None,
    source: Optional[List[str]] = None,
    since: Optional[str] = None,
    host: Optional[List[str]] = None,
    limit: int = 20,
) -> SessionList:
    """
    Args:
        cwd: substring match on the project path.
        source: restrict to these sources.
        since: at or after this time (ISO 8601).
        host: restrict to these machines.
        limit: maximum results (default 20, capped at 100).
    """
    rows = search.sessions(_db(), cwd=cwd, source=source, since=since,
                           limit=_clip(limit, 20), host=host)
    return {
        'count': len(rows),
        'sessions': [{
            'id': r['ext_id'],
            'source': r['source'],
            'title': r['title'],
            'cwd': r['cwd'],
            'git_branch': r['git_branch'],
            'host': r['host'],
            'started_at': r['started_at'],
            'ended_at': r['ended_at'],
            'messages': r['msg_count'],
        } for r in rows],
    }


@server.tool(
    name='get_session',
    description=(
        'Return the body of one session. Pass an id returned by search_history or '
        'list_sessions. Long conversations are truncated, so advance offset to read on.'
    ),
    annotations=READ_ONLY,
)
def get_session(
    session_id: str,
    source: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
    thinking: bool = False,
) -> SessionBody:
    """
    Args:
        session_id: the session's ext_id.
        source: disambiguate when the same id exists in several sources. Pass the source
            returned by search_history.
        offset: index of the first message to return.
        limit: maximum messages (default 50, capped at 100).
        thinking: include thinking blocks when true.
    """
    kinds = ['message'] + (['thinking'] if thinking else [])
    sess, msgs = search.show(_db(), session_id, masked=True, kinds=kinds, source=source)
    if not sess:
        # The SDK turns exceptions into an is_error=True result, so the return type
        # stays a single shape.
        raise ValueError('Session not found: %s' % session_id)

    offset = max(0, int(offset or 0))
    limit = _clip(limit, 50)
    window = msgs[offset:offset + limit]

    out = []
    used = 0
    truncated = False
    for m in window:
        text = m['text']
        if used + len(text) > MAX_BODY_CHARS:
            text = text[:max(0, MAX_BODY_CHARS - used)] + '… (truncated)'
            truncated = True
        used += len(text)
        out.append({'role': m['role'], 'kind': m['kind'], 'ts': m['ts'],
                    'model': m['model'], 'text': text})
        if truncated:
            break

    return {
        'session': {'id': sess['ext_id'], 'source': sess['source'],
                    'title': sess['title'], 'cwd': sess['cwd'],
                    'git_branch': sess['git_branch'],
                    'host': sess['host'],
                    'started_at': sess['started_at'], 'ended_at': sess['ended_at'],
                    'messages': sess['msg_count']},
        'total': len(msgs),
        'offset': offset,
        'returned': len(out),
        'has_more': offset + len(out) < len(msgs),
        'messages': out,
    }


def main(argv=None):
    p = argparse.ArgumentParser(prog='chist-mcp', description='MCP server for chist')
    p.add_argument('--transport', default='stdio',
                   choices=['stdio', 'streamable-http'],
                   help='defaults to stdio; use streamable-http when running in Docker')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8848)
    p.add_argument('--db', help='database path (defaults to CHIST_DB or '
                                '~/.local/share/chist/history.db)')
    args = p.parse_args(argv)

    if args.db:
        os.environ['CHIST_DB'] = args.db

    path = db.db_path(os.environ.get('CHIST_DB'))
    if not os.path.exists(path):
        sys.stderr.write('No database at %s\nRun `python3 -m chist ingest` first.\n' % path)
        return 1

    if args.transport == 'stdio':
        server.run(transport='stdio')
    else:
        # 2026-07-28 removed protocol-level sessions, so run stateless. That also lets
        # a load balancer route requests freely.
        server.run(transport='streamable-http', host=args.host, port=args.port,
                   stateless_http=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
