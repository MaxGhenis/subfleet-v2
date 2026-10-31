"""Build compact, durable tables from the completed per-hold evidence."""
import collections
import json
from pathlib import Path
import re

DATA = Path(__file__).resolve().parent

def read(path):
    return json.loads(path.read_text())

runs = {}
for path in DATA.glob('*.json'):
    try:
        data = read(path)
    except json.JSONDecodeError:
        continue
    if isinstance(data, dict) and 'phases' in data:
        runs[path.stem] = data

ordinary = ['green-r7', 'parent-hint-corrected'] + [name for name in runs if name.startswith('retry-')]
for name in ('ordinary-a.json', 'ordinary-b.json'):
    ordinary += [key for key, _ in read(DATA / name) if key != 'parent-hint']
rows = collections.defaultdict(lambda: dict(nodes=set(), starts=[], runs=[]))
for name in ordinary:
    run = runs.get(name)
    if not run:
        continue
    for phase in run['phases']:
        if phase['phase'] == 'call' and phase['outcome'] == 'passed':
            path = phase['node'].split('::')[0]
            rows[path]['nodes'].add(phase['node'])
            if name not in rows[path]['runs']:
                rows[path]['runs'].append(name)
                rows[path]['starts'].append((run['start'], run['load']))
file_rows = []
for path, item in sorted(rows.items()):
    start, load = min(item['starts'])
    file_rows.append(dict(file=path, passed=len(item['nodes']), start=start, load=load, runs=item['runs']))

original_run = runs['pointer-original']
original = [json.loads(o['value']) for o in original_run['observations'] if o['key'] == 'git-input']
assert len(original) == 115
original = original[15:]
new = []
head_original = []
for o in runs.get('test_retention_pointer_inputs', {}).get('observations', []):
    if o['key'] == 'named-own-input':
        new.append(json.loads(o['value']))
    elif o['key'] == 'git-input':
        head_original.append(json.loads(o['value']))
if head_original:
    assert [o['input'] for o in original] == [o['input'] for o in head_original[15:]]

dimensions = {}
for name, obs in [('original', original), ('focused', new)]:
    dimensions[name] = {key: dict(collections.Counter(o['input'][key] for o in obs))
                        for key in (obs[0]['input'] if obs else []) if key != 'slot'}
if new:
    dimensions['focused'].update(layout={'gitfile': len(new), 'directory': 0},
                                 prefix={'present': len(new), 'absent': 0, 'misspelled': 0, 'colon-only': 0},
                                 target={'normal': len(new), 'blank': 0, 'spaces': 0, 'padded': 0},
                                 named={'true': len(new), 'false': 0},
                                 padding={'short': len(new), 'under': 0, 'over': 0},
                                 pad_file={'gitfile': len(new), 'commondir': 0})
cells = {}
for name, obs in [('original', original), ('focused', new)]:
    cells[name] = {common: {own: sum(o['input']['common'] == common and o['input']['own'] == own for o in obs)
                           for own in ('absent', 'plain', 'symlink', 'alternate')}
                   for common in ('absent', 'empty', 'lf', 'crlf', 'target')}
named = [o for o in original if o['input']['layout'] == 'gitfile' and o['input']['named']
         and o['outcome'] == 'reads-owner']
critical = {common: sum(o['input']['common'] == common for o in named) for common in ('absent', 'lf', 'crlf')}
missing = [(common, own) for common in ('absent', 'lf', 'crlf') for own in ('plain', 'symlink', 'alternate')
           if not cells['focused'][common][own]]

mutants = []
for name, definition in read(DATA / 'mutants.json').items():
    run = runs.get('mutant-' + name)
    if not run or 'exit' not in run:
        continue
    failed = [p for p in run['phases'] if p['phase'] == 'call' and p['outcome'] == 'failed']
    other_failures = [p for p in run['phases'] if p['phase'] != 'call' and p['outcome'] == 'failed']
    assertions = []
    for phase in failed:
        # Short tracebacks retain the source assertion immediately after the test location.
        text = phase['failure']
        lines = text.splitlines()
        assertions += [line.strip() for line in lines if line.lstrip().startswith('assert ')]
    previous = Path(definition.get('previous_log', '/nonexistent'))
    same = None
    if previous.exists():
        prior_text = previous.read_text()
        same = bool(assertions) and all(assertion in prior_text for assertion in assertions)
    mutants.append(dict(name=name, tests=definition['tests'], killed=bool(failed) and bool(assertions) and not other_failures and run['exit'] == 1,
                        assertions=assertions, same_assertion_as_r6=same,
                        failures=[dict(node=p['node'], failure=p['failure']) for p in failed],
                        setup_teardown_passed=not other_failures))

events = []
for run in runs.values():
    if 'end' in run:
        events += [(run['start'], 1), (run['end'], -1)]
active = peak = 0
for _, change in sorted(events):
    active += change
    peak = max(peak, active)
summary = dict(peak_concurrent_pytest=peak,
               outside_lock=[name for name, run in runs.items() if not run.get('locked', True)],
               files=file_rows, total_unique_passes=sum(row['passed'] for row in file_rows),
               dimensions=dimensions, cells=cells, critical_original=critical,
               original_generated=len(original), focused_generated=len(new), missing_focused_cells=missing,
               head_original_inputs_identical=bool(head_original), mutants=mutants,
               longest_seconds=max((r.get('seconds', 0) for r in runs.values()), default=0),
               peak_rss_mib=max((r.get('peak_rss_mib', 0) for r in runs.values()), default=0),
               completed_holds=sum('exit' in r for r in runs.values()))
(DATA / 'summary.json').write_text(json.dumps(summary, indent=2))
print(json.dumps(dict(ordinary_passes=summary['total_unique_passes'], focused=len(new),
                     mutants=len(mutants), killed=sum(m['killed'] for m in mutants), missing=missing)))
