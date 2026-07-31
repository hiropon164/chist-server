"""Shared fixtures for the tests.

Everything runs against a throwaway database in a temporary directory, so no test
touches a real history file.
"""
import os
import tempfile
import unittest

from chist import db


class DBTestCase(unittest.TestCase):
    """Base class giving each test its own empty database."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix='chist-test-')
        self.db_path = os.path.join(self._dir.name, 'history.db')
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    # -- fixtures ---------------------------------------------------------

    def add_session(self, source, ext_id, title=None, cwd=None, host=None,
                    started_at=None, ended_at=None):
        if host:
            # sessions.host references hosts(host), and foreign keys are enforced.
            self.conn.execute(
                'INSERT OR IGNORE INTO hosts (host, os) VALUES (?, ?)', (host, 'linux'))
        self.conn.execute(
            'INSERT INTO sessions (source, ext_id, title, cwd, host, started_at, ended_at)'
            ' VALUES (?,?,?,?,?,?,?)',
            (source, ext_id, title, cwd, host, started_at, ended_at))
        self.conn.commit()
        return self.conn.execute(
            'SELECT id FROM sessions WHERE source=? AND ext_id=?',
            (source, ext_id)).fetchone()['id']

    def add_message(self, session_id, source, ext_id, text, role='user',
                    kind='message', ts=None, part=0, cwd=None):
        self.conn.execute(
            'INSERT OR IGNORE INTO messages'
            ' (session_id, source, ext_id, part, role, kind, ts, text, cwd)'
            ' VALUES (?,?,?,?,?,?,?,?,?)',
            (session_id, source, ext_id, part, role, kind, ts, text, cwd))
        self.conn.commit()

    def count(self, table):
        return self.conn.execute('SELECT COUNT(*) FROM %s' % table).fetchone()[0]
