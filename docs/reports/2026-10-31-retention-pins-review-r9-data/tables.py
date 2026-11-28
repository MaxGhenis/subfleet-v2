"""Count stored pytest phases/observations; no test execution or inferred passes."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import re

DATA=Path(__file__).resolve().parent
ROOT=DATA.parent
runs={}
for path in DATA.glob('*.json'):
    obj=json.loads(path.read_text())
    if isinstance(obj,dict) and 'phases' in obj:
        runs[path.stem]=obj
completed={n:r for n,r in runs.items() if 'exit' in r}
ordinary={n:r for n,r in completed.items() if r.get('valid',True) and not n.startswith(('mutant-','revert-','red-'))}
files=defaultdict(dict)
for name,run in sorted(ordinary.items(),key=lambda pair:pair[1]["start"]):
    for phase in run['phases']:
        if phase['phase']=='call' and phase['outcome']=='passed':
            file=phase['node'].split('::')[0]
            files[file][phase['node']]=(name,run)
lines=['| File | Pass | Start (UTC) | Load 1/5/15 | Holds |','|---|---:|---|---|---|']
filedata=[]
for file,nodes in sorted(files.items()):
    distinct={name:run for name,run in nodes.values()}
    earliest=min(distinct.values(),key=lambda r:r['start'])
    row=dict(file=file,passed=len(nodes),start=earliest['start'],load=earliest['load'],holds=sorted(distinct))
    filedata.append(row)
    lines.append('| '+file+' | '+str(len(nodes))+' | '+earliest['start'][11:19]+' | '+ '/'.join(f'{v:.2f}' for v in earliest['load'])+' | '+', '.join(sorted(distinct))+' |')
(DATA/'file-table.md').write_text('\n'.join(lines)+'\n')
(DATA/'files.json').write_text(json.dumps(filedata,indent=2)+'\n')

loc=runs.get('linked-location-inputs',{})
observations=[json.loads(o['value']) for o in loc.get('observations',[]) if o['key']=='location-input']
positions=['gitfile-target','intermediate','gitfile-pointer','commondir-file','commondir-target','objects','objects-info','alternates-file','alternates-line','git-entry','objects-pack']
lines=['| Position | Inputs | Chain 1/2/3 | Absolute/relative links | Unnamed/named admin | Needs owner / independent |','|---|---:|---|---|---|---|']
for position in positions:
    rows=[o for o in observations if o['input']['position']==position]
    chain=Counter(o['input']['chain'] for o in rows)
    relative=Counter(o['input']['relative'] for o in rows)
    named=Counter(o['input']['named'] for o in rows)
    outcome=Counter(o['outcome'] for o in rows)
    lines.append(f"| {position} | {len(rows)} | {'/'.join(str(chain[i]) for i in (1,2,3))} | {relative[False]}/{relative[True]} | {named[False]}/{named[True]} | {outcome['needs-owner']}/{outcome['independent']} |")
(DATA/'class-table.md').write_text('\n'.join(lines)+'\n')

pointer=runs.get('linked-pointer',{})
focused=[json.loads(o['value']) for o in pointer.get('observations',[]) if o['key']=='named-own-input']
original=[json.loads(o['value']) for o in pointer.get('observations',[]) if o['key']=='git-input']
classdata=dict(location=Counter(o['input']['position'] for o in observations),location_outcomes=Counter(o['outcome'] for o in observations),
               focused_count=len(focused),focused_complete=sum(not o['incomplete'] for o in focused),
               original_count=len(original),original_outcomes=Counter(o['outcome'] for o in original),
               focused_dimensions={key:dict(Counter(str(o['input'][key]) for o in focused)) for key in ('common','own','parent')})
(DATA/'classes.json').write_text(json.dumps(classdata,indent=2)+'\n')


def assertion(run):
    failures=[p for p in run.get('phases',[]) if p['phase']=='call' and p['outcome']=='failed']
    if not failures:return None
    text=failures[0]['failure']
    matches=re.findall(r'((?:tests/|docs/reports/)[^\s:]+\.py:\d+)',text)
    return matches[-1] if matches else text[:160]


mutants=json.loads((DATA/'mutants.json').read_text()) if (DATA/'mutants.json').exists() else {}
lines=['| Mutant | Named killer | Result | Same r7 assertion |','|---|---|---|---|']
mutantdata=[]
for name,mutation in mutants.items():
    run=completed.get('mutant-'+name)
    state='Pending'
    same='New' if name.startswith('r8-') else '—'
    if run:
        bad_fixture=any(p['outcome']=='failed' and p['phase']!='call' for p in run['phases'])
        state='Killed' if run['exit']==1 and assertion(run) and not bad_fixture else 'Survived' if run['exit']==0 else 'Invalid run'
        if name == 'r9-canonical-auxiliary-trial' and run['exit'] == 0:
            state = 'Positive trial'
        prior=ROOT / mutation.get('previous_result','nonexistent')
        if prior.is_file() and state=='Killed':
            same='Yes' if assertion(run)==assertion(json.loads(prior.read_text())) else 'No'
        elif prior.is_file():same='No — survived'
    killer='; '.join(item for item in mutation['tests'] if '.py' in item and not item.startswith('--'))
    lines.append('| '+name+' | '+killer+' | '+state+' | '+same+' |')
    mutantdata.append(dict(name=name,killer=killer,result=state,same_r7_assertion=same,assertion=assertion(run) if run else None))
(DATA/'mutant-table.md').write_text('\n'.join(lines)+'\n')
(DATA/'mutant-results.json').write_text(json.dumps(mutantdata,indent=2)+'\n')

lines=['| Reverted | Location property /55 | Focused /40 | First failing generated input or strategy boundary |','|---|---|---|---|']
revertdata=[]
for decision in ('decision1','decision2','decision3','r7-f1'):
    states=[];firsts=[]
    for prop in ('location','focused'):
        run=completed.get('revert-'+decision+'-'+prop)
        if not run:states.append('Pending');continue
        first=next((a for a in run.get('attempts',[]) if a['outcome']=='failed'),None)
        states.append('Detected' if run['exit']==1 and first else 'Pass' if run['exit']==0 else 'Invalid run')
        if first:firsts.append(prop+': `'+json.dumps(first['input'],sort_keys=True)+'`')
        revertdata.append(dict(decision=decision,property=prop,result=states[-1],first_failure=first))
    if decision!='r7-f1':firsts.append('Focused strategy has no metadata-only owner or entry selector (NAMED_OWN_INPUTS); it records the owner’s object storage directly.')
    lines.append('| '+decision+' | '+states[0]+' | '+states[1]+' | '+' '.join(firsts)+' |')
(DATA/'reversion-table.md').write_text('\n'.join(lines)+'\n')
(DATA/'reversion-results.json').write_text(json.dumps(revertdata,indent=2)+'\n')

last_calls={}
for run in sorted(ordinary.values(),key=lambda r:r['start']):
    for phase in run['phases']:
        if phase['phase']=='call': last_calls[phase['node']]=phase['outcome']
summary=dict(completed_runs=len(completed),ordinary_runs=len(ordinary),unique_ordinary_nodes=sum(len(x) for x in files.values()),
             unresolved_ordinary_failures=[node for node,outcome in last_calls.items() if outcome=='failed'],
             locked_runs=sum(r.get('locked',False) for r in completed.values()),
             longest_seconds=max((r['seconds'] for r in completed.values()),default=0),
             peak_rss_mib=max((r.get('peak_rss_mib') or 0 for r in completed.values()),default=0),
             incomplete_runs=[n for n,r in runs.items() if 'exit' not in r],
             invalid_runs=[n for n,r in completed.items() if not r.get('valid',True)],
             ordinary_failures={n:r['exit'] for n,r in completed.items() if r.get('valid',True) and r['exit']!=0 and not n.startswith(('mutant-','revert-','red-'))},
             mutants=Counter(m['result'] for m in mutantdata))
events=[]
for run in completed.values():
    events.extend([(run['start'],1),(run['end'],-1)])
active=peak=0
for when,change in sorted(events):
    active+=change;peak=max(peak,active)
summary['peak_pytest_concurrency']=peak
holdlines=['| Hold | Start UTC | Load 1/5/15 | Seconds | Exit |','|---|---|---|---:|---:|']
for name,run in sorted(completed.items(),key=lambda pair:pair[1]['start']):
    holdlines.append('| '+name+' | '+run['start'][11:19]+' | '+'/'.join(f'{v:.2f}' for v in run['load'])+' | '+f"{run['seconds']:.2f}"+' | '+str(run['exit'])+' |')
(DATA/'hold-table.md').write_text('\n'.join(holdlines)+'\n')
(DATA/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
print(json.dumps(summary,indent=2))
