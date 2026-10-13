"""C-6.6a: allocate a job's worktree from clones of the caller's checkout.

`git worktree add` writes every tracked file, so each worktree of a large
repository costs its full size on disk: about 5 GB for
TheAxiomFoundation/axiom-roadmap-dashboard, whose sealed evaluation ledger and
plan records are tracked. On APFS a file the caller's checkout already holds
can instead be an APFS clone: a regular file with its own inode, mode and
metadata that shares its blocks with the original until either side writes.

`add_worktree` makes the same worktree `git worktree add --detach` makes, in
the same steps git takes, with one step put in the middle:

1. `git worktree add --no-checkout --detach PATH COMMIT`: the administrative
   directory, HEAD, and any sparse-checkout and worktree config git copies
   from the caller's worktree; no files.
2. Clone stage (best effort): `read-tree` fills the index; each tracked regular
   file whose blob the caller's checkout records at the same path, and whose
   checkout git writes as the blob's bytes unchanged (`_writes_blob_bytes`), is
   cloned from that checkout into PATH with the mode git would create it with;
   `update-index --refresh` then hashes every clone against its blob. A clone
   whose bytes are not the blob's stays out of date in the index.
3. `git reset --hard --no-recurse-submodules`: what `git worktree add` itself
   runs to check a worktree out. It keeps every entry the refresh found up to
   date and writes all the others, so a clone the refresh rejected, and every
   file the clone stage skipped, is written by git.
4. `git status` must be empty. If it is not, the worktree is removed and a
   plain `git worktree add` makes it instead.
5. The `post-checkout` hook, as `git worktree add` runs it (null old head,
   new head, flag 1), once.

What a clone carries that git's own write would not is ruled out before the
clone is kept: it must be a regular file of the blob's size with no file flags,
extended attributes or ACL. A clone keeps its source's modification time, which
git never reads for content. Anything the clone stage cannot do (no clone
support, another volume, a sparse checkout, a timeout, an error) leaves fewer
or no clones and git writes the rest: the stage never decides what the
worktree contains, git's refresh and reset do.

Off with `SUBFLEET_WORKTREE_CLONE=0` in the daemon's environment.
"""

from __future__ import annotations

import ctypes
import dataclasses
import errno
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

from . import retention_fs as rfs

#: `SUBFLEET_WORKTREE_CLONE=0` in the daemon's environment turns the clone stage off.
CLONE_ENV = "SUBFLEET_WORKTREE_CLONE"
#: The clone stage gets at most this share of the allocation's cap; git's own
#: checkout (step 3) has the rest, so a slow stage never costs the allocation.
CLONE_SHARE = 0.5
NULL_OID = {"sha1": "0" * 40, "sha256": "0" * 64}
#: Errors that say this volume (or this pair of volumes) cannot clone at all.
NO_CLONES = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.ENOSYS, errno.EINVAL})
#: `check-attr` names read for `_writes_blob_bytes`.
ATTRIBUTES = ("text", "eol", "crlf", "ident", "filter", "working-tree-encoding")
_REGULAR_MODES = {"100644": 0o666, "100755": 0o777}

_libc = ctypes.CDLL(None, use_errno=True)
_flistxattr = getattr(_libc, "flistxattr", None)
if _flistxattr is not None:
    _flistxattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
    _flistxattr.restype = ctypes.c_ssize_t
_acl_get_fd_np = getattr(_libc, "acl_get_fd_np", None)
_acl_free = getattr(_libc, "acl_free", None)
if _acl_get_fd_np is not None and _acl_free is not None:
    _acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    _acl_get_fd_np.restype = ctypes.c_void_p
    _acl_free.argtypes = [ctypes.c_void_p]
    _acl_free.restype = ctypes.c_int
ACL_TYPE_EXTENDED = 0x00000100


