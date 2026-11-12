"""Server-side guards: authentication, the body cap, and malformed payloads.

These need the MCP SDK (via chist.server), so they skip when it is absent. Ingest,
search and the CLI stay dependency-free, and their tests never import this module.
"""
import json
import unittest

try:
    from chist import server
    HAVE_SDK = True
except ImportError:                                  # pragma: no cover
    HAVE_SDK = False

from .helpers import DBTestCase


class FakeRequest:
    """Just enough of a Starlette request for the auth and ingest paths."""

    def __init__(self, headers=None, body=b'', params=None):
        self.headers = headers or {}
        self._body = body
        self.query_params = _Params(params or {})

    async def body(self):
        return self._body


class _Params(dict):
    def getlist(self, key):
        v = self.get(key)
        if v is None:
            return []
        return v if isinstance(v, list) else [v]


@unittest.skipUnless(HAVE_SDK, 'MCP SDK not installed')
class TestAuthorization(unittest.TestCase):
    def setUp(self):
        self._saved = server.TOKEN
        server.TOKEN = 'correct-horse-battery-staple'

    def tearDown(self):
        server.TOKEN = self._saved

    def test_valid_token(self):
        r = FakeRequest({'authorization': 'Bearer correct-horse-battery-staple'})
        self.assertTrue(server._authorized(r))

    def test_wrong_token(self):
        self.assertFalse(server._authorized(FakeRequest({'authorization': 'Bearer nope'})))

    def test_missing_header(self):
        self.assertFalse(server._authorized(FakeRequest()))

    def test_wrong_scheme(self):
        r = FakeRequest({'authorization': 'Basic correct-horse-battery-staple'})
        self.assertFalse(server._authorized(r))

    def test_token_as_prefix_is_rejected(self):
        r = FakeRequest({'authorization': 'Bearer correct-horse'})
        self.assertFalse(server._authorized(r))

    def test_unset_token_denies_everything(self):
        """There is no 'no token configured, so allow' escape hatch."""
        server.TOKEN = None
        self.assertFalse(server._authorized(FakeRequest({'authorization': 'Bearer x'})))
        self.assertFalse(server._authorized(FakeRequest()))


@unittest.skipUnless(HAVE_SDK, 'MCP SDK not installed')
class TestPayloadHandling(unittest.TestCase):
    def test_items_accepts_a_list_of_objects(self):
        payload = {'sessions': [{'a': 1}, {'b': 2}]}
        self.assertEqual(len(server._items(payload, 'sessions')), 2)

    def test_items_rejects_non_list(self):
        for bad in ['a string', 42, {'not': 'a list'}, None]:
            with self.subTest(bad=bad):
                self.assertEqual(server._items({'sessions': bad}, 'sessions'), [])

    def test_items_drops_non_objects_inside_the_list(self):
        payload = {'messages': [{'ok': 1}, None, 'string', 7]}
        self.assertEqual(server._items(payload, 'messages'), [{'ok': 1}])

    def test_missing_key(self):
        self.assertEqual(server._items({}, 'sessions'), [])

    def test_body_cap_is_64_mib(self):
        self.assertEqual(server.MAX_BODY, 64 * 1024 * 1024)


@unittest.skipUnless(HAVE_SDK, 'MCP SDK not installed')
class TestIngestMasksBeforeStoring(DBTestCase):
    """The sender masks too, but --raw bypasses that, so the server masks again."""

    def setUp(self):
        super().setUp()
        self._saved_conn, self._saved_token = server._conn, server.TOKEN
        server._conn = self.conn
        server.TOKEN = 'tok'

    def tearDown(self):
        server._conn, server.TOKEN = self._saved_conn, self._saved_token
        super().tearDown()

    def _ingest(self, payload):
        import asyncio
        req = FakeRequest({'authorization': 'Bearer tok'},
                          body=json.dumps(payload).encode())
        return asyncio.run(server.ingest(req))

    def test_body_and_title_are_masked_on_arrival(self):
        secret = 'deploy with TOKEN=abcdef1234567890'
        self._ingest({
            'host': {'host': 'mac', 'os': 'darwin'},
            'sessions': [{'source': 'cursor-cli', 'ext_id': 'S1', 'title': secret}],
            'messages': [{'source': 'cursor-cli', 'session_ext_id': 'S1',
                          'ext_id': 'M1', 'role': 'user', 'kind': 'message',
                          'text': secret}],
        })
        title = self.conn.execute(
            "SELECT title FROM sessions WHERE ext_id='S1'").fetchone()['title']
        text = self.conn.execute(
            "SELECT text FROM messages WHERE ext_id='M1'").fetchone()['text']
        self.assertNotIn('abcdef1234567890', title)
        self.assertNotIn('abcdef1234567890', text)

    def test_rows_without_identifiers_are_skipped(self):
        res = self._ingest({
            'sessions': [{'source': 'codex'}],                    # no ext_id
            'messages': [{'session_ext_id': 'S1', 'text': 'x'}],  # no source/ext_id
        })
        self.assertEqual(self.count('sessions'), 0)
        self.assertEqual(self.count('messages'), 0)
        self.assertEqual(json.loads(res.body)['skipped'], 1)


if __name__ == '__main__':
    unittest.main()
