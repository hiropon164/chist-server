"""chist — unified search over CLI chat history."""
import argparse
import os
import sys

from . import (db, ingest, project, prune as prune_mod, push as push_mod, search,
               searchdb)
from .adapters import ADAPTERS

BOLD, DIM, HL, RESET = '\033[1m', '\033[2m', '\033[43;30m', '\033[0m'


def _color():
    return sys.stdout.isatty() and os.environ.get('NO_COLOR') is None


def _render(text):
    """Turn the \x02/\x03 snippet markers into terminal highlighting."""
    if _color():
        return text.replace('\x02', HL).replace('\x03', RESET)
    return text.replace('\x02', '[').replace('\x03', ']')


def _dim(s):
    return DIM + s + RESET if _color() else s


def _bold(s):
    return BOLD + s + RESET if _color() else s


def cmd_ingest(args):
    conn = db.connect(args.db)

    def progress(source, i, n, st):
        sys.stderr.write('\r  %-16s %5d/%-5d  %6d new' % (source, i, n, st['messages']))
        sys.stderr.flush()

    sources = args.source or list(ADAPTERS)
    stats = ingest.ingest_all(conn, sources, progress=progress, limit=args.limit)
    sys.stderr.write('\r' + ' ' * 60 + '\r')
    searchdb.after_write(conn, log=lambda m: sys.stderr.write(m + '\n'))

    print('%-16s %7s %7s %7s %8s %8s %6s' %
          ('source', 'files', 'read', 'skip', 'msgs', 'sessions', 'sec'))
    for s in stats:
        print('%-16s %7d %7d %7d %8d %8d %6.1f' %
              (s['source'], s['files'], s['scanned'], s['skipped'],
               s['messages'], s['sessions'], s['seconds']))
        if s['errors']:
            print('  %s' % _dim('%d parse failures (see: chist errors)' % s['errors']))
        if s['reingested']:
            print('  %s' % _dim('%d files rewritten, re-ingested in full' % s['reingested']))
    print()
    print('DB: %s' % db.db_path(args.db))


def cmd_search(args):
    conn = db.connect(args.db, readonly=True)
    target, engine, fell_back = search.resolve_target(args.target)
    rows, mode, dropped = search.search(
        conn, args.query, source=args.source, cwd=args.cwd, role=args.role,
        since=args.since, until=args.until, limit=args.limit, masked=not args.raw,
        host=args.host, engine=engine)

    if fell_back:
        print(_dim('Note: ' + fell_back))
    if dropped:
        print(_dim('Note: left out of the index search: %s (under %d characters)'
                   % (', '.join(dropped), search.floor(engine))))
    if mode == 'like':
        print(_dim('Note: every term is under %d characters, so the longest one was '
                   'matched as a substring' % search.floor(engine)))
    if not rows:
        # Say which index found nothing: "none here" and "none there" differ.
        print('No matches (%s, %s)' % (mode, target))
        return

    for r in rows:
        head = '%s  %s  %s' % (r['ts'] or '(no ts)', r['source'], r['role'])
        if r['model']:
            head += '  ' + r['model']
        print(_bold(head))
        if r['title']:
            print('  %s' % _dim(r['title']))
        if r['cwd']:
            print('  %s' % _dim(r['cwd']))
        print('  %s' % _render(r['hit']).replace('\n', ' '))
        print('  %s' % _dim('id=%d' % r['id']))
        print()
    print(_dim('%d results (%s, %s)' % (len(rows), mode, target)))


def cmd_sessions(args):
    conn = db.connect(args.db, readonly=True)
    rows = search.sessions(conn, cwd=args.cwd, source=args.source,
                           since=args.since, limit=args.limit, host=args.host)
    if not rows:
        print('No matches')
        return
    for r in rows:
        print('%s  %-16s %5d msgs  %s' %
              ((r['ended_at'] or r['started_at'] or '(no ts)')[:19], r['source'],
               r['msg_count'], _bold(r['title'] or r['ext_id'])))
        if r['cwd']:
            print('  %s' % _dim(r['cwd']))
        print('  %s' % _dim(r['ext_id']))
        print()


