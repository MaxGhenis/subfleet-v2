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
   the original modes, time, Subfleet version);
2. ``files/`` and the bundle are copied to ``DIR/subfleet-archive/<name>/``:
   each path created exclusively, directories 0700, files 0600, each file and
   directory `F_FULLFSYNC`ed;
3. the copy is read back: each stored file's SHA-256 against the manifest,
   the bundle's bytes against the source and its heads against the manifest;
   a mismatch stops the archive with the internal copy untouched;
4. the stub reads ``verified``; unless ``--keep-internal`` it is marked
   ``removing``, and only internal entries whose size and SHA-256 still match
   are removed; then it reads ``offloaded``;
5. one line per archive goes to ``<state>/archive/.offload.jsonl``.

Every step is safe to run again after a crash: ``copying`` checks or replaces
a partial copy, ``verified`` reads the external copy back again before it
removes anything, and a half-removed internal copy continues once the
external copy verifies again. `contents` is the one resolver that says where
an archive's ``files/`` and bundle are: the internal folder, or the external
folder once removal may have begun (a mounted volume with the recorded UUID).

Nothing here touches an archive whose job still has ``<state>/retention/<job>/``
(the name minus any ``.N`` suffix), and nothing writes outside
``<state>/archive/<name>/``, the lock and log files in ``<state>/archive/``, and
``DIR/subfleet-archive/<name>/``. All of it runs offline, without the daemon.
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


class Failed(Exception):
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


def read_stub(root: Path, name: str) -> dict[str, Any] | None:
    """The archive's offload stub, None without one; ValueError if it is not ours."""
    try:
        data = json.loads(rfs.read_regular(stub_path(root, name), limit=64 << 20))
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
    rfs.write_atomic(stub_path(root, name), json.dumps(stub, sort_keys=True, indent=1).encode() + b"\n")


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

    def __enter__(self) -> ArchiveLock:
        for _ in range(16):
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise Busy(f"archive {self.name} is busy: an offload, recall or restore holds it") from None
            try:
                named = os.stat(self.path, follow_symlinks=False)
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
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        finally:
            os.close(self.fd)
            self.fd = None


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
    summary = json.loads(rfs.read_regular(root / "archive" / name / "summary.json", limit=1 << 20))
    if not isinstance(summary, dict):
        raise ValueError("summary.json is not an object")
    return summary


def plan(root: Path, name: str) -> Plan:
    """What the archive's manifest says it stores; ValueError if it won't parse."""
    base = root / "archive" / name
    summary = read_summary(root, name)
    raw = rfs.read_regular(base / "manifest.json", limit=4 << 30)
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


def _copy_into(src_dir_fd: int, name: str, dst_dir_fd: int, dst_name: str, mode: int) -> None:
    """Copy one regular file into a new path (created exclusively), with its
    mtime, `mode`, and a full sync."""
    src = os.open(name, rfs.O_FILE, dir_fd=src_dir_fd)
    try:
        st = os.fstat(src)
        if not stat.S_ISREG(st.st_mode):
            raise Failed(f"{name} is not a regular file")
        dst = os.open(dst_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                      dir_fd=dst_dir_fd)
        try:
            _nocache(dst)
            offset = 0
            while True:
                chunk = os.pread(src, rfs.CHUNK, offset)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    view = view[os.write(dst, view):]
                offset += len(chunk)
            os.fchmod(dst, mode)
            os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
            rfs.fullsync(dst)
        finally:
            os.close(dst)
    finally:
        os.close(src)


def _place(src_dir_fd: int | None, name: str, dst_dir_fd: int, size: int, sha256: str, mode: int) -> None:
    """Make `name` in `dst_dir_fd` a copy of the source: an existing entry is
    kept only if it already holds these bytes, else replaced (which needs the
    source; the read-back decides whether the source was right)."""
    st = _lstat_at(dst_dir_fd, name)
    if st is not None:
        if stat.S_ISREG(st.st_mode) and st.st_size == size and _digest_at(dst_dir_fd, name, size) == sha256:
            return
    if src_dir_fd is None or _lstat_at(src_dir_fd, name) is None:
        raise Failed(f"{name}: the external copy does not hold it and no internal copy is left to copy")
    if st is not None:
        _remove_at(dst_dir_fd, name)
    _copy_into(src_dir_fd, name, dst_dir_fd, name, mode)


def _heads_ok(bundle: Path, expected: dict[str, str]) -> bool:
    try:
        heads = rgit.bundle_heads(bundle)
    except (rgit.GitError, OSError):
        return False
    return all(heads.get(ref) == oid for ref, oid in expected.items())


