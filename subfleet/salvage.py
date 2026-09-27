"""Private git snapshots which leave HEAD, the index and worktree alone."""

from __future__ import annotations

import errno
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .adapters.base import AdapterError
from .sessions.transcripts import open_regular

#: One git call's wall-clock cap when the caller names none. The 15 s this
#: replaced failed jobs whose repository was healthy: on 2026-09-20, under a
#: load average near 10, a snapshot that takes 0.6 s on an idle machine ran past
#: it. `caps.workspace_git_timeout_s` is the daemon's setting; this variable is
#: for callers that have no policy in hand.
DEFAULT_GIT_TIMEOUT_S = 60
GIT_TIMEOUT_ENV = "SUBFLEET_GIT_TIMEOUT_S"

#: `OSError`s that describe the machine at this moment, not the repository: the
#: same call is expected to succeed once the pressure passes.
TRANSIENT_ERRNOS = frozenset({
    errno.EAGAIN, errno.EINTR, errno.ENOMEM, errno.EMFILE, errno.ENFILE,
    errno.EBUSY, errno.ETIMEDOUT, errno.ENOBUFS,
})


class SalvageError(RuntimeError):
    """A snapshot failed; callers must retain the workspace for reconciliation.

    ``transient`` is true when the failure says nothing about the repository (a
    timeout, or an `OSError` in `TRANSIENT_ERRNOS`), so the caller may retry.
    """

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def git_timeout_s(timeout_s: float | None = None) -> float:
    """The explicit cap, else `SUBFLEET_GIT_TIMEOUT_S`, else the default."""
    if timeout_s is not None:
        return timeout_s
    try:
        value = float(os.environ.get(GIT_TIMEOUT_ENV, ""))
    except ValueError:
        return DEFAULT_GIT_TIMEOUT_S
    return value if value > 0 else DEFAULT_GIT_TIMEOUT_S


def transient_os_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in TRANSIENT_ERRNOS


def _git(workdir: str | Path, *args: str, env: dict[str, str] | None = None,
         optional: bool = False, timeout_s: float | None = None) -> str | None:
    cap = git_timeout_s(timeout_s)
    try:
        result = subprocess.run(["git", "-C", str(workdir), *args], env=env,
                                capture_output=True, text=True, timeout=cap)
    except subprocess.TimeoutExpired as exc:
        # Never `optional`: a call that did not finish has not said "no HEAD" or
        # "no branch", and reading it that way admits a writable job with no
        # baseline or lets one past the main/master refusal.
        raise SalvageError(f"git {args[0]} timed out after {cap:g} s", transient=True) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        if transient_os_error(exc):
            raise SalvageError(f"git {args[0]} could not run: {exc}", transient=True) from exc
        if optional:
            return None
        raise SalvageError(f"git {args[0]} unavailable: {type(exc).__name__}: {exc}") from exc
    if result.returncode:
        if optional:
            return None
        raise SalvageError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_bytes(workdir: str | Path, *args: str, env: dict[str, str] | None = None,
               stdin: bytes | None = None, timeout_s: float | None = None) -> bytes | None:
    """``_git`` for byte streams (paths need not be UTF-8); None on a non-zero
    exit. Timeouts and transient OS errors raise exactly as in ``_git``."""
    cap = git_timeout_s(timeout_s)
    try:
        result = subprocess.run(["git", "-C", str(workdir), *args], env=env, input=stdin,
                                capture_output=True, timeout=cap)
    except subprocess.TimeoutExpired as exc:
        raise SalvageError(f"git {args[0]} timed out after {cap:g} s", transient=True) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        if transient_os_error(exc):
            raise SalvageError(f"git {args[0]} could not run: {exc}", transient=True) from exc
        return None
    return None if result.returncode else result.stdout


def git_head(workdir: str | Path, *, timeout_s: float | None = None) -> str | None:
    return _git(workdir, "rev-parse", "--verify", "HEAD", optional=True, timeout_s=timeout_s)


def git_branch(workdir: str | Path, *, timeout_s: float | None = None) -> str | None:
    return _git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD", optional=True,
                timeout_s=timeout_s)


def git_toplevel(workdir: str | Path, *, timeout_s: float | None = None) -> str | None:
    """C-6.5: the real path of the worktree that holds ``workdir``, or None outside one.

    Two directories of one checkout (`/repo` and `/repo/sub`) are one place to
    write; two linked worktrees of one repository are two.
    """
    # Only git's line end is removed: a directory whose name ends in a space is
    # still that directory (review of 5aa2718).
    raw = _git_bytes(workdir, "rev-parse", "--show-toplevel", timeout_s=timeout_s)
    top = os.fsdecode(raw[:-1] if raw and raw.endswith(b"\n") else raw or b"")
    return os.path.realpath(top) if top else None


