"""Build the report tables solely from this round's durable receipts."""
import collections
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent
runs = []
for path in DATA.glob('*.json'):
    value = json.loads(path.read_text())
    if isinstance(value, dict) and 'phases' in value and 'end' in value:
        runs.append(value)
runs.sort(key=lambda run: run['start'])
superseded = {'green-spelling', 'green-spelling-complete', 'linked-test_retention_location_spelling'}
ordinary = [run for run in runs if not run['name'].startswith(('red-', 'mutant-', 'revert-'))
            and run['name'] not in superseded]
passing = collections.defaultdict(set)
first = {}
all_failed = {}
for run in ordinary:
    for phase in run['phases']:
        node = phase['node']
        if phase['phase'] == 'call' and phase['outcome'] == 'passed':
            filename = node.split('::')[0]
            passing[filename].add(node)
            first.setdefault(filename, run)
        if phase['outcome'] == 'failed':
            all_failed[node] = run['name']
unresolved = {node: run for node, run in all_failed.items()
              if node not in passing[node.split('::')[0]]}
summary = dict(completed_runs=len(runs), ordinary_runs=len(ordinary),
               ordinary_passing_nodes=sum(map(len, passing.values())),
               superseded=sorted(superseded), unresolved=unresolved,
               longest_seconds=max(run['seconds'] for run in runs),
               peak_rss_mib=max(run['peak_rss_mib'] for run in runs),
               locked_runs=sum(run['locked'] for run in runs))
edges = sorted([(run['start'], 1) for run in runs] + [(run['end'], -1) for run in runs])
active = peak = 0
for _, delta in edges:
    active += delta
    peak = max(peak, active)
summary['peak_pytest_concurrency'] = peak
(DATA / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')

text = '| File | Passing nodes | First contributing start (UTC) | Load 1/5/15 | Hold |\n|---|---:|---|---|---|\n'
for filename, nodes in sorted(passing.items()):
    run = first[filename]
    text += f"| {filename} | {len(nodes)} | {run['start']} | {'/'.join(f'{v:.2f}' for v in run['load'])} | {run['name']} |\n"
(DATA / 'file-table.md').write_text(text)

text = '| Hold | Start (UTC) | Load 1/5/15 | Seconds | Passed / failed / skipped | Exit | Locked |\n|---|---|---|---:|---|---:|---|\n'
for run in runs:
    counts = collections.Counter(p['outcome'] for p in run['phases'] if p['phase'] == 'call')
    text += f"| {run['name']} | {run['start']} | {'/'.join(f'{v:.2f}' for v in run['load'])} | {run['seconds']:.2f} | {counts['passed']}/{counts['failed']}/{counts['skipped']} | {run['exit']} | {run['locked']} |\n"
(DATA / 'hold-table.md').write_text(text)

definitions = json.loads((DATA / 'mutants.json').read_text())
text = '| Mutant | Named killer | Result | Same r8 assertion |\n|---|---|---|---|\n'
mutants = []
for name, definition in definitions.items():
    run = next((r for r in runs if r['name'] == 'mutant-' + name), None)
    if run is None:
        continue
    failures = [p for p in run['phases'] if p['outcome'] == 'failed']
    assert len(failures) == 1 and failures[0]['phase'] == 'call', name
    assert run['exit'] == 1, name
    failure = failures[0]['failure']
    assertion = next(line.strip() for line in failure.splitlines() if line.strip().startswith('assert '))
    same = None
    if name.startswith('r8-'):
        before = json.loads((DATA.parents[2] / definition['previous_result']).read_text())
        previous = next(p['failure'] for p in before['phases'] if p['outcome'] == 'failed')
        previous_assertion = next(line.strip() for line in previous.splitlines() if line.strip().startswith('assert '))
        same = assertion == previous_assertion
        assert same, (name, assertion, previous_assertion)
    mutants.append(dict(name=name, result='Killed', node=failures[0]['node'], assertion=assertion, same_r8_assertion=same))
    text += f"| {name} | {definition['tests'][0]} | Killed | {'Yes' if same else 'New'} |\n"
(DATA / 'mutant-table.md').write_text(text)
(DATA / 'mutant-results.json').write_text(json.dumps(mutants, indent=2) + '\n')

text = '| Single-site reversion | Result | First failing generated input, before shrinking | Assertion |\n|---|---|---|---|\n'
reversions = []
for run in runs:
    if not run['name'].startswith('revert-'):
        continue
    failures = [p for p in run['phases'] if p['outcome'] == 'failed']
    assert len(failures) == 1 and failures[0]['phase'] == 'call', run['name']
    attempt = next(a for a in run['attempts'] if a['outcome'] == 'failed')
    assertion = next(line.strip() for line in failures[0]['failure'].splitlines() if line.strip().startswith('assert '))
    entry = dict(name=run['name'], input=attempt['input'], assertion=assertion, result='Detected')
    reversions.append(entry)
    text += f"| {run['name'].removeprefix('revert-')} | Detected | `{json.dumps(attempt['input'], sort_keys=True)}` | `{assertion}` |\n"
(DATA / 'reversion-table.md').write_text(text)
(DATA / 'reversion-results.json').write_text(json.dumps(reversions, indent=2) + '\n')
print(json.dumps(summary, indent=2))