def _open_below(top: Path, rel: str, *, create: bool = False) -> int:
    """Open `top/rel` one component at a time, never following a link; with
    `create`, make each missing directory (0700) and sync its parent."""
    if not _valid_relative(rel):
        raise Failed(f"bad path {rel!r} under {top}")
    fd = rfs.open_dir(top)
    try:
        for part in rel.split("/"):
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    rfs.fullsync(fd)
                except FileExistsError:
                    pass
            child = rfs.open_dir(part, dir_fd=fd)
            os.close(fd)
            fd = child
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
    stub = read_stub(root, name)
    if stub is None or not external_authoritative(stub):
        return root / "archive" / name
    found, folder = _external(stub)
    os.close(_open_below(found.mount, stub["path"]))       # no link anywhere below the mount
    return folder


def _verify(folder: Path, folder_fd: int, plan_: Plan, bundle_sha256: str | None) -> list[str]:
    """Read a copy back: every stored file against the manifest, the bundle
    against `bundle_sha256` and its heads against the manifest."""
    problems: list[str] = []
    try:
        files_fd = rfs.open_dir("files", dir_fd=folder_fd)
    except OSError as exc:
        return [f"files/: {exc}"]
    try:
        for store, (size, sha) in sorted(plan_.stores.items()):
            if _digest_at(files_fd, store, size) != sha:
                problems.append(f"files/{store}")
    finally:
        os.close(files_fd)
    if plan_.bundle:
        if bundle_sha256 is None or _digest_at(folder_fd, plan_.bundle, plan_.bundle_bytes) != bundle_sha256:
            problems.append(plan_.bundle)
        elif not _heads_ok(folder / plan_.bundle, plan_.bundle_heads):
            problems.append(f"{plan_.bundle} (heads)")
    return problems


def _log(root: Path, record: dict[str, Any]) -> None:
    fd = os.open(root / "archive" / LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600)
    try:
        data = memoryview(json.dumps({"at": _now(), **record}, sort_keys=True).encode() + b"\n")
        while data:
            data = data[os.write(fd, data):]
        rfs.fullsync(fd)
    finally:
        os.close(fd)


def _clean_stub_temps(base: Path) -> None:
    """A stub write a crash cut short leaves its temporary file; remove it."""
    try:
        names = os.listdir(base)
    except FileNotFoundError:
        return
    for name in names:
        if _STUB_TEMP.match(name):
            try:
                os.unlink(base / name)
            except FileNotFoundError:
                pass


def _modes(base: Path, plan_: Plan) -> dict[str, Any]:
    """The modes recall puts back: the files folder, stored files that are not
    0600, and the bundle."""
    out: dict[str, Any] = {"files": None, "stored": {}, "bundle": None}
    try:
        st = os.lstat(base / "files")
    except FileNotFoundError:
        st = None
    if st is not None and stat.S_ISDIR(st.st_mode):
        out["files"] = stat.S_IMODE(st.st_mode)
        fd = rfs.open_dir(base / "files")
        try:
            for store in plan_.stores:
                found = _lstat_at(fd, store)
                if found is not None and stat.S_IMODE(found.st_mode) != STORED_MODE:
                    out["stored"][store] = stat.S_IMODE(found.st_mode)
        finally:
            os.close(fd)
    if plan_.bundle:
        try:
            out["bundle"] = stat.S_IMODE(os.lstat(base / plan_.bundle).st_mode)
        except FileNotFoundError:
            pass
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

    def path_of(self, name: str) -> str:
        return "/".join(p for p in (self.under, FOLDER, name) if p)

    def describe(self) -> dict[str, Any]:
        return {"dir": str(self.directory), "under": self.under, **self.volume.describe()}


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
        return Target(directory, volume, ""), [*why, str(exc)]
    return Target(directory, volume, under), why


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
            reason, stub = _offload_skip(root, name, where, keep_internal)      # again, under the lock
            if reason is not None:
                return {**result, "outcome": "skipped", "reason": reason}
            try:
                result["outcome"], result["left_internal"] = _offload_one(root, found, where, keep_internal, stub)
            except (Failed, OSError, ValueError, rgit.GitError, rfs.TreeError) as exc:
                result.update(outcome="failed", error=str(exc))
            _log(root, {"op": "offload", **result, "volume": where.volume.uuid, "path": where.path_of(name)})
    except Busy as exc:
        return {**result, "outcome": "skipped", "reason": str(exc)}
    return result


