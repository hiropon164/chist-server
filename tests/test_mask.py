"""Masking: what must be redacted, and what must be left alone.

The false-positive half matters as much as the detection half. A rule that also eats
ordinary prose makes the history unreadable, which is how masking gets switched off.
"""
import unittest

from chist.mask import mask


class TestMaskDetects(unittest.TestCase):
    """Strings that must never survive masking."""

    def assertMasked(self, text, leak):
        out = mask(text)
        self.assertNotIn(leak, out, 'secret survived: %r -> %r' % (text, out))

    def test_known_prefixes(self):
        for text, leak in [
            ('key is sk-abcdefghijklmnopqrstuvwxyz012345', 'sk-abcdefghijkl'),
            ('token ghp_abcdefghijklmnopqrstuvwxyz01', 'ghp_abcdefghijkl'),
            ('github_pat_11ABCDEFG0abcdefghijklmnop', 'github_pat_11ABCDEFG0'),
            ('slack xoxb-1234567890-abcdefghij', 'xoxb-1234567890'),
            ('aws AKIAIOSFODNN7EXAMPLE here', 'AKIAIOSFODNN7EXAMPLE'),
            ('google AIzaSyDabcdefghijklmnopqrstuvwxyz012345 x', 'AIzaSyDabcdefghijkl'),
            ('gitlab glpat-abcdefghijklmnopqrst', 'glpat-abcdefghijkl'),
        ]:
            with self.subTest(text=text):
                self.assertMasked(text, leak)

    def test_authorization_header(self):
        self.assertMasked('Authorization: Bearer a1b2c3d4e5f6a7b8c9d0e1f2',
                          'a1b2c3d4e5f6a7b8c9d0e1f2')

    def test_key_suffixed_names(self):
        """`SECRET_KEY=` went through because the rule required the name to *end*
        with the keyword, so a name such as `…__SECRET_KEY` was not recognised."""
        for s in (
            'FORGEJO__security__SECRET_KEY=a1b2c3d4e5f6a7b8c9d0e1f2',
            'SECRET_KEY=a1b2c3d4e5f6a7b8c9d0e1f2',
            'MY_TOKEN_VALUE=a1b2c3d4e5f6a7b8c9d0e1f2',
        ):
            self.assertMasked(s, 'a1b2c3d4e5f6a7b8c9d0e1f2')

    def test_plain_key_names_are_left_alone(self):
        """The control. Widening the name match must not swallow names that carry
        no secret keyword -- masking those costs searchability for nothing."""
        for s in (
            'PUBLIC_KEY=a1b2c3d4e5f6a7b8c9d0e1f2',
            'SSH_KEY_PATH=/home/you/.ssh/id_ed25519',
        ):
            self.assertEqual(mask(s), s)

    def test_json_shapes(self):
        """The shape a config file writes, which is the shape this fleet pastes.

        Every assignment rule here was written against a shell: `Authorization:
        Bearer x`, `TOKEN=x`. In JSON the name is quoted, so a `"` sits between the
        name and the colon and `authorization\\s*:` never reaches it. The one form
        the machines are actually configured in -- `.claude.json`, `settings.json` --
        was the one form that went through unmasked.

        A tool that fails on a config file can print the whole file as its error
        (PowerShell's `ConvertFrom-Json` does), and the text then sits in a
        transcript. Masking has to catch that shape on the way into the database,
        which is the half that does not scroll away.
        """
        for text, leak in [
            ('"Authorization": "Bearer a1b2c3d4e5f6a7b8c9d0e1f2"',
             'a1b2c3d4e5f6a7b8c9d0e1f2'),
            ('"token": "a1b2c3d4e5f6a7b8c9d0e1f2"', 'a1b2c3d4e5f6a7b8c9d0e1f2'),
            ('"CHIST_TOKEN": "a1b2c3d4e5f6a7b8c9d0e1f2"', 'a1b2c3d4e5f6a7b8c9d0e1f2'),
            ('"password": "a1b2c3d4e5f6a7b8c9d0e1f2"', 'a1b2c3d4e5f6a7b8c9d0e1f2'),
            # Indented and comma-terminated, the way it actually appears in a file
            ('    "Authorization": "Bearer a1b2c3d4e5f6a7b8c9d0e1f2",',
             'a1b2c3d4e5f6a7b8c9d0e1f2'),
        ]:
            with self.subTest(text=text):
                self.assertMasked(text, leak)

    def test_jwt(self):
        self.assertMasked(
            'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w',
            'dozjgNryP4J3jVmNHl0w')

    def test_pem_private_key(self):
        text = ('-----BEGIN RSA PRIVATE KEY-----\n'
                'MIIEowIBAAKCAQEAsecretkeymaterialhere\n'
                '-----END RSA PRIVATE KEY-----')
        self.assertMasked(text, 'MIIEowIBAAKCAQEAsecretkeymaterialhere')

    def test_uppercase_assignment(self):
        for text, leak in [
            ('CHIST_TOKEN=a1b2c3d4e5f6a7b8c9d0e1f2', 'a1b2c3d4e5f6a7b8c9d0e1f2'),
            ('DB_PASSWORD="hunter2xyz789"', 'hunter2xyz789'),
            ('API_KEY: abcdefghijklmnop', 'abcdefghijklmnop'),
        ]:
            with self.subTest(text=text):
                self.assertMasked(text, leak)

    def test_japanese_labels(self):
        """Regression: these passed through untouched before the label rule grew."""
        for text, leak in [
            ('トークン: a1b2c3d4e5f6a7b8c9d0e1f2', 'a1b2c3d4e5f6a7b8c9d0e1f2'),
            ('トークン：a1b2c3d4e5f6a7b8c9d0e1f2 です', 'a1b2c3d4e5f6a7b8c9d0e1f2'),
            ('シークレット: hunter2xyz789', 'hunter2xyz789'),
            ('秘密鍵: MIIEvQIBADANBgkqhkiG9w0', 'MIIEvQIBADANBgkqhkiG9w0'),
            ('APIキー: sk_live_abcdefghijk', 'sk_live_abcdefghijk'),
            ('API キー = abcdefghijklmnop', 'abcdefghijklmnop'),
            ('アクセスキー: AKIA1234567890ABCDEF', 'AKIA1234567890ABCDEF'),
            ('認証情報: admin_p4ssw0rd123', 'admin_p4ssw0rd123'),
            ('パスコード: 987654321012', '987654321012'),
            ('パスワード: Xy9pQr3sTu7vWx1z', 'Xy9pQr3sTu7vWx1z'),
        ]:
            with self.subTest(text=text):
                self.assertMasked(text, leak)

    def test_credential_bearing_commands(self):
        """A bare random string has no assignment form, so the command shape is matched."""
        for text, leak in [
            ('curl -s -u alice:Kj4mNb8vCx2zAs6d https://example.com', 'Kj4mNb8vCx2zAs6d'),
            ('curl --user alice:Kj4mNb8vCx2zAs6d https://example.com', 'Kj4mNb8vCx2zAs6d'),
            ("htpasswd -nbB alice 'Kj4mNb8vCx2zAs6d'", 'Kj4mNb8vCx2zAs6d'),
        ]:
            with self.subTest(text=text):
                self.assertMasked(text, leak)


