#!/usr/bin/env python3
"""Daily check that the search database agrees with the record. Notifies on failure.

Runs on the Docker host, from a timer. It asks the running server container to
compare the two (`chist reindex --check`, which changes nothing) and sends one
Gotify message when they disagree -- or when the question could not be asked at
all. That second case matters as much as the first: a check that cannot run must
not look like a check that passed.

Every run appends one line to the state file, so "no notification" can be told
apart from "did not run": the last line carries a timestamp.

Configuration (environment, all optional):
  CHIST_CONTAINER      container name                (chist-server)
  GOTIFY_CONTAINER     Gotify container name         (gotify)
  GOTIFY_NETWORK       its Docker network            (traefik_traefik)
  GOTIFY_TOKEN_FILE    file holding an app token     (~/.config/gotify/app-token)
  CHIST_CHECK_STATE    state file                    (~/.local/state/chist/search-check.log)

The token is read from a file and sent in a header from this process: it is never
on a command line.
"""
import datetime
import json
import os
import pathlib
import subprocess
import sys
import urllib.request

CONTAINER = os.environ.get('CHIST_CONTAINER', 'chist-server')
GOTIFY = os.environ.get('GOTIFY_CONTAINER', 'gotify')
NETWORK = os.environ.get('GOTIFY_NETWORK', 'traefik_traefik')
TOKEN_FILE = pathlib.Path(os.path.expanduser(
    os.environ.get('GOTIFY_TOKEN_FILE', '~/.config/gotify/app-token')))
STATE = pathlib.Path(os.path.expanduser(
    os.environ.get('CHIST_CHECK_STATE', '~/.local/state/chist/search-check.log')))


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def run(cmd, timeout=600):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:                    # noqa: BLE001 -- docker missing, timeout, ...
        return None, repr(e)


def notify(title, message):
    """Send one Gotify message. Returns '' on success, else why not."""
    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError as e:
        return 'no token: %r' % (e,)
    rc, ip = run(['docker', 'inspect', GOTIFY, '-f',
                  '{{(index .NetworkSettings.Networks "%s").IPAddress}}' % NETWORK], 30)
    if rc != 0 or not ip:
        return 'gotify container not found: %s' % ip
    req = urllib.request.Request(
        'http://%s/message' % ip,
        data=json.dumps({'title': title, 'message': message, 'priority': 8}).encode(),
        headers={'Content-Type': 'application/json', 'X-Gotify-Key': token}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return '' if r.status == 200 else 'http %d' % r.status
    except Exception as e:                    # noqa: BLE001
        return repr(e)


def main():
    rc, out = run(['docker', 'exec', CONTAINER, 'python', '-m', 'chist', 'reindex', '--check'])
    last = out.splitlines()[-3:] if out else []
    lines = out.splitlines() if out else []
    # Judge by what the command said, not only by its exit code: `docker exec`
    # also exits 1 when the container is missing, and that is not "the copy
    # disagrees" -- it is "nobody looked".
    if rc == 0 and lines and lines[-1] == 'ok':
        verdict, sent = 'ok', ''
    else:
        if rc == 1 and any(l.startswith('NOT IN STEP') for l in lines):
            verdict = 'NOT IN STEP'
        else:
            verdict = 'COULD NOT CHECK (rc=%s)' % rc
        sent = notify('chist: search database %s' % verdict,
                      '\n'.join(last) or '(no output)')
        sent = 'notified' if sent == '' else 'NOTIFY FAILED: ' + sent
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.open('a') as f:
        f.write('%s\t%s\t%s\t%s\n' % (now(), verdict, sent, ' | '.join(last)))
    print(verdict, sent)
    return 0 if rc == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
