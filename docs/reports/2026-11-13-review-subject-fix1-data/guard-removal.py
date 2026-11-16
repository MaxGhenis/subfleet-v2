"""Remove only unchanged, manifest-created job guard files after evidence copy.

This helper deliberately leaves the guard source and scratch evidence intact.
It does not inspect processes or invoke any external command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time

ROOT = Path('/private/tmp/sf-a2-fix1')
PROJECT = Path('/Users/maxghenis/.subfleet/worktrees/20261115-180648-rv-a2-fix-r1b')


def allowed(path):
    if path in (ROOT / 'sitecustomize.py', PROJECT / 'sitecustomize.py'):
        return True
    return (path.name == 'sitecustomize.py'
            and path.parent.name == 'site-packages'
            and path.parent.parent.name.startswith('python')
            and (path.is_relative_to(PROJECT / '.venv' / 'lib')
                 or path.is_relative_to(ROOT / 'uv-cache' / 'builds-v0')))


def record(path, status):
    with (ROOT / 'guard-removals.jsonl').open('a') as stream:
        stream.write(json.dumps({'path': str(path), 'status': status, 'at': time.time()}) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-copied', action='store_true', required=True)
    parser.parse_args()
    entries = {}
    for line in (ROOT / 'guard-installations.jsonl').read_text().splitlines():
        entry = json.loads(line)
        path = Path(entry['path'])
        assert path.is_absolute() and allowed(path), 'unexpected guard manifest path'
        assert not path.is_symlink(), 'refusing a symlink in the guard manifest'
        entries[path] = entry['sha256']
    # Preflight all hashes before removing any file, preserving changed files.
    for path, expected in entries.items():
        if path.exists():
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            assert actual == expected, 'refusing a changed guard file: ' + str(path)
    for path in entries:
        if path.exists():
            record(path, 'verified; removal about to run')
            path.unlink()
            record(path, 'removed')
        else:
            record(path, 'already absent')
    print(json.dumps({'manifest_entries': len(entries), 'remaining_guard_files':
                      sum(path.exists() for path in entries)}))


if __name__ == '__main__':
    main()