@dataclasses.dataclass
class Allocation:
    """What `add_worktree` did. `result` is the git call the caller reads as
    `git worktree add`'s: return code 0 when the worktree is ready."""
    result: subprocess.CompletedProcess
    method: str = "plain"            # "clone" when the clone stage ran, else "plain"
    candidates: int = 0              # tracked regular files the caller's checkout records with the same blob
    cloned: int = 0                  # clones kept after the refresh
    cloned_bytes: int = 0
    skipped: dict[str, int] = dataclasses.field(default_factory=dict)
    reason: str | None = None        # why the clone stage stopped early, or did not run
    seconds: float = 0.0
    cloned_paths: list[str] = dataclasses.field(default_factory=list, repr=False)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1

    def summary(self) -> str:
        shared = f"{self.cloned} of {self.candidates} candidate files cloned ({self.cloned_bytes / 1e9:.2f} GB shared)"
        extra = f"; stopped: {self.reason}" if self.reason else ""
        skipped = "".join(f", {n} {why}" for why, n in sorted(self.skipped.items()))
        return f"{self.method}: {shared}{skipped}{extra} in {self.seconds:.1f} s"


class _Deadline:
    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds

    def left(self) -> float:
        return self.end - time.monotonic()


def enabled() -> bool:
    return os.environ.get(CLONE_ENV, "1") != "0" and rfs._fclonefileat is not None


def add_worktree(repository: str, path: str, commit: str, *, env: dict[str, str],
                 timeout_s: float, clone: bool | None = None) -> Allocation:
    """Make `path` a detached worktree of `repository` at `commit` (C-6.6a).

    Raises what `subprocess.run` raises for the add (a `TimeoutExpired` names
    the add and the whole allocation's cap); otherwise returns an `Allocation`
    whose `result` is non-zero when git refused, as `git worktree add` would.
    """
    started = time.monotonic()
    add = ["git", "-C", repository, "worktree", "add", "--detach", path, commit]
    if not (enabled() if clone is None else clone):
        allocation = Allocation(_run_text(add, env, timeout_s), reason="clone stage off")
        allocation.seconds = time.monotonic() - started
        return allocation
    deadline = _Deadline(timeout_s)
    no_checkout = ["git", "-C", repository, "worktree", "add", "--no-checkout", "--detach", path, commit]
    try:
        result = _run_text(no_checkout, env, deadline.left())
        if result.returncode:
            return Allocation(result, reason="worktree add refused")
        allocation = Allocation(result, method="clone")
        try:
            _clone_stage(allocation, repository, path, env, deadline, timeout_s * CLONE_SHARE)
        except subprocess.TimeoutExpired:
            allocation.reason = allocation.reason or "clone stage reached its share of the cap"
            _clear_index_lock(path, env, deadline)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            allocation.reason = f"clone stage failed: {type(exc).__name__}: {exc}"
        reset = _run_text(["git", "-C", path, "reset", "--hard", "--no-recurse-submodules", "--quiet"],
                          env, deadline.left())
        if reset.returncode:
            allocation.result = reset
            return allocation
        # No fsmonitor: a check `git worktree add` does not make must not start its daemon.
        status = _run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", path, "status", "--porcelain=v1", "-z",
                       "--untracked-files=all", "--ignore-submodules=none"], env, deadline.left())
        if status.returncode or status.stdout:
            # Not what `git worktree add` gives (or git gives it that way too):
            # either way the plain add decides, never the clone stage.
            _remove(repository, path, env, max(1.0, deadline.left()))
            plain = Allocation(_run_text(add, env, max(1.0, deadline.left())),
                               reason="status after the clone stage was not clean; plain add used")
            plain.skipped = allocation.skipped
            plain.seconds = time.monotonic() - started
            return plain
        head = _run_text(["git", "-C", path, "rev-parse", "--verify", "HEAD"], env, deadline.left())
        object_format = _run_text(["git", "-C", path, "rev-parse", "--show-object-format"], env, deadline.left())
        if head.returncode or object_format.returncode:
            allocation.result = head if head.returncode else object_format
            return allocation
        hook = _run_text(["git", "-C", path, "hook", "run", "--ignore-missing", "post-checkout", "--",
                          NULL_OID.get(object_format.stdout.strip(), NULL_OID["sha1"]), head.stdout.strip(), "1"],
                         env, deadline.left())
        allocation.result = hook if hook.returncode else result
        allocation.seconds = time.monotonic() - started
        return allocation
    except subprocess.TimeoutExpired as exc:
        # One cap covers the whole allocation, as it covered the one add.
        raise subprocess.TimeoutExpired(add, timeout_s, output=exc.output, stderr=exc.stderr) from exc


