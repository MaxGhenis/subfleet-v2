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
import stat
import time
from pathlib import Path

ROOT = Path("/tmp/sf-failed")

#: A gone process's folder is pruned once nothing has been kept in it for longer than this.
PRUNE_AFTER_S = 24 * 60 * 60

_PID = re.compile(r"[1-9][0-9]*")
_CLAIMED = re.compile(r"\.pruning-([1-9][0-9]*)-([1-9][0-9]*)-[0-9a-f]{8}")   # .pruning-<pid>-<pruner pid>-<hex>
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
    is put back, and so is one that is no longer a directory (a symlink swapped in) or that removal left
    partly in place (#247 review r1, P3-1): such a folder is never reported removed.

    A claimed folder left behind, by a pruner stopped mid-removal or one that could not put a folder back,
    is removed once its pruner is gone and it is older than PRUNE_AFTER_S, by the same rules, and returned
    under its claimed name. Nothing here raises: a folder that cannot be read, claimed or removed is left
    under the name it had."""
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
            st = os.lstat(claimed)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode) or st.st_mtime_ns != seen or _alive(pid, kill):
            _put_back(claimed, folder)
            continue
        if _remove(claimed):
            removed.append(folder)
        else:
            _put_back(claimed, folder)  # partly removed: it keeps its pid name, and a later session retries
    removed.extend(_sweep_claimed(root, entries, now_ns, kill))
    return removed


def _remove(path: Path) -> bool:
    """rmtree, never following a symlink; True only when nothing is left under `path`."""
    shutil.rmtree(path, ignore_errors=True)
    return not os.path.lexists(path)


def _put_back(claimed: Path, folder: Path) -> None:
    try:
        os.rename(claimed, folder)
    except OSError:
        pass                            # the pid has a new folder already: this stays claimed, for the sweep


def _sweep_claimed(root: Path, entries, now_ns: int, kill) -> list[Path]:
    """Claimed folders a pruner left behind: removed once the pruner and the folder's pid are gone and it is
    older than PRUNE_AFTER_S (its own mtime; a rename does not change it). Each is claimed again by rename before
    removal, so it is reported once however many pruners run; what a sweep cannot finish stays under the
    sweeper's own claim name, for a later sweep once that sweeper is gone and the claim is a day old."""
    swept = []
    for entry in entries:
        m = _CLAIMED.fullmatch(entry.name)
        if not m or int(m.group(1)) > _PID_MAX or int(m.group(2)) > _PID_MAX:
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            seen = entry.stat(follow_symlinks=False).st_mtime_ns
        except OSError:
            continue
        if now_ns - seen <= PRUNE_AFTER_S * 10**9:
            continue
        if _alive(int(m.group(2)), kill) or _alive(int(m.group(1)), kill):
            continue
        path = Path(entry.path)
        # Claim it first, as the main loop does (#247 review r2, P3-1): of several sweepers one takes it, and a
        # claim that went, or was put back, meanwhile is not this one's to report.
        mine = root / f".pruning-{m.group(1)}-{os.getpid()}-{secrets.token_hex(4)}"
        try:
            os.rename(path, mine)
            st = os.lstat(mine)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode):
            _put_back(mine, path)
        elif _remove(mine):
            swept.append(path)
    return swept


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
