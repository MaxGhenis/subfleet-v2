"""Filesystem primitives for retention by archive (C-8.4, C-13.4, design d635).

Everything here is descriptor-relative and never follows a symlink: a directory
is opened with ``O_DIRECTORY | O_NOFOLLOW`` relative to its parent and checked
against the (device, inode) its parent listed, so a directory swapped for a link
mid-walk sends nothing outside the tree.

An entry's *signature* is what its ``lstat`` said when it was archived. Verified
deletion (`reclaim`) unlinks an entry only if its signature is unchanged right
before the unlink; anything new or changed is renamed into the job's conflicts
folder instead, never deleted.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import os
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
O_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
CHUNK = 1 << 20
#: Deeper trees are refused (deferred) rather than walked with a descriptor per level.
MAX_DEPTH = 400

Check = Callable[[], None]


class TreeError(Exception):
    """The tree cannot be archived as it is; `reason` is a short stable code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "f"
    if stat.S_ISDIR(mode):
        return "d"
    if stat.S_ISLNK(mode):
        return "l"
    if stat.S_ISFIFO(mode):
        return "p"
    if stat.S_ISSOCK(mode):
        return "s"
    if stat.S_ISCHR(mode):
        return "c"
    if stat.S_ISBLK(mode):
        return "b"
    return "?"


def signature(st: os.stat_result) -> dict[str, int | str]:
    return {"t": kind(st.st_mode), "mode": stat.S_IMODE(st.st_mode), "dev": st.st_dev, "ino": st.st_ino,
            "size": st.st_size, "mtime": st.st_mtime_ns, "ctime": st.st_ctime_ns, "nlink": st.st_nlink}


def unchanged(recorded: dict[str, Any], st: os.stat_result) -> bool:
    """Whether `st` is still the entry `recorded` describes.

    A directory is matched on type, device and inode only: removing its children
    and the rename into quarantine change its times, and deletion makes a
    read-only directory writable (review of ccc85387, finding 8). A file whose
    inode had other links when archived is matched without its ctime, because
    unlinking one link changes the others' ctime (APFS); its size, mtime and
    mode still have to match, so a write is still seen.
    """
    t = kind(st.st_mode)
    if t != recorded["t"] or st.st_dev != recorded["dev"] or st.st_ino != recorded["ino"]:
        return False
    if t == "d":
        return True
    if (st.st_size != recorded["size"] or st.st_mtime_ns != recorded["mtime"]
            or stat.S_IMODE(st.st_mode) != recorded["mode"]):
        return False
    return recorded.get("nlink", 1) > 1 or st.st_ctime_ns == recorded["ctime"]


def same_content_signature(a: os.stat_result, b: os.stat_result) -> bool:
    """For one open file before and after it was read: no write happened between."""
    return (a.st_ino == b.st_ino and a.st_dev == b.st_dev and a.st_size == b.st_size
            and a.st_mtime_ns == b.st_mtime_ns and a.st_ctime_ns == b.st_ctime_ns)


def sig_key(st: os.stat_result) -> str:
    """A name for one version of one file: any write changes it."""
    return f"{st.st_dev:x}-{st.st_ino:x}-{st.st_size:x}-{st.st_mtime_ns:x}-{st.st_ctime_ns:x}"


# --- walking -------------------------------------------------------------------

def _names(fd: int) -> list[str]:
    return sorted(os.listdir(fd), key=os.fsencode)


def join(parent: str, name: str) -> str:
    return name if not parent else parent + "/" + name


def open_dir(name: str | os.PathLike, *, dir_fd: int | None = None,
             expect: os.stat_result | None = None) -> int:
    fd = os.open(name, O_DIR, dir_fd=dir_fd)
    if expect is not None:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (expect.st_dev, expect.st_ino):
            os.close(fd)
            raise TreeError("swapped", f"{name} changed while it was opened")
    return fd


