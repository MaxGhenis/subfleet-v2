"""Private provider scratch and conservative reclamation (C-12.10, D-TMP1).

The store is the authority. An attempt's evidence (a probe's record) names its
allocation; a sweep decides whether any other folder is known, and how old it
is, from the store alone. A provider can write its folder's marker and its
timestamps, birth time included, so neither ever makes a folder known or old.
A marker can only keep a folder: one naming another store, or none, is never
removed as unknown.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import time
from typing import Callable

from .guardian import atomic_publish
from .home_paths import under_pytest
from . import retention_fs as rfs

PREFIX = "subfleet-attempt-"
#: Removal renames the allocation to this, then checks what it moved (P3-8).
TOMBSTONE = PREFIX + "removing-"
MARKER = ".subfleet-attempt.json"
ORPHAN_AGE_S = 24 * 3600
TERMINAL = frozenset({"succeeded", "failed", "interrupted", "cancelled", "lost", "completed"})
IDENTITY = ("dev", "ino", "uid")


def user_temp_root() -> Path:
    # Test subprocesses must stay in their session's scratch, including after
    # fixture undo. Production on Darwin never inherits the daemon's TMPDIR.
    if sys.platform == "darwin" and not under_pytest():
        value = os.confstr(os.confstr_names.get("CS_DARWIN_USER_TEMP_DIR", 65537))
        if not value:
            raise OSError("the user's Darwin temp root is unavailable")
        return Path(value).resolve(strict=True)
    return Path(tempfile.gettempdir()).resolve(strict=True)


def env_value(path: str) -> str:
    """`TMPDIR` as the platform spells its own: the folder and one trailing slash,
    so `"${TMPDIR}name"` names a file inside it, never a sibling (P3-7)."""
    return path.rstrip("/") + "/"


class AttemptTemp:
    def __init__(self, store_root: Path, *, root: Path | None = None, log=None):
        self.store_root = str(Path(store_root).resolve())
        self.root = (root if root is not None else user_temp_root()).resolve(strict=True)
        self.log = log or logging.getLogger(__name__)

    def create(self, attempt_id: str) -> dict:
        # mkdtemp supplies an exclusive mkdir(0700), even for colliding sanitized
        # ids or independent stores launching identically named attempts.
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "-", attempt_id)[:100] or "attempt"
        path = Path(tempfile.mkdtemp(prefix=PREFIX + safe_id + "-", dir=self.root))
        os.chmod(path, 0o700)
        found = path.lstat()
        # `created_at` is the daemon's clock; the caller records all of this in
        # the store, which is the allocation's authority.
        record = {"attempt_id": attempt_id, "store_root": self.store_root,
                  "created_at": time.time(), "dev": found.st_dev, "ino": found.st_ino,
                  "uid": found.st_uid}
        # The marker only tells this store's folders from another's. It carries
        # no clock: nothing the provider can write decides a folder's age.
        marker = {key: record[key] for key in ("attempt_id", "store_root", *IDENTITY)}
        atomic_publish(path / MARKER, (json.dumps(marker) + "\n").encode())
        return {**record, "path": str(path)}

    @staticmethod
    def _is(record: dict, found: os.stat_result) -> bool:
        """`found` (an lstat) is the recorded allocation: a real directory of this
        user with the recorded device, inode and owner."""
        return (stat.S_ISDIR(found.st_mode) and found.st_uid == os.getuid()
                and (found.st_dev, found.st_ino, found.st_uid) ==
                (record.get("dev"), record.get("ino"), record.get("uid")))

    def _marker(self, path: Path, found: os.stat_result) -> dict | None:
        """This store's marker, written for exactly this folder, or None.

        Consulted only to refuse: a folder without one is never removed as
        unknown. It never makes a folder known, and never says how old it is.
        """
        try:
            fd = os.open(path, rfs.O_DIR)
            try:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino) != (found.st_dev, found.st_ino):
                    return None
                marker = os.open(MARKER, rfs.O_FILE, dir_fd=fd)
                try:
                    info = os.fstat(marker)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 8192 or info.st_uid != os.getuid():
                        return None
                    record = json.loads(os.read(marker, 8193))
                finally:
                    os.close(marker)
            finally:
                os.close(fd)
        except (OSError, ValueError, TypeError):
            return None
        if (not isinstance(record, dict) or record.get("store_root") != self.store_root
                or tuple(record.get(key) for key in IDENTITY) != (found.st_dev, found.st_ino, found.st_uid)):
            return None
        return record

    def presence(self, record: dict) -> str:
        """Where a recorded allocation is: `present`; `absent` (nothing has its
        path); `replaced` (something else has it, which is never removed); or
        `outside` (its path is not one of this temp root's allocations). Any
        other failure to look raises, and the caller keeps the allocation."""
        path = Path(record["path"])
        try:
            found = path.lstat()             # never follow a top-level symlink
        except FileNotFoundError:
            return "absent"
        if path.parent != self.root or not path.name.startswith(PREFIX):
            return "outside"
        return "present" if self._is(record, found) else "replaced"

    def owns(self, record: dict) -> bool:
        """The store's recorded allocation is at its path, whatever its marker says."""
        try:
            return self.presence(record) == "present"
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def size(self, record: dict) -> int | None:
        """Apparent bytes under the allocation, or None when it cannot be read."""
        try:
            if not self.owns(record):
                return 0
            fd = rfs.open_dir(record["path"])
            try:
                if not self._is(record, os.fstat(fd)):
                    return 0
                return sum(found.st_size for _, found, _, _ in rfs.walk(fd) if not stat.S_ISDIR(found.st_mode))
            finally:
                os.close(fd)
        except (OSError, rfs.TreeError, KeyError, TypeError):
            return None

    def remove(self, record: dict, subject: dict, *, verified_empty: bool) -> str | None:
        """Release an allocation: the receipt's outcome once the store may forget
        it (`removed`, `absent`, `replaced`, `outside`), or None while it stays.

        An absent folder is released whatever holds it: there is nothing to
        keep. Anything else needs a terminal attempt (never a quarantined one)
        and a verified-empty census (C-12.10). Nothing else holds scratch.
        """
        try:
            where = self.presence(record)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.log.warning("TMPDIR removal deferred for %s: %s", record.get("attempt_id"), type(exc).__name__)
            return None
        if where == "absent":
            return "absent"                  # the provider may remove its own scratch
        if (not verified_empty or subject.get("state") not in TERMINAL
                or record.get("attempt_id") != (subject.get("attempt_id") or subject.get("holder"))):
            return None
        if where != "present":
            # Not the allocation: whatever has its path is left alone.
            self.log.warning("TMPDIR %s for %s is %s; nothing removed", record.get("path"), record.get("attempt_id"), where)
            return where
        return "removed" if self._remove(record) else None

    def _remove(self, record: dict) -> bool:
        """Delete exactly the recorded allocation (P3-8).

        Rename it to a fresh tombstone in the same parent, then check that the
        entry moved is still the store's recorded directory (device, inode,
        owner, no symlink). If it is not, or the deletion fails, put it back
        and refuse. A crash leaves a tombstone, an unknown folder the sweep
        ages from when the store first sees it.
        """
        name = Path(record["path"]).name
        tomb = TOMBSTONE + secrets.token_hex(8)
        parent = os.open(self.root, rfs.O_DIR)
        moved = False
        try:
            os.rename(name, tomb, src_dir_fd=parent, dst_dir_fd=parent)
            moved = True
            found = os.stat(tomb, dir_fd=parent, follow_symlinks=False)
            if not self._is(record, found):
                self.log.warning("TMPDIR removal refused for %s: its folder was swapped", record.get("attempt_id"))
                return False
            fd = rfs.open_dir(tomb, dir_fd=parent, expect=found)
            try:
                _empty(fd, found.st_dev)
            finally:
                os.close(fd)
            os.rmdir(tomb, dir_fd=parent)
            moved = False
            return True
        except (OSError, rfs.TreeError) as exc:
            self.log.warning("TMPDIR removal deferred for %s: %s", record.get("attempt_id"), type(exc).__name__)
            return False
        finally:
            if moved:
                _put_back(parent, tomb, name, self.log)
            os.close(parent)

    def sweep_unknown(self, named: Callable[[str], bool], seen: Callable[[str], dict | None],
                      note: Callable[[dict], None], removed: Callable[[dict], None], *,
                      now: float | None = None) -> None:
        """Age and remove this store's prefix folders that no allocation names.

        `named(path)` says whether any allocation the store records has that
        path. `seen(path)` is the store's first sighting of it, `note` records
        one, and `removed` records a removal. Age runs from the store's
        sighting, by the daemon's clock, and removal checks the sighting's
        identity: a folder swapped in since is a new sighting.
        """
        clock = time.time() if now is None else now
        for path in sorted(self.root.iterdir()):
            try:
                if named(str(path)) or not path.name.startswith(PREFIX):
                    continue
                found = path.lstat()
                if (not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid()
                        or self._marker(path, found) is None):
                    continue                 # not this store's to judge; nothing is recorded
                identity = {"path": str(path), "dev": found.st_dev, "ino": found.st_ino, "uid": found.st_uid}
                first = seen(str(path))
                if first is None or any(first.get(key) != identity[key] for key in IDENTITY):
                    note({**identity, "first_seen_at": clock})
                    continue
                born = first["first_seen_at"]   # the store's clock: never the folder's or its marker's
                if not isinstance(born, (int, float)) or clock - born < ORPHAN_AGE_S:
                    continue
                size = self.size(identity)
                if self._remove(identity):
                    removed({**identity, "bytes_at_cleanup": size})
            except Exception as exc:
                # Neither an inspection outage nor a stale directory blocks
                # startup, finalization, or the rest of retention's work.
                self.log.warning("TMPDIR sweep deferred for %s: %s", path.name, type(exc).__name__)


def _put_back(parent: int, tomb: str, name: str, log) -> None:
    """Return a moved entry to its name, never over one that appeared since."""
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
        log.warning("TMPDIR tombstone %s kept: %s reappeared", tomb, name)
        return
    except FileNotFoundError:
        pass
    except OSError:
        return
    try:
        os.rename(tomb, name, src_dir_fd=parent, dst_dir_fd=parent)
    except OSError as exc:
        log.warning("TMPDIR tombstone %s kept: %s", tomb, type(exc).__name__)


def _empty(top: int, dev: int) -> None:
    """Delete everything under an open, verified directory.

    Never follows a symlink (it is unlinked) and never leaves the device.
    Each directory inside the tree, and the tree itself, is made readable,
    writable and searchable by its owner before it is emptied, so a read-only
    tree (Go's 0555 module cache) goes too; nothing above the tree changes.
    """
    def writable(fd: int) -> None:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode & 0o700 != 0o700:
            os.fchmod(fd, mode | 0o700)

    writable(top)
    stack: list[tuple[int, list[str], str | None]] = [(top, os.listdir(top), None)]
    try:
        while stack:
            fd, names, own = stack[-1]
            if not names:
                stack.pop()
                if own is not None:
                    os.close(fd)
                    os.rmdir(own, dir_fd=stack[-1][0])
                continue
            name = names.pop()
            found = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if found.st_dev != dev:
                raise rfs.TreeError("mount-point", name)
            if not stat.S_ISDIR(found.st_mode):
                os.unlink(name, dir_fd=fd)
                continue
            if stat.S_IMODE(found.st_mode) & 0o500 != 0o500:
                # Unreadable or unsearchable: open needs read. The mode change
                # never follows a symlink, and the open below checks identity.
                os.chmod(name, stat.S_IMODE(found.st_mode) | 0o700, dir_fd=fd, follow_symlinks=False)
            child = rfs.open_dir(name, dir_fd=fd, expect=found)
            try:
                writable(child)
                stack.append((child, os.listdir(child), name))
            except BaseException:
                os.close(child)
                raise
    finally:
        for fd, _, own in stack:
            if own is not None:
                os.close(fd)


def probe_tmpdir_rows(store, *, job_id: str | None = None, pinning: bool = False) -> list[dict]:
    """Latest durable probe allocations not yet released, with no filesystem reads.

    `pinning`: only those still held, which keep their job (an allocation
    whose census was verified empty, `cleared_at`, holds nothing)."""
    return store.query(
        "SELECT job_id,json_extract(data_json,'$.holder') AS holder,json_extract(data_json,'$.tmpdir') AS tmpdir "
        "FROM events WHERE event_id IN ("
        "SELECT MAX(event_id) FROM events WHERE kind='probe.state' AND json_valid(data_json) "
        + ("AND job_id=? " if job_id is not None else "")
        + "GROUP BY json_extract(data_json,'$.holder')) "
        "AND json_type(data_json,'$.tmpdir')='object' "
        "AND json_extract(data_json,'$.tmpdir.removed_at') IS NULL"
        + (" AND json_extract(data_json,'$.tmpdir.cleared_at') IS NULL" if pinning else ""),
        (job_id,) if job_id is not None else ())


def job_tmpdirs(store, store_root: Path, *, job_id: str | None = None):
    """Validated, still-present allocations, counted in their job's bytes."""
    scratch = AttemptTemp(store_root)
    rows = store.query(
        "SELECT job_id,json_extract(evidence_json,'$.tmpdir') AS tmpdir FROM attempts "
        "WHERE json_valid(evidence_json) AND json_type(evidence_json,'$.tmpdir')='object' "
        "AND json_extract(evidence_json,'$.tmpdir.removed_at') IS NULL"
        + (" AND job_id=?" if job_id is not None else ""), (job_id,) if job_id is not None else ())
    for row in [*rows, *probe_tmpdir_rows(store, job_id=job_id)]:
        try:
            record = json.loads(row["tmpdir"])
            if scratch.owns(record):
                yield row["job_id"], record
        except (TypeError, ValueError, KeyError):
            continue
