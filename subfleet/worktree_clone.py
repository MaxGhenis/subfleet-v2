"""C-6.6a: allocate a job's worktree from clones of the caller's checkout.

`git worktree add` writes every tracked file, so each worktree of a large
repository costs its full size on disk: about 5 GB for
TheAxiomFoundation/axiom-roadmap-dashboard, whose sealed evaluation ledger and
plan records are tracked. On APFS a file the caller's checkout already holds
can instead be an APFS clone: a regular file with its own inode, mode and
metadata that shares its blocks with the original until either side writes.

`add_worktree` makes the worktree `git worktree add --detach` makes, by git's
own steps (git 2.55 `builtin/worktree.c`: `add_worktree`, `checkout_worktree`)
with one step put in the middle:

1. Preflight, in the caller's repository as `worktree add` sees it: the
   caller's directory must be in a worktree, and no `post-checkout` hook may be
   configured there (`git hook list`). `worktree add` runs that hook itself
   (serially, from the caller's repository, in the new tree, without
   `GIT_DIR`/`GIT_WORK_TREE`); rather than imitate that, a repository with one
   gets the plain add.
2. `git worktree add --no-checkout --detach PATH COMMIT`: the same
   administrative directory, HEAD, and copied sparse and worktree config as
   the plain add, without its checkout (which runs no hook here).
3. Clone stage, best effort, within `CLONE_SHARE` of the cap. Its own git
   calls run with no hook (`core.hooksPath=/dev/null`, `hook.<event>.enabled`
   off for the index and ref events) and no fsmonitor, so it starts nothing a
   plain add would not. `read-tree` fills the index. A tracked regular file is
   cloned when the caller's checkout records the same blob at the same path,
   git checks it out as the blob's bytes unchanged (`_writes_blob_bytes`), it
   is not a `.gitattributes` file, and the caller's file is a regular file of
   the blob's size with no flags. A clone is kept only if it has no flags,
   extended attributes or ACL and its bytes hash to the blob (a raw git blob
   hash, no conversion); it is given the mode git would create it with under
   the umask. `update-index --refresh` then records stat data for the clones.
4. git's own checkout, run as `worktree add` runs it: `git reset --hard
   --no-recurse-submodules` with `GIT_DIR=PATH/.git` and `GIT_WORK_TREE=PATH`,
   from the caller's worktree top. It keeps every entry the refresh found up to
   date and writes all the others.
5. The tree must then be clean (`diff-files`, `diff-index --cached`, no
   untracked file). If it is not, this tree and only its own registration are
   removed (never a repository-wide prune, d635) and the plain add makes it.

One deadline covers every step, as the cap covered the one add, and a timeout
anywhere is reported as the add's. A clone keeps its source's modification
time, which git never reads for content. Anything the clone stage cannot do
leaves git to write what it did not clone: the stage never decides what the
worktree contains.

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
#: checkout (step 4) has the rest.
CLONE_SHARE = 0.5
#: Errors that say this volume (or this pair of volumes) cannot clone at all.
NO_CLONES = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.ENOSYS, errno.EINVAL})
#: `check-attr` names read for `_writes_blob_bytes`.
ATTRIBUTES = ("text", "eol", "crlf", "ident", "filter", "working-tree-encoding")
#: For every git call this module adds (never the add or the checkout, which run
#: as git runs them): no hook fires and no fsmonitor daemon starts.
QUIET = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
         "-c", "hook.post-index-change.enabled=false", "-c", "hook.reference-transaction.enabled=false")
#: Reads in the caller's repository that must see its hooks as they are.
NO_FSMONITOR = ("-c", "core.fsmonitor=false")
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

    Raises what `subprocess.run` raises for the add, a `TimeoutExpired` naming
    the add and the whole allocation's cap; an `OSError` if a worktree this call
    made cannot be removed for the plain add. Otherwise returns an `Allocation`
    whose `result` is non-zero when git refused, as `git worktree add` would.
    A relative `path` is read under `repository`, as `git -C` reads it.
    """
    started = time.monotonic()
    deadline = _Deadline(timeout_s)
    if not os.path.isabs(path):
        path = os.path.join(os.path.abspath(repository), path)
    add = ["git", "-C", repository, "worktree", "add", "--detach", path, commit]
    try:
        top = None
        if not (enabled() if clone is None else clone):
            reason = "clone stage off"
        else:
            reason, top = _preflight(repository, env, deadline)
        if reason is not None:
            return _plain(add, env, deadline, started, reason)
        no_checkout = ["git", "-C", repository, "worktree", "add", "--no-checkout", "--detach", path, commit]
        result = _run_text(no_checkout, env, deadline.left())
        if result.returncode:
            return Allocation(result, reason="worktree add refused", seconds=time.monotonic() - started)
        allocation = Allocation(result, method="clone")
        admin = _admin_dir(path, env, deadline)
        try:
            _clone_stage(allocation, top, path, env, deadline, timeout_s * CLONE_SHARE)
        except subprocess.TimeoutExpired:
            allocation.reason = allocation.reason or "clone stage reached its share of the cap"
            if admin is not None:
                _clear_index_lock(admin)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            allocation.reason = f"clone stage failed: {type(exc).__name__}: {exc}"
        # `checkout_worktree`: the child git `worktree add` starts, with its environment and directory.
        checkout_env = {**env, "GIT_DIR": os.path.join(path, ".git"), "GIT_WORK_TREE": path}
        reset = _run_text(["git", "reset", "--hard", "--no-recurse-submodules"], checkout_env, deadline.left(),
                          cwd=top)
        if reset.returncode:
            allocation.result = reset
            allocation.seconds = time.monotonic() - started
            return allocation
        if not _clean(path, env, deadline):
            # Not what `git worktree add` gives, or git gives it that way too
            # (names a case-insensitive volume folds together): the plain add decides.
            _discard(path, admin)
            plain = _plain(add, env, deadline, started, "not clean after the checkout; plain add used")
            plain.skipped = allocation.skipped
            return plain
        allocation.seconds = time.monotonic() - started
        return allocation
    except subprocess.TimeoutExpired as exc:
        # One cap covers the whole allocation, as it covered the one add.
        raise subprocess.TimeoutExpired(add, timeout_s, output=exc.output, stderr=exc.stderr) from exc