def _offload_one(root: Path, found: Plan, where: Target, keep_internal: bool,
                 stub: dict[str, Any] | None) -> tuple[str, list[str]]:
    name, base = found.name, found.base
    _clean_stub_temps(base)
    if stub is None:
        stub = {"format": FORMAT, "archive": name, "job_id": found.job_id, "state": "copying",
                "volume": {"uuid": where.volume.uuid, "name": where.volume.name, "fstype": where.volume.fstype,
                           "encrypted": where.volume.encrypted},
                "path": where.path_of(name), "manifest_sha256": found.manifest_sha256,
                "files": len(found.stores), "bytes": found.files_bytes, "bundle": found.bundle,
                "bundle_bytes": found.bundle_bytes, "bundle_sha256": None, "modes": _modes(base, found),
                "keep_internal": keep_internal, "removing": False, "started_at": _now(),
                "subfleet_version": __version__}
        _save(root, name, stub)
        _step("stub-copying")
    elif stub.get("manifest_sha256") != found.manifest_sha256:
        raise Failed(f"{STUB} names another manifest than the archive holds")
    folder = where.volume.mount / stub["path"]
    if stub["state"] == "copying":
        folder_fd = _open_below(where.volume.mount, stub["path"], create=True)
        try:
            bundle_sha = _copy_all(base, folder_fd, found)
            _step("copied")
            problems = _verify(folder, folder_fd, found, bundle_sha)
        finally:
            os.close(folder_fd)
        if problems:
            raise Failed("the copy did not read back: " + ", ".join(problems[:5])
                         + (f" and {len(problems) - 5} more" if len(problems) > 5 else ""))
        _save(root, name, stub, state="verified", bundle_sha256=bundle_sha, verified_at=_now(),
              keep_internal=keep_internal)
        _step("verified")
    else:
        # `verified`: read the external copy back again before anything
        # internal goes. A copy that no longer reads back is repaired from
        # the internal files still whole, and read back once more.
        folder_fd = _open_below(where.volume.mount, stub["path"])
        try:
            problems = _verify(folder, folder_fd, found, stub.get("bundle_sha256"))
            if problems:
                _copy_all(base, folder_fd, found, stub.get("bundle_sha256"))
                problems = _verify(folder, folder_fd, found, stub.get("bundle_sha256"))
                if problems:
                    raise Failed("the external copy no longer reads back: " + ", ".join(problems[:5]))
                _step("repaired")
            else:
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


def _copy_all(base: Path, folder_fd: int, found: Plan, bundle_sha: str | None = None) -> str | None:
    """Make the external folder hold the stored files and the bundle, copying
    what it lacks from the internal archive; the bundle's SHA-256 (the
    source's, unless `bundle_sha` is already known; None without a bundle)."""
    try:
        os.mkdir("files", 0o700, dir_fd=folder_fd)
        rfs.fullsync(folder_fd)
    except FileExistsError:
        pass
    files_fd = rfs.open_dir("files", dir_fd=folder_fd)
    try:
        if found.stores:
            try:
                source: int | None = rfs.open_dir(base / "files")
            except FileNotFoundError:
                source = None          # a resumed removal took the last of them
            try:
                for store, (size, sha) in sorted(found.stores.items()):
                    _place(source, store, files_fd, size, sha, STORED_MODE)
                    _step("copy-file")
            finally:
                if source is not None:
                    os.close(source)
        rfs.fullsync(files_fd)
    finally:
        os.close(files_fd)
    if found.bundle:
        source = rfs.open_dir(base)
        try:
            if bundle_sha is None:
                bundle_sha = _digest_at(source, found.bundle, found.bundle_bytes)
            if bundle_sha is None:
                raise Failed(f"{found.bundle} is missing or not the {found.bundle_bytes} bytes the manifest records")
            _place(source, found.bundle, folder_fd, found.bundle_bytes, bundle_sha, STORED_MODE)
            _step("copy-bundle")
        finally:
            os.close(source)
    rfs.fullsync(folder_fd)
    return bundle_sha


