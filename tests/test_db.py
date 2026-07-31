"""Schema behaviour: constraints, PRAGMAs, and the deduplication that ingest relies on."""
import os
import sqlite3
import unittest

from chist import db

from .helpers import DBTestCase


class TestSchema(DBTestCase):
    def test_foreign_keys_are_enforced(self):
        """PRAGMA foreign_keys is per-connection; the declaration in schema.sql only
        applied to the connection that ran executescript."""
        self.assertEqual(self.conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)

    def test_readonly_connection_also_enables_foreign_keys(self):
        ro = db.connect(self.db_path, readonly=True)
        try:
            self.assertEqual(ro.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        finally:
            ro.close()

    def test_deleting_a_session_cascades_to_messages(self):
        sid = self.add_session('codex', 'S1', title='t')
        self.add_message(sid, 'codex', 'M1', 'body')
        self.conn.execute('DELETE FROM sessions WHERE id = ?', (sid,))
        self.conn.commit()
        self.assertEqual(self.count('messages'), 0)

    def test_database_file_is_owner_only(self):
        self.assertEqual(os.stat(self.db_path).st_mode & 0o777, 0o600)


class TestDeduplication(DBTestCase):
    """Ingest is idempotent because of UNIQUE (source, ext_id, part)."""

    def setUp(self):
        super().setUp()
        self.sid = self.add_session('claude-code', 'S1', title='t')

    def test_same_message_twice_is_stored_once(self):
        for _ in range(2):
            self.add_message(self.sid, 'claude-code', 'M1', 'body')
        self.assertEqual(self.count('messages'), 1)

    def test_same_ext_id_different_part_is_kept(self):
        """One record can split into several blocks; part separates them."""
        self.add_message(self.sid, 'claude-code', 'M1', 'first block', part=0)
        self.add_message(self.sid, 'claude-code', 'M1', 'second block', part=1)
        self.assertEqual(self.count('messages'), 2)

    def test_same_ext_id_different_source_is_kept(self):
        other = self.add_session('codex', 'S1', title='t')
        self.add_message(self.sid, 'claude-code', 'M1', 'from claude')
        self.add_message(other, 'codex', 'M1', 'from codex')
        self.assertEqual(self.count('messages'), 2)


class TestFtsIndexing(DBTestCase):
    """Only the searchable kinds enter the index."""

    def setUp(self):
        super().setUp()
        self.sid = self.add_session('claude-code', 'S1', title='t')

    def _fts_hits(self, term):
        return self.conn.execute(
            'SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?',
            ('"%s"' % term,)).fetchone()[0]

    def test_message_is_indexed(self):
        self.add_message(self.sid, 'claude-code', 'M1', 'indexable text', kind='message')
        self.assertEqual(self._fts_hits('indexable'), 1)

    def test_tool_output_is_not_indexed(self):
        """Large diffs and file dumps would drown out the signal."""
        self.add_message(self.sid, 'claude-code', 'T1', 'toolnoise text', kind='tool_result')
        self.assertEqual(self._fts_hits('toolnoise'), 0)

    def test_thinking_is_indexed(self):
        self.add_message(self.sid, 'claude-code', 'K1', 'reasoning text', kind='thinking')
        self.assertEqual(self._fts_hits('reasoning'), 1)


class TestMigration(unittest.TestCase):
    """A version-1 database must come up to the current schema without losing rows."""

    def test_v1_database_is_migrated(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix='chist-mig-') as d:
            path = os.path.join(d, 'old.db')
            raw = sqlite3.connect(path)
            raw.executescript(db.SCHEMA_PATH.read_text())
            # Pretend this is the pre-aggregation schema. Foreign keys stay off while
            # the referenced table is dropped.
            raw.executescript(
                'PRAGMA foreign_keys = OFF;'
                'DROP TABLE IF EXISTS hosts;'
                'DROP TABLE IF EXISTS push_state;'
                'PRAGMA user_version = 1;')
            raw.execute("INSERT INTO sessions (source, ext_id, title)"
                        " VALUES ('codex','S1','kept')")
            raw.commit()
            raw.close()

            conn = db.connect(path)
            try:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],
                                 db.SCHEMA_VERSION)
                # The new tables exist...
                conn.execute('SELECT COUNT(*) FROM hosts').fetchone()
                conn.execute('SELECT COUNT(*) FROM push_state').fetchone()
                # ...and the existing row survived.
                row = conn.execute("SELECT title FROM sessions WHERE ext_id='S1'").fetchone()
                self.assertEqual(row['title'], 'kept')
            finally:
                conn.close()

    def test_migration_is_idempotent(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix='chist-mig2-') as d:
            path = os.path.join(d, 'db.sqlite')
            db.connect(path).close()
            db.connect(path).close()   # must not raise on the second open


if __name__ == '__main__':
    unittest.main()
