"""DB 接続とスキーマ適用。"""
import os
import pathlib
import sqlite3

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / 'db' / 'schema.sql'
DEFAULT_DB = os.path.expanduser('~/.local/share/chist/history.db')


SCHEMA_VERSION = 2

# 版 1（単一マシン）から 2（複数マシン集約）への差分。
_MIGRATIONS = {
    2: [
        'CREATE TABLE IF NOT EXISTS hosts ('
        '  host TEXT PRIMARY KEY, os TEXT, last_seen TEXT)',
        'ALTER TABLE sessions ADD COLUMN host TEXT',
        'ALTER TABLE sessions ADD COLUMN project_key TEXT',
        'CREATE INDEX IF NOT EXISTS ix_sessions_project ON sessions(project_key)',
        'CREATE INDEX IF NOT EXISTS ix_sessions_host ON sessions(host)',
        'CREATE TABLE IF NOT EXISTS push_state ('
        '  endpoint TEXT PRIMARY KEY, last_id INTEGER NOT NULL DEFAULT 0,'
        '  sent INTEGER NOT NULL DEFAULT 0, updated_at TEXT)',
    ],
}


def _migrate(conn):
    """既存 DB を最新スキーマへ寄せる。列が既にあれば黙って飛ばす。"""
    cur = conn.execute('PRAGMA user_version').fetchone()[0]
    if cur >= SCHEMA_VERSION:
        return 0
    applied = 0
    for version in range(cur + 1, SCHEMA_VERSION + 1):
        for stmt in _MIGRATIONS.get(version, []):
            try:
                conn.execute(stmt)
                applied += 1
            except sqlite3.OperationalError as e:
                if 'duplicate column' not in str(e).lower():
                    raise
    conn.execute('PRAGMA user_version = %d' % SCHEMA_VERSION)
    conn.commit()
    return applied


def connect(path=None, readonly=False):
    """DB を開く。存在しなければスキーマを適用して作り、古ければ移行する。"""
    path = os.path.expanduser(path or os.environ.get('CHIST_DB') or DEFAULT_DB)
    if readonly:
        conn = sqlite3.connect('file:%s?mode=ro' % path, uri=True)
        conn.row_factory = sqlite3.Row
        # PRAGMA は接続ごとの設定。schema.sql の宣言は引き継がれない
        conn.execute('PRAGMA foreign_keys = ON')
        return conn

    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    fresh = not os.path.exists(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # schema.sql の PRAGMA foreign_keys は executescript した接続にしか効かない。
    # 明示的に入れないと messages の ON DELETE CASCADE が動かない。
    conn.execute('PRAGMA foreign_keys = ON')
    if fresh:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
        # 履歴には認証情報が混入しうるため、所有者のみ読める権限にする
        os.chmod(path, 0o600)
    else:
        _migrate(conn)
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA synchronous = NORMAL')
    return conn


def db_path(path=None):
    return os.path.expanduser(path or os.environ.get('CHIST_DB') or DEFAULT_DB)
