"""Antigravity CLI (`agy`): ~/.gemini/antigravity-cli/conversations/<id>.db

会話は SQLite に入った生 protobuf（暗号化なし）。`.proto` 定義がないため、
実データから同定したフィールド番号で直接取り出す。対応は以下のとおり。

  steps.step_payload
    .1                 step_type
    .5.1.1             作成時刻（epoch 秒）
    .5.4.2 / .5.4.3    ツール名 / 引数 JSON
    .5.30 / .5.31      人間可読のツール説明
    .19.2              ユーザー発言          (step_type=14)
    .30.19             ユーザー発言          (step_type=23)
    .20.1 / .20.8      アシスタント応答      (step_type=15)
    .20.3              思考ブロック          (step_type=15)
    .14.4              ツール結果・取得内容  (step_type=8)
    .13.4.3            編集後テキスト        (step_type=7)
  trajectory_metadata_blob.data
    .7                 cwd（file:// 付き）
    .2.1               開始時刻（epoch 秒）
  gen_metadata.data
    .1.19              モデル名（例 gemini-default / gemini-3-flash-a）
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

# SQLite ファイルの先頭は書き込みのたびに変わる（変更カウンタ）。
# このアダプタのカーソルは steps.idx なので先頭ハッシュによる判定は使わない。
USE_HEAD_SHA = False

# (フィールドパス, role, kind) の抽出規則。上から順に適用する。
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
    """書き込み中の DB を避けるため一時コピーを開く。"""
    fd, tmp = tempfile.mkstemp(suffix='.db', prefix='chist-ag-')
    os.close(fd)
    shutil.copyfile(path, tmp)
    conn = sqlite3.connect(tmp)
    return conn, tmp


def _model(conn):
    """会話全体のモデル名。gen_metadata.idx は steps.idx と対応しないため
    セッション単位の近似として最頻値を使う。"""
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
            # 'file:///Volumes/USB%20SDD/...' の形で入っている
            s.cwd = urllib.parse.unquote(cwd[7:] if cwd.startswith('file://') else cwd)
        s.started_at = _ts(pb.get(nodes, '.2.1'))
    return s


def parse(path, start=0):
    """start は取り込み済み steps.idx の最大値。それより大きい行だけ読む。"""
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
                    # .20.1 と .20.8 のように同じ本文が二重に入ることがある
                    if len(text) < 2 or text in seen:
                        continue
                    seen.add(text)
                    msgs.append(Msg(
                        ext_id=base, text=text, role=role, kind=kind, part=part,
                        seq=idx, ts=ts, src_pos=idx, session_ext_id=ext_id,
                        model=model if role == 'assistant' else None,
                        provider='google'))
                    part += 1

            # ツール呼び出し（ツール名 + 引数 JSON）
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
