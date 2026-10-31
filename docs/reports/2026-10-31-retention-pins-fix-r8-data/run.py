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

ROOT = Path(__file__).resolve().parents[3]
DATA = Path(__file__).resolve().parent
SCRATCH = Path('/private/tmp/sf-fix272r8')
HOLD = '/Users/maxghenis/reviews/subfleet-hub/host-runs/suite_hold.sh'


def now():
    return datetime.datetime.now(datetime.UTC).isoformat()


if sys.argv[1] != '--admitted':
    name, *args = sys.argv[1:]
    linked = '--linked' in args
    if linked:
        args.remove('--linked')
    archive = '--archive-path' in args
    if archive:
        args.remove('--archive-path')
    fixture = (ROOT / 'archive-fixtures' if archive else
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
                    env['SF_FIX272R8_DIRECT'] = '1'
                    rc = subprocess.call(command[2:], env=env, stdout=stream, stderr=subprocess.STDOUT)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait()
    print(name, 'exit', rc, flush=True)
    sys.exit(rc)

_, name, *args = sys.argv[1:]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
import pytest
record = dict(name=name, start=now(), load=os.getloadavg(), args=args, phases=[], observations=[],
              locked=os.environ.get('SF_FIX272R8_DIRECT') != '1')
start = time.monotonic()


class Evidence:
    def pytest_collection_finish(self, session):
        record['collected'] = [item.nodeid for item in session.items]
        (DATA / (name + '.json')).write_text(json.dumps(record, indent=2))

    def pytest_runtest_logreport(self, report):
        record['phases'].append(dict(node=report.nodeid, phase=report.when, outcome=report.outcome,
                                     duration=report.duration,
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
