"""出力時のシークレットマスキング。

履歴には認証情報が混入しうる。DB には原文のまま保存し、
表示・MCP 応答の直前でマスクする。
"""
import re

_PATTERNS = [
    # 具体的なプレフィックスを持つもの
    (re.compile(r'\bsk-[A-Za-z0-9_-]{16,}'), 'sk-***'),
    (re.compile(r'\b(gh[pousr]_[A-Za-z0-9]{16,})'), 'ghX_***'),
    (re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}'), 'github_pat_***'),
    (re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}'), 'xox*-***'),
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), 'AKIA***'),
    (re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b'), 'AIza***'),
    (re.compile(r'\bya29\.[0-9A-Za-z_-]{20,}'), 'ya29.***'),
    (re.compile(r'\bglpat-[A-Za-z0-9_-]{16,}'), 'glpat-***'),
    (re.compile(r'\bnpm_[A-Za-z0-9]{30,}'), 'npm_***'),
    # ヘッダ・環境変数の代入形
    (re.compile(r'(?i)\b(authorization\s*:\s*bearer)\s+\S+'), r'\1 ***'),
    (re.compile(r'(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|APIKEY|API_KEY))\s*[=:]\s*'
                r'["\']?([^\s"\'&]{8,})'), r'\1=***'),
    # 「パスワード: xxx」「password = xxx」など、ラベル付きで書かれた値。
    # 全角コロンと日本語ラベルも拾う（英語の代入形だけでは実運用で漏れる）。
    (re.compile(r'(?i)(パスワード|パスフレーズ|password|passwd|passphrase)'
                r'\s*[:：=]\s*["\'`]?([^\s"\'`、。，]{6,})'), r'\1: ***'),
    # 資格情報をコマンド引数で渡す形。裸のランダム文字列は代入形として拾えない。
    #   curl -u user:pass / --user user:pass
    (re.compile(r'(?i)(--user[= ]|\s-u\s+)([^\s:"\']{1,64}):([^\s"\']{4,})'), r'\1\2:***'),
    #   htpasswd -nbB user pass
    (re.compile(r'(?i)(htpasswd(?:\s+-\S+)*\s+\S+\s+)(["\']?)([^\s"\']{4,})\2'), r'\1***'),
    # 秘密鍵ブロック
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----',
                re.S), '-----BEGIN PRIVATE KEY----- *** -----END PRIVATE KEY-----'),
    (re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'), 'eyJ***.***'),
]


def mask(text, enabled=True):
    """検出したシークレットを伏せ字にして返す。"""
    if not enabled or not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text
