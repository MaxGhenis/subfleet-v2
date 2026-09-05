"""Private git snapshots which leave HEAD, the index and worktree alone."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .adapters.base import AdapterError


class SalvageError(RuntimeError):
    """A snapshot failed; callers must retain the workspace for reconciliation."""


def _git(workdir: str | Path, *args: str, env: dict[str, str] | None = None,
         optional: bool = False) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(workdir), *args], env=env,
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        if optional:
            return None
        raise SalvageError(f"git {args[0]} unavailable") from exc
    if result.returncode:
        if optional:
            return None
        raise SalvageError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def git_head(workdir: str | Path) -> str | None:
    return _git(workdir, "rev-parse", "--verify", "HEAD", optional=True)


def git_branch(workdir: str | Path) -> str | None:
    return _git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD", optional=True)


def validate_writable_workdir(workdir: str | Path) -> None:
    """Refuse writable admission on main/master while permitting private refs."""
    branch = git_branch(workdir)
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
            state: str = "finalizing", timestamp: str | datetime | None = None) -> SalvageResult | None:
    """C-13.1: commit a differing working tree beneath a private salvage ref.

    ``baseline_commit`` is the HEAD recorded at reservation; its tree is the
    comparison point and that commit is always the snapshot's parent. Supply
    the recorded attempt timestamp to make a finalization replay idempotent.
    Private refs are permitted even when the current branch is main (C-13.2).
    """
    if not writable:
        return None
    if state not in {"finalizing", "lost", "kill"}:
        raise ValueError("salvage requires finalizing, lost or kill state")
    if seq < 1:
        raise ValueError("attempt sequence must be positive")
    baseline = _git(workdir, "rev-parse", "--verify", f"{baseline_commit}^{{commit}}")
    baseline_tree = _git(workdir, "rev-parse", "--verify", f"{baseline}^{{tree}}")
    gitdir = _git(workdir, "rev-parse", "--absolute-git-dir")
    branch = re.sub(r"[^A-Za-z0-9_-]+", "-", git_branch(workdir) or "detached").strip("-") or "detached"
    ref = f"refs/subfleet-salvage/{branch}-{_stamp(timestamp)}-a{seq}"
    # A private temporary directory avoids index-name races and never points
    # git at the user's real index, including in linked worktrees.
    with tempfile.TemporaryDirectory(prefix="subfleet-salvage-", dir=gitdir) as temporary:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        _git(workdir, "read-tree", baseline, env=env)
        _git(workdir, "add", "-A", env=env)
        tree = _git(workdir, "write-tree", env=env)
    if tree == baseline_tree:
        return None
    previous = _git(workdir, "rev-parse", "--verify", ref, optional=True)
    if previous:
        previous_tree = _git(workdir, "rev-parse", f"{previous}^{{tree}}")
        previous_parent = _git(workdir, "rev-parse", f"{previous}^", optional=True)
        if previous_tree == tree and previous_parent == baseline:
            return SalvageResult(ref, previous, tree, baseline)
        # Never overwrite a previous snapshot with different bytes.
        ref = f"{ref}-{tree[:12]}"
    commit = _git(workdir, "-c", "user.name=subfleet", "-c", "user.email=subfleet@localhost",
                  "commit-tree", tree, "-p", baseline, "-m", f"subfleet salvage attempt a{seq}")
    existing = _git(workdir, "rev-parse", "--verify", ref, optional=True)
    if existing:
        if (_git(workdir, "rev-parse", f"{existing}^{{tree}}") == tree
                and _git(workdir, "rev-parse", f"{existing}^") == baseline):
            return SalvageResult(ref, existing, tree, baseline)
        raise SalvageError("salvage reference already names a different snapshot")
    _git(workdir, "update-ref", ref, commit, "0" * 40)
    return SalvageResult(ref, commit, tree, baseline)
