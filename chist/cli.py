"""chist — CLI チャット履歴の統合検索."""
import argparse
import os
import sys

from . import db, ingest, project, prune as prune_mod, push as push_mod, search
from .adapters import ADAPTERS

BOLD, DIM, HL, RESET = '\033[1m', '\033[2m', '\033[43;30m', '\033[0m'


def _color():
    return sys.stdout.isatty() and os.environ.get('NO_COLOR') is None


def _render(text):
    """snippet の \x02/\x03 マーカーを端末のハイライトに変換する。"""
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
        sys.stderr.write('\r  %-16s %5d/%-5d  新規 %6d 件' % (source, i, n, st['messages']))
        sys.stderr.flush()

    sources = args.source or list(ADAPTERS)
    stats = ingest.ingest_all(conn, sources, progress=progress, limit=args.limit)
    sys.stderr.write('\r' + ' ' * 60 + '\r')

    print('%-16s %7s %7s %7s %8s %8s %6s' %
          ('source', 'files', 'read', 'skip', 'msgs', 'sessions', 'sec'))
    for s in stats:
        print('%-16s %7d %7d %7d %8d %8d %6.1f' %
              (s['source'], s['files'], s['scanned'], s['skipped'],
               s['messages'], s['sessions'], s['seconds']))
        if s['errors']:
            print('  %s' % _dim('パース失敗 %d 件（chist errors で確認）' % s['errors']))
        if s['reingested']:
            print('  %s' % _dim('書き換え検知により全再取り込み %d 件' % s['reingested']))
    print()
    print('DB: %s' % db.db_path(args.db))


def cmd_search(args):
    conn = db.connect(args.db, readonly=True)
    rows, mode = search.search(
        conn, args.query, source=args.source, cwd=args.cwd, role=args.role,
        since=args.since, until=args.until, limit=args.limit, masked=not args.raw,
        host=args.host)

    if mode == 'like':
        print(_dim('※ 2文字以下のため trigram を使わず LIKE で検索しています'))
    if not rows:
        print('該当なし')
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
    print(_dim('%d 件 (%s)' % (len(rows), mode)))


def cmd_sessions(args):
    conn = db.connect(args.db, readonly=True)
    rows = search.sessions(conn, cwd=args.cwd, source=args.source,
                           since=args.since, limit=args.limit, host=args.host)
    if not rows:
        print('該当なし')
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
        print('セッションが見つかりません: %s' % args.session)
        return 1
    if sess.get('ambiguous'):
        print(_dim('※ 同じ id が複数のソースに存在します。-s で絞ってください。'))
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
    print('host        : %s' % r['host'])
    print('cwd 解決     : %d 件' % r['cwds'])
    print('sessions 更新: %d 件' % r['sessions'])
    print()
    for row in conn.execute(
            'SELECT project_key, COUNT(*) n FROM sessions WHERE project_key IS NOT NULL '
            'GROUP BY project_key ORDER BY n DESC LIMIT 10'):
        print('  %4d  %s' % (row['n'], row['project_key']))


def cmd_push(args):
    conn = db.connect(args.db)
    # project_key / host が未設定なら先に埋める（サーバ側で解決できないため）
    project.backfill(conn, only_missing=True)

    kinds = push_mod.DEFAULT_KINDS + (('tool_call', 'tool_result')
                                      if args.include_tools else ())

    def progress(st):
        sys.stderr.write('\r  送信 %6d 件 / 受理 %6d 件' % (st['sent'], st['accepted']))
        sys.stderr.flush()

    try:
        st = push_mod.push(conn, args.url, args.token or os.environ.get('CHIST_TOKEN'),
                           kinds=kinds, masked=not args.raw, reset=args.reset,
                           progress=progress, limit=args.limit)
    except Exception as e:
        sys.stderr.write('\r' + ' ' * 50 + '\r')
        print('送信に失敗しました: %s' % e)
        return 1
    sys.stderr.write('\r' + ' ' * 50 + '\r')

    print('宛先     : %s' % st['endpoint'])
    print('host     : %s' % st['host'])
    print('開始 id  : %d → %d' % (st['from_id'], st['last_id']))
    print('送信     : %d 件 (%d バッチ)' % (st['sent'], st['batches']))
    print('受理     : %d 件（残りは既に存在した分）' % st['accepted'])
    print('所要     : %.1f 秒' % st['seconds'])


