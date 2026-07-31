"""Claude Code: ~/.claude/projects/**/*.jsonl

セッション ID はレコード内の sessionId を使う。サブエージェントのログ
(<session-id>/subagents/agent-*.jsonl) も同じ sessionId を持つため、
親セッションに自然に合流する。
"""
import glob
import os

from .base import Sess, blocks_to_msgs, read_jsonl

SOURCE = 'claude-code'
ROOT = os.path.expanduser('~/.claude/projects')

# uuid を持たず繰り返し追記されるメタ情報型
SKIP_TYPES = {
    'last-prompt', 'mode', 'permission-mode', 'file-history-snapshot',
    'file-history-delta', 'queue-operation', 'agent-name', 'attachment',
}


def discover():
    # サブエージェントが入れ子に置かれるため再帰必須（3.5-1）
    return sorted(glob.glob(os.path.join(ROOT, '**', '*.jsonl'), recursive=True))


def parse(path, start=0):
    records, next_off, errors = read_jsonl(path, start)
    sessions = {}
    msgs = []
    seq = start  # ファイル内オフセットを順序の代理に使う

    for pos, d in records:
        t = d.get('type')
        if t == 'ai-title':
            sid = d.get('sessionId')
            if sid and d.get('aiTitle'):
                sessions.setdefault(sid, Sess(ext_id=sid)).title = d['aiTitle']
            continue
        if t in SKIP_TYPES:
            continue

        sid = d.get('sessionId')
        if not sid:
            continue
        s = sessions.setdefault(sid, Sess(ext_id=sid))
        if d.get('cwd'):
            s.cwd = d['cwd']
        if d.get('gitBranch'):
            s.git_branch = d['gitBranch']
        ts = d.get('timestamp')
        if ts:
            if not s.started_at or ts < s.started_at:
                s.started_at = ts
            if not s.ended_at or ts > s.ended_at:
                s.ended_at = ts

        if t not in ('user', 'assistant', 'system'):
            continue

        uuid = d.get('uuid')
        if not uuid:
            continue

        m = d.get('message')
        content = None
        model = None
        role = t
        if isinstance(m, dict):
            content = m.get('content')
            model = m.get('model')
            role = m.get('role') or t
        elif isinstance(m, str):
            content = m
        elif d.get('content') is not None:
            content = d.get('content')

        if model == '<synthetic>':
            model = None
        if role not in ('user', 'assistant', 'system'):
            role = 'assistant'

        seq += 1
        for msg in blocks_to_msgs(content, uuid, role, ts, model, SOURCE, seq, pos):
            msg.session_ext_id = sid
            msgs.append(msg)

    return list(sessions.values()), msgs, next_off, errors
