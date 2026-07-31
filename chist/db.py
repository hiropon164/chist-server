"""Database connection and schema management."""
import os
import pathlib
import sqlite3

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / 'db' / 'schema.sql'
DEFAULT_DB = os.path.expanduser('~/.local/share/chist/history.db')


SCHEMA_VERSION = 2

# Delta from version 1 (single machine) to 2 (multi-machine aggregation).
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
    """Bring an existing database up to the current schema, skipping what is already there."""
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
    """Open the database, creating it from the schema or migrating it as needed."""
    path = os.path.expanduser(path or os.environ.get('CHIST_DB') or DEFAULT_DB)
    if readonly:
        conn = sqlite3.connect('file:%s?mode=ro' % path, uri=True)
        conn.row_factory = sqlite3.Row
        # PRAGMAs are per-connection; the declaration in schema.sql does not carry over
        conn.execute('PRAGMA foreign_keys = ON')
        return conn

    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    fresh = not os.path.exists(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # PRAGMA foreign_keys in schema.sql only applies to the connection that ran
    # executescript. Without setting it here, ON DELETE CASCADE on messages is inert.
    conn.execute('PRAGMA foreign_keys = ON')
    if fresh:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
        # History can contain credentials, so keep it readable by the owner only
        os.chmod(path, 0o600)
    else:
        _migrate(conn)
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA synchronous = NORMAL')
    return conn


def db_path(path=None):
    return os.path.expanduser(path or os.environ.get('CHIST_DB') or DEFAULT_DB)