def git_tree(workdir: str | Path, commit: str, *, timeout_s: float | None = None) -> str | None:
    """The tree of ``commit``, or None when the repository cannot name it."""
    return _git(workdir, "rev-parse", "--verify", f"{commit}^{{tree}}", optional=True,
                timeout_s=timeout_s)


def working_tree(workdir: str | Path, baseline_commit: str, *,
                 timeout_s: float | None = None) -> str:
    """Snapshot tracked and untracked files without changing the real index.

    The temporary index holds ``baseline_commit`` before ``add -A`` records
    the worktree over it. It is seeded from a copy of the real index and
    read with ``read-tree -m``: the entries are the baseline's either way,
    but git keeps the real index's stat data for every path whose content
    matches, so ``add -A`` hashes only the files that changed. Read into an
    empty index, the baseline has no stat data and every tracked file is
    hashed again, which on a large checkout outlasts the preparation cap
    (C-6.8). The copy's assume-unchanged and skip-worktree bits, which
    ``read-tree -m`` keeps, are cleared before ``add -A``: it skips such paths, and the empty-index read has no such
    bits, so the two reads give the same tree. When the real index cannot
    seed it (there is none yet, it has unmerged entries, git cannot read the
    copy or clear its bits), the baseline is read into an empty index as
    before.

    Trusting stat data is git's own model (``git status`` does the same): a
    file rewritten with the same size, mtime and inode is read as unchanged,
    where the empty-index read would hash it.
    """
    gitdir = _git(workdir, "rev-parse", "--absolute-git-dir", timeout_s=timeout_s)
    with tempfile.TemporaryDirectory(prefix="subfleet-salvage-", dir=gitdir) as temporary:
        index = Path(temporary) / "index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        seeded = (_seed_index(workdir, index, timeout_s=timeout_s)
                  and _git(workdir, "read-tree", "-m", baseline_commit, env=env,
                           optional=True, timeout_s=timeout_s) is not None
                  and _clear_skip_bits(workdir, env, timeout_s=timeout_s))
        if not seeded:
            index.unlink(missing_ok=True)
            _git(workdir, "read-tree", baseline_commit, env=env, timeout_s=timeout_s)
        _git(workdir, "add", "-A", env=env, timeout_s=timeout_s)
        return _git(workdir, "write-tree", env=env, timeout_s=timeout_s)


