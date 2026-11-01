import collections, json
from pathlib import Path

DATA = Path(__file__).resolve().parent
expected = json.loads((DATA / 'expected-counts.json').read_text())
ledger = []
for filename in DATA.glob('*.json'):
    value = json.loads(filename.read_text())
    if isinstance(value, dict) and all(key in value for key in ('tests', 'start', 'label')):
        ledger.append(value)
ledger.sort(key=lambda d: d['start'])
(DATA / 'ledger.json').write_text(json.dumps(ledger, indent=2) + '\n')
ordinary = [d for d in ledger if not d['label'].startswith(('mutant-', 'generated-', 'r8-trial-', 'r8-red'))]
by_file, failed = collections.defaultdict(dict), []
for run in ordinary:
    for test in run['tests']:
        if test['phase'] == 'call':
            if test['outcome'] == 'passed':
                by_file[test['nodeid'].split('::')[0]][test['nodeid']] = run
            elif test['outcome'] == 'failed':
                failed.append(dict(label=run['label'], node=test['nodeid'], failure=test['failure']))
lines = ['| File | Passed / expected | First successful start UTC | Load 1/5/15 |', '|---|---:|---|---|']
for filename in expected:
    passes = by_file.get(filename, {})
    first = min(passes.values(), key=lambda d: d['start']) if passes else None
    lines.append(f'| {filename} | {len(passes)} / {expected[filename]} | ' +
                 (f'{first["start"][11:19]} | ' + '/'.join(f'{n:.2f}' for n in first['loads']) if first else '— | —') + ' |')
(DATA / 'holds.md').write_text('\n'.join(lines) + '\n')
xfails = {t['nodeid'] for d in ordinary for t in d['tests'] if t['phase']=='call' and t.get('wasxfail')}
summary = dict(requested_expected=sum(expected.values()), requested_passed=sum(len(by_file.get(f, {})) for f in expected),
               head_extra_passed=sum(len(nodes) for f,nodes in by_file.items() if f not in expected),
               head_xfails=sorted(xfails),
               ordinary_failures=failed, completed_runs=len(ledger),
               outside_lock=[d['label'] for d in ledger if not d['locked']],
               max_seconds=max((d['seconds'] for d in ledger), default=0),
               max_rss=max((d['rss'] for d in ledger), default=0))
(DATA / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
print(json.dumps({k:v for k,v in summary.items() if k!='ordinary_failures'}, indent=2))
