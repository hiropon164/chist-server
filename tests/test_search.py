"""Search: the trigram floor, masking on output, and session identity."""
import unittest

from chist import search

from .helpers import DBTestCase


class TestTrigramFallback(DBTestCase):
    """FTS5 trigram cannot MATCH under three characters; the fallback must cover it."""

    def setUp(self):
        super().setUp()
        sid = self.add_session('claude-code', 'S1', title='backup', cwd='/tmp/p')
        self.add_message(sid, 'claude-code', 'M1', 'we discussed the backup layout')
        self.add_message(sid, 'claude-code', 'M2', 'チャット履歴を検索する')

    def test_long_query_uses_fts(self):
        rows, mode = search.search(self.conn, 'backup')
        self.assertEqual(mode, 'fts')
        self.assertEqual(len(rows), 1)

    def test_cjk_substring_matches_via_trigram(self):
        """The point of trigram: a substring inside CJK text with no spaces."""
        rows, mode = search.search(self.conn, 'チャット')
        self.assertEqual(mode, 'fts')
        self.assertEqual(len(rows), 1)

    def test_short_query_falls_back_to_like(self):
        rows, mode = search.search(self.conn, '履歴')
        self.assertEqual(mode, 'like')
        self.assertEqual(len(rows), 1, 'a 2-character query must still find its match')

    def test_fts_special_characters_do_not_raise(self):
        """User input goes through the FTS parser; quotes must not become syntax errors."""
        for q in ['"quoted"', 'a AND b', 'x* OR y', 'NEAR(a b)', "it's"]:
            with self.subTest(q=q):
                search.search(self.conn, q)


class TestSearchMasking(DBTestCase):
    """Both the body and the title are masked on the way out."""

    def setUp(self):
        super().setUp()
        secret = 'deploy with TOKEN=abcdef1234567890 please'
        sid = self.add_session('cursor-cli', 'S1', title=secret)
        self.add_message(sid, 'cursor-cli', 'M1', secret)

    def test_body_is_masked(self):
        rows, _ = search.search(self.conn, 'deploy')
        self.assertNotIn('abcdef1234567890', rows[0]['hit'])

    def test_title_is_masked(self):
        """Regression: Cursor and Antigravity derive titles from the first user message,
        so an unmasked title leaked whatever the body was hiding."""
        rows, _ = search.search(self.conn, 'deploy')
        self.assertNotIn('abcdef1234567890', rows[0]['title'])

    def test_sessions_listing_masks_title(self):
        rows = search.sessions(self.conn)
        self.assertNotIn('abcdef1234567890', rows[0]['title'])

    def test_show_masks_body_and_title(self):
        sess, msgs = search.show(self.conn, 'S1')
        self.assertNotIn('abcdef1234567890', sess['title'])
        self.assertNotIn('abcdef1234567890', msgs[0]['text'])

    def test_raw_disables_masking(self):
        """snippet() truncates around the match, so check the full body instead."""
        sess, msgs = search.show(self.conn, 'S1', masked=False)
        self.assertIn('abcdef1234567890', msgs[0]['text'])
        self.assertIn('abcdef1234567890', sess['title'])


class TestSessionIdentity(DBTestCase):
    """ext_id is only unique together with source."""

    def setUp(self):
        super().setUp()
        dup = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        a = self.add_session('cursor-cli', dup, title='from cursor')
        b = self.add_session('claude-code', dup, title='from claude')
        self.add_message(a, 'cursor-cli', 'D1', 'cursor body')
        self.add_message(b, 'claude-code', 'D2', 'claude body')
        self.dup = dup

    def test_both_rows_coexist(self):
        """UNIQUE (source, ext_id) permits the same id under two sources."""
        self.assertEqual(self.count('sessions'), 2)

    def test_source_disambiguates(self):
        for src, body in [('cursor-cli', 'cursor body'), ('claude-code', 'claude body')]:
            with self.subTest(source=src):
                sess, msgs = search.show(self.conn, self.dup, source=src)
                self.assertEqual(sess['source'], src)
                self.assertEqual(msgs[0]['text'], body)

    def test_ambiguity_is_reported(self):
        """Without source the caller still gets an answer, but is told it was ambiguous."""
        sess, _ = search.show(self.conn, self.dup)
        self.assertTrue(sess['ambiguous'])

    def test_unambiguous_id_is_not_flagged(self):
        sid = self.add_session('codex', 'unique-one', title='only one')
        self.add_message(sid, 'codex', 'U1', 'body')
        sess, _ = search.show(self.conn, 'unique-one')
        self.assertFalse(sess['ambiguous'])

    def test_missing_session(self):
        sess, msgs = search.show(self.conn, 'does-not-exist')
        self.assertIsNone(sess)
        self.assertEqual(msgs, [])


class TestFilters(DBTestCase):
    def setUp(self):
        super().setUp()
        a = self.add_session('claude-code', 'A', title='a', cwd='/home/me/app', host='mac')
        b = self.add_session('codex', 'B', title='b', cwd='/srv/thing', host='server')
        self.add_message(a, 'claude-code', 'A1', 'traefik certificate', cwd='/home/me/app',
                         ts='2026-07-01T00:00:00Z')
        self.add_message(b, 'codex', 'B1', 'traefik routing', cwd='/srv/thing',
                         ts='2026-07-20T00:00:00Z')

    def test_filter_by_source(self):
        rows, _ = search.search(self.conn, 'traefik', source=['codex'])
        self.assertEqual([r['source'] for r in rows], ['codex'])

    def test_filter_by_host(self):
        rows, _ = search.search(self.conn, 'traefik', host=['mac'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['session_id'], 'A')

    def test_multiple_hosts_are_or(self):
        rows, _ = search.search(self.conn, 'traefik', host=['mac', 'server'])
        self.assertEqual(len(rows), 2)

    def test_host_and_source_are_and(self):
        rows, _ = search.search(self.conn, 'traefik', host=['mac'], source=['codex'])
        self.assertEqual(rows, [])

    def test_filter_by_cwd_substring(self):
        rows, _ = search.search(self.conn, 'traefik', cwd='/srv')
        self.assertEqual(len(rows), 1)

    def test_filter_by_time_range(self):
        rows, _ = search.search(self.conn, 'traefik', since='2026-07-10')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['session_id'], 'B')

    def test_search_result_carries_session_id(self):
        """Needed so a hit can be opened with get_session."""
        rows, _ = search.search(self.conn, 'traefik')
        for r in rows:
            self.assertIn(r['session_id'], ('A', 'B'))


if __name__ == '__main__':
    unittest.main()