def walk(root_fd: int, check: Check | None = None) -> Iterator[tuple[str, os.stat_result, int, str | None]]:
    """Every entry under an open directory, pre-order, names in byte order.

    Yields ``(relative path, lstat, parent descriptor, name)``; the root comes
    first as ``("", fstat, root_fd, None)``. The parent descriptor is valid only
    until the generator is advanced. A mount point (another device) or a tree
    deeper than `MAX_DEPTH` raises `TreeError`; an unreadable directory raises
    `TreeError("unreadable")`.
    """
    root = os.fstat(root_fd)
    yield "", root, root_fd, None
    stack: list[tuple[int, str, Iterator[str]]] = []
    try:
        stack.append((root_fd, "", iter(_list(root_fd, ""))))
        while stack:
            fd, rel, names = stack[-1]
            name = next(names, None)
            if name is None:
                stack.pop()
                if fd != root_fd:
                    os.close(fd)
                continue
            if check is not None:
                check()
            path = join(rel, name)
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                raise TreeError("changed", f"{path} vanished during the walk") from None
            if st.st_dev != root.st_dev:
                raise TreeError("mount-point", path)
            yield path, st, fd, name
            if stat.S_ISDIR(st.st_mode):
                if len(stack) >= MAX_DEPTH:
                    raise TreeError("too-deep", path)
                try:
                    child = open_dir(name, dir_fd=fd, expect=st)
                except PermissionError as exc:
                    raise TreeError("unreadable", f"{path}: {exc.strerror}") from None
                except FileNotFoundError:
                    raise TreeError("changed", f"{path} vanished during the walk") from None
                except NotADirectoryError:
                    raise TreeError("changed", f"{path} was replaced during the walk") from None
                stack.append((child, path, iter(_list(child, path))))
    finally:
        for fd, _, _ in stack:
            if fd != root_fd:
                os.close(fd)


def _list(fd: int, rel: str) -> list[str]:
    try:
        return _names(fd)
    except PermissionError as exc:
        raise TreeError("unreadable", f"{rel or '.'}: {exc.strerror}") from None


def tree_bytes(path: Path, check: Check | None = None) -> int:
    """Apparent bytes of regular files and links under `path` (0 if absent)."""
    try:
        fd = os.open(path, O_DIR)
    except FileNotFoundError:
        return 0
    except NotADirectoryError:
        return os.lstat(path).st_size
    try:
        return sum(st.st_size for rel, st, _, _ in walk(fd, check)
                   if rel and not stat.S_ISDIR(st.st_mode))
    finally:
        os.close(fd)


# --- reading, hashing, cloning ---------------------------------------------------

def git_blob_hasher(object_format: str, size: int):
    h = hashlib.new(object_format)
    h.update(b"blob %d\0" % size)
    return h


def read_hashes(fd: int, size: int, object_format: str | None, check: Check | None = None) -> tuple[str, str | None]:
    """sha256 of an open file's bytes, and its git blob id when `object_format`
    is given. Raises `TreeError("changed")` if fewer or more bytes than `size`
    are read (the file changed under us)."""
    digest = hashlib.sha256()
    blob = git_blob_hasher(object_format, size) if object_format else None
    offset = 0
    while True:
        if check is not None and offset and offset % (64 * CHUNK) == 0:
            check()
        chunk = os.pread(fd, CHUNK, offset)
        if not chunk:
            break
        offset += len(chunk)
        digest.update(chunk)
        if blob is not None:
            blob.update(chunk)
        if offset > size:
            raise TreeError("changed", "file grew while it was read")
    if offset != size:
        raise TreeError("changed", "file shrank while it was read")
    return digest.hexdigest(), (blob.hexdigest() if blob is not None else None)


_libc = ctypes.CDLL(None, use_errno=True)
_fclonefileat = getattr(_libc, "fclonefileat", None)
if _fclonefileat is not None:
    _fclonefileat.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    _fclonefileat.restype = ctypes.c_int
CLONE_NOFOLLOW = 0x0001
#: Tests set this to exercise the byte-copy path that a non-APFS volume takes.
FORCE_COPY = False
#: A byte copy (no clones on this volume) or a bundle never takes free space
#: below this: retention must never be what fills the disk.
LOW_SPACE_FLOOR = 2 * 1024 ** 3


