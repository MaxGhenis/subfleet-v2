"""Read-only proof that deleting a job cannot delete its salvage (C-8.4)."""

from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
import re
import subprocess
import threading
from typing import Any

from .contracts import attempt_dir
from .retention import _Interrupted, _checkpoint, _git
from .sessions.transcripts import read_regular
from .store import Store


# C-8.4: only these durable, shared namespaces can authorize destruction.
ALLOWED_REF_PREFIXES = ("refs/heads/", "refs/tags/", "refs/subfleet-salvage/", "refs/subfleet/")
REGENERABLE_CACHE_DIRECTORIES = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules",
    ".venv", ".hypothesis", ".tox",
})


def _regenerable_ignored(path: str) -> bool:
    """Allow only named cache directories, *.egg-info directories and .DS_Store.

    Git's --directory ends directory entries with '/'. An ordinary ignored
    file named 'build' or '.venv' is not a regenerable directory.
    """
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return ((path.endswith("/") and (name in REGENERABLE_CACHE_DIRECTORIES
                                     or (name.endswith(".egg-info") and name != ".egg-info")))
            or (not path.endswith("/") and name == ".DS_Store"))


def _directory_signature(path: Path) -> tuple[int, int, int]:
    metadata = path.stat(follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns


@dataclass
class _NestedGitScan:
    identity: tuple[int, int]
    pending: list[Path]
    inspected: list[tuple[Path, tuple[int, int, int]]] = field(default_factory=list)


# A partial traversal is work saved, never cached permission. Every directory
# already inspected is revalidated before success; successful/failed proofs
# discard their traversal. The bound also prevents abandoned jobs accumulating.
_NESTED_GIT_SCANS: OrderedDict[Path, _NestedGitScan] = OrderedDict()


def _prove_no_nested_git(worktree: Path, cancel, deadline, *,
                         on_progress: Callable[[], None] = lambda: None) -> None:
    worktree = worktree.resolve()
    identity = _directory_signature(worktree)[:2]
    scan = _NESTED_GIT_SCANS.get(worktree)
    if scan is None or scan.identity != identity:
        scan = _NestedGitScan(identity, [worktree])
        _NESTED_GIT_SCANS[worktree] = scan
    _NESTED_GIT_SCANS.move_to_end(worktree)
    while len(_NESTED_GIT_SCANS) > 32:
        _NESTED_GIT_SCANS.popitem(last=False)
    try:
        while scan.pending:
            _checkpoint(cancel, deadline)
            directory = scan.pending[-1]
            before = _directory_signature(directory)
            children = []
            names = set()
            # Close the descriptor before a checkpoint can suspend the scan.
            with os.scandir(directory) as entries:
                for entry in entries:
                    names.add(entry.name.casefold())
                    if entry.name.casefold() == ".git":
                        if directory != worktree or entry.name != ".git":
                            nested = Path(entry.path).relative_to(worktree)
                            raise ValueError(f"nested Git metadata is not preserved by salvage: {str(nested)!r}")
                    elif entry.is_dir(follow_symlinks=False):
                        children.append(Path(entry.path))
            if directory != worktree and (directory.name.casefold().endswith(".git")
                                          or {"head", "objects"} <= names):
                raise ValueError(f"nested bare Git repository is not preserved: {directory.relative_to(worktree)!s}")
            if _directory_signature(directory) != before:
                raise ValueError("worktree directory changed during nested Git proof")
            scan.pending.pop()
            scan.pending.extend(children)
            scan.inspected.append((directory, before))
            on_progress()
        for directory, signature in scan.inspected:
            _checkpoint(cancel, deadline)
            if _directory_signature(directory) != signature:
                raise ValueError("worktree directory changed during nested Git proof")
    except _Interrupted:
        raise
    except BaseException:
        _NESTED_GIT_SCANS.pop(worktree, None)
        raise
    else:
        _NESTED_GIT_SCANS.pop(worktree, None)


def prove_worktree_preserved(job: dict[str, Any], state_root: Path,
                             salvage_artifacts: list[dict[str, Any]], *,
                             cancel: threading.Event | None = None,
                             deadline: float | None = None,
                             on_progress: Callable[[], None] = lambda: None) -> None:
    """Fail closed unless the worktree's history and unsnapshotted entries are safe.

    The caller additionally verifies the dirty tracked/untracked tree against
    salvage, and rechecks database pins inside its deletion transaction. These
    filesystem/Git checks must run outside that transaction, before removal.
    """
    worktree = Path(job["worktree"]).resolve()
    _checkpoint(cancel, deadline)
    if not worktree.exists():
        return

    def git(repository: Path, *args: str) -> str:
        return _git(repository, "--no-replace-objects", *args,
                    cancel=cancel, deadline=deadline)

    common = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    removed = (worktree.resolve(), (Path(state_root) / "jobs" / job["job_id"]).resolve())
    if any(common.is_relative_to(path) for path in removed):
        raise ValueError("worktree Git object database is inside a directory being removed")
    head = git(worktree, "rev-parse", "--verify", "HEAD^{commit}")
    refs = git(common, "--git-dir=" + str(common), "for-each-ref",
               "--format=%(refname)", "--contains=" + head)
    if not any(ref.startswith(ALLOWED_REF_PREFIXES) for ref in refs.splitlines()):
        raise ValueError("worktree HEAD is not preserved by an allowed named ref (baseline included)")

    # The tag prefix protects initial whitespace from _git's .strip(); -z
    # preserves embedded newlines and Git path quoting cannot disguise names.
    ignored = git(worktree, "ls-files", "-t", "-z", "-o", "-i", "--exclude-standard", "--directory")
    for entry in ignored.split("\0"):
        _checkpoint(cancel, deadline)
        if entry and (not entry.startswith("? ") or not _regenerable_ignored(entry[2:])):
            raise ValueError(f"ignored worktree entry is not a regenerable cache: {entry[2:]!r}")

    _prove_no_nested_git(worktree, cancel, deadline, on_progress=on_progress)


class SalvageReachability:
    """Prove the recorded commit is held by a shared, named repository ref.

    A shared salvage ref is sufficient: removing a linked worktree removes
    neither the common object database nor its refs. Detached HEAD, reflogs,
    and objects with no named ref are insufficient. Every call makes a fresh
    proof outside store transactions; failures leave the artifact pinned.
    """

    def __init__(self, store: Store, state_root: str | Path, *,
                 cancel: threading.Event | None = None, deadline: float | None = None):
        self.store = store
        self.root = Path(state_root)
        self.cancel = cancel
        self.deadline = deadline

    def _git(self, repository: Path, *args: str) -> str:
        return _git(repository, "--no-replace-objects", *args,
                    cancel=self.cancel, deadline=self.deadline)

    @staticmethod
    def _matches(commit: Any, artifact: dict[str, Any]) -> bool:
        return (isinstance(commit, str)
                and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) is not None
                and hashlib.sha256(commit.encode()).hexdigest() == artifact.get("sha256"))

    def _receipt_commit(self, job: dict[str, Any], artifact: dict[str, Any]) -> str | None:
        attempt = self.store.one("SELECT seq FROM attempts WHERE attempt_id=? AND job_id=?",
                                 (artifact["attempt_id"], job["job_id"]))
        if attempt is None:
            return None
        directory = self.root / "jobs" / job["job_id"]
        receipt = attempt_dir(self.root, job["job_id"], attempt["seq"]) / "salvage.json"
        # Do not follow a swapped receipt or attempt directory outside this job.
        if directory.resolve() not in receipt.resolve().parents:
            return None
        _checkpoint(self.cancel, self.deadline)
        value = json.loads(read_regular(receipt, 64 * 1024))
        _checkpoint(self.cancel, self.deadline)
        result = value.get("result") if isinstance(value, dict) else None
        if not isinstance(result, dict):
            return None
        if (result.get("ref") or result.get("ref_name")) != artifact["path"]:
            return None
        commit = result.get("commit") or result.get("commit_sha")
        return commit if self._matches(commit, artifact) else None

    def __call__(self, artifact: dict[str, Any]) -> bool:
        _checkpoint(self.cancel, self.deadline)
        if artifact.get("role") != "salvage" or not artifact.get("path", "").startswith("refs/subfleet-salvage/"):
            return False
        job = self.store.one("SELECT j.* FROM jobs j JOIN attempts a USING(job_id) WHERE a.attempt_id=?",
                             (artifact["attempt_id"],))
        if job is None or Path(job["job_id"]).name != job["job_id"] or job["job_id"] in (".", ".."):
            return False
        try:
            # Prefer the caller's repository; a removed caller worktree can
            # still be proved through the job's linked worktree when it exists.
            common = None
            for source in dict.fromkeys((job.get("workdir"), job.get("worktree"))):
                if not source:
                    continue
                try:
                    common = Path(self._git(Path(source), "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
                    break
                except (OSError, subprocess.SubprocessError):
                    _checkpoint(self.cancel, self.deadline)
            if common is None:
                return False
            removed = [self.root / "jobs" / job["job_id"]]
            if not job.get("in_place") and job.get("worktree"):
                removed.append(Path(job["worktree"]))
            if any(common.is_relative_to(path.resolve()) for path in removed):
                return False

            # Resolve in the common Git directory, never in a per-worktree
            # namespace that would disappear with the allocated directory.
            gitdir = "--git-dir=" + str(common)
            try:
                commit = self._git(common, gitdir, "rev-parse", "--verify", artifact["path"] + "^{commit}")
                if self._matches(commit, artifact):
                    return True
            except (OSError, subprocess.SubprocessError):
                _checkpoint(self.cancel, self.deadline)

            # A renamed/moved salvage ref can still be held by a branch or
            # another shared ref. The receipt recovers the ID, but only a
            # digest match plus named reachability authorizes retention.
            commit = self._receipt_commit(job, artifact)
            if commit is None:
                return False
            refs = self._git(common, gitdir, "for-each-ref", "--format=%(refname)", "--contains=" + commit)
            return any(ref.startswith(ALLOWED_REF_PREFIXES) for ref in refs.splitlines())
        except (OSError, ValueError, subprocess.SubprocessError):
            _checkpoint(self.cancel, self.deadline)
            return False
