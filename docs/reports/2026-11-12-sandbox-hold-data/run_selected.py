"""One xdist run of every hold-referencing test module plus CAS integration."""
import os
import shlex
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[3]
data = Path(__file__).resolve().parent
paths = (data / 'selected-tests.txt').read_text().splitlines()
run = os.environ.get('SF_SBH_RUN', '1')
env = {**os.environ, 'TMPDIR': '/private/tmp/sf-sbh-b1', 'UV_CACHE_DIR': '/private/tmp/sf-sbh-b1/uv-cache',
       'UV_PROJECT_ENVIRONMENT': '/private/tmp/sf-sbh-b1/venv',
       'PYTHONPATH': '/private/tmp/sf-sbh-b1:' + str(root)}
command = ['uv', '--cache-dir', env['UV_CACHE_DIR'], 'run', '--no-sync', '--with', 'pytest-xdist>=3.6',
           'pytest', '-q', '-n', '4', '-p', 'pytest_scratch', *paths,
           '--basetemp', f'/private/tmp/sf-sbh-b1/pytest-selected-{run}']
(data / f'selected-command-{run}.txt').write_text(shlex.join(command) + '\n')
with (data / f'selected-{run}.log').open('w') as log:
    result = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=7200)
print((data / f'selected-{run}.log').read_text())
raise SystemExit(result.returncode)
