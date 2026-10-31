import os, sys, json
from pathlib import Path
import datetime
_early_data = Path(__file__).resolve().parent
(_early_data / (sys.argv[1] + '-bootstrap.json')).write_text(json.dumps(dict(pid=os.getpid(), parent=os.getppid())) + '\n')
# Mark actual runner admission before importing pytest. Older queued drivers
# otherwise mistake slow initialization for failure to acquire the suite lock.
(_early_data / (sys.argv[1] + '-start.json')).write_text(json.dumps(dict(start=datetime.datetime.now(datetime.UTC).isoformat(), loads=os.getloadavg(), pid=os.getpid(), args=sys.argv[2:], bootstrap=True)) + '\n')
import datetime, hashlib, importlib.abc, importlib.machinery, resource, shutil, signal, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = Path(__file__).resolve().parent
SCRATCH = Path('/private/tmp/sf-r272r8')
ROOT = Path(os.environ.get('SF_SOURCE_ROOT', str(ROOT)))
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
os.environ['SUBFLEET_RETENTION_QOS'] = 'inherit'
label, *args = sys.argv[1:]
additional = DATA / (label + '-extra-args.json')
if additional.exists():
    args.extend(json.loads(additional.read_text()))
if datetime.datetime.now(datetime.UTC) >= datetime.datetime(2026, 10, 31, 9, 19, 30, tzinfo=datetime.UTC):
    (DATA / (label + '-cutoff.json')).write_text(json.dumps(dict(label=label, cutoff=True)) + '\n')
    print('CUTOFF before pytest', label, flush=True)
    sys.exit(3)
loaded = []
definition = json.loads(os.environ['SF_PATCH']) if os.environ.get('SF_PATCH') else None
if definition:
    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            source = Path(self.get_filename(fullname)).read_text()
            mutated = source
            for patch in definition.get('patches', [definition]):
                assert mutated.count(patch['old']) == 1, (fullname, patch['old'], mutated.count(patch['old']))
                mutated = mutated.replace(patch['old'], patch['new'])
            loaded.append(dict(module=fullname, original=hashlib.sha256(source.encode()).hexdigest(), mutated=hashlib.sha256(mutated.encode()).hexdigest()))
            return compile(mutated, self.get_filename(fullname), 'exec')
    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != definition['file'].removesuffix('.py').replace('/', '.'):
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            spec.loader = Loader(fullname, spec.origin)
            return spec
    sys.meta_path.insert(0, Finder())

import tests.failed_keep
tests.failed_keep.ROOT = SCRATCH / 'failed'
import pytest
fixture_root = Path(os.environ.get('SF_FIXTURE_ROOT', str(SCRATCH)))
if label == 'archive-worktree-rerun':
    fixture_root = ROOT / 'sf-r272r8-fixtures'
base = fixture_root / ('cases-' + str(os.getpid()))
base.mkdir(parents=True)
os.environ['TMPDIR'] = str(base)
tempfile.tempdir = None
start = datetime.datetime.now(datetime.UTC).isoformat()
loads = os.getloadavg()
results, nodes, all_nodes, properties = [], [], [], {}
wrapped_modules = set()
class Observer:
    @pytest.hookimpl(tryfirst=True)
    def pytest_collection_modifyitems(self, session, config, items):
        all_nodes.extend(item.nodeid for item in items)
    def pytest_collection_finish(self, session):
        nodes.extend(item.nodeid for item in session.items)
        for item in session.items:
            if item.module.__name__ not in wrapped_modules and hasattr(item.module, 'check_named_own_input'):
                wrapped_modules.add(item.module.__name__)
                for name in ('check_input', 'check_named_own_input'):
                    original = getattr(item.module, name)
                    def observing(base, data, _original=original, _name=name):
                        observation = dict(check=_name, input=data)
                        try:
                            result = _original(base, data)
                            observation.update(result=result, passed=True)
                            return result
                        except BaseException as exc:
                            observation.update(passed=False, error=repr(exc))
                            traceback = exc.__traceback__
                            while traceback is not None:
                                local = traceback.tb_frame.f_locals
                                if traceback.tb_frame.f_code.co_name in ('check_input', 'check_named_own_input'):
                                    observation['failure_evidence'] = {
                                        key: local[key].returncode for key in ('store', 'read') if key in local}
                                    observation['failure_evidence']['recorded'] = local.get('recorded')
                                traceback = traceback.tb_next
                            raise
                        finally:
                            with (DATA / (label + '-inputs.jsonl')).open('a') as stream:
                                stream.write(json.dumps(observation, default=str) + '\n')
                    setattr(item.module, name, observing)
            configured = getattr(item.obj, '_hypothesis_internal_use_settings', None)
            if configured is not None:
                if os.environ.get('SF_PHASE'):
                    from hypothesis import settings, Phase
                    selected = (Phase.explicit,) if os.environ['SF_PHASE'] == 'explicit' else tuple(p for p in configured.phases if p != Phase.explicit)
                    configured = settings(parent=configured, phases=selected)
                    item.obj._hypothesis_internal_use_settings = configured
                from hypothesis.internal.reflection import function_digest
                properties[item.nodeid] = dict(max_examples=configured.max_examples, derandomize=configured.derandomize, database_none=configured.database is None, explicit_examples=len(getattr(item.obj, 'hypothesis_explicit_examples', [])), phases=[str(p) for p in configured.phases], digest=function_digest(item.obj.hypothesis.inner_test).hex())
    def pytest_runtest_logreport(self, report):
        results.append(dict(nodeid=report.nodeid, phase=report.when, outcome=report.outcome, wasxfail=getattr(report, 'wasxfail', None), duration=report.duration, properties=report.user_properties, failure=str(report.longrepr) if report.failed else None))
    def pytest_sessionfinish(self, session, exitstatus):
        from subfleet.home_paths import PYTEST_SENTINEL
        assert (session.config._subfleet_safe_home_base / PYTEST_SENTINEL).is_file()

signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt('480 second hold guard')))
signal.alarm(480)
print(f'START {label} {start} load={loads}', flush=True)
(DATA / (label + '-start.json')).write_text(json.dumps(dict(start=start, loads=loads, pid=os.getpid(), args=args, fixture_root=str(fixture_root))) + '\n')
code = 99
t0 = time.monotonic()
try:
    code = pytest.main(['-q', '--hypothesis-show-statistics', '-p', 'no:cacheprovider', '--basetemp', str(base / 'pytest'), *args], plugins=[Observer()])
finally:
    signal.alarm(0)
    dependency_module = sys.modules.get('subfleet.dependencies')
    dependency_file = Path(dependency_module.__file__) if dependency_module else None
    source = dict(file=str(dependency_file), sha256=hashlib.sha256(dependency_file.read_bytes()).hexdigest()) if dependency_file else None
    (DATA / (label + '.json')).write_text(json.dumps(dict(label=label, start=start, loads=loads, seconds=time.monotonic()-t0, exit=int(code), locked=not bool(os.environ.get('SF_SKIP_LOCK')), tests=results, nodes=nodes, all_nodes=all_nodes, property_settings=properties, loaded=loaded, dependency_source=source, rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss), indent=2) + '\n')
    for folder, directories, _ in os.walk(base, followlinks=False):
        os.chmod(folder, 0o700)
        for directory in directories:
            child = Path(folder) / directory
            if not child.is_symlink():
                os.chmod(child, 0o700)
    shutil.rmtree(base, ignore_errors=True)
sys.exit(code)
