import datetime, json, os, signal, subprocess, sys, time
from pathlib import Path
DATA = Path(__file__).resolve().parent
SCRATCH = Path('/private/tmp/sf-r272r8')
plan = json.loads(Path(sys.argv[1]).read_text())
for job in plan:
    label = job['label']
    if datetime.datetime.now(datetime.UTC) >= datetime.datetime(2026, 10, 31, 9, 19, 30, tzinfo=datetime.UTC):
        print('CUTOFF', label, flush=True)
        break
    case = SCRATCH / ('hold-' + label)
    case.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(TMPDIR=str(case), HYPOTHESIS_STORAGE_DIRECTORY=str(SCRATCH/'hyp'), UV_PROJECT_ENVIRONMENT=str(SCRATCH/'venv'), UV_CACHE_DIR=str(SCRATCH/'uv-cache'), PYTHONDONTWRITEBYTECODE='1')
    env.pop('SUBFLEET_RETENTION_DORMANT', None)
    env.pop('PYTHONPATH', None)
    env.update(job.get('env', {}))
    if 'patch' in job:
        env['SF_PATCH'] = json.dumps(job['patch'])
    command = ['uv', 'run', '--no-sync', 'python', str(DATA/'run_hold.py'), label, *job['args']]
    queued = dict(label=label, queued=datetime.datetime.now(datetime.UTC).isoformat(), args=job['args'])
    (DATA/(label+'-queue.json')).write_text(json.dumps(queued)+'\n')
    print('QUEUE', label, flush=True)
    with (DATA/(label+'.log')).open('w') as log:
        if job.get('direct'):
            env['SF_SKIP_LOCK'] = '1'
        invocation = command if job.get('direct') else ['/bin/zsh', '/Users/maxghenis/reviews/subfleet-hub/host-runs/suite_hold.sh', *command]
        child = subprocess.Popen(invocation, env=env, stdout=log, stderr=subprocess.STDOUT)
        queued['pid'] = child.pid
        (DATA/(label+'-queue.json')).write_text(json.dumps(queued)+'\n')
        t0 = time.monotonic()
        while child.poll() is None:
            if not env.get('SF_SKIP_LOCK') and time.monotonic()-t0 > 1800 and not (DATA/(label+'-start.json')).exists():
                child.terminate()
                child.wait()
                # A queued wrapper's current bounded lock probe/sleep ends on
                # its own; no pytest has started and it holds no suite slot.
                time.sleep(6)
                print('FALLBACK', label, flush=True)
                env['SF_SKIP_LOCK'] = '1'
                child = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
                queued['fallback_pid'] = child.pid
                (DATA/(label+'-queue.json')).write_text(json.dumps(queued)+'\n')
                t0 = time.monotonic()
            time.sleep(1)
        print('END', label, 'rc', child.returncode, flush=True)