def cmd_show(args):
    conn = db.connect(args.db, readonly=True)
    kinds = ['message'] + (['thinking'] if args.thinking else [])
    sess, msgs = search.show(conn, args.session, masked=not args.raw, kinds=kinds,
                             source=args.source)
    if not sess:
        print('Session not found: %s' % args.session)
        return 1
    if sess.get('ambiguous'):
        print(_dim('Note: this id exists in more than one source. Narrow it with -s.'))
    print(_bold(sess['title'] or sess['ext_id']))
    print(_dim('%s / %s / %s' % (sess['source'], sess['cwd'] or '-',
                                 sess['started_at'] or sess['ended_at'] or '-')))
    print()
    for m in msgs:
        tag = m['role'] if m['kind'] == 'message' else '%s(%s)' % (m['role'], m['kind'])
        print(_bold('── %s %s' % (tag, _dim(m['ts'] or ''))))
        print(m['text'])
        print()


def cmd_backfill(args):
    conn = db.connect(args.db)
    r = project.backfill(conn, only_missing=not args.all)
    print('host             : %s' % r['host'])
    print('cwds resolved    : %d' % r['cwds'])
    print('sessions updated : %d' % r['sessions'])
    print()
    for row in conn.execute(
            'SELECT project_key, COUNT(*) n FROM sessions WHERE project_key IS NOT NULL '
            'GROUP BY project_key ORDER BY n DESC LIMIT 10'):
        print('  %4d  %s' % (row['n'], row['project_key']))


def cmd_push(args):
    conn = db.connect(args.db)
    # Fill in project_key / host first — the server cannot resolve them.
    project.backfill(conn, only_missing=True)

    kinds = push_mod.DEFAULT_KINDS + (('tool_call', 'tool_result')
                                      if args.include_tools else ())

    def progress(st):
        sys.stderr.write('\r  sent %6d / accepted %6d' % (st['sent'], st['accepted']))
        sys.stderr.flush()

    try:
        st = push_mod.push(conn, args.url, args.token or os.environ.get('CHIST_TOKEN'),
                           kinds=kinds, masked=not args.raw, reset=args.reset,
                           progress=progress, limit=args.limit)
    except Exception as e:
        sys.stderr.write('\r' + ' ' * 50 + '\r')
        print('Push failed: %s' % e)
        return 1
    sys.stderr.write('\r' + ' ' * 50 + '\r')

    print('endpoint : %s' % st['endpoint'])
    print('host     : %s' % st['host'])
    print('from id  : %d -> %d' % (st['from_id'], st['last_id']))
    print('sent     : %d (%d batches)' % (st['sent'], st['batches']))
    print('accepted : %d (the rest were already present)' % st['accepted'])
    print('elapsed  : %.1fs' % st['seconds'])


