"""Claude Code: ~/.claude/projects/**/*.jsonl

The session id comes from the sessionId field in each record. Subagent logs
(<session-id>/subagents/agent-*.jsonl) carry the same sessionId, so they merge into
the parent session naturally.
"""
import glob
import os

from .base import Sess, blocks_to_msgs, read_jsonl

SOURCE = 'claude-code'
ROOT = os.path.expanduser('~/.claude/projects')

# Metadata records that carry no uuid and are appended repeatedly
SKIP_TYPES = {
    'last-prompt', 'mode', 'permission-mode', 'file-history-snapshot',
    'file-history-delta', 'queue-operation', 'agent-name', 'attachment',
}


def discover():
    # Must recurse: subagent logs are nested in subdirectories
    return sorted(glob.glob(os.path.join(ROOT, '**', '*.jsonl'), recursive=True))


def parse(path, start=0):
    records, next_off, errors = read_jsonl(path, start)
    sessions = {}
    msgs = []
    seq = start  # use the in-file offset as a stand-in for ordering

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
