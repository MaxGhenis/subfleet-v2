"""Bounded job retention, with filesystem work outside store transactions (C-8.4)."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .contracts import RETENTION_MAX_BYTES, RETENTION_MAX_JOBS
from .store import Store

_TERMINAL = {"succeeded", "failed", "cancelled", "lost"}


def _size(path: Path) -> int:
    """Count regular files without following workdir or artifact symlinks."""
    size = 0
    if path.is_symlink() or not path.is_dir():
        return path.lstat().st_size if path.exists() or path.is_symlink() else 0
    for directory, _, filenames in os.walk(path, followlinks=False):
        for name in filenames:
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


def maintenance(store: Store, state_root: str | Path, *, max_jobs: int = RETENTION_MAX_JOBS,
                max_bytes: int = RETENTION_MAX_BYTES, referenced_job_ids: Iterable[str] = (),
                salvage_referenced_elsewhere: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any]:
    """Prune oldest unpinned terminal jobs until both C-8.4 limits hold.

    Salvage is conservatively pinned unless a caller proves its ref is held
    elsewhere. Explicit evidence ids let later gate implementations add pins
    without changing this module. A failed filesystem deletion is reported and
    audited; pruning never performs a subprocess, stat, or removal inside a tx.
    """
    if max_jobs < 0 or max_bytes < 0:
        raise ValueError("retention limits must be nonnegative")
    root = Path(state_root).resolve() / "jobs"
    jobs = store.list_jobs()
    sizes = {}
    errors = []
    for job in jobs:
        identity = job["job_id"]
        if Path(identity).name != identity or identity in (".", ".."):
            errors.append({"job_id": identity, "error": "invalid job directory name"})
            continue
        try:
            sizes[identity] = _size(root / identity)
        except OSError as exc:
            errors.append({"job_id": identity, "error": str(exc)})
    explicit = set(referenced_job_ids) | {error["job_id"] for error in errors}
    landed = set()
    if salvage_referenced_elsewhere is not None:
        for artifact in store.query("SELECT * FROM artifacts WHERE role='salvage'"):
            if salvage_referenced_elsewhere(artifact):
                landed.add(artifact["artifact_id"])
    protected = _pins(store, explicit, landed)
    count, total = len(jobs), sum(sizes.values())
    before = total
    pruned = []
    for job in reversed(jobs):
        if count <= max_jobs and total <= max_bytes:
            break
        identity = job["job_id"]
        if identity in protected:
            continue
        with store.transaction("retention.pruned", job_id=identity, data={"bytes": sizes.get(identity, 0)}) as conn:
            if identity in _pins(store, explicit, landed):
                protected.add(identity)
                continue
            for table in ("artifacts", "readings"):
                conn.execute(f"DELETE FROM {table} WHERE attempt_id IN (SELECT attempt_id FROM attempts WHERE job_id=?)", (identity,))
            for table in ("notices", "decisions", "attempts", "jobs"):
                conn.execute(f"DELETE FROM {table} WHERE job_id=?", (identity,))
        directory = root / identity
        try:
            if directory.is_symlink():
                directory.unlink()
            elif directory.exists():
                shutil.rmtree(directory)
        except OSError as exc:
            error = {"job_id": identity, "error": str(exc)}
            errors.append(error)
            store.add_event("retention.remove_error", job_id=identity, data=error)
        pruned.append(identity)
        count -= 1
        total -= sizes.get(identity, 0)
    return {"pruned": pruned, "protected": sorted(protected), "bytes_before": before,
            "bytes_after": total, "jobs_after": count, "errors": errors}
