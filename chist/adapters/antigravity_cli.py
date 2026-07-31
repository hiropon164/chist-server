"""Antigravity CLI (`agy`): ~/.gemini/antigravity-cli/conversations/<id>.db

Conversations are raw protobuf inside SQLite (not encrypted). With no `.proto`
definition available, values are pulled out by field numbers identified from real
data. The mapping is:

  steps.step_payload
    .1                 step_type
    .5.1.1             created at (epoch seconds)
    .5.4.2 / .5.4.3    tool name / argument JSON
    .5.30 / .5.31      human-readable tool description
    .19.2              user message          (step_type=14)
    .30.19             user message          (step_type=23)
    .20.1 / .20.8      assistant reply       (step_type=15)
    .20.3              thinking block        (step_type=15)
    .14.4              tool result / fetched (step_type=8)
    .13.4.3            text after an edit     (step_type=7)
  trajectory_metadata_blob.data
    .7                 cwd (with a file:// prefix)
    .2.1               started at (epoch seconds)
  gen_metadata.data
    .1.19              model name (e.g. gemini-default / gemini-3-flash-a)
"""
import collections
import datetime
import glob
import os
import shutil
import sqlite3
import tempfile
import urllib.parse

from .. import pb
from .base import Msg, Sess

SOURCE = 'antigravity-cli'
ROOT = os.path.expanduser('~/.gemini/antigravity-cli/conversations')

# The first bytes of a SQLite file change on every write (the change counter).
# This adapter's cursor is steps.idx, so the head-hash check is not used.
USE_HEAD_SHA = False

# Extraction rules as (field path, role, kind), applied in order.
FIELDS = [
    ('.19.2',   'user',      'message'),
    ('.30.19',  'user',      'message'),
    ('.20.1',   'assistant', 'message'),
    ('.20.3',   'assistant', 'thinking'),
    ('.14.4',   'tool',      'tool_result'),
    ('.13.4.3', 'tool',      'tool_result'),
]


def discover():
    return sorted(glob.glob(os.path.join(ROOT, '*.db')))


def _ts(sec):
    if not sec or sec < 1_000_000_000 or sec > 4_000_000_000:
        return None
    return datetime.datetime.utcfromtimestamp(sec).strftime('%Y-%m-%dT%H:%M:%SZ')


def _open_snapshot(path):
    """Open a temporary copy so a database being written to is never read directly."""
    fd, tmp = tempfile.mkstemp(suffix='.db', prefix='chist-ag-')
    os.close(fd)
    shutil.copyfile(path, tmp)
    conn = sqlite3.connect(tmp)
    return conn, tmp


def _model(conn):
    """Model name for the whole conversation. gen_metadata.idx does not line up with
    steps.idx, so the most common value is used as a per-session approximation."""
    c = collections.Counter()
    try:
        rows = conn.execute('SELECT data FROM gen_metadata').fetchall()
    except sqlite3.Error:
        return None
    for (b,) in rows:
        if not b:
            continue
        v = pb.get(pb.decode(b), '.1.19')
        if isinstance(v, str) and v:
            c[v] += 1
    return c.most_common(1)[0][0] if c else None


def _session(conn, ext_id):
    s = Sess(ext_id=ext_id)
    try:
        row = conn.execute('SELECT data FROM trajectory_metadata_blob').fetchone()
    except sqlite3.Error:
        row = None
    if row and row[0]:
        nodes = pb.decode(row[0])
        cwd = pb.get(nodes, '.7')
        if isinstance(cwd, str):
            # Stored as e.g. 'file:///Volumes/USB%20SDD/...'
            s.cwd = urllib.parse.unquote(cwd[7:] if cwd.startswith('file://') else cwd)
        s.started_at = _ts(pb.get(nodes, '.2.1'))
    return s


def parse(path, start=0):
    """`start` is the highest steps.idx already ingested; only later rows are read."""
    ext_id = os.path.splitext(os.path.basename(path))[0]
    conn, tmp = _open_snapshot(path)
    errors = []
    msgs = []
    max_idx = start
    try:
        sess = _session(conn, ext_id)
        model = _model(conn)
        rows = conn.execute(
            'SELECT idx, step_type, step_payload FROM steps WHERE idx > ? ORDER BY idx',
            (start,)).fetchall()

        for idx, step_type, payload in rows:
            max_idx = max(max_idx, idx)
            if not payload:
                continue
            try:
                nodes = pb.decode(payload)
            except Exception as e:
                errors.append('%s@idx=%d: %s' % (os.path.basename(path), idx, e))
                continue

            ts = _ts(pb.get(nodes, '.5.1.1'))
            if ts and (not sess.ended_at or ts > sess.ended_at):
                sess.ended_at = ts

            base = '%s:%d' % (ext_id, idx)
            part = 0
            seen = set()

            for fpath, role, kind in FIELDS:
                for text in pb.getall(nodes, fpath):
                    if not isinstance(text, str):
                        continue
                    text = text.strip()
                    # The same body can appear twice, e.g. in both .20.1 and .20.8
                    if len(text) < 2 or text in seen:
                        continue
                    seen.add(text)
                    msgs.append(Msg(
                        ext_id=base, text=text, role=role, kind=kind, part=part,
                        seq=idx, ts=ts, src_pos=idx, session_ext_id=ext_id,
                        model=model if role == 'assistant' else None,
                        provider='google'))
                    part += 1

            # Tool call (tool name + argument JSON)
            name = pb.get(nodes, '.5.4.2')
            args = pb.get(nodes, '.5.4.3')
            if isinstance(name, str) or isinstance(args, str):
                label = pb.get(nodes, '.5.30') or pb.get(nodes, '.5.31') or ''
                text = ' '.join(x for x in (name, label, args) if isinstance(x, str)).strip()
                if text and text not in seen:
                    msgs.append(Msg(ext_id=base, text=text, role='assistant',
                                    kind='tool_call', part=part, seq=idx, ts=ts,
                                    src_pos=idx, session_ext_id=ext_id, provider='google'))

            if not sess.title:
                for m in msgs:
                    if m.role == 'user' and m.kind == 'message':
                        lines = m.text.splitlines()
                        if lines:
                            sess.title = lines[0][:80]
                        break
    finally:
        conn.close()
        try:
            os.unlink(tmp)
        except OSError:
            pass

    return [sess], msgs, max_idx, errors
