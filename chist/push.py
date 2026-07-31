"""Agent side: send the local delta to the aggregation server.

Dependency-free (urllib only). This is the half distributed to each machine, so it
must never require the MCP SDK.

Only the searchable kinds are sent (message / thinking / artifact). tool_call and
tool_result make up about 70% of all rows and are not in the FTS index, so they are
omitted by default (--include-tools overrides).
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
    """A push failed. Callers catch this and turn it into an exit code."""


def _post(endpoint, path, token, payload, timeout=60, retries=3):
    """Send one batch, retrying a few times with backoff on transient failures."""
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
            # Auth errors and other 4xx will not change on retry, so give up at once
            if e.code in (401, 403):
                raise PushError('Authentication failed (%s). Check CHIST_TOKEN.' % e.code)
            if 400 <= e.code < 500:
                raise PushError('The server rejected the request (%s): %s' % (e.code, detail))
            last = 'HTTP %s: %s' % (e.code, detail)
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = str(e)
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    raise PushError('Could not send to %s: %s' % (endpoint, last))


def _sessions_for(conn, ids, masked=True):
    """Session rows to send. Some sources derive the title from the first user message,
    so it can carry the same secrets as the body and is masked the same way."""
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
    """Send unsent messages in batches. Returns a stats dict."""
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
                # Mask before it leaves the machine
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
