"""エージェント側: ローカル DB の差分を管理サーバへ送る。

依存ゼロ（urllib）で動く。各マシンに配るのはこちら側なので、
MCP SDK には依存させない。

送るのは検索対象になる kind（message / thinking / artifact）だけ。
tool_call / tool_result は全体の 7 割を占めるうえ FTS 索引の対象外なので、
既定では送らない（--include-tools で変更できる）。
"""
import json
import time
import urllib.error
import urllib.request

from . import project
from .mask import mask

DEFAULT_KINDS = ('message', 'thinking', 'artifact')
BATCH = 500


class PushError(RuntimeError):
    """送信に失敗した。呼び出し側で捕まえて終了コードに変える。"""


def _post(endpoint, path, token, payload, timeout=60, retries=3):
    """1 バッチを送る。一時的な失敗は間隔を空けて数回だけ再試行する。"""
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            endpoint.rstrip('/') + path, data=body, method='POST',
            headers={'Content-Type': 'application/json',
                     'Authorization': 'Bearer ' + (token or '')})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:200]
            # 認証エラーや 4xx は何度投げても同じなので即座に諦める
            if e.code in (401, 403):
                raise PushError('認証に失敗しました (%s)。CHIST_TOKEN を確認してください。'
                                % e.code)
            if 400 <= e.code < 500:
                raise PushError('サーバが受け付けませんでした (%s): %s' % (e.code, detail))
            last = 'HTTP %s: %s' % (e.code, detail)
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = str(e)
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    raise PushError('%s へ送信できませんでした: %s' % (endpoint, last))


def _sessions_for(conn, ids, masked=True):
    """送信するセッション情報。title は最初のユーザー発言から作られることがあり、
    本文と同じだけ秘密が混じりうるので、本文と同様にマスクしてから出す。"""
    if not ids:
        return []
    q = ('SELECT id, source, ext_id, title, cwd, git_branch, project_key,'
         '       started_at, ended_at FROM sessions WHERE id IN (%s)'
         % ','.join('?' * len(ids)))
    out = []
    for r in conn.execute(q, list(ids)):
        out.append({'source': r['source'], 'ext_id': r['ext_id'],
                    'title': mask(r['title'], masked),
                    'cwd': r['cwd'], 'git_branch': r['git_branch'],
                    'project_key': r['project_key'],
                    'started_at': r['started_at'], 'ended_at': r['ended_at']})
    return out


def push(conn, endpoint, token, kinds=DEFAULT_KINDS, masked=True,
         reset=False, progress=None, limit=None):
    """未送信の messages を順に送る。統計 dict を返す。"""
    info = project.host_info()

    row = conn.execute('SELECT last_id FROM push_state WHERE endpoint = ?',
                       (endpoint,)).fetchone()
    last_id = 0 if reset else (row['last_id'] if row else 0)

    stat = {'endpoint': endpoint, 'host': info['host'], 'from_id': last_id,
            'batches': 0, 'sent': 0, 'accepted': 0, 'seconds': 0.0}
    t0 = time.time()

    ph = ','.join('?' * len(kinds))
    while True:
        rows = conn.execute(
            'SELECT id, session_id, source, cwd, ext_id, part, seq, role, kind, ts,'
            '       model, provider, text FROM messages '
            'WHERE id > ? AND kind IN (%s) ORDER BY id LIMIT ?' % ph,
            [last_id] + list(kinds) + [BATCH]).fetchall()
        if not rows:
            break

        sess_ids = {r['session_id'] for r in rows}
        sessions = _sessions_for(conn, sess_ids, masked)
        sess_key = {}
        for r in conn.execute('SELECT id, source, ext_id FROM sessions WHERE id IN (%s)'
                              % ','.join('?' * len(sess_ids)), list(sess_ids)):
            sess_key[r['id']] = (r['source'], r['ext_id'])

        messages = []
        for r in rows:
            key = sess_key.get(r['session_id'])
            if not key:
                continue
            messages.append({
                'source': r['source'], 'ext_id': r['ext_id'], 'part': r['part'],
                'session_ext_id': key[1], 'seq': r['seq'], 'role': r['role'],
                'kind': r['kind'], 'ts': r['ts'], 'model': r['model'],
                'provider': r['provider'], 'cwd': r['cwd'],
                # ネットワークに出す前にマスクする
                'text': mask(r['text'], masked),
            })

        res = _post(endpoint, '/ingest', token,
                    {'host': info, 'sessions': sessions, 'messages': messages})

        last_id = rows[-1]['id']
        stat['batches'] += 1
        stat['sent'] += len(messages)
        stat['accepted'] += int(res.get('accepted', 0))

        conn.execute(
            'INSERT INTO push_state (endpoint, last_id, sent, updated_at) '
            'VALUES (?,?,?,datetime("now")) ON CONFLICT(endpoint) DO UPDATE SET '
            'last_id=excluded.last_id, sent=push_state.sent+excluded.sent,'
            'updated_at=excluded.updated_at', (endpoint, last_id, len(messages)))
        conn.commit()

        if progress:
            progress(stat)
        if limit and stat['sent'] >= limit:
            break

    stat['last_id'] = last_id
    stat['seconds'] = round(time.time() - t0, 1)
    return stat
