"""Fix round 3 cleanup: move generated folders under the scratch, then remove them and the scratch.

Every removal is a shutil.rmtree guarded by an assertion that `sf-eff-fix3` is in its path.
Writes fix-r3/cleanup.json with the paths, the time and what remains.
"""
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
TMP = Path(subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True, check=True)
           .stdout.strip()).resolve()
SCRATCH = TMP / "sf-eff-fix3"
OUTSIDE = [ROOT / ".venv", ROOT / ".hypothesis"]


def guarded_rmtree(path: Path) -> None:
    assert "sf-eff-fix3" in str(path), path
    shutil.rmtree(path)


record = {"started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
          "scratch": str(SCRATCH), "outside": [str(path) for path in OUTSIDE], "moved": {}}
assert "sf-eff-fix3" in str(SCRATCH) and SCRATCH.is_dir(), SCRATCH
holding = SCRATCH / "outside"
holding.mkdir()
for path in OUTSIDE:
    if path.exists():
        target = holding / path.name
        shutil.move(str(path), str(target))
        record["moved"][str(path)] = str(target)
for target in sorted(holding.iterdir()):
    guarded_rmtree(target)
guarded_rmtree(SCRATCH)
record["remaining"] = {str(path): path.exists() for path in (*OUTSIDE, SCRATCH)}
record["uv_cache_kept"] = (TMP / "uv-cache").is_dir()
record["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
assert not any(record["remaining"].values()), record
(HERE / "cleanup.json").write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
print(json.dumps(record, indent=1, sort_keys=True))
