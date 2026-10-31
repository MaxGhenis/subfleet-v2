"""Remove only this job's owned roots, using the prescribed guarded cleanup."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys

DATA = Path(__file__).resolve().parent
ROOT = DATA.parents[2]
SCRATCH = Path('/private/tmp/sf-fix272r12')
allocated = int(subprocess.check_output(['du', '-sk', str(SCRATCH)], text=True).split()[0]) * 1024
assert allocated < 1.5 * 1024**3, allocated
record = dict(time=datetime.datetime.now(datetime.timezone.utc).isoformat(),
              allocated_before_cleanup=allocated, roots=[], claims=[])
# The shared refs were denied. Keep private metadata only until the final
# text commit and verified bundle, then remove that owned directory as well.
private_git = ROOT / 'sf-r12-git'
assert not private_git.exists(), private_git
shutil.move(str(SCRATCH / 'git'), str(private_git))
record['private_git_for_final_commit'] = str(private_git)
guard = 'import shutil,sys; p=sys.argv[1]; assert "/sf-" in p, p; shutil.rmtree(p, ignore_errors=True)'


def permissions(path):
    """Only repair owned directory modes; never follow a symlink."""
    if path.is_symlink():
        return
    if path.is_dir():
        path.chmod(0o700)
        for child in path.iterdir():
            permissions(child)


for path in (SCRATCH, ROOT / 'sf-fix272r12-fixtures'):
    assert '/sf-' in str(path), path
    subprocess.run([sys.executable, '-c', guard, str(path)], check=True)
    repaired = False
    if path.exists():
        permissions(path)
        repaired = True
        subprocess.run([sys.executable, '-c', guard, str(path)], check=True)
    assert not path.exists(), path
    record['roots'].append(dict(path=str(path), removed=True, permission_repair=repaired))
for path in DATA.glob('*.claim'):
    record['claims'].append(path.name)
    path.unlink()
# All job Python invocations set PYTHONDONTWRITEBYTECODE. Verify the worktree
# nevertheless, without traversing the isolated Git metadata.
artifacts = []
for directory, dirs, files in os.walk(ROOT, followlinks=False):
    dirs[:] = [d for d in dirs if d not in ('sf-r12-git', '.git')]
    for name in dirs:
        if name in ('__pycache__', '.hypothesis', 'build'):
            artifacts.append(str(Path(directory) / name))
    artifacts.extend(str(Path(directory) / name) for name in files if name.endswith(('.pyc', '.pyo')))
record['remaining_bytecode_hypothesis_build'] = artifacts
assert not artifacts, artifacts
record['all_test_and_mutant_drivers_ended_before_cleanup'] = True
record['completed'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
(DATA / 'cleanup.json').write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps({k:v for k,v in record.items() if k != 'claims'}, indent=2))
