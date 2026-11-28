"""Finish the ordinary holds, then rerun only the six known archive path controls."""
import json
from pathlib import Path
import sys
from batch import call_waited

DATA = Path(__file__).resolve().parent
runs = []
for path in DATA.glob('*.json'):
    value = json.loads(path.read_text())
    if isinstance(value, dict) and 'phases' in value and 'end' in value:
        runs.append(value)
passed = {p['node'] for r in runs if not r['name'].startswith(('red-', 'mutant-'))
          for p in r['phases'] if p['phase'] == 'call' and p['outcome'] == 'passed'}
partial = {}
resume = []
scheduled = set()
for run in sorted(runs, key=lambda r: (r['name'].startswith('dependency-property'), r['start'])):
    if run['exit'] != 2 or run['name'].startswith(('red-', 'mutant-')):
        continue
    assert run['seconds'] >= 535, run['name']
    remaining = [n for n in run['collected'] if n not in passed]
    initial = {p['node'] for p in run['phases'] if p['phase'] == 'call' and p['outcome'] == 'passed'}
    partial[run['name']] = dict(reason='Nine-minute pytest guard', remaining=remaining, initial_unfinished=[n for n in run['collected'] if n not in initial])
    remaining = [n for n in remaining if n not in scheduled]
    scheduled.update(remaining)
    memory = [n for n in remaining if 'many_short_valid_lines_use_bounded_input_memory' in n]
    rest = [n for n in remaining if n not in memory]
    groups = [[n] for n in memory] + [rest[i:i+5] for i in range(0, len(rest), 5)]
    for i, group in enumerate(groups):
        resume.append(['resume-' + run['name'] + '-' + str(i), group])
(DATA / 'partial-holds.json').write_text(json.dumps(partial, indent=2) + '\n')
(DATA / 'resume-plan.json').write_text(json.dumps(resume, indent=2) + '\n')
if resume and "--plan-only" not in sys.argv:
    rc = call_waited([sys.executable, str(DATA / 'batch.py'), 'resume-plan.json'])
    assert rc == 0, rc
pending = []
for name, args in json.loads((DATA / 'follow-up-tests-plan.json').read_text()):
    receipt = DATA / (name + '.json')
    if receipt.exists() and json.loads(receipt.read_text()).get('exit') == 0:
        continue
    pending.append([name, args])
(DATA / 'follow-up-pending-plan.json').write_text(json.dumps(pending, indent=2) + '\n')
if '--plan-only' not in sys.argv:
    rc = call_waited([sys.executable, str(DATA / 'batch.py'), 'follow-up-pending-plan.json'])
    assert rc == 0, rc
known = {
 'test_round_trip_dirty_unpushed_detached_job_restores_every_byte_and_commit',
 'test_blob_only_in_a_local_branch_is_archived_not_omitted',
 'test_local_path_remote_is_not_a_remote',
 'test_borrowed_or_shallow_object_store_is_scratch[alternates]',
 'test_borrowed_or_shallow_object_store_is_scratch[shallow]',
 'test_survey_is_read_only_and_predicts_the_pass',
}
failed = set()
for name in ['archive-' + str(i) for i in range(7)]:
    run = json.loads((DATA / (name + '.json')).read_text())
    failed.update(p['node'] for p in run['phases'] if p['outcome'] == 'failed')
assert {n.split('::')[-1] for n in failed} <= known, sorted(failed)
control = DATA / 'archive-path-controls.json'
covered = set()
if control.exists():
    prior = json.loads(control.read_text())
    if prior.get('exit') == 0:
        covered = {p['node'] for p in prior['phases'] if p['phase']=='call' and p['outcome']=='passed'}
remaining = failed - covered
plan = [['archive-path-controls-final', ['--archive-path', *sorted(remaining)]]] if remaining else []
(DATA / 'retry-plan.json').write_text(json.dumps(plan, indent=2) + '\n')
if plan and "--plan-only" not in sys.argv:
    rc = call_waited([sys.executable, str(DATA / 'batch.py'), 'retry-plan.json'])
    assert rc == 0, rc