def _remove_internal(base: Path, found: Plan, stub: dict[str, Any], mount: Path) -> list[str]:
    """Remove the internal stored files and bundle that still match; the
    names left (changed since, or not the archive's)."""
    left: list[str] = []
    folder_fd = _open_below(mount, stub["path"])
    try:
        external_files = rfs.open_dir("files", dir_fd=folder_fd)
        try:
            files_fd = rfs.open_dir(base / "files")
        except FileNotFoundError:
            files_fd = None
        if files_fd is not None:
            try:
                for store, (size, sha) in sorted(found.stores.items()):
                    st = _lstat_at(files_fd, store)
                    if st is None:
                        continue
                    there = _lstat_at(external_files, store)
                    if there is None or not stat.S_ISREG(there.st_mode) or there.st_size != size:
                        raise Failed(f"the external copy of {store} went while the internal one was removed")
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
                os.rmdir(base / "files")
        os.close(external_files)
        if found.bundle:
            internal = rfs.open_dir(base)
            try:
                st = _lstat_at(internal, found.bundle)
                if st is not None:
                    there = _lstat_at(folder_fd, found.bundle)
                    if there is None or there.st_size != found.bundle_bytes:
                        raise Failed(f"the external {found.bundle} went while the internal one was removed")
                    if _digest_at(internal, found.bundle, found.bundle_bytes) == stub.get("bundle_sha256"):
                        os.unlink(found.bundle, dir_fd=internal)
                        _step("remove-bundle")
                    else:
                        left.append(found.bundle)
            finally:
                os.close(internal)
    finally:
        os.close(folder_fd)
    rfs.sync_path(base)
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
        where = Target(directory, volume, _under(volume.mount, directory))
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
    return result


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
            external["path"], external["fd"] = path, _open_below(volume.mount, stub["path"])
        return external["path"], external["fd"]

    try:
        if found.stores or modes.get("files") is not None:
            try:
                os.mkdir(base / "files", 0o700)
            except FileExistsError:
                pass
            files_fd = rfs.open_dir(base / "files")
            try:
                for store, (size, sha) in sorted(found.stores.items()):
                    st = _lstat_at(files_fd, store)
                    if st is not None:
                        if stat.S_ISREG(st.st_mode) and st.st_size == size \
                                and _digest_at(files_fd, store, size) == sha:
                            continue
                        raise Failed(f"files/{store} is there and is not the archived file; left as it is")
                    temporary = _RECALL + store
                    _remove_at(files_fd, temporary)
                    source = rfs.open_dir("files", dir_fd=folder()[1])
                    try:
                        _copy_into(source, store, files_fd, temporary, stored_modes.get(store, STORED_MODE))
                    finally:
                        os.close(source)
                    if _digest_at(files_fd, temporary, size) != sha:
                        os.unlink(temporary, dir_fd=files_fd)
                        raise Failed(f"files/{store} did not read back from the external copy")
                    os.rename(temporary, store, src_dir_fd=files_fd, dst_dir_fd=files_fd)
                    out["copied_back"] += 1
                    _step("recall-file")
                if modes.get("files") is not None:
                    os.fchmod(files_fd, modes["files"])
                for store, mode in stored_modes.items():
                    if store in found.stores:
                        os.chmod(store, mode, dir_fd=files_fd, follow_symlinks=False)
                rfs.fullsync(files_fd)
            finally:
                os.close(files_fd)
        if found.bundle:
            internal = rfs.open_dir(base)
            try:
                st = _lstat_at(internal, found.bundle)
                expected = stub.get("bundle_sha256")
                if st is not None:
                    if expected is not None and _digest_at(internal, found.bundle, found.bundle_bytes) != expected:
                        raise Failed(f"{found.bundle} is there and is not the archived bundle; left as it is")
                else:
                    if expected is None:
                        raise Failed(f"{found.bundle} is gone and {STUB} records no checksum for it")
                    temporary = _RECALL + found.bundle
                    _remove_at(internal, temporary)
                    _copy_into(folder()[1], found.bundle, internal, temporary, modes.get("bundle") or 0o644)
                    if _digest_at(internal, temporary, found.bundle_bytes) != expected \
                            or not _heads_ok(base / temporary, found.bundle_heads):
                        os.unlink(temporary, dir_fd=internal)
                        raise Failed(f"{found.bundle} did not read back from the external copy")
                    os.rename(temporary, found.bundle, src_dir_fd=internal, dst_dir_fd=internal)
                    out["copied_back"] += 1
                    _step("recall-bundle")
                if modes.get("bundle") is not None:
                    os.chmod(found.bundle, modes["bundle"], dir_fd=internal, follow_symlinks=False)
            finally:
                os.close(internal)
        rfs.sync_path(base)
        if not stub.get("recalled"):
            # Every file is back and read back: the internal copy is the one
            # to read again, before the external one may go.
            _save(root, name, stub, recalled=True, recalled_at=_now())
        _step("recalled")
        out["external"] = _drop_external(found, stub, folder) if drop_external else "kept"
        os.unlink(stub_path(root, name))
        rfs.sync_path(base)
        _step("stub-dropped")
    finally:
        if "fd" in external:
            os.close(external["fd"])
    return out


def _drop_external(found: Plan, stub: dict[str, Any], folder: Callable[[], tuple[Path, int]]) -> str:
    """Remove the external copy's own entries (the internal copy is verified by now)."""
    try:
        path, folder_fd = folder()
    except (VolumeMissing, OSError, Failed) as exc:
        return f"kept: {exc}"
    try:
        files_fd = rfs.open_dir("files", dir_fd=folder_fd)
    except FileNotFoundError:
        files_fd = None
    if files_fd is not None:
        try:
            for store in sorted(found.stores):
                try:
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
            os.unlink(found.bundle, dir_fd=folder_fd)
        except FileNotFoundError:
            pass
    rfs.fullsync(folder_fd)
    try:
        os.rmdir(path)
        rfs.sync_path(path.parent)
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
