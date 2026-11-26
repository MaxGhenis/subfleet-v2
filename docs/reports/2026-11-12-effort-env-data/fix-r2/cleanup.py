"""Record generated folders, then remove them through guarded scratch paths."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil

ROOT = Path.cwd().resolve()
EVIDENCE = Path(__file__).resolve().parent
SCRATCH = Path(os.environ['SF_EFF_SCRATCH']).resolve()
assert 'sf-eff-fix2' in str(SCRATCH)
assert SCRATCH.name == 'sf-eff-fix2'
assert ROOT.name == '20261115-100031-eff-fix-r2'

outside = []
for parent, directories, _ in os.walk(ROOT, followlinks=False):
    for name in list(directories):
        path = Path(parent) / name
        generated = name == '__pycache__' or (
            Path(parent) == ROOT and name in {'.hypothesis', '.pytest_cache', 'build'})
        if generated:
            assert not path.is_symlink(), path
            outside.append(path)
            directories.remove(name)
        elif name in {'.git', '.agents', '.codex', '.aws'}:
            directories.remove(name)

extra_scratch = Path('/private/tmp/sf-eff-fix2-201115200058')
if extra_scratch.exists():
    assert not extra_scratch.is_symlink()
    outside.append(extra_scratch)

record = {'outside_scratch': [str(p) for p in outside],
          'scratch': str(SCRATCH), 'removed': []}
report = EVIDENCE / 'cleanup.json'
report.write_text(json.dumps(record, indent=2) + '\n')
staging = SCRATCH / 'removed-outside'
staging.mkdir(parents=True, exist_ok=True)
for index, path in enumerate(outside):
    moved = staging / f'{index}-{path.name}'
    shutil.move(str(path), str(moved))
    assert 'sf-eff-fix2' in str(moved)
    shutil.rmtree(moved)
    record['removed'].append(str(path))
    report.write_text(json.dumps(record, indent=2) + '\n')
assert 'sf-eff-fix2' in str(SCRATCH)
shutil.rmtree(SCRATCH)
record.update(scratch_removed=not SCRATCH.exists(),
              finished=datetime.now(timezone.utc).isoformat())
report.write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps(record))
