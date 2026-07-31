"""Deletion: the guard against wiping everything, and the scope of what goes."""
import unittest

from chist import prune, search

from .helpers import DBTestCase


class TestFilterGuard(DBTestCase):
    def test_no_condition_returns_none(self):
        """An argument-less prune must not be able to match everything."""
        where, args = prune.build_filter()
        self.assertIsNone(where)
        self.assertEqual(args, [])

    def test_any_single_condition_builds_a_filter(self):
        for kwargs in [{'before': '2026-01-01'}, {'after': '2026-01-01'},
                       {'source': ['codex']}, {'session': 'S1'}, {'match': 'x'}]:
            with self.subTest(**kwargs):
                where, _ = prune.build_filter(**kwargs)
                self.assertIsNotNone(where)


class TestPruneScope(DBTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.add_session('claude-code', 'A', title='keep')
        self.b = self.add_session('codex', 'B', title='drop')
        # A session that was already empty before any deletion.
        self.empty = self.add_session('codex', 'EMPTY', title='never had a body')
        self.add_message(self.a, 'claude-code', 'A1', 'keep this', ts='2026-07-01T00:00:00Z')
        self.add_message(self.b, 'codex', 'B1', 'drop this', ts='2026-07-01T00:00:00Z')

    def test_deletes_only_matching_rows(self):
        where, args = prune.build_filter(source=['codex'])
        st = prune.prune(self.conn, where, args)
        self.assertEqual(st['messages'], 1)
        self.assertEqual(self.count('messages'), 1)

    def test_emptied_sessions_are_removed(self):
        where, args = prune.build_filter(source=['codex'])
        prune.prune(self.conn, where, args)
        rows = self.conn.execute("SELECT ext_id FROM sessions WHERE ext_id='B'").fetchall()
        self.assertEqual(rows, [])

    def test_pre_existing_empty_session_is_not_swept_up(self):
        """Regression: scanning for empty sessions after the delete also removed
        sessions that never had a body, which no condition had selected."""
        where, args = prune.build_filter(source=['codex'])
        st = prune.prune(self.conn, where, args)
        self.assertEqual(st['sessions'], 1, 'only the session emptied by this delete')
        survived = self.conn.execute(
            "SELECT 1 FROM sessions WHERE ext_id='EMPTY'").fetchone()
        self.assertIsNotNone(survived, 'an unrelated empty session must survive')

    def test_keep_sessions_leaves_the_shell(self):
        where, args = prune.build_filter(source=['codex'])
        st = prune.prune(self.conn, where, args, drop_empty_sessions=False)
        self.assertEqual(st['sessions'], 0)
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM sessions WHERE ext_id='B'").fetchone())

    def test_match_targets_the_body(self):
        where, args = prune.build_filter(match='drop this')
        st = prune.prune(self.conn, where, args)
        self.assertEqual(st['messages'], 1)

    def test_preview_does_not_delete(self):
        where, args = prune.build_filter(source=['codex'])
        pv = prune.preview(self.conn, where, args)
        self.assertEqual(pv['messages'], 1)
        self.assertEqual(self.count('messages'), 2, 'preview must be read-only')


class TestPruneTimestampFallback(DBTestCase):
    """Cursor CLI records no per-message timestamp."""

    def setUp(self):
        super().setUp()
        # ts is NULL on the messages; only the session carries a time.
        old = self.add_session('cursor-cli', 'OLD', title='old',
                               started_at='2026-01-01T00:00:00Z',
                               ended_at='2026-01-02T00:00:00Z')
        new = self.add_session('cursor-cli', 'NEW', title='new',
                               started_at='2026-07-01T00:00:00Z',
                               ended_at='2026-07-02T00:00:00Z')
        self.add_message(old, 'cursor-cli', 'O1', 'old body', ts=None)
        self.add_message(new, 'cursor-cli', 'N1', 'new body', ts=None)

    def test_range_uses_the_session_time_when_ts_is_null(self):
        """A naive `ts < ?` would match nothing and silently skip these sources."""
        where, args = prune.build_filter(before='2026-06-01')
        pv = prune.preview(self.conn, where, args)
        self.assertEqual(pv['messages'], 1, 'the older session must be selected')

        prune.prune(self.conn, where, args)
        remaining = [r['text'] for r in self.conn.execute('SELECT text FROM messages')]
        self.assertEqual(remaining, ['new body'])


class TestPruneUpdatesIndex(DBTestCase):
    """The FTS index is external-content, so the DELETE trigger has to keep up."""

    def setUp(self):
        super().setUp()
        sid = self.add_session('claude-code', 'S1', title='t')
        self.add_message(sid, 'claude-code', 'M1', 'findable phrase here')

    def test_deleted_rows_stop_matching(self):
        rows, _ = search.search(self.conn, 'findable')
        self.assertEqual(len(rows), 1)

        where, args = prune.build_filter(match='findable')
        prune.prune(self.conn, where, args)

        rows, _ = search.search(self.conn, 'findable')
        self.assertEqual(rows, [], 'the FTS index still returns a deleted row')

    def test_integrity_check_passes_after_delete(self):
        where, args = prune.build_filter(match='findable')
        prune.prune(self.conn, where, args)
        self.conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')")


if __name__ == '__main__':
    unittest.main()
