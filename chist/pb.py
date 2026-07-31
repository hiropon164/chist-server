"""Minimal protobuf wire-format decoder.

Antigravity CLI stores conversations as raw protobuf inside SQLite, and no `.proto`
definition is available. This interprets the wire format alone, so values can be
pulled out by field number.
"""
from typing import Any, List, Optional, Tuple

Node = Tuple[int, str, Any]  # (field_number, kind, value); kind is 'v'|'s'|'m'|'f'

MAX_DEPTH = 8


def _varint(b: bytes, i: int) -> Tuple[int, int]:
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        if not x & 0x80:
            return r, i
        s += 7


def decode(b: bytes, depth: int = 0) -> List[Node]:
    """Decode bytes into nodes, stopping at the first corruption. Never raises."""
    out: List[Node] = []
    i = 0
    while i < len(b):
        try:
            tag, i = _varint(b, i)
            fn, wt = tag >> 3, tag & 7
            if fn == 0:
                break
            if wt == 0:
                v, i = _varint(b, i)
                out.append((fn, 'v', v))
            elif wt == 1:
                out.append((fn, 'f', b[i:i + 8])); i += 8
            elif wt == 5:
                out.append((fn, 'f', b[i:i + 4])); i += 4
            elif wt == 2:
                ln, i = _varint(b, i)
                v = b[i:i + ln]
                i += ln
                if len(v) != ln:
                    break
                s = _as_text(v)
                if s is not None:
                    out.append((fn, 's', s))
                    continue
                sub = decode(v, depth + 1) if depth < MAX_DEPTH else None
                out.append((fn, 'm', sub if sub else v))
            else:
                break
        except (IndexError, ValueError):
            break
    return out


def _as_text(v: bytes) -> Optional[str]:
    """Treat as a string when it is valid UTF-8 and mostly printable."""
    if not v:
        return None
    try:
        s = v.decode('utf-8')
    except UnicodeDecodeError:
        return None
    printable = sum(c.isprintable() or c in '\n\t\r' for c in s)
    return s if printable / len(s) > 0.95 else None


def get(nodes: List[Node], path: str) -> Optional[Any]:
    """Fetch the first value at a path such as '.19.2'."""
    for v in getall(nodes, path):
        return v
    return None


def getall(nodes: List[Node], path: str):
    """Return every value at a path (handles proto repeated fields)."""
    parts = [int(x) for x in path.strip('.').split('.')]

    def rec(cur, depth):
        if depth == len(parts):
            return
        fn = parts[depth]
        last = depth == len(parts) - 1
        for f, kind, v in cur:
            if f != fn:
                continue
            if last:
                if kind in ('s', 'v'):
                    yield v
            elif kind == 'm' and isinstance(v, list):
                for x in rec(v, depth + 1):
                    yield x

    return rec(nodes, 0)


def texts(nodes: List[Node], min_len: int = 1):
    """Enumerate contained strings as (path, value), for exploring an unknown structure."""
    def rec(cur, path):
        for fn, kind, v in cur:
            p = '%s.%d' % (path, fn)
            if kind == 's':
                if len(v) >= min_len:
                    yield p, v
            elif kind == 'm' and isinstance(v, list):
                for x in rec(v, p):
                    yield x
    return rec(nodes, '')
