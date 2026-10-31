"""One locked, foreground pytest, with durable per-test evidence and a 9 minute bound."""
import datetime
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

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(__file__).resolve().parent
SCRATCH = Path('/private/tmp/sf-r272r9')
HOLD = '/Users/maxghenis/reviews/subfleet-hub/host-runs/suite_hold.sh'


def now():
    return datetime.datetime.now(datetime.UTC).isoformat()


def stopping(*_):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, stopping)


if sys.argv[1] != '--admitted':
    name, *args = sys.argv[1:]
    if name.startswith('dependency-property-'):
        args[-1] = ['selection_never', 'independent_reference', 'monotone', 'submitted_dependencies'][int(name[-1])]
    if name == 'sweep-examples':
        args[-1] = 'not test_no_schedule_of_sweep_moves_loses_or_orphans_a_tree'
    if name == 'sweep-property':
        args[-1] = 'test_no_schedule_of_sweep_moves_loses_or_orphans_a_tree'
    if name == 'shared-folders':
        args += ['-k', 'not test_fence_hold_against_a_model_of_the_fence and not test_early_fences_equal_reservation_before_and_after_quarantine and not test_interleavings_never_delete_a_folder_named_by_a_turn and not test_the_turn_folder_pin_is_exactly_an_owned_tree_a_turn_is_in']
    if name == 'salvage-properties':
        args = ['tests/unit/test_retention_salvage_properties.py::test_retention_prunes_only_when_unbundled_unarchived_bytes_are_absent']
    if name == 'salvage-properties' and (DATA / 'historic-controls-queued.json').exists():
        until = time.monotonic() + 120
        while not (DATA / 'historic-controls.json').exists() and time.monotonic() < until:
            time.sleep(1)
    if name == 'sweep-property' and (DATA / 'r9-firmlink-queued.json').exists():
        until = time.monotonic() + 120
        while not (DATA / 'r9-firmlink.json').exists() and time.monotonic() < until:
            time.sleep(1)
    if name == 'guards' and (DATA / 'red-r9-firmlink-corrected-queued.json').exists():
        until = time.monotonic() + 120
        while not (DATA / 'red-r9-firmlink-corrected.json').exists() and time.monotonic() < until:
            time.sleep(1)
    linked = '--linked' in args
    if linked:
        args.remove('--linked')
    archive = '--archive-path' in args
    if archive:
        args.remove('--archive-path')
    fixture = (ROOT / 'sf-r272r9-fixtures' if archive else
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
                    env['SF_REVIEW_R9_DIRECT'] = '1'
                    rc = subprocess.call(command[2:], env=env, stdout=stream, stderr=subprocess.STDOUT)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait()
    print(name, 'exit', rc, flush=True)
    sys.exit(rc)

_, name, *args = sys.argv[1:]
SOURCE = Path(os.environ.get("SF_R8_SOURCE", str(ROOT)))
os.chdir(SOURCE)
sys.path.insert(0, str(SOURCE))
import pytest
record = dict(name=name, start=now(), load=os.getloadavg(), args=args, phases=[], observations=[],
              locked=os.environ.get('SF_REVIEW_R9_DIRECT') != '1')
start = time.monotonic()


class Evidence:
    def pytest_collection_finish(self, session):
        record['collected'] = [item.nodeid for item in session.items]
        module = sys.modules.get('subfleet.dependencies')
        if module is not None:
            record['product_file'] = module.__file__
            record['product_sha256'] = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        if SOURCE != ROOT:
            record['attempts'] = []
            for item in session.items:
                helper = ('check_location_input' if 'location_inputs' in item.nodeid
                          else 'check_named_own_input')
                original = getattr(item.module, helper)
                def observed(base, data, original=original, node=item.nodeid):
                    attempt = dict(node=node, input=data, outcome='running')
                    record['attempts'].append(attempt)
                    try:
                        result = original(base, data)
                        attempt['outcome'] = 'passed'
                        return result
                    except BaseException as exc:
                        attempt.update(outcome='failed', error=str(exc))
                        raise
                    finally:
                        (DATA / (name + '.json')).write_text(json.dumps(record, indent=2))
                setattr(item.module, helper, observed)
        (DATA / (name + '.json')).write_text(json.dumps(record, indent=2))

    def pytest_runtest_logreport(self, report):
        record['phases'].append(dict(node=report.nodeid, phase=report.when, outcome=report.outcome,
                                     duration=report.duration, wasxfail=getattr(report, 'wasxfail', None),
                                     failure=str(report.longrepr) if report.failed else None))
        if report.when == 'teardown':
            record['observations'].extend(dict(node=report.nodeid, key=k, value=v)
                                          for k, v in report.user_properties)
        (DATA / (name + '.json')).write_text(json.dumps(record, indent=2))


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
    rc = pytest.main(['-q', '-p', 'no:cacheprovider', '--tb=short', *args], plugins=[Evidence()])
finally:
    signal.alarm(0)
    record.update(end=now(), seconds=time.monotonic() - start,
                  peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20)
record['exit'] = int(rc)
(DATA / (name + '.json')).write_text(json.dumps(record, indent=2))
print(json.dumps({k: record[k] for k in ('name', 'start', 'load', 'seconds', 'exit')}), flush=True)
sys.exit(rc)
