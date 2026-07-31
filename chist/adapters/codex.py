"""Codex: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

モデルはターン単位（turn_context.payload.model）で記録されるため、
直近の turn_context の値を以降のメッセージへ引き継ぐ。
タイトルは ~/.codex/session_index.jsonl の thread_name から補う。
"""
import glob
import json
import os

from .base import Msg, Sess, provider_of, read_jsonl

SOURCE = 'codex'
ROOT = os.path.expanduser('~/.codex/sessions')
INDEX = os.path.expanduser('~/.codex/session_index.jsonl')

_titles = None


def _title_for(sid):
    global _titles
    if _titles is None:
        _titles = {}
        try:
            with open(INDEX) as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get('id'):
                        _titles[d['id']] = d.get('thread_name')
        except OSError:
            pass
    return _titles.get(sid)


def discover():
    return sorted(glob.glob(os.path.join(ROOT, '**', '*.jsonl'), recursive=True))


def parse(path, start=0):
    records, next_off, errors = read_jsonl(path, start)
    sess = None
    msgs = []
    model = None
    seq = start

    for pos, d in records:
        t = d.get('type')
        ts = d.get('timestamp')
        pl = d.get('payload') or {}

        if t == 'session_meta':
            sid = pl.get('id') or d.get('session_id')
            if not sid:
                continue
            sess = Sess(ext_id=sid, cwd=pl.get('cwd'), started_at=pl.get('timestamp') or ts,
                        title=_title_for(sid))
            git = pl.get('git') or {}
            sess.git_branch = git.get('branch')
            continue

        if t == 'turn_context':
            if pl.get('model'):
                model = pl['model']
            continue

        if t != 'response_item' or sess is None:
            continue

        if ts and (not sess.ended_at or ts > sess.ended_at):
            sess.ended_at = ts

        ptype = pl.get('type')
        seq += 1
        base_id = '%s:%d' % (sess.ext_id, pos)
        prov = provider_of(model, SOURCE)

        def add(text, role, kind, part=0):
            if text and str(text).strip():
                msgs.append(Msg(ext_id=base_id, text=str(text), role=role, kind=kind,
                                part=part, seq=seq, ts=ts, model=model, provider=prov,
                                src_pos=pos, session_ext_id=sess.ext_id))

        if ptype == 'message':
            role = pl.get('role') or 'assistant'
            if role == 'developer':
                role = 'system'
            if role not in ('user', 'assistant', 'system'):
                role = 'assistant'
            part = 0
            for c in (pl.get('content') or []):
                if not isinstance(c, dict):
                    continue
                if c.get('type') in ('input_text', 'output_text', 'text'):
                    add(c.get('text'), role, 'message', part)
                    part += 1
        elif ptype == 'reasoning':
            # content は encrypted_content のみのことが多く復号できない。
            # 平文の summary があるときだけ拾う。
            part = 0
            for s in (pl.get('summary') or []):
                txt = s.get('text') if isinstance(s, dict) else s
                add(txt, 'assistant', 'thinking', part)
                part += 1
        elif ptype in ('function_call', 'custom_tool_call'):
            add('%s %s' % (pl.get('name') or '', pl.get('arguments') or pl.get('input') or ''),
                'assistant', 'tool_call')
        elif ptype in ('function_call_output', 'custom_tool_call_output'):
            out = pl.get('output')
            if isinstance(out, (dict, list)):
                out = json.dumps(out, ensure_ascii=False)
            add(out, 'tool', 'tool_result')

    return ([sess] if sess else []), msgs, next_off, errors
