import json, os, sys, tempfile, time
from pathlib import Path
ROOT = Path('/private/tmp/sf-a2-fix1')
REPO = Path.cwd()
sys.path.insert(0, str(REPO))
sys.path.insert(0, '/private/tmp/sf-a2-fix1/historical-cli')
import subfleet.cli
run_id = str(os.getpid())
run_root = ROOT / ('t-' + run_id)
run_root.mkdir()
original_mkdtemp = tempfile.mkdtemp
outside = []
def job_mkdtemp(suffix=None, prefix=None, dir=None):
    if dir is not None and Path(dir).resolve() == Path('/private/tmp'):
        dir = str(run_root)
    if dir is not None and not Path(dir).resolve().is_relative_to(ROOT):
        prefix = 'sf-a2-fix1-' + (prefix or '')
    result = original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)
    if not Path(result).resolve().is_relative_to(ROOT):
        outside.append(result)
        with (ROOT / 'outside.jsonl').open('a') as f:
            f.write(json.dumps({'path':result}) + '\n')
    return result
tempfile.mkdtemp = job_mkdtemp
import shutil
original_rmtree = shutil.rmtree
def job_rmtree(path, *args, **kwargs):
    if str(path) in outside:
        assert 'sf-a2-fix1' in str(path)
    return original_rmtree(path, *args, **kwargs)
shutil.rmtree = job_rmtree
tempfile.tempdir = str(run_root)
os.environ['TMPDIR'] = str(run_root)
from tests import failed_keep
failed_keep.ROOT = run_root / 'failed'
import pytest
class Safety:
    @pytest.hookimpl(trylast=True)
    def pytest_configure(self, config):
        # Inherited runtime markers must not trigger real ancestry inspection.
        for name in ('CLAUDECODE','CLAUDE_CODE_SESSION_ID','CLAUDE_PID','CODEX_THREAD_ID','CODEX_SESSION_ID','SUBFLEET_CALLER_FAMILY'):
            os.environ.pop(name, None)
    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        self.counts = dict(session.config.pluginmanager.get_plugin('terminalreporter').stats)
plugin = Safety()
started=time.monotonic()
args=sys.argv[1:]
try:
    code=pytest.main(args, plugins=[plugin])
finally:
    import shutil, stat
    def fix(fn, path, exc):
        os.chmod(path, stat.S_IRWXU)
        fn(path)
    assert 'sf-a2-fix1' in str(run_root) and run_root.parent == ROOT
    shutil.rmtree(run_root, onexc=fix)
record={'args':args, 'exit':int(code), 'seconds':round(time.monotonic()-started,2), 'counts':{k:len(v) for k,v in getattr(plugin,'counts',{}).items()}}
with (ROOT/'ledger.jsonl').open('a') as f:
    f.write(json.dumps(record)+'\n')
print('LEDGER',json.dumps(record),flush=True)
raise SystemExit(code)
