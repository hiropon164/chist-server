"""ソース別アダプタのレジストリ。

各アダプタは以下を提供する。
  SOURCE           : sources.source と一致する識別子
  discover()       : 走査対象のファイルパス列
  parse(path, off) : (Sess列, Msg列, 次オフセット, エラー列)
"""
from . import antigravity_cli, claude_code, codex, cursor_cli

ADAPTERS = {
    claude_code.SOURCE: claude_code,
    codex.SOURCE: codex,
    cursor_cli.SOURCE: cursor_cli,
    antigravity_cli.SOURCE: antigravity_cli,
}

__all__ = ['ADAPTERS']
