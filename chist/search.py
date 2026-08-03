"""Search.

The trigram tokenizer cannot MATCH queries shorter than 3 characters, so anything
shorter falls back to LIKE automatically.
"""
from .mask import mask

MIN_TRIGRAM = 3


def plan(query):
    """Decide how to run this query, and what had to be left out.

    The trigram index cannot match a term shorter than three characters. Sending
    the whole query to a substring search when *any* term is short looks safe and
    is not: `watcher 残骸` then means the literal string "watcher 残骸", which
    appears nowhere, so a query whose terms both match returns nothing. Japanese
    makes this easy to hit -- 残骸, 常駐, 既読, 検証 are all two characters.

    So the short terms are dropped and the rest goes to the index. Only when
    every term is short does it fall back, and then on the longest one alone
    rather than the whole string.

    Returns (mode, terms, dropped).
    """
    terms = [t for t in query.split() if t]
    long = [t for t in terms if len(t) >= MIN_TRIGRAM]
    dropped = [t for t in terms if len(t) < MIN_TRIGRAM]
    if long:
        return 'fts', long, dropped
    return 'like', (max(terms, key=len),) if terms else (), []


def _fts_query(terms):
    """Wrap user input as FTS5 string literals so it cannot cause a syntax error."""
    return ' '.join('"%s"' % t.replace('"', '""') for t in terms)


def uses_fts(query):
    return plan(query)[0] == 'fts'


def search(conn, query, source=None, cwd=None, role=None, since=None, until=None,
           limit=20, masked=True, host=None):
    """Return search results with a snippet around each match."""
    where = []
    args = []
    if source:
        where.append('m.source IN (%s)' % ','.join('?' * len(source)))
        args += list(source)
    if host:
        # host lives on sessions, not messages (both branches already JOIN it)
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

    mode, terms, dropped = plan(query)
    if mode == 'fts':
        sql = ('SELECT m.id, m.source, m.role, m.kind, m.ts, m.model, m.cwd, s.title,'
               '       s.ext_id AS session_id,'
               "       snippet(messages_fts, 0, '\x02', '\x03', '…', 14) AS hit,"
               '       bm25(messages_fts) AS score '
               'FROM messages_fts f '
               'JOIN messages m ON m.id = f.rowid '
               'JOIN sessions s ON s.id = m.session_id '
               'WHERE messages_fts MATCH ?')
        params = [_fts_query(terms)] + args
        if where:
            sql += ' AND ' + ' AND '.join(where)
        sql += ' ORDER BY score LIMIT ?'
        params.append(limit)
    else:
        # Every term is too short for trigram. Match the longest one as a
        # substring rather than the whole query, which as a literal string would
        # almost never appear.
        sql = ('SELECT m.id, m.source, m.role, m.kind, m.ts, m.model, m.cwd, s.title,'
               '       s.ext_id AS session_id,'
               '       m.text AS hit, 0 AS score '
               'FROM messages m JOIN sessions s ON s.id = m.session_id '
               "WHERE m.kind IN ('message','thinking','artifact') AND m.text LIKE ?")
        params = ['%' + (terms[0] if terms else query) + '%'] + args
        if where:
            sql += ' AND ' + ' AND '.join(where)
        sql += ' ORDER BY m.ts DESC LIMIT ?'
        params.append(limit)

    rows = []
    for r in conn.execute(sql, params):
        d = dict(r)
        if mode == 'like':
            d['hit'] = _excerpt(d['hit'], terms[0] if terms else query)
        d['hit'] = mask(d['hit'], masked)
        d['title'] = mask(d['title'], masked)
        rows.append(d)
    return rows, mode, dropped


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
    """Return one session and its messages.

    ext_id is only unique together with source. If two CLIs happen to use the same
    UUID, ext_id alone cannot identify a session, so source can be supplied. Omitting
    it returns the first match and sets ambiguous=True when more than one exists.
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
    out['title'] = mask(out['title'], masked)
    out['ambiguous'] = len(rows) > 1
    return out, msgs
