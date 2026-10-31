"""Report incomplete holds and nodes without starting any additional test."""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent
splits = json.loads((DATA / 'split-holds.json').read_text())

def expand(name, args):
    if name in splits:
        for child, values in splits[name]:
            yield from expand(child, values)
    else:
        yield name, args

expected = {}
for filename in ['required-plan.json','follow-up-tests-plan.json','mutants-plan.json',
                 'early-resume-plan.json','diagnostic-plan.json','archive-control-plan.json','final-shared-plan.json','resume-plan.json','retry-plan.json']:
    path = DATA / filename
    if path.exists():
        for name, args in json.loads(path.read_text()):
            expected.update(expand(name, args))
runs = {}
for path in DATA.glob('*.json'):
    try:
        value = json.loads(path.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        continue
    if isinstance(value, dict) and 'phases' in value and 'end' in value:
        runs[value['name']] = value
ordinary = [v for k,v in runs.items() if not k.startswith(('red-','mutant-'))]
passed = {p['node'] for r in ordinary for p in r['phases']
          if p['phase']=='call' and p['outcome']=='passed'}
skipped = {p['node'] for r in ordinary for p in r['phases'] if p['outcome']=='skipped'}
collected = {n for r in ordinary for n in r.get('collected',[])}
failed = {p['node']:r['name'] for r in ordinary for p in r['phases'] if p['outcome']=='failed'}
missing = {n:args for n,args in expected.items() if n not in runs}
unfinished = sorted(collected - passed - skipped)
status = dict(complete=not missing and not unfinished and not(set(failed)-passed),
              planned_holds=len(expected), completed_holds=len(runs),
              missing_holds=missing, unfinished_nodes=unfinished,
              unresolved_failures={n:r for n,r in failed.items() if n not in passed},
              skipped_nodes=sorted(skipped))
(DATA / 'completion-status.json').write_text(json.dumps(status,indent=2)+'\n')
text = '| Hold not completed | Selection |\n|---|---|\n'
for name,args in missing.items():
    text += f"| {name} | {' '.join(args)} |\n"
text += '\nNodes without a completed passing call:\n\n'
text += '\n'.join('- `' + n + '`' for n in unfinished) + '\n'
(DATA / 'uncompleted-table.md').write_text(text)
print(json.dumps({k:v for k,v in status.items() if k not in ('missing_holds','unfinished_nodes','skipped_nodes')},indent=2))
print('Missing holds:',len(missing),'unfinished nodes:',len(unfinished))
