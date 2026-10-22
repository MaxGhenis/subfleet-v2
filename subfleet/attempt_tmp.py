"""Private provider scratch and conservative reclamation (C-12.10, D-TMP1).

The marker binds an allocation to its store and filesystem identity. Unknown
allocations are aged from creation, never from provider-controlled mtimes.
"""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time
from typing import Callable

from .guardian import atomic_publish
from .home_paths import under_pytest
from . import retention_fs as rfs

PREFIX = "subfleet-attempt-"
MARKER = ".subfleet-attempt.json"
ORPHAN_AGE_S = 24 * 3600
TERMINAL = frozenset({"succeeded", "failed", "interrupted", "cancelled", "lost", "contained", "completed"})


def user_temp_root() -> Path:
    # Test subprocesses must stay in their session's scratch, including after
    # fixture undo. Production on Darwin never inherits the daemon's TMPDIR.
    if sys.platform == "darwin" and not under_pytest():
        value = os.confstr(os.confstr_names.get("CS_DARWIN_USER_TEMP_DIR", 65537))
        if not value:
            raise OSError("the user's Darwin temp root is unavailable")
        return Path(value).resolve(strict=True)
    return Path(tempfile.gettempdir()).resolve(strict=True)


def retained(subject: dict) -> bool:
    try:
        evidence = json.loads(subject.get("evidence_json") or "{}")
    except (ValueError, TypeError):
        return True                         # unreadable authority keeps scratch
    return not isinstance(evidence, dict) or bool(
        evidence.get("salvage_error") or evidence.get("salvage_skipped") or evidence.get("tmpdir_keep"))


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
        record = {"attempt_id": attempt_id, "store_root": self.store_root,
                  "created_at": time.time(), "dev": found.st_dev, "ino": found.st_ino,
                  "uid": found.st_uid}
        atomic_publish(path / MARKER, (json.dumps(record) + "\n").encode())
        return {**record, "path": str(path)}

    def _read(self, path: Path) -> dict | None:
        """One deletion authority check, used by direct cleanup and the sweep."""
        if path.parent != self.root or not path.name.startswith(PREFIX):
            return None
        try:
            found = path.lstat()             # never follow a top-level symlink
            if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid():
                return None
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
            if (record.get("store_root") != self.store_root or not isinstance(record.get("attempt_id"), str)
                    or (record.get("dev"), record.get("ino"), record.get("uid")) !=
                    (found.st_dev, found.st_ino, found.st_uid)
                    or type(record.get("created_at")) not in (int, float)
                    or not math.isfinite(record["created_at"])):
                return None
            # A provider can edit its marker, but cannot backdate the OS birth
            # time. On filesystems without it ctime conservatively delays age.
            created = max(record["created_at"], getattr(found, "st_birthtime", found.st_ctime))
            return {**record, "created_at": created, "path": str(path)}
        except (OSError, ValueError, TypeError, AttributeError, OverflowError):
            return None

    def size(self, record: dict) -> int:
        path = Path(record["path"])
        current = self._read(path)
        if current is None or any(current[key] != record.get(key) for key in ("dev", "ino", "attempt_id")):
            return 0
        fd = rfs.open_dir(path)
        try:
            return sum(found.st_size for _, found, _, _ in rfs.walk(fd) if not stat.S_ISDIR(found.st_mode))
        finally:
            os.close(fd)

    def remove(self, record: dict, subject: dict, *, verified_empty: bool) -> bool:
        if (not verified_empty or subject.get("state") not in TERMINAL or retained(subject)
                or record.get("attempt_id") != (subject.get("attempt_id") or subject.get("holder"))):
            return False
        try:
            path = Path(record["path"])
            if path.parent != self.root or not path.name.startswith(PREFIX):
                return False
            if not os.path.lexists(path):
                return True                  # the provider may remove its own scratch
            current = self._read(path)
            if current is None or any(current[key] != record.get(key) for key in ("dev", "ino", "attempt_id")):
                self.log.warning("TMPDIR removal deferred for %s: allocation identity unavailable", record.get("attempt_id"))
                return False
            # shutil's descriptor-relative walker refuses symlinks swapped in
            # after its own lstat; its directory fd pins the canonical parent.
            parent = os.open(self.root, rfs.O_DIR)
            try:
                shutil.rmtree(path.name, dir_fd=parent)
            finally:
                os.close(parent)
            return True
        except (OSError, ValueError, KeyError, rfs.TreeError) as exc:
            self.log.warning("TMPDIR removal deferred for %s: %s", record.get("attempt_id"), type(exc).__name__)
            return False

    def sweep(self, lookup: Callable[[str], dict | None], verified_empty: Callable[[dict], bool], *,
              now: float | None = None, removed: Callable[[dict], None] | None = None) -> None:
        clock = time.time() if now is None else now
        for path in self.root.iterdir():
            record = self._read(path)
            if record is None:
                continue
            try:
                subject = lookup(record["attempt_id"])
                if subject is None:
                    if clock - record["created_at"] < ORPHAN_AGE_S:
                        continue
                    subject = {"attempt_id": record["attempt_id"], "state": "lost"}
                    empty = True             # never launched, or its store rows were retired
                elif subject.get("state") not in TERMINAL or retained(subject):
                    continue
                else:
                    empty = verified_empty(subject)
                if empty:
                    record["bytes_at_cleanup"] = self.size(record)
                if self.remove(record, subject, verified_empty=empty) and removed is not None:
                    removed(record)
            except Exception as exc:
                # Neither an inspection outage nor a stale directory blocks
                # startup, finalization, or the rest of retention's work.
                self.log.warning("TMPDIR sweep deferred for %s: %s", record["attempt_id"], type(exc).__name__)


def probe_tmpdir_rows(store, *, job_id: str | None = None) -> list[dict]:
    """Latest durable probe allocations, with no filesystem reads."""
    return store.query(
        "SELECT job_id,json_extract(data_json,'$.tmpdir') AS tmpdir FROM events WHERE event_id IN ("
        "SELECT MAX(event_id) FROM events WHERE kind='probe.state' AND json_valid(data_json) "
        + ("AND job_id=? " if job_id is not None else "")
        + "GROUP BY json_extract(data_json,'$.holder')) "
        "AND json_type(data_json,'$.tmpdir')='object' "
        "AND json_extract(data_json,'$.tmpdir.removed_at') IS NULL", (job_id,) if job_id is not None else ())


def job_tmpdirs(store, store_root: Path, *, job_id: str | None = None):
    """Validated, still-present allocations whose job evidence must be kept.

    Retention must not discard a terminal attempt's census authority just
    because deletion failed. Its scratch also belongs in that job's byte count.
    """
    scratch = AttemptTemp(store_root)
    rows = store.query(
        "SELECT job_id,json_extract(evidence_json,'$.tmpdir') AS tmpdir FROM attempts "
        "WHERE json_valid(evidence_json) AND json_type(evidence_json,'$.tmpdir')='object'"
        + (" AND job_id=?" if job_id is not None else ""), (job_id,) if job_id is not None else ())
    for row in [*rows, *probe_tmpdir_rows(store, job_id=job_id)]:
        try:
            record = json.loads(row["tmpdir"])
            current = scratch._read(Path(record["path"]))
            if current is not None and all(current[key] == record.get(key) for key in ("dev", "ino", "attempt_id")):
                yield row["job_id"], record
        except (TypeError, ValueError, KeyError):
            continue
