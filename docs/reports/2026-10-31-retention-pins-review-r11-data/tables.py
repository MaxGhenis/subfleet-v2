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
superseded = set()
ordinary = [run for run in runs if not run['name'].startswith(('red-', 'mutant-', 'revert-', 'r11-red', 'r11-trial'))
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
# Include selected files with no completed passing call, using their first attempt.
for run in ordinary:
    for node in run.get('collected', []):
        filename = node.split('::')[0]
        if filename not in passing:
            passing[filename] = set()
            first[filename] = run
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

text = '| File | Passing nodes | First passing start (first attempt if zero) (UTC) | Load 1/5/15 | Hold |\n|---|---:|---|---|---|\n'
for filename, nodes in sorted(passing.items()):
    run = first[filename]
    text += f"| {filename} | {len(nodes)} | {run['start']} | {'/'.join(f'{v:.2f}' for v in run['load'])} | {run['name']} |\n"
(DATA / 'file-table.md').write_text(text)

text = '| Hold | Start (UTC) | Load 1/5/15 | Seconds | Passed / failed / skipped | Exit | Locked |\n|---|---|---|---:|---|---:|---|\n'
for run in runs:
    counts = collections.Counter(p['outcome'] for p in run['phases'] if p['phase'] == 'call')
    text += f"| {run['name']} | {run['start']} | {'/'.join(f'{v:.2f}' for v in run['load'])} | {run['seconds']:.2f} | {counts['passed']}/{counts['failed']}/{counts['skipped']} | {run['exit']} | {run['locked']} |\n"
(DATA / 'hold-table.md').write_text(text)

summary['product_passing_nodes'] = sum(len(v) for k,v in passing.items() if k.startswith('tests/'))
summary['control_passing_nodes'] = sum(len(v) for k,v in passing.items() if not k.startswith('tests/'))
(DATA / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')

definitions = json.loads((DATA / 'mutants.json').read_text())
text = '| Mutant | Named killer | Result | Same r9 assertion |\n|---|---|---|---|\n'
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
    same_r9 = same_r8 = None
    if 'previous_r9_result' in definition:
        before = json.loads((DATA.parent / definition['previous_r9_result']).read_text())
        previous = next(p['failure'] for p in before['phases'] if p['outcome'] == 'failed')
        previous_assertion = next(line.strip() for line in previous.splitlines() if line.strip().startswith('assert '))
        same_r9 = assertion == previous_assertion
    if name.startswith('r8-'):
        before = json.loads((DATA.parent / definition['previous_result']).read_text())
        previous = next(p['failure'] for p in before['phases'] if p['outcome'] == 'failed')
        previous_assertion = next(line.strip() for line in previous.splitlines() if line.strip().startswith('assert '))
        same_r8 = assertion == previous_assertion
    prior_path = DATA.parent / 'docs/reports/2026-10-31-retention-pins-fix-r10-data' / ('mutant-' + name + '.json')
    same_r10 = None
    if prior_path.exists():
        prior_round = json.loads(prior_path.read_text())
        prior_failure = next(p['failure'] for p in prior_round['phases'] if p['outcome'] == 'failed')
        same_r10 = assertion == next(line.strip() for line in prior_failure.splitlines() if line.strip().startswith('assert '))
    mutants.append(dict(same_r10_assertion=same_r10, name=name, result='Killed', node=failures[0]['node'], assertion=assertion,
                        same_r9_assertion=same_r9, same_r8_assertion=same_r8))
    prior = 'New' if same_r9 is None else ('Yes' if same_r9 else 'No')
    text += f"| {name} | {definition['tests'][0]} | Killed | {prior} |\n"
(DATA / 'mutant-table.md').write_text(text)
(DATA / 'mutant-results.json').write_text(json.dumps(mutants, indent=2) + '\n')


print(json.dumps(summary, indent=2))

