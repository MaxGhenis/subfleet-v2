"""Archive a job directory, verify it, delete only what was archived, restore it (C-8.4).

Retention keeps Subfleet's own record of a pruned job: before a job directory
is deleted, every entry in it is written to a compressed PAX tar with a manifest
of each entry's signature and sha256, and the job's database rows are written
beside it. The archive is made durable and read back in full before anything is
deleted. Deletion then removes only entries that the verified manifest lists
with an unchanged signature, and removes directories only with `rmdir`, so an
entry that appeared or changed afterwards survives. Worktrees are never handled
here: retention never touches one, and the machine's worktree archiver reclaims
them (`docs/desktop/retention-archive.md`).

All filesystem access is descriptor-relative and never follows a symlink.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
MANIFEST = "manifest.json"
ROWS = "rows.json"
PARTIAL_PREFIX = ".partial-"
CHUNK = 4 << 20
#: Refuse to write an archive that would leave less than this free (review Opus 7).
FREE_FLOOR_BYTES = 2 * 1024 ** 3
COMPRESSION = "zst" if "zst" in tarfile.TarFile.OPEN_METH else "gz"
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_KINDS = {stat.S_IFDIR: "dir", stat.S_IFREG: "file", stat.S_IFLNK: "symlink", stat.S_IFIFO: "fifo",
          stat.S_IFSOCK: "socket", stat.S_IFCHR: "char", stat.S_IFBLK: "block"}
_COMPARED = ("type", "mode", "size", "mtime_ns", "ctime_ns", "dev", "ino")


class NotQuiet(Exception):
    """The directory changed while it was archived; try again later."""


class Unarchivable(Exception):
    """The directory cannot be archived faithfully now (unreadable, a mount point, no space)."""


class ArchiveCorrupt(Exception):
    """The archive does not match its manifest; it authorizes no deletion."""


def _never() -> None:
    return None


def kind(mode: int) -> str:
    return _KINDS.get(stat.S_IFMT(mode), "other")


def signature(st: os.stat_result) -> dict[str, int | str]:
    return {"type": kind(st.st_mode), "mode": st.st_mode, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "ctime_ns": st.st_ctime_ns, "dev": st.st_dev, "ino": st.st_ino, "nlink": st.st_nlink}


def unchanged(entry: Mapping[str, Any], st: os.stat_result, ctime_ns: int | None = None) -> bool:
    """Whether `st` is the entry as archived. `ctime_ns` stands in for the recorded
    ctime when retention itself changed it, by unlinking another hard link."""
    now = signature(st)
    return all((ctime_ns if key == "ctime_ns" and ctime_ns is not None else entry[key]) == now[key]
               for key in _COMPARED)


# ---------------------------------------------------------------------------
# durability
# ---------------------------------------------------------------------------

def full_sync(fd: int) -> None:
    """fsync, and on macOS F_FULLFSYNC, which also flushes the drive's cache (fsync(2))."""
    os.fsync(fd)
    if hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
        except OSError:
            pass                                    # a filesystem without it; fsync stands


def sync_dir(path: str | Path) -> None:
    fd = os.open(path, _DIR)
    try:
        full_sync(fd)
    finally:
        os.close(fd)


