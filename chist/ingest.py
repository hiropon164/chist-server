"""Incremental ingest driver.

Files whose mtime and size are unchanged are skipped without opening them. If either
changed, the hash of the first 4 KB is compared: on a match only the appended part is
read from the previous offset; on a mismatch the file is treated as rewritten and
re-ingested in full.
"""
import datetime
import os
import time

from .adapters import ADAPTERS
from .adapters.base import head_sha


def _now():
    # utcnow() is deprecated in 3.12. Take a tz-aware value but keep the Z suffix.
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _upsert_session(conn, source, s):
    conn.execute(
        'INSERT INTO sessions (source, ext_id, title, cwd, git_branch, started_at, ended_at) '
        'VALUES (?,?,?,?,?,?,?) ON CONFLICT(source, ext_id) DO UPDATE SET '
        '  title      = COALESCE(excluded.title, sessions.title),'
        '  cwd        = COALESCE(excluded.cwd, sessions.cwd),'
        '  git_branch = COALESCE(excluded.git_branch, sessions.git_branch),'
        '  started_at = MIN(COALESCE(excluded.started_at, sessions.started_at),'
        '                   COALESCE(sessions.started_at, excluded.started_at)),'
        '  ended_at   = MAX(COALESCE(excluded.ended_at, sessions.ended_at),'
        '                   COALESCE(sessions.ended_at, excluded.ended_at))',
        (source, s.ext_id, s.title, s.cwd, s.git_branch, s.started_at, s.ended_at))
    row = conn.execute('SELECT id, cwd FROM sessions WHERE source=? AND ext_id=?',
                       (source, s.ext_id)).fetchone()
    return row['id'], row['cwd']


def ingest_source(conn, source, progress=None, limit=None):
    """Ingest one source. Returns a stats dict."""
    mod = ADAPTERS[source]
    stat = {'source': source, 'files': 0, 'scanned': 0, 'skipped': 0,
            'messages': 0, 'sessions': 0, 'errors': 0, 'reingested': 0}

    paths = mod.discover()
    if limit:
        paths = paths[:limit]
    stat['files'] = len(paths)

    for i, path in enumerate(paths):
        try:
            st = os.stat(path)
        except OSError:
            continue

        prev = conn.execute(
            'SELECT cursor, size, mtime, head_sha FROM ingest_state WHERE source=? AND path=?',
            (source, path)).fetchone()

        if prev and prev['size'] == st.st_size and prev['mtime'] == st.st_mtime:
            stat['skipped'] += 1
            continue

        # Adapters reading SQLite skip the hash check: the leading bytes change on every
        # write (the change counter). Their cursor is a row id, so resuming is always safe.
        use_sha = getattr(mod, 'USE_HEAD_SHA', True)

        start = 0
        if prev:
            try:
                if use_sha and prev['head_sha'] and head_sha(path) != prev['head_sha']:
                    # The head changed, so this was not an append. Re-read everything.
                    start = 0
                    stat['reingested'] += 1
                else:
                    start = prev['cursor']
            except OSError:
                continue

        try:
            sessions, msgs, next_off, errors = mod.parse(path, start)
        except Exception as e:  # one bad file must not stop the whole run
            conn.execute('INSERT INTO ingest_errors (source, path, pos, reason, seen_at) '
                         'VALUES (?,?,?,?,?)', (source, path, start, repr(e), _now()))
            stat['errors'] += 1
            continue

        stat['scanned'] += 1
        sid_map = {}
        cwd_map = {}
        for s in sessions:
            sid, cwd = _upsert_session(conn, source, s)
            sid_map[s.ext_id] = sid
            cwd_map[s.ext_id] = cwd
            stat['sessions'] += 1

        rows = []
        for m in msgs:
            key = m.session_ext_id
            if key not in sid_map:
                sid, cwd = _upsert_session(conn, source, _bare(key))
                sid_map[key] = sid
                cwd_map[key] = cwd
            rows.append((sid_map[key], source, cwd_map.get(key), m.ext_id, m.part, m.seq,
                         m.role, m.kind, m.ts, m.model, m.provider, m.text, path, m.src_pos))

        if rows:
            cur = conn.executemany(
                'INSERT OR IGNORE INTO messages '
                '(session_id, source, cwd, ext_id, part, seq, role, kind, ts, model, provider,'
                ' text, src_path, src_pos) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
            stat['messages'] += max(cur.rowcount, 0)

        for e in errors:
            conn.execute('INSERT INTO ingest_errors (source, path, pos, reason, seen_at) '
                         'VALUES (?,?,?,?,?)', (source, path, None, e, _now()))
        stat['errors'] += len(errors)

        try:
            hs = head_sha(path) if use_sha else None
        except OSError:
            hs = None
        conn.execute(
            'INSERT INTO ingest_state (source, path, cursor, size, mtime, head_sha,'
            ' ok_count, err_count, updated_at) VALUES (?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(source, path) DO UPDATE SET cursor=excluded.cursor, size=excluded.size,'
            ' mtime=excluded.mtime, head_sha=excluded.head_sha,'
            ' ok_count=ingest_state.ok_count+excluded.ok_count,'
            ' err_count=ingest_state.err_count+excluded.err_count, updated_at=excluded.updated_at',
            (source, path, next_off, st.st_size, st.st_mtime, hs, len(msgs), len(errors), _now()))
        conn.commit()

        if progress and (i % 50 == 0 or i == len(paths) - 1):
            progress(source, i + 1, len(paths), stat)

    conn.execute('UPDATE sessions SET msg_count = ('
                 '  SELECT COUNT(*) FROM messages WHERE messages.session_id = sessions.id'
                 ') WHERE source = ?', (source,))
    conn.commit()
    return stat


def _bare(ext_id):
    from .adapters.base import Sess
    return Sess(ext_id=ext_id)


def ingest_all(conn, sources=None, progress=None, limit=None):
    stats = []
    for source in (sources or list(ADAPTERS)):
        t0 = time.time()
        s = ingest_source(conn, source, progress=progress, limit=limit)
        s['seconds'] = round(time.time() - t0, 1)
        stats.append(s)
    return stats
