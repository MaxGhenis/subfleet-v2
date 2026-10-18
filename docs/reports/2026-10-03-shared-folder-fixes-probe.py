"""Probe macOS canonicalization without listing or opening an 0111 target.

Run from the repository root with PYTHONPATH=. uv run python docs/reports/2026-10-03-shared-folder-fixes-probe.py.
All folders are task-owned and restored before removal.
"""

import fcntl
import json
import os
from pathlib import Path
import tempfile

from subfleet import folders

Path("build/verification").mkdir(parents=True, exist_ok=True)

with tempfile.TemporaryDirectory(prefix="spell-probe-", dir="build/verification") as root:
    root = Path(root).resolve()
    parent = root / "Secret-Parent"
    target = parent / "User-Home"
    target.mkdir(parents=True)
    typed = root / "sECRET-pARENT" / "uSER-hOME"
    expected = str(target)
    if not typed.is_dir():
        raise RuntimeError("probe needs a case-insensitive volume")
    parent.chmod(0o111)
    target.chmod(0o111)
    try:
        try:
            os.listdir(parent)
            raise AssertionError("ancestor was listable")
        except PermissionError:
            pass
        try:
            fd = os.open(typed, os.O_RDONLY)
        except PermissionError:
            open_result = "PermissionError (target is also 0111)"
        else:
            os.close(fd)
            raise AssertionError("0111 target was openable for reading")
        direct = folders._kernel_path(str(typed))
        assert direct == expected, (direct, expected)
        assert folders.spelling(typed) == (expected, None)
        target.chmod(0o755)
        fd = os.open(typed, os.O_RDONLY)
        try:
            descriptor = os.fsdecode(fcntl.fcntl(fd, 50, bytes(1024)).split(b"\0", 1)[0])
        finally:
            os.close(fd)
        assert descriptor == expected
        print(json.dumps({"getattrlist_FULLPATH": direct, "ancestor_mode": "0111", "target_mode": "0111", "listdir": "PermissionError", "open_for_F_GETPATH": open_result, "F_GETPATH_after_target_0755": descriptor}, indent=2))
    finally:
        target.chmod(0o755)
        parent.chmod(0o755)