def free_bytes(fd: int) -> int:
    st = os.fstatvfs(fd)
    return st.f_bavail * st.f_frsize


def clone_or_copy(src_fd: int, dst_dir_fd: int, name: str, check: Check | None = None) -> str:
    """Put the open file's bytes at `name` in `dst_dir_fd`: an APFS clone (no
    space until either side changes), else a byte copy. Returns the method."""
    if _fclonefileat is not None and not FORCE_COPY:
        if _fclonefileat(src_fd, dst_dir_fd, os.fsencode(name), CLONE_NOFOLLOW) == 0:
            return "clone"
        code = ctypes.get_errno()
        if code not in (errno.ENOTSUP, errno.EXDEV, errno.ENOSYS, errno.EINVAL):
            raise OSError(code, os.strerror(code), name)
    size = os.fstat(src_fd).st_size
    if free_bytes(dst_dir_fd) - size < LOW_SPACE_FLOOR:
        raise TreeError("low-space", f"copying {size} bytes would leave less than {LOW_SPACE_FLOOR} free")
    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                  dir_fd=dst_dir_fd)
    try:
        offset = 0
        while True:
            if check is not None and offset and offset % (64 * CHUNK) == 0:
                check()
            chunk = os.pread(src_fd, CHUNK, offset)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                view = view[os.write(out, view):]
            offset += len(chunk)
        fullsync(out)
    finally:
        os.close(out)
    return "copy"


# --- durability ------------------------------------------------------------------

def fullsync(fd: int) -> None:
    """fsync that reaches the medium on macOS (``F_FULLFSYNC``; review finding 6)."""
    try:
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    except (AttributeError, OSError):
        os.fsync(fd)


def sync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_DIRECTORY if path.is_dir() else 0))
    try:
        fullsync(fd)
    finally:
        os.close(fd)


def write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Replace `path` with `data` durably: temp file, full sync, rename, sync the directory."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        fullsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    sync_path(path.parent)


def read_regular(path: Path, limit: int = 1 << 30) -> bytes:
    fd = os.open(path, O_FILE)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, CHUNK)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > limit:
                raise OSError(errno.EFBIG, "file too large", str(path))
            chunks.append(chunk)
    finally:
        os.close(fd)


def remove_own_tree(path: Path) -> None:
    """Delete a directory retention itself created and alone writes (a cache, a
    temporary verification repository). Never used on a job's trees."""
    try:
        fd = os.open(path, O_DIR)
    except FileNotFoundError:
        return
    try:
        _remove_children(fd)
    finally:
        os.close(fd)
    os.rmdir(path)


def _remove_children(fd: int) -> None:
    os.fchmod(fd, stat.S_IMODE(os.fstat(fd).st_mode) | 0o700)
    for name in os.listdir(fd):
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            child = open_dir(name, dir_fd=fd, expect=st)
            try:
                _remove_children(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)


# --- verified deletion --------------------------------------------------------------

