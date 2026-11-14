"""Apply each D-RO-ADMIT1 source mutant alone; restore pristine bytes every time."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[3]
DATA = Path(__file__).resolve().parent
SCRATCH = Path('/private/tmp/sf-sbh-b1/mutants')
SCRATCH.mkdir(parents=True, exist_ok=True)
FILES = ('subfleet/task_holds.py', 'subfleet/daemon.py', 'subfleet/store.py')
pristine = {name: (ROOT / name).read_bytes() for name in FILES}
for name, contents in pristine.items():
    (SCRATCH / name.replace('/', '__')).write_bytes(contents)


def replace(name, old, new):
    path = ROOT / name
    source = path.read_text()
    assert source.count(old) == 1, (name, old, source.count(old))
    path.write_text(source.replace(old, new))


def default_instead_of_row():
    replace('subfleet/daemon.py', 'return task_holds.hold_for(job, holds)',
            'return task_holds.hold_for({**job, "sandbox": resolve_sandbox(\n'
            '            self.policy, protocol.POLICY_SANDBOX, task=job["task"],\n'
            '            in_place=bool(job.get("in_place")), git=bool(job.get("workdir_head")),\n'
            '            isolated=bool(job.get("isolated_review")), gate=job["kind"] == "gate-review")}, holds)')


def empty_read_only():
    replace('subfleet/task_holds.py', 'job.get("sandbox") or SANDBOX', 'job.get("sandbox") or "read-only"')
    replace('subfleet/task_holds.py', 'row[2] or SANDBOX', 'row[2] or "read-only"')
    replace('subfleet/daemon.py',
            'COALESCE(NULLIF(sandbox,\'\'),\'workspace-write\')=?',
            'COALESCE(NULLIF(sandbox,\'\'),\'read-only\')=?')


def hold_turn():
    replace('subfleet/task_holds.py', 'if job["kind"] == "turn" or not job["task"]:',
            'if not job["task"]:')
    replace('subfleet/task_holds.py', 'if row is None or row[0] == "turn" or not row[1]:',
            'if row is None or not row[1]:')


def expired_still_holds():
    replace('subfleet/task_holds.py', 'return instant(hold["until"]) > current', 'return True')


def release_task_too():
    replace('subfleet/daemon.py', 'tx.execute("DELETE FROM leases WHERE lease_key=?", (key,))',
            'tx.execute("DELETE FROM leases WHERE lease_key=? OR lease_key LIKE \'task-hold:%\'", (key,))')


def reap_sandbox():
    replace('subfleet/store.py', '"DELETE FROM leases WHERE holder=?", (holder,)).rowcount',
            '"DELETE FROM leases WHERE holder=? OR lease_key LIKE \'sandbox-hold:%\'", (holder,)).rowcount')


PROPERTY = 'tests/fake/test_sandbox_hold_properties.py::'
MUTANTS = [
    ('policy-default', default_instead_of_row, PROPERTY + 'test_sandbox_hold_reads_the_job_row_instead_of_the_task_default'),
    ('empty-read-only', empty_read_only, PROPERTY + 'test_empty_sandbox_is_conservatively_held'),
    ('hold-turn', hold_turn, PROPERTY + 'test_s5_turns_and_jobs_without_a_task_are_never_held'),
    ('expired-holds', expired_still_holds, PROPERTY + 'test_expired_sandbox_hold_stops_nothing_without_being_reaped'),
    ('release-task-too', release_task_too, PROPERTY + 'test_sandbox_release_keeps_an_independent_task_hold'),
    ('reaper-deletes', reap_sandbox, PROPERTY + 'test_sandbox_hold_survives_lease_reapers_and_is_outside_task_range'),
]
records = []
markers = ['row sandbox:', 'empty sandbox:', 'S5: exempt job was held',
           'expired sandbox:', 'sandbox release: task hold was changed', 'reaper deleted sandbox hold']
env = {**os.environ, 'TMPDIR': '/private/tmp/sf-sbh-b1', 'UV_CACHE_DIR': '/private/tmp/sf-sbh-b1/uv-cache',
       'PYTHONDONTWRITEBYTECODE': '1', 'UV_PROJECT_ENVIRONMENT': '/private/tmp/sf-sbh-b1/venv',
       'PYTHONPATH': '/private/tmp/sf-sbh-b1:' + str(ROOT)}
for number, (name, mutate, test) in enumerate(MUTANTS, 1):
    try:
        mutate()
        command = ['uv', '--cache-dir', env['UV_CACHE_DIR'], 'run', '--no-sync', '--with', 'pytest-xdist>=3.6',
                   'pytest', '-q', '-p', 'pytest_scratch', test, '--basetemp', str(SCRATCH / f'pytest-{number}')]
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        (DATA / f'mutant-{number}-{name}.log').write_text(result.stdout + result.stderr)
        records.append({'mutant': number, 'name': name, 'test': test, 'command': command,
                        'exit': result.returncode, 'assertion': markers[number - 1],
                        'killed': result.returncode == 1 and markers[number - 1] in (result.stdout + result.stderr)})
        print(json.dumps(records[-1]), flush=True)
    finally:
        for path, contents in pristine.items():
            (ROOT / path).write_bytes((SCRATCH / path.replace('/', '__')).read_bytes())
            assert (ROOT / path).read_bytes() == contents
        for record in records:
            record['restored_sha256'] = {path: hashlib.sha256(contents).hexdigest()
                                         for path, contents in pristine.items()}
        (DATA / 'mutation-results.json').write_text(json.dumps(records, indent=2) + '\n')
assert len(records) == 6 and all(record['killed'] for record in records), records
