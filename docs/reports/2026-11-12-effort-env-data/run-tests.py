"""Run one requested pytest group with caches, homes and logs contained."""
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import time

DATA = Path(__file__).resolve().parent
REPO = DATA.parents[2]
SCRATCH = Path('/private/tmp/sf-eff-b1')
BASETEMP_ROOT = Path(os.environ.get('SF_EFF_BASETEMP_ROOT', str(SCRATCH)))
assert 'sf-eff-b1' in BASETEMP_ROOT.parts, BASETEMP_ROOT
BOOTSTRAP_NAME = 'sf_eff_b1_processes.pth'


def remove_bootstraps(manifest):
    manifest.with_suffix('.tmp').unlink(missing_ok=True)
    if not manifest.exists():
        return
    for value in json.loads(manifest.read_text()):
        path = Path(value)
        assert 'sf-eff-b1' in path.parts and path.name == BOOTSTRAP_NAME, path
        path.unlink(missing_ok=True)
    manifest.unlink()


def record_bootstraps(manifest, installed):
    temporary = manifest.with_suffix('.tmp')
    temporary.write_text(json.dumps(installed) + '\n')
    os.replace(temporary, manifest)


if sys.argv[1:2] == ['--inner']:
    label, *args = sys.argv[2:]
    assert label and all(c.isalnum() or c in '-_' for c in label)
    manifest = SCRATCH / f'bootstraps-{label}.json'
    assert not manifest.exists(), manifest
    # Guardian/provider environment selection deliberately drops PYTHONPATH and
    # SF_EFF_OWNER_PID. A temporary site bootstrap keeps the same census scope
    # without adding a provider variable or changing its argv. Cover both uv's
    # --with overlay and the project interpreter used by installed entrypoints.
    directories = {Path(sysconfig.get_path(name)).resolve() for name in ('purelib', 'platlib')}
    directories.update(Path(value).resolve() for value in sys.path
                       if value and Path(value).name == 'site-packages')
    directories.update(path.resolve() for path in (SCRATCH / 'venv' / 'lib').glob('python*/site-packages'))
    scoped = sorted(path for path in directories if 'sf-eff-b1' in path.parts)
    assert scoped, directories
    owner = int(os.environ['SF_EFF_OWNER_PID'])
    bootstrap = (f'import sys; sys.path.insert(0, {str(REPO)!r}); '
                 f'sys.path.insert(0, {str(DATA)!r}); '
                 'from sf_eff_processes import install as _sf_eff_install; '
                 f'_sf_eff_install(owner_pid={owner})\n')
    installed = []
    record_bootstraps(manifest, installed)
    try:
        for directory in scoped:
            assert 'sf-eff-b1' in directory.parts and directory.name == 'site-packages', directory
            path = directory / BOOTSTRAP_NAME
            assert not path.exists(), path
            installed.append(str(path))
            record_bootstraps(manifest, installed)
            with path.open('x') as stream:
                stream.write(bootstrap)
        import pytest
        result = pytest.main(['-p', 'sf_eff_safety', '-o', f'cache_dir={SCRATCH / "pytest-cache"}',
                              '--basetemp', str(BASETEMP_ROOT / label), *args])
    finally:
        remove_bootstraps(manifest)
    sys.exit(result)


label, *args = sys.argv[1:]
assert label and all(c.isalnum() or c in '-_' for c in label)
base = BASETEMP_ROOT / label
assert not base.exists(), base
home = SCRATCH / 'test-home'
home.mkdir(exist_ok=True)
env = dict(os.environ, TMPDIR=str(SCRATCH), UV_CACHE_DIR=str(SCRATCH / 'uv-cache'),
           UV_PROJECT_ENVIRONMENT=str(SCRATCH / 'venv'), UV_PYTHON_INSTALL_DIR=str(SCRATCH / 'python'),
           SF_EFF_OWNER_PID=str(os.getpid()), XDG_CACHE_HOME=str(SCRATCH / 'xdg-cache'),
           CLANG_MODULE_CACHE_PATH=str(SCRATCH / 'clang-cache'),
           SWIFT_MODULECACHE_PATH=str(SCRATCH / 'swift-cache'),
           PYTHONDONTWRITEBYTECODE='1', HYPOTHESIS_STORAGE_DIRECTORY=str(SCRATCH / 'hypothesis'),
           PYTHONPATH=os.pathsep.join((str(DATA), str(REPO))), HOME=str(home))
command = ['uv', '--cache-dir', str(SCRATCH / 'uv-cache'), 'run', '--no-sync', '--with',
           'pytest-xdist>=3.6', 'python', str(Path(__file__).resolve()), '--inner', label, *args]
print(json.dumps({'label': label, 'command': command}), flush=True)
start = time.monotonic()
try:
    with (DATA / f'{label}.log').open('w') as log:
        done = subprocess.run(command, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
finally:
    # Also clean if an inner interpreter exits before its own finally block.
    remove_bootstraps(SCRATCH / f'bootstraps-{label}.json')
record = {'label': label, 'command': command, 'rc': done.returncode,
          'basetemp': str(base),
          'seconds': round(time.monotonic() - start, 2)}
(DATA / f'{label}.json').write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps(record), flush=True)
print('\n'.join((DATA / f'{label}.log').read_text().splitlines()[-25:]), flush=True)
sys.exit(done.returncode)
