"""アダプタ共通の型と JSONL の差分読み取り。"""
import dataclasses
import hashlib
import json
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple

HEAD_BYTES = 4096


@dataclasses.dataclass
class Msg:
    ext_id: str
    text: str
    role: str = 'assistant'
    kind: str = 'message'
    part: int = 0
    seq: Optional[int] = None
    ts: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    src_pos: Optional[int] = None
    session_ext_id: Optional[str] = None


@dataclasses.dataclass
class Sess:
    ext_id: str
    title: Optional[str] = None
    cwd: Optional[str] = None
    git_branch: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


def head_sha(path: str) -> str:
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read(HEAD_BYTES)).hexdigest()


def read_jsonl(path: str, start: int = 0) -> Tuple[List[Tuple[int, Dict[str, Any]]], int, List[str]]:
    """start バイト目以降の JSONL を読む。

    返り値は (レコード列, 次に読むべきオフセット, エラー説明).
    レコード列の各要素は (その行の先頭オフセット, dict)。

    進行中のセッションでは最終行が途中まで書かれていることがある。
    その場合はその行を捨て、オフセットを「最後に成功した行の終端」に戻す。
    """
    out: List[Tuple[int, Dict[str, Any]]] = []
    errors: List[str] = []
    with open(path, 'rb') as f:
        f.seek(start)
        pos = start
        good = start
        for raw in f:
            line_start = pos
            pos += len(raw)
            if not raw.endswith(b'\n'):
                # 行が閉じていない = 書き込み途中。次回この位置から読み直す
                break
            s = raw.strip()
            if not s:
                good = pos
                continue
            try:
                out.append((line_start, json.loads(s)))
            except (ValueError, UnicodeDecodeError) as e:
                errors.append('%s@%d: %s' % (os.path.basename(path), line_start, e))
            good = pos
    return out, good, errors


def provider_of(model: Optional[str], source: str) -> str:
    """モデル名から provider を判定する。判定できなければソース既定へ。"""
    m = (model or '').lower()
    if m.startswith(('claude', 'sonnet', 'opus', 'haiku', 'fable')):
        return 'anthropic'
    if m.startswith(('gpt', 'o1', 'o3', 'o4', 'codex')):
        return 'openai'
    if m.startswith('gemini'):
        return 'google'
    if m.startswith('composer'):
        return 'cursor'
    return {
        'claude-code': 'anthropic',
        'codex': 'openai',
        'antigravity-cli': 'google',
    }.get(source, 'unknown')


def blocks_to_msgs(content, ext_id, role, ts, model, source, seq, src_pos) -> Iterator[Msg]:
    """Anthropic 形式の content（str または block 配列）を Msg 列に変換する。"""
    prov = provider_of(model, source)

    def mk(text, kind, part):
        return Msg(ext_id=ext_id, text=text, role=role, kind=kind, part=part,
                   seq=seq, ts=ts, model=model, provider=prov, src_pos=src_pos)

    if isinstance(content, str):
        if content.strip():
            yield mk(content, 'message', 0)
        return
    if not isinstance(content, list):
        return

    part = 0
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get('type')
        text = None
        kind = 'message'
        if t == 'text':
            text, kind = b.get('text'), 'message'
        elif t == 'thinking':
            text, kind = b.get('thinking'), 'thinking'
        elif t == 'tool_use':
            text = '%s %s' % (b.get('name') or '', json.dumps(b.get('input') or {},
                                                              ensure_ascii=False))
            kind = 'tool_call'
        elif t == 'tool_result':
            c = b.get('content')
            if isinstance(c, list):
                text = '\n'.join(x.get('text', '') for x in c if isinstance(x, dict))
            elif isinstance(c, str):
                text = c
            kind = 'tool_result'
        if text and text.strip():
            yield mk(text, kind, part)
            part += 1
