import datetime,json
from pathlib import Path
D=Path(__file__).resolve().parent
runs=[]
for p in D.glob('*.json'):
 try:r=json.loads(p.read_text())
 except json.JSONDecodeError:continue
 if isinstance(r,dict) and 'phases' in r:runs.append(r)
ordinary=[r for r in runs if not r['name'].startswith(('mutant-','r11-red','r11-trial'))]
passed={p['node'] for r in ordinary for p in r['phases'] if p['phase']=='call' and p['outcome']=='passed'}
required=set(json.loads((D/'collect-earlier-corrected.json').read_text())['collected'])
for r in runs:
 if r['name'].startswith('round-'):required.update(r['collected'])
required.add('tests/unit/test_retention_regenerable_properties.py::test_only_and_all_regenerable_output_is_dropped_and_the_rest_restored')
remaining=sorted(required-passed)
active=[r['name'] for r in runs if 'end' not in r]
queued_names={p.stem.removesuffix('-queued') for p in D.glob('*-queued.json')}
waiting=sorted(queued_names-{r['name'] for r in runs})
active.extend('waiting:'+n for n in waiting)
failures={p['node'] for r in ordinary for p in r['phases'] if p['outcome']=='failed'}
unresolved=sorted(failures-passed)
assert not unresolved,unresolved
assert all(r['locked'] for r in runs)
assert all(r.get('seconds',0)<600 for r in runs)
assert all(r.get('peak_rss_mib',0)<8192 for r in runs)
assert all(r['start']<'2026-10-31T20:08:30' for r in runs)
mutants=json.loads((D/'mutant-results.json').read_text())
assert len(mutants)==17,len(mutants)
assert sum(r['same_r10_assertion'] is True for r in mutants)==16
assert sum(r['same_r9_assertion'] is True for r in mutants)==13
assert sum(r['same_r8_assertion'] is True for r in mutants)==9
trial=next(r for r in runs if r['name']=='r11-trial')
assert trial['exit']==0 and len(trial['collected'])==57
new=next(r for r in runs if r['name']=='r11-green')
xfails=[p for p in new['phases'] if p['phase']=='call' and p['wasxfail']]
assert len(xfails)==2 and all('R11-F1' in p['wasxfail'] for p in xfails)
assert all(p['outcome']=='skipped' for p in xfails)
skips=[p['node'] for r in ordinary for p in r['phases'] if p['outcome']=='skipped' and not p['wasxfail']]
assert not skips,skips
allow={'tests/unit/test_retention_shared_folders.py::test_early_fences_equal_reservation_before_and_after_quarantine'}
archive=json.loads((D/'archive-path-environment.json').read_text())
result=dict(time=datetime.datetime.now(datetime.UTC).isoformat(),required_nodes=len(required),
 required_passing=len(required&passed),remaining=remaining,active=active,unresolved=[],
 strict_expected_failures=[p['node'] for p in xfails],unsupported_skips=[],
 shared_lock_holds=len(runs),outside_lock=[],nine_minute_incomplete=list(allow),
 archive_environment_failures=archive['failed_nodes'],archive_control_passed=all(n in passed for n in archive['failed_nodes']),
 all_requested_holds_started=all(n in {r['name'] for r in runs} for n,_ in json.loads((D/'earlier-plan.json').read_text())),
 final=not active and set(remaining)==allow)
(D/'completion.json').write_text(json.dumps(result,indent=2))
print(json.dumps(dict(required_nodes=result['required_nodes'],required_passing=result['required_passing'],remaining_count=len(remaining),remaining_files=dict(__import__('collections').Counter(n.split('::')[0] for n in remaining)),active=active,archive_control_passed=result['archive_control_passed'],final=result['final']),indent=2))
