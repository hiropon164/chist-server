"""chist の MCP サーバ。

MCP 2026-07-28 対応。公式 Python SDK (`mcp` 2.x) に依存する唯一のモジュールで、
`chist.search` / `chist.db` を薄く包むだけ。取り込み側（`chist ingest`）は
依存ゼロのまま保つ。

  stdio            python -m chist.mcp_server
  streamable HTTP  python -m chist.mcp_server --transport streamable-http --port 8848

2026-07-28 で `initialize` ハンドシェイクとセッションが廃止されたため、
このサーバは完全にステートレス。読み取り専用でリクエスト間の状態を持たない。
"""
import argparse
import os
import sys
from typing import List, Optional, TypedDict

from mcp.server import CacheHint, MCPServer
from mcp.types import ToolAnnotations

from . import db, search


class Hit(TypedDict):
    """検索でヒットした 1 メッセージ。"""
    id: int
    source: str
    role: str
    ts: Optional[str]
    model: Optional[str]
    cwd: Optional[str]
    session_title: Optional[str]
    session_id: Optional[str]   # get_session にそのまま渡せる ext_id
    excerpt: str


class SearchResult(TypedDict):
    mode: str           # 'fts'（全文検索）か 'like'（部分一致）
    count: int
    hits: List[Hit]
    note: str           # 補足。なければ空文字（NotRequired だと SDK が default=None を入れ検証に落ちる）


class SessionInfo(TypedDict):
    id: str
    source: str
    title: Optional[str]
    cwd: Optional[str]
    git_branch: Optional[str]
    host: Optional[str]      # どのマシンで交わした会話か
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

# ツール一覧は静的なので長めに、共有中間者もキャッシュ可 ('public')。
CACHE_HINTS = {
    'tools/list': CacheHint(ttl_ms=3_600_000, scope='public'),
    'server/discover': CacheHint(ttl_ms=3_600_000, scope='public'),
}

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=False)

# 1 回の応答でモデルのコンテキストを埋め尽くさないための上限。
MAX_LIMIT = 100
MAX_BODY_CHARS = 40_000

server = MCPServer(
    name='chist',
    title='CLI チャット履歴検索',
    version='0.1.0',
    instructions=(
        'Claude Code / Codex / Cursor CLI / Antigravity CLI の過去のチャット履歴を検索する。'
        '「前にこの件をどう解決したか」「このプロジェクトで何を話したか」を調べるときに使う。'
        'まず search_history で当たりをつけ、必要なら get_session で本文を読む。'
        '結果は既定でシークレットをマスクしてある。'
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
        '過去のチャット履歴を全文検索し、該当箇所の抜粋を返す。'
        'query は 3 文字以上だと全文検索インデックスを使い、2 文字以下は自動的に部分一致検索へ切り替わる。'
        'cwd にプロジェクトのパス（一部でよい）を渡すと、そのプロジェクトの会話に絞れる。'
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
        query: 検索語。
        source: 絞り込むソース。claude-code / codex / cursor-cli / antigravity-cli。
        cwd: プロジェクトパスの部分一致。
        role: user / assistant / system / tool。
        since: この日時以降 (ISO8601、例 2026-07-01)。
        until: この日時以前。
        host: 絞り込むマシン名。どのマシンで交わした会話かで絞れる。
        limit: 最大件数（既定 20、上限 100）。
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
        notes.append('query が 3 文字未満のため全文検索ではなく部分一致で検索した。')
    if not hits:
        notes.append('該当なし。語を短くするか cwd の絞り込みを外して再試行。')
    return {'mode': mode, 'count': len(hits), 'hits': hits, 'note': ' '.join(notes)}


@server.tool(
    name='list_sessions',
    description='会話セッションの一覧を新しい順に返す。プロジェクトや期間で絞れる。',
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
        cwd: プロジェクトパスの部分一致。
        source: 絞り込むソース。
        since: この日時以降 (ISO8601)。
        host: 絞り込むマシン名。
        limit: 最大件数（既定 20、上限 100）。
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
        '1 つのセッションの本文を返す。search_history や list_sessions が返した id を渡す。'
        '長い会話は途中で打ち切られるため、続きは offset を進めて取得する。'
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
        session_id: セッションの ext_id。
        source: 同じ id が複数ソースにある場合に絞り込む。search_history が返す source をそのまま渡せる。
        offset: 何件目から返すか。
        limit: 最大件数（既定 50、上限 100）。
        thinking: True なら思考ブロックも含める。
    """
    kinds = ['message'] + (['thinking'] if thinking else [])
    sess, msgs = search.show(_db(), session_id, masked=True, kinds=kinds, source=source)
    if not sess:
        # 例外は SDK が is_error=True の結果へ変換する。返り値の型を分岐させない。
        raise ValueError('セッションが見つかりません: %s' % session_id)

    offset = max(0, int(offset or 0))
    limit = _clip(limit, 50)
    window = msgs[offset:offset + limit]

    out = []
    used = 0
    truncated = False
    for m in window:
        text = m['text']
        if used + len(text) > MAX_BODY_CHARS:
            text = text[:max(0, MAX_BODY_CHARS - used)] + '…（省略）'
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
    p = argparse.ArgumentParser(prog='chist-mcp', description='chist の MCP サーバ')
    p.add_argument('--transport', default='stdio',
                   choices=['stdio', 'streamable-http'],
                   help='既定 stdio。Docker で動かすときは streamable-http')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8848)
    p.add_argument('--db', help='DB パス（既定は CHIST_DB か ~/.local/share/chist/history.db）')
    args = p.parse_args(argv)

    if args.db:
        os.environ['CHIST_DB'] = args.db

    path = db.db_path(os.environ.get('CHIST_DB'))
    if not os.path.exists(path):
        sys.stderr.write('DB がありません: %s\n先に `python3 -m chist ingest` を実行してください。\n' % path)
        return 1

    if args.transport == 'stdio':
        server.run(transport='stdio')
    else:
        # 2026-07-28 でプロトコルレベルのセッションが廃止されたため stateless で動かす。
        # Docker 越しにロードバランサへ載せてもリクエストの振り分けが自由になる。
        server.run(transport='streamable-http', host=args.host, port=args.port,
                   stateless_http=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
