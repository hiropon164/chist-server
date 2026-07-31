"""管理サーバ: 取り込み API と MCP を 1 プロセスで提供する。

Docker サーバ側で動かす。各マシンのエージェントが `chist push` で正規化済みの
データを POST し、各ツールは同じポートの `/mcp` を MCP で叩く。

  POST /ingest   エージェントからの取り込み（bearer token 必須）
  GET  /health   死活監視（認証なし）
  GET  /stats    集計（bearer token 必須）
  GET  /auth     Traefik forwardAuth 用の検証だけを行う（bearer token 必須）
  POST /mcp      MCP 2026-07-28 (streamable HTTP)
  GET  /         Web UI（Svelte SPA を単一 HTML にしたもの）
  GET  /api/*    Web UI 用の読み取り API

MCP のツール定義は chist.mcp_server と共有する。

**このアプリを単体で公開してはいけない。**
`/mcp` は MCP SDK がルーティングを持つためこのモジュールの `_authorized` を通らない。
`/` と `/api/*` はブラウザからの利用が前提で bearer token を要求できない。
いずれもリバースプロキシ側で守る設計になっている（docker-compose.yml のラベル参照）。

  /health              認証なし（ヘルスチェック）
  /ingest /stats /mcp  forwardAuth → /auth で bearer token 検証
  / と /api/*          basicauth
  すべて               ipwhitelist で LAN 限定
"""
import argparse
import hmac
import json
import os
import pathlib
import sys

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from . import db, search
from .mask import mask
from .mcp_server import server

TOKEN = None
_conn = None

# 1 リクエストの上限。既定バッチ 500 件なら十分な余裕がある。
MAX_BODY = 64 * 1024 * 1024


def _db():
    global _conn
    if _conn is None:
        _conn = db.connect(os.environ.get('CHIST_DB'))
    return _conn


def _authorized(request: Request) -> bool:
    # トークン未設定なら通す、という逃げ道は作らない（main で起動を止めている）。
    if not TOKEN:
        return False
    auth = request.headers.get('authorization', '')
    if not auth.startswith('Bearer '):
        return False
    # 比較時間からトークンを推測されないようにする
    return hmac.compare_digest(auth[7:], TOKEN)


def _items(payload, key):
    """payload の配列フィールドを取り出す。壊れた形でも例外にしない。"""
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
    """Traefik forwardAuth 用。ヘッダだけ見て 200 か 401 を返す。

    MCP SDK が生やす `/mcp` はこのモジュールのルートではないため、
    プロキシ側からここを叩いてもらうことで全体を保護する。
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

    # セッションを先に確定させる。既存行の非 NULL 値は壊さない。
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
            # title は最初のユーザー発言が使われることがあるので本文と同じく必ずマスクする
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
                     # 送信側も push でマスクしているが、--raw で素通しできてしまう。
                     # 保存する前にここでも必ず通す。
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
# ここから下は basicauth 前提。アプリ内では bearer token を要求しない。

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
    """SPA を返す。ビルド成果物は単一 HTML なのでこの 1 ルートで足りる。"""
    global _index_cache
    if _index_cache is None:
        if not WEB_INDEX.exists():
            return JSONResponse({'error': 'Web UI が同梱されていません'}, status_code=404)
        _index_cache = WEB_INDEX.read_text(encoding='utf-8')
    return HTMLResponse(_index_cache)


@server.custom_route('/api/search', methods=['GET'])
async def api_search(request: Request):
    q = _str(request, 'q', '')
    if not q:
        return JSONResponse({'mode': '', 'count': 0, 'hits': [],
                             'note': '検索語を入力してください。'})
    sources = [s for s in request.query_params.getlist('source') if s]
    hosts = [h for h in request.query_params.getlist('host') if h]
    rows, mode = search.search(
        _db(), q, source=sources or None, cwd=_str(request, 'cwd'),
        role=_str(request, 'role'), since=_str(request, 'since'),
        until=_str(request, 'until'), limit=_int(request, 'limit', 30),
        masked=True, host=hosts or None)
    hits = [{'id': r['id'], 'source': r['source'], 'role': r['role'], 'ts': r['ts'],
             'model': r['model'], 'cwd': r['cwd'], 'session_title': r['title'],
             'session_id': r['session_id'], 'excerpt': r['hit']} for r in rows]
    note = ''
    if mode == 'like':
        note = '検索語が 3 文字未満のため、全文検索ではなく部分一致で検索しました。'
    elif not hits:
        note = '該当なし。語を短くするか絞り込みを外して再試行してください。'
    return JSONResponse({'mode': mode, 'count': len(hits), 'hits': hits, 'note': note})


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
    """?id=<ext_id> で本文を返す。パスパラメータは使わず query で受ける。"""
    ext_id = _str(request, 'id')
    if not ext_id:
        return JSONResponse({'error': 'id が必要です'}, status_code=400)
    kinds = ['message'] + (['thinking'] if _str(request, 'thinking') else [])
    # ext_id は (source, ext_id) でしか一意にならない。source が来ていれば使う。
    sess, msgs = search.show(_db(), ext_id, masked=True, kinds=kinds,
                             source=_str(request, 'source'))
    if not sess:
        return JSONResponse({'error': 'セッションが見つかりません'}, status_code=404)
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
        # 同じ ext_id が別ソースにもある状態。source を付けて呼び直すと確定する。
        'ambiguous': bool(sess.get('ambiguous')),
        'messages': [{'role': m['role'], 'kind': m['kind'], 'ts': m['ts'],
                      'model': m['model'], 'text': m['text']} for m in window],
    })


@server.custom_route('/api/stats', methods=['GET'])
async def api_stats(request: Request):
    return JSONResponse(_stats_payload(_db(), projects=_int(request, 'projects', 20)))


def main(argv=None):
    global TOKEN
    p = argparse.ArgumentParser(prog='chist-server', description='chist 管理サーバ')
    p.add_argument('--host', default='0.0.0.0')
    p.add_argument('--port', type=int, default=8848)
    p.add_argument('--db', help='既定は CHIST_DB か ~/.local/share/chist/history.db')
    p.add_argument('--token', help='既定は環境変数 CHIST_TOKEN')
    args = p.parse_args(argv)

    if args.db:
        os.environ['CHIST_DB'] = args.db
    TOKEN = args.token or os.environ.get('CHIST_TOKEN')
    if not TOKEN:
        sys.stderr.write(
            'CHIST_TOKEN が未設定です。履歴を無認証で公開しないため起動を中止します。\n'
            '  openssl rand -hex 32 で生成して .env に設定してください。\n')
        return 2

    _db()  # 起動時にスキーマ作成・移行を済ませておく
    sys.stderr.write('chist-server http://%s:%d  (MCP: /mcp, ingest: /ingest)\n'
                     % (args.host, args.port))
    # 2026-07-28 でセッションが廃止されたため stateless で動かす
    server.run(transport='streamable-http', host=args.host, port=args.port,
               stateless_http=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
