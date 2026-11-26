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
        before = json.loads((DATA.parents[2] / definition['previous_r9_result']).read_text())
        previous = next(p['failure'] for p in before['phases'] if p['outcome'] == 'failed')
        previous_assertion = next(line.strip() for line in previous.splitlines() if line.strip().startswith('assert '))
        same_r9 = assertion == previous_assertion
    if name.startswith('r8-'):
        before = json.loads((DATA.parents[2] / definition['previous_result']).read_text())
        previous = next(p['failure'] for p in before['phases'] if p['outcome'] == 'failed')
        previous_assertion = next(line.strip() for line in previous.splitlines() if line.strip().startswith('assert '))
        same_r8 = assertion == previous_assertion
    mutants.append(dict(name=name, result='Killed', node=failures[0]['node'], assertion=assertion,
                        same_r9_assertion=same_r9, same_r8_assertion=same_r8))
    prior = 'New' if same_r9 is None else ('Yes' if same_r9 else 'No')
    text += f"| {name} | {definition['tests'][0]} | Killed | {prior} |\n"
(DATA / 'mutant-table.md').write_text(text)
(DATA / 'mutant-results.json').write_text(json.dumps(mutants, indent=2) + '\n')


print(json.dumps(summary, indent=2))

# The hub's seven routes plus the two-link scan and alternates variants.
def observations(run_name, key):
    run = next(r for r in runs if r['name'] == run_name)
    return [json.loads(v['value']) for v in run['observations'] if v['key'] == key]

if any(r['name'] == 'route-and-routine-comparison' for r in runs):
    comparisons = observations('route-and-routine-comparison', 'route-comparison')
    red = {(v['route'], v['cycle']): v for v in observations('red-cycles-final', 'cycle-route')}
    green = {(v['route'], v['cycle']): v for v in observations('green-test_retention_location_cycles', 'cycle-route')}
    text = '| Route | Cycle | Base fsck/read | Head fsck/read | Base/head incomplete | Base/head unspelled |\n|---|---|---|---|---|---|\n'
    for c in comparisons:
        a, b = red[(c['route'], c['cycle'])], green[(c['route'], c['cycle'])]
        text += f"| {c['route']} | {c['cycle']} | {a['fsck']}/{a['read']} | {b['fsck']}/{b['read']} | {len(c['baseline'].get('incomplete', []))}/{len(c['actual'].get('incomplete', []))} | {len(c['baseline_unspelled'])}/{len(c['actual_unspelled'])} |\n"
    (DATA / 'route-table.md').write_text(text)
    (DATA / 'route-comparisons.json').write_text(json.dumps(comparisons, indent=2) + '\n')
    controls = observations('route-and-routine-comparison', 'baseline-routine')
    (DATA / 'routine-comparisons.json').write_text(json.dumps(controls, indent=2) + '\n')

classes = {}
for run_name in ['green-test_retention_location_spelling', 'linked-test_retention_location_spelling']:
    if not any(r['name'] == run_name for r in runs):
        continue
    values = observations(run_name, 'spelling-input')
    cells = collections.Counter((v['input']['position'], v['input']['spelling']) for v in values)
    classes[run_name] = dict(inputs=len(values), distinct=len({json.dumps(v['input'], sort_keys=True) for v in values}),
                             outcomes=dict(collections.Counter(v['outcome'] for v in values)),
                             cells={p+' / '+s: n for (p,s),n in sorted(cells.items())})
(DATA / 'spelling-classes.json').write_text(json.dumps(classes, indent=2) + '\n')
