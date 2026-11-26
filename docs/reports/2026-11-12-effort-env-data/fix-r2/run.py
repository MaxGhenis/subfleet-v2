"""Record this round's bounded test runs without inspecting inherited values."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

SCRATCH = Path(os.environ['SF_EFF_SCRATCH'])
EVIDENCE = Path(__file__).resolve().parent
label, *arguments = sys.argv[1:]
base = SCRATCH / ('pytest-' + label)
assert not base.exists(), base
environment = dict(os.environ)
environment.update(
    TMPDIR=subprocess.check_output(['getconf', 'DARWIN_USER_TEMP_DIR'], text=True).strip(),
    UV_CACHE_DIR=str(SCRATCH / 'uv-cache'),
    UV_PROJECT_ENVIRONMENT=str(SCRATCH / 'venv'),
    PYTHONDONTWRITEBYTECODE='1',
    PYTHONUNBUFFERED='1',
    HYPOTHESIS_STORAGE_DIRECTORY=str(SCRATCH / 'hypothesis'),
    SUBFLEET_V1_RUNNER=str(SCRATCH / 'missing-v1-runner'),
)
environment.pop('PYTHONPATH', None)
command = ['uv', 'run', '--no-sync', '--with', 'pytest-xdist>=3.6', 'python', '-m', 'pytest',
           '-p', 'no:cacheprovider', '-p', 'sf_eff_fix2_safety',
           '--timeout=90', '--timeout-method=thread', '--basetemp=' + str(base), *arguments]
started = datetime.datetime.now(datetime.timezone.utc)
with (EVIDENCE / (label + '.log')).open('w') as log:
    with subprocess.Popen(command, env=environment, stdout=log, stderr=subprocess.STDOUT) as child:
        try:
            returncode = child.wait()
        except KeyboardInterrupt:
            # Let pytest finish its owned workers/fixtures before another run.
            try:
                child.wait(timeout=45)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            returncode = 130
finished = datetime.datetime.now(datetime.timezone.utc)
record = dict(command=command, exit_code=returncode,
              started=started.isoformat(), finished=finished.isoformat())
(EVIDENCE / (label + '.json')).write_text(json.dumps(record, indent=2) + '\n')
lines = (EVIDENCE / (label + '.log')).read_text().splitlines()
print(json.dumps(dict(label=label, exit_code=returncode,
                      started=record['started'], finished=record['finished'])))
print('\n'.join(lines[-12:]))
sys.exit(returncode)