def _clone_stage(allocation: Allocation, repository: str, path: str, env: dict[str, str],
                 deadline: _Deadline, share_s: float) -> None:
    stage = _Deadline(min(share_s, deadline.left()))
    if _git_out(path, env, stage, "config", "--bool", "core.sparseCheckout", check=False).strip() == b"true":
        allocation.reason = "sparse checkout"
        return
    # Under either, git's checkout marks index entries (assume-unchanged, or
    # fsmonitor-valid) that an entry a refresh verified would not carry.
    if _git_out(path, env, stage, "config", "--bool", "core.ignoreStat", check=False).strip() == b"true":
        allocation.reason = "core.ignoreStat"
        return
    fsmonitor = _git_out(path, env, stage, "config", "--get", "core.fsmonitor", check=False).strip()
    if fsmonitor and _git_out(path, env, stage, "config", "--type=bool", "--get", "core.fsmonitor",
                              check=False).strip() != b"false":
        allocation.reason = "core.fsmonitor"
        return
    source = _git_out(repository, env, stage, "rev-parse", "--show-toplevel", check=False).rstrip(b"\n")
    if not source:
        allocation.reason = "the caller's directory is in no worktree"
        return
    source_top = os.path.realpath(os.fsdecode(source))
    if os.stat(source_top).st_dev != os.stat(path).st_dev:
        allocation.reason = "the caller's checkout is on another volume"
        return
    _git_out(path, env, stage, "read-tree", "HEAD")
    targets = _tree_files(_git_out(path, env, stage, "ls-tree", "-r", "-l", "-z", "--full-tree", "HEAD"))
    recorded = _index_blobs(_git_out(source_top, env, stage, "ls-files", "-s", "-z"))
    candidates = [t for t in targets if recorded.get(t[0]) == t[1]]
    allocation.candidates = len(candidates)
    if not candidates:
        allocation.reason = "the caller's checkout records none of these blobs"
        return
    autocrlf = _autocrlf(path, env, stage)
    attributes = _attributes(path, env, stage, [c[0] for c in candidates])
    clones = []
    umask = _umask(path)
    folders: dict[str, int] = {}
    try:
        for rel, oid, size, mode in candidates:
            if stage.left() <= 0:
                allocation.reason = "clone stage reached its share of the cap"
                break
            if not _writes_blob_bytes(attributes.get(rel, {}), autocrlf):
                allocation.skip("converted by git on checkout")
                continue
            outcome = _clone_one(source_top, path, rel, size, mode & ~umask, folders)
            if outcome is None:
                allocation.reason = "this volume cannot clone"
                break
            if outcome != "cloned":
                allocation.skip(outcome)
                continue
            clones.append((rel, size))
    finally:
        for fd in folders.values():
            os.close(fd)
    if not clones:
        return
    # Hashes every clone against its blob; a clone that differs stays out of
    # date, and the reset that follows writes it. Exit 1 says some did.
    _run(["git", "-C", path, "update-index", "-q", "--ignore-missing", "--refresh"], env, stage.left())
    stale = set(os.fsdecode(p) for p in
                _git_out(path, env, deadline, "diff-files", "--name-only", "-z").split(b"\0") if p)
    for rel, size in clones:
        if rel in stale:
            allocation.skip("not the blob's bytes")
        else:
            allocation.cloned += 1
            allocation.cloned_bytes += size
            allocation.cloned_paths.append(rel)