def cmd_stats(args):
    conn = db.connect(args.db, readonly=True)
    print(_bold('By source'))
    for r in conn.execute(
            'SELECT m.source, COUNT(*) n, COUNT(DISTINCT m.session_id) sess,'
            '       MIN(m.ts) first, MAX(m.ts) last '
            'FROM messages m GROUP BY m.source ORDER BY n DESC'):
        print('  %-16s %8d msgs  %5d sessions  %s -> %s' %
              (r['source'], r['n'], r['sess'], (r['first'] or '-')[:10], (r['last'] or '-')[:10]))

    print()
    print(_bold('By kind'))
    for r in conn.execute('SELECT kind, COUNT(*) n FROM messages GROUP BY kind ORDER BY n DESC'):
        print('  %-14s %8d' % (r['kind'], r['n']))

    print()
    print(_bold('By model (top 10)'))
    for r in conn.execute('SELECT COALESCE(model,"(not recorded)") model, provider, COUNT(*) n '
                          'FROM messages GROUP BY model, provider ORDER BY n DESC LIMIT 10'):
        print('  %-22s %-10s %8d' % (r['model'], r['provider'] or '-', r['n']))

    print()
    print(_bold('By project (top 10)'))
    for r in conn.execute(
            'SELECT cwd, COUNT(*) n, COUNT(DISTINCT source) srcs FROM messages '
            'WHERE cwd IS NOT NULL GROUP BY cwd ORDER BY n DESC LIMIT 10'):
        print('  %8d msgs  %d tools  %s' % (r['n'], r['srcs'], r['cwd']))

    row = conn.execute('SELECT COUNT(*) n FROM ingest_errors').fetchone()
    if row['n']:
        print()
        print(_dim('%d parse failures (see: chist errors)' % row['n']))
    print()
    p = db.db_path(args.db)
    print(_dim('DB: %s (%.1f MB)' % (p, os.path.getsize(p) / 1e6)))


def cmd_errors(args):
    conn = db.connect(args.db, readonly=True)
    for r in conn.execute('SELECT * FROM ingest_errors ORDER BY id DESC LIMIT ?', (args.limit,)):
        print('%s  %s' % (r['seen_at'], r['source']))
        print('  %s' % r['path'])
        print('  %s' % r['reason'])


def cmd_remask(args):
    conn = db.connect(args.db)
    st = prune_mod.remask(conn, apply=False)
    print('Stored in a form the current masking rules would change:')
    print('  messages : %d' % st['messages'])
    print('  titles   : %d' % st['titles'])
    if not (st['messages'] or st['titles']):
        return 0
    if not args.yes:
        print()
        print(_dim('Nothing was changed. Pass --yes to overwrite them with the masked '
                   'form. This cannot be undone, and is meant for the aggregation '
                   'server -- a local database keeps its text verbatim on purpose.'))
        return 0
    st = prune_mod.remask(conn, apply=True)
    print('Overwritten: %d messages / %d titles' % (st['messages'], st['titles']))
    searchdb.after_write(conn, log=lambda m: sys.stderr.write(m + '\n'))
    return 0


def cmd_reindex(args):
    """Maintain the search database (chist.searchdb)."""
    hp = db.db_path(args.db)
    if not os.path.exists(hp):
        print('No history database at %s' % hp)
        return 1
    sconn = searchdb.connect(hp, args.search_db)
    print('record: %s' % hp)
    print('copy  : %s' % (args.search_db or searchdb.default_path(hp)))

    if args.compare:
        hconn = db.connect(hp, readonly=True)
        bad = 0
        for q in args.compare:
            a, mode_a, _ = search.search(hconn, q, limit=args.limit, engine='legacy')
            b, mode_b, _ = searchdb.search(sconn, q, limit=args.limit)
            ia, ib = {r['id'] for r in a}, {r['id'] for r in b}
            same = ia == ib
            bad += not same
            print('%-4s %-24r legacy %4d (%s) / split %4d (%s)  only-legacy %d  only-split %d'
                  % ('ok' if same else 'DIFF', q, len(ia), mode_a, len(ib), mode_b,
                     len(ia - ib), len(ib - ia)))
        hconn.close()
        sconn.close()
        return 1 if bad else 0

    if args.check:
        pass
    elif args.rebuild:
        print('rebuilt: %d rows' % searchdb.rebuild(sconn))
    elif searchdb.stale(sconn) and \
            sconn.execute('SELECT 1 FROM docs LIMIT 1').fetchone():
        print('masking rules changed since the copy was built')
        print('rebuilt: %d rows' % searchdb.rebuild(sconn))
    else:
        dropped = searchdb.reconcile(sconn)
        added = searchdb.sync(sconn)
        searchdb.optimize(sconn)
        print('synced: +%d / -%d rows' % (added, dropped))

    st = searchdb.check(sconn)
    sconn.close()
    print('record %(record)d rows / copy %(copy)d rows / missing %(missing)d / '
          'extra %(extra)d / text differs %(text_differs)d' % st)
    if not st['mask_rules_current']:
        print('note: the copy was built with other masking rules than the current ones')
    if st['record_unmasked']:
        print('note: %d rows of the record are stored in a form the current masking rules '
              'would change (the copy holds them masked)' % st['record_unmasked'])
    print('ok' if st['ok'] else 'NOT IN STEP -- run `chist reindex`; if it stays so, '
          '`chist reindex --rebuild`')
    return 0 if st['ok'] else 1


