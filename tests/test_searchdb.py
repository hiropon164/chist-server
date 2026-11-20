"""The search database: a derived copy that must agree with the record."""
import os
import unittest

from chist import cli, prune, search, searchdb

from .helpers import DBTestCase


class SearchDBTestCase(DBTestCase):
    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.pop(k, None)
                     for k in ('CHIST_SEARCH_DB', 'CHIST_SEARCH_ENGINE')}
        self.sconn = searchdb.connect(self.db_path)

    def tearDown(self):
        self.sconn.close()
        searchdb.close_all()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def ids(self, query, **kw):
        rows, mode, dropped = searchdb.search(self.sconn, query, **kw)
        return sorted(r['id'] for r in rows), mode, dropped


class TestSync(SearchDBTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.add_session('claude-code', 'S1', title='backup', cwd='/tmp/p')
        self.add_message(self.sid, 'claude-code', 'M1', 'we discussed the backup layout')
        self.add_message(self.sid, 'claude-code', 'M2', 'チャット履歴を検索する')
        self.add_message(self.sid, 'claude-code', 'M3', 'ls -la backup',
                         role='tool', kind='tool_result')

    def test_copies_only_indexed_kinds(self):
        self.assertEqual(searchdb.sync(self.sconn), 2)
        st = searchdb.check(self.sconn)
        self.assertTrue(st['ok'], st)
        self.assertEqual(st['copy'], 2)

    def test_sync_is_incremental_and_idempotent(self):
        searchdb.sync(self.sconn)
        self.assertEqual(searchdb.sync(self.sconn), 0)
        self.add_message(self.sid, 'claude-code', 'M4', 'a later message')
        self.assertEqual(searchdb.sync(self.sconn), 1)
        self.assertTrue(searchdb.check(self.sconn)['ok'])

    def test_watermark_passes_unindexed_rows(self):
        """A tool_result at the end must still move the watermark, or every
        later sync would re-read it."""
        searchdb.sync(self.sconn)
        top = self.conn.execute('SELECT MAX(id) FROM messages').fetchone()[0]
        self.assertEqual(searchdb.watermark(self.sconn), top)

    def test_small_batches_reach_the_end(self):
        for i in range(7):
            self.add_message(self.sid, 'claude-code', 'B%d' % i, 'batch row %d' % i)
        self.assertEqual(searchdb.sync(self.sconn, batch=2), 9)
        self.assertTrue(searchdb.check(self.sconn)['ok'])

    def test_check_notices_a_copy_that_is_behind(self):
        searchdb.sync(self.sconn)
        self.add_message(self.sid, 'claude-code', 'M4', 'not yet copied')
        st = searchdb.check(self.sconn)
        self.assertFalse(st['ok'])
        self.assertEqual(st['missing'], 1)

    def test_rebuild_restores_a_damaged_copy(self):
        searchdb.sync(self.sconn)
        self.sconn.execute('DELETE FROM docs')
        self.sconn.commit()
        self.assertFalse(searchdb.check(self.sconn)['ok'])
        self.assertEqual(searchdb.rebuild(self.sconn), 2)
        self.assertTrue(searchdb.check(self.sconn)['ok'])
        self.assertEqual(self.ids('backup')[0], self.ids('layout')[0])

    def test_readers_see_the_old_copy_until_a_rebuild_commits(self):
        """A rebuild must not leave a window in which searches come back short."""
        searchdb.sync(self.sconn)
        reader = searchdb.connect(self.db_path)
        try:
            self.sconn.execute('BEGIN')
            self.sconn.execute("INSERT INTO docs_tri (docs_tri) VALUES ('delete-all')")
            self.sconn.execute("INSERT INTO docs_bi (docs_bi) VALUES ('delete-all')")
            self.sconn.execute('DELETE FROM docs')
            # Mid-rebuild, as another connection sees it:
            self.assertEqual(len(searchdb.search(reader, 'backup')[0]), 1)
            self.assertEqual(len(searchdb.search(reader, '履歴')[0]), 1)
            self.sconn.rollback()
        finally:
            reader.close()
        self.assertTrue(searchdb.check(self.sconn)['ok'], 'a rebuild that dies leaves the old copy')

    def test_rebuild_is_one_transaction(self):
        """Control for the test above: if rebuild committed batch by batch, a
        failure part way would leave a partial copy."""
        for i in range(6):
            self.add_message(self.sid, 'claude-code', 'R%d' % i, 'rebuild row %d' % i)
        searchdb.sync(self.sconn)
        real, calls = searchdb._copy_batch, []

        def failing(conn, batch):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('dies part way')
            return real(conn, batch)
        searchdb._copy_batch = failing
        try:
            with self.assertRaises(RuntimeError):
                searchdb.rebuild(self.sconn, batch=3)
        finally:
            searchdb._copy_batch = real
        st = searchdb.check(self.sconn)
        self.assertTrue(st['ok'], st)
        self.assertEqual(st['copy'], 8)

    def test_record_cannot_be_written_through_the_copy(self):
        with self.assertRaises(Exception):
            self.sconn.execute('DELETE FROM hist.messages')

    def test_same_file_is_refused(self):
        with self.assertRaises(ValueError):
            searchdb.connect(self.db_path, self.db_path)


class TestSearch(SearchDBTestCase):
    def setUp(self):
        super().setUp()
        s1 = self.add_session('claude-code', 'S1', title='backup', cwd='/tmp/p', host='desk')
        s2 = self.add_session('codex', 'S2', title='other', cwd='/tmp/q', host='laptop')
        self.add_message(s1, 'claude-code', 'M1', 'we discussed the backup layout',
                         ts='2026-08-01T00:00:00Z', cwd='/tmp/p')
        self.add_message(s1, 'claude-code', 'M2', 'チャット履歴を検索する',
                         ts='2026-08-02T00:00:00Z', role='assistant')
        self.add_message(s2, 'codex', 'M3', 'watcher の残骸を数える', ts='2026-08-03T00:00:00Z')
        self.add_message(s2, 'codex', 'M4', 'watcher を張り直す', ts='2026-08-04T00:00:00Z')
        self.add_message(s2, 'codex', 'M5', 'qs is short for quickshell',
                         ts='2026-08-05T00:00:00Z')
        self.add_message(s2, 'codex', 'M6', 'sqs and qsub are different things',
                         ts='2026-08-06T00:00:00Z')
        searchdb.sync(self.sconn)

    def test_long_term_uses_trigram(self):
        ids, mode, dropped = self.ids('backup')
        self.assertEqual((len(ids), mode, dropped), (1, 'fts', []))

    def test_two_character_cjk_term_uses_the_bigram_index(self):
        ids, mode, dropped = self.ids('履歴')
        self.assertEqual((len(ids), mode, dropped), (1, 'bigram', []))

    def test_two_character_term_is_not_dropped_beside_a_long_one(self):
        """The case the old index cannot do: `watcher 残骸` used to mean just
        `watcher`, returning both rows."""
        ids, mode, dropped = self.ids('watcher 残骸')
        self.assertEqual((len(ids), mode, dropped), (1, 'fts', []))

    def test_bigram_is_a_substring_match_and_case_insensitive(self):
        ids, mode, _ = self.ids('QS')
        self.assertEqual(mode, 'bigram')
        self.assertEqual(len(ids), 2, 'qs matches inside sqs/qsub too -- it is a substring index')

    def test_two_bigram_terms_are_anded(self):
        self.assertEqual(len(self.ids('残骸 数え')[0]), 1)
        self.assertEqual(len(self.ids('残骸 張り')[0]), 0)

    def test_bigram_excerpt_marks_the_term(self):
        rows, _, _ = searchdb.search(self.sconn, '履歴')
        self.assertIn('\x02履歴\x03', rows[0]['hit'])

    def test_single_character_falls_back_to_like(self):
        ids, mode, dropped = self.ids('数')
        self.assertEqual((len(ids), mode, dropped), (1, 'like', []))

    def test_single_character_is_dropped_beside_others(self):
        ids, mode, dropped = self.ids('watcher 数')
        self.assertEqual((len(ids), mode, dropped), (2, 'fts', ['数']))

    def test_filters(self):
        self.assertEqual(len(self.ids('watcher', host=['laptop'])[0]), 2)
        self.assertEqual(len(self.ids('watcher', host=['desk'])[0]), 0)
        self.assertEqual(len(self.ids('watcher', source=['claude-code'])[0]), 0)
        self.assertEqual(len(self.ids('watcher', since='2026-08-04')[0]), 1)
        self.assertEqual(len(self.ids('チャット', role='assistant')[0]), 1)
        self.assertEqual(len(self.ids('チャット', role='user')[0]), 0)
        self.assertEqual(len(self.ids('backup', cwd='/tmp/p')[0]), 1)
        self.assertEqual(len(self.ids('backup', cwd='/tmp/q')[0]), 0)

    def test_special_characters_do_not_raise(self):
        for q in ['"quoted"', 'a AND b', 'x* OR y', 'NEAR(a b)', "it's", '""', '"a', 'a"',
                  '( )', '% _', '日 本']:
            with self.subTest(q=q):
                searchdb.search(self.sconn, q)

    def test_title_is_read_from_the_record_not_copied(self):
        """A title that arrives after the copy was made must still show."""
        self.conn.execute("UPDATE sessions SET title = 'renamed' WHERE ext_id = 'S1'")
        self.conn.commit()
        rows, _, _ = searchdb.search(self.sconn, 'backup')
        self.assertEqual(rows[0]['title'], 'renamed')

    def test_agrees_with_the_old_index_on_long_terms(self):
        for q in ['backup', 'チャット', 'watcher', 'quickshell', 'nothing-here']:
            with self.subTest(q=q):
                old, _, _ = search.search(self.conn, q, limit=100, engine='legacy')
                self.assertEqual(sorted(r['id'] for r in old), self.ids(q, limit=100)[0])



class TestMasking(SearchDBTestCase):
    """The copy is masked when it is made, not only when it is shown."""

    SECRET = 'abcdefghijklmnopqrstuvwxyz012345'

    def setUp(self):
        super().setUp()
        sid = self.add_session('codex', 'S9')
        self.add_message(sid, 'codex', 'K1', 'export API_TOKEN=%s then run' % self.SECRET)
        self.add_message(sid, 'codex', 'K2', 'nothing secret in this one')
        searchdb.sync(self.sconn)

    def _file_bytes(self):
        # main only: a bare checkpoint also tries the attached, read-only record.
        self.sconn.execute('PRAGMA main.wal_checkpoint(TRUNCATE)')
        with open(os.path.join(self._dir.name, 'search.db'), 'rb') as f:
            return f.read()

    def test_control_the_record_holds_the_secret(self):
        """Without this the tests below could pass on a fixture with no secret."""
        n = self.conn.execute('SELECT COUNT(*) FROM messages WHERE instr(text, ?) > 0',
                              (self.SECRET,)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_stored_text_is_masked(self):
        n = self.sconn.execute('SELECT COUNT(*) FROM docs WHERE instr(text, ?) > 0',
                               (self.SECRET,)).fetchone()[0]
        self.assertEqual(n, 0)
        self.assertNotIn(self.SECRET.encode(), self._file_bytes(),
                         'not in docs, and not left in either index')

    def test_secret_is_not_searchable_but_its_context_is(self):
        self.assertEqual(len(self.ids(self.SECRET)[0]), 0)
        self.assertEqual(len(self.ids('abcdefgh')[0]), 0)
        rows, _, _ = searchdb.search(self.sconn, 'API_TOKEN')
        self.assertEqual(len(rows), 1)
        self.assertNotIn(self.SECRET, rows[0]['hit'])

    def test_check_passes_and_reports_the_unmasked_record_row(self):
        st = searchdb.check(self.sconn)
        self.assertTrue(st['ok'], st)
        self.assertEqual(st['record_unmasked'], 1)

    def test_a_rule_added_later_shows_in_check_and_rebuild_applies_it(self):
        """The reason masking sits here: the record cannot follow a new rule,
        the copy can."""
        import re
        from chist import mask as mask_mod
        self.assertEqual(len(self.ids('nothing secret')[0]), 1)
        mask_mod._PATTERNS.append((re.compile(r'nothing secret'), '***'))
        try:
            st = searchdb.check(self.sconn)
            self.assertFalse(st['ok'])
            self.assertEqual(st['text_differs'], 1)
            searchdb.rebuild(self.sconn)
            self.assertTrue(searchdb.check(self.sconn)['ok'])
            self.assertEqual(len(self.ids('nothing secret')[0]), 0)
        finally:
            mask_mod._PATTERNS.pop()

    def test_fingerprint_follows_the_rules(self):
        import re
        from chist import mask as mask_mod
        before = mask_mod.fingerprint()
        mask_mod._PATTERNS.append((re.compile(r'zzz'), '***'))
        try:
            self.assertNotEqual(mask_mod.fingerprint(), before)
        finally:
            mask_mod._PATTERNS.pop()
        self.assertEqual(mask_mod.fingerprint(), before)

    def test_ensure_current_rebuilds_only_after_a_rule_change(self):
        import re
        from chist import mask as mask_mod
        sp = os.path.join(self._dir.name, 'search.db')
        self.assertFalse(searchdb.stale(self.sconn))
        self.assertEqual(searchdb.ensure_current(self.db_path, sp), 'current')
        mask_mod._PATTERNS.append((re.compile(r'nothing secret'), '***'))
        try:
            self.assertTrue(searchdb.stale(self.sconn))
            logged = []
            self.assertEqual(searchdb.ensure_current(self.db_path, sp, log=logged.append),
                             'rebuilt')
            self.assertEqual(len(logged), 2)
            self.assertFalse(searchdb.stale(self.sconn))
            self.assertTrue(searchdb.check(self.sconn)['ok'])
            self.assertEqual(len(self.ids('nothing secret')[0]), 0)
            self.assertEqual(searchdb.ensure_current(self.db_path, sp), 'current')
        finally:
            mask_mod._PATTERNS.pop()

    def test_copy_without_a_fingerprint_counts_as_stale(self):
        self.sconn.execute("DELETE FROM meta WHERE k = 'mask_rules'")
        self.sconn.commit()
        self.assertTrue(searchdb.stale(self.sconn))
        self.assertFalse(searchdb.check(self.sconn)['mask_rules_current'])

    def test_ensure_current_never_raises(self):
        logged = []
        bad = os.path.join(self.db_path, 'sub', 'search.db')
        self.assertEqual(searchdb.ensure_current(self.db_path, bad, log=logged.append),
                         'failed')
        self.assertEqual(len(logged), 1)

    def test_masked_row_can_still_be_removed(self):
        """Deleting from the indexes needs the text exactly as it was indexed --
        the masked form, not the record's."""
        where, args = prune.build_filter(match='API_TOKEN')
        prune.prune(self.conn, where, args)
        self.assertEqual(searchdb.reconcile(self.sconn), 1)
        self.assertEqual(len(self.ids('API_TOKEN')[0]), 0)
        self.assertEqual(len(self.ids('export')[0]), 0)
        self.assertTrue(searchdb.check(self.sconn)['ok'])


class TestDeletion(SearchDBTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.add_session('claude-code', 'S1')
        self.add_message(self.sid, 'claude-code', 'M1', 'keep this backup note',
                         ts='2026-08-01T00:00:00Z')
        self.add_message(self.sid, 'claude-code', 'M2', 'drop this backup 残骸',
                         ts='2026-08-09T00:00:00Z')
        searchdb.sync(self.sconn)

    def _prune_newest(self):
        where, args = prune.build_filter(after='2026-08-05')
        prune.prune(self.conn, where, args)

    def test_reconcile_removes_deleted_rows_from_both_indexes(self):
        self._prune_newest()
        self.assertFalse(searchdb.check(self.sconn)['ok'])
        self.assertEqual(searchdb.reconcile(self.sconn), 1)
        self.assertTrue(searchdb.check(self.sconn)['ok'])
        self.assertEqual(len(self.ids('backup')[0]), 1)
        self.assertEqual(len(self.ids('残骸')[0]), 0)

    def test_reused_id_is_indexed_after_reconcile(self):
        """SQLite hands the deleted top id to the next insert. Without pulling the
        watermark back, sync would skip that row for good."""
        self._prune_newest()
        searchdb.reconcile(self.sconn)
        self.add_message(self.sid, 'claude-code', 'M3', 'a new row with a reused id')
        self.assertEqual(searchdb.sync(self.sconn), 1)
        self.assertEqual(len(self.ids('reused')[0]), 1)
        self.assertEqual(len(self.ids('残骸')[0]), 0, 'the old text must not answer for the id')
        self.assertTrue(searchdb.check(self.sconn)['ok'])

    def test_control_without_reconcile_the_reused_id_is_missed(self):
        """The control for the test above: this is the failure reconcile prevents."""
        self._prune_newest()
        self.add_message(self.sid, 'claude-code', 'M3', 'a new row with a reused id')
        self.assertEqual(searchdb.sync(self.sconn), 0)
        st = searchdb.check(self.sconn)
        self.assertFalse(st['ok'])
        self.assertEqual(st['text_differs'], 1)


class TestWiring(SearchDBTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.add_session('claude-code', 'S1')
        self.add_message(self.sid, 'claude-code', 'M1', 'watcher の残骸')
        self.add_message(self.sid, 'claude-code', 'M2', 'watcher を張る')

    def test_default_engine_is_legacy_and_keeps_the_old_floor(self):
        rows, mode, dropped = search.search(self.conn, 'watcher 残骸')
        self.assertEqual((len(rows), mode, dropped), (2, 'fts', ['残骸']))
        self.assertEqual(search.floor(), 3)
        self.assertFalse(searchdb.enabled())

    def test_after_write_does_nothing_unless_enabled(self):
        self.assertIsNone(searchdb.after_write(self.conn))
        self.assertEqual(searchdb.check(self.sconn)['copy'], 0)

    def test_after_write_syncs_when_a_path_is_configured(self):
        os.environ['CHIST_SEARCH_DB'] = os.path.join(self._dir.name, 'search.db')
        self.assertEqual(searchdb.after_write(self.conn), 2)
        self.assertEqual(search.search(self.conn, 'watcher 残骸')[2], ['残骸'],
                         'keeping the copy must not change which index answers')

    def test_split_engine_answers_from_the_copy_and_catches_up_by_itself(self):
        os.environ['CHIST_SEARCH_ENGINE'] = 'split'
        rows, mode, dropped = search.search(self.conn, 'watcher 残骸')
        self.assertEqual((len(rows), mode, dropped), (1, 'fts', []))
        self.assertEqual(search.floor(), 2)

    def test_split_search_answers_while_another_writer_holds_the_lock(self):
        """A rebuild holds the write lock for minutes. Searches must not fail
        for that -- they need no lock."""
        os.environ['CHIST_SEARCH_ENGINE'] = 'split'
        search.search(self.conn, 'watcher')                     # builds the copy
        self.add_message(self.sid, 'claude-code', 'M3', 'watcher が増えた')
        self.sconn.execute('BEGIN IMMEDIATE')
        try:
            rows, mode, _ = search.search(self.conn, 'watcher')
            self.assertEqual((len(rows), mode), (2, 'fts'), 'the copy as it stood')
        finally:
            self.sconn.rollback()
        self.assertEqual(len(search.search(self.conn, 'watcher')[0]), 3, 'and it catches up after')

    def test_after_write_never_raises(self):
        # A path below a regular file: the directory cannot be created.
        os.environ['CHIST_SEARCH_DB'] = os.path.join(self.db_path, 'sub', 'search.db')
        logged = []
        self.assertIsNone(searchdb.after_write(self.conn, log=logged.append))
        self.assertEqual(len(logged), 1)

    def test_reindex_command(self):
        sp = os.path.join(self._dir.name, 'search.db')
        self.assertEqual(cli.main(['--db', self.db_path, 'reindex', '--check']), 1,
                         'an empty copy is not in step with a record that has rows')
        self.assertEqual(cli.main(['--db', self.db_path, 'reindex']), 0)
        self.assertEqual(cli.main(['--db', self.db_path, 'reindex', '--check']), 0)
        self.assertEqual(cli.main(['--db', self.db_path, 'reindex', '--rebuild']), 0)
        self.assertEqual(cli.main(['--db', self.db_path, 'reindex', '--compare', 'watcher']), 0)
        self.assertTrue(os.path.exists(sp))


if __name__ == '__main__':
    unittest.main()
