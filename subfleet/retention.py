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
_RETRY_COOLDOWN = 900.0
_REMOVAL_RESERVE = 1.0  # reserve time for the journal, atomic renames and row transaction


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
    steps: int = 0

    def advance(self, cancel, deadline) -> int:
        while not self.complete:
            _checkpoint(cancel, deadline)
            try:
                self.size += next(self.iterator)
                self.steps += 1
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
    paths: tuple = ()

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


def _path_signature(paths):
    result = []
    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        result.append((str(path), info.st_dev, info.st_ino, info.st_mtime_ns))
    return tuple(result)


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
                     recovering: bool = False, on_progress: Callable[[], None] = lambda: None) -> str | None:
    """Read-only preflight. Actual retirement uses atomic, reversible renames."""
    from .retention_salvage import prove_worktree_preserved

    _checkpoint(cancel, deadline)
    def git(path, *args, **kwargs):
        return _git(path, "--no-replace-objects", *args,
                    cancel=cancel, deadline=deadline, **kwargs)
    worktree = _owned_worktree(job, state_root)
    if worktree != expected_worktree:
        raise ValueError("allocated worktree path changed during retention")
    if worktree is None or not worktree.exists():
        return None  # Both caller and worktree may have been removed already.
    if recovering and not (worktree / ".git").exists():
        # Old git-worktree-remove could unlink the gitfile before being killed.
        # Reconnect only an exact surviving registration, then run every proof
        # below. Never infer a safe HEAD from an arbitrary directory's contents.
        common = Path(git(Path(job["workdir"]), "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        if common.is_relative_to(worktree) or common.is_relative_to(state_root / "jobs" / job["job_id"]):
            raise ValueError("legacy worktree registration is not held outside the job")
        for registration in (common / "worktrees").iterdir():
            if registration.is_symlink() or not registration.is_dir():
                continue
            gitfile = registration / "gitdir"
            if gitfile.is_file() and Path(gitfile.read_text().strip()).resolve() == worktree / ".git":
                with (worktree / ".git").open("x") as stream:
                    stream.write(f"gitdir: {registration}\n")
                break
    registered = any(line.startswith("worktree ") and Path(line[9:]).resolve() == worktree
                     for line in git(worktree, "worktree", "list", "--porcelain").splitlines())
    if not registered:
        raise ValueError("allocated path is not a registered Git worktree")
    prove_worktree_preserved(job, state_root, salvage_artifacts, cancel=cancel, deadline=deadline,
                             on_progress=on_progress)
    common = git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    # Rebuild an independent index even for an apparently clean tree: status
    # can hide tracked changes marked skip-worktree or assume-unchanged.
    with tempfile.TemporaryDirectory(prefix="retention-index-", dir=state_root) as temporary:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        git(worktree, "read-tree", "HEAD", env=env)
        git(worktree, "add", "-A", env=env)
        current_tree = git(worktree, "write-tree", env=env)
    if current_tree == git(worktree, "rev-parse", "HEAD^{tree}"):
        return common
    if not salvage_artifacts and not recovering:
        raise ValueError("dirty allocated worktree has no recorded salvage snapshot")
    refs = [a["path"] for a in salvage_artifacts if a["path"].startswith("refs/subfleet-salvage/")]
    # A legacy removal lease is the durable intent from the previous code. It
    # can have deleted tracked files already, but surviving edits must still
    # match a preserved snapshot. Never waive HEAD/ignored/nested-repo checks.
    if recovering:
        refs.append("HEAD")
    for ref in refs:
        try:
            preserved_tree = git(worktree, "rev-parse", "--verify", ref + "^{tree}")
            if preserved_tree == current_tree:
                return common
            if recovering and not git(worktree, "diff-tree", "--no-commit-id", "-r",
                                      "--diff-filter=ACMRTUXB", "--name-only", preserved_tree, current_tree):
                return common
        except OSError:
            continue
    raise ValueError("dirty allocated worktree is not preserved by an existing salvage ref")


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
    operations. Renames through row commit form an uninterruptible retirement;
    journals recover process crashes and restore newly pinned directories.
    The Store retains partial walks and validated completed measurements across
    interrupted passes; a restart safely starts fresh. Count and
    measured-byte pressure trigger deletion before the complete scan finishes.
    """
    progress = {"pruned": [], "errors": [], "bytes_before": None, "bytes_after": None, "made_progress": False}
    try:
        return _maintenance(store, state_root, budgets={"detached": (max_jobs, max_bytes),
                                                        "turn": (turn_max_jobs, turn_max_bytes)},
                            turn_keep_s=turn_keep_s, pins=pins, referenced_job_ids=referenced_job_ids,
                            salvage_referenced_elsewhere=salvage_referenced_elsewhere,
                            cancel=cancel, deadline=deadline, progress=progress)
    except _Interrupted as exc:
        jobs = store.list_jobs()
        # Scanning or physical trash reclamation may be unfinished. Cached
        # scans and committed row deletions survive the retry.
        return {**progress, "protected": sorted(job["job_id"] for job in jobs),
                "bytes_after": None, "jobs_after": len(jobs), "interrupted": str(exc)}


def _pool(job: dict[str, Any]) -> str:
    return "turn" if job.get("kind") == "turn" else "detached"


def _maintenance(store, state_root, *, budgets, turn_keep_s, pins, referenced_job_ids,
                 salvage_referenced_elsewhere, cancel, deadline, progress):
    from . import retention_trash as trash

    if any(limit < 0 for pair in budgets.values() for limit in pair) or turn_keep_s < 0:
        raise ValueError("retention limits must be nonnegative")
    _checkpoint(cancel, deadline)
    state_root = Path(state_root).resolve()
    root = state_root / "jobs"
    errors = progress["errors"]
    explicit = set(referenced_job_ids)
    recovered = set()
    recovery_errors = set()
    # A committed lease is intent to finish even when pressure has disappeared.
    # Restore a journaled move before re-proving pins or file preservation.
    for lease in store.query("SELECT * FROM leases WHERE holder LIKE 'retention:%'"):
        identity = lease["holder"][len("retention:"):]
        job = store.get_job(identity)
        if job is None:
            store.release_leases(lease["holder"])
            continue
        recovered.add(identity)
        try:
            trash.restore(state_root, identity, _owned_worktree(job, state_root))
        except (OSError, ValueError) as exc:
            error = {"job_id": identity, "error": str(exc)}
            errors.append(error)
            explicit.add(identity)
            recovery_errors.add(identity)
            store.add_event("retention.recovery_error", job_id=identity, data=error)
    jobs = list(reversed(store.list_jobs()))
    caches = getattr(store, "_retention_caches", None)
    if caches is None:
        caches = store._retention_caches = {}
    cache = caches.setdefault(str(state_root), _RetentionCache())
    by_id = {job["job_id"]: job for job in jobs}
    cache.retries = {identity: retry for identity, retry in cache.retries.items()
                     if identity in by_id and time.monotonic() - retry[1] < _RETRY_COOLDOWN}
    unsettled = {row["job_id"] for row in store.query(
        "SELECT DISTINCT job_id FROM attempts WHERE state IN ('reserved','starting','running','finalizing','quarantined')")}
    for identity, measurement in list(cache.measurements.items()):
        job = by_id.get(identity)
        paths_unchanged = (measurement.paths and measurement.paths == _path_signature(
            [Path(item[0]) for item in measurement.paths]))
        if (job is None or measurement.signature != _signature(job) or not paths_unchanged
                or (measurement.complete and (job["state"] not in _TERMINAL or identity in unsettled))):
            del cache.measurements[identity]
    sizes = {identity: m.size for identity, m in cache.measurements.items() if m.complete}
    reused_sizes = set(sizes)
    measurement_errors = set()
    artifacts = store.query("SELECT r.*,a.job_id FROM artifacts r JOIN attempts a USING(attempt_id) WHERE r.role='salvage'")
    salvage_by_job = {}
    for artifact in artifacts:
        salvage_by_job.setdefault(artifact["job_id"], []).append(artifact)
    landed = set()

    def pinned():
        return _pins(store, explicit, landed, pins=pins, turn_keep_s=turn_keep_s)

    other_pins = _pins(store, explicit, {a["artifact_id"] for a in artifacts},
                       pins=pins, turn_keep_s=turn_keep_s)
    protected = other_pins | set(salvage_by_job)
    counts = {name: sum(_pool(job) == name for job in jobs) for name in budgets}
    totals = {name: sum(sizes.get(job["job_id"], 0) for job in jobs if _pool(job) == name) for name in budgets}
    before_totals = dict(totals)
    unknown_before = {name: sum(_pool(j) == name and j["job_id"] not in sizes for j in jobs) for name in budgets}
    unknown_after = dict(unknown_before)
    progress["pools"] = {name: {"jobs_before": counts[name], "bytes_before": None,
                                "max_jobs": pair[0], "max_bytes": pair[1]} for name, pair in budgets.items()}
    pruned = progress["pruned"]
    removed = set()

    def account():
        # Constant work per update: byte accounting is linear over the pass.
        for name in budgets:
            progress["pools"][name].update(
                bytes_before=before_totals[name] if not unknown_before[name] else None,
                jobs_after=counts[name], bytes_after=totals[name] if not unknown_after[name] else None,
                measured_bytes=totals[name])
        progress["bytes_before"] = sum(before_totals.values()) if not any(unknown_before.values()) else None
        progress["bytes_after"] = sum(totals.values()) if not any(unknown_after.values()) else None

    def report(identity, exc, kind="retention.worktree_error"):
        error = {"job_id": identity, "error": str(exc)}
        errors.append(error)
        protected.add(identity)
        store.add_event(kind, job_id=identity, data=error)

    def measure(job):
        identity = job["job_id"]
        if identity in sizes or identity in measurement_errors:
            return
        try:
            if Path(identity).name != identity or identity in (".", ".."):
                raise ValueError("invalid job directory name")
            measurement = cache.measurements.get(identity)
            if measurement is None:
                worktree = _owned_worktree(job, state_root)
                paths = [root / identity] + ([worktree] if worktree is not None else [])
                measurement = _Measurement(_signature(job), [_SizeScan(_file_sizes(path)) for path in paths],
                                           _path_signature(paths))
                cache.measurements[identity] = measurement
            steps = sum(scan.steps for scan in measurement.scans)
            try:
                for scan in measurement.scans:
                    _size(root / identity, cancel=cancel, deadline=deadline, scan=scan)
            finally:
                if sum(scan.steps for scan in measurement.scans) > steps:
                    progress["made_progress"] = True
            size = measurement.size
            sizes[identity] = size
            pool = _pool(job)
            totals[pool] += size
            before_totals[pool] += size
            unknown_before[pool] -= 1
            unknown_after[pool] -= 1
            account()
        except (OSError, ValueError) as exc:
            report(identity, exc, "retention.measure_error")
            cache.measurements.pop(identity, None)
            explicit.add(identity)
            measurement_errors.add(identity)

    def candidate_evidence(job):
        return (_signature(job), tuple((a["artifact_id"], a["path"], a["sha256"])
                                      for a in salvage_by_job.get(job["job_id"], [])))

    def retry_order(job):
        previous = cache.retries.get(job["job_id"])
        if previous is not None and previous[0] == candidate_evidence(job):
            return (True, previous[1])
        return (False, 0)

    def prune(job, *, recovering=False):
        identity = job["job_id"]
        holder = f"retention:{identity}"
        if identity in other_pins or identity in explicit:
            # A restored newly pinned job must not retain the maintenance lease.
            if recovering and identity not in recovery_errors:
                store.release_leases(holder)
            return
        salvage_artifacts = salvage_by_job.get(identity, [])
        if salvage_artifacts and salvage_referenced_elsewhere is None:
            if recovering:
                store.release_leases(holder)
            return
        evidence = candidate_evidence(job)

        def defer():
            cache.retries[identity] = (evidence, time.monotonic())

        if salvage_artifacts:
            try:
                for artifact in salvage_artifacts:
                    _checkpoint(cancel, deadline)
                    if salvage_referenced_elsewhere(artifact):
                        landed.add(artifact["artifact_id"])
            except _Interrupted:
                defer()
                store.release_leases(holder)
                raise
            if any(a["artifact_id"] not in landed for a in salvage_artifacts):
                defer()
                report(identity, "salvage commit is not provably held by an allowed named ref")
                store.release_leases(holder)
                return
            protected.discard(identity)
        measure(job)
        if identity in explicit:
            store.release_leases(holder)
            return
        try:
            worktree = _owned_worktree(job, state_root)
        except (OSError, ValueError) as exc:
            report(identity, exc)
            defer()
            store.release_leases(holder)
            return
        lease_key = f"worktree:{worktree}" if worktree is not None else holder
        selected = False
        try:
            with store.transaction("retention.selected", job_id=identity) as conn:
                _checkpoint(cancel, deadline)
                if identity in pinned():
                    protected.add(identity)
                    conn.execute("DELETE FROM leases WHERE holder=?", (holder,))
                    return
                current = conn.execute("SELECT holder FROM leases WHERE lease_key=?", (lease_key,)).fetchone()
                if current and current["holder"] != holder:
                    protected.add(identity)
                    return
                conn.execute("INSERT OR IGNORE INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                             (lease_key, holder, utc_now()))
                _checkpoint(cancel, deadline)
            selected = True
            common = _remove_worktree(job, state_root, salvage_artifacts, worktree,
                                      cancel=cancel, deadline=deadline, recovering=recovering,
                                      on_progress=lambda: progress.__setitem__("made_progress", True))
            _checkpoint(cancel, deadline)
            if deadline is not None and deadline - time.monotonic() < _REMOVAL_RESERVE:
                raise _Interrupted("deadline")
            target = trash.prepare(state_root, identity, worktree, common, sizes[identity])
            # The journal is now durable. Never observe cancellation/deadline
            # between the first rename and row commit (or a complete restore).
            trash.stage(state_root, identity, worktree, target)
            retained = False
            with store.transaction("retention.pruned", job_id=identity,
                                   data={"bytes": sizes[identity], "pool": _pool(job)}) as conn:
                if identity in pinned():
                    retained = True
                else:
                    for table in ("artifacts", "readings"):
                        conn.execute(f"DELETE FROM {table} WHERE attempt_id IN (SELECT attempt_id FROM attempts WHERE job_id=?)", (identity,))
                    for table in ("notices", "decisions", "attempts", "jobs"):
                        conn.execute(f"DELETE FROM {table} WHERE job_id=?", (identity,))
                    conn.execute("DELETE FROM leases WHERE holder=?", (holder,))
            if retained:
                trash.restore(state_root, identity, worktree)
                store.release_leases(holder)
                protected.add(identity)
                return
        except _Interrupted:
            if identity in reused_sizes:
                defer()
            store.release_leases(holder)
            raise
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            report(identity, exc)
            defer()
            if selected:
                try:
                    trash.restore(state_root, identity, worktree)
                except (OSError, ValueError) as restore_error:
                    report(identity, restore_error, "retention.recovery_error")
                    return  # durable journal/lease must survive a failed restore
            store.release_leases(holder)
            return
        cache.measurements.pop(identity, None)
        cache.retries.pop(identity, None)
        protected.discard(identity)
        removed.add(identity)
        pruned.append(identity)
        progress["made_progress"] = True
        pool = _pool(job)
        counts[pool] -= 1
        totals[pool] -= sizes.pop(identity)
        account()

    account()
    for identity in recovered:
        _checkpoint(cancel, deadline)
        prune(by_id[identity], recovering=True)
    trash.clean(store, state_root, checkpoint=partial(_checkpoint, cancel, deadline),
                git=partial(_git, cancel=cancel, deadline=deadline), progress=progress)
    ordered = sorted((j for j in jobs if j["job_id"] not in recovered), key=retry_order)
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
                prune(job)

    drain()
    for job in jobs:
        _checkpoint(cancel, deadline)
        if job["job_id"] not in removed:
            measure(job)
            drain()
    account()
    # Completed sizes are only a bridge across interrupted passes, never a
    # lifetime cache of terminal directories that an operator can still edit.
    cache.measurements.clear()
    trash.clean(store, state_root, checkpoint=partial(_checkpoint, cancel, deadline),
                git=partial(_git, cancel=cancel, deadline=deadline), progress=progress)
    _checkpoint(cancel, deadline)
    return {**progress, "protected": sorted(protected), "jobs_after": sum(counts.values())}