def _clone_one(source_top: str, path: str, rel: str, size: int, mode: int,
               folders: dict[str, int]) -> str | None:
    """Clone `source_top/rel` to `path/rel`; "cloned", a skip reason, or None
    when the volume cannot clone. A kept clone is a regular file of `size`
    bytes, `mode`, and no flags, extended attributes or ACL."""
    try:
        src = os.open(os.path.join(source_top, rel), rfs.O_FILE)
    except OSError:
        return "missing or not a file in the caller's checkout"
    try:
        st = os.fstat(src)
        if not stat.S_ISREG(st.st_mode) or st.st_size != size:
            return "changed in the caller's checkout"
        if getattr(st, "st_flags", 0):
            return "file flags in the caller's checkout"
        parent, _, name = rel.rpartition("/")
        folder = _folder(path, parent, folders)
        if rfs._fclonefileat(src, folder, os.fsencode(name), rfs.CLONE_NOFOLLOW) != 0:
            code = ctypes.get_errno()
            if code in NO_CLONES:
                return None
            return f"clone failed ({errno.errorcode.get(code, code)})"
    finally:
        os.close(src)
    clone = os.open(name, rfs.O_FILE, dir_fd=folder)
    keep = False
    try:
        cst = os.fstat(clone)
        if (stat.S_ISREG(cst.st_mode) and cst.st_size == size and not getattr(cst, "st_flags", 0)
                and not _has_xattrs(clone) and not _has_acl(clone)):
            os.fchmod(clone, mode)
            keep = True
    finally:
        os.close(clone)
    if not keep:
        os.unlink(name, dir_fd=folder)
        return "metadata git would not write"
    return "cloned"


def _folder(path: str, parent: str, folders: dict[str, int]) -> int:
    """A descriptor for `path/parent`, made as git makes directories (0777
    under the umask). `path` is new and only this allocation writes in it, so
    nothing on the way is a link."""
    fd = folders.get(parent)
    if fd is None:
        os.makedirs(os.path.join(path, parent), mode=0o777, exist_ok=True)
        if len(folders) >= 64:
            for old in folders.values():
                os.close(old)
            folders.clear()
        fd = os.open(os.path.join(path, parent) if parent else path, rfs.O_DIR)
        folders[parent] = fd
    return fd


def _writes_blob_bytes(attributes: dict[str, str], autocrlf: bool) -> bool:
    """Whether git checks this path out as its blob's bytes unchanged, and reads
    it back the same way, so a refresh that finds a file clean found the blob's
    bytes. No filter, `ident` or `working-tree-encoding`, and the file is not
    text: `-text` (or `-crlf` where `text` is unspecified), or nothing set at
    all with `core.autocrlf` false. Every other path is left to git."""
    def off(name: str) -> bool:
        return attributes.get(name, "unspecified") in ("unspecified", "unset")
    if not (off("filter") and off("ident") and off("working-tree-encoding")):
        return False
    text = attributes.get("text", "unspecified")
    crlf = attributes.get("crlf", "unspecified")
    if text == "unset" or (text == "unspecified" and crlf == "unset"):
        return True
    return (text == "unspecified" and crlf == "unspecified"
            and attributes.get("eol", "unspecified") == "unspecified" and not autocrlf)


def _autocrlf(path: str, env: dict[str, str], deadline: _Deadline) -> bool:
    """True unless `core.autocrlf` is unset or false; `input` counts as set (it
    normalises what a refresh reads)."""
    raw = _git_out(path, env, deadline, "config", "--get", "core.autocrlf", check=False).strip()
    if not raw:
        return False
    if raw.lower() == b"input":
        return True
    parsed = _git_out(path, env, deadline, "config", "--type=bool", "--get", "core.autocrlf", check=False).strip()
    return parsed != b"false"


def _attributes(path: str, env: dict[str, str], deadline: _Deadline, paths: list[str]) -> dict[str, dict[str, str]]:
    """`check-attr --cached` for every path: the index's .gitattributes, as checkout reads them."""
    out = _run(["git", "-C", path, "check-attr", "--cached", "-z", "--stdin", *ATTRIBUTES], env, deadline.left(),
               stdin=b"\0".join(os.fsencode(p) for p in paths) + b"\0")
    if out.returncode:
        raise ValueError("check-attr failed: " + os.fsdecode(out.stderr).strip()[-200:])
    fields = out.stdout.split(b"\0")
    found: dict[str, dict[str, str]] = {}
    for i in range(0, len(fields) - 2, 3):
        found.setdefault(os.fsdecode(fields[i]), {})[os.fsdecode(fields[i + 1])] = os.fsdecode(fields[i + 2])
    return found


