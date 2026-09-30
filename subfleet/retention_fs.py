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

import base64
import csv
import ctypes
import errno
import fcntl
import hashlib
import io
import json
import os
import posixpath
import re
import stat
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass
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


# --- what deleting a file frees ----------------------------------------------------

class _AttrList(ctypes.Structure):
    _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_uint16),
                ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32), ("dirattr", ctypes.c_uint32),
                ("fileattr", ctypes.c_uint32), ("forkattr", ctypes.c_uint32)]


_getattrlistat = getattr(_libc, "getattrlistat", None)
if _getattrlistat is not None:
    _getattrlistat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.POINTER(_AttrList), ctypes.c_void_p,
                               ctypes.c_size_t, ctypes.c_ulong]
    _getattrlistat.restype = ctypes.c_int
_ATTR_BIT_MAP_COUNT = 5
_ATTR_CMNEXT_PRIVATESIZE = 0x00000008       # <sys/attr.h>: bytes not shared with any clone (APFS)
_FSOPT_NOFOLLOW = 0x00000001
_FSOPT_ATTR_CMN_EXTENDED = 0x00000020


def private_bytes(parent_fd: int, name: str, st: os.stat_result) -> int:
    """Bytes deleting this file would give back: 0 if it has other links; on
    APFS, its blocks no clone shares (a file cloned into the archive, or a
    virtualenv cloned from uv's cache, gives back little or nothing); else its
    allocated blocks."""
    if st.st_nlink > 1:
        return 0
    if _getattrlistat is not None:
        request = _AttrList(_ATTR_BIT_MAP_COUNT, 0, 0, 0, 0, 0, _ATTR_CMNEXT_PRIVATESIZE)
        buffer = ctypes.create_string_buffer(32)
        if _getattrlistat(parent_fd, os.fsencode(name), ctypes.byref(request), buffer, len(buffer),
                          _FSOPT_NOFOLLOW | _FSOPT_ATTR_CMN_EXTENDED) == 0:
            length, value = struct.unpack_from("=Iq", buffer, 0)
            if length >= 12:
                return max(0, value)
    return st.st_blocks * 512


# --- regenerable output (d635 disk relief) -----------------------------------------------
#
# A retired job's virtualenv, node_modules, bytecode and tool caches are
# deleted with its tree, not archived, when their structure (never their name
# alone) says a tool wrote them, and then only what a rule proves the tool
# makes again (final review of e50716e8, N3):
#
# - a file an installed distribution's RECORD lists, whose sha256 matches;
# - bytecode with a magic number, beside its source;
# - a file inside an installed npm, pnpm or yarn package that was neither
#   written nor changed after the package manager's install marker;
# - a tool cache's own files, by the names the tool gives them.
#
# Only a tool's own top-level entries are looked into: live job trees held
# agents' logs, test reports, scripts, a git bundle and a bare repository at
# the top of tagged `.pytest_cache` and `.uv-cache` directories, and a project
# folder at the top of a `.venv`, and those are archived like any other file.
# Inside a tool's entries, everything no rule proves is archived too: an
# agent's patch to an installed package, data under a venv's `share/`, a
# notebook in a package directory, a `.pyc` that is not bytecode.

#: The CACHEDIR.TAG standard's signature (https://bford.info/cachedir/): the
#: tool that wrote the tag declares the directory a cache it can make again.
CACHEDIR_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"
BYTECODE = (".pyc", ".pyo")
VENV_DIRS = (".venv", "venv")
#: The files a package manager writes at the top of `node_modules` when an
#: install ends (npm, pnpm, yarn 1, yarn 2+).
NODE_MARKERS = (".package-lock.json", ".modules.yaml", ".yarn-integrity", ".yarn-state.yml")
#: pytest's own cache files (`cacheprovider`: `cache/lastfailed`, `cache/nodeids`,
#: `cache/stepwise` under `v/`).
PYTEST_OWN = frozenset({"v/cache/lastfailed", "v/cache/nodeids", "v/cache/stepwise"})
#: mypy's cache files under a Python version directory.
MYPY_OWN = (".data.json", ".meta.json", ".data.ff", ".meta.ff")
_DIGITS = re.compile(r"[0-9]+")
#: `installed_files`' mark for a vouching distribution's own RECORD (listed
#: without a hash, as the wheel format requires).
RECORD_SELF = "RECORD"
_RECORD_SHA256 = re.compile(r"sha256=([A-Za-z0-9_-]{43})=?")
#: PEP 610: an install from a URL on another machine (an index needs none).
_NETWORK_URL = re.compile(r"(?:https?|git\+https?|git\+ssh|ssh|git|hg\+https?|svn\+https?|bzr\+https?)://[^/]+/",
                          re.IGNORECASE)


