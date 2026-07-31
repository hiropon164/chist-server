"""Deleting history.

Ingest only appends, so the database grows without bound. Deletion is also the way
to remove something imported by mistake, or a conversation you would rather not keep.

**Nothing is deleted unless at least one condition is given.** Deleting everything is
deliberately not offered, so an argument-less invocation cannot wipe the database
(remove the file instead).

FTS uses the external-content form (`content=`), so the DELETE trigger on `messages`
also drops the index entry. messages_fts is never touched directly here.
"""

# The timestamp used for range checks. Cursor CLI leaves ts NULL, so fall back to the
# session's end time (or start time). Without this, sources without timestamps would
# silently escape any time-range deletion.
_TS = ('COALESCE(m.ts, (SELECT COALESCE(s.ended_at, s.started_at)'
       '                FROM sessions s WHERE s.id = m.session_id))')


def build_filter(before=None, after=None, source=None, session=None, match=None):
    """Build the delete condition as (SQL fragment, args). Returns (None, []) if empty."""
    conds, args = [], []
    if before:
        conds.append('%s < ?' % _TS)
        args.append(before)
    if after:
        conds.append('%s >= ?' % _TS)
        args.append(after)
    if source:
        conds.append('m.source IN (%s)' % ','.join('?' * len(source)))
        args += list(source)
    if session:
        # ext_id is only unique together with source. When --source is also given it
        # ANDs with the clause above, so a same-named session in another source is safe.
        conds.append('m.session_id IN (SELECT id FROM sessions WHERE ext_id = ? OR id = ?)')
        args += [session, session]
    if match:
        conds.append('m.text LIKE ?')
        args.append('%' + match + '%')
    if not conds:
        return None, []
    return ' AND '.join(conds), args


def preview(conn, where, args):
    """Report what would be deleted, broken down by source. Deletes nothing."""
    total = conn.execute(
        'SELECT COUNT(*) FROM messages m WHERE %s' % where, args).fetchone()[0]
    by_source = [dict(r) for r in conn.execute(
        'SELECT m.source, COUNT(*) n, MIN(%s) first, MAX(%s) last '
        'FROM messages m WHERE %s GROUP BY m.source ORDER BY n DESC'
        % (_TS, _TS, where), args)]
    sessions = conn.execute(
        'SELECT COUNT(DISTINCT m.session_id) FROM messages m WHERE %s' % where,
        args).fetchone()[0]
    # Sessions whose every message is in the delete set (i.e. they will be left empty)
    empty = conn.execute(
        'SELECT COUNT(*) FROM sessions s WHERE EXISTS ('
        '  SELECT 1 FROM messages m WHERE m.session_id = s.id AND %s) '
        'AND NOT EXISTS ('
        '  SELECT 1 FROM messages m WHERE m.session_id = s.id AND NOT (%s))'
        % (where, where), args + args).fetchone()[0]
    return {'messages': total, 'sessions': sessions, 'empty_sessions': empty,
            'by_source': by_source}


def prune(conn, where, args, drop_empty_sessions=True):
    """Delete the messages matching the condition. Returns a stats dict."""
    # Note the affected sessions before deleting, so only those emptied by *this*
    # deletion are removed. Scanning for empty sessions afterwards would also sweep up
    # sessions that never had a body to begin with.
    targets = [r[0] for r in conn.execute(
        'SELECT DISTINCT m.session_id FROM messages m WHERE %s' % where, args)]

    deleted = conn.execute(
        'DELETE FROM messages WHERE id IN ('
        '  SELECT m.id FROM messages m WHERE %s)' % where, args).rowcount

    dropped = 0
    if drop_empty_sessions and targets:
        dropped = conn.execute(
            'DELETE FROM sessions WHERE id IN (%s) AND NOT EXISTS ('
            '  SELECT 1 FROM messages m WHERE m.session_id = sessions.id)'
            % ','.join('?' * len(targets)), targets).rowcount

    conn.execute(
        'UPDATE sessions SET msg_count = ('
        '  SELECT COUNT(*) FROM messages WHERE messages.session_id = sessions.id)')
    conn.commit()
    return {'messages': max(deleted, 0), 'sessions': max(dropped, 0)}


def vacuum(conn):
    """Return freed space to the filesystem and optimise the FTS index.

    VACUUM rewrites the database, so it needs free space equal to its current size.
    """
    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('optimize')")
    conn.commit()
    conn.execute('VACUUM')
    conn.commit()
