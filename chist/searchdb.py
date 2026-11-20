"""The search database: a derived copy of the history, shaped for searching.

`history.db` stays the system of record. Nothing here changes it: this module
opens it read-only, attached to a second file (`search.db`) that holds only what
a search needs --

  docs       the indexed kinds of message, with the columns filters use
  docs_tri   trigram full-text index over docs.text (3+ character terms)
  docs_bi    bigram index (exactly-2-character terms, which trigram cannot match)
  meta       how far the copy has got

Everything in search.db can be thrown away and rebuilt from history.db with
`rebuild()`. That is the point of the split: the index design can change, or the
file can be lost, without touching the record -- and the record no longer has to
carry an index several times the size of its text.

Why not triggers, as history.db's own index uses: a trigger cannot write to a
table in another database file. So the copy is advanced explicitly by `sync()`,
from a watermark, after each write to the record. The watermark and the rows it
covers are committed in one search.db transaction, so a crash between the two
files leaves the copy behind, never wrong: the next sync picks up where it was.

The text is masked on the way in, with the rules in force when the row is
copied (chist.mask). The record's own masking is frozen at the day each row was
stored: the server masks before storing, but a rule added later never reaches
the rows already there, and a machine's local record is not masked at all. The
copy is the one place that can follow the rules -- change a rule, `rebuild()`,
and every row is masked by it. So secrets the rules know about are not in
search.db, and a search for the secret itself finds nothing. `check()` compares
against the masked record, so a rule change shows up there as rows that differ.

Session attributes (title, ext_id, host) are NOT copied. They change after the
fact -- a title arrives late, a host is filled in by backfill -- and a copy would
go stale silently. Queries read them through the attached record instead.

Phase 1: this runs beside the old index. Nothing uses it for answers unless
CHIST_SEARCH_ENGINE=split.
"""
import os
import pathlib
import sqlite3
import threading

from .mask import fingerprint as mask_fingerprint, mask

SCHEMA_VERSION = 1
INDEXED_KINDS = ('message', 'thinking', 'artifact')
MIN_TRIGRAM = 3
MIN_BIGRAM = 2

_KINDS_SQL = ','.join("'%s'" % k for k in INDEXED_KINDS)

_SCHEMA = '''
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID;

CREATE TABLE docs (
  id         INTEGER PRIMARY KEY,     -- messages.id in the record
  session_id INTEGER NOT NULL,
  source     TEXT NOT NULL,
  cwd        TEXT,
  role       TEXT,
  kind       TEXT NOT NULL,
  ts         TEXT,
  model      TEXT,
  text       TEXT NOT NULL
);
CREATE INDEX ix_docs_session ON docs(session_id);
CREATE INDEX ix_docs_ts      ON docs(ts DESC);

CREATE VIRTUAL TABLE docs_tri USING fts5(
  text, content='docs', content_rowid='id', tokenize='trigram');

-- Contentless and without positions: it only has to answer "which rows contain
-- this pair of characters". The excerpt comes from docs.text.
CREATE VIRTUAL TABLE docs_bi USING fts5(
  grams, content='', detail=none, columnsize=0);
'''


def enabled():
    """Whether this process should keep a search database up to date."""
    return bool(os.environ.get('CHIST_SEARCH_DB')) or engine() == 'split'


def engine(override=None):
    """Which index answers searches: 'legacy' (inside history.db) or 'split'."""
    e = (override or os.environ.get('CHIST_SEARCH_ENGINE') or 'legacy').strip().lower()
    return 'split' if e == 'split' else 'legacy'


def default_path(history_path):
    """search.db lives beside history.db unless CHIST_SEARCH_DB says otherwise."""
    env = os.environ.get('CHIST_SEARCH_DB')
    if env:
        return os.path.expanduser(env)
    return os.path.join(os.path.dirname(os.path.abspath(history_path)), 'search.db')


def history_path_of(conn):
    """The file behind a history connection."""
    for row in conn.execute('PRAGMA database_list'):
        if row[1] == 'main':
            return row[2]
    return None


