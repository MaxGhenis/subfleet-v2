"""One locked, foreground pytest, with durable per-test evidence and a 9 minute bound."""
import datetime
import functools
import traceback
import hashlib
import importlib
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
DATA = Path(__file__).resolve().parent
SCRATCH = Path('/private/tmp/sf-fix272r11')
HOLD = '/Users/maxghenis/reviews/subfleet-hub/host-runs/suite_hold.sh'


def now():
    return datetime.datetime.now(datetime.UTC).isoformat()


def stopping(*_):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, stopping)


if sys.argv[1] != '--admitted':
    name, *args = sys.argv[1:]
    # Two foreground queue drivers may share later selections. Reuse only a
    # completed receipt for the same named hold; never run it twice or bypass
    # the suite lock. An exclusive claim also covers their meeting in flight.
    receipt_path = DATA / (name + '.json')
    def completed():
        try:
            value = json.loads(receipt_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        return value if 'exit' in value and 'end' in value else None
    previous = completed()
    if previous is None:
        claim = DATA / (name + '.claim')
        try:
            with claim.open('x') as stream:
                stream.write(str(os.getpid()))
        except FileExistsError:
            while previous is None:
                if datetime.datetime.now(datetime.UTC) >= datetime.datetime.fromisoformat('2026-10-31T22:02:38+00:00'):
                    sys.exit(4)
                time.sleep(1)
                previous = completed()
    if previous is not None:
        print(name, 'reusing completed receipt, exit', previous['exit'], flush=True)
        sys.exit(previous['exit'])
    if datetime.datetime.now(datetime.UTC) >= datetime.datetime.fromisoformat('2026-10-31T22:02:38+00:00'):
        (DATA / (name + '-unrun.json')).write_text(json.dumps(dict(args=args, reason='Four-hour hold cutoff')))
        sys.exit(4)
    split_path = DATA / 'split-holds.json'
    splits = json.loads(split_path.read_text()) if split_path.exists() else {}
    if name in splits:
        receipts = []
        for child_name, child_args in splits[name]:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), child_name, *child_args],
                                     env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
            try:
                code = child.wait()
            except KeyboardInterrupt:
                if child.poll() is None:
                    child.send_signal(signal.SIGINT)
                child.wait()
                raise
            receipts.append(dict(name=child_name, exit=code))
        (DATA / (name + '-split.json')).write_text(json.dumps(dict(name=name, split_into=receipts), indent=2))
        sys.exit(max(r['exit'] for r in receipts))
    linked = '--linked' in args
    if linked:
        args.remove('--linked')
    archive = '--archive-path' in args
    if archive:
        args.remove('--archive-path')
    fixture = (ROOT / 'sf-fix272r11-fixtures' if archive else
               SCRATCH / ('link-tmp' if linked else 'real-tmp') / name)
    fixture.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TMPDIR=str(fixture) + '/',
               HYPOTHESIS_STORAGE_DIRECTORY=str(SCRATCH / 'hyp'),
               PYTHONDONTWRITEBYTECODE='1',
               UV_PROJECT_ENVIRONMENT=str(SCRATCH / 'venv'),
               UV_CACHE_DIR=str(SCRATCH / 'uv-cache'))
    env.pop('SUBFLEET_RETENTION_DORMANT', None)
    env.pop('PYTHONPATH', None)
    command = ['/bin/zsh', HOLD, 'env', '-u', 'SUBFLEET_RETENTION_DORMANT', '-u', 'PYTHONPATH',
               'uv', 'run', '--no-sync', 'python', str(Path(__file__).resolve()), '--admitted', name, *args]
    (DATA / (name + '-queued.json')).write_text(json.dumps(dict(queued=now(), args=args,
                                                              tmpdir=str(fixture), linked=linked), indent=2))
    with (DATA / (name + '.log')).open('w') as stream:
        process = subprocess.Popen(command, env=env, stdout=stream, stderr=subprocess.STDOUT, process_group=0)
        queued = json.loads((DATA / (name + '-queued.json')).read_text())
        queued.update(driver_pid=os.getpid(), lock_process_group=process.pid)
        (DATA / (name + '-queued.json')).write_text(json.dumps(queued, indent=2))
        try:
            try:
                rc = process.wait(timeout=1800)
            except subprocess.TimeoutExpired:
                if (DATA / (name + '.json')).exists():
                    rc = process.wait(timeout=600)
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait()
                    print('No admission in 30 minutes; running directly', file=stream, flush=True)
                    env['SF_FIX_R11_DIRECT'] = '1'
                    rc = subprocess.call(command[2:], env=env, stdout=stream, stderr=subprocess.STDOUT)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait()
    print(name, 'exit', rc, flush=True)
    sys.exit(rc)

_, name, *args = sys.argv[1:]
if datetime.datetime.now(datetime.UTC) >= datetime.datetime.fromisoformat('2026-10-31T22:02:38+00:00'):
    (DATA / (name + '-unrun.json')).write_text(json.dumps(dict(args=args,
        reason='Four-hour cutoff before pytest admission'), indent=2))
    sys.exit(4)
