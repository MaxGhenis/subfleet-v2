"""Foreground-only hold runner with complete per-phase results and a 19m cap."""
from __future__ import annotations
import collections
import ast
import ctypes
import json
import os
from pathlib import Path
import resource
import re
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

DATA = Path(__file__).resolve().parent
SCRATCH = Path('/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-rdp217c1/recovery-0747')
RULES = Path('/Users/maxghenis/reviews/subfleet-hub/host-runs')


def stamp():
    return {'utc': datetime.now(timezone.utc).isoformat(), 'load': os.getloadavg()}


def child(label, args):
    sys.path.insert(0, str(Path.cwd()))
    import pytest
    import faulthandler
    if label.startswith("c1c-remaining-"):
        faulthandler.dump_traceback_later(240, repeat=True)
    if label == "c1c-remaining-46":
        args = [*args, "--capture=no", "--hypothesis-verbosity=verbose"]
    from tests import failed_keep
    failed_keep.ROOT = SCRATCH / "failed"
    start = time.monotonic()
    admitted = {**stamp(), 'pid': os.getpid()}
    (DATA / f'{label}-admitted.json').write_text(json.dumps(admitted))
    print('ADMITTED ' + json.dumps(admitted), flush=True)
    class Evidence:
        def pytest_collection_modifyitems(self, items):
            if label.startswith('c1c-remaining-'):
                generated = {}
                for item in items:
                    path = str(item.path)
                    if path not in generated:
                        tree = ast.parse(Path(path).read_text())
                        generated[path] = {node.name for node in tree.body
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and any(isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                                    and any(isinstance(decorator, ast.Call)
                                            and isinstance(decorator.func, ast.Name)
                                            and decorator.func.id == 'given' for decorator in child.decorator_list)
                                    for child in ast.walk(node))}
                def cost(item):
                    if item.nodeid.endswith('::runTest') or any(name in item.nodeid for name in (
                            'test_no_schedule_of_sweep_moves', 'test_c6_3_c26_9_admission_places',
                            'test_early_fences_equal_reservation_before_and_after_quarantine',
                            'test_the_snapshot_is_what_git_itself_can_index')):
                        return 2
                    return int(bool(getattr(item.obj, 'is_hypothesis_test', False))
                               or item.name.split('[')[0] in generated[str(item.path)])
                items.sort(key=cost)
        def pytest_collection_finish(self, session):
            (DATA / f'{label}-collected.json').write_text(json.dumps([i.nodeid for i in session.items], indent=2) + '\n')
        def pytest_runtest_logreport(self, report):
            if report.failed:
                with (DATA / f'{label}-failures.txt').open('a') as out:
                    out.write(report.nodeid + '\n' + str(report.longrepr) + '\n')
            with (DATA / f'{label}-results.jsonl').open('a') as out:
                out.write(json.dumps({'nodeid': report.nodeid, 'when': report.when,
                                     'outcome': report.outcome, 'xfail': getattr(report, 'wasxfail', None)}) + '\n')
        def pytest_runtest_setup(self, item):
            if time.monotonic() - start > 1000:
                pytest.exit('Hold budget: resume uncompleted nodes in another foreground hold', returncode=2)
    rc = pytest.main(['-q', '-p', 'no:cacheprovider', '--durations=5', '--hypothesis-show-statistics', *args], plugins=[Evidence()])
    faulthandler.cancel_dump_traceback_later()
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


def grouped_nodes(label, args):
    """Combine future short slices belonging to this same foreground worker."""
    match = re.fullmatch(r'c1c-remaining-(\d+)(?:-resume-\d+)?', label)
    groups = ([48, 50, 52, 54, 56], [49, 51, 53, 55, 57], [4, 6, 16, 18, 20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40, 42, 44],
              [7, 11, 15, 19, 23, 27, 31, 35, 39, 43],
              [5, 9, 13, 17, 21, 25, 29, 33, 37, 41],
              [14, 12, 10, 8, 45, 46])
    number = int(match[1]) if match else None
    group = next((g for g in groups if number in g), None)
    if not group:
        return args
    batches = json.loads((DATA / 'c1c-batches.json').read_text())
    nodes = [n for i in group[group.index(number):] for n in batches[i - 1]]
    done = set()
    for file in DATA.glob('c1c-remaining-*-results.jsonl'):
        reports = collections.defaultdict(dict)
        for line in file.read_text().splitlines():
            row = json.loads(line)
            reports[row['nodeid']][row['when']] = row['outcome']
        done.update(n for n, phases in reports.items() if phases.get('teardown') == 'passed'
                    and (phases.get('call') in ('passed', 'skipped') or phases.get('setup') == 'skipped')
                    and 'failed' not in phases.values())
    return [n for n in nodes if n not in done]


def main(label, args):
    # The four-hour rule stops new holds. A hold queued before that cutoff
    # retains its original 30-minute lock wait and sub-20-minute pytest cap.
    if datetime.now(timezone.utc) >= datetime(2026, 10, 28, 11, 46, tzinfo=timezone.utc):
        print(label + ': no new hold after the four-hour cutoff', flush=True)
        return 2
    if args[:1] == ['--nodes']:
        args = Path(args[1]).read_text().splitlines() + args[2:]
        args = grouped_nodes(label, args)
        owner_file = DATA / 'c1c-node-owners.json'
        match = re.fullmatch(r'c1c-remaining-(\d+)(?:-resume-\d+)?', label)
        if owner_file.exists() and match:
            owners = json.loads(owner_file.read_text())
            args = [node for node in args if owners.get(node, int(match[1])) == int(match[1])]
        if not args:
            print(label + ': consolidated into another batch; no pytest started', flush=True)
            return 0
    claim = DATA / f"{label}-claimed.json"
    try:
        with claim.open("x") as out:
            json.dump({"pid": os.getpid(), **stamp()}, out)
    except FileExistsError:
        # A foreground peer may reach this same future slice first. Reuse it.
        while datetime.now(timezone.utc) < datetime(2026, 10, 28, 11, 46, tzinfo=timezone.utc):
            ledger = DATA / 'c1c-holds.jsonl'
            records = [json.loads(line) for line in ledger.read_text().splitlines()] if ledger.exists() else []
            if any(record['label'] == label for record in records):
                print(label + ': foreground peer finished; driver will recheck successful phases', flush=True)
                return 2
            time.sleep(5)
        return 2
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
    with (DATA / 'c1c-holds.jsonl').open('a') as out:
        out.write(json.dumps(record) + '\n')
    print(json.dumps({**{k: v for k, v in record.items() if k != 'args'}, 'nodes': len(args)}), flush=True)
    print('\n'.join(line for line in body.splitlines() if not line.startswith('sysctl: '))[-7000:], flush=True)
    return 2 if proc.returncode in (-signal.SIGKILL, -signal.SIGINT) else proc.returncode

if __name__ == '__main__':
    if sys.argv[1] == '--child':
        raise SystemExit(child(sys.argv[2], sys.argv[3:]))
    raise SystemExit(main(sys.argv[1], sys.argv[2:]))
