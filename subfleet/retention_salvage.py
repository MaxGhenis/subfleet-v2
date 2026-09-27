"""Read-only proof that deleting a job cannot delete its salvage (C-8.4)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import threading
from typing import Any

from .contracts import attempt_dir
from .retention import _checkpoint, _git
from .sessions.transcripts import read_regular
from .store import Store


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
            return any(ref.startswith("refs/")
                       and not ref.startswith(("refs/bisect/", "refs/worktree/", "refs/rewritten/"))
                       for ref in refs.splitlines())
        except (OSError, ValueError, subprocess.SubprocessError):
            _checkpoint(self.cancel, self.deadline)
            return False
