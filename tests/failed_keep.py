"""Where a failed test's state root is kept, to be read after the run: under /tmp/sf-failed,
which CI uploads as an artifact.

Each test process keeps its own under /tmp/sf-failed/<pid>/. Two suites can run at once on
one host (the shared suite lock has two slots), and a test that failed in both would
otherwise have one run's rmtree and copytree remove or collide with the other's evidence.

Each pytest session starts by pruning (`prune`, from tests/conftest.py): a pid folder goes
once its process is gone and nothing has been kept in it for 24 hours.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import time
from pathlib import Path

ROOT = Path("/tmp/sf-failed")

#: A gone process's folder is pruned once nothing has been kept in it for longer than this.
PRUNE_AFTER_S = 24 * 60 * 60

_PID = re.compile(r"[1-9][0-9]*")
_PID_MAX = 2**31 - 1              # pid_t is a 32-bit int: a longer number names no process


def failed_keep_dir(test_name: str) -> Path:
    """The folder a failed test's state root is copied to: one per process and test name.

    Keyed by `request.node.name`, not the full node id, so two modules' tests with the same name share a folder
    within one process (one such pair exists today, and the fake one keeps nothing)."""
    return ROOT / str(os.getpid()) / re.sub(r"[^A-Za-z0-9_.-]", "_", test_name)


def prune(root: Path | None = None, *, now_ns: int | None = None, kill=os.kill) -> list[Path]:
    """Remove the kept roots of test processes that are gone, and return the folders removed.

    A folder `<root>/<name>` goes only when all of these hold:
    - it is a directory, not a symlink or a file, and its name is a pid (`[1-9][0-9]*`, at most pid_t's maximum);
    - its process is gone: `kill(pid, 0)` raises ProcessLookupError (any other answer, such as EPERM for
      another user's process, counts as alive), and the pid is not this process's;
    - nothing has been kept in it for more than PRUNE_AFTER_S: its own mtime, which changes whenever a
      test's folder is added to it or removed, is older than that.
    Each folder is first claimed by renaming it to a name that is not a pid, so of several pruners at
    once (every xdist worker prunes at its start) one removes it, and a new process that takes the pid
    keeps its roots in a new folder. If the claimed folder changed or its pid came alive meanwhile, it
    is put back. Nothing here raises: a folder that cannot be read, claimed or removed is left as it is."""
    root = ROOT if root is None else root
    now_ns = time.time_ns() if now_ns is None else now_ns
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    removed = []
    for entry in entries:
        if not _PID.fullmatch(entry.name) or int(entry.name) > _PID_MAX:
            continue
        pid, folder = int(entry.name), Path(entry.path)
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            seen = entry.stat(follow_symlinks=False).st_mtime_ns
        except OSError:
            continue
        if now_ns - seen <= PRUNE_AFTER_S * 10**9 or _alive(pid, kill):
            continue
        claimed = root / f".pruning-{pid}-{os.getpid()}-{secrets.token_hex(4)}"
        try:
            os.rename(folder, claimed)
        except OSError:
            continue                    # another pruner claimed it first, or it went
        try:
            changed = os.lstat(claimed).st_mtime_ns != seen
        except OSError:
            continue
        if changed or _alive(pid, kill):
            try:
                os.rename(claimed, folder)
            except OSError:
                pass                    # the pid has a new folder already; this one stays claimed, unremoved
            continue
        shutil.rmtree(claimed, ignore_errors=True)
        removed.append(folder)
    return removed


def _alive(pid: int, kill) -> bool:
    if pid == os.getpid():
        return True
    try:
        kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True
