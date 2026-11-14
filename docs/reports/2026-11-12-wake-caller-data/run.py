import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

root = Path('/private/tmp/sf-wc-b1')
test_root = Path(os.environ.get('SF_JOB_TEST_ROOT', '/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261111-122601-wc-build-r1s-a1-uwzfonyi/sf-wc-b1'))
test_root.mkdir(parents=True, exist_ok=True)
label, *command = sys.argv[1:]
env = dict(os.environ, TMPDIR=str(test_root), UV_CACHE_DIR=str(test_root / 'uv-cache'),
           UV_PROJECT_ENVIRONMENT=str(root / 'venv'), GIT_CONFIG_GLOBAL='/dev/null',
           GIT_CONFIG_NOSYSTEM='1', PYTHONPYCACHEPREFIX=str(root / 'pycache'))
source_root = env.get('SF_JOB_SOURCE_ROOT', os.getcwd())
env['PYTHONPATH'] = str(root) + os.pathsep + source_root + os.pathsep + os.getcwd() + os.pathsep + env.get('PYTHONPATH', '')
env['CLANG_MODULE_CACHE_PATH'] = str(root / 'clang-cache')
env['SWIFT_MODULECACHE_PATH'] = str(root / 'swift-cache')
env['SUBFLEET_SWIFT_NESTED_SANDBOX'] = 'off'
env['PYTHONUNBUFFERED'] = '1'
if command and command[0] == 'pytest':
    command = [str(root / 'venv/bin/python'), '-m', 'pytest', '-p', 'sf_job_isolation', *command[1:],
               '--basetemp', str(test_root / ('pytest-' + label + '-' + uuid.uuid4().hex[:10]))]
start = time.time()
log = root / (label + '.log')
with log.open('w') as stream:
    try:
        result = subprocess.run(command, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                timeout=int(env.get('SF_JOB_TIMEOUT_S', '4200')))
        rc = result.returncode
    except subprocess.TimeoutExpired:
        rc = 124
record = {'at': datetime.datetime.fromtimestamp(start, datetime.UTC).isoformat(),
          'label': label, 'command': command, 'rc': rc, 'seconds': round(time.time()-start, 2),
          'log': str(log)}
with (root / 'commands.jsonl').open('a') as stream:
    stream.write(json.dumps(record) + '\n')
print(json.dumps(record), flush=True)
print(log.read_text(errors='replace')[-10000:], flush=True)
sys.exit(rc)