def _plain(add: list[str], env: dict[str, str], deadline: _Deadline, started: float,
           reason: str) -> Allocation:
    allocation = Allocation(_run_text(add, env, deadline.left()), reason=reason)
    allocation.seconds = time.monotonic() - started
    return allocation


def _preflight(repository: str, env: dict[str, str], deadline: _Deadline) -> tuple[str | None, str | None]:
    """(why the plain add is used, or None; the caller's worktree top)."""
    out = _run(["git", *QUIET, "-C", repository, "rev-parse", "--show-toplevel"], env, deadline.left())
    top = out.stdout.rstrip(b"\n") if not out.returncode else b""
    if not top:
        return "the caller's directory is in no worktree", None
    # As `worktree add` selects it: from the caller's repository, its config
    # (conditional includes too) and its hooks directory, as git resolves them there.
    hooks = _run(["git", *NO_FSMONITOR, "-C", repository, "hook", "list", "post-checkout"], env, deadline.left())
    if not (hooks.returncode == 1 and b"no hooks found" in hooks.stderr):
        return "post-checkout hook", None
    return None, os.fsdecode(top)


def _clone_stage(allocation: Allocation, source_top: str, path: str, env: dict[str, str],
                 deadline: _Deadline, share_s: float) -> None:
    stage = _Deadline(min(share_s, deadline.left()))
    if _config(path, env, stage, "--bool", "core.sparseCheckout") == b"true":
        allocation.reason = "sparse checkout"
        return
    # Under either, git's checkout marks index entries (assume-unchanged, or
    # fsmonitor-valid) that an entry a refresh verified would not carry.
    if _config(path, env, stage, "--bool", "core.ignoreStat") == b"true":
        allocation.reason = "core.ignoreStat"
        return
    if _config(path, env, stage, "--get", "core.fsmonitor") and \
            _config(path, env, stage, "--type=bool", "--get", "core.fsmonitor") != b"false":
        allocation.reason = "core.fsmonitor"
        return
    listed = _run(["git", *QUIET, "-C", path, "hook", "list", "post-index-change"], env, stage.left())
    if not (listed.returncode == 1 and b"no hooks found" in listed.stderr):
        allocation.reason = "an index write here would run a hook"
        return
    if os.stat(source_top).st_dev != os.stat(path).st_dev:
        allocation.reason = "the caller's checkout is on another volume"
        return
    _git_out(path, env, stage, "read-tree", "HEAD")
    object_format = _git_out(path, env, stage, "rev-parse", "--show-object-format").strip().decode()
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
            if rel.rpartition("/")[2] == ".gitattributes":
                # A refresh reads the tree's attributes files: only git writes them.
                allocation.skip("attributes file")
                continue
            if not _writes_blob_bytes(attributes.get(rel, {}), autocrlf):
                allocation.skip("converted by git on checkout")
                continue
            outcome = _clone_one(source_top, path, rel, oid, size, mode & ~umask, object_format, folders)
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
    # Records stat data for the clones (each already hashed to its blob); one the
    # refresh does not find up to date is written by the reset. Exit 1 says some were not.
    _run(["git", *QUIET, "-C", path, "update-index", "-q", "--ignore-missing", "--refresh"], env, stage.left())
    stale = set(os.fsdecode(p) for p in
                _git_out(path, env, stage, "diff-files", "--name-only", "-z").split(b"\0") if p)
    for rel, size in clones:
        if rel in stale:
            allocation.skip("not up to date after the refresh")
        else:
            allocation.cloned += 1
            allocation.cloned_bytes += size
            allocation.cloned_paths.append(rel)