class Reclaim:
    """Delete exactly the entries a verified manifest lists with unchanged signatures.

    ``entries`` maps each relative path ("" is the root) to its manifest record
    (``sig``). Anything unlisted or changed is renamed, under its own relative
    path, into ``conflicts/<label>/``; nothing unlisted or changed is ever
    unlinked. A directory is removed only with ``rmdir`` once its listed
    children are gone. Re-running on a partly deleted tree finishes it: entries
    already gone are simply absent (resumable, finding 8 of the design review).
    """

    def __init__(self, entries: dict[str, dict[str, Any]], conflicts: Path, label: str,
                 check: Check | None = None):
        self.entries = entries
        self.conflicts = conflicts
        self.label = label
        self.check = check
        self.deleted = 0
        self.bytes = 0
        self.kept: list[dict[str, str]] = []
        self.errors: list[dict[str, str]] = []

    def run(self, root: Path) -> bool:
        """True when the root is gone; False when something had to stay in place."""
        try:
            st = os.lstat(root)
        except FileNotFoundError:
            return True
        record = self.entries.get("")
        if record is None or not unchanged(record["sig"], st):
            # Not the tree that was archived: move it aside whole, untouched.
            self._set_aside(root.parent, root.name, "", "root-replaced")
            return not os.path.lexists(root)
        fd = open_dir(root, expect=st)
        try:
            self._empty(fd, "")
        finally:
            os.close(fd)
        try:
            os.rmdir(root)
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.EPERM, errno.EACCES):
                raise
            # Whatever stayed (an entry that could not be moved or unlinked)
            # goes aside with its directory, so nothing half-deleted is left.
            self._set_aside(root.parent, root.name, "", "remainder")
        return not os.path.lexists(root)

    def _empty(self, fd: int, rel: str, rounds: int = 3) -> None:
        for _ in range(rounds):
            names = os.listdir(fd)
            if not names:
                return
            for name in sorted(names, key=os.fsencode):
                if self.check is not None:
                    self.check()
                self._one(fd, rel, name)

    def _one(self, fd: int, rel: str, name: str) -> None:
        path = join(rel, name)
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        record = self.entries.get(path)
        if record is None:
            self._set_aside_fd(fd, name, path, "new")
            return
        if not unchanged(record["sig"], st):
            self._set_aside_fd(fd, name, path, "changed")
            return
        if stat.S_ISDIR(st.st_mode):
            try:
                child = open_dir(name, dir_fd=fd, expect=st)
            except (TreeError, OSError) as exc:
                self.errors.append({"path": path, "error": str(exc)})
                self._set_aside_fd(fd, name, path, "unopenable")
                return
            try:
                mode = stat.S_IMODE(st.st_mode)
                if mode & 0o700 != 0o700:
                    os.fchmod(child, mode | 0o700)     # 0555 module caches (design 5.7)
                self._empty(child, path)
            finally:
                os.close(child)
            try:
                os.rmdir(name, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    self._set_aside_fd(fd, name, path, "not-empty")
                else:
                    self.errors.append({"path": path, "error": exc.strerror or str(exc)})
                    self._set_aside_fd(fd, name, path, "rmdir-failed")
            return
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            return
        except OSError as exc:
            self.errors.append({"path": path, "error": exc.strerror or str(exc)})
            self._set_aside_fd(fd, name, path, "unlink-failed")
            return
        self.deleted += 1
        self.bytes += st.st_size

    def _conflict_dir(self, rel_parent: str) -> int:
        """An open descriptor for conflicts/<label>/<rel_parent>, created as needed."""
        self.conflicts.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = open_dir(self.conflicts)
        for part in [self.label, *[p for p in rel_parent.split("/") if p]]:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            try:
                child = open_dir(part, dir_fd=fd)
            except (NotADirectoryError, TreeError, OSError):
                os.close(fd)
                raise
            os.close(fd)
            fd = child
        return fd

    def _set_aside_fd(self, fd: int, name: str, path: str, reason: str) -> None:
        parent = path.rpartition("/")[0]
        try:
            target = self._conflict_dir(parent)
        except OSError as exc:
            self.errors.append({"path": path, "error": f"conflicts folder: {exc}"})
            self.kept.append({"path": path, "reason": reason, "where": "in place"})
            return
        try:
            final = name
            for n in range(1, 1000):
                if not _exists(final, target):
                    break
                final = f"{name}.late-{n}"
            os.rename(name, final, src_dir_fd=fd, dst_dir_fd=target)
            self.kept.append({"path": path, "reason": reason, "where": join(join(self.label, parent), final)})
        except OSError as exc:
            self.errors.append({"path": path, "error": exc.strerror or str(exc)})
            self.kept.append({"path": path, "reason": reason, "where": "in place"})
        finally:
            os.close(target)

    def _set_aside(self, parent: Path, name: str, path: str, reason: str) -> None:
        fd = open_dir(parent)
        try:
            self._set_aside_fd(fd, name, path, reason)
        finally:
            os.close(fd)


def _exists(name: str, dir_fd: int) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
