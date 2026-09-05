"""Bounded job retention, with filesystem work outside store transactions (C-8.4)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from functools import partial
from pathlib import Path
from typing import Any

from .contracts import RETENTION_MAX_BYTES, RETENTION_MAX_JOBS
from .store import Store, utc_now

_TERMINAL = {"succeeded", "failed", "cancelled", "lost"}


class _Interrupted(Exception):
    pass


def _checkpoint(cancel: threading.Event | None, deadline: float | None) -> None:
    if cancel is not None and cancel.is_set():
        raise _Interrupted("cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise _Interrupted("deadline")


def _size(path: Path, *, cancel: threading.Event | None = None, deadline: float | None = None) -> int:
    """Count regular files without following workdir or artifact symlinks."""
    _checkpoint(cancel, deadline)
    size = 0
    if path.is_symlink() or not path.is_dir():
        return path.lstat().st_size if path.exists() or path.is_symlink() else 0

    def unreadable(error: OSError) -> None:
        raise error

    for directory, dirnames, filenames in os.walk(path, followlinks=False, onerror=unreadable):
        _checkpoint(cancel, deadline)
        # os.walk puts links to directories in dirnames even when followlinks
        # is false. Their own bytes count, but their external contents do not.
        links = [name for name in dirnames if (Path(directory) / name).is_symlink()]
        for name in [*filenames, *links]:
            _checkpoint(cancel, deadline)
            try:
                size += (Path(directory) / name).lstat().st_size
            except FileNotFoundError:
                pass
    return size


def _contains(value: Any, job_id: str) -> bool:
    if isinstance(value, str):
        return value == job_id or value.startswith(job_id + "/a")
    if isinstance(value, dict):
        return any(_contains(item, job_id) for item in value.values())
    if isinstance(value, list):
        return any(_contains(item, job_id) for item in value)
    return False


def _pins(store: Store, explicit: set[str], landed_salvage: set[int]) -> set[str]:
    """Read only database evidence; safe to recheck inside the delete transaction."""
    protected = set(explicit)
    for row in store.query("SELECT job_id,kind,state FROM jobs"):
        if row["state"] not in _TERMINAL or row["kind"] == "gate-review":
            protected.add(row["job_id"])
    queries = (
        "SELECT DISTINCT job_id FROM attempts WHERE state='quarantined' OR state IN ('reserved','starting','running','finalizing')",
        "SELECT DISTINCT job_id FROM notices WHERE state IN ('pending','offered')",
        "SELECT DISTINCT parent_job_id AS job_id FROM jobs WHERE parent_job_id IS NOT NULL",
        "SELECT j.job_id FROM jobs j JOIN leases l ON l.holder=j.job_id",
        "SELECT a.job_id FROM attempts a JOIN leases l ON l.holder=a.attempt_id",
        "SELECT j.job_id FROM jobs j JOIN leases l ON l.lease_key='worktree:' || j.worktree "
        "WHERE l.holder != 'retention:' || j.job_id",
    )
    for sql in queries:
        protected.update(row["job_id"] for row in store.query(sql))
    for row in store.query("SELECT r.artifact_id,a.job_id FROM artifacts r JOIN attempts a USING(attempt_id) WHERE r.role='salvage'"):
        if row["artifact_id"] not in landed_salvage:
            protected.add(row["job_id"])
    actions = store.query("SELECT subject,request_json,result_json FROM actions WHERE kind LIKE '%gate%' OR kind LIKE '%merge%'")
    jobs = store.query("SELECT job_id FROM jobs")
    for action in actions:
        evidence = [action["subject"]]
        for key in ("request_json", "result_json"):
            try:
                evidence.append(json.loads(action[key] or "null"))
            except (TypeError, ValueError):
                # Unknown evidence remains a pin for all jobs until reconciled.
                protected.update(row["job_id"] for row in jobs)
        for row in jobs:
            if _contains(evidence, row["job_id"]):
                protected.add(row["job_id"])
    return protected


def _owned_worktree(job: dict[str, Any], state_root: Path) -> Path | None:
    """C-13.4: only daemon-allocated paths strictly below worktrees are owned."""
    if job.get("in_place") or job.get("sandbox") != "workspace-write" or not job.get("worktree"):
        return None
    allocated_root = state_root / "worktrees"
    # A symlinked container must not turn an external directory into our owner
    # boundary. Individual paths are resolved to reject escapes the same way.
    if allocated_root.resolve() != allocated_root:
        raise ValueError("allocated worktree root is a symlink")
    worktree = Path(job["worktree"]).resolve()
    if worktree == allocated_root or allocated_root not in worktree.parents:
        raise ValueError("worktree path is outside the daemon's allocated worktrees")
    return worktree


def _git(worktree: Path, *args: str, env: dict[str, str] | None = None,
         cancel: threading.Event | None = None, deadline: float | None = None) -> str:
    _checkpoint(cancel, deadline)
    timeout = 15 if deadline is None else max(.001, min(15, deadline - time.monotonic()))
    result = subprocess.run(["git", "-C", str(worktree), *args], env=env,
                            capture_output=True, text=True, timeout=timeout, check=False)
    _checkpoint(cancel, deadline)
    if result.returncode:
        raise OSError(f"git {args[0]} failed while inspecting or removing allocated worktree")
    return result.stdout.strip()


def _remove_worktree(job: dict[str, Any], state_root: Path,
                     salvage_artifacts: list[dict[str, Any]], expected_worktree: Path | None, *,
                     cancel: threading.Event | None = None, deadline: float | None = None) -> None:
    """Verify preservation, then remove both the owned tree and Git registration.

    A forced removal additionally requires a recorded, existing salvage ref
    whose tree exactly matches the current files. This prevents later operator
    edits from being discarded merely because an earlier salvage row exists.
    All Git commands and temporary-index work run outside store transactions.
    """
    _checkpoint(cancel, deadline)
    git = partial(_git, cancel=cancel, deadline=deadline)
    worktree = _owned_worktree(job, state_root)
    if worktree != expected_worktree:
        raise ValueError("allocated worktree path changed during retention")
    if worktree is None:
        return
    source = worktree if worktree.exists() else Path(job["workdir"])
    registered = any(line.startswith("worktree ") and Path(line[9:]).resolve() == worktree
                     for line in git(source, "worktree", "list", "--porcelain").splitlines())
    if not registered:
        if worktree.exists():
            raise ValueError("allocated path is not a registered Git worktree")
        return
    dirty = bool(worktree.exists() and git(worktree, "status", "--porcelain=v1", "--untracked-files=all"))
    if dirty:
        if not salvage_artifacts:
            raise ValueError("dirty allocated worktree has no recorded salvage snapshot")
        with tempfile.TemporaryDirectory(prefix="retention-index-", dir=state_root) as temporary:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
            git(worktree, "read-tree", "HEAD", env=env)
            git(worktree, "add", "-A", env=env)
            current_tree = git(worktree, "write-tree", env=env)
        preserved = False
        for artifact in salvage_artifacts:
            ref = artifact["path"]
            if not ref.startswith("refs/subfleet-salvage/"):
                continue
            try:
                preserved = git(worktree, "rev-parse", "--verify", ref + "^{tree}") == current_tree
            except OSError:
                continue
            if preserved:
                break
        if not preserved:
            raise ValueError("dirty allocated worktree is not preserved by an existing salvage ref")
    options = ("--force",) if dirty else ()
    git(source, "worktree", "remove", *options, "--", str(worktree))


def maintenance(store: Store, state_root: str | Path, *, max_jobs: int = RETENTION_MAX_JOBS,
                max_bytes: int = RETENTION_MAX_BYTES, referenced_job_ids: Iterable[str] = (),
                salvage_referenced_elsewhere: Callable[[dict[str, Any]], bool] | None = None,
                cancel: threading.Event | None = None, deadline: float | None = None) -> dict[str, Any]:
    """Prune oldest unpinned terminal jobs until both C-8.4 limits hold.

    Salvage is conservatively pinned unless a caller proves its ref is held
    elsewhere. Explicit evidence ids let later gate implementations add pins
    without changing this module. A failed filesystem deletion is reported and
    audited; pruning never performs a subprocess, stat, or removal inside a tx.
    C-16.4 cancellation is cooperative between filesystem operations. A cancelled
    selection keeps its rows and any deletion lease for the next maintenance pass.
    """
    progress = {"pruned": [], "errors": [], "bytes_before": None, "bytes_after": None}
    try:
        return _maintenance(store, state_root, max_jobs=max_jobs, max_bytes=max_bytes,
                            referenced_job_ids=referenced_job_ids,
                            salvage_referenced_elsewhere=salvage_referenced_elsewhere,
                            cancel=cancel, deadline=deadline, progress=progress)
    except _Interrupted as exc:
        jobs = store.list_jobs()
        # Filesystem removal may have stopped between stages, so the remaining
        # bytes are unknown until the next pass. Conservatively pin every row.
        return {**progress, "protected": sorted(job["job_id"] for job in jobs),
                "bytes_after": None, "jobs_after": len(jobs), "interrupted": str(exc)}


def _maintenance(store, state_root, *, max_jobs, max_bytes, referenced_job_ids,
                 salvage_referenced_elsewhere, cancel, deadline, progress):
    if max_jobs < 0 or max_bytes < 0:
        raise ValueError("retention limits must be nonnegative")
    _checkpoint(cancel, deadline)
    state_root = Path(state_root).resolve()
    root = state_root / "jobs"
    jobs = store.list_jobs()
    sizes = {}
    errors = progress["errors"]
    for job in jobs:
        _checkpoint(cancel, deadline)
        identity = job["job_id"]
        if Path(identity).name != identity or identity in (".", ".."):
            errors.append({"job_id": identity, "error": "invalid job directory name"})
            continue
        try:
            sizes[identity] = _size(root / identity, cancel=cancel, deadline=deadline)
            worktree = _owned_worktree(job, state_root)
            if worktree is not None:
                sizes[identity] += _size(worktree, cancel=cancel, deadline=deadline)
        except (OSError, ValueError) as exc:
            errors.append({"job_id": identity, "error": str(exc)})
    explicit = set(referenced_job_ids) | {error["job_id"] for error in errors}
    landed = set()
    if salvage_referenced_elsewhere is not None:
        for artifact in store.query("SELECT * FROM artifacts WHERE role='salvage'"):
            _checkpoint(cancel, deadline)
            if salvage_referenced_elsewhere(artifact):
                landed.add(artifact["artifact_id"])
    _checkpoint(cancel, deadline)
    protected = _pins(store, explicit, landed)
    count, total = len(jobs), sum(sizes.values())
    before = total
    progress["bytes_before"] = before
    pruned = progress["pruned"]
    for job in reversed(jobs):
        _checkpoint(cancel, deadline)
        if count <= max_jobs and total <= max_bytes:
            break
        identity = job["job_id"]
        if identity in protected:
            continue
        # Fence new in-place admission for the entire filesystem removal. The
        # dedicated holder survives a daemon crash and this pass can resume it.
        worktree = _owned_worktree(job, state_root)
        lease_key = f"worktree:{worktree}" if worktree is not None else None
        lease_holder = f"retention:{identity}"
        with store.transaction("retention.selected", job_id=identity) as conn:
            _checkpoint(cancel, deadline)
            if identity in _pins(store, explicit, landed):
                protected.add(identity)
                continue
            if lease_key is not None:
                current = conn.execute("SELECT holder FROM leases WHERE lease_key=?", (lease_key,)).fetchone()
                if current and current["holder"] != lease_holder:
                    protected.add(identity)
                    continue
                conn.execute("INSERT OR IGNORE INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                             (lease_key, lease_holder, utc_now()))
            _checkpoint(cancel, deadline)
        salvage_artifacts = store.query("SELECT r.* FROM artifacts r JOIN attempts a USING(attempt_id) "
                                        "WHERE a.job_id=? AND r.role='salvage'", (identity,))
        try:
            _remove_worktree(job, state_root, salvage_artifacts, worktree, cancel=cancel, deadline=deadline)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            error = {"job_id": identity, "error": str(exc)}
            errors.append(error)
            protected.add(identity)
            with store.transaction("retention.worktree_error", job_id=identity, data=error) as conn:
                _checkpoint(cancel, deadline)
                conn.execute("DELETE FROM leases WHERE lease_key=? AND holder=?", (lease_key, lease_holder))
                _checkpoint(cancel, deadline)
            continue
        _checkpoint(cancel, deadline)
        directory = root / identity
        try:
            if directory.is_symlink():
                directory.unlink()
            elif directory.exists():
                shutil.rmtree(directory)
        except OSError as exc:
            error = {"job_id": identity, "error": str(exc)}
            errors.append(error)
            protected.add(identity)
            # A partial removal must retain the job so a later pass can retry
            # it, and must not claim bytes that remain on disk were reclaimed.
            try:
                remaining = _size(directory, cancel=cancel, deadline=deadline)
                if worktree is not None:
                    remaining += _size(worktree, cancel=cancel, deadline=deadline)
            except OSError:
                remaining = sizes.get(identity, 0)
            total += remaining - sizes.get(identity, 0)
            sizes[identity] = remaining
            with store.transaction("retention.lease_released", job_id=identity) as conn:
                _checkpoint(cancel, deadline)
                conn.execute("DELETE FROM leases WHERE lease_key=? AND holder=?", (lease_key, lease_holder))
                _checkpoint(cancel, deadline)
            _checkpoint(cancel, deadline)
            store.add_event("retention.remove_error", job_id=identity, data=error)
            continue
        with store.transaction("retention.pruned", job_id=identity, data={"bytes": sizes.get(identity, 0)}) as conn:
            _checkpoint(cancel, deadline)
            conn.execute("DELETE FROM leases WHERE lease_key=? AND holder=?", (lease_key, lease_holder))
            if identity in _pins(store, explicit, landed):
                protected.add(identity)
                continue
            for table in ("artifacts", "readings"):
                conn.execute(f"DELETE FROM {table} WHERE attempt_id IN (SELECT attempt_id FROM attempts WHERE job_id=?)", (identity,))
            for table in ("notices", "decisions", "attempts", "jobs"):
                conn.execute(f"DELETE FROM {table} WHERE job_id=?", (identity,))
            _checkpoint(cancel, deadline)
        pruned.append(identity)
        count -= 1
        total -= sizes.get(identity, 0)
    return {"pruned": pruned, "protected": sorted(protected), "bytes_before": before,
            "bytes_after": total, "jobs_after": count, "errors": errors}