def _clone_one(source_top: str, path: str, rel: str, oid: str, size: int, mode: int, object_format: str,
               folders: dict[str, int]) -> str | None:
    """Clone `source_top/rel` to `path/rel`; "cloned", a skip reason, or None
    when the volume cannot clone. A kept clone is a regular file of `size`
    bytes that hash to `oid`, with `mode` and no flags, extended attributes or ACL."""
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
    outcome = "metadata git would not write"
    try:
        cst = os.fstat(clone)
        if (stat.S_ISREG(cst.st_mode) and cst.st_size == size and not getattr(cst, "st_flags", 0)
                and not _has_xattrs(clone) and not _has_acl(clone)):
            if _blob_id(clone, size, object_format) != oid:
                outcome = "not the blob's bytes"
            else:
                os.fchmod(clone, mode)
                outcome = "cloned"
    finally:
        os.close(clone)
    if outcome != "cloned":
        os.unlink(name, dir_fd=folder)
    return outcome


def _blob_id(fd: int, size: int, object_format: str) -> str | None:
    """The git blob id of exactly `size` bytes read from `fd`, or None if the
    file is not that long. A clone is private to this allocation: nothing else
    writes it between this read and the refresh."""
    blob = rfs.git_blob_hasher(object_format, size)
    offset = 0
    while offset < size:
        chunk = os.pread(fd, min(rfs.CHUNK, size - offset), offset)
        if not chunk:
            return None
        blob.update(chunk)
        offset += len(chunk)
    if os.pread(fd, 1, offset):
        return None
    return blob.hexdigest()


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
    it back the same way. No filter, `ident` or `working-tree-encoding`, and the
    file is not text: `-text` (or `-crlf` where `text` is unspecified), or
    nothing set at all with `core.autocrlf` false. Every other path is left to git."""
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


def _config(path: str, env: dict[str, str], deadline: _Deadline, *args: str) -> bytes:
    """The repository's own setting: read without `QUIET`, whose overrides it
    would otherwise answer with (`git config` starts no daemon and runs no hook)."""
    return _git_out(path, env, deadline, "config", *args, check=False, overrides=()).strip()


def _autocrlf(path: str, env: dict[str, str], deadline: _Deadline) -> bool:
    """True unless `core.autocrlf` is unset or false; `input` counts as set (it
    normalises what a refresh reads)."""
    raw = _config(path, env, deadline, "--get", "core.autocrlf")
    if not raw:
        return False
    if raw.lower() == b"input":
        return True
    return _config(path, env, deadline, "--type=bool", "--get", "core.autocrlf") != b"false"


def _attributes(path: str, env: dict[str, str], deadline: _Deadline, paths: list[str]) -> dict[str, dict[str, str]]:
    """`check-attr --cached` for every path: the index's .gitattributes, as checkout reads them."""
    out = _run(["git", *QUIET, "-C", path, "check-attr", "--cached", "-z", "--stdin", *ATTRIBUTES], env,
               deadline.left(), stdin=b"\0".join(os.fsencode(p) for p in paths) + b"\0")
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
    return _flistxattr(fd, None, 0, 0) != 0