def cmd_prune(args):
    where, wargs = prune_mod.build_filter(
        before=args.before, after=args.after, source=args.source,
        session=args.session, match=args.match)
    if where is None:
        print('Specify at least one condition (--before / --after / --source /'
              ' --session / --match).')
        print('To wipe everything, delete the database file instead: %s'
              % db.db_path(args.db))
        return 2

    conn = db.connect(args.db)
    pv = prune_mod.preview(conn, where, wargs)
    if not pv['messages']:
        print('No messages match those conditions.')
        return 0

    print('To be deleted')
    print('  messages : %d' % pv['messages'])
    print('  sessions : spread over %d (%d of them will be left empty and removed)'
          % (pv['sessions'], pv['empty_sessions']))
    for r in pv['by_source']:
        print('    %-16s %6d  %s .. %s'
              % (r['source'], r['n'], r['first'] or '-', r['last'] or '-'))

    if args.dry_run:
        print()
        print(_dim('--dry-run: nothing was deleted.'))
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print()
            print('Cannot ask for confirmation here. Pass --yes to proceed.')
            return 2
        try:
            ans = input('\nDelete the above? [y/N]: ')
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if ans.strip().lower() not in ('y', 'yes'):
            print('Aborted.')
            return 1

    st = prune_mod.prune(conn, where, wargs, drop_empty_sessions=not args.keep_sessions)
    print('Deleted: %d messages / %d empty sessions' % (st['messages'], st['sessions']))
    searchdb.after_write(conn, deleted=True, log=lambda m: sys.stderr.write(m + '\n'))

    if args.vacuum:
        print('Running VACUUM... (needs free space equal to the database size)')
        prune_mod.vacuum(conn)
        print('Done.')
    else:
        print(_dim('The file did not shrink. Re-run with --vacuum to reclaim space.'))

    print()
    print(_dim('Note: if the original log files are still present and you clear '
               'ingest_state, a re-ingest brings these back.'))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='chist',
                                description='Unified search over CLI chat history')
    p.add_argument('--db', help='database path (default: ~/.local/share/chist/history.db)')
    sub = p.add_subparsers(dest='cmd')

    def add_src(sp):
        sp.add_argument('-s', '--source', action='append', choices=sorted(ADAPTERS),
                        help='filter by source (repeatable)')

    sp = sub.add_parser('ingest', help='import history (incremental)')
    add_src(sp)
    sp.add_argument('--limit', type=int, help='cap the number of files (for smoke tests)')
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser('search', help='full-text search')
    sp.add_argument('query')
    add_src(sp)
    sp.add_argument('-c', '--cwd', help='substring match on the project path')
    sp.add_argument('-r', '--role', choices=['user', 'assistant', 'system', 'tool'])
    sp.add_argument('--since', help='ISO 8601 (e.g. 2026-07-01)')
    sp.add_argument('--until')
    sp.add_argument('-n', '--limit', type=int, default=20)
    sp.add_argument('--host', action='append', help='filter by machine (repeatable)')
    sp.add_argument('--raw', action='store_true', help='do not mask secrets')
    sp.add_argument('-t', '--target', choices=list(search.TARGETS),
                    help='which database answers: the search database (default where '
                         'one is kept) or the index inside the record')
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser('sessions', help='list sessions')
    add_src(sp)
    sp.add_argument('-c', '--cwd')
    sp.add_argument('--host', action='append', help='filter by machine (repeatable)')
    sp.add_argument('--since')
    sp.add_argument('-n', '--limit', type=int, default=20)
    sp.set_defaults(func=cmd_sessions)

    sp = sub.add_parser('show', help='print a conversation')
    sp.add_argument('session', help='ext_id or id')
    sp.add_argument('-s', '--source', choices=sorted(ADAPTERS),
                    help='narrow it down when the same id exists in several sources')
    sp.add_argument('--thinking', action='store_true', help='include thinking blocks')
    sp.add_argument('--raw', action='store_true')
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser('stats', help='statistics')
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser('backfill', help='fill in host / project_key on existing rows')
    sp.add_argument('--all', action='store_true', help='re-resolve rows that already have them')
    sp.set_defaults(func=cmd_backfill)

    sp = sub.add_parser('push', help='send the delta to an aggregation server')
    sp.add_argument('url', help='e.g. http://192.168.1.10:8848')
    sp.add_argument('--token', help='defaults to the CHIST_TOKEN environment variable')
    sp.add_argument('--include-tools', action='store_true',
                    help='also send tool_call / tool_result (off by default)')
    sp.add_argument('--raw', action='store_true', help='send without masking secrets')
    sp.add_argument('--reset', action='store_true', help='resend everything from the start')
    sp.add_argument('--limit', type=int, help='cap the number sent (for smoke tests)')
    sp.set_defaults(func=cmd_push)

    sp = sub.add_parser('errors', help='list parse failures')
    sp.add_argument('-n', '--limit', type=int, default=20)
    sp.set_defaults(func=cmd_errors)

    sp = sub.add_parser('prune', help='delete history (at least one condition required)')
    add_src(sp)
    sp.add_argument('--before', help='delete entries older than this (ISO 8601, e.g. 2026-01-01)')
    sp.add_argument('--after', help='delete entries at or after this time')
    sp.add_argument('--session', help='delete a single session (ext_id or id)')
    sp.add_argument('--match', help='delete entries whose body contains this string')
    sp.add_argument('--dry-run', action='store_true', help='report counts without deleting')
    sp.add_argument('-y', '--yes', action='store_true', help='skip the confirmation prompt')
    sp.add_argument('--vacuum', action='store_true',
                    help='run VACUUM afterwards to reclaim file size')
    sp.add_argument('--keep-sessions', action='store_true',
                    help='keep sessions even when no body remains')
    sp.set_defaults(func=cmd_prune)

    sp = sub.add_parser('remask', help='apply the current masking rules to stored text '
                        '(server; dry run unless --yes)')
    sp.add_argument('-y', '--yes', action='store_true',
                    help='overwrite the stored text (cannot be undone)')
    sp.set_defaults(func=cmd_remask)

    sp = sub.add_parser('reindex', help='maintain the search database (search.db)')
    sp.add_argument('--search-db', help='path of the search database '
                    '(default: CHIST_SEARCH_DB, else search.db beside the history)')
    g = sp.add_mutually_exclusive_group()
    g.add_argument('--check', action='store_true',
                   help='only compare the copy with the record; change nothing')
    g.add_argument('--rebuild', action='store_true',
                   help='empty the copy and fill it again from the record')
    g.add_argument('--compare', action='append', metavar='QUERY',
                   help='run QUERY on the old index and on the copy and report '
                        'whether the same rows come back (repeatable)')
    sp.add_argument('-n', '--limit', type=int, default=1000,
                    help='row cap per engine for --compare (default 1000)')
    sp.set_defaults(func=cmd_reindex)

    args = p.parse_args(argv)
    if not getattr(args, 'func', None):
        p.print_help()
        return 1
    return args.func(args) or 0


if __name__ == '__main__':
    sys.exit(main())