class TestMaskLeavesAlone(unittest.TestCase):
    """Ordinary prose that must come back unchanged."""

    def assertUnchanged(self, text):
        self.assertEqual(mask(text), text, 'false positive on %r' % text)

    def test_labels_without_a_value(self):
        for text in [
            'トークンを再生成してください',
            'シークレットは環境変数で渡します',
            '秘密鍵の管理方法について',
            'APIキーはどこに置くべきですか',
            'token の有効期限は 30 日',
            'access key rotation policy',
            'Reset the password if you forgot it',
        ]:
            with self.subTest(text=text):
                self.assertUnchanged(text)

    def test_short_values_are_not_secrets(self):
        """Under six characters is placeholder text, not a credential."""
        for text in ['パスワード: 未設定', 'トークン: なし', 'password: none']:
            with self.subTest(text=text):
                self.assertUnchanged(text)

    def test_ordinary_commands(self):
        for text in [
            'curl -s https://example.com/api',
            'git -u origin main',
            'docker compose up -d --build',
        ]:
            with self.subTest(text=text):
                self.assertUnchanged(text)

    def test_disabled_and_empty(self):
        self.assertEqual(mask('CHIST_TOKEN=abcdefghijkl', enabled=False),
                         'CHIST_TOKEN=abcdefghijkl')
        self.assertEqual(mask(''), '')
        self.assertIsNone(mask(None))


if __name__ == '__main__':
    unittest.main()