def write_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        full_sync(fd)
    finally:
        os.close(fd)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _plain(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$base64": base64.b64encode(value).decode()}
    return value


def rows_snapshot(query: Callable[[str, Sequence[Any]], list], job_id: str) -> dict[str, Any]:
    """The rows retention deletes for `job_id`, as plain JSON, with their digest.

    `query` may be the store's reader or a transaction's; both give the same
    answer while nothing else writes, which is what the commit checks."""
    by_attempt = "attempt_id IN (SELECT attempt_id FROM attempts WHERE job_id=?)"
    tables = {"jobs": "job_id=?", "attempts": "job_id=?", "artifacts": by_attempt, "readings": by_attempt,
              "notices": "job_id=?", "decisions": "job_id=?"}
    rows = {}
    for table, where in tables.items():
        found = query(f"SELECT * FROM {table} WHERE {where} ORDER BY rowid", (job_id,))
        rows[table] = [{key: _plain(row[key]) for key in row.keys()} for row in found]
    return {"job_id": job_id, "rows": rows, "sha256": hashlib.sha256(canonical(rows)).hexdigest()}


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

class _Reader(io.RawIOBase):
    """`size` bytes from a descriptor, hashed as they pass; fails if the file shrinks."""

    def __init__(self, fd: int, size: int, check: Callable[[], None]):
        self.fd, self.remaining, self.check = fd, size, check
        self.sha256 = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        want = min(len(buffer), self.remaining, CHUNK)
        if want <= 0:
            return 0
        data = os.read(self.fd, want)
        if not data:
            raise NotQuiet("a file shrank while it was archived")
        buffer[:len(data)] = data
        self.remaining -= len(data)
        self.sha256.update(data)
        self.check()
        return len(data)


def _join(parent: str, name: str) -> str:
    return name if not parent else parent + "/" + name


def _names(fd: int) -> list[str]:
    return sorted(os.listdir(fd), key=os.fsencode)


def _add_tree(root: Path, tar: tarfile.TarFile, check: Callable[[], None]) -> dict[str, Any]:
    """Every entry below the directory `root`, into `tar`; the manifest's entry list."""
    root_fd = os.open(root, _DIR)
    try:
        top = os.fstat(root_fd)
        entries: list[dict[str, Any]] = [{"path": "", **signature(top)}]
        known = {"": (top.st_dev, top.st_ino)}
        first_link: dict[tuple[int, int], str] = {}
        size = 0
        pending = [""]
        while pending:
            check()
            relative = pending.pop()
            fd = root_fd if not relative else os.open(relative, _DIR, dir_fd=root_fd)
            try:
                here = os.fstat(fd)
                if (here.st_dev, here.st_ino) != known[relative]:
                    raise NotQuiet(f"directory {relative!r} was replaced while it was archived")
                below = []
                for name in _names(fd):
                    check()
                    path = _join(relative, name)
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if st.st_dev != top.st_dev:
                        raise Unarchivable(f"{path!r} is on another device")
                    entry = {"path": path, **signature(st)}
                    info = tarfile.TarInfo(path)
                    info.mode, info.mtime = stat.S_IMODE(st.st_mode), st.st_mtime_ns / 1e9
                    info.uid, info.gid = st.st_uid, st.st_gid
                    if stat.S_ISDIR(st.st_mode):
                        known[path] = (st.st_dev, st.st_ino)
                        below.append(path)
                        info.type = tarfile.DIRTYPE
                        tar.addfile(info)
                    elif stat.S_ISLNK(st.st_mode):
                        entry["link"] = os.readlink(name, dir_fd=fd)
                        info.type, info.linkname = tarfile.SYMTYPE, entry["link"]
                        tar.addfile(info)
                        size += st.st_size
                    elif stat.S_ISREG(st.st_mode):
                        size += st.st_size
                        _add_file(fd, name, st, entry, info, tar, check, first_link)
                    elif stat.S_ISFIFO(st.st_mode):
                        info.type = tarfile.FIFOTYPE
                        tar.addfile(info)
                    # a socket or device holds no data: recorded, not stored
                    entries.append(entry)
                pending.extend(reversed(below))
            except PermissionError as exc:
                raise Unarchivable(f"unreadable entry below {relative!r}: {exc.strerror}") from None
            finally:
                if fd != root_fd:
                    os.close(fd)
        return {"entries": entries, "bytes": size}
    finally:
        os.close(root_fd)


def _add_file(dir_fd, name, st, entry, info, tar, check, first_link) -> None:
    identity = (st.st_dev, st.st_ino)
    if st.st_nlink > 1 and identity in first_link:
        entry["hardlink"] = first_link[identity]
        info.type, info.linkname = tarfile.LNKTYPE, entry["hardlink"]
        tar.addfile(info)
        return
    fd = os.open(name, _FILE, dir_fd=dir_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != identity:
            raise NotQuiet(f"{entry['path']!r} was replaced while it was archived")
        info.size = st.st_size
        reader = _Reader(fd, st.st_size, check)
        try:
            tar.addfile(info, io.BufferedReader(reader, CHUNK))
        except OSError as exc:
            if "unexpected end of data" in str(exc):
                raise NotQuiet(f"{entry['path']!r} shrank while it was archived") from None
            raise
        after = os.fstat(fd)
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (st.st_size, st.st_mtime_ns, st.st_ctime_ns):
            raise NotQuiet(f"{entry['path']!r} changed while it was archived")
        entry["sha256"] = reader.sha256.hexdigest()
        if st.st_nlink > 1:
            first_link[identity] = entry["path"]
    finally:
        os.close(fd)


def tree_bytes(path: Path) -> int:
    """Logical bytes of the regular files and symlinks below `path` (no symlink followed)."""
    total = 0
    for directory, dirnames, filenames in os.walk(path, followlinks=False):
        for name in [*filenames, *(d for d in dirnames if os.path.islink(os.path.join(directory, d)))]:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except FileNotFoundError:
                pass
    return total


def archive(job_dir: Path, archive_root: Path, job_id: str, rows: dict[str, Any], *,
            check: Callable[[], None] = _never, free_floor: int = FREE_FLOOR_BYTES) -> Path:
    """Archive `job_dir` and `rows` to `archive_root/<job_id>`, durably, verified; return it.

    Written under a partial name and renamed into place only after the read-back
    verification passes, so an archive at the final name is always complete. A
    missing job directory is recorded as such; a symlinked one as its target."""
    final = archive_root / job_id
    if os.path.lexists(final):
        raise Unarchivable(f"an archive already exists at {final}")
    archive_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    partial = archive_root / (PARTIAL_PREFIX + job_id)
    if os.path.lexists(partial):
        discard(partial)
    try:
        top = os.lstat(job_dir)
    except FileNotFoundError:
        top = None
    size = tree_bytes(job_dir) if top is not None and stat.S_ISDIR(top.st_mode) else 0
    if shutil.disk_usage(archive_root).free < free_floor + size:
        raise Unarchivable(f"less than {free_floor + size} bytes free for the archive")
    os.mkdir(partial, 0o700)
    manifest: dict[str, Any] = {"version": FORMAT_VERSION, "job_id": job_id, "original": str(job_dir),
                                "compression": COMPRESSION, "rows_sha256": rows["sha256"],
                                "request_id": _request_id(rows)}
    try:
        if top is None:
            manifest["tree"] = None
        elif stat.S_ISLNK(top.st_mode):
            manifest["tree"] = {"symlink": os.readlink(job_dir), "entry": {"path": "", **signature(top)}}
        elif stat.S_ISDIR(top.st_mode):
            name = f"job.tar.{COMPRESSION}"
            raw = open(partial / name, "xb")
            try:
                options = {"level": 3} if COMPRESSION == "zst" else {"compresslevel": 3}
                with tarfile.open(fileobj=raw, mode=f"w:{COMPRESSION}", format=tarfile.PAX_FORMAT,
                                  encoding="utf-8", errors="surrogateescape", **options) as tar:
                    manifest["tree"] = {"tar": name, **_add_tree(job_dir, tar, check)}
                raw.flush()
                full_sync(raw.fileno())
            finally:
                raw.close()
        else:
            raise Unarchivable(f"{job_dir} is not a directory")
        write_file(partial / ROWS, canonical(rows))
        write_file(partial / MANIFEST, canonical(manifest))
        sync_dir(partial)
        verify(partial, check=check)
        os.rename(partial, final)
        sync_dir(archive_root)
    except BaseException:
        discard(partial)
        raise
    return final


def _request_id(rows: Mapping[str, Any]) -> str | None:
    jobs = rows.get("rows", {}).get("jobs") or []
    return jobs[0].get("request_id") if jobs else None


def load(archive_dir: Path) -> dict[str, Any]:
    with os.fdopen(os.open(archive_dir / MANIFEST, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb") as stream:
        return json.loads(stream.read())


def discard(archive_dir: Path) -> None:
    """Remove an archive retention wrote: only its own file names, then the directory.

    Anything else found inside keeps the directory (rmdir fails), by design."""
    for name in os.listdir(archive_dir) if archive_dir.is_dir() and not archive_dir.is_symlink() else []:
        if name in (MANIFEST, ROWS, "job.tar.zst", "job.tar.gz"):
            os.unlink(archive_dir / name)
    try:
        os.rmdir(archive_dir)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------

def verify(archive_dir: Path, *, check: Callable[[], None] = _never) -> dict[str, Any]:
    """Read the whole archive back; raise ArchiveCorrupt on any difference from its manifest."""
    try:
        manifest = load(archive_dir)
        with open(archive_dir / ROWS, "rb") as stream:
            rows = json.loads(stream.read())
    except (OSError, ValueError) as exc:
        raise ArchiveCorrupt(f"unreadable manifest or rows ({type(exc).__name__})") from None
    if manifest.get("version") != FORMAT_VERSION:
        raise ArchiveCorrupt("unknown archive format")
    if rows.get("sha256") != manifest.get("rows_sha256") or \
            hashlib.sha256(canonical(rows.get("rows"))).hexdigest() != rows.get("sha256"):
        raise ArchiveCorrupt("rows.json does not match its digest")
    tree = manifest.get("tree")
    if not tree or "tar" not in tree:
        return manifest
    expected = [e for e in tree["entries"] if e["path"] and e["type"] in ("dir", "file", "symlink", "fifo")]
    try:
        with tarfile.open(archive_dir / tree["tar"], mode=f"r:{manifest['compression']}",
                          encoding="utf-8", errors="surrogateescape") as tar:
            count = 0
            for member in tar:
                check()
                if count >= len(expected):
                    raise ArchiveCorrupt(f"extra member {member.name!r}")
                _compare(tar, member, expected[count], check)
                count += 1
            if count != len(expected):
                raise ArchiveCorrupt(f"{len(expected) - count} members missing")
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        raise ArchiveCorrupt(f"unreadable archive ({type(exc).__name__}: {exc})") from None
    return manifest


def _compare(tar, member, entry, check) -> None:
    problem = None
    if member.name != entry["path"]:
        problem = f"name {member.name!r}"
    elif member.mode != stat.S_IMODE(entry["mode"]):
        problem = "mode"
    elif entry["type"] == "dir":
        problem = None if member.isdir() else "type"
    elif entry["type"] == "symlink":
        problem = None if member.issym() and member.linkname == entry["link"] else "symlink"
    elif entry["type"] == "fifo":
        problem = None if member.isfifo() else "type"
    elif "hardlink" in entry:
        problem = None if member.islnk() and member.linkname == entry["hardlink"] else "hard link"
    elif not member.isreg() or member.size != entry["size"]:
        problem = "size"
    else:
        digest = hashlib.sha256()
        stream = tar.extractfile(member)
        while data := stream.read(CHUNK):
            digest.update(data)
            check()
        problem = None if digest.hexdigest() == entry["sha256"] else "content"
    if problem:
        raise ArchiveCorrupt(f"{entry['path']!r}: {problem}")


# ---------------------------------------------------------------------------
# verified deletion
# ---------------------------------------------------------------------------

def delete_archived(job_dir: Path, manifest: Mapping[str, Any], *,
                    check: Callable[[], None] = _never) -> list[str]:
    """Delete the entries of `job_dir` the manifest lists with unchanged signatures.

    Directories are visited deepest first, matched by identity only (removing
    their contents changes their times), and removed with `rmdir`. Anything the
    manifest does not list, or whose signature changed, is kept. Idempotent: a
    resumed deletion passes over what is already gone. Returns the kept paths
    ("" for the directory itself)."""
    tree = manifest.get("tree")
    if tree is None:
        return []
    if "symlink" in tree:
        try:
            st = os.lstat(job_dir)
        except FileNotFoundError:
            return []
        if stat.S_ISLNK(st.st_mode) and os.readlink(job_dir) == tree["symlink"] and \
                (st.st_dev, st.st_ino) == (tree["entry"]["dev"], tree["entry"]["ino"]):
            os.unlink(job_dir)
            return []
        return [""]
    entries = {e["path"]: e for e in tree["entries"]}
    directories = sorted((p for p, e in entries.items() if e["type"] == "dir"),
                         key=lambda p: (-(p.count("/") + 1) if p else 1, os.fsencode(p)))
    kept: list[str] = []
    relinked: dict[tuple[int, int], int] = {}
    parent_fd = os.open(job_dir.parent, _DIR)
    try:
        try:
            root_fd = os.open(job_dir.name, _DIR, dir_fd=parent_fd)
        except FileNotFoundError:
            return kept
        except OSError:
            return [""]
        try:
            root = os.fstat(root_fd)
            if (root.st_dev, root.st_ino) != (entries[""]["dev"], entries[""]["ino"]):
                return [""]
            for directory in directories:
                check()
                _empty(root_fd, directory, entries, kept, relinked)
        finally:
            os.close(root_fd)
        if not kept:
            try:
                os.rmdir(job_dir.name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                kept.append("")
    finally:
        os.close(parent_fd)
    return kept


def _relinked_by_us(root_fd, entry, st, entries) -> bool:
    """A hard link whose only change is the ctime an earlier, interrupted deletion gave it.

    Everything but ctime must match, and the drop in the link count must be exactly
    the number of this inode's archived paths that are already gone."""
    if entry["type"] != "file" or entry["nlink"] < 2 or \
            not unchanged(entry, st, st.st_ctime_ns if st.st_ctime_ns > entry["ctime_ns"] else None):
        return False
    group = [e["path"] for e in entries.values() if (e.get("dev"), e.get("ino")) == (entry["dev"], entry["ino"])]
    gone = 0
    for path in group:
        try:
            os.stat(path, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            gone += 1
        except OSError:
            return False
    return gone > 0 and st.st_nlink == entry["nlink"] - gone


def _empty(root_fd, directory, entries, kept, relinked) -> None:
    try:
        fd = os.open(directory, _DIR, dir_fd=root_fd) if directory else os.dup(root_fd)
    except FileNotFoundError:
        return                                       # gone in an earlier, interrupted run
    except OSError:
        kept.append(directory)                       # replaced by a symlink or a file
        return
    try:
        st = os.fstat(fd)
        entry = entries[directory]
        if (st.st_dev, st.st_ino) != (entry["dev"], entry["ino"]):
            kept.append(directory)
            return
        if stat.S_IMODE(st.st_mode) & stat.S_IRWXU != stat.S_IRWXU:
            os.fchmod(fd, stat.S_IMODE(st.st_mode) | stat.S_IRWXU)
        for name in os.listdir(fd):
            path = _join(directory, name)
            entry = entries.get(path)
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if entry is None:
                kept.append(path)
                continue
            if stat.S_ISDIR(st.st_mode):
                if entry["type"] != "dir" or (st.st_dev, st.st_ino) != (entry["dev"], entry["ino"]):
                    kept.append(path)
                    continue
                try:
                    os.rmdir(name, dir_fd=fd)
                except OSError:
                    pass                             # not empty: what it holds was kept already
                continue
            identity = (st.st_dev, st.st_ino)
            if not unchanged(entry, st, relinked.get(identity)) and not _relinked_by_us(root_fd, entry, st, entries):
                kept.append(path)
                continue
            if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                # Unlinking one link changes the inode's ctime for the others; read
                # the new value through a descriptor taken before the unlink.
                handle = os.open(name, _FILE, dir_fd=fd)
                try:
                    now = os.fstat(handle)
                    if (now.st_dev, now.st_ino) != identity:
                        kept.append(path)
                        continue
                    os.unlink(name, dir_fd=fd)
                    relinked[identity] = os.fstat(handle).st_ctime_ns
                finally:
                    os.close(handle)
            else:
                os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------

def restore(archive_dir: Path, destination: Path) -> list[str]:
    """Recreate the archived job directory at `destination`, which must not exist.

    Returns the paths not recreated: sockets and devices, which carry no data."""
    manifest = verify(archive_dir)
    tree = manifest.get("tree")
    if tree is None:
        return []
    if os.path.lexists(destination):
        raise FileExistsError(f"{destination} exists")
    if "symlink" in tree:
        os.symlink(tree["symlink"], destination)
        return []
    destination.mkdir(mode=0o700)
    with tarfile.open(archive_dir / tree["tar"], mode=f"r:{manifest['compression']}",
                      encoding="utf-8", errors="surrogateescape") as tar:
        tar.extractall(destination, filter="tar")
    skipped = []
    by_depth = sorted(tree["entries"], key=lambda e: -(e["path"].count("/") + 1) if e["path"] else 1)
    for entry in by_depth:
        target = destination / entry["path"] if entry["path"] else destination
        if entry["type"] in ("socket", "char", "block", "other"):
            skipped.append(entry["path"])
            continue
        if entry["type"] != "symlink":
            os.chmod(target, stat.S_IMODE(entry["mode"]))
        os.utime(target, ns=(entry["mtime_ns"], entry["mtime_ns"]), follow_symlinks=False)
    return skipped