@dataclass(frozen=True)
class Regenerable:
    """A directory a tool wrote (`regenerable`)."""
    kind: str
    #: The tool's own top-level entries, name -> (inode, type letter): only
    #: these are looked into. None: the whole directory (bytecode).
    children: dict[str, tuple[int, str]] | None
    #: Top-level names the tool did not write: archived byte for byte.
    extra: tuple[str, ...] = ()


@dataclass(frozen=True)
class Verify:
    """`RegenerableWalk.classify`'s answer for a file a RECORD lists: it is
    regenerable only if its bytes hash to `sha256` (hex); the caller reads it
    and says so with `confirm`."""
    sha256: str


@dataclass(frozen=True)
class _Layout:
    """What a tool writes at the top level of its directory."""
    kind: str
    #: name -> the types it may have ("f" file, "d" directory, "l" link)
    fixed: dict[str, str]
    #: names the tool makes up (a version, a cache bucket, a temporary directory)
    patterns: tuple[tuple[re.Pattern[str], str], ...] = ()
    #: a directory that is the tool's own by its structure
    child_dir: Callable[[int, str, os.stat_result], bool] | None = None
    #: links the tool makes (a package manager's), not starting with "."
    links: bool = False


def _venv_marker(parent_fd: int, name: str, st: os.stat_result) -> bool:
    """PEP 405: a virtual environment holds a `pyvenv.cfg` naming `home`."""
    text = _marker(parent_fd, name, st, "pyvenv.cfg")
    return text is not None and any(
        "=" in line and line.partition("=")[0].strip() == "home"
        for line in text.decode("utf-8", "replace").splitlines())


