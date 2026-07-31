"""Cursor CLI: ~/.cursor/projects/<cwd-encoded>/agent-transcripts/<id>/<id>.jsonl

制約が二つある。
  - タイムスタンプが記録されていない。セッションの期間はファイルの mtime で代用し、
    メッセージの ts は NULL のままにする。
  - モデル名も記録されていない（sources.model_capture = 'none'）。
"""
import glob
import os
import re

from .base import Msg, Sess, read_jsonl

SOURCE = 'cursor-cli'
ROOT = os.path.expanduser('~/.cursor/projects')

_USER_QUERY = re.compile(r'^\s*<user_query>\s*(.*?)\s*</user_query>\s*$', re.S)


def discover():
    return sorted(glob.glob(os.path.join(ROOT, '*', 'agent-transcripts', '*', '*.jsonl')))


def _cwd_from_path(path):
    """`Users-you-Desktop-example` のようなディレクトリ名から cwd を復元する。

    '-' 区切りのため元のパスにハイフンが含まれると曖昧になる。
    実在するパスを優先し、無ければ素朴な復元結果を返す。
    """
    rel = os.path.relpath(path, ROOT)
    enc = rel.split(os.sep)[0]
    if enc.isdigit():
        return None  # ワークスペース未確定のプロジェクト
    naive = '/' + enc.replace('-', '/')
    if os.path.isdir(naive):
        return naive
    # 末尾から順にハイフンを結合し直して実在するものを探す
    parts = enc.split('-')
    for join_at in range(len(parts) - 1, 0, -1):
        cand = '/' + '/'.join(parts[:join_at]) + '/' + '-'.join(parts[join_at:])
        if os.path.isdir(cand):
            return cand
    return naive


def parse(path, start=0):
    records, next_off, errors = read_jsonl(path, start)
    sid = os.path.splitext(os.path.basename(path))[0]
    mtime = os.path.getmtime(path)
    import datetime
    ended = datetime.datetime.utcfromtimestamp(mtime).strftime('%Y-%m-%dT%H:%M:%SZ')

    sess = Sess(ext_id=sid, cwd=_cwd_from_path(path), ended_at=ended)
    msgs = []
    seq = start

    for pos, d in records:
        role = d.get('role')
        if role not in ('user', 'assistant', 'system'):
            continue
        m = d.get('message') or {}
        content = m.get('content') if isinstance(m, dict) else None
        if isinstance(content, str):
            content = [{'type': 'text', 'text': content}]
        if not isinstance(content, list):
            continue

        seq += 1
        part = 0
        for b in content:
            if not isinstance(b, dict):
                continue
            text = b.get('text')
            if b.get('type') != 'text' or not text or not text.strip():
                continue
            hit = _USER_QUERY.match(text)
            if hit:
                text = hit.group(1)
            msgs.append(Msg(ext_id='%s:%d' % (sid, pos), text=text, role=role,
                            kind='message', part=part, seq=seq, src_pos=pos,
                            provider='unknown', session_ext_id=sid))
            part += 1

        # 最初のユーザー発言をタイトルの代わりにする
        if not sess.title and role == 'user' and msgs:
            lines = msgs[-1].text.strip().splitlines()
            if lines:
                sess.title = lines[0][:80]

    return [sess], msgs, next_off, errors
