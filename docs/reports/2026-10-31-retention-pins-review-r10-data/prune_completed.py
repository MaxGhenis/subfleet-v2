"""Remove only ended pytest runs' disposable roots; keep all durable receipts."""
import json
from pathlib import Path
import shutil
D=Path(__file__).resolve().parent
S=Path('/private/tmp/sf-r272r10/real-tmp')
removed=[]
for p in sorted(D.glob('*.json')):
    run=json.loads(p.read_text())
    if not isinstance(run,dict) or 'phases' not in run or 'end' not in run: continue
    target=S/run['name']
    assert '/sf-' in str(target) and target.parent==S
    if target.is_dir():
        shutil.rmtree(target,ignore_errors=True)
        removed.append(dict(name=run['name'],absent=not target.exists()))
prior=D/'incremental-cleanup.json'
previous=json.loads(prior.read_text()) if prior.exists() else []
prior.write_text(json.dumps(previous+removed,indent=2))
print('Removed ended fixture roots:',len(removed),flush=True)