def _package_or_scope(parent_fd: int, name: str, st: os.stat_result) -> bool:
    """A package directory (holding a regular `package.json`), or an `@scope`
    directory of package directories and links."""
    fd = open_dir(name, dir_fd=parent_fd, expect=st)
    try:
        if not name.startswith("@"):
            return _regular(fd, "package.json")
        names = os.listdir(fd)
        for child in names:
            cst = os.stat(child, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(cst.st_mode):
                continue
            if not stat.S_ISDIR(cst.st_mode):
                return False
            inner = open_dir(child, dir_fd=fd, expect=cst)
            try:
                if not _regular(inner, "package.json"):
                    return False
            finally:
                os.close(inner)
        return bool(names)
    finally:
        os.close(fd)


_VENV = _Layout("venv", {
    "pyvenv.cfg": "f", "CACHEDIR.TAG": "f", ".gitignore": "f", ".lock": "f",
    "bin": "d", "lib": "d", "lib64": "dl", "include": "d", "share": "d", "etc": "d", "man": "d",
    "Lib": "d", "Scripts": "d", "Include": "d"})
_NODE_MODULES = _Layout("node_modules", {
    ".bin": "d", ".pnpm": "d", ".package-lock.json": "f", ".modules.yaml": "f", ".yarn-integrity": "f",
    ".yarn-state.yml": "f"}, child_dir=_package_or_scope, links=True)
#: Tool caches, each identified by its CACHEDIR.TAG and then by its layout.
_CACHES = {
    ".pytest_cache": _Layout("pytest_cache", {"CACHEDIR.TAG": "f", "README.md": "f", ".gitignore": "f", "v": "d"}),
    ".ruff_cache": _Layout("ruff_cache", {"CACHEDIR.TAG": "f", ".gitignore": "f"},
                           ((re.compile(r"\d+\.\d+\.\d+"), "d"),)),
    ".mypy_cache": _Layout("mypy_cache", {"CACHEDIR.TAG": "f", ".gitignore": "f", "missing_stubs": "f"},
                           ((re.compile(r"\d+\.\d+"), "d"),)),
    ".uv-cache": _Layout("uv_cache", {"CACHEDIR.TAG": "f", ".gitignore": "f", ".lock": "f"},
                         ((re.compile(r"[a-z]+(?:-[a-z]+)*-v\d+"), "d"),
                          (re.compile(r"\.tmp[0-9A-Za-z]{6}"), "df"))),
    ".tox": _Layout("tox", {"CACHEDIR.TAG": "f", ".gitignore": "f"}, child_dir=_venv_marker),
}


def regenerable(parent_fd: int, name: str, st: os.stat_result) -> Regenerable | None:
    """The regenerable output a directory is, identified by its structure,
    never by its name alone (d635 disk relief), or None:

    - ``venv``: ``.venv`` or ``venv`` holding a regular ``pyvenv.cfg`` with a
      ``home`` key;
    - ``node_modules``: ``node_modules`` beside a regular ``package.json``;
    - ``pycache``: a ``__pycache__`` directory (each file in it is judged by
      itself, `bytecode`);
    - a tool cache: ``.pytest_cache``, ``.ruff_cache``, ``.mypy_cache``,
      ``.uv-cache`` or ``.tox`` holding a regular ``CACHEDIR.TAG`` that starts
      with the standard's signature.

    Of every kind but bytecode, only the top-level entries the tool writes
    (its layout: fixed names and types, the names it makes up, and for
    ``node_modules`` and ``.tox`` directories whose structure says they are
    packages or environments) are the tool's; anything else is `extra`.
    Inside the tool's entries `RegenerableWalk` judges each file by its rule.
    `build/`, `dist/`, `target/` and `.cache/` are never regenerable here: work
    has been found in them. Whether git ignores the directory and tracks
    nothing in it, and whether it sits in a repository, is `RegenerableWalk`'s
    to check. Any error answers None: when in doubt, the bytes are archived.
    """
    if not stat.S_ISDIR(st.st_mode):
        return None
    try:
        if name == "__pycache__":
            return Regenerable("pycache", None)
        if name in VENV_DIRS:
            return _layout(parent_fd, name, st, _VENV) if _venv_marker(parent_fd, name, st) else None
        if name == "node_modules":
            return _layout(parent_fd, name, st, _NODE_MODULES) if _regular(parent_fd, "package.json") else None
        layout = _CACHES.get(name)
        if layout is not None:
            text = _marker(parent_fd, name, st, "CACHEDIR.TAG")
            if text is not None and text.startswith(CACHEDIR_SIGNATURE):
                return _layout(parent_fd, name, st, layout)
    except (OSError, TreeError):
        return None
    return None


def _layout(parent_fd: int, name: str, st: os.stat_result, layout: _Layout) -> Regenerable | None:
    fd = open_dir(name, dir_fd=parent_fd, expect=st)
    children: dict[str, tuple[int, str]] = {}
    extra: list[str] = []
    try:
        for child in _names(fd):
            cst = os.stat(child, dir_fd=fd, follow_symlinks=False)
            t = kind(cst.st_mode)
            if _belongs(layout, fd, child, cst, t):
                children[child] = (cst.st_ino, t)
            else:
                extra.append(child)
    finally:
        os.close(fd)
    return Regenerable(layout.kind, children, tuple(extra)) if children else None


def _belongs(layout: _Layout, fd: int, name: str, st: os.stat_result, t: str) -> bool:
    allowed = layout.fixed.get(name)
    if allowed is not None:
        return t in allowed
    for pattern, types in layout.patterns:
        if pattern.fullmatch(name):
            return t in types
    if t == "d" and layout.child_dir is not None:
        try:
            return layout.child_dir(fd, name, st)
        except (OSError, TreeError):
            return False
    return t == "l" and layout.links and not name.startswith(".")


# --- what proves a file regenerable ------------------------------------------------------

def bytecode(parent_fd: int, name: str, st: os.stat_result) -> bool:
    """A `.pyc` or `.pyo` in the `__pycache__` open as `parent_fd` that Python
    makes again: its first four bytes are a magic number (two bytes, then CR
    LF), and its source, `<module>.py`, is a regular file beside the
    `__pycache__` folder. Anything else there (data named `.pyc`, bytecode
    whose source is gone) is archived."""
    if not stat.S_ISREG(st.st_mode) or not name.endswith(BYTECODE):
        return False
    module = name.split(".", 1)[0]
    if not module:
        return False
    try:
        source = os.stat(f"../{module}.py", dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(source.st_mode):
            return False
        fd = os.open(name, O_FILE, dir_fd=parent_fd)
    except OSError:
        return False
    try:
        now = os.fstat(fd)
        head = os.pread(fd, 4, 0)
    except OSError:
        return False
    finally:
        os.close(fd)
    return (now.st_dev, now.st_ino) == (st.st_dev, st.st_ino) and len(head) == 4 and head[2:] == b"\r\n"


def installed_files(venv_fd: int, limit: int = 64 << 20) -> dict[str, tuple[str, int | None]]:
    """Every file an installed distribution vouches for, venv-relative path ->
    (sha256 hex, size or None): the rows of each `*.dist-info/RECORD` in the
    environment's `site-packages` that carry a sha256 (paths are relative to
    `site-packages`, `../../../bin/tool` included; none may leave the
    environment). A distribution installed from a local path (PEP 610's
    `direct_url.json` naming no network URL: an editable install, a local
    wheel or directory) vouches for nothing: its source may be the only copy.
    A path two distributions list with different hashes is left out. Errors
    leave a distribution out: when in doubt, the bytes are archived."""
    found: dict[str, tuple[str, int | None] | None] = {}
    for base in _site_packages(venv_fd):
        try:
            sp = _open_path(venv_fd, base)
        except (OSError, TreeError):
            continue
        try:
            for dist in _names(sp):
                if not dist.endswith(".dist-info"):
                    continue
                try:
                    fd = open_dir(dist, dir_fd=sp)
                except (OSError, TreeError):
                    continue
                try:
                    if not _from_the_network(fd):
                        continue
                    record = _small(fd, "RECORD", limit)
                finally:
                    os.close(fd)
                if record is None:
                    continue
                found[f"{base}/{dist}/RECORD"] = (RECORD_SELF, None)
                for row in csv.reader(io.StringIO(record.decode("utf-8", "replace"))):
                    if len(row) < 2 or not row[0] or row[0].startswith("/"):
                        continue
                    match = _RECORD_SHA256.fullmatch(row[1])
                    path = posixpath.normpath(posixpath.join(base, row[0]))
                    if match is None or path == ".." or path.startswith("../"):
                        continue
                    if path == f"{base}/{dist}/RECORD":
                        continue
                    digest = base64.urlsafe_b64decode(match.group(1) + "=").hex()
                    size = int(row[2]) if len(row) > 2 and _DIGITS.fullmatch(row[2]) else None
                    vouched = (digest, size)
                    found[path] = vouched if found.get(path, vouched) == vouched else None
        finally:
            os.close(sp)
    return {path: v for path, v in found.items() if v is not None}


def record_complete(dist_fd: int, installed: dict[str, tuple[str, int | None]], record: str) -> bool:
    """Whether a distribution's `RECORD` (at venv-relative path `record`, in
    the `*.dist-info` directory open as `dist_fd`) is the installer's alone:
    every other entry of that directory is a directory or a regular file the
    RECORD lists, and each such file hashes as listed. Then the whole
    directory is the installer's, and dropping it with the rest leaves no
    distribution half there on restore (pip and uv read a `*.dist-info`
    holding only a RECORD as a broken distribution)."""
    prefix = record.rpartition("/")[0]
    try:
        for rel, st, parent, name in walk(dist_fd):
            if not rel or rel == "RECORD" or stat.S_ISDIR(st.st_mode):
                continue
            vouched = installed.get(f"{prefix}/{rel}")
            if not stat.S_ISREG(st.st_mode) or vouched is None or vouched[0] == RECORD_SELF \
                    or vouched[1] not in (None, st.st_size) or name is None:
                return False
            fd = os.open(name, O_FILE, dir_fd=parent)
            try:
                digest, _ = read_hashes(fd, os.fstat(fd).st_size, None)
            finally:
                os.close(fd)
            if digest != vouched[0]:
                return False
    except (OSError, TreeError):
        return False
    return True


def _site_packages(venv_fd: int) -> list[str]:
    """`lib/python3.X/site-packages` (and PyPy's, and Windows' `Lib/site-packages`),
    real directories only, spelled as on disk."""
    out = []
    try:
        top = _names(venv_fd)
    except OSError:
        return out
    for lib in (n for n in top if n in ("lib", "Lib")):
        try:
            fd = open_dir(lib, dir_fd=venv_fd)
        except (OSError, TreeError):
            continue
        try:
            for name in _names(fd):
                if name == "site-packages" and _is_dir(fd, name):
                    out.append(f"{lib}/{name}")
                elif name.startswith(("python", "pypy")) and _is_dir(fd, name):
                    inner = open_dir(name, dir_fd=fd)
                    try:
                        if _is_dir(inner, "site-packages"):
                            out.append(f"{lib}/{name}/site-packages")
                    finally:
                        os.close(inner)
        except (OSError, TreeError):
            continue
        finally:
            os.close(fd)
    return out


def _from_the_network(dist_fd: int) -> bool:
    """Whether a distribution came from an index (no `direct_url.json`) or a
    URL on another machine (PEP 610)."""
    try:
        os.stat("direct_url.json", dir_fd=dist_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    data = _small(dist_fd, "direct_url.json", 1 << 20)
    try:
        url = json.loads(data).get("url") if data is not None else None
    except (ValueError, AttributeError):
        return False
    return isinstance(url, str) and _NETWORK_URL.match(url) is not None


def node_installed_at(node_modules_fd: int) -> int | None:
    """When the package manager last finished an install into this
    `node_modules` (ns): the earliest mtime or ctime of the markers it writes
    at the end (`NODE_MARKERS`); None without one."""
    times = []
    for name in NODE_MARKERS:
        try:
            st = os.stat(name, dir_fd=node_modules_fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            times.append(min(st.st_mtime_ns, st.st_ctime_ns))
    return min(times) if times else None


class NotRegenerable(Exception):
    """A regenerable root holds a repository (a `.git`): walk again with it
    archived."""

    def __init__(self, root: str):
        super().__init__(root)
        self.root = root


@dataclass
class _Rule:
    """How the files under one tool entry are judged."""
    kind: str
    #: the directory paths are judged relative to (a venv, an environment, a cache)
    base: str
    #: installed_files() of a venv or a tox environment
    installed: dict[str, tuple[str, int | None]] | None = None
    #: node_installed_at() of a node_modules
    installed_at: int | None = None


class RegenerableWalk:
    """Follows one worktree's pre-order `walk` and marks its regenerable output,
    for the archive and the survey alike (d635 disk relief).

    A directory `regenerable` identifies is used only when `clear(rel)` (git
    tracks nothing at, in or above it and ignores all of it) and neither it nor
    a directory between it and the tree's root holds a `.git` (a nested
    repository answers for its own files). Its tool's entries are then
    *roots*, and each file under a root is regenerable only when its kind's
    rule proves it (final review of e50716e8, N3):

    - a virtualenv, or a tox environment: a file an installed distribution's
      RECORD lists (`installed_files`) whose sha256 matches (`classify`
      answers `Verify`; the caller reads the file and calls `confirm`), and
      bytecode (`bytecode`);
    - ``__pycache__``: bytecode;
    - ``node_modules``: a regular file inside a package, neither modified nor
      changed (mtime, ctime) after the install marker (`node_installed_at`);
      none without a marker;
    - ``.pytest_cache``: `PYTEST_OWN`; ``.ruff_cache``: files named by digits
      in a version directory; ``.mypy_cache``: `MYPY_OWN` files in a version
      directory; ``.uv-cache``: none.

    Links are always archived. A directory under a root is dropped when it
    held something and everything in it was (`finish`); the tool's directory
    itself stays, except a ``__pycache__``. A `.git` met under a root raises
    `NotRegenerable`; the caller walks again with that root in `denied`, so
    it is archived. `records` has one record per directory: its path, kind,
    the paths dropped whose parent was not (`roots`), the top-level names not
    wholly dropped (`kept`), and the entries, bytes (`st_size` of regular
    files) and `freed_disk_bytes` (`private_bytes`) dropped.
    """

    def __init__(self, root_fd: int, clear: Callable[[str], bool], denied: set[str] | frozenset[str] = frozenset()):
        self.root_fd = root_fd
        self.clear = clear
        self.denied = denied
        self.records: list[dict[str, Any]] = []
        self._containers: dict[str, tuple[Regenerable, dict[str, Any], _Rule]] = {}
        self._root: str | None = None
        self._rule: _Rule | None = None
        self._record: dict[str, Any] | None = None
        #: [path, is a directory, dropped, record] for every entry under a root
        self._seen: list[list[Any]] = []
        self._finished: set[str] | None = None

    def classify(self, rel: str, st: os.stat_result, parent: int, name: str | None) -> bool | Verify:
        """Whether this entry of the walk is regenerable (listed, not stored):
        True, False, or `Verify` for a file whose hash decides."""
        if self._root is not None:
            if rel.startswith(self._root + "/"):
                if name is not None and name.lower() == ".git":
                    raise NotRegenerable(self._root)
                return self._judge(rel, st, parent, name)
            self._root = self._rule = self._record = None
        if not rel or name is None:
            return False
        found = self._containers.get(rel.rpartition("/")[0])
        if found is not None:
            spec, record, rule = found
            if spec.children is not None and spec.children.get(name) == (st.st_ino, kind(st.st_mode)) \
                    and rel not in self.denied:
                return self._start(rel, record, rule, st, parent, name)
        if not stat.S_ISDIR(st.st_mode) or rel in self.denied:
            return False
        spec = regenerable(parent, name, st)
        if spec is None or not self.clear(rel) or self._in_repository(rel):
            return False
        record: dict[str, Any] = {"p": rel, "kind": spec.kind, "roots": [], "kept": list(spec.extra),
                                  "entries": 0, "bytes": 0, "freed_disk_bytes": 0}
        try:
            rule = self._rule_for(spec.kind, rel, parent, name, st)
        except (OSError, TreeError):
            return False
        self.records.append(record)
        if spec.children is None:
            return self._start(rel, record, rule, st, parent, name)
        self._containers[rel] = (spec, record, rule)
        return False

    def confirm(self, rel: str, st: os.stat_result, parent: int, name: str) -> None:
        """The file `classify` answered `Verify` for hashed as its RECORD says."""
        item = self._seen[-1]
        assert item[0] == rel and not item[2]
        item[2] = True
        self._drop(item[3], st, parent, name)

    def finish(self) -> set[str]:
        """After the walk: the directories under a root dropped because each
        held something and everything in it was dropped. Also fills each
        record's `roots` and `kept`."""
        if self._finished is not None:
            return self._finished
        dirs: set[str] = set()
        inside: dict[str, list[bool]] = {}          # directory -> [held something, all of it dropped]
        for item in reversed(self._seen):
            path, is_dir, dropped, record = item
            if is_dir:
                held, every = inside.pop(path, [False, True])
                if held and every:
                    item[2] = dropped = True
                    dirs.add(path)
                    record["entries"] += 1
            state = inside.setdefault(path.rpartition("/")[0], [False, True])
            state[0] = True
            state[1] = state[1] and dropped
        gone = {item[0] for item in self._seen if item[2]}
        for item in self._seen:
            if item[2] and item[0].rpartition("/")[0] not in gone:
                item[3]["roots"].append(item[0])
        for spec, record, _ in self._containers.values():
            if spec.children is not None:
                whole = {r.rpartition("/")[2] for r in record["roots"] if r.rpartition("/")[0] == record["p"]}
                record["kept"] = sorted(set(record["kept"]) | (set(spec.children) - whole))
        self._finished = dirs
        return dirs

    def summary(self) -> list[dict[str, Any]]:
        """The records of directories that dropped something."""
        self.finish()
        return [record for record in self.records if record["roots"]]

    def _rule_for(self, kind_: str, rel: str, parent: int, name: str, st: os.stat_result) -> _Rule:
        if kind_ not in ("venv", "node_modules"):
            return _Rule(kind_, rel)
        fd = open_dir(name, dir_fd=parent, expect=st)
        try:
            if kind_ == "venv":
                return _Rule(kind_, rel, installed=installed_files(fd))
            return _Rule(kind_, rel, installed_at=node_installed_at(fd))
        finally:
            os.close(fd)

    def _start(self, rel: str, record: dict[str, Any], rule: _Rule, st: os.stat_result, parent: int,
               name: str) -> bool:
        self._record = record
        self._rule = rule
        if rule.kind == "tox" and stat.S_ISDIR(st.st_mode):
            # Each environment is a virtualenv: its own distributions' RECORDs.
            try:
                fd = open_dir(name, dir_fd=parent, expect=st)
                try:
                    self._rule = _Rule("venv", rel, installed=installed_files(fd))
                finally:
                    os.close(fd)
            except (OSError, TreeError):
                self._rule = _Rule("tox", rel)
        dropped = self._judge(rel, st, parent, name)     # a directory is only noted, for `finish`
        self._root = rel if stat.S_ISDIR(st.st_mode) else None
        if self._root is None:
            self._rule = self._record = None
        return dropped

    def _judge(self, rel: str, st: os.stat_result, parent: int, name: str | None) -> bool | Verify:
        record, rule = self._record, self._rule
        assert record is not None and rule is not None
        is_dir = stat.S_ISDIR(st.st_mode)
        item = [rel, is_dir, False, record]
        self._seen.append(item)
        if is_dir or name is None or not stat.S_ISREG(st.st_mode):
            return False                              # directories are decided by `finish`; links are archived
        verdict: bool | Verify = False
        inner = rel[len(rule.base) + 1:] if rel.startswith(rule.base + "/") else ""
        parts = inner.split("/")
        if rule.kind == "venv":
            installed = rule.installed or {}
            vouched = installed.get(inner)
            if vouched is not None and vouched[0] == RECORD_SELF:
                verdict = record_complete(parent, installed, inner)
            elif vouched is not None and vouched[1] in (None, st.st_size):
                return Verify(vouched[0])
            else:
                verdict = len(parts) > 1 and parts[-2] == "__pycache__" and bytecode(parent, name, st)
        elif rule.kind == "pycache":
            verdict = bool(inner) and "/" not in inner and bytecode(parent, name, st)
        elif rule.kind == "node_modules":
            at = rule.installed_at
            verdict = (at is not None and len(parts) > 1 and st.st_mtime_ns <= at and st.st_ctime_ns <= at)
        elif rule.kind == "pytest_cache":
            verdict = inner in PYTEST_OWN
        elif rule.kind == "ruff_cache":
            verdict = len(parts) == 2 and _DIGITS.fullmatch(parts[1]) is not None
        elif rule.kind == "mypy_cache":
            verdict = len(parts) > 1 and (name.endswith(MYPY_OWN) or name == "@plugins_snapshot.json")
        if verdict:
            item[2] = True
            self._drop(record, st, parent, name)
        return bool(verdict)

    def _drop(self, record: dict[str, Any], st: os.stat_result, parent: int, name: str) -> None:
        record["entries"] += 1
        record["bytes"] += st.st_size
        record["freed_disk_bytes"] += private_bytes(parent, name, st)

    def _in_repository(self, rel: str) -> bool:
        """Whether `rel` or a directory between it and the tree's root holds a
        `.git` (the root's own is the job's repository). Fails closed."""
        opened: list[int] = []
        fd = self.root_fd
        try:
            for part in rel.split("/"):
                fd = open_dir(part, dir_fd=fd)
                opened.append(fd)
                if _exists(".git", fd):
                    return True
            return False
        except (OSError, TreeError):
            return True
        finally:
            for x in opened:
                os.close(x)


def _is_dir(dir_fd: int, name: str) -> bool:
    try:
        return stat.S_ISDIR(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _open_path(dir_fd: int, path: str) -> int:
    """Open a relative path one real directory at a time (no link followed)."""
    fd = dir_fd
    for part in path.split("/"):
        child = open_dir(part, dir_fd=fd)
        if fd != dir_fd:
            os.close(fd)
        fd = child
    return fd


def _small(dir_fd: int, name: str, limit: int) -> bytes | None:
    """A regular file's bytes, or None when it is missing, not regular or larger than `limit`."""
    try:
        fd = os.open(name, O_FILE, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        chunks = []
        while chunk := os.read(fd, CHUNK):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _regular(dir_fd: int, name: str) -> bool:
    try:
        return stat.S_ISREG(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _marker(parent_fd: int, directory: str, st: os.stat_result, name: str, limit: int = 65536) -> bytes | None:
    """The first `limit` bytes of a regular file `name` inside `directory`, or None."""
    fd = open_dir(directory, dir_fd=parent_fd, expect=st)
    try:
        try:
            handle = os.open(name, O_FILE, dir_fd=fd)
        except OSError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(handle).st_mode):
                return None
            return os.read(handle, limit)
        finally:
            os.close(handle)
    finally:
        os.close(fd)


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
