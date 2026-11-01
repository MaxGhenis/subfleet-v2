"""Final receipt audit; no pytest, providers or live stores are started."""
import hashlib
import json
from pathlib import Path
import subprocess

D = Path(__file__).resolve().parent
R = D.parents[2]
expected = {'red-r9', 'start-before', 'green-r9', 'green-spelling',
            'green-spelling-complete', 'green-spelling-final'}
for plan in ['required-plan.json', 'second-plan.json', 'additional-required-plan.json',
             'archive-retry-plan.json', 'mutants-plan.json']:
    expected.update(name for name, _ in json.loads((D / plan).read_text()))
mutations = json.loads((D / 'mutants.json').read_text())
expected.update('revert-' + name for name in mutations if name.startswith('r9-'))
runs = {}
for name in expected:
    run = json.loads((D / (name + '.json')).read_text())
    assert 'end' in run and run['locked'], name
    for node in run['collected']:
        phases = [phase for phase in run['phases'] if phase['node'] == node]
        assert [phase['phase'] for phase in phases] == ['setup', 'call', 'teardown'], (name, node)
        assert phases[0]['outcome'] == phases[2]['outcome'] == 'passed', (name, node)
    if not name.startswith(('red-', 'mutant-', 'revert-')) and name not in {'archive-0', 'archive-4'}:
        assert run['exit'] == 0, name
    assert run['seconds'] < 600 and run['peak_rss_mib'] < 8192, name
    runs[name] = run

summary = json.loads((D / 'summary.json').read_text())
assert not summary['unresolved']
assert summary['completed_runs'] == len(expected)
assert len(json.loads((D / 'mutant-results.json').read_text())) == 13
assert all(row['same_r8_assertion'] for row in json.loads((D / 'mutant-results.json').read_text())
           if row['name'].startswith('r8-'))
assert len(json.loads((D / 'reversion-results.json').read_text())) == 4
classes = json.loads((D / 'class-counts.json').read_text())
assert classes['inputs'] == 55 and len(classes['classes']) == 33 and not classes['skips']

preserved = {}
for relative in ['docs/acceptance-contract.md', 'tests/unit/test_retention_location_inputs.py']:
    actual = (R / relative).read_bytes()
    assert actual == subprocess.check_output(['git', 'show', '236c80883:' + relative], cwd=R)
    preserved[relative] = hashlib.sha256(actual).hexdigest()
relative = 'tests/fake/test_retention_dependency_review_r9.py'
baseline = subprocess.check_output(['git', 'show', '236c80883:' + relative], cwd=R, text=True)
assert (R / relative).read_text() == ''.join(line for line in baseline.splitlines(keepends=True)
                                          if not line.startswith('@pytest.mark.xfail('))
(D / 'audit.json').write_text(json.dumps(dict(expected_holds=len(expected), all_complete=True,
    all_locked=True, all_setup_teardown_passed=True, no_unresolved_ordinary_failures=True,
    every_hold_under_ten_minutes=True, preserved_sha256=preserved), indent=2) + '\n')
print('Audited', len(expected), 'completed locked holds')
