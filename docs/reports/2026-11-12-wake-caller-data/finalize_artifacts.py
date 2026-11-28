from pathlib import Path
import json
import shutil

scratch = Path('/private/tmp/sf-wc-b1')
destination = Path.cwd() / 'docs/reports/2026-11-12-wake-caller-data'
destination.mkdir(parents=True, exist_ok=True)
for pattern in ('*.log', '*commands.txt', '*.jsonl'):
    for source in scratch.glob(pattern):
        if source.name == 'root-commands.txt':
            continue
        shutil.copy2(source, destination/source.name)
for name in ('run.py','sf_job_isolation.py','run_final_regression.py','postio-identity.patch',
             'generated-paths-before-cleanup.json','regression-files.txt','run_mutants_delivery.py',
             'cleanup_workspace.py','finalize_artifacts.py','deliver.py','capture_changes.py'):
    source = scratch/name
    if source.exists():shutil.copy2(source,destination/name)
for folder in ('mutants-final','mutants-delivery'):
    source=scratch/folder
    if source.exists():shutil.copytree(source,destination/folder,dirs_exist_ok=True)
root_ledger=destination/'root-commands.txt'
base=scratch/'root-commands-base.txt'
if not base.exists():shutil.copy2(root_ledger,base)
root_ledger.write_text(base.read_text() + (scratch/'root-commands.txt').read_text())
runs=[]
for line in (scratch/'commands.jsonl').read_text().splitlines():
    run=json.loads(line)
    run['archived_log']=Path(run['log']).name
    runs.append(run)
(destination/'runs.json').write_text(json.dumps(runs,indent=2)+'\n')
manifest=[{'path':str(p.relative_to(destination)), 'bytes':p.stat().st_size}
          for p in sorted(destination.rglob('*')) if p.is_file() and p.name!='artifact-manifest.json']
(destination/'artifact-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps({'files':len(manifest),'bytes':sum(p['bytes'] for p in manifest)}))
