"""Bounded job retention, with filesystem work outside store transactions (C-8.4).

Detached jobs and conversation turn jobs are pruned against separate budgets
(C-8.4, C-26.12): each pool drops its own oldest unpinned jobs until its own
count and byte limits hold. A turn job is also kept for `turn_keep_days` after
it ends, and while the conversation service still needs it (its message is not
terminal, its conversation is blocked, or a live runner reads its attempt):
the service answers that through `pins`, which is asked again inside each
delete transaction, like every other pin.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from .contracts import (
    RETENTION_MAX_BYTES, RETENTION_MAX_JOBS, TURN_RETENTION_KEEP_DAYS, TURN_RETENTION_MAX_BYTES,
    TURN_RETENTION_MAX_JOBS,
)
from .store import Store, utc_now

_TERMINAL = {"succeeded", "failed", "cancelled", "lost"}


class _Interrupted(Exception):
    pass


def _checkpoint(cancel: threading.Event | None, deadline: float | None) -> None:
    if cancel is not None and cancel.is_set():
        raise _Interrupted("cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise _Interrupted("deadline")


def _file_sizes(path: Path):
    """Yield after each filesystem operation, keeping the walk resumable.

    Checkpoints belong to the consumer: throwing into this generator would
    close it and force the next pass to start the same large tree over again.
    os.walk closes each scandir before yielding, so no directory descriptors
    are held between maintenance passes.
    """
    if path.is_symlink() or not path.is_dir():
        yield path.lstat().st_size if path.exists() or path.is_symlink() else 0
        return

    def unreadable(error: OSError) -> None:
        raise error

    for directory, dirnames, filenames in os.walk(path, followlinks=False, onerror=unreadable):
        yield 0
        for name in dirnames:
            entry = Path(directory) / name
            try:
                metadata = entry.lstat()
                yield metadata.st_size if stat.S_ISLNK(metadata.st_mode) else 0
            except FileNotFoundError:
                pass
        for name in filenames:
            try:
                yield (Path(directory) / name).lstat().st_size
            except FileNotFoundError:
                pass


@dataclass
class _SizeScan:
    iterator: Any
    size: int = 0
    complete: bool = False

    def advance(self, cancel, deadline) -> int:
        while not self.complete:
            _checkpoint(cancel, deadline)
            try:
                self.size += next(self.iterator)
            except StopIteration:
                self.complete = True
        return self.size


def _size(path: Path, *, cancel: threading.Event | None = None, deadline: float | None = None,
          scan: _SizeScan | None = None) -> int:
    """Count files without following links; a supplied scan survives interruption."""
    _checkpoint(cancel, deadline)
    return (scan or _SizeScan(_file_sizes(path))).advance(cancel, deadline)


@dataclass
class _Measurement:
    signature: tuple
    scans: list[_SizeScan]

    @property
    def complete(self) -> bool:
        return all(scan.complete for scan in self.scans)

    @property
    def size(self) -> int:
        return sum(scan.size for scan in self.scans)


@dataclass
class _RetentionCache:
    # Scoped to one Store and state root, used by the single retention worker.
    # No schema change or filesystem I/O is needed to save interrupted work.
    measurements: dict[str, _Measurement] = field(default_factory=dict)
    # Failed Git proofs/removals must not spend every deadline on the same
    # oldest jobs. Remember their last attempt, never a cached permission.
    retries: dict[str, tuple[tuple, float]] = field(default_factory=dict)


def _signature(job):
    return tuple(job.get(key) for key in ("state", "finished_at", "worktree", "workdir", "sandbox", "in_place"))


def _contains(value: Any, job_id: str) -> bool:
    if isinstance(value, str):
        return value == job_id or value.startswith(job_id + "/a")
    if isinstance(value, dict):
        return any(_contains(item, job_id) for item in value.values())
    if isinstance(value, list):
        return any(_contains(item, job_id) for item in value)
    return False


def _pins(store: Store, explicit: set[str], landed_salvage: set[int], *,
          pins: Callable[[], Iterable[str]] | None = None, turn_keep_s: float = 0) -> set[str]:
    """Read only database evidence; safe to recheck inside the delete transaction.

    `pins` is the conversation service's evidence (C-26.12); it reads the
    conversation store and the service's live runners, never this store's
    transaction state, so it is as safe to ask inside a transaction.
    """
    protected = set(explicit)
    if pins is not None:
        protected.update(pins())
    kept_since = (datetime.now(UTC) - timedelta(seconds=turn_keep_s)).isoformat(timespec="milliseconds")
    for row in store.query("SELECT job_id,kind,state,finished_at FROM jobs"):
        if row["state"] not in _TERMINAL or row["kind"] == "gate-review":
            protected.add(row["job_id"])
        elif row["kind"] == "turn" and turn_keep_s > 0 and not _ended_before(row["finished_at"], kept_since):
            protected.add(row["job_id"])   # C-26.12: kept for days after it ends
    queries = (
        "SELECT DISTINCT job_id FROM attempts WHERE state='quarantined' OR state IN ('reserved','starting','running','finalizing')",
        # C-8.4, IR-17: an unread notice pins its job only when some session can
        # still read it; a notice with no session is never delivered, so it would
        # pin its job for ever.
        "SELECT DISTINCT job_id FROM notices WHERE state IN ('pending','offered') "
        "AND session_id IS NOT NULL AND session_id <> ''",
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


def _ended_before(finished_at: str | None, cutoff: str) -> bool:
    """Whether a job ended before `cutoff` (both UTC ISO); an unknown end has not."""
    if not finished_at:
        return False
    try:
        ended = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=UTC)
        return ended < datetime.fromisoformat(cutoff)
    except ValueError:
        return False


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
                     cancel: threading.Event | None = None, deadline: float | None = None,
                     before_remove: Callable[[], None] = lambda: None) -> None:
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
    _checkpoint(cancel, deadline)
    before_remove()
    git(source, "worktree", "remove", *options, "--", str(worktree))


def maintenance(store: Store, state_root: str | Path, *, max_jobs: int = RETENTION_MAX_JOBS,
                max_bytes: int = RETENTION_MAX_BYTES, turn_max_jobs: int = TURN_RETENTION_MAX_JOBS,
                turn_max_bytes: int = TURN_RETENTION_MAX_BYTES,
                turn_keep_s: float = TURN_RETENTION_KEEP_DAYS * 86400,
                pins: Callable[[], Iterable[str]] | None = None, referenced_job_ids: Iterable[str] = (),
                salvage_referenced_elsewhere: Callable[[dict[str, Any]], bool] | None = None,
                cancel: threading.Event | None = None, deadline: float | None = None) -> dict[str, Any]:
    """Prune oldest unpinned terminal jobs until each pool's limits hold.

    Detached jobs are held to `max_jobs`/`max_bytes` (C-8.4) and turn jobs to
    `turn_max_jobs`/`turn_max_bytes` (C-26.12); a pool over its budget never
    prunes the other. Salvage is conservatively pinned unless a caller proves
    its ref is held elsewhere. Explicit evidence ids let later gate
    implementations add pins without changing this module, and `pins` is asked
    again inside every delete transaction. A failed filesystem deletion is
    reported and audited; pruning never performs a subprocess, stat, or removal
    inside a tx. C-16.4 cancellation is cooperative between filesystem
    operations. A cancelled selection keeps its rows and any deletion lease for
    the next maintenance pass. The Store retains partial walks and completed
    terminal measurements in memory; a restart safely starts fresh. Count and
    measured-byte pressure trigger deletion before the complete scan finishes.
    """
    progress = {"pruned": [], "errors": [], "bytes_before": None, "bytes_after": None}
    try:
        return _maintenance(store, state_root, budgets={"detached": (max_jobs, max_bytes),
                                                        "turn": (turn_max_jobs, turn_max_bytes)},
                            turn_keep_s=turn_keep_s, pins=pins, referenced_job_ids=referenced_job_ids,
                            salvage_referenced_elsewhere=salvage_referenced_elsewhere,
                            cancel=cancel, deadline=deadline, progress=progress)
    except _Interrupted as exc:
        jobs = store.list_jobs()
        # Filesystem removal may have stopped between stages, so remaining
        # bytes are unknown. This result protects the remaining rows for this
        # pass only; cached scans and committed deletions survive the retry.
        return {**progress, "protected": sorted(job["job_id"] for job in jobs),
                "bytes_after": None, "jobs_after": len(jobs), "interrupted": str(exc)}


def _pool(job: dict[str, Any]) -> str:
    return "turn" if job.get("kind") == "turn" else "detached"


def _maintenance(store, state_root, *, budgets, turn_keep_s, pins, referenced_job_ids,
                 salvage_referenced_elsewhere, cancel, deadline, progress):
    if any(limit < 0 for pair in budgets.values() for limit in pair) or turn_keep_s < 0:
        raise ValueError("retention limits must be nonnegative")
    _checkpoint(cancel, deadline)
    state_root = Path(state_root).resolve()
    root = state_root / "jobs"
    jobs = list(reversed(store.list_jobs()))   # oldest first, for both scanning and pruning
    cache_key = str(state_root)
    caches = getattr(store, "_retention_caches", None)
    if caches is None:
        caches = store._retention_caches = {}
    cache = caches.setdefault(cache_key, _RetentionCache())
    by_id = {job["job_id"]: job for job in jobs}
    cache.retries = {identity: retry for identity, retry in cache.retries.items() if identity in by_id}
    # Terminal directories are immutable once their attempts have settled.
    # Never reuse a completed live measurement, or one of a changed job.
    unsettled = {row["job_id"] for row in store.query(
        "SELECT DISTINCT job_id FROM attempts WHERE state IN ('reserved','starting','running','finalizing','quarantined')")}
    for identity, measurement in list(cache.measurements.items()):
        job = by_id.get(identity)
        if (job is None or measurement.signature != _signature(job)
                or (measurement.complete and (job["state"] not in _TERMINAL or identity in unsettled))):
            del cache.measurements[identity]
    sizes = {identity: m.size for identity, m in cache.measurements.items() if m.complete}
    initial_sizes = dict(sizes)
    reused_sizes = set(sizes)
    errors = progress["errors"]
    explicit = set(referenced_job_ids)
    measurement_errors = set()
    artifacts = store.query("SELECT r.*,a.job_id FROM artifacts r JOIN attempts a USING(attempt_id) WHERE r.role='salvage'")
    salvage_by_job = {}
    for artifact in artifacts:
        salvage_by_job.setdefault(artifact["job_id"], []).append(artifact)
    landed = set()

    def pinned():
        return _pins(store, explicit, landed, pins=pins, turn_keep_s=turn_keep_s)

    # Separate salvage from other pins so expensive Git proofs are made only
    # for candidates under pressure, never as a precondition for all pruning.
    other_pins = _pins(store, explicit, {a["artifact_id"] for a in artifacts},
                       pins=pins, turn_keep_s=turn_keep_s)
    protected = other_pins | set(salvage_by_job)
    counts = {name: sum(_pool(job) == name for job in jobs) for name in budgets}
    totals = {name: sum(sizes.get(job["job_id"], 0) for job in jobs if _pool(job) == name) for name in budgets}
    progress["pools"] = {name: {"jobs_before": counts[name], "bytes_before": None,
                                "max_jobs": pair[0], "max_bytes": pair[1]} for name, pair in budgets.items()}
    pruned = progress["pruned"]
    removed = set()

    def account():
        # Unknown bytes are never advertised as zero. Measured totals are a
        # lower bound, sufficient to prove pressure before the whole scan ends.
        for name in budgets:
            pool_jobs = [job for job in jobs if _pool(job) == name]
            complete_before = all(job["job_id"] in initial_sizes for job in pool_jobs)
            complete_after = all(job["job_id"] in sizes for job in pool_jobs if job["job_id"] not in removed)
            progress["pools"][name].update(
                bytes_before=sum(initial_sizes[j["job_id"]] for j in pool_jobs) if complete_before else None,
                jobs_after=counts[name], bytes_after=totals[name] if complete_after else None,
                measured_bytes=totals[name])
        progress["bytes_before"] = sum(initial_sizes.values()) if len(initial_sizes) == len(jobs) else None
        progress["bytes_after"] = sum(totals.values()) if all(
            p["bytes_after"] is not None for p in progress["pools"].values()) else None

    def measure(job):
        identity = job["job_id"]
        if identity in sizes or identity in measurement_errors:
            return
        if Path(identity).name != identity or identity in (".", ".."):
            error = "invalid job directory name"
        else:
            try:
                measurement = cache.measurements.get(identity)
                if measurement is None:
                    worktree = _owned_worktree(job, state_root)
                    paths = [root / identity] + ([worktree] if worktree is not None else [])
                    measurement = _Measurement(_signature(job), [_SizeScan(_file_sizes(path)) for path in paths])
                    cache.measurements[identity] = measurement
                for scan in measurement.scans:
                    _size(root / identity, cancel=cancel, deadline=deadline, scan=scan)
                sizes[identity] = measurement.size
                initial_sizes[identity] = measurement.size
                totals[_pool(job)] += measurement.size
                account()
                return
            except (OSError, ValueError) as exc:
                error = str(exc)
        errors.append({"job_id": identity, "error": error})
        cache.measurements.pop(identity, None)
        explicit.add(identity)
        measurement_errors.add(identity)
        protected.add(identity)

    def candidate_evidence(job):
        return (_signature(job), tuple((a["artifact_id"], a["path"], a["sha256"])
                                      for a in salvage_by_job.get(job["job_id"], [])))

    # Try fresh candidates oldest-first, then known failures least-recently
    # tried first. This stays fair with arbitrarily many slow Git failures and
    # any worker backoff, while promptly noticing refs repaired between passes.
    def retry_order(job):
        previous = cache.retries.get(job["job_id"])
        if previous is not None and previous[0] == candidate_evidence(job):
            return (True, previous[1])
        return (False, 0)

    ordered = sorted(jobs, key=retry_order)
    candidates = {name: iter([job for job in ordered if _pool(job) == name]) for name in budgets}
    exhausted = set()

    def drain():
        for pool, (max_jobs, max_bytes) in budgets.items():
            while pool not in exhausted and (counts[pool] > max_jobs or totals[pool] > max_bytes):
                _checkpoint(cancel, deadline)
                job = next(candidates[pool], None)
                if job is None:
                    exhausted.add(pool)
                    break
                identity = job["job_id"]
                if identity in other_pins or identity in explicit:
                    continue
                salvage_artifacts = salvage_by_job.get(identity, [])
                if salvage_artifacts and salvage_referenced_elsewhere is None:
                    continue   # no expensive proof was attempted to defer
                evidence = candidate_evidence(job)
                def defer():
                    cache.retries[identity] = (evidence, time.monotonic())

                if salvage_artifacts:
                    try:
                        for artifact in salvage_artifacts:
                            _checkpoint(cancel, deadline)
                            if salvage_referenced_elsewhere is not None and salvage_referenced_elsewhere(artifact):
                                landed.add(artifact["artifact_id"])
                    except _Interrupted:
                        defer()
                        raise
                    if any(artifact["artifact_id"] not in landed for artifact in salvage_artifacts):
                        defer()
                        continue
                    protected.discard(identity)
                # Even count-based selection validates the candidate's tree;
                # unreadable trees remain pinned, without sizing every job.
                measure(job)
                if identity in explicit:
                    continue
                worktree = _owned_worktree(job, state_root)
                lease_key = f"worktree:{worktree}" if worktree is not None else None
                lease_holder = f"retention:{identity}"
                with store.transaction("retention.selected", job_id=identity) as conn:
                    _checkpoint(cancel, deadline)
                    if identity in pinned():
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
                def invalidate():
                    cache.measurements.pop(identity, None)

                try:
                    _remove_worktree(job, state_root, salvage_artifacts, worktree, cancel=cancel, deadline=deadline,
                                     before_remove=invalidate)
                except _Interrupted:
                    # A scan that just finished must get a fresh deadline for
                    # read-only Git checks before a slow candidate is deferred.
                    if identity in reused_sizes or identity not in cache.measurements:
                        defer()
                    raise
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    error = {"job_id": identity, "error": str(exc)}
                    errors.append(error)
                    protected.add(identity)
                    defer()
                    # Audit before any further interruptible work. A failed
                    # removal must remain visible even at the deadline.
                    with store.transaction("retention.worktree_error", job_id=identity, data=error) as conn:
                        conn.execute("DELETE FROM leases WHERE lease_key=? AND holder=?", (lease_key, lease_holder))
                    # Git can fail after deleting some files. Its old size is
                    # no longer a lower bound on bytes still present.
                    if identity not in cache.measurements:
                        totals[pool] -= sizes.pop(identity)
                    account()
                    continue
                _checkpoint(cancel, deadline)
                directory = root / identity
                invalidate()   # all read-only worktree checks have now finished
                try:
                    if directory.is_symlink():
                        directory.unlink()
                    elif directory.exists():
                        shutil.rmtree(directory)
                except OSError as exc:
                    error = {"job_id": identity, "error": str(exc)}
                    errors.append(error)
                    protected.add(identity)
                    with store.transaction("retention.lease_released", job_id=identity) as conn:
                        conn.execute("DELETE FROM leases WHERE lease_key=? AND holder=?", (lease_key, lease_holder))
                    store.add_event("retention.remove_error", job_id=identity, data=error)
                    try:
                        remaining = _size(directory, cancel=cancel, deadline=deadline)
                        if worktree is not None:
                            remaining += _size(worktree, cancel=cancel, deadline=deadline)
                    except OSError:
                        totals[pool] -= sizes.pop(identity)
                        measurement_errors.add(identity)
                    else:
                        totals[pool] += remaining - sizes[identity]
                        sizes[identity] = remaining
                    account()
                    continue
                # Files are gone even if the final pin recheck retains the
                # row. Do not charge those bytes to later deletion decisions.
                reclaimed = sizes[identity]
                totals[pool] -= reclaimed
                sizes[identity] = 0
                account()
                with store.transaction("retention.pruned", job_id=identity,
                                       data={"bytes": reclaimed, "pool": pool}) as conn:
                    _checkpoint(cancel, deadline)
                    conn.execute("DELETE FROM leases WHERE lease_key=? AND holder=?", (lease_key, lease_holder))
                    if identity in pinned():
                        protected.add(identity)
                        continue
                    for table in ("artifacts", "readings"):
                        conn.execute(f"DELETE FROM {table} WHERE attempt_id IN (SELECT attempt_id FROM attempts WHERE job_id=?)", (identity,))
                    for table in ("notices", "decisions", "attempts", "jobs"):
                        conn.execute(f"DELETE FROM {table} WHERE job_id=?", (identity,))
                    _checkpoint(cancel, deadline)
                pruned.append(identity)
                cache.retries.pop(identity, None)
                removed.add(identity)
                counts[pool] -= 1
                totals[pool] -= sizes[identity]
                account()

    account()
    # Count pressure and bytes already measured by previous passes can prune
    # immediately. Each new measurement can establish more byte pressure.
    drain()
    for job in jobs:
        _checkpoint(cancel, deadline)
        if job["job_id"] not in removed:
            measure(job)
            drain()
    account()
    return {**progress, "protected": sorted(protected), "jobs_after": sum(counts.values())}