def connect(history_path, path=None, timeout=30):
    """Open search.db with the record attached read-only as `hist`.

    `timeout` is how long a write waits for another writer, in seconds.
    """
    history_path = os.path.abspath(os.path.expanduser(history_path))
    path = os.path.abspath(os.path.expanduser(path or default_path(history_path)))
    if path == history_path:
        raise ValueError('search database and history database are the same file')
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    fresh = not os.path.exists(path)

    # The server and a CLI run in the same container may both write.
    conn = sqlite3.connect(pathlib.Path(path).as_uri(), uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    if fresh:
        try:
            conn.executescript(_SCHEMA)
            conn.execute('PRAGMA user_version = %d' % SCHEMA_VERSION)
            conn.commit()
        except Exception:
            conn.close()
            os.unlink(path)           # never leave a half-built file to be found
            raise
        # It holds the same text as the record, so the same permissions.
        os.chmod(path, 0o600)
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA synchronous = NORMAL')
    # mode=ro: this connection cannot change the record, whatever is run on it.
    conn.execute('ATTACH DATABASE ? AS hist',
                 (pathlib.Path(history_path).as_uri() + '?mode=ro',))
    # So check() can ask, in SQL, what a row of the record should look like here.
    conn.create_function('chist_mask', 1, mask, deterministic=True)
    return conn


_cache = {}


def for_history(conn):
    """The search connection that goes with a history connection (one per file)."""
    hp = history_path_of(conn)
    if not hp:
        raise ValueError('an in-memory history database has no search database')
    # Per thread as well: a sqlite3 connection must stay on the thread that
    # made it, and the server's requests and the MCP tools do not promise to
    # share one.
    key = (hp, default_path(hp), threading.get_ident())
    if key not in _cache:
        # A short wait: this is the connection the server syncs on from inside a
        # request. If a rebuild holds the write lock for minutes, give up quickly
        # (after_write logs it and the next sync catches up) rather than stall.
        _cache[key] = connect(hp, timeout=2)
    return _cache[key]


def close_all():
    for c in _cache.values():
        c.close()
    _cache.clear()


# -- bigrams ---------------------------------------------------------------

def _gram(a, b):
    # Hex code points joined by a letter that is not a hex digit: one plain
    # alphanumeric token per pair, so the default tokenizer never splits it,
    # whatever the two characters are.
    return '%xz%x' % (ord(a), ord(b))


def grams(text):
    """The distinct character pairs of `text`, as index tokens.

    Lower-cased, and pairs containing whitespace are left out: a search term
    cannot contain whitespace, since terms are split on it.
    """
    t = text.lower()
    out = set()
    for i in range(len(t) - 1):
        a, b = t[i], t[i + 1]
        if a.isspace() or b.isspace():
            continue
        out.add(_gram(a, b))
    return ' '.join(sorted(out))


def _bi_query(terms):
    return ' '.join('"%s"' % _gram(*t.lower()) for t in terms)


def _tri_query(terms):
    """Wrap user input as FTS5 string literals so it cannot cause a syntax error."""
    return ' '.join('"%s"' % t.replace('"', '""') for t in terms)


# -- keeping the copy current ------------------------------------------------

def _meta(conn, k, default=None):
    row = conn.execute('SELECT v FROM meta WHERE k = ?', (k,)).fetchone()
    return row[0] if row else default


def _set_meta(conn, k, v):
    conn.execute('INSERT INTO meta (k, v) VALUES (?, ?) '
                 'ON CONFLICT(k) DO UPDATE SET v = excluded.v', (k, str(v)))


def watermark(conn):
    return int(_meta(conn, 'last_id', 0))


def _copy_batch(conn, batch):
    """Copy one batch past the watermark. No transaction handling: the caller
    decides what commits together. Returns (rows added, rows read)."""
    last = watermark(conn)
    rows = conn.execute(
        'SELECT id, session_id, source, cwd, role, kind, ts, model, text '
        'FROM hist.messages WHERE id > ? ORDER BY id LIMIT ?',
        (last, batch)).fetchall()
    added = 0
    for r in rows:
        if r['kind'] not in INDEXED_KINDS:
            continue
        # Masked before it is stored or indexed: neither the copy nor
        # either index ever holds the unmasked form.
        text = mask(r['text'])
        # OR IGNORE: an id at or below the watermark can only reappear
        # after rows were deleted from the record, and reconcile() has
        # already removed the old copy by then.
        cur = conn.execute(
            'INSERT OR IGNORE INTO docs '
            '(id, session_id, source, cwd, role, kind, ts, model, text) '
            'VALUES (?,?,?,?,?,?,?,?,?)', tuple(r)[:8] + (text,))
        if cur.rowcount != 1:
            continue
        conn.execute('INSERT INTO docs_tri (rowid, text) VALUES (?, ?)', (r['id'], text))
        conn.execute('INSERT INTO docs_bi (rowid, grams) VALUES (?, ?)',
                     (r['id'], grams(text)))
        added += 1
    if rows:
        _set_meta(conn, 'last_id', rows[-1]['id'])
    return added, len(rows)


def sync(conn, batch=2000):
    """Copy what the record gained since the last call. Returns the number added.

    Each batch is one transaction in search.db: rows, both indexes and the
    watermark move together or not at all.
    """
    added = 0
    while True:
        with conn:
            if _meta(conn, 'mask_rules') is None and \
                    not conn.execute('SELECT 1 FROM docs LIMIT 1').fetchone():
                # An empty copy is about to be filled by the rules in force now.
                _set_meta(conn, 'mask_rules', mask_fingerprint())
            n, read = _copy_batch(conn, batch)
        added += n
        if not read:
            return added


def _forget(conn, doc_id, text):
    conn.execute("INSERT INTO docs_tri (docs_tri, rowid, text) VALUES ('delete', ?, ?)",
                 (doc_id, text))
    conn.execute("INSERT INTO docs_bi (docs_bi, rowid, grams) VALUES ('delete', ?, ?)",
                 (doc_id, grams(text)))
    conn.execute('DELETE FROM docs WHERE id = ?', (doc_id,))


def reconcile(conn):
    """Drop copies of rows the record no longer has. Returns the number dropped.

    Call after anything deletes from the record (prune). It also pulls the
    watermark back to the record's highest id: SQLite reuses the ids above it,
    and a watermark left high would make sync() skip the rows that get them.
    """
    gone = conn.execute(
        'SELECT d.id, d.text FROM docs d '
        'WHERE NOT EXISTS (SELECT 1 FROM hist.messages m '
        '                  WHERE m.id = d.id AND m.kind IN (%s))' % _KINDS_SQL).fetchall()
    top = conn.execute('SELECT COALESCE(MAX(id), 0) FROM hist.messages').fetchone()[0]
    with conn:
        for r in gone:
            _forget(conn, r['id'], r['text'])
        if top < watermark(conn):
            _set_meta(conn, 'last_id', top)
    return len(gone)


def rebuild(conn, batch=2000):
    """Empty the copy and fill it again from the record. Returns the number added.

    **One transaction.** In WAL mode a reader keeps the snapshot it started with,
    so searches go on answering from the old copy for the minutes this takes and
    switch to the new one at the commit -- there is no moment when the copy is
    half full. If it fails or the process dies, the old copy is what remains.

    The price is that nothing else can write to search.db meanwhile. A sync that
    arrives during a rebuild waits, gives up, and is not lost: the rebuild reads
    the record as it is when each batch runs, and the next sync covers the rest.
    """
    added = 0
    with conn:
        conn.execute("INSERT INTO docs_tri (docs_tri) VALUES ('delete-all')")
        conn.execute("INSERT INTO docs_bi (docs_bi) VALUES ('delete-all')")
        conn.execute('DELETE FROM docs')
        _set_meta(conn, 'last_id', 0)
        _set_meta(conn, 'mask_rules', mask_fingerprint())
        while True:
            n, read = _copy_batch(conn, batch)
            added += n
            if not read:
                break
    optimize(conn)
    return added


def stale(conn):
    """True when the copy was built with masking rules other than the current ones.

    A copy with no fingerprint at all (built before this was recorded) counts as
    stale: there is no telling which rules made it.
    """
    return _meta(conn, 'mask_rules') != mask_fingerprint()


def ensure_current(history_path, path=None, log=None):
    """Rebuild the copy if the masking rules changed since it was built.

    Meant to run once at server start, in a thread of its own: it opens its own
    connection (a sqlite3 connection must not cross threads) and never raises.
    Returns 'current', 'rebuilt' or 'failed'.
    """
    try:
        conn = connect(history_path, path)
        try:
            if not stale(conn):
                return 'current'
            if log:
                log('search database: masking rules changed since it was built; rebuilding')
            n = rebuild(conn)
            if log:
                log('search database: rebuilt, %d rows' % n)
            return 'rebuilt'
        finally:
            conn.close()
    except Exception as e:                     # noqa: BLE001 -- must not stop the server
        if log:
            log('search database: rebuild failed: %r' % (e,))
        return 'failed'


def optimize(conn):
    """Merge index segments and refresh the planner's statistics."""
    with conn:
        conn.execute("INSERT INTO docs_tri (docs_tri) VALUES ('optimize')")
        conn.execute("INSERT INTO docs_bi (docs_bi) VALUES ('optimize')")
    conn.execute('ANALYZE main')
    conn.commit()


def check(conn):
    """Compare the copy with the record. `ok` is True only when they agree.

    "Agree" means the copy holds the record's text as the current masking rules
    render it. After a rule change the old rows differ until `rebuild()`.
    """
    want = conn.execute('SELECT COUNT(*) FROM hist.messages WHERE kind IN (%s)'
                        % _KINDS_SQL).fetchone()[0]
    have = conn.execute('SELECT COUNT(*) FROM docs').fetchone()[0]
    missing = conn.execute(
        'SELECT COUNT(*) FROM hist.messages m WHERE m.kind IN (%s) '
        'AND NOT EXISTS (SELECT 1 FROM docs d WHERE d.id = m.id)' % _KINDS_SQL).fetchone()[0]
    extra = conn.execute(
        'SELECT COUNT(*) FROM docs d WHERE NOT EXISTS ('
        '  SELECT 1 FROM hist.messages m WHERE m.id = d.id AND m.kind IN (%s))'
        % _KINDS_SQL).fetchone()[0]
    differ = conn.execute(
        'SELECT COUNT(*) FROM docs d JOIN hist.messages m ON m.id = d.id '
        'WHERE chist_mask(m.text) IS NOT d.text').fetchone()[0]
    top = conn.execute('SELECT COALESCE(MAX(id), 0) FROM hist.messages').fetchone()[0]
    # Rows whose stored form in the record still carries something the current
    # rules would mask. Not a fault of the copy -- reported so it is not invisible.
    unmasked = conn.execute(
        'SELECT COUNT(*) FROM hist.messages m WHERE m.kind IN (%s) '
        'AND chist_mask(m.text) IS NOT m.text' % _KINDS_SQL).fetchone()[0]
    return {'record': want, 'copy': have, 'missing': missing, 'extra': extra,
            'record_unmasked': unmasked, 'mask_rules_current': not stale(conn),
            'text_differs': differ, 'watermark': watermark(conn), 'record_max_id': top,
            # Not `and not stale`: text_differs is the test of what the rules do
            # to these rows; the fingerprint is only the cheap early warning.
            'ok': want == have and not missing and not extra and not differ}


def after_write(history_conn, deleted=False, log=None):
    """Bring the copy up to date after the record changed. Never raises.

    The record is already committed when this runs, and it is the record that
    matters: a failure here must not turn a successful ingest into an error. The
    copy is then merely behind, which the next call -- or `chist reindex` --
    repairs. Returns the number of rows added, or None when it is switched off
    or failed.
    """
    if not enabled():
        return None
    try:
        sconn = for_history(history_conn)
        if deleted:
            reconcile(sconn)
        return sync(sconn)
    except Exception as e:                     # noqa: BLE001 -- see docstring
        if log:
            log('search database not updated: %r' % (e,))
        return None


# -- searching ---------------------------------------------------------------

def plan(query):
    """Decide how to run this query.

    Returns (mode, long, two, dropped):
      long     terms of 3+ characters -> trigram index
      two      terms of exactly 2     -> bigram index
      dropped  1-character terms left out because something else carries the query
    mode is 'fts' (at least one long term), 'bigram' (only 2-character terms),
    or 'like' (only 1-character terms: the longest is matched as a substring).
    """
    terms = [t for t in query.split() if t]
    long = [t for t in terms if len(t) >= MIN_TRIGRAM]
    two = [t for t in terms if len(t) == MIN_BIGRAM]
    one = [t for t in terms if len(t) < MIN_BIGRAM]
    if long:
        return 'fts', long, two, one
    if two:
        return 'bigram', [], two, one
    return 'like', [], [], []


def _excerpt(text, query, span=60):
    i = text.lower().find(query.lower())
    if i < 0:
        return text[:span * 2]
    a = max(0, i - span)
    b = min(len(text), i + len(query) + span)
    return ('…' if a else '') + text[a:i] + '\x02' + text[i:i + len(query)] + '\x03' + \
           text[i + len(query):b] + ('…' if b < len(text) else '')


_COLS = ('d.id, d.source, d.role, d.kind, d.ts, d.model, d.cwd, s.title,'
         ' s.ext_id AS session_id')


def search(conn, query, source=None, cwd=None, role=None, since=None, until=None,
           limit=20, masked=True, host=None):
    """Same contract as chist.search.search, answered from search.db."""
    where, args = [], []
    if source:
        where.append('d.source IN (%s)' % ','.join('?' * len(source)))
        args += list(source)
    if host:
        where.append('s.host IN (%s)' % ','.join('?' * len(host)))
        args += list(host)
    if cwd:
        where.append('d.cwd LIKE ?')
        args.append('%' + cwd + '%')
    if role:
        where.append('d.role = ?')
        args.append(role)
    if since:
        where.append('d.ts >= ?')
        args.append(since)
    if until:
        where.append('d.ts <= ?')
        args.append(until)

    mode, long, two, dropped = plan(query)
    bi = 'd.id IN (SELECT rowid FROM docs_bi WHERE docs_bi MATCH ?)'
    if mode == 'fts':
        sql = ('SELECT ' + _COLS + ','
               "       snippet(docs_tri, 0, '\x02', '\x03', '…', 14) AS hit,"
               '       bm25(docs_tri) AS score '
               'FROM docs_tri f '
               'JOIN docs d ON d.id = f.rowid '
               'JOIN hist.sessions s ON s.id = d.session_id '
               'WHERE docs_tri MATCH ?')
        params = [_tri_query(long)]
        if two:
            sql += ' AND ' + bi
            params.append(_bi_query(two))
        order = ' ORDER BY score LIMIT ?'
        needle = None
    elif mode == 'bigram':
        sql = ('SELECT ' + _COLS + ', d.text AS hit, 0 AS score '
               'FROM docs d JOIN hist.sessions s ON s.id = d.session_id '
               'WHERE ' + bi)
        params = [_bi_query(two)]
        order = ' ORDER BY d.ts DESC LIMIT ?'
        needle = two[0]
    else:
        terms = [t for t in query.split() if t]
        needle = max(terms, key=len) if terms else query
        sql = ('SELECT ' + _COLS + ', d.text AS hit, 0 AS score '
               'FROM docs d JOIN hist.sessions s ON s.id = d.session_id '
               'WHERE d.text LIKE ?')
        params = ['%' + needle + '%']
        order = ' ORDER BY d.ts DESC LIMIT ?'
    params += args
    if where:
        sql += ' AND ' + ' AND '.join(where)
    sql += order
    params.append(limit)

    rows = []
    for r in conn.execute(sql, params):
        d = dict(r)
        if needle is not None:
            d['hit'] = _excerpt(d['hit'], needle)
        d['hit'] = mask(d['hit'], masked)
        d['title'] = mask(d['title'], masked)
        rows.append(d)
    return rows, mode, dropped
