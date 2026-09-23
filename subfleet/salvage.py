"""Private git snapshots which leave HEAD, the index and worktree alone."""

from __future__ import annotations

import errno
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from .adapters.base import AdapterError

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
    top = _git(workdir, "rev-parse", "--show-toplevel", optional=True, timeout_s=timeout_s)
    return os.path.realpath(top) if top else None


def git_tree(workdir: str | Path, commit: str, *, timeout_s: float | None = None) -> str | None:
    """The tree of ``commit``, or None when the repository cannot name it."""
    return _git(workdir, "rev-parse", "--verify", f"{commit}^{{tree}}", optional=True,
                timeout_s=timeout_s)


class WorktreeCheck(NamedTuple):
    """What `check_worktree` found at a path the daemon allocates (C-6.8)."""
    #: The path's entry in its repository's `git worktree list --porcelain`,
    #: {"head": sha, "locked": reason}, each when shown; None when not listed.
    registration: dict[str, str] | None
    #: Why the path is not a finished checkout of the commit; None when it is.
    unfinished: str | None


def worktree_registration(listing: str, worktree: str | Path) -> dict[str, str] | None:
    """``worktree``'s entry in a `git worktree list --porcelain` listing, or None.

    The path's last component is not resolved: a link there is not the
    worktree it points to.
    """
    absolute = os.path.abspath(worktree)
    target = os.path.join(os.path.realpath(os.path.dirname(absolute)), os.path.basename(absolute))
    for stanza in listing.split("\n\n"):
        lines = stanza.splitlines()
        if lines and lines[0].startswith("worktree ") and os.path.realpath(lines[0][len("worktree "):]) == target:
            entry = {}
            for line in lines[1:]:
                if line.startswith("HEAD "):
                    entry["head"] = line[len("HEAD "):]
                elif line == "locked" or line.startswith("locked "):
                    entry["locked"] = line[len("locked "):]
            return entry
    return None


def check_worktree(repository: str | Path, worktree: str | Path, commit: str, *,
                   timeout_s: float | None = None) -> WorktreeCheck:
    """C-6.8: whether ``worktree`` is a finished checkout of ``commit``, and its registration.

    Finished means a directory with a `.git` link (git run in a directory
    without one answers for whatever repository encloses it), listed by
    ``repository`` as one of its worktrees, unlocked (git locks a worktree
    `initializing` until its add returns, so an add that was killed leaves the
    lock), with HEAD at ``commit`` and an index holding that commit's tree (a
    checkout stopped before it wrote its index has none, however many files it
    wrote). The registration is read whether or not the directory exists: an
    add stopped partway can leave one with no directory. A git call that did
    not finish raises; one that failed reads as not listed, or not finished.
    """
    path = Path(worktree)
    listing = _git(repository, "worktree", "list", "--porcelain", optional=True, timeout_s=timeout_s)
    entry = worktree_registration(listing or "", path)
    if not os.path.lexists(path):
        return WorktreeCheck(entry, "no directory")
    link = path / ".git"
    if path.is_symlink() or not path.is_dir() or link.is_symlink() or not link.is_file():
        return WorktreeCheck(entry, "no .git link")
    if entry is None:
        return WorktreeCheck(entry, f"{repository} does not list it")
    if "locked" in entry:
        return WorktreeCheck(entry, f"locked ({entry['locked'] or 'no reason given'})")
    if entry.get("head") != commit:
        return WorktreeCheck(entry, f"HEAD is {entry.get('head') or 'unreadable'}")
    if _git(path, "diff-index", "--cached", "--quiet", commit, "--", optional=True, timeout_s=timeout_s) is None:
        return WorktreeCheck(entry, "its index is not a checkout of the commit")
    return WorktreeCheck(entry, None)


def working_tree(workdir: str | Path, baseline_commit: str, *,
                 timeout_s: float | None = None) -> str:
    """Snapshot tracked and untracked files without changing the real index."""
    gitdir = _git(workdir, "rev-parse", "--absolute-git-dir", timeout_s=timeout_s)
    with tempfile.TemporaryDirectory(prefix="subfleet-salvage-", dir=gitdir) as temporary:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        _git(workdir, "read-tree", baseline_commit, env=env, timeout_s=timeout_s)
        _git(workdir, "add", "-A", env=env, timeout_s=timeout_s)
        return _git(workdir, "write-tree", env=env, timeout_s=timeout_s)


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
