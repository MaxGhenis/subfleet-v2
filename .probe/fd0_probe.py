"""Probe: the fence pipe when the owning process has fd 0 closed.

`_start_catalog` makes the fence with `os.pipe()` and hands its read end to
`spawn_refresh`, which also passes `stdin=DEVNULL`. If fd 0 is free, `os.pipe()`
returns 0 as the read end and the child's stdin redirection lands on top of it.
Run: python .probe/fd0_probe.py   (it re-runs itself with fd 0 open, then closed)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def child() -> None:
    sys.path.insert(0, str(REPO))
    from subfleet.conversations import catalog
    base = Path(tempfile.mkdtemp(prefix="pr47-fd0-"))
    home, root = base / "home", base / "state"
    (home / ".claude" / "projects").mkdir(parents=True)
    root.mkdir()
    os.environ["HOME"] = str(home)
    os.environ.pop("SUBFLEET_CLAUDE_DIR", None)
    fence = os.pipe()                           # as ConversationService._start_catalog does
    process = catalog.spawn_refresh(root, fence_fd=fence[0])
    rc = process.wait(60)
    print(f"fd0 open={sys.argv[2]} fence={fence} run exit={rc} root={sorted(os.listdir(root))}", file=sys.stderr)
    os.close(fence[1])
    os.close(fence[0])
    shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        child()
    else:
        me = [sys.executable, __file__, "child"]
        subprocess.run(me + ["yes"], stdin=subprocess.DEVNULL, check=True)
        # The same, with the owner's stdin closed (as an embedder that closed 0-2 would have it).
        subprocess.run(me + ["no"], preexec_fn=lambda: os.close(0), check=True)
