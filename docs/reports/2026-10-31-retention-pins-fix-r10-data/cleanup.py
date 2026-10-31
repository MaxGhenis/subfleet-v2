"""Remove only this job's fixture roots and scratch after all holds have ended."""
import datetime
import json
import os
import stat
from pathlib import Path
import shutil
import subprocess

DATA = Path(__file__).resolve().parent
ROOT = DATA.parents[2]
SCRATCH = Path('/private/tmp/sf-fix272r10')
for path in DATA.glob('*-queued.json'):
    receipt = DATA / path.name.replace('-queued.json', '.json')
    unrun = DATA / path.name.replace('-queued.json', '-unrun.json')
    assert receipt.exists() or unrun.exists(), path.name
    if receipt.exists():
        run = json.loads(receipt.read_text())
        assert 'end' in run, path.name
# The archive interruption fixture deliberately retains a read-only directory.
# Restore owner permissions only within this job's remaining physical tree.
restored = []
if SCRATCH.exists():
    for folder, dirs, files in os.walk(SCRATCH, followlinks=False):
        path = Path(folder)
        info = path.lstat()
        assert not stat.S_ISLNK(info.st_mode), path
        assert info.st_uid == os.getuid(), path
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o700 != 0o700:
            os.chmod(path, mode | 0o700, follow_symlinks=False)
            restored.append(str(path))
measurement = subprocess.run(['du', '-sk', str(SCRATCH)], check=True, text=True,
                             stdout=subprocess.PIPE).stdout
scratch_kib = int(measurement.split()[0])
assert scratch_kib < 1.5 * 1024 * 1024, scratch_kib
removed = []
for name in ['sf-fix272r10-fixtures', 'sf-r272r10-fixtures']:
    path = ROOT / name
    assert path.parent == ROOT
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
    removed.append(str(path))
# No tree-local environment or build was created. Remove this job's bytecode,
# if any script import produced it; all tracked source paths are retained.
for path in sorted(ROOT.rglob('*.pyc')):
    if '.lane-git-r10' not in path.parts and '.git' not in path.parts:
        path.unlink()
for path in sorted(ROOT.rglob('__pycache__'), reverse=True):
    if '.lane-git-r10' not in path.parts and '.git' not in path.parts:
        path.rmdir()
if (ROOT / '.hypothesis').exists():
    shutil.rmtree(ROOT / '.hypothesis')
subprocess.run(['python3', '-c',
    'import shutil,sys; p=sys.argv[1]; assert "/sf-" in p, p; shutil.rmtree(p, ignore_errors=True)',
    str(SCRATCH)], check=True)
assert not SCRATCH.exists()
result = dict(time=datetime.datetime.now(datetime.UTC).isoformat(),
              scratch_kib_before_removal=scratch_kib,
              earlier_measured_scratch_kib=545056,
              earlier_measurement_utc='2026-10-31T15:38:09+00:00',
              first_cleanup_cap_assertion_passed=True,
              restored_owner_permissions=restored,
              scratch_cap_kib=1.5 * 1024 * 1024,
              removed=[*removed, str(SCRATCH)],
              all_queued_holds_ended=True,
              managed_test_and_driver_sessions_waited=True,
              bytecode_and_hypothesis_removed=True)
(DATA / 'cleanup.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