def cmd_stats(args):
    conn = db.connect(args.db, readonly=True)
    print(_bold('ソース別'))
    for r in conn.execute(
            'SELECT m.source, COUNT(*) n, COUNT(DISTINCT m.session_id) sess,'
            '       MIN(m.ts) first, MAX(m.ts) last '
            'FROM messages m GROUP BY m.source ORDER BY n DESC'):
        print('  %-16s %8d msgs  %5d sessions  %s → %s' %
              (r['source'], r['n'], r['sess'], (r['first'] or '-')[:10], (r['last'] or '-')[:10]))

    print()
    print(_bold('kind 別'))
    for r in conn.execute('SELECT kind, COUNT(*) n FROM messages GROUP BY kind ORDER BY n DESC'):
        print('  %-14s %8d' % (r['kind'], r['n']))

    print()
    print(_bold('モデル別 (上位10)'))
    for r in conn.execute('SELECT COALESCE(model,"(記録なし)") model, provider, COUNT(*) n '
                          'FROM messages GROUP BY model, provider ORDER BY n DESC LIMIT 10'):
        print('  %-22s %-10s %8d' % (r['model'], r['provider'] or '-', r['n']))

    print()
    print(_bold('プロジェクト別 (上位10)'))
    for r in conn.execute(
            'SELECT cwd, COUNT(*) n, COUNT(DISTINCT source) srcs FROM messages '
            'WHERE cwd IS NOT NULL GROUP BY cwd ORDER BY n DESC LIMIT 10'):
        print('  %8d msgs  %d tools  %s' % (r['n'], r['srcs'], r['cwd']))

    row = conn.execute('SELECT COUNT(*) n FROM ingest_errors').fetchone()
    if row['n']:
        print()
        print(_dim('パース失敗 %d 件（chist errors で確認）' % row['n']))
    print()
    p = db.db_path(args.db)
    print(_dim('DB: %s (%.1f MB)' % (p, os.path.getsize(p) / 1e6)))


def cmd_errors(args):
    conn = db.connect(args.db, readonly=True)
    for r in conn.execute('SELECT * FROM ingest_errors ORDER BY id DESC LIMIT ?', (args.limit,)):
        print('%s  %s' % (r['seen_at'], r['source']))
        print('  %s' % r['path'])
        print('  %s' % r['reason'])


