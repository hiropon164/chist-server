"""検索。

trigram は 3 文字未満のクエリを MATCH できないため、
2 文字以下は LIKE に自動フォールバックする。
"""
from .mask import mask

MIN_TRIGRAM = 3


def _fts_query(q):
    """ユーザー入力を FTS5 の文字列リテラルへ包む（構文エラーを避ける）。"""
    terms = [t for t in q.split() if t]
    return ' '.join('"%s"' % t.replace('"', '""') for t in terms)


def uses_fts(query):
    return all(len(t) >= MIN_TRIGRAM for t in query.split() if t)


def search(conn, query, source=None, cwd=None, role=None, since=None, until=None,
           limit=20, masked=True, host=None):
    """snippet 付きの検索結果を返す。"""
    where = []
    args = []
    if source:
        where.append('m.source IN (%s)' % ','.join('?' * len(source)))
        args += list(source)
    if host:
        # host は messages ではなく sessions 側が持つ（どちらの分岐でも JOIN 済み）
        where.append('s.host IN (%s)' % ','.join('?' * len(host)))
        args += list(host)
    if cwd:
        where.append('m.cwd LIKE ?')
        args.append('%' + cwd + '%')
    if role:
        where.append('m.role = ?')
        args.append(role)
    if since:
        where.append('m.ts >= ?')
        args.append(since)
    if until:
        where.append('m.ts <= ?')
        args.append(until)

    if uses_fts(query):
        sql = ('SELECT m.id, m.source, m.role, m.kind, m.ts, m.model, m.cwd, s.title,'
               '       s.ext_id AS session_id,'
               "       snippet(messages_fts, 0, '\x02', '\x03', '…', 14) AS hit,"
               '       bm25(messages_fts) AS score '
               'FROM messages_fts f '
               'JOIN messages m ON m.id = f.rowid '
               'JOIN sessions s ON s.id = m.session_id '
               'WHERE messages_fts MATCH ?')
        params = [_fts_query(query)] + args
        if where:
            sql += ' AND ' + ' AND '.join(where)
        sql += ' ORDER BY score LIMIT ?'
        params.append(limit)
        mode = 'fts'
    else:
        # 2 文字以下: trigram では引けないので LIKE で拾う
        sql = ('SELECT m.id, m.source, m.role, m.kind, m.ts, m.model, m.cwd, s.title,'
               '       s.ext_id AS session_id,'
               '       m.text AS hit, 0 AS score '
               'FROM messages m JOIN sessions s ON s.id = m.session_id '
               "WHERE m.kind IN ('message','thinking','artifact') AND m.text LIKE ?")
        params = ['%' + query + '%'] + args
        if where:
            sql += ' AND ' + ' AND '.join(where)
        sql += ' ORDER BY m.ts DESC LIMIT ?'
        params.append(limit)
        mode = 'like'

    rows = []
    for r in conn.execute(sql, params):
        d = dict(r)
        if mode == 'like':
            d['hit'] = _excerpt(d['hit'], query)
        d['hit'] = mask(d['hit'], masked)
        d['title'] = mask(d['title'], masked)
        rows.append(d)
    return rows, mode


def _excerpt(text, query, span=60):
    i = text.lower().find(query.lower())
    if i < 0:
        return text[:span * 2]
    a = max(0, i - span)
    b = min(len(text), i + len(query) + span)
    return ('…' if a else '') + text[a:i] + '\x02' + text[i:i + len(query)] + '\x03' + \
           text[i + len(query):b] + ('…' if b < len(text) else '')


def sessions(conn, cwd=None, source=None, since=None, limit=20, host=None, masked=True):
    where, args = [], []
    if cwd:
        where.append('cwd LIKE ?')
        args.append('%' + cwd + '%')
    if source:
        where.append('source IN (%s)' % ','.join('?' * len(source)))
        args += list(source)
    if host:
        where.append('host IN (%s)' % ','.join('?' * len(host)))
        args += list(host)
    if since:
        where.append('COALESCE(ended_at, started_at) >= ?')
        args.append(since)
    sql = 'SELECT * FROM sessions'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY COALESCE(ended_at, started_at) DESC LIMIT ?'
    args.append(limit)
    rows = []
    for r in conn.execute(sql, args):
        d = dict(r)
        d['title'] = mask(d['title'], masked)
        rows.append(d)
    return rows


def show(conn, session_ext_id, masked=True, kinds=None, source=None):
    """セッション 1 件と本文を返す。

    ext_id の一意性は (source, ext_id) でしか保証されない。別々の CLI が同じ
    UUID を使うと ext_id だけでは特定できないため、source を渡せるようにしてある。
    省略時は先頭 1 件（従来動作）。曖昧なときは ambiguous を True で返す。
    """
    if source:
        rows = conn.execute(
            'SELECT * FROM sessions WHERE source = ? AND (ext_id = ? OR id = ?)',
            (source, session_ext_id, session_ext_id)).fetchall()
    else:
        rows = conn.execute('SELECT * FROM sessions WHERE ext_id = ? OR id = ?',
                            (session_ext_id, session_ext_id)).fetchall()
    if not rows:
        return None, []
    row = rows[0]
    kinds = kinds or ['message', 'thinking']
    q = ('SELECT * FROM messages WHERE session_id = ? AND kind IN (%s) '
         'ORDER BY COALESCE(seq, id)' % ','.join('?' * len(kinds)))
    msgs = []
    for m in conn.execute(q, [row['id']] + kinds):
        d = dict(m)
        d['text'] = mask(d['text'], masked)
        msgs.append(d)
    out = dict(row)
    out['ambiguous'] = len(rows) > 1
    return out, msgs
