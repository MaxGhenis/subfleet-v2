"""Record isolated, bounded CI-fix validation commands and their results."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
DATA = Path(__file__).resolve().parent
SCRATCH = Path('/private/tmp/sf-sbh-c1')
name, *args = sys.argv[1:]
args = [value for arg in args for value in (
    Path(arg[1:]).read_text().splitlines() if arg.startswith('@') else [arg])]
env = {**os.environ, 'TMPDIR': str(SCRATCH), 'UV_CACHE_DIR': str(SCRATCH / 'uv-cache'),
       'UV_PROJECT_ENVIRONMENT': str(SCRATCH / 'venv'),
       'UV_PYTHON_INSTALL_DIR': str(SCRATCH / 'python'),
       'PYTHONPYCACHEPREFIX': str(SCRATCH / 'pycache'),
       'HYPOTHESIS_STORAGE_DIRECTORY': str(SCRATCH / 'hypothesis'),
       'PYTHONPATH': str(SCRATCH) + os.pathsep + str(ROOT)}
command = ['uv', '--cache-dir', env['UV_CACHE_DIR'], 'run', '--no-sync',
           '--with', 'pytest-xdist>=3.6', 'pytest', '-q', '-p', 'no:cacheprovider',
           '-p', 'pytest_scratch', '--basetemp', str(SCRATCH / ('pytest-' + name)), *args]
record = {'command': shlex.join(command), 'environment': {k: env[k] for k in (
    'TMPDIR', 'UV_CACHE_DIR', 'UV_PROJECT_ENVIRONMENT', 'UV_PYTHON_INSTALL_DIR',
    'PYTHONPYCACHEPREFIX', 'HYPOTHESIS_STORAGE_DIRECTORY', 'PYTHONPATH')}}
(DATA / (name + '.command.json')).write_text(json.dumps(record, indent=2) + '\n')
start = time.monotonic()
with (DATA / (name + '.log')).open('w') as log:
    result = subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                            stderr=subprocess.STDOUT, timeout=3600)
record.update(returncode=result.returncode, seconds=round(time.monotonic() - start, 2))
(DATA / (name + '.command.json')).write_text(json.dumps(record, indent=2) + '\n')
print((DATA / (name + '.log')).read_text())
raise SystemExit(result.returncode)
