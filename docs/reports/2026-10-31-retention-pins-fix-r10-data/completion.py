"""Verify the requested run manifest against durable per-phase receipts."""
import json
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent
plans = ['required-plan.json', 'follow-up-tests-plan.json', 'retry-plan.json', 'resume-plan.json', 'early-resume-plan.json', 'diagnostic-plan.json', 'archive-control-plan.json', 'final-shared-plan.json', 'mutants-plan.json']
splits = json.loads((DATA / 'split-holds.json').read_text())
expected = [child for plan in plans for name, _ in json.loads((DATA / plan).read_text())
            for child in ([n for n, _ in splits[name]] if name in splits else [name])]
expected = list(dict.fromkeys(expected))
completed = {}
for path in DATA.glob('*.json'):
    v = json.loads(path.read_text())
    if isinstance(v, dict) and 'phases' in v and 'end' in v:
        completed[v['name']] = v
missing = sorted(set(expected) - completed.keys())
assert not missing, missing
assert all(completed[n]['start'] < '2026-10-31T15:33:45' for n in expected)
assert all(completed[n]['locked'] for n in expected)
partial = json.loads((DATA / 'partial-holds.json').read_text())
assert all(completed[n]['exit'] in (0, 1) or n in partial for n in expected), {n: completed[n]['exit'] for n in expected if completed[n]['exit'] not in (0, 1) and n not in partial}
assert all(completed[n]['seconds'] < 600 for n in expected)
base_hash = json.loads((DATA / 'byte-audit.json').read_text())['baseline_snapshot']['sha256']
for name in ['red-review-r10', 'red-cycles-final']:
    assert completed[name]['product_sha256'] == base_hash
failures = {}
passed = set()
for name, run in completed.items():
    if name.startswith(('mutant-', 'red-')):
        continue
    for phase in run['phases']:
        if phase['phase'] == 'call' and phase['outcome'] == 'passed':
            passed.add(phase['node'])
        if phase['outcome'] == 'failed':
            failures[phase['node']] = name
assert not (set(failures) - passed), {k:v for k,v in failures.items() if k not in passed}
unfinished = sorted({n for name in partial for n in completed[name]['collected']} - passed)
if '--allow-incomplete' not in sys.argv:
    assert not unfinished, unfinished
archive_nodes = {n.split('::')[1].split('[')[0] for n in passed if n.startswith('tests/unit/test_retention_archive.py::')}
import ast
source = DATA.parents[2] / 'tests/unit/test_retention_archive.py'
required_archive = {n.name for n in ast.parse(source.read_text()).body
                    if isinstance(n, ast.FunctionDef) and n.name.startswith('test_')}
required_archive.remove('test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree')
assert archive_nodes == required_archive, (archive_nodes ^ required_archive)
summary = dict(expected_holds=len(expected), completed_holds=len(completed), missing=[],
               outside_lock=[], all_starts_before_cutoff=True,
               unfinished_nodes=unfinished, coverage_complete=not unfinished,
               all_pytests_under_ten_minutes=True, unresolved=[],
               covered_archive_functions=len(archive_nodes),
               intentional_live_lsof_exclusion=True)
(DATA / 'completion.json').write_text(json.dumps(summary, indent=2) + '\n')
print(json.dumps(summary, indent=2))
