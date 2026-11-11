"""Ordered, time-bounded requested checks; all state stays in the job's temp child."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / 'docs/reports'
TEMP = Path(os.environ['TMPDIR']) / 'tree-thrash-r1'
PREFIX = '2026-11-06-tree-thrash-fix-r1-'
DEADLINE = datetime(2026, 11, 11, 19, 28, tzinfo=timezone.utc).timestamp()
GROUPS = [
    ('retention-ci', 360, ['tests/fake/test_detached_retention_fence.py']),
    ('ordered-3', 300, [
        'tests/fake/test_admission_tree_thrash.py::test_a_reopened_lane_before_the_sweep_keeps_its_unused_tree',
        'tests/fake/test_admission_tree_thrash.py::test_the_contract_no_git_for_a_waiter_covers_a_lease_arriving_during_preparation',
        'tests/fake/test_admission_tree_thrash.py::test_i1_two_pinned_lanes_can_each_lose_a_prepared_tree_in_one_pass']),
    ('ordered-4', 600, ['tests/fake/test_admission_tree_thrash.py', 'tests/fake/test_admission_tree_thrash_head.py', 'tests/fake/test_unrun_worktrees.py']),
    ('ordered-5', 600, [
        'tests/fake/test_capacity_wait_backoff.py', 'tests/fake/test_admission_latency.py',
        'tests/fake/test_admission_properties.py', 'tests/fake/test_admission_stateful.py',
        'tests/fake/test_admission_visibility.py', 'tests/fake/test_admission_liveness.py',
        'tests/fake/test_admission_route_isolation.py', 'tests/fake/test_task_hold_admission.py',
        'tests/fake/test_workspace_transient.py', 'tests/unit/test_daemon_verbs.py',
        'tests/fake/test_state_contract.py', 'tests/unit/test_test_module_names.py',
        'tests/unit/test_invariants_index.py']),
]
results = []
print('Waiting for ordered retention module completion marker.', flush=True)
while not (REPORTS / (PREFIX + 'retention-ready.json')).exists():
    if time.time() >= DEADLINE:
        break
    time.sleep(5)
for label, budget, modules in GROUPS:
    remaining = DEADLINE - time.time()
    if remaining < 60:
        results.append({'label': label, 'result': 'not run: overall validation budget'})
        continue
    command = ['uv', '--cache-dir', os.environ['UV_CACHE_DIR'], 'run', '--no-sync', '--with',
               'pytest-xdist>=3.6', 'python', '-m', 'pytest', '-n', '4', '-q', *modules,
               '--timeout=1200', '--basetemp', str(TEMP / label), '--durations=0', '--tb=short',
               '--junitxml=' + str(REPORTS / (PREFIX + label + '.xml'))]
    started = time.time()
    print(json.dumps({'starting': label, 'command': command, 'at': started}), flush=True)
    interrupted = False
    with (REPORTS / (PREFIX + label + '.log')).open('w') as output:
        process = subprocess.Popen(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                   env={**os.environ, 'PYTHONUNBUFFERED': '1'}, process_group=0)
        try:
            code = process.wait(timeout=min(budget, remaining))
        except subprocess.TimeoutExpired:
            interrupted = True
            os.killpg(process.pid, signal.SIGINT)
            try:
                code = process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                code = process.wait()
    result = {'label': label, 'command': command, 'exit': code,
              'interrupted_for_budget': interrupted, 'seconds': time.time() - started}
    results.append(result)
    (REPORTS / (PREFIX + 'checks-results.json')).write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(result), flush=True)
(REPORTS / (PREFIX + 'checks-results.json')).write_text(json.dumps(results, indent=2) + '\n')
(REPORTS / (PREFIX + 'checks-ready.json')).write_text(json.dumps({'results': results}) + '\n')
