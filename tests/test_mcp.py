"""The MCP search tool: which database answers, and that it says so.

Needs the MCP SDK, so it skips when that is absent (as test_server does).
"""
import os
import unittest

try:
    from chist import mcp_server
    HAVE_SDK = True
except ImportError:                                  # pragma: no cover
    HAVE_SDK = False

from chist import db, searchdb

from .helpers import DBTestCase


@unittest.skipUnless(HAVE_SDK, 'MCP SDK not installed')
class TestSearchTarget(DBTestCase):
    SECRET = 'abcdefghijklmnopqrstuvwxyz012345'

    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.pop(k, None)
                     for k in ('CHIST_SEARCH_DB', 'CHIST_SEARCH_ENGINE')}
        sid = self.add_session('claude-code', 'S1', title='t')
        self.add_message(sid, 'claude-code', 'M1', 'watcher の残骸を数える')
        self.add_message(sid, 'claude-code', 'M2', 'watcher を張り直す')
        self.add_message(sid, 'claude-code', 'M3', 'export API_TOKEN=%s done' % self.SECRET)
        self._saved = mcp_server._conn
        mcp_server._conn = db.connect(self.db_path, readonly=True)

    def tearDown(self):
        mcp_server._conn.close()
        mcp_server._conn = self._saved
        searchdb.close_all()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def _keep_a_search_db(self):
        os.environ['CHIST_SEARCH_DB'] = os.path.join(self._dir.name, 'search.db')

    def test_default_is_the_search_database(self):
        self._keep_a_search_db()
        r = mcp_server.search_history('watcher 残骸')
        self.assertEqual((r['target'], r['count'], r['note']), ('search', 1, ''))

    def test_record_target_uses_the_master_index(self):
        """Same query, other database: the 2-character term is left out there,
        and the note says so."""
        self._keep_a_search_db()
        r = mcp_server.search_history('watcher 残骸', target='record')
        self.assertEqual((r['target'], r['count']), ('record', 2))
        self.assertIn('under 3 characters', r['note'])

    def test_two_character_query_reports_its_mode(self):
        self._keep_a_search_db()
        self.assertEqual(mcp_server.search_history('残骸')['mode'], 'bigram')
        self.assertEqual(mcp_server.search_history('残骸', target='record')['mode'], 'like')

    def test_secret_value_is_findable_only_in_the_record(self):
        self._keep_a_search_db()
        self.assertEqual(mcp_server.search_history(self.SECRET)['count'], 0)
        r = mcp_server.search_history(self.SECRET, target='record')
        self.assertEqual(r['count'], 1)
        self.assertNotIn(self.SECRET, r['hits'][0]['excerpt'], 'still masked on the way out')

    def test_without_a_search_database_the_record_answers(self):
        r = mcp_server.search_history('watcher を張り')
        self.assertEqual((r['target'], r['count'], r['note']), ('record', 1, ''),
                         'not asked for by name: fall back without comment')
        self.assertFalse(os.path.exists(os.path.join(self._dir.name, 'search.db')),
                         'asking must not create one as a side effect')

    def test_asking_for_search_by_name_where_there_is_none_says_so(self):
        r = mcp_server.search_history('watcher を張り', target='search')
        self.assertEqual((r['target'], r['count']), ('record', 1))
        self.assertIn('No search database', r['note'])

    def test_unknown_target_is_refused(self):
        with self.assertRaises(ValueError):
            mcp_server.search_history('watcher', target='both')


class TestResolveTarget(DBTestCase):
    """The one place every front end asks. No SDK needed."""

    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.pop(k, None)
                     for k in ('CHIST_SEARCH_DB', 'CHIST_SEARCH_ENGINE')}

    def tearDown(self):
        searchdb.close_all()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def test_table(self):
        from chist import search
        R = search.resolve_target
        self.assertEqual(R(None), ('record', 'legacy', ''))
        self.assertEqual(R('record'), ('record', 'legacy', ''))
        self.assertEqual(R('search')[:2], ('record', 'legacy'))
        self.assertTrue(R('search')[2])
        os.environ['CHIST_SEARCH_DB'] = os.path.join(self._dir.name, 'search.db')
        self.assertEqual(R(None), ('search', 'split', ''))
        self.assertEqual(R(''), ('search', 'split', ''))
        self.assertEqual(R(' Search '), ('search', 'split', ''))
        self.assertEqual(R('record'), ('record', 'legacy', ''))
        with self.assertRaises(ValueError):
            R('both')

    def test_cli_defaults_to_the_search_database_where_one_is_kept(self):
        import contextlib
        import io
        from chist import cli
        sid = self.add_session('claude-code', 'S1')
        self.add_message(sid, 'claude-code', 'M1', 'watcher の残骸を数える')
        self.add_message(sid, 'claude-code', 'M2', 'watcher を張り直す')

        def run(*extra):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.main(['--db', self.db_path, 'search', 'watcher 残骸'] + list(extra))
            return buf.getvalue()

        self.assertIn('2 results (fts, record)', run())
        os.environ['CHIST_SEARCH_DB'] = os.path.join(self._dir.name, 'search.db')
        self.assertIn('1 results (fts, search)', run())
        self.assertIn('2 results (fts, record)', run('--target', 'record'))


if __name__ == '__main__':
    unittest.main()
