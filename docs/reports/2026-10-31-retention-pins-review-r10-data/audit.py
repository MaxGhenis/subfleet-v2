"""Audit final review receipts without running tests or reaching external state."""
import json
from pathlib import Path
import subprocess

D=Path(__file__).resolve().parent
R=D.parent
expected=set()
for plan in ['round-plan.json','independent-plan.json','edge-plan.json','remaining-plan.json',
             'mutants-plan.json','finding-plan.json','archive-path-plan.json','archive-path-last-plan.json']:
    expected.update(n for n,_ in json.loads((D/plan).read_text()))
expected.update(['edges-final','quiet-final','changed-targets','loop-growth','cycle-base-comparison','r10-invariant-red'])
expected.update('revert-r9-'+name+'-dropped' for name in ['text-canonicalization','alternates-canonicalization','scan-canonicalization','start-spelling'])
assert len(expected)==85,len(expected)
runs={}
for name in sorted(expected):
    run=json.loads((D/(name+'.json')).read_text())
    assert 'end' in run and run['locked'],name
    assert run['seconds']<600 and run['peak_rss_mib']<8192,name
    for node in run['collected']:
        phases=[p for p in run['phases'] if p['node']==node]
        assert [p['phase'] for p in phases]==['setup','call','teardown'],(name,node)
        assert phases[0]['outcome']==phases[2]['outcome']=='passed',(name,node)
        if phases[1]['outcome']=='skipped':
            assert name=='r10-green' and 'R10-F1' in (phases[1]['wasxfail'] or ''),(name,node)
    failures=[p for p in run['phases'] if p['outcome']=='failed']
    if name.startswith(('mutant-','revert-')):
        assert run['exit']==1 and len(failures)==1 and failures[0]['phase']=='call',name
    elif name=='r10-invariant-red':
        assert run['exit']==1 and len(failures)==2 and all(p['phase']=='call' for p in failures),name
    elif name in ['archive-0','archive-4']:
        assert run['exit']==1 and len(failures)==(5 if name=='archive-0' else 1),name
    else:
        assert run['exit']==0 and not failures,name
    runs[name]=run
summary=json.loads((D/'summary.json').read_text())
assert summary['completed_runs']==85 and summary['locked_runs']==85 and not summary['unresolved']
assert summary['peak_pytest_concurrency']<=2
for name in ['class-table.md','reversion-table.md','mutant-table.md']:
    assert (D/name).read_bytes()==(R/'docs/reports/2026-10-31-retention-pins-fix-r9-data'/name).read_bytes(),name
assert json.loads((D/'invariant-audit.json').read_text())['total']==440
assert len(json.loads((D/'mutant-results.json').read_text()))==13
assert len(json.loads((D/'reversion-results.json').read_text()))==4
assert len(runs['r10-trial']['collected'])==49
assert len([p for p in runs['r10-green']['phases'] if p['phase']=='call' and p['outcome']=='skipped'])==2
assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=R,text=True).strip()=='d2f1f36ab12bcde0577283c30b00500ecdd8e9c4'
subprocess.run(['git','diff','--exit-code'],cwd=R,check=True,stdout=subprocess.DEVNULL)
for relative in ['docs/acceptance-contract.md','tests/unit/test_retention_location_inputs.py']:
    assert (R/relative).read_bytes()==subprocess.check_output(['git','show','236c80883:'+relative],cwd=R)
passing={}
for name,run in runs.items():
    if name.startswith(('mutant-','revert-','r10-invariant-red','r10-trial')): continue
    for phase in run['phases']:
        if phase['phase']=='call' and phase['outcome']=='passed':
            passing.setdefault(phase['node'].split('::')[0],set()).add(phase['node'])
for filename,count in [('test_retention_archive.py',108),('test_retention_archive_properties.py',2),('test_retention_salvage_properties.py',2),('test_retention_81_notes.py',38),('test_retention_sweep_race.py',13)]:
    assert len(passing['tests/unit/'+filename])==count,(filename,len(passing['tests/unit/'+filename]))
record=dict(holds=85,all_complete=True,all_locked=True,all_under_ten_minutes=True,
            all_setup_teardown_passed=True,only_expected_call_failures=True,
            named_tables_byte_identical=True,head_and_product_files_unchanged=True,
            r10_f1_strict_expected_failures=2,trial_passes=49)
(D/'audit.json').write_text(json.dumps(record,indent=2))
print(json.dumps(record,indent=2))
