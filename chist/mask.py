"""Secret masking applied on output.

History can contain credentials. The local database keeps the text verbatim and
masking happens just before display or an MCP response. The aggregation server
additionally masks before storing.
"""
import re

_PATTERNS = [
    # Credentials with a recognisable prefix
    (re.compile(r'\bsk-[A-Za-z0-9_-]{16,}'), 'sk-***'),
    (re.compile(r'\b(gh[pousr]_[A-Za-z0-9]{16,})'), 'ghX_***'),
    (re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}'), 'github_pat_***'),
    (re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}'), 'xox*-***'),
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), 'AKIA***'),
    (re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b'), 'AIza***'),
    (re.compile(r'\bya29\.[0-9A-Za-z_-]{20,}'), 'ya29.***'),
    (re.compile(r'\bglpat-[A-Za-z0-9_-]{16,}'), 'glpat-***'),
    (re.compile(r'\bnpm_[A-Za-z0-9]{30,}'), 'npm_***'),
    # Header and environment-variable assignment forms
    (re.compile(r'(?i)\b(authorization\s*:\s*bearer)\s+\S+'), r'\1 ***'),
    (re.compile(r'(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|APIKEY|API_KEY))\s*[=:]\s*'
                r'["\']?([^\s"\'&]{8,})'), r'\1=***'),
    # Values written with a label, e.g. "password: xxx" or "password = xxx".
    # Japanese labels and the full-width colon are included: in practice the English
    # assignment form alone lets real secrets through. The English words here overlap
    # with the assignment rule above, but that one only fires on an all-caps name.
    (re.compile(r'(?i)(パスワード|パスフレーズ|パスコード|トークン|シークレット|'
                r'秘密鍵|認証情報|API\s*キー|アクセスキー|'
                r'password|passwd|passphrase|token|secret|api\s*key|access\s*key)'
                r'\s*[:：=]\s*["\'`]?([^\s"\'`、。，]{6,})'), r'\1: ***'),
    # Credentials passed as command arguments. A bare random string has no assignment
    # form to key on, so these command shapes are matched explicitly.
    #   curl -u user:pass / --user user:pass
    (re.compile(r'(?i)(--user[= ]|\s-u\s+)([^\s:"\']{1,64}):([^\s"\']{4,})'), r'\1\2:***'),
    #   htpasswd -nbB user pass
    (re.compile(r'(?i)(htpasswd(?:\s+-\S+)*\s+\S+\s+)(["\']?)([^\s"\']{4,})\2'), r'\1***'),
    # Private key blocks
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----',
                re.S), '-----BEGIN PRIVATE KEY----- *** -----END PRIVATE KEY-----'),
    (re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'), 'eyJ***.***'),
]


def mask(text, enabled=True):
    """Return the text with any detected secrets replaced by placeholders."""
    if not enabled or not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text
