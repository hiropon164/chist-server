"""protobuf wire format の最小デコーダ。

Antigravity CLI は会話を SQLite に入れた生 protobuf として保存しており、
`.proto` 定義が手に入らない。フィールド番号を直接指定して値を取り出すために、
wire format だけを解釈する。
"""
from typing import Any, List, Optional, Tuple

Node = Tuple[int, str, Any]  # (field_number, kind, value) / kind は 'v'|'s'|'m'|'f'

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
    """バイト列をノード列へ復号する。壊れた位置で打ち切る（例外は投げない）。"""
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
    """UTF-8 として妥当かつほぼ印字可能なら文字列とみなす。"""
    if not v:
        return None
    try:
        s = v.decode('utf-8')
    except UnicodeDecodeError:
        return None
    printable = sum(c.isprintable() or c in '\n\t\r' for c in s)
    return s if printable / len(s) > 0.95 else None


def get(nodes: List[Node], path: str) -> Optional[Any]:
    """'.19.2' のようなパスで最初の一致を取り出す。"""
    for v in getall(nodes, path):
        return v
    return None


def getall(nodes: List[Node], path: str):
    """同一パスの値をすべて返す（proto の repeated 対応）。"""
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
    """含まれる文字列を (パス, 値) で列挙する（構造調査用）。"""
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
