"""Antigravity CLI の steps.step_payload (生protobuf) からテキストを抽出する最小パーサ"""
import sqlite3, sys


def varint(b, i):
    r = s = 0
    while True:
        x = b[i]; i += 1
        r |= (x & 0x7F) << s
        if not x & 0x80:
            return r, i
        s += 7


def walk(b, depth=0, out=None):
    """length-delimited フィールドを再帰的に辿り、UTF-8として妥当な文字列を集める"""
    if out is None:
        out = []
    i = 0
    while i < len(b):
        try:
            tag, i = varint(b, i)
            fn, wt = tag >> 3, tag & 7
            if wt == 0:
                _, i = varint(b, i)
            elif wt == 1:
                i += 8
            elif wt == 5:
                i += 4
            elif wt == 2:
                ln, i = varint(b, i)
                v = b[i:i + ln]; i += ln
                if len(v) != ln:
                    return out
                try:
                    s = v.decode('utf-8')
                    printable = sum(c.isprintable() or c in '\n\t' for c in s)
                    if len(s) >= 4 and printable / len(s) > 0.95:
                        out.append((depth, fn, s))
                        continue
                except UnicodeDecodeError:
                    pass
                if depth < 6:
                    walk(v, depth + 1, out)
            else:
                return out
        except (IndexError, ValueError):
            return out
    return out


ROLE = {14: 'user', 15: 'assistant', 8: 'assistant', 5: 'artifact',
        17: 'artifact', 9: 'tool_call', 33: 'tool_result'}

db = sys.argv[1]
c = sqlite3.connect(db)
for idx, st, pl in c.execute('select idx, step_type, step_payload from steps order by idx'):
    if not pl:
        continue
    texts = [s for _, _, s in walk(pl) if len(s) > 20]
    if not texts:
        continue
    longest = max(texts, key=len)
    print(f'[{idx:3}] step_type={st:3} ({ROLE.get(st, "?"):11}) {longest[:110]!r}')
