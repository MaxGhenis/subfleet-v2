"""Private provider scratch and conservative reclamation (C-12.10, D-TMP1).

The store is the authority. An attempt's evidence (a probe's record) names its
allocation; a sweep decides whether any other folder is known, and how old it
is, from the store alone. A provider can write its folder's marker and its
timestamps, birth time included, so neither ever makes a folder known or old.
A marker can only keep a folder: one naming another store, or none, is never
removed as unknown, unless the store's own journal says the store made it.

Every folder this store makes is named in the store before it exists (the
intents journal, until its marker is published), and every rename removal
makes goes to a tombstone name the store recorded first (the allocation
record, or the unknown folder's sighting). So a crash at any point leaves a
folder the store can still name, and a later sweep finishes it (D-TMP1-P3).
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
from typing import Callable

from .guardian import atomic_publish
from .home_paths import under_pytest
from . import retention_fs as rfs

PREFIX = "subfleet-attempt-"
#: Removal renames the allocation to this, then checks what it moved (P3-8).
TOMBSTONE = PREFIX + "removing-"
MARKER = ".subfleet-attempt.json"
#: The store root's journal of folders being made: each name is written here
#: before its mkdir and removed once the folder's marker is published (T1).
INTENTS = "tmpdir-intents"
ORPHAN_AGE_S = 24 * 3600
TERMINAL = frozenset({"succeeded", "failed", "interrupted", "cancelled", "lost", "completed"})
IDENTITY = ("dev", "ino", "uid")
_SUFFIX = "abcdefghijklmnopqrstuvwxyz0123456789_"     # tempfile's alphabet


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


def new_tombstone() -> str:
    """A fresh tombstone name, recorded in the store before any rename uses it."""
    return TOMBSTONE + secrets.token_hex(8)


#: The codes removal's refusal of a tree can carry (`_empty`, `retention_fs.open_dir`):
#: the only ones its log line names, never the refusal's detail, a name a
#: provider chose (C-25.7).
TREE_CODES = ("mount-point", "swapped", "too-deep")


class AttemptTemp:
    def __init__(self, store_root: Path, *, root: Path | None = None, log=None):
        self.store_root = str(Path(store_root).resolve())
        self.root = (root if root is not None else user_temp_root()).resolve(strict=True)
        self.log = log or logging.getLogger(__name__)
        self.intents = Path(self.store_root) / INTENTS
        self._creating: set[str] = set()       # names whose create() is running in this process
        self._guard = threading.Lock()

    def create(self, attempt_id: str) -> dict:
        """A fresh, marked folder for `attempt_id`; the caller records it in the store.

        `mkdir(0700)` is exclusive, even for colliding sanitized ids or
        independent stores launching identically named attempts. The name is in
        this store's journal before the folder exists and until its marker is
        published, so a crash anywhere in between leaves a folder the sweep can
        tell is this store's (T1). A failure once the folder exists removes it:
        it is empty and was made here (P3-3).
        """
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "-", attempt_id)[:100] or "attempt"
        for _ in range(100):
            name = PREFIX + safe_id + "-" + "".join(secrets.choice(_SUFFIX) for _ in range(8))
            with self._guard:
                if name in self._creating:
                    continue
                self._creating.add(name)
            try:
                self._intend(name)
                try:
                    os.mkdir(self.root / name, 0o700)
                except FileExistsError:
                    self._settle(name)
                    continue
                except BaseException:
                    self._settle(name)
                    raise
                record = self._mark(name, attempt_id)
                self._settle(name)
                return record
            finally:
                with self._guard:
                    self._creating.discard(name)
        raise FileExistsError(errno.EEXIST, "no unused attempt TMPDIR name")

    def _mark(self, name: str, attempt_id: str) -> dict:
        path = self.root / name
        try:
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
        except BaseException:
            # P3-3: nothing has been given the folder, so it is still empty. One
            # that will not go keeps its journal entry, and a sweep removes it.
            try:
                os.rmdir(path)
            except OSError as exc:
                self.log.warning("TMPDIR %s kept after its allocation failed: %s", name, type(exc).__name__)
            else:
                self._settle(name)
            raise
        # The tombstone removal will use is recorded with the allocation, before
        # any rename (P3-1).
        return {**record, "path": str(path), "tombstone": new_tombstone()}

    def _intend(self, name: str) -> None:
        try:
            self.intents.mkdir(mode=0o700, parents=True)
        except OSError:
            if not self.intents.is_dir():
                raise
        else:
            # r3: the journal folder's own entry survives a power loss before any
            # name in it is relied on.
            parent = os.open(self.intents.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        atomic_publish(self.intents / name, (json.dumps({"path": str(self.root / name)}) + "\n").encode())

    def _settle(self, name: str) -> None:
        try:
            os.unlink(self.intents / name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.log.warning("TMPDIR journal entry %s kept: %s", name, type(exc).__name__)

    def _intended(self) -> set[str]:
        """The journal's names, except those a create() in this process is making."""
        try:
            names = os.listdir(self.intents)
        except FileNotFoundError:
            return set()
        with self._guard:
            busy = set(self._creating)
        return {name for name in names if name.startswith(PREFIX) and "/" not in name} - busy

    @staticmethod
    def _is(record: dict, found: os.stat_result) -> bool:
        """`found` (an lstat) is the recorded allocation: a real directory of this
        user with the recorded device, inode and owner."""
        return (stat.S_ISDIR(found.st_mode) and found.st_uid == os.getuid()
                and (found.st_dev, found.st_ino, found.st_uid) ==
                (record.get("dev"), record.get("ino"), record.get("uid")))

    def _tombstone(self, record: dict) -> str | None:
        """The tombstone name the store recorded for this allocation or sighting.

        One recorded before D-TMP1-P3 has none; its name is derived from what
        the store recorded instead, so it too is fixed before any rename. A
        recorded value that is not a tombstone name is None: removal refuses.
        """
        name = record.get("tombstone")
        if name is None:
            key = json.dumps([self.store_root, record.get("path"), *(record.get(k) for k in IDENTITY)])
            return TOMBSTONE + hashlib.sha256(key.encode()).hexdigest()[:16]
        if isinstance(name, str) and name.startswith(TOMBSTONE) and "/" not in name and len(name) <= 128:
            return name
        return None

    def tombstone_path(self, record: dict) -> str | None:
        name = self._tombstone(record)
        return None if name is None else str(self.root / name)

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
        """Where a recorded allocation is: `present` (at its path); `tombstoned`
        (at the tombstone the store recorded for it, where a crash during its
        removal left it); `absent` (neither its path nor its tombstone has it,
        and nothing has its path); `replaced` (something else has its path,
        which is never removed); or `outside` (its path is not one of this temp
        root's allocations). Any other failure to look raises, and the caller
        keeps the allocation."""
        path = Path(record["path"])
        try:
            found = path.lstat()             # never follow a top-level symlink
        except FileNotFoundError:
            found = None
        if found is not None:
            if path.parent != self.root or not path.name.startswith(PREFIX):
                return "outside"
            if self._is(record, found):
                return "present"
        tomb = self._tombstone(record)
        if tomb is not None:
            try:
                if self._is(record, os.lstat(self.root / tomb)):
                    return "tombstoned"
            except FileNotFoundError:
                pass
        return "absent" if found is None else "replaced"

    def owns(self, record: dict) -> bool:
        """The store's recorded allocation is at its path, whatever its marker says."""
        try:
            return self.presence(record) == "present"
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def located(self, record: dict) -> str | None:
        """The path the recorded allocation is at now (its own or its tombstone's),
        or None when it is at neither or cannot be looked at."""
        try:
            where = self.presence(record)
        except (OSError, ValueError, TypeError, KeyError):
            return None
        if where == "present":
            return record["path"]
        return self.tombstone_path(record) if where == "tombstoned" else None

    def size(self, record: dict) -> int | None:
        """Apparent bytes under the allocation, or None when it cannot be read."""
        try:
            where = self.located(record)
            if where is None:
                return 0
            with rfs._cleanup([None]) as owner:
                owner.resources[0] = rfs.open_dir(where)
                fd = owner.resources[0]
                if not self._is(record, os.fstat(fd)):
                    return 0
                iterator = rfs.walk(fd)
                with rfs._cleanup((iterator,), close=lambda it: it.close()):
                    return sum(found.st_size for _, found, _, _ in iterator if not stat.S_ISDIR(found.st_mode))
        except (OSError, rfs.TreeError, KeyError, TypeError):
            return None

    def remove(self, record: dict, subject: dict, *, verified_empty: bool) -> str | None:
        """Release an allocation: the receipt's outcome once the store may forget
        it (`removed`, `absent`, `replaced`, `outside`), or None while it stays.

        An absent folder is released whatever holds it: there is nothing to
        keep. Anything else needs a terminal attempt (never a quarantined one)
        and a verified-empty census (C-12.10). Nothing else holds scratch. A
        tombstoned allocation is not absent: removal finishes it (P3-1).
        `verified_empty` here is the holder's census having found none of its
        own processes, verifiably (`Containment.containment_empty`): a cwd
        observer in the job's worktree holds only the worktree (C-5.5).
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
        if where in ("replaced", "outside"):
            # Not the allocation: whatever has its path is left alone.
            self.log.warning("TMPDIR %s for %s is %s; nothing removed", record.get("path"), record.get("attempt_id"), where)
            return where
        return "removed" if self._remove(record) else None

    def _remove(self, record: dict) -> bool:
        """Delete exactly the recorded allocation (P3-8, P3-1).

        Rename it to the tombstone the store recorded for it, in the same
        parent, then check that the entry there is still the store's recorded
        directory (device, inode, owner, no symlink). A tombstone already
        holding it is what a crash left: it is finished the same way. If the
        check or the deletion fails, put it back and refuse. Because the name
        was recorded before the rename, a crash leaves a folder the store
        names, which `presence()` and the sweep find and finish.

        Opening the folder needs owner read and search, and macOS 15 refuses to
        rename a directory without owner write, even within one parent, where
        macOS 26 allows it (P3-2, r2). So a folder lacking any of them is set to
        0700 before its rename, once its entry passes the same identity check
        as after the rename; a crash-left tombstone, which no rename precedes,
        is set to 0700 before it is opened. Every mode change sets exactly
        0700, never the folder's mode plus owner bits (r3), and goes through the
        folder's descriptor, opened without following a symlink and checked
        again, so it reaches nothing else (`_own`). Only a folder without owner
        read, which no open admits (0000, 0300), is changed by name, never
        through a symlink: an entry swapped in between the check and the mode
        change can be set to 0700, only if it is this user's (a symlink's own
        mode, never its target's); it never gains group or other bits, and the
        check after the rename then refuses it. After a refused removal, the
        folder put back is at 0700 (one that already had owner read, write and
        search keeps its own mode: removal never changed it).
        """
        name = Path(record["path"]).name
        tomb = self._tombstone(record)
        if tomb is None:
            self.log.warning("TMPDIR removal refused for %s: its recorded tombstone is not one", record.get("attempt_id"))
            return False
        parent = os.open(self.root, rfs.O_DIR)
        moved = False
        try:
            try:
                found = os.stat(tomb, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                found = None
            if found is None:
                source = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not self._is(record, source):
                    self.log.warning("TMPDIR removal refused for %s: its folder was swapped", record.get("attempt_id"))
                    return False
                if stat.S_IMODE(source.st_mode) & 0o700 != 0o700:
                    # r2: before the rename, which macOS 15 refuses without owner write.
                    if not self._own(record, name, parent):
                        self.log.warning("TMPDIR removal refused for %s: its folder was swapped", record.get("attempt_id"))
                        return False
                os.rename(name, tomb, src_dir_fd=parent, dst_dir_fd=parent)
                moved = True
                found = os.stat(tomb, dir_fd=parent, follow_symlinks=False)
            elif self._is(record, found):
                moved = True                 # left by a crash mid-removal: finish it
            else:
                self.log.warning("TMPDIR removal refused for %s: something else has its tombstone", record.get("attempt_id"))
                return False
            if not self._is(record, found):
                self.log.warning("TMPDIR removal refused for %s: its folder was swapped", record.get("attempt_id"))
                return False
            if stat.S_IMODE(found.st_mode) & 0o700 != 0o700:
                # P3-2: a tombstone a crash left before its mode was fixed, which
                # opening needs. Checked above.
                if not self._own(record, tomb, parent):
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
        except rfs.TreeError as exc:
            for code in TREE_CODES:          # the log names a literal of the set, never the refusal's words
                if exc.reason == code:
                    break
            else:
                code = None
            self.log.warning("TMPDIR removal deferred for %s: %s", record.get("attempt_id"),
                             f"{type(exc).__name__}({code})" if code else type(exc).__name__)
            return False
        except OSError as exc:
            self.log.warning("TMPDIR removal deferred for %s: %s", record.get("attempt_id"), type(exc).__name__)
            return False
        finally:
            if moved:
                _put_back(parent, tomb, name, self.log)
            os.close(parent)

    def _own(self, record: dict, name: str, parent: int) -> bool:
        """Set the recorded directory at `name` in `parent` to exactly 0700 (r3).

        It is opened without following a symlink, checked against the record
        through that descriptor, and changed through it, so the change reaches
        nothing else. An open refused with EACCES (no owner read: 0000, 0300)
        is the one case changed by name, after the caller's check of the entry
        and never through a symlink. False, with nothing changed: the entry
        there now is not the recorded directory. Any other failure to open
        raises, and removal defers.
        """
        try:
            fd = os.open(name, rfs.O_DIR, dir_fd=parent)
        except PermissionError as exc:
            if exc.errno != errno.EACCES:
                raise
            os.chmod(name, stat.S_IRWXU, dir_fd=parent, follow_symlinks=False)
            return True
        try:
            if not self._is(record, os.fstat(fd)):
                return False
            os.fchmod(fd, stat.S_IRWXU)
            return True
        finally:
            os.close(fd)

    def sweep_unknown(self, named: Callable[[str], bool], seen: Callable[[str], dict | None],
                      note: Callable[[dict], None], removed: Callable[[dict], None], *,
                      tombstoned: Callable[[str], dict | None] | None = None,
                      now: float | None = None) -> None:
        """Age and remove this store's prefix folders that no allocation names.

        `named(path)` says whether any allocation the store records has that
        path, or has it as its tombstone. `seen(path)` is the store's first
        sighting of it, `note` records one (with the tombstone its removal will
        use), and `removed` records a removal. `tombstoned(path)` is the
        store's current sighting whose recorded tombstone is `path`, if any.
        Age runs from the store's sighting, by the daemon's clock, and removal
        checks the sighting's identity: a folder swapped in since is a new
        sighting. A folder is this store's when it carries this store's marker
        for exactly that folder, or when the store's journal says the store
        made it (a crash before its marker, T1).
        """
        clock = time.time() if now is None else now
        intended = self._intended()
        for path in sorted(self.root.iterdir()):
            try:
                if named(str(path)) or not path.name.startswith(PREFIX):
                    continue
                found = path.lstat()
                if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid():
                    continue                 # not this store's to judge; nothing is recorded
                pending = tombstoned(str(path)) if tombstoned is not None else None
                if pending is not None and self._is(pending, found):
                    # A removal the store recorded and began (P3-1): finish it.
                    born = pending.get("first_seen_at")
                    if isinstance(born, (int, float)) and clock - born >= ORPHAN_AGE_S:
                        identity = {key: pending[key] for key in ("path", *IDENTITY)}
                        size = self.size(pending)
                        if self._remove(pending):
                            removed({**identity, "bytes_at_cleanup": size})
                    continue
                if self._marker(path, found) is None and path.name not in intended:
                    continue                 # not this store's to judge; nothing is recorded
                identity = {"path": str(path), "dev": found.st_dev, "ino": found.st_ino, "uid": found.st_uid}
                first = seen(str(path))
                if first is None or any(first.get(key) != identity[key] for key in IDENTITY):
                    note({**identity, "first_seen_at": clock, "tombstone": new_tombstone()})
                    continue
                born = first["first_seen_at"]   # the store's clock: never the folder's or its marker's
                if not isinstance(born, (int, float)) or clock - born < ORPHAN_AGE_S:
                    continue
                sighted = {**first, **identity}
                size = self.size(sighted)
                if self._remove(sighted):
                    removed({**identity, "bytes_at_cleanup": size})
            except Exception as exc:
                # Neither an inspection outage nor a stale directory blocks
                # startup, finalization, or the rest of retention's work.
                self.log.warning("TMPDIR sweep deferred for %s: %s", path.name, type(exc).__name__)
        self._settle_intents(intended, named, clock=time.time())

    def _settle_intents(self, intended: set[str], named: Callable[[str], bool], *, clock: float) -> None:
        """Drop the journal's entries a folder no longer needs: its folder is gone
        (or is no directory of this user's), a record names it, or it carries
        this store's marker. A publish a crash cut short leaves a dot-named
        temporary file; one an hour old belongs to no create still running."""
        for name in intended:
            path = self.root / name
            try:
                found = path.lstat()
            except FileNotFoundError:
                self._settle(name)
                continue
            except OSError:
                continue
            if (not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid() or named(str(path))
                    or self._marker(path, found) is not None):
                self._settle(name)
        try:
            debris = [name for name in os.listdir(self.intents) if name.startswith("." + PREFIX)]
        except FileNotFoundError:
            return
        for name in debris:
            try:
                if clock - os.lstat(self.intents / name).st_mtime > 3600:
                    os.unlink(self.intents / name)
            except OSError:
                continue


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
    Each directory inside the tree that lacks owner read, write or search is
    set to exactly 0700 (r3) before it is emptied, and `_remove` does the same
    for the tree itself before renaming or opening it (P3-2, r2), so a
    read-only or unreadable tree (Go's 0555 module cache, a 0000 TMPDIR) goes
    too; nothing above the tree changes. The change goes through the
    directory's descriptor, opened without following a symlink and checked
    against its lstat; only one whose open is refused with EACCES (no owner
    read) is changed by name first, never through a symlink, so an entry
    swapped in between can be set to 0700, only if it is this user's, and
    never gains group or other bits. Nothing inside the tree is renamed,
    so macOS 15's rename rule never applies to it (r2). A tree deeper than
    `retention_fs.MAX_DEPTH` is refused, as `size()` refuses to measure it, so
    removal holds at most that many descriptors (P3-5).
    """
    def writable(fd: int) -> None:
        if stat.S_IMODE(os.fstat(fd).st_mode) & 0o700 != 0o700:
            os.fchmod(fd, stat.S_IRWXU)

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
            if len(stack) >= rfs.MAX_DEPTH:
                raise rfs.TreeError("too-deep", name)
            try:
                child = rfs.open_dir(name, dir_fd=fd, expect=found)
            except PermissionError as exc:
                if exc.errno != errno.EACCES:
                    raise
                # No owner read, so no open (r3): by name, never through a
                # symlink, and the open below checks identity.
                os.chmod(name, stat.S_IRWXU, dir_fd=fd, follow_symlinks=False)
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
    """Validated allocations still there, counted in their job's bytes: at their
    path, or at the tombstone a crash during their removal left."""
    scratch = AttemptTemp(store_root)
    rows = store.query(
        "SELECT job_id,json_extract(evidence_json,'$.tmpdir') AS tmpdir FROM attempts "
        "WHERE json_valid(evidence_json) AND json_type(evidence_json,'$.tmpdir')='object' "
        "AND json_extract(evidence_json,'$.tmpdir.removed_at') IS NULL"
        + (" AND job_id=?" if job_id is not None else ""), (job_id,) if job_id is not None else ())
    for row in [*rows, *probe_tmpdir_rows(store, job_id=job_id)]:
        try:
            record = json.loads(row["tmpdir"])
            where = scratch.located(record)
            if where is not None:
                yield row["job_id"], {**record, "path": where}
        except (TypeError, ValueError, KeyError, AttributeError):
            continue


def cleared_tmpdirs(store, job_id: str) -> list[str]:
    """The folders a job's attempts record as cleared (census verified empty)
    but not removed: what retiring the job would leave unrecorded (P3-2)."""
    return [row["path"] for row in store.query(
        "SELECT json_extract(evidence_json,'$.tmpdir.path') AS path FROM attempts "
        "WHERE job_id=? AND json_valid(evidence_json) AND json_type(evidence_json,'$.tmpdir')='object' "
        "AND json_extract(evidence_json,'$.tmpdir.cleared_at') IS NOT NULL "
        "AND json_extract(evidence_json,'$.tmpdir.removed_at') IS NULL", (job_id,))
        if isinstance(row["path"], str)]
