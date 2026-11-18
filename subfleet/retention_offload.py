"""`subfleet retention offload | recall`: settled archives on another volume (C-8.8, D-ARCHIVE-OFFLOAD1).

A published retention archive (`<state>/archive/<name>/`, `retention_archive`)
holds a job's stored files under ``files/`` and the bundle of its private
commits. Both are bulky; the manifest, summary and rows are small. Offload
moves the bulky part to another volume and leaves the small part, plus a stub
(``offload.json``), on the internal disk, so the archive's name stays taken
and its records stay here. Recall brings the bulky part back.

Offload, for one archive, under an exclusive lock (``<state>/archive/.lock.<name>``):

1. the stub is written as ``copying`` (format, target volume UUID and name,
   the path under the volume, the manifest's SHA-256, file count and bytes,
   the bundle's SHA-256 read before any byte is copied, the original modes,
   time, Subfleet version);
2. ``files/`` and the bundle are copied to ``DIR/subfleet-archive/<source>/<name>/``:
   each path created exclusively, directories 0700, files 0600, each file and
   directory `F_FULLFSYNC`ed; an ownership marker binds the source, archive,
   manifest and volume UUID and records each exclusive file creation (inode
   and birth time) before its bytes; a file is copied only from internal
   bytes that hash to the manifest's SHA-256, or the stub's for the bundle;
3. the copy is read back: each stored file's SHA-256 against the manifest,
   the bundle's against the stub and its heads against the manifest;
   a mismatch stops the archive with the internal copy untouched;
4. the stub reads ``verified``; unless ``--keep-internal`` it is marked
   ``removing``, and only internal entries whose size and SHA-256 still match
   are removed; then it reads ``offloaded``;
5. one line per archive goes to ``<state>/archive/.offload.jsonl``.

Every step is safe to run again after a crash: ``copying`` keeps each owned
external entry that matches the recorded SHA-256 and replaces only owned
ones that do not, ``verified`` reads the external copy back again before it
removes anything, and a half-removed internal copy continues once the
external copy verifies again. `contents` is the one resolver that says where
an archive's ``files/`` and bundle are: the internal folder, or the external
folder once removal may have begun (on the mounted volume with the recorded
UUID; its ledger is matched by inode and birth time, not the device number a
remount changes).

Nothing here touches an archive whose job still has ``<state>/retention/<job>/``
(the name minus any ``.N`` suffix), and nothing writes outside
``<state>/archive/<name>/``, the lock and log files in ``<state>/archive/``, and
``DIR/subfleet-archive/<source>/<name>/``, except that offload creates
``DIR/subfleet-archive/`` and its ``<source>/`` when missing, sets both to 0700
and flushes them and ``DIR``. All of it runs offline, without the daemon.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from . import retention_archive as rarch
from . import retention_fs as rfs
from . import retention_git as rgit
from . import retention_volume as rvol

FORMAT = 1
STUB = "offload.json"
LOG = ".offload.jsonl"
FOLDER = "subfleet-archive"
STATES = ("copying", "verified", "offloaded")
#: The target volume must have the batch's bytes plus this share free.
HEADROOM = 0.10
STORED_MODE = 0o600
OWNER = ".owner.json"
_OWNER_TEMP = re.compile(r"^\.owner\.[0-9]+\.[0-9]+\.tmp$")
_RECALL = ".recall."
_STUB_TEMP = re.compile(r"^\.offload\.json\.[0-9]+\.tmp$")
_SUFFIX = re.compile(r"^(.+)\.([0-9]+)$")
_F_NOCACHE = getattr(fcntl, "F_NOCACHE", 48)

#: Crash injection: tests set a function that raises at a named step.
HOOK: Callable[[str], None] | None = None


def _step(name: str) -> None:
    if HOOK is not None:
        HOOK(name)


class Busy(rarch.RestoreError):
    """Another offload, recall or restore holds the archive."""


class VolumeMissing(rarch.RestoreError):
    """The volume an offloaded archive's files are on is not mounted."""


class Failed(rarch.RestoreError):
    """One archive could not be moved; what is already in place stays."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --- names, the retention-folder rule, the stub, the lock -------------------------------------

def valid_name(name: str) -> bool:
    return (isinstance(name, str) and bool(name) and "/" not in name and "\0" not in name
            and not name.startswith(".") and name not in (".", ".."))


def job_of(name: str) -> str:
    """The job an archive belongs to: its name minus any ``.N`` suffix (`_archive_name`)."""
    match = _SUFFIX.match(name)
    return match.group(1) if match else name


def retention_folder(root: Path, name: str) -> Path | None:
    """The job's retention folder, if it has one: the archive is not settled.

    The rule of the in-place compression tool (2026-11-06): the name minus any
    ``.N`` suffix; the whole name as well, for a job id that ends in ``.N``."""
    for job in dict.fromkeys((job_of(name), name)):
        folder = root / "retention" / job
        if os.path.lexists(folder):
            return folder
    return None


def stub_path(root: Path, name: str) -> Path:
    return root / "archive" / name / STUB


def _archive_fd(root: Path, name: str | None = None) -> int:
    """Pin the archive directory without following any link below the state root."""
    if name is not None and not valid_name(name):
        raise ValueError(f"invalid archive name {name!r}")
    return _open_below(root.resolve(), "archive" + (f"/{name}" if name is not None else ""))


def _read_at(folder_fd: int, name: str, limit: int) -> bytes:
    fd = os.open(name, rfs.O_FILE, dir_fd=folder_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{name} is not a regular file")
        chunks, total = [], 0
        while chunk := os.read(fd, rfs.CHUNK):
            total += len(chunk)
            if total > limit:
                raise ValueError(f"{name} is too large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_stub(root: Path, name: str) -> dict[str, Any] | None:
    """The archive's offload stub, None without one; ValueError if it is not ours."""
    try:
        folder_fd = _archive_fd(root, name)
        try:
            data = json.loads(_read_at(folder_fd, STUB, 64 << 20))
        finally:
            os.close(folder_fd)
    except FileNotFoundError:
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("state") not in STATES \
            or data.get("archive") != name or not isinstance(data.get("volume"), dict) \
            or not _valid_relative(data.get("path")):
        raise ValueError(f"{STUB} of {name} is not one this version reads")
    return data


def _save(root: Path, name: str, stub: dict[str, Any], **changes: Any) -> None:
    """Replace the stub atomically: temporary file, full sync, rename, sync the folder."""
    stub.update(changes, updated_at=_now())
    folder_fd = _archive_fd(root, name)
    temporary = f".{STUB}.{os.getpid()}.tmp"
    try:
        try:
            os.unlink(temporary, dir_fd=folder_fd)
        except FileNotFoundError:
            pass
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=folder_fd)
        try:
            view = memoryview(json.dumps(stub, sort_keys=True, indent=1).encode() + b"\n")
            while view:
                view = view[os.write(fd, view):]
            rfs.fullsync(fd)
        finally:
            os.close(fd)
        os.rename(temporary, STUB, src_dir_fd=folder_fd, dst_dir_fd=folder_fd)
        rfs.fullsync(folder_fd)
    finally:
        os.close(folder_fd)


def external_authoritative(stub: dict[str, Any]) -> bool:
    """Whether the external copy is the one to read: once removal may have
    begun, until a recall has put every file back (``recalled``)."""
    if stub.get("recalled"):
        return False
    return stub["state"] == "offloaded" or (stub["state"] == "verified" and bool(stub.get("removing")))


