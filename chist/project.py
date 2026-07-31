"""マシンを跨いでプロジェクトを同定するための鍵を作る。

チャットログには git remote もホスト名も記録されていない。
`git -C <cwd> remote get-url origin` を実行できるのはリポジトリが存在するマシン
だけなので、必ず取り込み側で解決してから送る。
"""
import os
import platform
import re
import socket
import subprocess

_cache = {}

# scp 形式 (git@github.com:user/repo.git) と URL 形式の両方を受ける
_SCP = re.compile(r'^[\w.+-]+@([\w.-]+):(.+)$')
_URL = re.compile(r'^[a-z+]+://(?:[^@/]+@)?([\w.-]+(?::\d+)?)/(.+)$', re.I)


def host_info():
    return {'host': socket.gethostname(), 'os': platform.system().lower()}


def normalize_remote(url):
    """remote URL を host/path 形式へ正規化する。

    以下はすべて 'github.com/user/repo' になる。
      git@github.com:user/repo.git
      https://github.com/user/repo.git
      ssh://git@github.com/user/repo
    """
    url = (url or '').strip()
    if not url:
        return None
    m = _SCP.match(url) or _URL.match(url)
    if not m:
        return url
    host, path = m.group(1), m.group(2)
    host = host.split(':')[0].lower()
    path = path.strip('/')
    if path.endswith('.git'):
        path = path[:-4]
    return '%s/%s' % (host, path)


def project_key(cwd):
    """cwd からプロジェクト鍵を求める。

    git remote があればそれを正規化した値、なければ `local:<basename>`。
    後者はマシンを跨いだ同定には使えないが、少なくとも同じ名前で並ぶ。
    """
    if not cwd:
        return None
    if cwd in _cache:
        return _cache[cwd]

    key = None
    if os.path.isdir(cwd):
        try:
            out = subprocess.run(
                ['git', '-C', cwd, 'remote', 'get-url', 'origin'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
            if out.returncode == 0:
                key = normalize_remote(out.stdout.decode('utf-8', 'replace'))
        except (OSError, subprocess.SubprocessError):
            pass
    if not key:
        key = 'local:' + os.path.basename(cwd.rstrip('/\\')) if cwd else None

    _cache[cwd] = key
    return key


def backfill(conn, only_missing=True):
    """既存の sessions に host / project_key を埋める。

    cwd の異なり数だけ git を呼ぶので、数十回で済む。
    """
    info = host_info()
    conn.execute('INSERT INTO hosts (host, os, last_seen) VALUES (?,?,datetime("now")) '
                 'ON CONFLICT(host) DO UPDATE SET os=excluded.os, last_seen=excluded.last_seen',
                 (info['host'], info['os']))

    q = 'SELECT DISTINCT cwd FROM sessions WHERE cwd IS NOT NULL'
    if only_missing:
        q += ' AND (project_key IS NULL OR host IS NULL)'
    cwds = [r[0] for r in conn.execute(q)]

    n = 0
    for cwd in cwds:
        key = project_key(cwd)
        cur = conn.execute(
            'UPDATE sessions SET project_key = ?, host = COALESCE(host, ?) WHERE cwd = ?'
            + (' AND (project_key IS NULL OR host IS NULL)' if only_missing else ''),
            (key, info['host'], cwd))
        n += cur.rowcount
    # cwd が取れないセッション（Cursor CLI の数値ディレクトリなど）にも host は付ける
    conn.execute('UPDATE sessions SET host = ? WHERE host IS NULL', (info['host'],))
    conn.commit()
    return {'cwds': len(cwds), 'sessions': n, 'host': info['host']}
