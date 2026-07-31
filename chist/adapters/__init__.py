"""Registry of per-source adapters.

Each adapter provides:
  SOURCE           : identifier matching sources.source
  discover()       : the file paths to scan
  parse(path, off) : (sessions, messages, next offset, errors)
"""
from . import antigravity_cli, claude_code, codex, cursor_cli

ADAPTERS = {
    claude_code.SOURCE: claude_code,
    codex.SOURCE: codex,
    cursor_cli.SOURCE: cursor_cli,
    antigravity_cli.SOURCE: antigravity_cli,
}

__all__ = ['ADAPTERS']