class ArchiveLock:
    """An exclusive, non-blocking lock on one archive: `<state>/archive/.lock.<name>`.

    The file is removed while it is still held, and a lock taken on a file
    that was removed meanwhile is taken again, so two holders never hold
    different files of one name."""

    def __init__(self, root: Path, name: str):
        if not valid_name(name):
            raise ValueError(f"invalid archive name {name!r}")
        self.path = root / "archive" / f".lock.{name}"
        self.name = name
        self.fd: int | None = None
        self.parent_fd: int | None = None

    def __enter__(self) -> ArchiveLock:
        self.parent_fd = _archive_fd(self.path.parent.parent)
        try:
            return self._acquire()
        except BaseException:
            os.close(self.parent_fd)
            self.parent_fd = None
            raise

    def _acquire(self) -> ArchiveLock:
        for _ in range(16):
            fd = os.open(self.path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=self.parent_fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise Busy(f"archive {self.name} is busy: an offload, recall or restore holds it") from None
            try:
                named = os.stat(self.path.name, dir_fd=self.parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                os.close(fd)
                continue
            held = os.fstat(fd)
            if (named.st_dev, named.st_ino) == (held.st_dev, held.st_ino):
                self.fd = fd
                return self
            os.close(fd)
        raise Busy(f"archive {self.name}: its lock file kept changing")

    def __exit__(self, *exc: Any) -> None:
        assert self.fd is not None
        try:
            os.unlink(self.path.name, dir_fd=self.parent_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(self.fd)
            self.fd = None
            assert self.parent_fd is not None
            os.close(self.parent_fd)
            self.parent_fd = None


# --- what an archive holds --------------------------------------------------------------------

@dataclass
class Plan:
    name: str
    base: Path
    job_id: str
    manifest_sha256: str
    #: store name -> (size, sha256), each stored file once.
    stores: dict[str, tuple[int, str]]
    bundle: str | None
    bundle_heads: dict[str, str]
    bundle_bytes: int

    @property
    def files_bytes(self) -> int:
        return sum(size for size, _ in self.stores.values())

    @property
    def total(self) -> int:
        return self.files_bytes + self.bundle_bytes


def _valid_relative(path: Any) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/") or "\0" in path:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def read_summary(root: Path, name: str) -> dict[str, Any]:
    folder_fd = _archive_fd(root, name)
    try:
        summary = json.loads(_read_at(folder_fd, "summary.json", 1 << 20))
    finally:
        os.close(folder_fd)
    if not isinstance(summary, dict):
        raise ValueError("summary.json is not an object")
    return summary


def plan(root: Path, name: str) -> Plan:
    """What the archive's manifest says it stores; ValueError if it won't parse."""
    base = root.resolve() / "archive" / name
    summary = read_summary(root, name)
    folder_fd = _archive_fd(root, name)
    try:
        raw = _read_at(folder_fd, "manifest.json", 4 << 30)
    finally:
        os.close(folder_fd)
    digest = hashlib.sha256(raw).hexdigest()
    if summary.get("manifest_sha256") and summary["manifest_sha256"] != digest:
        raise ValueError("manifest.json is not the one summary.json recorded")
    try:
        manifest = json.loads(raw)
        stores: dict[str, tuple[int, str]] = {}
        for _, entry in rarch.iter_entries(manifest):
            store = entry.get("store")
            if not store:
                continue
            if not valid_name(store) or store.startswith(_RECALL):
                raise ValueError(f"stored file name {store!r}")
            record = (int(entry["size"]), str(entry["sha256"]))
            if stores.setdefault(store, record) != record:
                raise ValueError(f"stored file {store} is recorded twice, differently")
        git = manifest.get("git") or {}
        bundle = git.get("bundle") or None
        if bundle is not None and (not valid_name(bundle) or bundle == STUB):
            raise ValueError(f"bundle name {bundle!r}")
        heads = dict(git.get("bundle_heads") or {}) if bundle else {}
        return Plan(name=name, base=base, job_id=str(manifest.get("job_id") or job_of(name)),
                    manifest_sha256=digest, stores=stores, bundle=bundle, bundle_heads=heads,
                    bundle_bytes=int(git.get("bundle_bytes") or 0) if bundle else 0)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"manifest.json: {exc!r}") from exc


# --- reading, copying, checking ---------------------------------------------------------------

def _nocache(fd: int) -> None:
    """Keep these bytes out of the page cache, so a read-back reads the medium."""
    try:
        fcntl.fcntl(fd, _F_NOCACHE, 1)
    except OSError:
        pass


def _digest_at(dir_fd: int, name: str, size: int) -> str | None:
    """The SHA-256 of a regular file of `size` bytes at `name`; None otherwise."""
    try:
        fd = os.open(name, rfs.O_FILE, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        _nocache(fd)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size != size:
            return None
        digest, _ = rfs.read_hashes(fd, size, None)
        return digest
    except (OSError, rfs.TreeError):
        return None
    finally:
        os.close(fd)


def _lstat_at(dir_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _sync_matching(folder_fd: int, name: str, mode: int) -> None:
    """Give a retained regular file its promised mode and flush its contents."""
    fd = os.open(name, rfs.O_FILE, dir_fd=folder_fd)
    try:
        held = os.fstat(fd)
        if not stat.S_ISREG(held.st_mode):
            raise Failed(f"{name} is not a regular file")
        os.fchmod(fd, mode)
        rfs.fullsync(fd)
        named = _lstat_at(folder_fd, name)
        if named is None or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
            raise Failed(f"{name} changed while it was flushed")
    finally:
        os.close(fd)


def _remove_at(dir_fd: int, name: str) -> None:
    """Remove a wrong entry of our own external folder (a partial copy, a stray link)."""
    st = _lstat_at(dir_fd, name)
    if st is None:
        return
    if stat.S_ISDIR(st.st_mode):
        child = rfs.open_dir(name, dir_fd=dir_fd, expect=st)
        try:
            rfs._remove_children(child)
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=dir_fd)
    else:
        os.unlink(name, dir_fd=dir_fd)


def _identity(st: os.stat_result) -> list[int]:
    """An entry's identity on its volume: inode and birth time. Not `st_dev`:
    macOS gives a disk a new one when its disk number changes at a remount.
    Which volume is the one with the recorded UUID (`_external`)."""
    return [st.st_ino, getattr(st, "st_birthtime_ns", int(getattr(st, "st_birthtime", 0) * 10 ** 9))]


@dataclass
class Owner:
    """The external folder's identity and the inodes this offload created.

    An exclusive file creation is recorded durably before any bytes are
    written, so a torn copy is repairable without claiming unrelated files.
    """

    fd: int
    data: dict[str, Any]
    marker: list[int]

    def check(self, relative: str, st: os.stat_result) -> None:
        if self.data["entries"].get(relative) != _identity(st):
            raise Failed(f"{relative}: refusing an external entry this offload did not create")

    def save(self) -> None:
        marker = _lstat_at(self.fd, OWNER)
        if marker is None or _identity(marker) != self.marker:
            raise Failed("the external ownership marker changed")
        self.marker = _write_owner(self.fd, self.data, replace=True)

    def claim(self, relative: str, fd: int) -> None:
        self.data["entries"][relative] = _identity(os.fstat(fd))
        self.save()

    def preflight(self) -> None:
        """Refuse replaced entries before modifying any part of this copy."""
        files = _lstat_at(self.fd, "files")
        files_fd = None
        if files is not None:
            self.check("files", files)
            files_fd = rfs.open_dir("files", dir_fd=self.fd, expect=files)
        try:
            for relative in self.data["entries"]:
                if relative == "files":
                    continue
                if relative.startswith("files/"):
                    parent, leaf = files_fd, relative[len("files/"):]
                else:
                    parent, leaf = self.fd, relative
                if parent is not None:
                    found = _lstat_at(parent, leaf)
                    if found is not None:
                        self.check(relative, found)
        finally:
            if files_fd is not None:
                os.close(files_fd)


def _unlink_quietly(folder_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=folder_fd)
    except FileNotFoundError:
        pass


def _write_owner(folder_fd: int, data: dict[str, Any], *, replace: bool) -> list[int]:
    """Write the ledger through a synced temporary file, linked (the first
    write) or renamed over the ledger. An error at any point removes the
    temporary; one a crash leaves is swept by the next `_ownership(create=True)`."""
    temporary = f".owner.{os.getpid()}.{time.time_ns()}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=folder_fd)
    try:
        try:
            pending = memoryview(json.dumps(data, sort_keys=True).encode() + b"\n")
            while pending:
                pending = pending[os.write(fd, pending):]
            os.fchmod(fd, 0o600)
            rfs.fullsync(fd)
            identity = _identity(os.fstat(fd))
        finally:
            os.close(fd)
        if replace:
            os.replace(temporary, OWNER, src_dir_fd=folder_fd, dst_dir_fd=folder_fd)
        else:
            _step("ledger-first")
            os.link(temporary, OWNER, src_dir_fd=folder_fd, dst_dir_fd=folder_fd, follow_symlinks=False)
            os.unlink(temporary, dir_fd=folder_fd)
    except Exception:
        _unlink_quietly(folder_fd, temporary)
        raise
    rfs.fullsync(folder_fd)
    return identity


def _sweep_owner_temps(folder_fd: int) -> None:
    """Under the archive's lock: remove the ledger temporaries a crash left."""
    swept = False
    for name in os.listdir(folder_fd):
        if _OWNER_TEMP.match(name):
            found = _lstat_at(folder_fd, name)
            if found is not None and stat.S_ISREG(found.st_mode):
                _unlink_quietly(folder_fd, name)
                swept = True
    if swept:
        rfs.fullsync(folder_fd)


def _ownership(folder_fd: int, stub: dict[str, Any], *, create: bool) -> Owner:
    parts = stub["path"].split("/")
    source = parts[-2] if len(parts) >= 3 else ""
    if re.fullmatch(r"[0-9a-f]{64}", source) is None:
        raise Failed("the external path has no source identity; refusing an unowned target")
    expected = {"format": FORMAT, "source": source, "archive": stub["archive"],
                "manifest_sha256": stub["manifest_sha256"],
                "volume": str(stub["volume"].get("uuid") or "").upper(),
                "directory": _identity(os.fstat(folder_fd))}
    st = _lstat_at(folder_fd, OWNER)
    if st is None:
        # Only ledger temporaries (a crash in the first ledger write): unclaimed.
        if not create or [n for n in os.listdir(folder_fd) if not _OWNER_TEMP.match(n)]:
            raise Failed("the external folder is not owned by this archive; left as it is")
        _sweep_owner_temps(folder_fd)
        if os.listdir(folder_fd):
            raise Failed("the external folder is not owned by this archive; left as it is")
        data = {**expected, "entries": {}}
        marker = _write_owner(folder_fd, data, replace=False)
    else:
        fd = os.open(OWNER, rfs.O_FILE, dir_fd=folder_fd)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > 64 << 20:
                raise Failed("the external ownership marker is not a regular metadata file")
            chunks = []
            while chunk := os.read(fd, rfs.CHUNK):
                chunks.append(chunk)
            try:
                data = json.loads(b"".join(chunks))
            except ValueError as exc:
                raise Failed(f"the external ownership marker {OWNER} of {stub['archive']} is damaged "
                             f"({exc}); left as it is") from None
            marker = _identity(opened)
            if not isinstance(data, dict) or any(data.get(key) != value for key, value in expected.items()) \
                    or not isinstance(data.get("entries"), dict):
                raise Failed("the external folder belongs to another archive or source")
            for relative, identity in data["entries"].items():
                if not _valid_relative(relative) or len(relative.split("/")) > 2 \
                        or ("/" in relative and not relative.startswith("files/")) \
                        or not isinstance(identity, list) or len(identity) != 2 \
                        or any(not isinstance(value, int) for value in identity):
                    raise Failed("the external ownership ledger is malformed")
            if create:
                os.fchmod(fd, 0o600)
                rfs.fullsync(fd)
        finally:
            os.close(fd)
        if create:
            _sweep_owner_temps(folder_fd)
    owner = Owner(folder_fd, data, marker)
    owner.preflight()
    if create:
        os.fchmod(folder_fd, 0o700)
        rfs.fullsync(folder_fd)
    return owner


def _copy_into(src_dir_fd: int, name: str, dst_dir_fd: int, dst_name: str, mode: int,
               *, created: Callable[[int], None] | None = None, sha256: str | None = None) -> None:
    """Copy one regular file into a new path (created exclusively), with its
    mtime, `mode`, and a full sync. With `sha256`, the bytes read must hash to
    it: else the new copy is removed again and `Failed` names the file."""
    src = os.open(name, rfs.O_FILE, dir_fd=src_dir_fd)
    try:
        st = os.fstat(src)
        if not stat.S_ISREG(st.st_mode):
            raise Failed(f"{name} is not a regular file")
        dst = os.open(dst_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                      dir_fd=dst_dir_fd)
        try:
            # Darwin can lower birthtime when mtime is set earlier. Establish
            # the final timestamps before persisting the inode's identity.
            os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
            if created is not None:
                created(dst)
            _nocache(dst)
            digest = hashlib.sha256()
            offset = 0
            while True:
                chunk = os.pread(src, rfs.CHUNK, offset)
                if not chunk:
                    break
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(dst, view):]
                offset += len(chunk)
            if sha256 is not None and digest.hexdigest() != sha256:
                named, held = _lstat_at(dst_dir_fd, dst_name), os.fstat(dst)
                if named is not None and (named.st_dev, named.st_ino) == (held.st_dev, held.st_ino):
                    os.unlink(dst_name, dir_fd=dst_dir_fd)
                raise Failed(f"{name}: the internal copy does not match the SHA-256 recorded for it; "
                             "not copied, and nothing that matches was replaced")
            os.fchmod(dst, mode)
            os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
            rfs.fullsync(dst)
        finally:
            os.close(dst)
    finally:
        os.close(src)


def _place(src_dir_fd: int | None, name: str, dst_dir_fd: int, size: int, sha256: str, mode: int,
           *, owner: Owner, relative: str) -> None:
    """Make `name` in `dst_dir_fd` a copy of the source: an existing entry is
    kept if it already holds the bytes `sha256` names, else replaced from a
    source that must hash to `sha256` too (`_copy_into`)."""
    st = _lstat_at(dst_dir_fd, name)
    if st is not None:
        owner.check(relative, st)
        if stat.S_ISREG(st.st_mode) and st.st_size == size and _digest_at(dst_dir_fd, name, size) == sha256:
            fd = os.open(name, rfs.O_FILE, dir_fd=dst_dir_fd)
            try:
                owner.check(relative, os.fstat(fd))
                os.fchmod(fd, mode)
                rfs.fullsync(fd)
            finally:
                os.close(fd)
            return
    if src_dir_fd is None or _lstat_at(src_dir_fd, name) is None:
        raise Failed(f"{name}: the external copy does not hold it and no internal copy is left to copy")
    if st is not None:
        _remove_at(dst_dir_fd, name)
    _copy_into(src_dir_fd, name, dst_dir_fd, name, mode,
               created=lambda fd: owner.claim(relative, fd), sha256=sha256)


def _heads_ok(folder_fd: int, name: str, expected: dict[str, str]) -> bool:
    """Read the bundle header through the same confined, nofollow directory."""
    try:
        fd = os.open(name, rfs.O_FILE, dir_fd=folder_fd)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return False
            if stream.readline(64) not in (b"# v2 git bundle\n", b"# v3 git bundle\n"):
                return False
            heads: dict[str, str] = {}
            budget = 1 << 20
            while budget > 0:
                line = stream.readline(min(budget, 65536))
                budget -= len(line)
                if line == b"\n":
                    return all(heads.get(ref) == oid for ref, oid in expected.items())
                if not line or not line.endswith(b"\n"):
                    return False
                if line.startswith((b"-", b"@")):
                    continue
                oid, separator, ref = line.rstrip(b"\n").partition(b" ")
                if not separator:
                    return False
                heads[ref.decode("utf-8", "surrogateescape")] = oid.decode("ascii")
    except (OSError, UnicodeError):
        return False
    return False


def _open_below(top: Path, rel: str, *, create: bool = False, private: bool = False) -> int:
    """Open `top/rel` one component at a time, never following a link.

    With `create` or `private`, `rel` is a stub's path, `DIR/subfleet-archive/
    <source>/<name>` below the mount, and the program's own folders are its
    last three components, found by position: a user's folder above them is
    only opened, whatever its name. `create` makes each of the three that is
    missing (0700) and flushes its parent on every pass, made now or before,
    so a resume flushes what a crash left unflushed; `private` sets
    `subfleet-archive` and `<source>` to 0700 (`<name>` is `_ownership`'s)."""
    if not _valid_relative(rel):
        raise Failed(f"bad path {rel!r} under {top}")
    parts = rel.split("/")
    own = len(parts)
    if create or private:
        own = len(parts) - 3
        if own < 0 or parts[own] != FOLDER:
            raise Failed(f"bad path {rel!r} under {top}: not {FOLDER}/<source>/<name>")
    fd = rfs.open_dir(top)
    try:
        for index, part in enumerate(parts):
            if create and index >= own:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                rfs.fullsync(fd)
            child = rfs.open_dir(part, dir_fd=fd)
            os.close(fd)
            fd = child
            if private and own <= index < len(parts) - 1:
                os.fchmod(fd, 0o700)
                rfs.fullsync(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _external(stub: dict[str, Any]) -> tuple[rvol.Volume, Path]:
    """The mounted volume an offloaded archive's files are on, and their folder."""
    volume = stub["volume"]
    found = rvol.mounted_with(str(volume.get("uuid") or ""))
    if found is None:
        raise VolumeMissing(f"archive {stub['archive']} is offloaded to volume {volume.get('name')!r} "
                            f"({volume.get('uuid')}), which is not mounted; mount it and run again")
    return found, found.mount / stub["path"]


def contents(root: Path, name: str) -> Path:
    """The folder holding the archive's ``files/`` and bundle (the one resolver).

    The internal archive, unless its stub says removal may have begun: then
    the external folder on the mounted volume with the recorded UUID, or
    `VolumeMissing` naming the volume and its UUID."""
    os.close(_archive_fd(root, name))
    stub = read_stub(root, name)
    if stub is None or not external_authoritative(stub):
        return root.resolve() / "archive" / name
    found, folder = _external(stub)
    folder_fd = _open_below(found.mount, stub["path"])
    try:
        _ownership(folder_fd, stub, create=False)
    finally:
        os.close(folder_fd)
    return folder


def _verify(folder: Path, folder_fd: int, plan_: Plan, bundle_sha256: str | None) -> list[str]:
    """Read a copy back: every stored file against the manifest, the bundle
    against `bundle_sha256` and its heads against the manifest."""
    problems: list[str] = []
    try:
        files_fd = rfs.open_dir("files", dir_fd=folder_fd)
    except OSError as exc:
        if plan_.stores:
            return [f"files/: {exc}"]
        files_fd = None
    try:
        for store, (size, sha) in sorted(plan_.stores.items()):
            if _digest_at(files_fd, store, size) != sha:
                problems.append(f"files/{store}")
    finally:
        if files_fd is not None:
            os.close(files_fd)
    if plan_.bundle:
        if bundle_sha256 is None or _digest_at(folder_fd, plan_.bundle, plan_.bundle_bytes) != bundle_sha256:
            problems.append(plan_.bundle)
        elif not _heads_ok(folder_fd, plan_.bundle, plan_.bundle_heads):
            problems.append(f"{plan_.bundle} (heads)")
        elif _digest_at(folder_fd, plan_.bundle, plan_.bundle_bytes) != bundle_sha256:
            problems.append(f"{plan_.bundle} (changed during verification)")
    return problems


def _log(root: Path, record: dict[str, Any]) -> None:
    parent = _archive_fd(root)
    try:
        fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=parent)
        try:
            data = memoryview(json.dumps({"at": _now(), **record}, sort_keys=True).encode() + b"\n")
            while data:
                data = data[os.write(fd, data):]
            rfs.fullsync(fd)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _clean_stub_temps(base: Path) -> None:
    """A stub write a crash cut short leaves its temporary file; remove it."""
    try:
        folder_fd = _archive_fd(base.parent.parent, base.name)
    except FileNotFoundError:
        return
    try:
        for name in os.listdir(folder_fd):
            if _STUB_TEMP.match(name):
                try:
                    os.unlink(name, dir_fd=folder_fd)
                except FileNotFoundError:
                    pass
    finally:
        os.close(folder_fd)


def _modes(base: Path, plan_: Plan) -> dict[str, Any]:
    """The modes recall puts back: the files folder, stored files that are not
    0600, and the bundle."""
    out: dict[str, Any] = {"files": None, "stored": {}, "bundle": None}
    parent = _archive_fd(base.parent.parent, base.name)
    try:
        st = _lstat_at(parent, "files")
        if st is not None:
            fd = rfs.open_dir("files", dir_fd=parent)
            try:
                out["files"] = stat.S_IMODE(os.fstat(fd).st_mode)
                for store in plan_.stores:
                    found = _lstat_at(fd, store)
                    if found is not None and stat.S_IMODE(found.st_mode) != STORED_MODE:
                        out["stored"][store] = stat.S_IMODE(found.st_mode)
            finally:
                os.close(fd)
        if plan_.bundle:
            st = _lstat_at(parent, plan_.bundle)
            if st is not None:
                if not stat.S_ISREG(st.st_mode):
                    raise Failed(f"{plan_.bundle} is not a regular file")
                out["bundle"] = stat.S_IMODE(st.st_mode)
    finally:
        os.close(parent)
    return out


# --- the target -------------------------------------------------------------------------------

def _under(mount: Path, directory: Path) -> str:
    """`directory`'s path below its volume's mount point ("" for the mount itself)."""
    try:
        rel = directory.relative_to(mount)
        return "" if str(rel) == "." else str(rel)
    except ValueError:
        pass
    # A firmlinked path (/Users on the Data volume) is also below the mount.
    alternative = mount / str(directory).lstrip("/")
    if os.path.isdir(alternative) and os.path.samefile(alternative, directory):
        return str(directory).lstrip("/")
    raise ValueError(f"{directory} is not below its volume's mount point {mount}")


@dataclass
class Target:
    directory: Path            # DIR, resolved
    volume: rvol.Volume
    under: str                 # DIR's path below the volume's mount point
    source: str                # one filesystem identity for the source archive root

    def path_of(self, name: str) -> str:
        return "/".join(p for p in (self.under, FOLDER, self.source, name) if p)

    def describe(self) -> dict[str, Any]:
        return {"dir": str(self.directory), "under": self.under, **self.volume.describe()}


def _source(root: Path) -> str:
    """One name for the source archive root: its canonical path, inode and
    birth time (`_identity`; a remount's new `st_dev` keeps the name)."""
    archive = Path(os.path.realpath(root / "archive"))
    try:
        st = os.stat(archive, follow_symlinks=False)
        filesystem = "\0".join(str(value) for value in _identity(st)).encode()
    except FileNotFoundError:
        filesystem = b"absent"
    identity = os.fsencode(archive) + b"\0" + filesystem
    return hashlib.sha256(identity).hexdigest()


def target(root: Path, to: Path, *, allow_unencrypted: bool) -> tuple[Target | None, list[str]]:
    """The target and why it is refused (empty: it is not)."""
    directory = Path(os.path.realpath(to if to.is_absolute() else Path.cwd() / to))
    if not directory.is_dir():
        return None, [f"{to} is not an existing folder"]
    try:
        volume = rvol.volume_of(directory)
    except (OSError, ValueError) as exc:
        return None, [f"{to}: its volume could not be read ({exc})"]
    why = rvol.refusals(volume, allow_unencrypted=allow_unencrypted)
    try:
        here = rvol.volume_of(Path(os.path.realpath(root / "archive")))
        if volume.uuid and here.uuid and here.uuid.upper() == volume.uuid.upper():
            why.append(f"{to} is on the state root's own volume {volume.name!r}; offloading there frees nothing")
    except (OSError, ValueError) as exc:
        why.append(f"the state root's volume could not be read ({exc})")
    try:
        under = _under(volume.mount, directory)
    except ValueError as exc:
        return Target(directory, volume, "", _source(root)), [*why, str(exc)]
    return Target(directory, volume, under, _source(root)), why


# --- offload ----------------------------------------------------------------------------------

def _offload_skip(root: Path, name: str, where: Target | None, keep_internal: bool) -> tuple[str | None, dict | None]:
    """Why offload leaves this archive alone now (None: it goes), and its stub."""
    folder = retention_folder(root, name)
    if folder is not None:
        return f"its job has a retention folder ({folder}): not settled", None
    try:
        stub = read_stub(root, name)
    except (OSError, ValueError) as exc:
        return f"{STUB} unreadable: {exc}", None
    if stub is None:
        return None, None
    volume = stub["volume"]
    there = f"{volume.get('name')!r} ({volume.get('uuid')})"
    if stub.get("recalled"):
        return f"a recall from {there} is unfinished; run `subfleet retention recall {name}`", stub
    if stub["state"] == "offloaded":
        return f"already offloaded to {there}", stub
    same = where is not None and (volume.get("uuid") or "").upper() == (where.volume.uuid or "").upper() \
        and stub["path"] == where.path_of(name)
    if not same:
        return f"an offload to {there} at {stub['path']} is {stub['state']}; run it with that target", stub
    if keep_internal and stub["state"] == "verified":
        if stub.get("removing"):
            return "its internal copy is being removed; run without --keep-internal to finish", stub
        return f"already copied to {there} and verified (kept internal)", stub
    return None, stub


def sweep_stale_locks(root: Path) -> None:
    """Remove lock files a killed run left (the kernel released their locks)."""
    base = root / "archive"
    try:
        names = [n for n in os.listdir(base) if n.startswith(".lock.")]
    except FileNotFoundError:
        return
    for name in names:
        archive = name[len(".lock."):]
        if not valid_name(archive):
            continue
        try:
            with ArchiveLock(root, archive):
                pass
        except (Busy, OSError):
            continue


def offload(root: Path, to: Path, *, limit: int | None = None, min_bytes: int = 0, keep_internal: bool = False,
            dry_run: bool = False, allow_unencrypted: bool = False) -> dict[str, Any]:
    """Move settled archives' stored files and bundles to `to`, largest first."""
    report: dict[str, Any] = {"dry_run": dry_run, "keep_internal": keep_internal, "target": None, "refused": [],
                              "batch": [], "batch_bytes": 0, "skipped": [], "results": [], "not_examined": 0}
    where, refused = target(root, to, allow_unencrypted=allow_unencrypted)
    report["target"] = where.describe() if where is not None else {"dir": str(to)}
    if where is None:
        report["refused"] = refused
        return report
    base = root / "archive"
    try:
        names = sorted(n for n in os.listdir(base) if not n.startswith("."))
    except FileNotFoundError:
        names = []
    sized: list[tuple[int, str, dict | None]] = []
    for name in names:
        if not valid_name(name) or not os.path.isdir(base / name) or os.path.islink(base / name):
            continue
        reason, stub = _offload_skip(root, name, where, keep_internal)
        if reason is None:
            try:
                summary = read_summary(root, name)
                size = int(summary.get("archived_bytes") or 0) + int(summary.get("bundle_bytes") or 0)
            except (OSError, ValueError, TypeError) as exc:
                reason = f"summary.json unreadable: {exc}"
        if reason is not None:
            report["skipped"].append({"archive": name, "reason": reason})
            continue
        sized.append((size, name, stub))
    sized.sort(key=lambda item: (-item[0], item[1]))
    batch: list[tuple[Plan, dict | None]] = []
    for size, name, stub in sized:
        if limit is not None and len(batch) >= limit:
            report["not_examined"] += 1
            continue
        if size < min_bytes:
            report["skipped"].append({"archive": name, "reason": f"{size} bytes, under --min-bytes {min_bytes}"})
            continue
        try:
            found = plan(root, name)
        except (OSError, ValueError) as exc:
            report["skipped"].append({"archive": name, "reason": f"manifest.json unreadable: {exc}"})
            continue
        if not found.stores and not found.bundle:
            report["skipped"].append({"archive": name, "reason": "no stored files and no bundle: nothing to move"})
            continue
        batch.append((found, stub))
        report["batch"].append({"archive": name, "files": len(found.stores), "bytes": found.total,
                                "resume": stub["state"] if stub else None})
    report["batch_bytes"] = sum(p.total for p, _ in batch)
    need = report["batch_bytes"] + int(report["batch_bytes"] * HEADROOM)
    if batch and where.volume.free < need:
        refused.append(f"volume {where.volume.name!r} has {where.volume.free} bytes free; the batch needs "
                       f"{report['batch_bytes']} plus 10% ({need})")
    report["refused"] = refused
    if refused or dry_run:
        return report
    sweep_stale_locks(root)
    for found, _ in batch:
        report["results"].append(_offload_locked(root, found, where, keep_internal))
    return report


def _offload_locked(root: Path, found: Plan, where: Target, keep_internal: bool) -> dict[str, Any]:
    name = found.name
    result: dict[str, Any] = {"archive": name, "files": len(found.stores), "bytes": found.total}
    try:
        with ArchiveLock(root, name):
            try:
                # Planning precedes the lock; the archive may have changed meanwhile.
                fresh = plan(root, name)
                if fresh.manifest_sha256 != found.manifest_sha256:
                    raise Failed("manifest.json changed after planning")
                found = fresh
                reason, stub = _offload_skip(root, name, where, keep_internal)
                if reason is not None:
                    return {**result, "outcome": "skipped", "reason": reason}
                result["outcome"], result["left_internal"] = _offload_one(root, found, where, keep_internal, stub)
            except (Failed, OSError, ValueError, rgit.GitError, rfs.TreeError) as exc:
                result.update(outcome="failed", error=str(exc))
            _log(root, {"op": "offload", **result, "volume": where.volume.uuid, "path": where.path_of(name)})
    except Busy as exc:
        return {**result, "outcome": "skipped", "reason": str(exc)}
    except (OSError, ValueError, rfs.TreeError) as exc:
        return {**result, "outcome": "failed", "error": str(exc)}
    return result


def _offload_one(root: Path, found: Plan, where: Target, keep_internal: bool,
                 stub: dict[str, Any] | None) -> tuple[str, list[str]]:
    name, base = found.name, found.base
    _clean_stub_temps(base)
    if stub is None:
        # The bundle's SHA-256 is read once, before any byte is copied; every
        # later step, resume and recall compares against this value.
        stub = {"format": FORMAT, "archive": name, "job_id": found.job_id, "state": "copying",
                "volume": {"uuid": where.volume.uuid, "name": where.volume.name, "fstype": where.volume.fstype,
                           "encrypted": where.volume.encrypted},
                "path": where.path_of(name), "manifest_sha256": found.manifest_sha256,
                "files": len(found.stores), "bytes": found.files_bytes, "bundle": found.bundle,
                "bundle_bytes": found.bundle_bytes, "bundle_sha256": _bundle_sha(root, found),
                "modes": _modes(base, found), "keep_internal": keep_internal, "removing": False,
                "started_at": _now(), "subfleet_version": __version__}
        _save(root, name, stub)
        _step("stub-copying")
    elif stub.get("manifest_sha256") != found.manifest_sha256:
        raise Failed(f"{STUB} names another manifest than the archive holds")
    if found.bundle and not stub.get("bundle_sha256"):
        raise Failed(f"{found.bundle}: {STUB} records no checksum; nothing copied or removed")
    folder = where.volume.mount / stub["path"]
    if stub["state"] == "copying":
        folder_fd = _open_below(where.volume.mount, stub["path"], create=True, private=True)
        try:
            owner = _ownership(folder_fd, stub, create=True)
            _copy_all(base, folder_fd, found, stub.get("bundle_sha256"), owner=owner)
            _step("copied")
            problems = _verify(folder, folder_fd, found, stub.get("bundle_sha256"))
        finally:
            os.close(folder_fd)
        if problems:
            raise Failed("the copy did not read back: " + ", ".join(problems[:5])
                         + (f" and {len(problems) - 5} more" if len(problems) > 5 else ""))
        _save(root, name, stub, state="verified", verified_at=_now(), keep_internal=keep_internal)
        _step("verified")
    else:
        # `verified`: read the external copy back again before anything
        # internal goes. A copy that no longer reads back is repaired from
        # the internal files still whole, and read back once more.
        folder_fd = _open_below(where.volume.mount, stub["path"], private=True)
        try:
            owner = _ownership(folder_fd, stub, create=True)
            problems = _verify(folder, folder_fd, found, stub.get("bundle_sha256"))
            if problems:
                internal_fd = _archive_fd(root, name)
                try:
                    internal_problems = _verify(base, internal_fd, found, stub.get("bundle_sha256"))
                finally:
                    os.close(internal_fd)
                if internal_problems:
                    raise Failed("the external copy no longer reads back and no internal copy is left "
                                 "that verifies completely: " + ", ".join(internal_problems[:5]))
                # Persist the verified internal authority before changing the selected copy.
                _save(root, name, stub, state="copying", removing=False)
                _step("repairing")
                _copy_all(base, folder_fd, found, stub.get("bundle_sha256"), owner=owner)
                problems = _verify(folder, folder_fd, found, stub.get("bundle_sha256"))
                if problems:
                    raise Failed("the external copy no longer reads back: " + ", ".join(problems[:5]))
                _save(root, name, stub, state="verified", removing=False, verified_at=_now())
                _step("repaired")
            else:
                # Matching resumed entries need the same modes and durability as new copies.
                _copy_all(base, folder_fd, found, stub.get("bundle_sha256"), owner=owner)
                problems = _verify(folder, folder_fd, found, stub.get("bundle_sha256"))
                if problems:
                    raise Failed("the external copy changed: " + ", ".join(problems[:5]))
                _step("reverified")
        finally:
            os.close(folder_fd)
    if keep_internal:
        return "verified", []
    if not stub.get("removing"):
        _save(root, name, stub, removing=True, keep_internal=False)
        _step("removing")
    left = _remove_internal(base, found, stub, where.volume.mount)
    _step("removed")
    _save(root, name, stub, state="offloaded", offloaded_at=_now(), left_internal=left)
    _step("offloaded")
    return "offloaded", left


def _bundle_sha(root: Path, found: Plan) -> str | None:
    """The internal bundle's SHA-256 (None without one), read before offload
    copies anything; `Failed` if it is not the bundle the manifest records."""
    if not found.bundle:
        return None
    folder_fd = _archive_fd(root, found.name)
    try:
        digest = _digest_at(folder_fd, found.bundle, found.bundle_bytes)
        if digest is None:
            raise Failed(f"{found.bundle} is missing or not the {found.bundle_bytes} bytes the manifest records")
        if not _heads_ok(folder_fd, found.bundle, found.bundle_heads) \
                or _digest_at(folder_fd, found.bundle, found.bundle_bytes) != digest:
            raise Failed(f"{found.bundle} does not hold the heads the manifest records")
    finally:
        os.close(folder_fd)
    return digest


def _copy_all(base: Path, folder_fd: int, found: Plan, bundle_sha: str | None, *, owner: Owner) -> None:
    """Make the external folder hold the stored files and the bundle, copying
    what it lacks from the internal archive: stored files against the
    manifest's SHA-256, the bundle against the one the stub recorded."""
    owner.preflight()
    made_files = False
    try:
        os.mkdir("files", 0o700, dir_fd=folder_fd)
        made_files = True
        rfs.fullsync(folder_fd)
    except FileExistsError:
        pass
    files_fd = rfs.open_dir("files", dir_fd=folder_fd)
    try:
        if made_files:
            owner.claim("files", files_fd)
        else:
            owner.check("files", os.fstat(files_fd))
        os.fchmod(files_fd, 0o700)
        if found.stores:
            archive_fd = _archive_fd(base.parent.parent, base.name)
            try:
                try:
                    source: int | None = rfs.open_dir("files", dir_fd=archive_fd)
                except FileNotFoundError:
                    source = None          # a resumed removal took the last of them
            finally:
                os.close(archive_fd)
            try:
                for store, (size, sha) in sorted(found.stores.items()):
                    _place(source, store, files_fd, size, sha, STORED_MODE,
                           owner=owner, relative=f"files/{store}")
                    _step("copy-file")
            finally:
                if source is not None:
                    os.close(source)
        rfs.fullsync(files_fd)
    finally:
        os.close(files_fd)
    if found.bundle:
        if bundle_sha is None:
            raise Failed(f"{found.bundle}: {STUB} records no checksum; nothing copied")
        source = _archive_fd(base.parent.parent, base.name)
        try:
            _place(source, found.bundle, folder_fd, found.bundle_bytes, bundle_sha, STORED_MODE,
                   owner=owner, relative=found.bundle)
            _step("copy-bundle")
        finally:
            os.close(source)
    rfs.fullsync(folder_fd)


def _remove_internal(base: Path, found: Plan, stub: dict[str, Any], mount: Path) -> list[str]:
    """Remove the internal stored files and bundle that still match; the
    names left (changed since, or not the archive's)."""
    left: list[str] = []
    folder_fd = _open_below(mount, stub["path"])
    base_fd = None
    external_files = None
    try:
        base_fd = _archive_fd(base.parent.parent, base.name)
        owner = _ownership(folder_fd, stub, create=False)
        problems = _verify(mount / stub["path"], folder_fd, found, stub.get("bundle_sha256"))
        if problems:
            raise Failed("the external copy changed before internal removal: " + ", ".join(problems[:5]))
        external_files = rfs.open_dir("files", dir_fd=folder_fd)
        try:
            files_fd = rfs.open_dir("files", dir_fd=base_fd)
        except FileNotFoundError:
            files_fd = None
        if files_fd is not None:
            try:
                for store, (size, sha) in sorted(found.stores.items()):
                    st = _lstat_at(files_fd, store)
                    if st is None:
                        continue
                    there = _lstat_at(external_files, store)
                    if there is None or not stat.S_ISREG(there.st_mode) \
                            or _digest_at(external_files, store, size) != sha:
                        raise Failed(f"the external copy of {store} went while the internal one was removed")
                    owner.check(f"files/{store}", there)
                    if stat.S_ISREG(st.st_mode) and st.st_size == size and _digest_at(files_fd, store, size) == sha:
                        os.unlink(store, dir_fd=files_fd)
                        _step("remove-file")
                    else:
                        left.append(f"files/{store}")
                rfs.fullsync(files_fd)
                remaining = os.listdir(files_fd)
            finally:
                os.close(files_fd)
            if remaining:
                left.extend(f"files/{n}" for n in remaining if f"files/{n}" not in left)
            else:
                os.rmdir("files", dir_fd=base_fd)
        os.close(external_files)
        external_files = None
        if found.bundle:
            internal = os.dup(base_fd)
            try:
                st = _lstat_at(internal, found.bundle)
                if st is not None:
                    there = _lstat_at(folder_fd, found.bundle)
                    if there is None or not stat.S_ISREG(there.st_mode) \
                            or _digest_at(folder_fd, found.bundle, found.bundle_bytes) != stub.get("bundle_sha256"):
                        raise Failed(f"the external {found.bundle} went while the internal one was removed")
                    owner.check(found.bundle, there)
                    if _digest_at(internal, found.bundle, found.bundle_bytes) == stub.get("bundle_sha256"):
                        os.unlink(found.bundle, dir_fd=internal)
                        _step("remove-bundle")
                    else:
                        left.append(found.bundle)
            finally:
                os.close(internal)
    finally:
        if external_files is not None:
            os.close(external_files)
        os.close(folder_fd)
        if base_fd is not None:
            try:
                rfs.fullsync(base_fd)
            finally:
                os.close(base_fd)
    return sorted(set(left))


# --- recall -----------------------------------------------------------------------------------

def recall(root: Path, name: str | None = None, *, all_on: Path | None = None,
           drop_external: bool = False) -> dict[str, Any]:
    """Bring offloaded archives' files back: `name`, or every one offloaded to `all_on`."""
    report: dict[str, Any] = {"results": [], "skipped": []}
    if all_on is not None:
        directory = Path(os.path.realpath(all_on if all_on.is_absolute() else Path.cwd() / all_on))
        if not directory.is_dir():
            raise Failed(f"{all_on} is not an existing folder")
        volume = rvol.volume_of(directory)
        where = Target(directory, volume, _under(volume.mount, directory), _source(root))
        names = []
        try:
            listed = sorted(n for n in os.listdir(root / "archive") if valid_name(n))
        except FileNotFoundError:
            listed = []
        for candidate in listed:
            try:
                stub = read_stub(root, candidate)
            except (OSError, ValueError) as exc:
                report["skipped"].append({"archive": candidate, "reason": f"{STUB} unreadable: {exc}"})
                continue
            if stub is not None and (stub["volume"].get("uuid") or "").upper() == (volume.uuid or "").upper() \
                    and stub["path"] == where.path_of(candidate):
                names.append(candidate)
    else:
        if name is None or not valid_name(name):
            raise Failed(f"invalid archive name {name!r}")
        names = [name]
    sweep_stale_locks(root)
    for each in names:
        report["results"].append(_recall_locked(root, each, drop_external))
    return report


def _recall_locked(root: Path, name: str, drop_external: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"archive": name}
    try:
        with ArchiveLock(root, name):
            unsettled = retention_folder(root, name)
            if unsettled is not None:
                return {**result, "outcome": "skipped",
                        "reason": f"its job has a retention folder ({unsettled}): not settled"}
            try:
                stub = read_stub(root, name)
            except (OSError, ValueError) as exc:
                return {**result, "outcome": "failed", "error": f"{STUB} unreadable: {exc}"}
            if stub is None:
                return {**result, "outcome": "skipped", "reason": "not offloaded"}
            try:
                result.update(_recall_one(root, name, stub, drop_external))
                result["outcome"] = "recalled"
            except (Failed, OSError, ValueError, rgit.GitError, rfs.TreeError, VolumeMissing) as exc:
                result.update(outcome="failed", error=str(exc))
            _log(root, {"op": "recall", **result, "volume": stub["volume"].get("uuid"), "path": stub["path"]})
    except Busy as exc:
        return {**result, "outcome": "skipped", "reason": str(exc)}
    except (OSError, ValueError, rfs.TreeError) as exc:
        return {**result, "outcome": "failed", "error": str(exc)}
    return result


def _recall_copy(parent: int, name: str, size: int, sha: str, mode: int,
                 source: Callable[[], int], heads: dict[str, str] | None = None) -> None:
    """Promote a verified interrupted copy, or copy from a verified source.

    A temporary file can be the only intact copy after an interruption. Keep
    it until either it verifies or another copy has been verified elsewhere.
    """
    def matches(fd: int, leaf: str) -> bool:
        if _digest_at(fd, leaf, size) != sha:
            return False
        return heads is None or (_heads_ok(fd, leaf, heads) and _digest_at(fd, leaf, size) == sha)

    temporary = _RECALL + name
    if not matches(parent, temporary):
        src = source()
        try:
            if not matches(src, name):
                raise Failed(f"{name} did not read back from the external copy; temporary copy kept")
            _remove_at(parent, temporary)
            _copy_into(src, name, parent, temporary, mode)
        finally:
            os.close(src)
        if not matches(parent, temporary):
            raise Failed(f"{name} did not read back from the external copy; temporary copy kept")
    _sync_matching(parent, temporary, mode)
    os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    rfs.fullsync(parent)


def _recall_one(root: Path, name: str, stub: dict[str, Any], drop_external: bool) -> dict[str, Any]:
    found = plan(root, name)
    if stub.get("manifest_sha256") != found.manifest_sha256:
        raise Failed(f"{STUB} names another manifest than the archive holds")
    base = found.base
    _clean_stub_temps(base)
    modes = stub.get("modes") or {}
    stored_modes = modes.get("stored") or {}
    out: dict[str, Any] = {"files": len(found.stores), "bytes": found.total, "copied_back": 0}
    external: dict[str, Any] = {}

    def folder() -> tuple[Path, int]:
        if "fd" not in external:
            volume, path = _external(stub)
            fd = _open_below(volume.mount, stub["path"])
            try:
                _ownership(fd, stub, create=False)
            except BaseException:
                os.close(fd)
                raise
            external["path"], external["fd"] = path, fd
        return external["path"], external["fd"]

    base_fd = _archive_fd(root, name)
    try:
        if external_authoritative(stub):
            folder()       # its volume and ledger answer before anything internal changes
        if found.stores or modes.get("files") is not None:
            try:
                os.mkdir("files", 0o700, dir_fd=base_fd)
            except FileExistsError:
                pass
            files_fd = rfs.open_dir("files", dir_fd=base_fd)
            try:
                for store, (size, sha) in sorted(found.stores.items()):
                    st = _lstat_at(files_fd, store)
                    if st is not None:
                        if stat.S_ISREG(st.st_mode) and st.st_size == size \
                                and _digest_at(files_fd, store, size) == sha:
                            _sync_matching(files_fd, store, stored_modes.get(store, STORED_MODE))
                            continue
                        raise Failed(f"files/{store} is there and is not the archived file; left as it is")
                    _recall_copy(files_fd, store, size, sha, stored_modes.get(store, STORED_MODE),
                                 lambda: rfs.open_dir("files", dir_fd=folder()[1]))
                    out["copied_back"] += 1
                    _step("recall-file")
                if modes.get("files") is not None:
                    os.fchmod(files_fd, modes["files"])
                rfs.fullsync(files_fd)
            finally:
                os.close(files_fd)
        if found.bundle:
            internal = os.dup(base_fd)
            try:
                st = _lstat_at(internal, found.bundle)
                expected = stub.get("bundle_sha256")
                if expected is None:
                    raise Failed(f"{found.bundle}: {STUB} records no checksum; external copy kept")
                if st is not None:
                    if _digest_at(internal, found.bundle, found.bundle_bytes) != expected \
                            or not _heads_ok(internal, found.bundle, found.bundle_heads):
                        raise Failed(f"{found.bundle} is there and is not the archived bundle; left as it is")
                    _sync_matching(internal, found.bundle,
                                   modes["bundle"] if modes.get("bundle") is not None else 0o644)
                else:
                    _recall_copy(internal, found.bundle, found.bundle_bytes, expected,
                                 modes["bundle"] if modes.get("bundle") is not None else 0o644,
                                 lambda: os.dup(folder()[1]), found.bundle_heads)
                    out["copied_back"] += 1
                    _step("recall-bundle")
            finally:
                os.close(internal)
        problems = _verify(base, base_fd, found, stub.get("bundle_sha256"))
        if problems:
            raise Failed("the recalled copy did not read back: " + ", ".join(problems[:5]))
        rfs.fullsync(base_fd)
        if not stub.get("recalled"):
            # Every file is back and read back: the internal copy is the one
            # to read again, before the external one may go.
            _save(root, name, stub, recalled=True, recalled_at=_now())
        _step("recalled")
        out["external"] = _drop_external(found, stub, folder) if drop_external else "kept"
        os.unlink(STUB, dir_fd=base_fd)
        rfs.fullsync(base_fd)
        _step("stub-dropped")
    finally:
        os.close(base_fd)
        if "fd" in external:
            os.close(external["fd"])
    return out


def _drop_external(found: Plan, stub: dict[str, Any], folder: Callable[[], tuple[Path, int]]) -> str:
    """Remove the external copy's own entries (the internal copy is verified by now)."""
    if stub["path"].split("/")[-2] != _source(found.base.parent.parent):
        return "kept: the external copy belongs to another source"
    files_fd = None
    try:
        path, folder_fd = folder()
        owner = _ownership(folder_fd, stub, create=False)
        try:
            files_fd = rfs.open_dir("files", dir_fd=folder_fd)
        except FileNotFoundError:
            pass
        # Check every planned path, including paths absent from the ledger,
        # before deleting anything. Unknown entries are always kept.
        if files_fd is not None:
            for store in sorted(found.stores):
                st = _lstat_at(files_fd, store)
                if st is not None:
                    owner.check(f"files/{store}", st)
        if found.bundle:
            st = _lstat_at(folder_fd, found.bundle)
            if st is not None:
                owner.check(found.bundle, st)
    except (VolumeMissing, OSError, Failed) as exc:
        if files_fd is not None:
            os.close(files_fd)
        return f"kept: {exc}"
    if files_fd is not None:
        try:
            for store in sorted(found.stores):
                try:
                    st = _lstat_at(files_fd, store)
                    if st is not None:
                        owner.check(f"files/{store}", st)
                    os.unlink(store, dir_fd=files_fd)
                    _step("drop-file")
                except FileNotFoundError:
                    pass
            rfs.fullsync(files_fd)
        finally:
            os.close(files_fd)
        try:
            os.rmdir("files", dir_fd=folder_fd)
        except OSError:
            return f"kept {path}/files: it holds files that are not the archive's"
    if found.bundle:
        try:
            st = _lstat_at(folder_fd, found.bundle)
            if st is not None:
                owner.check(found.bundle, st)
            os.unlink(found.bundle, dir_fd=folder_fd)
        except FileNotFoundError:
            pass
    rfs.fullsync(folder_fd)
    if set(os.listdir(folder_fd)) - {OWNER}:
        return f"kept {path}: it holds entries that are not the archive's"
    marker = _lstat_at(folder_fd, OWNER)
    if marker is None or _identity(marker) != owner.marker:
        return f"kept {path}: its ownership marker changed"
    os.unlink(OWNER, dir_fd=folder_fd)
    rfs.fullsync(folder_fd)
    try:
        volume, _ = _external(stub)
        parent_fd = _open_below(volume.mount, str(Path(stub["path"]).parent))
        try:
            named = _lstat_at(parent_fd, path.name)
            if named is None or _identity(named) != owner.data["directory"]:
                return f"kept {path}: its directory changed"
            os.rmdir(path.name, dir_fd=parent_fd)
            rfs.fullsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError:
        return f"kept {path}: it holds entries that are not the archive's"
    return "dropped"


def describe(stub: dict[str, Any]) -> str:
    """How `subfleet retention archives` shows an archive's offload."""
    volume = stub["volume"]
    where = f"{volume.get('name')} ({volume.get('uuid')})"
    if stub.get("recalled"):
        return f"recall from {where} unfinished; files back internal"
    if stub["state"] == "offloaded":
        return f"offloaded to {where}"
    if external_authoritative(stub):
        return f"offloaded to {where}; internal removal unfinished"
    if stub.get("keep_internal") and stub["state"] == "verified":
        return f"copied to {where}; kept internal"
    return f"offload to {where} {stub['state']}"
