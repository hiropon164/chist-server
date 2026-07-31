"""履歴の削除。

取り込みは追記のみなので、放っておくと DB は増える一方になる。誤って取り込んだ
ものや機微な会話を消す手段としても要る。

**条件を 1 つ以上指定しないと何も消さない。** 引数なしで全件削除される事故を
避けるため、全消しは意図的に用意していない（消したいなら DB ファイルごと消す）。

FTS は `content=` の外部コンテンツ方式なので、`messages` の DELETE トリガが
索引側も落とす。ここで messages_fts を直接触る必要はない。
"""

# 期間の判定に使う日時。cursor-cli は ts が NULL なので、セッションの
# 終了時刻（無ければ開始時刻）で代替する。これをしないと ts を持たない
# ソースが期間指定の削除から漏れる。
_TS = ('COALESCE(m.ts, (SELECT COALESCE(s.ended_at, s.started_at)'
       '                FROM sessions s WHERE s.id = m.session_id))')


def build_filter(before=None, after=None, source=None, session=None, match=None):
    """削除条件を (SQL 断片, 引数) にする。条件が無ければ (None, []) を返す。"""
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
        # ext_id は (source, ext_id) でしか一意にならない。source 指定があれば
        # 上の条件と AND されるので、別ソースの同名セッションを巻き込まない。
        conds.append('m.session_id IN (SELECT id FROM sessions WHERE ext_id = ? OR id = ?)')
        args += [session, session]
    if match:
        conds.append('m.text LIKE ?')
        args.append('%' + match + '%')
    if not conds:
        return None, []
    return ' AND '.join(conds), args


def preview(conn, where, args):
    """削除対象の件数と内訳を返す。実際には消さない。"""
    total = conn.execute(
        'SELECT COUNT(*) FROM messages m WHERE %s' % where, args).fetchone()[0]
    by_source = [dict(r) for r in conn.execute(
        'SELECT m.source, COUNT(*) n, MIN(%s) first, MAX(%s) last '
        'FROM messages m WHERE %s GROUP BY m.source ORDER BY n DESC'
        % (_TS, _TS, where), args)]
    sessions = conn.execute(
        'SELECT COUNT(DISTINCT m.session_id) FROM messages m WHERE %s' % where,
        args).fetchone()[0]
    # 全メッセージが対象になる（＝空になる）セッション
    empty = conn.execute(
        'SELECT COUNT(*) FROM sessions s WHERE EXISTS ('
        '  SELECT 1 FROM messages m WHERE m.session_id = s.id AND %s) '
        'AND NOT EXISTS ('
        '  SELECT 1 FROM messages m WHERE m.session_id = s.id AND NOT (%s))'
        % (where, where), args + args).fetchone()[0]
    return {'messages': total, 'sessions': sessions, 'empty_sessions': empty,
            'by_source': by_source}


def prune(conn, where, args, drop_empty_sessions=True):
    """条件に一致するメッセージを削除する。統計 dict を返す。"""
    # 「今回の削除で空になった」ものだけを対象にしたいので、消す前に控えておく。
    # 全体から空セッションを探すと、元々本文を持たないセッションまで巻き込む。
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
    """削除で空いた領域を OS に返す。FTS も最適化する。

    VACUUM はファイルを作り直すので、DB と同じサイズの空きが要る。
    """
    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('optimize')")
    conn.commit()
    conn.execute('VACUUM')
    conn.commit()