def _tree_files(raw: bytes) -> list[tuple[str, str, int, int]]:
    """(path, blob, size, mode bits) of every regular file `ls-tree -r -l -z` lists."""
    files = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, _, name = record.partition(b"\t")
        mode, kind, oid, size = meta.split()
        if kind == b"blob" and mode.decode() in _REGULAR_MODES:
            files.append((os.fsdecode(name), oid.decode(), int(size), _REGULAR_MODES[mode.decode()]))
    files.sort()
    return files


def _index_blobs(raw: bytes) -> dict[str, str]:
    """path -> blob of every stage-0 regular file `ls-files -s -z` lists."""
    blobs = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, _, name = record.partition(b"\t")
        mode, oid, stage_number = meta.split()
        if stage_number == b"0" and mode.decode() in _REGULAR_MODES:
            blobs[os.fsdecode(name)] = oid.decode()
    return blobs


def _umask(path: str) -> int:
    """The process umask, read without `os.umask` (which sets it for every
    thread): the mode a new file asked for 0777 is given."""
    probe = os.path.join(path, f".subfleet-umask-{os.getpid()}-{time.monotonic_ns()}")
    fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o777)
    try:
        granted = stat.S_IMODE(os.fstat(fd).st_mode)
    finally:
        os.close(fd)
        os.unlink(probe)
    return 0o777 & ~granted


def _has_xattrs(fd: int) -> bool:
    if _flistxattr is None:
        return False
    size = _flistxattr(fd, None, 0, 0)
    return size != 0


def _has_acl(fd: int) -> bool:
    if _acl_get_fd_np is None:
        return False
    acl = _acl_get_fd_np(fd, ACL_TYPE_EXTENDED)
    if not acl:
        return False
    _acl_free(acl)
    return True


def _clear_index_lock(path: str, env: dict[str, str], deadline: _Deadline) -> None:
    """A refresh cut short at the stage's cap leaves `index.lock` in this new
    worktree's own git directory, which the reset would trip on. Nothing else
    writes that index: the worktree was made by this call and is not handed out."""
    out = _run(["git", "-C", path, "rev-parse", "--git-dir"], env, max(1.0, deadline.left()))
    if out.returncode or not out.stdout.strip():
        return
    git_dir = os.fsdecode(out.stdout.strip())
    lock = Path(git_dir if os.path.isabs(git_dir) else os.path.join(path, git_dir)) / "index.lock"
    try:
        lock.unlink()
    except FileNotFoundError:
        pass


def _remove(repository: str, path: str, env: dict[str, str], timeout_s: float) -> None:
    shutil.rmtree(path, ignore_errors=True)
    _run(["git", "-C", repository, "worktree", "prune"], env, timeout_s)


def _git_out(where: str, env: dict[str, str], deadline: _Deadline, *args: str, check: bool = True) -> bytes:
    out = _run(["git", "-C", where, *args], env, deadline.left())
    if out.returncode and check:
        raise ValueError(f"git {args[0]} failed: " + os.fsdecode(out.stderr).strip()[-200:])
    return out.stdout if not out.returncode else b""


def _run(cmd: list[str], env: dict[str, str], timeout_s: float, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    if timeout_s <= 0:
        raise subprocess.TimeoutExpired(cmd, 0)
    return subprocess.run(cmd, env=env, input=stdin, capture_output=True, timeout=timeout_s)


def _run_text(cmd: list[str], env: dict[str, str], timeout_s: float) -> subprocess.CompletedProcess:
    """As the daemon ran `git worktree add`: text, with git's undecodable bytes escaped."""
    if timeout_s <= 0:
        raise subprocess.TimeoutExpired(cmd, 0)
    return subprocess.run(cmd, capture_output=True, text=True, errors="backslashreplace", env=env,
                          timeout=timeout_s)
