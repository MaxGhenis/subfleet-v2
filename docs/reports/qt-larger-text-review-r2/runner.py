"""Owned, bounded test holds; write evidence before and during each run."""
from __future__ import annotations

import datetime
from collections import Counter
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time

ARTIFACTS = Path(__file__).resolve().parent
SCRATCH = Path('/private/tmp/sf-r284r2')
PYTHON = SCRATCH / 'venv/bin/python'
SUITE = '/Users/maxghenis/reviews/subfleet-hub/host-runs/suite_hold.sh'


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def admitted(name, root, args):
    sys.path.insert(0, root)
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
            nodeids = [item.nodeid for item in session.items]
            write(ARTIFACTS / f'{name}.collection.json',
                  nodeids)
            if name == 'repeat-larger':
                counts = Counter(nodeids)
                assert len(nodeids) == 10 and len(counts) == 2, counts
                assert set(counts.values()) == {5}, counts
                assert all('[larger_text]' in nodeid for nodeid in counts), counts

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
    if name == 'diagnostics':
        # Navigation/replay checks run independently; this hold then audits mutant outputs.
        earlier = ARTIFACTS / 'identity-replay-diagnostics.end.json'
        stopped = ARTIFACTS / 'identity-replay-diagnostics.process-ended.json'
        while not earlier.exists() and not stopped.exists():
            time.sleep(1)
        if earlier.exists() and json.loads(earlier.read_text()).get('returncode') == 0:
            rows = [json.loads(line) for line in
                    (ARTIFACTS / 'identity-replay-diagnostics.items.jsonl').read_text().splitlines()]
            assert sum(row['when'] == 'call' and row['outcome'] == 'passed' for row in rows) == 9
            args = [*args, '-k', 'six_release_sensitive']
            write(ARTIFACTS / 'diagnostics-selection.json', {
                'earlier_nine_passed': True, 'args': args,
                'reason': 'Only the fresh six-run mutant audit remains; avoid repeating independent diagnostics',
            })

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
    queued_file = ARTIFACTS / f'{name}.queued.json'
    if not queued_file.exists():
        write(queued_file, {'queued': now(), 'root': root, 'args': args})
    queued_record = json.loads(queued_file.read_text())
    assert queued_record['root'] == root and queued_record['args'] == args
    elapsed_queue = (datetime.datetime.now(datetime.timezone.utc)
                     - datetime.datetime.fromisoformat(queued_record['queued'])).total_seconds()
    queued = time.monotonic() - elapsed_queue
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
            write(ARTIFACTS / f'{name}.process.json', {
                'controller_pid': os.getpid(), 'owned_process_group': process.pid,
                'started': now(), 'outside_lock': direct,
            })
            try:
                while process.poll() is None:
                    if start_file.exists() and admitted_at is None:
                        admitted_at = time.monotonic()
                    if admitted_at is not None and time.monotonic() - admitted_at > 1140:
                        write(ARTIFACTS / f'{name}.boundary.json', {'at': now(), 'seconds': 1140})
                        break
                    if admitted_at is None and not direct and time.monotonic() - queued > 1800:
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
                write(ARTIFACTS / f'{name}.process-ended.json', {
                    'at': now(), 'returncode': process.returncode,
                    'owned_group_terminated_and_waited': True,
                })
                end_file = ARTIFACTS / f'{name}.end.json'
                if admitted_at is not None and not end_file.exists():
                    write(end_file, {
                        'end': now(), 'seconds': time.monotonic() - admitted_at,
                        'returncode': process.returncode,
                        'max_child_rss_bytes': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                        'interrupted': 'controller boundary or interruption; only completed reports count',
                    })
        if admitted_at is not None or not direct:
            return process.returncode


if __name__ == '__main__':
    mode, name, root, *args = sys.argv[1:]
    sys.exit(admitted(name, root, args) if mode == 'admitted' else run(name, root, args))
