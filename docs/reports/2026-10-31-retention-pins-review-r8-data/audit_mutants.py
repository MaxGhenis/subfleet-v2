import datetime, json, re
from pathlib import Path
DATA = Path(__file__).resolve().parent
plan = json.loads((DATA / 'mutants.json').read_text())
plan += [d for d in json.loads((DATA / 'r8-followup.json').read_text()) if d['label'].startswith('mutant-')]
legacy = json.loads(Path('docs/reports/2026-10-31-retention-pins-fix-r7-data/mutants.json').read_text())
audit = []
for item in plan:
    d = json.loads((DATA / (item['label'] + '.json')).read_text())
    calls = [t for t in d['tests'] if t['phase'] == 'call']
    name = item['label'].removeprefix('mutant-')
    assertions = [re.findall(r'^>\s+(assert.*)$', t['failure'] or '', re.M) for t in calls]
    old = legacy.get(name, {}).get('previous_log')
    previous = re.findall(r'^>\s+(assert.*)$', Path(old).read_text(), re.M) if old else None
    entry = dict(name=name, args=item['args'], exit=d['exit'], loaded=d['loaded'],
                 failures=sum(t['outcome']=='failed' for t in calls),
                 all_setup_teardown_pass=all(t['outcome']=='passed' for t in d['tests'] if t['phase']!='call'),
                 assertions=assertions, previous_assertions=previous,
                 same_assertions=all(a and a[-1] in previous for a in assertions) if previous is not None else None,
                 start=d['start'], loads=d['loads'])
    assert entry['exit'] == 1 and entry['failures'] > 0 and entry['all_setup_teardown_pass'] and entry['loaded'], entry
    if previous is not None:
        assert entry['same_assertions'], entry
    audit.append(entry)
(DATA / 'mutant-audit.json').write_text(json.dumps(audit, indent=2)+'\n')
lines = ['| Mutant | Named killer | Result |', '|---|---|---|']
for item in audit:
    killer = next(arg for arg in item['args'] if '::' in arg)
    killer = re.sub(r'tests/fake/test_retention_dependency_review_r(\d+)\.py', r'R\1', killer).replace('tests/unit/test_retention_pointer_whitespace.py', 'W').replace('tests/fake/test_retention_pointer_failures.py', 'PF').replace('tests/fake/test_retention_parent_hint.py', 'PH').replace('docs/reports/2026-10-31-retention-pins-fix-r7-data/probes.py', 'P').replace('docs/reports/2026-10-31-retention-pins-review-r8-data/probes_r8.py', 'R8P')
    result = 'Killed; same r6 assertion' if item['same_assertions'] else 'Killed'
    if item['name'] in ('r8-alias-location-recording-dropped', 'r8-pointer-file-location-recording-dropped'):
        result += ' on positive trial baseline'
    lines.append(f'| {item["name"]} | `{killer}` | {result} |')
(DATA / 'mutants.md').write_text('\n'.join(lines)+'\n')
ledger = json.loads((DATA / 'ledger.json').read_text())
events = []
for run in ledger:
    start = datetime.datetime.fromisoformat(run['start']).timestamp()
    events += [(start, 1), (start+run['seconds'], -1)]
active=peak=0
for _,delta in sorted(events):
    active += delta
    peak=max(peak,active)
(DATA / 'run-metrics.json').write_text(json.dumps(dict(peak_pytest_concurrency=peak, named_mutants=20, added_mutants=3, killed=len(audit), legacy_same_assertions=sum(a['same_assertions'] is True for a in audit)), indent=2)+'\n')
print('Verified',len(audit),'killed mutants;',sum(a['same_assertions'] is True for a in audit),'same older assertions; peak pytest concurrency',peak)