def _has_acl(fd: int) -> bool:
    if _acl_get_fd_np is None:
        return False
    acl = _acl_get_fd_np(fd, ACL_TYPE_EXTENDED)
    if not acl:
        return False
    _acl_free(acl)
    return True


def _clean(path: str, env: dict[str, str], deadline: _Deadline) -> bool:
    """Plumbing, so no `status.*` setting decides: tracked files match the index,
    the index matches HEAD, and nothing is untracked."""
    worktree = _run(["git", *QUIET, "-C", path, "diff-files", "--quiet", "--ignore-submodules=none"],
                    env, deadline.left())
    index = _run(["git", *QUIET, "-C", path, "diff-index", "--cached", "--quiet", "--ignore-submodules=none",
                  "HEAD"], env, deadline.left())
    untracked = _run(["git", *QUIET, "-C", path, "ls-files", "-z", "--others", "--exclude-standard"],
                     env, deadline.left())
    return not (worktree.returncode or index.returncode or untracked.returncode or untracked.stdout)


def _admin_dir(path: str, env: dict[str, str], deadline: _Deadline) -> Path | None:
    out = _run(["git", *QUIET, "-C", path, "rev-parse", "--absolute-git-dir"], env, deadline.left())
    git_dir = out.stdout.rstrip(b"\n") if not out.returncode else b""
    return Path(os.fsdecode(git_dir)) if git_dir else None


def _clear_index_lock(admin: Path) -> None:
    """A refresh cut short at the stage's share leaves `index.lock` in this new
    worktree's own git directory, which the reset would trip on. Nothing else
    writes that index: the worktree was made by this call and is not handed out."""
    try:
        (admin / "index.lock").unlink()
    except FileNotFoundError:
        pass


def _discard(path: str, admin: Path | None) -> None:
    """Remove the tree this call made and its own registration, nothing else
    (d635: never a repository-wide prune). Raises `OSError` if it cannot, so the
    plain add never runs over a registration still in place."""
    if admin is None:
        raise OSError(errno.ENOENT, "the new worktree's git directory is unknown", path)
    backlink = rfs.read_regular(admin / "gitdir", limit=65536).decode("utf-8", "surrogateescape").strip()
    if not os.path.isabs(backlink):         # `worktree.useRelativePaths`: relative to the admin directory
        backlink = os.path.join(admin, backlink)
    if os.path.realpath(backlink) != os.path.realpath(os.path.join(path, ".git")):
        raise OSError(errno.EEXIST, "the git directory is not this worktree's", str(admin))
    if os.path.lexists(admin / "locked"):
        raise OSError(errno.EBUSY, "the worktree is locked", str(admin))
    shutil.rmtree(path)
    shutil.rmtree(admin)


def _git_out(where: str, env: dict[str, str], deadline: _Deadline, *args: str, check: bool = True,
             overrides: tuple[str, ...] = QUIET) -> bytes:
    out = _run(["git", *overrides, "-C", where, *args], env, deadline.left())
    if out.returncode and check:
        raise ValueError(f"git {args[0]} failed: " + os.fsdecode(out.stderr).strip()[-200:])
    return out.stdout if not out.returncode else b""


def _run(cmd: list[str], env: dict[str, str], timeout_s: float, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    if timeout_s <= 0:
        raise subprocess.TimeoutExpired(cmd, 0)
    return subprocess.run(cmd, env=env, input=stdin, capture_output=True, timeout=timeout_s)


def _run_text(cmd: list[str], env: dict[str, str], timeout_s: float, cwd: str | None = None) -> subprocess.CompletedProcess:
    """As the daemon ran `git worktree add`: text, with git's undecodable bytes escaped."""
    if timeout_s <= 0:
        raise subprocess.TimeoutExpired(cmd, 0)
    return subprocess.run(cmd, capture_output=True, text=True, errors="backslashreplace", env=env,
                          timeout=timeout_s, cwd=cwd)
