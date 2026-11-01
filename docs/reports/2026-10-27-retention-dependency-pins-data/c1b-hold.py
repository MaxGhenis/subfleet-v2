"""Foreground-only hold runner with complete per-phase results and a 19m cap."""
from __future__ import annotations
import collections
import ctypes
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

DATA = Path(__file__).resolve().parent
SCRATCH = Path('/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-rdp217c1')
RULES = Path('/Users/maxghenis/reviews/subfleet-hub/host-runs')


def stamp():
    return {'utc': datetime.now(timezone.utc).isoformat(), 'load': os.getloadavg()}


def child(label, args):
    import pytest
    start = time.monotonic()
    admitted = {**stamp(), 'pid': os.getpid()}
    (DATA / f'{label}-admitted.json').write_text(json.dumps(admitted))
    print('ADMITTED ' + json.dumps(admitted), flush=True)
    class Evidence:
        def pytest_collection_finish(self, session):
            (DATA / f'{label}-collected.json').write_text(json.dumps([i.nodeid for i in session.items], indent=2) + '\n')
        def pytest_runtest_logreport(self, report):
            with (DATA / f'{label}-results.jsonl').open('a') as out:
                out.write(json.dumps({'nodeid': report.nodeid, 'when': report.when,
                                     'outcome': report.outcome, 'xfail': getattr(report, 'wasxfail', None)}) + '\n')
        def pytest_runtest_setup(self, item):
            if time.monotonic() - start > 1000:
                pytest.exit('Hold budget: resume uncompleted nodes in another foreground hold', returncode=2)
    rc = pytest.main(['-q', '-p', 'no:cacheprovider', '--durations=5', '--hypothesis-show-statistics', *args], plugins=[Evidence()])
    print('FINISHED ' + json.dumps({**stamp(), 'rc': rc, 'seconds': time.monotonic() - start,
                                    'rss': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}), flush=True)
    return rc


def descendants(pid):
    # The sandbox disallows /bin/ps. Enumerate only children of our own process.
    lib = ctypes.CDLL('/usr/lib/libproc.dylib')
    listing = lib.proc_listchildpids
    listing.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    listing.restype = ctypes.c_int
    found = []
    def walk(parent):
        buffer = (ctypes.c_int * 4096)()
        size = listing(parent, buffer, ctypes.sizeof(buffer))
        if size < 0:
            raise OSError('Cannot enumerate our child processes')
        for child in buffer[:size]:
            if child:
                walk(child)
                found.append(child)
    walk(pid)
    return found


def stop(proc):
    owned = descendants(proc.pid)
    for pid in owned:
        try: os.kill(pid, signal.SIGINT)
        except ProcessLookupError: pass
    proc.send_signal(signal.SIGINT)
    try: proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        for pid in owned:
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass
        proc.kill()
        proc.wait()


def main(label, args):
    if args[:1] == ['--nodes']:
        args = Path(args[1]).read_text().splitlines() + args[2:]
        if not args:
            print(label + ': consolidated into another batch; no pytest started', flush=True)
            return 0
    marker = DATA / f'{label}-admitted.json'
    assert not marker.exists(), label
    env = os.environ.copy()
    for key in ('SUBFLEET_RETENTION_DORMANT', 'PYTHONPATH'):
        env.pop(key, None)
    tmp = SCRATCH / label
    tmp.mkdir(exist_ok=True)
    env.update(TMPDIR=str(tmp) + '/', HYPOTHESIS_STORAGE_DIRECTORY=str(SCRATCH / 'hyp'),
               UV_PROJECT_ENVIRONMENT=str(SCRATCH / 'venv'), UV_CACHE_DIR=str(SCRATCH / 'uv-cache'),
               PYTHONDONTWRITEBYTECODE='1')
    cmd = ['env', '-u', 'SUBFLEET_RETENTION_DORMANT', '-u', 'PYTHONPATH',
           '/opt/homebrew/bin/uv', 'run', '--no-sync', 'python', str(Path(__file__).resolve()), '--child', label, *args]
    (DATA / f'{label}-runner-pid.txt').write_text(str(os.getpid()) + '\n')
    queued = stamp()
    outside = False
    start = time.monotonic() - (float(os.environ.get('SF_C1_QUEUE_AGE_SECONDS', '0')) if label in ('c1b-remaining-02', 'c1b-remaining-03') else 0)
    log = DATA / f'{label}.log'
    with log.open('w') as out:
        def launch(locked):
            return subprocess.Popen((['/bin/zsh', str(RULES / 'suite_hold.sh')] if locked else []) + cmd,
                                    env=env, stdout=out, stderr=subprocess.STDOUT)
        proc = launch(True)
        admitted_at = None
        while proc.poll() is None:
            if marker.exists() and admitted_at is None:
                admitted_at = time.monotonic()
            if admitted_at is not None and time.monotonic() - admitted_at > 1120:
                stop(proc)
                break
            if admitted_at is None and time.monotonic() - start >= 1800:
                stop(proc)
                outside = True
                proc = launch(False)
                admitted_at = time.monotonic()
            time.sleep(1)
    body = log.read_text()
    summary = [line for line in body.splitlines() if line.startswith('FINISHED ')]
    record = {'label': label, 'args': args, 'queued': queued, 'outside_lock': outside,
              'rc': proc.returncode, 'wall_s': round(time.monotonic() - start, 2), 'end': stamp(),
              'admitted': json.loads(marker.read_text()) if marker.exists() else None,
              'finished': json.loads(summary[-1][9:]) if summary else None}
    with (DATA / 'c1b-holds.jsonl').open('a') as out:
        out.write(json.dumps(record) + '\n')
    print(json.dumps(record), flush=True)
    print(body[-9000:], flush=True)
    return proc.returncode

if __name__ == '__main__':
    if sys.argv[1] == '--child':
        raise SystemExit(child(sys.argv[2], sys.argv[3:]))
    raise SystemExit(main(sys.argv[1], sys.argv[2:]))
