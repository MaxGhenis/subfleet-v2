"""A private snapshot for an offline retention lookup, without SQLite sidecars.

SQLite's Unix VFS locks bytes starting at 0x40000000 (pending, reserved and
510 shared bytes). A separate process must own our exclusive lock: POSIX
record locks belong to processes, not descriptors, and the caller may already
have an open Store. While those bytes are exclusively locked, no SQLite
connection can open or checkpoint this database. Only a database with no WAL
is copied. A busy database, existing WAL or unavailable write-lock permission
uses SQLite's normal read-only protocol instead, never an unlocked copy or
an immutable connection to a potentially live database. No daemon.lock is used.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .store import Store


def copy_closed_database(database: Path, target: Path) -> bool:
    """Copy under SQLite's Unix locks, or decline without creating state files.

    The descriptor needs write permission solely for F_WRLCK; no byte is written
    to it. The child exits immediately after copying, releasing its locks even
    on error. An existing WAL is never deleted, ignored or copied piecemeal.
    """
    try:
        source = database.open("r+b")
    except OSError:
        return False
    with source:
        try:
            fcntl.lockf(source.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB, 512, 0x40000000)
        except (BlockingIOError, PermissionError):
            return False
        if any(database.with_name(database.name + suffix).exists() for suffix in ("-wal", "-journal")):
            return False
        with target.open("xb") as destination:
            shutil.copyfileobj(source, destination)
    return True


@contextmanager
def lookup_store(database: Path):
    """Yield (read-only Store, state-root file policy).

    A closed, checkpointed store is copied to private temporary storage and
    creates nothing in its state root, even if a writer starts during the read.
    If copying cannot be proven safe, or TMPDIR is inside the state root,
    SQLite may create/update only its WAL/SHM
    coordination files; this fallback writes no records and deletes no files.
    """
    temporary_root = Path(os.environ.get("TMPDIR") or "/tmp").resolve()
    state_root = database.resolve().parent
    if temporary_root == state_root or state_root in temporary_root.parents:
        with Store(database, read_only=True) as store:
            yield store, "sqlite-coordination"
        return
    with tempfile.TemporaryDirectory(prefix="subfleet-outcomes-", dir=temporary_root) as folder:
        snapshot = Path(folder) / "state.sqlite3"
        result = subprocess.run([sys.executable, "-m", "subfleet.retention_snapshot", str(database), str(snapshot)],
                                capture_output=True, text=True, check=False)
        if result.returncode not in (0, 1):
            raise OSError(f"retention lookup snapshot failed: {result.stderr.strip()}")
        source = snapshot if result.returncode == 0 else database
        with Store(source, read_only=True) as store:
            yield store, "unchanged" if result.returncode == 0 else "sqlite-coordination"


if __name__ == "__main__":
    sys.exit(0 if copy_closed_database(Path(sys.argv[1]), Path(sys.argv[2])) else 1)