def _seed_index(workdir: str | Path, index: Path, *,
                timeout_s: float | None = None) -> bool:
    """Copy the real index to ``index``; False when there is none to copy.

    The copy keeps the file's mtime: git treats entries at or after the
    index's own mtime as possibly modified and hashes them, and a copy
    stamped now would hide exactly those edits. The bytes and the mtime come
    from one open file, so a concurrent rewrite of the index (git replaces
    it by rename) cannot pair one index's entries with another's mtime.

    The real index is read only as a regular file, and the copy is a new file
    (O_EXCL, O_NOFOLLOW): this runs outside git's time limit, and a FIFO in
    the index's place had held a diff, and with it the file pool and the
    conversation service's close(), until a writer came (review of aa41312).
    Anything but a regular file there is no index to copy.
    """
    real = _git(workdir, "rev-parse", "--git-path", "index", optional=True,
                timeout_s=timeout_s)
    if not real:
        return False
    source = Path(real) if os.path.isabs(real) else Path(workdir) / real
    try:
        out = os.open(index, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with open(out, "wb") as writer, open_regular(source) as reader:
            stat = os.fstat(reader.fileno())
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.utime(writer.fileno(), ns=(stat.st_atime_ns, stat.st_mtime_ns))
    except OSError:
        index.unlink(missing_ok=True)
        return False
    return True


def _clear_skip_bits(workdir: str | Path, env: dict[str, str], *,
                     timeout_s: float | None = None) -> bool:
    """Clear assume-unchanged and skip-worktree bits in the temporary index.

    Flipping a bit hashes nothing. ``update-index`` applies only the first
    of the two options it is given, so each bit gets its own call.
    """
    # From the toplevel: from a subdirectory, ls-files lists only that
    # subdirectory's entries, and add -A still records the whole tree.
    top = _git(workdir, "rev-parse", "--show-toplevel", optional=True, timeout_s=timeout_s)
    if not top:
        return False
    listed = _git_bytes(top, "ls-files", "-v", "-z", env=env, timeout_s=timeout_s)
    if listed is None:
        return False
    assumed: list[bytes] = []
    skipped: list[bytes] = []
    for record in listed.split(b"\0"):
        if len(record) < 3:
            continue
        tag, path = record[:1], record[2:]
        if tag.islower():
            assumed.append(path)
        if tag in (b"S", b"s"):
            skipped.append(path)
    for option, paths in (("--no-assume-unchanged", assumed),
                          ("--no-skip-worktree", skipped)):
        if paths and _git_bytes(top, "update-index", option, "-z", "--stdin",
                                stdin=b"\0".join(paths) + b"\0", env=env,
                                timeout_s=timeout_s) is None:
            return False
    return True


def validate_writable_workdir(workdir: str | Path, *, timeout_s: float | None = None) -> None:
    """Refuse writable admission on main/master while permitting private refs."""
    branch = git_branch(workdir, timeout_s=timeout_s)
    if branch in {"main", "master"}:
        raise AdapterError(f"writable job refused on {branch}", code=7,
                           fix="Check out a task branch before submitting a writable job.")


@dataclass(frozen=True)
class SalvageResult:
    ref: str
    commit: str
    tree: str
    baseline: str


def _stamp(timestamp: str | datetime | None) -> str:
    if timestamp is None:
        value = datetime.now(UTC)
    elif isinstance(timestamp, str):
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    else:
        value = timestamp
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def salvage(workdir: str | Path, baseline_commit: str, seq: int, *, writable: bool = True,
            state: str = "finalizing", timestamp: str | datetime | None = None,
            baseline_tree: str | None = None,
            timeout_s: float | None = None) -> SalvageResult | None:
    """C-13.1: commit a differing working tree beneath a private salvage ref.

    ``baseline_commit`` is the HEAD recorded at reservation and always the
    snapshot's parent. ``baseline_tree`` is the reservation's working tree,
    including any pre-existing dirty files; older callers default to HEAD. Supply
    the recorded attempt timestamp to make a finalization replay idempotent.
    Private refs are permitted even when the current branch is main (C-13.2).
    ``timeout_s`` caps each git call (`git_timeout_s`).
    """
    if not writable:
        return None
    if state not in {"finalizing", "lost", "kill"}:
        raise ValueError("salvage requires finalizing, lost or kill state")
    if seq < 1:
        raise ValueError("attempt sequence must be positive")
    baseline = _git(workdir, "rev-parse", "--verify", f"{baseline_commit}^{{commit}}", timeout_s=timeout_s)
    baseline_tree = _git(workdir, "rev-parse", "--verify", f"{baseline_tree or baseline}^{{tree}}", timeout_s=timeout_s)
    branch = re.sub(r"[^A-Za-z0-9_-]+", "-", git_branch(workdir, timeout_s=timeout_s) or "detached").strip("-") or "detached"
    ref = f"refs/subfleet-salvage/{branch}-{_stamp(timestamp)}-a{seq}"
    # A private temporary directory avoids index-name races and never points
    # git at the user's real index, including in linked worktrees.
    tree = working_tree(workdir, baseline, timeout_s=timeout_s)
    if tree == baseline_tree:
        return None
    previous = _git(workdir, "rev-parse", "--verify", ref, optional=True, timeout_s=timeout_s)
    if previous:
        previous_tree = _git(workdir, "rev-parse", f"{previous}^{{tree}}", timeout_s=timeout_s)
        previous_parent = _git(workdir, "rev-parse", f"{previous}^", optional=True, timeout_s=timeout_s)
        if previous_tree == tree and previous_parent == baseline:
            return SalvageResult(ref, previous, tree, baseline)
        # Never overwrite a previous snapshot with different bytes.
        ref = f"{ref}-{tree[:12]}"
    commit = _git(workdir, "-c", "user.name=subfleet", "-c", "user.email=subfleet@localhost",
                  "commit-tree", tree, "-p", baseline, "-m", f"subfleet salvage attempt a{seq}", timeout_s=timeout_s)
    existing = _git(workdir, "rev-parse", "--verify", ref, optional=True, timeout_s=timeout_s)
    if existing:
        if (_git(workdir, "rev-parse", f"{existing}^{{tree}}", timeout_s=timeout_s) == tree
                and _git(workdir, "rev-parse", f"{existing}^", timeout_s=timeout_s) == baseline):
            return SalvageResult(ref, existing, tree, baseline)
        raise SalvageError("salvage reference already names a different snapshot")
    _git(workdir, "update-ref", ref, commit, "0" * 40, timeout_s=timeout_s)
    return SalvageResult(ref, commit, tree, baseline)
