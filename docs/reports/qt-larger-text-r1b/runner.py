"""Owned, bounded test holds; write evidence before and during each run."""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time

ARTIFACTS = Path(__file__).resolve().parent
SCRATCH = Path('/private/tmp/sf-fix284r1b')
PYTHON = SCRATCH / 'venv/bin/python'
SUITE = '/Users/maxghenis/reviews/subfleet-hub/host-runs/suite_hold.sh'


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def admitted(name, root, args):
    import pytest

    start = time.monotonic()
    write(ARTIFACTS / f'{name}.start.json', {
        'name': name, 'start': now(), 'load': os.getloadavg(),
        'root': root, 'args': args, 'python': sys.version,
    })
    output = ARTIFACTS / f'{name}.items.jsonl'
    output.write_text('')

    class Evidence:
        def pytest_runtest_logreport(self, report):
            outcome = ('xfailed' if getattr(report, 'wasxfail', False)
                       and report.skipped else report.outcome)
            with output.open('a') as handle:
                handle.write(json.dumps({
                    'nodeid': report.nodeid, 'when': report.when,
                    'outcome': outcome, 'duration': report.duration,
                    'longrepr': str(report.longrepr) if report.failed else None,
                }) + '\n')

        def pytest_collection_finish(self, session):
            write(ARTIFACTS / f'{name}.collection.json',
                  [item.nodeid for item in session.items])

    base = SCRATCH / 'cases' / name
    base.parent.mkdir(exist_ok=True)
    result = pytest.main([
        '-q', '-p', 'no:cacheprovider', '--basetemp', str(base),
        '--junitxml', str(ARTIFACTS / f'{name}.xml'), *args,
    ], plugins=[Evidence()])
    write(ARTIFACTS / f'{name}.end.json', {
        'end': now(), 'seconds': time.monotonic() - start,
        'returncode': int(result),
        'max_child_rss_bytes': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
    })
    return int(result)


def run(name, root, args):
    temporary = SCRATCH / f'hold-{name}'
    temporary.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.pop('SUBFLEET_RETENTION_DORMANT', None)
    env.update({
        'TMPDIR': str(temporary) + '/',
        'HYPOTHESIS_STORAGE_DIRECTORY': str(SCRATCH / 'hyp'),
        'UV_PROJECT_ENVIRONMENT': str(SCRATCH / 'venv'),
        'UV_CACHE_DIR': str(SCRATCH / 'uv-cache'),
        'CLANG_MODULE_CACHE_PATH': str(SCRATCH / 'clang-cache'),
        'SUBFLEET_SWIFT_NESTED_SANDBOX': 'off',
        'PYTHONDONTWRITEBYTECODE': '1',
    })
    command = [
        'env', '-u', 'SUBFLEET_RETENTION_DORMANT', '-u', 'PYTHONPATH',
        'uv', 'run', '--no-sync', 'python', str(Path(__file__).resolve()),
        'admitted', name, root, *args,
    ]
    queued = time.monotonic()
    write(ARTIFACTS / f'{name}.queued.json', {'queued': now(), 'root': root, 'args': args})
    direct = False
    while True:
        start_file = ARTIFACTS / f'{name}.start.json'
        admitted_at = None
        with (ARTIFACTS / f'{name}.log').open('a') as log:
            # A separate process group permits bounded cleanup of our own tree.
            # The controller stays attached to the tool session and always waits.
            process = subprocess.Popen(
                command if direct else ['/bin/zsh', SUITE, *command],
                cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                process_group=0,
            )
            try:
                while process.poll() is None:
                    if start_file.exists() and admitted_at is None:
                        admitted_at = time.monotonic()
                    if admitted_at is not None and time.monotonic() - admitted_at > 1140:
                        write(ARTIFACTS / f'{name}.boundary.json', {'at': now(), 'seconds': 1140})
                        break
                    if admitted_at is None and time.monotonic() - queued > 1800:
                        direct = True
                        write(ARTIFACTS / f'{name}.outside-lock.json', {'at': now(), 'reason': '30 minutes without admission'})
                        break
                    time.sleep(1)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        if admitted_at is not None or not direct:
            return process.returncode


if __name__ == '__main__':
    mode, name, root, *args = sys.argv[1:]
    sys.exit(admitted(name, root, args) if mode == 'admitted' else run(name, root, args))
