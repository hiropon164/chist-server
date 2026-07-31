"""Build the key that identifies a project across machines.

Chat logs record neither the git remote nor the hostname. Only the machine that has
the repository can run `git -C <cwd> remote get-url origin`, so this must be resolved
on the ingest side before anything is sent.
"""
import os
import platform
import re
import socket
import subprocess

_cache = {}

# Accept both the scp form (git@github.com:user/repo.git) and URL forms
_SCP = re.compile(r'^[\w.+-]+@([\w.-]+):(.+)$')
_URL = re.compile(r'^[a-z+]+://(?:[^@/]+@)?([\w.-]+(?::\d+)?)/(.+)$', re.I)


def host_info():
    return {'host': socket.gethostname(), 'os': platform.system().lower()}


def normalize_remote(url):
    """Normalise a remote URL to host/path form.

    These all become 'github.com/user/repo':
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
    """Derive the project key from a working directory.

    Uses the normalised git remote when there is one, otherwise `local:<basename>`.
    The latter cannot identify a project across machines, but at least groups by name.
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
    """Fill in host / project_key on existing sessions.

    git is invoked once per distinct cwd, so this is a few dozen calls at most.
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
    # Sessions without a cwd (e.g. Cursor CLI's numeric directories) still get a host
    conn.execute('UPDATE sessions SET host = ? WHERE host IS NULL', (info['host'],))
    conn.commit()
    return {'cwds': len(cwds), 'sessions': n, 'host': info['host']}