def cmd_prune(args):
    where, wargs = prune_mod.build_filter(
        before=args.before, after=args.after, source=args.source,
        session=args.session, match=args.match)
    if where is None:
        print('条件を 1 つ以上指定してください（--before / --after / --source /'
              ' --session / --match）。')
        print('全件を消したい場合は DB ファイルごと削除してください: %s'
              % db.db_path(args.db))
        return 2

    conn = db.connect(args.db)
    pv = prune_mod.preview(conn, where, wargs)
    if not pv['messages']:
        print('条件に一致するメッセージはありません。')
        return 0

    print('削除対象')
    print('  メッセージ  : %d 件' % pv['messages'])
    print('  セッション  : %d 件に含まれる（うち %d 件は空になるので削除）'
          % (pv['sessions'], pv['empty_sessions']))
    for r in pv['by_source']:
        print('    %-16s %6d 件  %s 〜 %s'
              % (r['source'], r['n'], r['first'] or '-', r['last'] or '-'))

    if args.dry_run:
        print()
        print(_dim('--dry-run のため削除していません。'))
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print()
            print('確認できない環境です。実行するなら --yes を付けてください。')
            return 2
        try:
            ans = input('\n上記を削除します。よろしいですか [y/N]: ')
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if ans.strip().lower() not in ('y', 'yes'):
            print('中止しました。')
            return 1

    st = prune_mod.prune(conn, where, wargs, drop_empty_sessions=not args.keep_sessions)
    print('削除: メッセージ %d 件 / 空セッション %d 件' % (st['messages'], st['sessions']))

    if args.vacuum:
        print('VACUUM 実行中…（DB と同じサイズの空き容量が要ります）')
        prune_mod.vacuum(conn)
        print('完了。')
    else:
        print(_dim('ファイルサイズは縮んでいません。詰めるなら --vacuum を付けて再実行。'))

    print()
    print(_dim('注意: 元のログファイルが残っていて ingest_state を消すと、'
               '再取り込みで復活します。'))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='chist', description='CLI チャット履歴の統合検索')
    p.add_argument('--db', help='DB パス (既定: ~/.local/share/chist/history.db)')
    sub = p.add_subparsers(dest='cmd')

    def add_src(sp):
        sp.add_argument('-s', '--source', action='append', choices=sorted(ADAPTERS),
                        help='ソースを絞る（複数指定可）')

    sp = sub.add_parser('ingest', help='履歴を取り込む（差分のみ）')
    add_src(sp)
    sp.add_argument('--limit', type=int, help='処理するファイル数の上限（動作確認用）')
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser('search', help='全文検索')
    sp.add_argument('query')
    add_src(sp)
    sp.add_argument('-c', '--cwd', help='プロジェクトパスの部分一致')
    sp.add_argument('-r', '--role', choices=['user', 'assistant', 'system', 'tool'])
    sp.add_argument('--since', help='ISO8601 (例 2026-07-01)')
    sp.add_argument('--until')
    sp.add_argument('-n', '--limit', type=int, default=20)
    sp.add_argument('--host', action='append', help='ホスト名で絞る（複数指定可）')
    sp.add_argument('--raw', action='store_true', help='シークレットをマスクしない')
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser('sessions', help='セッション一覧')
    add_src(sp)
    sp.add_argument('-c', '--cwd')
    sp.add_argument('--host', action='append', help='ホスト名で絞る（複数指定可）')
    sp.add_argument('--since')
    sp.add_argument('-n', '--limit', type=int, default=20)
    sp.set_defaults(func=cmd_sessions)

    sp = sub.add_parser('show', help='セッション本文を表示')
    sp.add_argument('session', help='ext_id または id')
    sp.add_argument('-s', '--source', choices=sorted(ADAPTERS),
                    help='同じ id が複数ソースにあるとき絞る')
    sp.add_argument('--thinking', action='store_true', help='思考ブロックも表示')
    sp.add_argument('--raw', action='store_true')
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser('stats', help='統計')
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser('backfill', help='host / project_key を既存データへ埋める')
    sp.add_argument('--all', action='store_true', help='設定済みのものも再解決する')
    sp.set_defaults(func=cmd_backfill)

    sp = sub.add_parser('push', help='管理サーバへ差分を送信する')
    sp.add_argument('url', help='例 http://192.168.1.10:8848')
    sp.add_argument('--token', help='既定は環境変数 CHIST_TOKEN')
    sp.add_argument('--include-tools', action='store_true',
                    help='tool_call / tool_result も送る（既定は送らない）')
    sp.add_argument('--raw', action='store_true', help='シークレットをマスクせず送る')
    sp.add_argument('--reset', action='store_true', help='最初から送り直す')
    sp.add_argument('--limit', type=int, help='送信件数の上限（動作確認用）')
    sp.set_defaults(func=cmd_push)

    sp = sub.add_parser('errors', help='パース失敗の一覧')
    sp.add_argument('-n', '--limit', type=int, default=20)
    sp.set_defaults(func=cmd_errors)

    sp = sub.add_parser('prune', help='履歴を削除する（条件の指定が必須）')
    add_src(sp)
    sp.add_argument('--before', help='この日時より古いものを削除 (ISO8601、例 2026-01-01)')
    sp.add_argument('--after', help='この日時以降のものを削除')
    sp.add_argument('--session', help='セッション単位で削除（ext_id または id）')
    sp.add_argument('--match', help='本文にこの文字列を含むものを削除（誤取り込みの救済用）')
    sp.add_argument('--dry-run', action='store_true', help='件数だけ表示して削除しない')
    sp.add_argument('-y', '--yes', action='store_true', help='確認を省略する')
    sp.add_argument('--vacuum', action='store_true',
                    help='削除後に VACUUM してファイルサイズを詰める')
    sp.add_argument('--keep-sessions', action='store_true',
                    help='本文が空になったセッションも残す')
    sp.set_defaults(func=cmd_prune)

    args = p.parse_args(argv)
    if not getattr(args, 'func', None):
        p.print_help()
        return 1
    return args.func(args) or 0


if __name__ == '__main__':
    sys.exit(main())