os.environ['SF_R11_HOLD_NAME'] = name
SOURCE = Path(os.environ.get("SF_R11_SOURCE", str(ROOT)))
os.chdir(SOURCE)
sys.path.insert(0, str(SOURCE))
import pytest
plugins = []
if '--differential' in args:
    args.remove('--differential')
    import differential_plugin
    plugins.append(differential_plugin)
# Keep the harness's failure-root pruning and any copied fake stores inside
# this job's scratch, rather than inspecting other pytest processes' roots.
from tests import failed_keep
failed_keep.ROOT = SCRATCH / 'failed-keep'
record = dict(name=name, start=now(), load=os.getloadavg(), pytest_pid=os.getpid(),
              failure_root=str(failed_keep.ROOT), args=args, phases=[], observations=[],
              locked=os.environ.get('SF_FIX_R11_DIRECT') != '1')
start = time.monotonic()


class Evidence:
    def pytest_collection_finish(self, session):
        record['collected'] = [item.nodeid for item in session.items]
        module = sys.modules.get('subfleet.dependencies')
        if module is not None:
            record['product_file'] = module.__file__
            record['product_sha256'] = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        for item in session.items:
            if (item.nodeid.startswith('tests/unit/test_retention_dependency_properties.py::')
                    and hasattr(item.obj, 'hypothesis')):
                original = item.obj.hypothesis.inner_test
                progress = dict(passed=0, failures=[], interrupted=0)
                record.setdefault('example_progress', {})[item.nodeid] = progress
                @functools.wraps(original)
                def observed(*args, original=original, progress=progress, **kwargs):
                    try:
                        result = original(*args, **kwargs)
                        progress['passed'] += 1
                        return result
                    except KeyboardInterrupt:
                        progress['interrupted'] += 1
                        raise
                    except BaseException as exc:
                        progress['failures'].append(dict(arguments=repr(kwargs or args),
                            error=repr(exc), traceback=traceback.format_exc()))
                        raise
                    finally:
                        if progress['failures'] or progress['interrupted'] or progress['passed'] % 10 == 0:
                            (DATA / (name + '.json')).write_text(json.dumps(record, indent=2))
                item.obj.hypothesis.inner_test = observed
        (DATA / (name + '.json')).write_text(json.dumps(record, indent=2))

    def pytest_runtest_logreport(self, report):
        record['phases'].append(dict(node=report.nodeid, phase=report.when, outcome=report.outcome,
                                     duration=report.duration, wasxfail=getattr(report, 'wasxfail', None),
                                     failure=str(report.longrepr) if report.failed else None))
        if report.when == 'teardown':
            record['observations'].extend(dict(node=report.nodeid, key=k, value=v)
                                          for k, v in report.user_properties)
        (DATA / (name + '.json')).write_text(json.dumps(record, indent=2))


if '--input-head' in args:
    args.remove('--input-head')
    module = importlib.import_module('subfleet.dependencies')
    snapshot = (DATA / 'ced-dependencies.py.txt').read_text()
    exec(compile(snapshot, 'ced02912b:subfleet/dependencies.py', 'exec'), module.__dict__)
    record['input_head_sha256'] = hashlib.sha256(snapshot.encode()).hexdigest()


if args and args[0] == '--mutant':
    definitions = json.loads((DATA / 'mutants.json').read_text())
    mutation = definitions[args[1]]
    args = args[2:]
    hashes = []
    sources = {}
    for edit in mutation['edits']:
        module = importlib.import_module(edit['module'])
        source = sources.get(edit['module'], Path(module.__file__).read_text())
        assert source.count(edit['old']) == 1, (edit, source.count(edit['old']))
        changed = source.replace(edit['old'], edit['new'])
        sources[edit['module']] = changed
        exec(compile(changed, module.__file__, 'exec'), module.__dict__)
        hashes.append(dict(module=edit['module'], original=hashlib.sha256(source.encode()).hexdigest(),
                           mutated=hashlib.sha256(changed.encode()).hexdigest()))
    record['mutation_hashes'] = hashes
(DATA / (name + '.json')).write_text(json.dumps(record, indent=2))
signal.signal(signal.SIGALRM, lambda *_: signal.raise_signal(signal.SIGINT))
signal.alarm(540)
try:
    rc = pytest.main(['-q', '-p', 'no:cacheprovider', '--tb=short', *args], plugins=[Evidence(), *plugins])
finally:
    signal.alarm(0)
    record.update(end=now(), seconds=time.monotonic() - start,
                  peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20)
record['exit'] = int(rc)
(DATA / (name + '.json')).write_text(json.dumps(record, indent=2))
print(json.dumps({k: record[k] for k in ('name', 'start', 'load', 'seconds', 'exit')}), flush=True)
sys.exit(rc)
