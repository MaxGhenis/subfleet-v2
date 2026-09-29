"""Bounded job retention that never deletes unpreserved work (C-8.4, C-13.4, C-26.12).

Detached jobs and conversation turn jobs are pruned against separate budgets
(C-8.4, C-26.12): each pool drops its own oldest unpinned jobs until its own
count and byte limits hold. A turn job is also kept for `turn_keep_days` after
it ends, and while the conversation service still needs it; the service answers
that through `pins`, which is asked again inside each delete transaction, like
every other pin. A detached job's record is kept at least a day after it ends.

Retention never deletes, moves, writes or runs anything for a worktree. An
allocated worktree is reclaimed by the machine's worktree archiver, which
preserves its local-only work first and runs on its own schedule; retention
prunes a job that owns one only once that worktree is gone for real (no path,
no quarantined copy, no registration that still points at one). Retention keeps
Subfleet's own record of each pruned job: its directory and rows are archived,
verified, and only then deleted, and only what the archive holds is deleted
(`subfleet/retention_archive.py`). Byte budgets count job directories, the part
retention can reclaim. Design and invariants: `docs/desktop/retention-archive.md`.

No subprocess, stat or removal ever runs inside a store transaction (C-3.3).
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import retention_archive as archive
from .contracts import (
    RETENTION_MAX_BYTES, RETENTION_MAX_JOBS, TURN_RETENTION_KEEP_DAYS, TURN_RETENTION_MAX_BYTES,
    TURN_RETENTION_MAX_JOBS,
)
from .store import Store, utc_now

_TERMINAL = {"succeeded", "failed", "cancelled", "lost"}
#: A detached job's record is kept at least this long after it ends (review of rev 2, 5).
MIN_AGE_S = 24 * 3600
#: A terminal job's measured size is trusted this long; a live job's this long.
SIZE_TTL_S = 6 * 3600
LIVE_SIZE_TTL_S = 3600
#: The share of the pass deadline sizing may use, so selection still gets a turn.
SIZING_SHARE = 0.5
#: How long a candidate is passed over after its directory could not be archived.
DEFER_BUSY_S = 3600
DEFER_UNARCHIVABLE_S = 86400
#: At most this many jobs are retired in one pass.
MAX_SELECTED = 200
GIT_TIMEOUT_S = 15
LEASE_PREFIX = "retire:"


class _Interrupted(Exception):
    pass


def _checkpoint(cancel: threading.Event | None, deadline: float | None) -> None:
    if cancel is not None and cancel.is_set():
        raise _Interrupted("cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise _Interrupted("deadline")


def _cancel_only(cancel: threading.Event | None) -> Callable[[], None]:
    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise _Interrupted("cancelled")
    return check


# ---------------------------------------------------------------------------
# state kept between passes
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class _Walk:
    """A resumable size measurement: directories still to read, bytes so far."""
    key: tuple
    pending: list[str]
    total: int = 0


@dataclasses.dataclass
class RetentionState:
    """What one daemon remembers between passes: sizes, unfinished walks, deferrals.

    Nothing here is a safety input. A stale size changes only which job goes
    first; the archive decides what may be deleted."""
    sizes: dict[str, tuple[tuple, int, float]] = dataclasses.field(default_factory=dict)
    walks: dict[str, _Walk] = dataclasses.field(default_factory=dict)
    deferred: dict[str, tuple[float, str]] = dataclasses.field(default_factory=dict)
    clock: Callable[[], float] = time.monotonic
    directories_read: int = 0

    def defer(self, job_id: str, seconds: float, reason: str) -> None:
        self.deferred[job_id] = (self.clock() + seconds, reason)

    def deferral(self, job_id: str) -> str | None:
        until, reason = self.deferred.get(job_id, (0.0, ""))
        if until > self.clock():
            return reason
        self.deferred.pop(job_id, None)
        return None

    def forget(self, job_id: str) -> None:
        for table in (self.sizes, self.walks, self.deferred):
            table.pop(job_id, None)


# ---------------------------------------------------------------------------
# pins
# ---------------------------------------------------------------------------

def _contains(value: Any, job_id: str) -> bool:
    if isinstance(value, str):
        return value == job_id or value.startswith(job_id + "/a")
    if isinstance(value, dict):
        return any(_contains(item, job_id) for item in value.values())
    if isinstance(value, list):
        return any(_contains(item, job_id) for item in value)
    return False


def _pins(store: Store, explicit: set[str], *, pins: Callable[[], Iterable[str]] | None = None,
          turn_keep_s: float = 0, min_age_s: float = 0) -> set[str]:
    """Read only database evidence; safe to recheck inside the delete transaction.

    `pins` is the conversation service's evidence (C-26.12); it reads the
    conversation store and the service's live runners, never this store's
    transaction state, so it is as safe to ask inside a transaction."""
    protected = set(explicit)
    if pins is not None:
        protected.update(pins())
    now = datetime.now(UTC)
    kept_since = (now - timedelta(seconds=turn_keep_s)).isoformat(timespec="milliseconds")
    young_since = (now - timedelta(seconds=min_age_s)).isoformat(timespec="milliseconds")
    for row in store.query("SELECT job_id,kind,state,finished_at FROM jobs"):
        if row["state"] not in _TERMINAL or row["kind"] == "gate-review":
            protected.add(row["job_id"])
        elif row["kind"] == "turn" and turn_keep_s > 0 and not _ended_before(row["finished_at"], kept_since):
            protected.add(row["job_id"])   # C-26.12: kept for days after it ends
        elif row["kind"] != "turn" and min_age_s > 0 and row["finished_at"] and \
                not _ended_before(row["finished_at"], young_since):
            protected.add(row["job_id"])   # C-8.4: a record outlives its job by a day
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


def _pool(job: dict[str, Any]) -> str:
    return "turn" if job.get("kind") == "turn" else "detached"


# ---------------------------------------------------------------------------
# is an allocated worktree gone?
# ---------------------------------------------------------------------------

def _named(entry: str, name: str) -> bool:
    """The tree itself, or a copy an archiver set aside under a suffixed name
    (disk-guard and worktree-archive-sweep quarantine to `.disk-guard-removing.<name>`)."""
    return entry == name or entry.endswith("." + name)


class _Worktrees:
    """Answers "is this job's worktree gone for real?" for one pass, read-only.

    Gone means: nothing under the allocated root is the tree or a set-aside copy
    of it, and the job's repository has no registration whose checkout exists
    and is that tree under any name. An archiver that quarantines a tree and
    may rename it back therefore never looks finished while it works. When the
    repository exists but cannot be read, the answer is "not gone"."""

    def __init__(self, root: Path):
        self.root = root / "worktrees"
        try:
            self.entries = os.listdir(self.root)
        except FileNotFoundError:
            self.entries = []
        self._registrations: dict[str, list[str] | None] = {}

    def present(self, worktree: Path, workdir: str | None) -> str | None:
        """None when gone; otherwise why the tree still counts as present."""
        name = worktree.name
        for entry in self.entries:
            if _named(entry, name):
                return f"worktree present: {self.root / entry}"
        if not workdir or not os.path.isdir(workdir):
            return None                                     # its repository is gone too
        checkouts = self._checkouts(workdir)
        if checkouts is None:
            return "worktree state unknown: its repository could not be read"
        for checkout in checkouts:
            if _named(os.path.basename(checkout.rstrip("/")), name) and os.path.lexists(checkout):
                return f"worktree present under another name: {checkout}"
        return None

    def _checkouts(self, workdir: str) -> list[str] | None:
        if workdir not in self._registrations:
            self._registrations[workdir] = _registered_checkouts(workdir)
        return self._registrations[workdir]


def _registered_checkouts(workdir: str) -> list[str] | None:
    """Every linked checkout the repository at `workdir` registers, read from its
    admin directories (`<common>/worktrees/*/gitdir`); None if unreadable."""
    try:
        result = subprocess.run(["git", "-C", workdir, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False,
                                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    admins = Path(result.stdout.strip()) / "worktrees"
    checkouts = []
    try:
        names = os.listdir(admins)
    except FileNotFoundError:
        return []
    except OSError:
        return None
    for name in names:
        try:
            gitdir = (admins / name / "gitdir").read_text().strip()
        except (OSError, UnicodeDecodeError):
            continue
        checkouts.append(os.path.dirname(gitdir))
    return checkouts


# ---------------------------------------------------------------------------
# sizes
# ---------------------------------------------------------------------------

def _measure(state: RetentionState, job: dict[str, Any], places: list[Path], *,
             cancel: threading.Event | None, deadline: float | None) -> tuple[int, bool]:
    """(bytes of regular files and symlinks below `places`, measured afresh now).

    Resumable across passes: at the deadline it raises _Interrupted with the
    walk saved. Raises OSError when a directory cannot be read."""
    identity = job["job_id"]
    key = (job["state"] in _TERMINAL, tuple(str(p) for p in places), tuple(os.path.lexists(p) for p in places))
    cached = state.sizes.get(identity)
    ttl = SIZE_TTL_S if job["state"] in _TERMINAL else LIVE_SIZE_TTL_S
    if cached is not None and cached[0] == key and state.clock() - cached[2] < ttl:
        return cached[1], False
    walk = state.walks.get(identity)
    if walk is None or walk.key != key:
        walk = state.walks[identity] = _Walk(key, [str(p) for p in reversed(places)])
    while walk.pending:
        _checkpoint(cancel, deadline)
        path = walk.pending[-1]
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            walk.pending.pop()
            continue
        if not os.path.isdir(path) or os.path.islink(path):
            walk.total += st.st_size
            walk.pending.pop()
            continue
        try:
            below, found = [], 0
            with os.scandir(path) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            below.append(entry.path)
                        else:
                            found += entry.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:
                        continue
        except FileNotFoundError:
            walk.pending.pop()
            continue
        except OSError:
            state.walks.pop(identity, None)
            raise
        _checkpoint(cancel, deadline)          # this directory is read again when resumed
        walk.pending.pop()
        walk.total += found
        walk.pending.extend(below)
        state.directories_read += 1
    state.walks.pop(identity, None)
    state.sizes[identity] = (key, walk.total, state.clock())
    return walk.total, True


# ---------------------------------------------------------------------------
# the pass
# ---------------------------------------------------------------------------

def maintenance(store: Store, state_root: str | Path, *, max_jobs: int = RETENTION_MAX_JOBS,
                max_bytes: int = RETENTION_MAX_BYTES, turn_max_jobs: int = TURN_RETENTION_MAX_JOBS,
                turn_max_bytes: int = TURN_RETENTION_MAX_BYTES,
                turn_keep_s: float = TURN_RETENTION_KEEP_DAYS * 86400, min_age_s: float = 0,
                pins: Callable[[], Iterable[str]] | None = None, referenced_job_ids: Iterable[str] = (),
                state: RetentionState | None = None, cancel: threading.Event | None = None,
                deadline: float | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Prune oldest unpinned terminal jobs until each pool's limits hold.

    Detached jobs are held to `max_jobs`/`max_bytes` (C-8.4) and turn jobs to
    `turn_max_jobs`/`turn_max_bytes` (C-26.12); a pool over its budget never
    prunes the other. Bytes are those of job directories. A job whose allocated
    worktree is not gone for real is kept, with the reason under `kept`; so is
    a detached job that ended less than `min_age_s` ago (the daemon passes
    MIN_AGE_S). `state` carries sizes, unfinished walks and deferrals from one
    pass to the next; a pass without one starts afresh. The deadline stops
    sizing and the start of retirement; cancellation (C-16.4) also stops
    archiving, leaving every unpruned job's files as they were.

    The result says what was pruned, protected, kept and deferred (with
    reasons) and measured; `interrupted` when work remained that the pass did
    not reach, and `made_progress` when it pruned, measured, or finished
    anything. `dry_run` only reads (a read-only store works): it neither
    recovers nor retires, and reports under `would_retire` what a pass would prune.
    """
    state = state if state is not None else RetentionState()
    progress: dict[str, Any] = {"pruned": [], "errors": [], "bytes_before": None, "bytes_after": None,
                                "deferred": {}, "kept": {}, "measured": 0, "recovered": [], "conflicts": [],
                                "made_progress": False}
    read_before = state.directories_read
    try:
        result = _maintenance(store, Path(state_root).resolve(), state=state,
                              budgets={"detached": (max_jobs, max_bytes), "turn": (turn_max_jobs, turn_max_bytes)},
                              turn_keep_s=turn_keep_s, min_age_s=min_age_s, pins=pins,
                              referenced_job_ids=referenced_job_ids, cancel=cancel, deadline=deadline,
                              progress=progress, dry_run=dry_run)
    except _Interrupted as exc:
        jobs = store.list_jobs()
        pruned = set(progress["pruned"])
        result = {**progress, "protected": sorted(job["job_id"] for job in jobs if job["job_id"] not in pruned),
                  "bytes_after": None, "jobs_after": len(jobs), "interrupted": str(exc)}
    if state.directories_read != read_before:
        result["made_progress"] = True
    return result


def _maintenance(store, root, *, state, budgets, turn_keep_s, min_age_s, pins, referenced_job_ids,
                 cancel, deadline, progress, dry_run):
    if any(limit < 0 for pair in budgets.values() for limit in pair) or turn_keep_s < 0 or min_age_s < 0:
        raise ValueError("retention limits must be nonnegative")
    _checkpoint(cancel, deadline)
    jobs_root = root / "jobs"
    if not dry_run:
        _recover(store, root, state, progress, cancel)
    jobs = store.list_jobs()                                   # newest first
    errors = progress["errors"]
    explicit = set(referenced_job_ids)
    worktrees = _Worktrees(root)
    for job in jobs:
        identity = job["job_id"]
        if Path(identity).name != identity or identity in (".", ".."):
            errors.append({"job_id": identity, "error": "invalid job directory name"})
            explicit.add(identity)
            continue
        try:
            worktree = _owned_worktree(job, root)
        except (OSError, ValueError) as exc:
            errors.append({"job_id": identity, "error": str(exc)})
            explicit.add(identity)
            continue
        if worktree is not None and job["state"] in _TERMINAL:
            reason = worktrees.present(worktree, job.get("workdir"))
            if reason is not None:
                progress["kept"][identity] = reason
                explicit.add(identity)             # retention never touches a worktree (W1, W2)

    def pinned() -> set[str]:
        return _pins(store, explicit, pins=pins, turn_keep_s=turn_keep_s, min_age_s=min_age_s)

    # Job-directory sizes: terminal jobs oldest first, then live ones, within a
    # share of the deadline. Only a terminal job left unmeasured leaves work over.
    sizes: dict[str, int] = {}
    unfinished = False
    sizing_deadline = None if deadline is None else time.monotonic() + max(
        0.0, deadline - time.monotonic()) * SIZING_SHARE
    order = [job for job in reversed(jobs) if job["state"] in _TERMINAL] + \
            [job for job in reversed(jobs) if job["state"] not in _TERMINAL]
    failed = {error["job_id"] for error in errors}
    try:
        for job in order:
            identity = job["job_id"]
            if identity in failed:
                continue
            try:
                sizes[identity], fresh = _measure(state, job, [jobs_root / identity], cancel=cancel,
                                                  deadline=sizing_deadline)
            except OSError as exc:
                errors.append({"job_id": identity, "error": str(exc)})
                failed.add(identity)
                explicit.add(identity)
                continue
            progress["measured"] += fresh and job["state"] in _TERMINAL
    except _Interrupted as exc:
        if str(exc) == "cancelled":
            raise
        unfinished = any(job["job_id"] not in sizes for job in order if job["state"] in _TERMINAL)
    protected = pinned()
    counts = {name: 0 for name in budgets}
    totals = {name: 0 for name in budgets}
    unmeasured = {name: 0 for name in budgets}
    for job in jobs:
        counts[_pool(job)] += 1
        if job["job_id"] in sizes:
            totals[_pool(job)] += sizes[job["job_id"]]
        else:
            unmeasured[_pool(job)] += 1
    progress["bytes_before"] = sum(totals.values())
    progress["pools"] = {name: {"jobs_before": counts[name], "bytes_before": totals[name],
                                "unmeasured": unmeasured[name], "max_jobs": budgets[name][0],
                                "max_bytes": budgets[name][1]} for name in budgets}

    def over(pool: str) -> bool:
        return counts[pool] > budgets[pool][0] or totals[pool] > budgets[pool][1]

    selected = []
    for job in reversed(jobs):
        if not any(over(pool) for pool in budgets):
            break
        if len(selected) >= MAX_SELECTED:
            unfinished = True
            break
        pool, identity = _pool(job), job["job_id"]
        if not over(pool) or identity in protected:
            continue
        reason = state.deferral(identity)
        if reason is not None:
            progress["deferred"][identity] = reason
            continue
        if identity not in sizes and counts[pool] <= budgets[pool][0]:
            continue                            # over by bytes only, and this one's are unknown
        selected.append(job)
        counts[pool] -= 1
        totals[pool] -= sizes.get(identity, 0)
    if dry_run:
        progress["would_retire"] = [{"job_id": job["job_id"], "pool": _pool(job), "bytes": sizes.get(job["job_id"])}
                                    for job in selected]
        selected = []
    counts = {name: progress["pools"][name]["jobs_before"] for name in budgets}
    totals = {name: progress["pools"][name]["bytes_before"] for name in budgets}
    if selected:
        _checkpoint(cancel, deadline)           # no retirement starts after the deadline
        _retire(store, root, selected, sizes, state=state, pinned=pinned, cancel=cancel, progress=progress,
                protected=protected, counts=counts, totals=totals, jobs_root=jobs_root,
                archive_root=root / "archive")
    for name in budgets:
        progress["pools"][name].update(jobs_after=counts[name], bytes_after=totals[name])
    progress["made_progress"] = progress["made_progress"] or bool(progress["measured"])
    result = {**progress, "protected": sorted(protected), "bytes_after": sum(totals.values()),
              "jobs_after": sum(counts.values())}
    if unfinished:
        result["interrupted"] = "deadline"
    return result


def _retire(store, root, selected, sizes, *, state, pinned, cancel, progress, protected, counts, totals,
            jobs_root, archive_root) -> None:
    check = _cancel_only(cancel)
    for job in selected:
        check()
        identity = job["job_id"]
        holder = f"retention:{identity}"
        with store.transaction("retention.selected", job_id=identity) as conn:
            if identity in pinned():
                protected.add(identity)
                continue
            current = conn.execute("SELECT holder FROM leases WHERE lease_key=?", (LEASE_PREFIX + identity,)).fetchone()
            if current and current["holder"] != holder:
                protected.add(identity)
                continue
            # Not a path lease: an archiver that reads Subfleet's leases (disk-guard)
            # treats every `worktree:/...` lease as a tree in use.
            conn.execute("INSERT OR IGNORE INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                         (LEASE_PREFIX + identity, holder, utc_now()))
        pool = _pool(job)
        directory = jobs_root / identity
        try:
            snapshot = archive.rows_snapshot(store.query, identity)
            final = archive.archive(directory, archive_root, identity, snapshot, check=check)
        except archive.NotQuiet as exc:
            _skip(store, state, progress, protected, identity, DEFER_BUSY_S, f"job directory changed: {exc}")
            continue
        except archive.Unarchivable as exc:
            _skip(store, state, progress, protected, identity, DEFER_UNARCHIVABLE_S, f"cannot archive: {exc}")
            continue
        except (OSError, ValueError, archive.ArchiveCorrupt) as exc:
            progress["errors"].append({"job_id": identity, "error": str(exc)})
            _skip(store, state, progress, protected, identity, DEFER_BUSY_S, f"archive failed: {exc}")
            continue
        except _Interrupted:
            _release(store, identity)
            raise
        manifest = archive.load(final)
        committed = False
        with store.transaction("retention.pruned", job_id=identity,
                               data={"bytes": sizes.get(identity, 0), "pool": pool, "archive": str(final)}) as conn:
            lease = conn.execute("SELECT holder FROM leases WHERE lease_key=?", (LEASE_PREFIX + identity,)).fetchone()
            if identity in pinned() or lease is None or lease["holder"] != holder:
                protected.add(identity)
            elif archive.rows_snapshot(lambda sql, params: conn.execute(sql, params).fetchall(),
                                       identity)["sha256"] != manifest["rows_sha256"]:
                protected.add(identity)         # a row changed after the archive was written
            else:
                for table in ("artifacts", "readings"):
                    conn.execute(f"DELETE FROM {table} WHERE attempt_id IN "
                                 "(SELECT attempt_id FROM attempts WHERE job_id=?)", (identity,))
                for table in ("notices", "decisions", "attempts", "jobs"):
                    conn.execute(f"DELETE FROM {table} WHERE job_id=?", (identity,))
                committed = True
        if not committed:
            archive.discard(final)
            _release(store, identity)
            continue
        progress["pruned"].append(identity)
        progress["made_progress"] = True
        counts[pool] -= 1
        totals[pool] -= sizes.get(identity, 0)
        state.forget(identity)
        _finish(store, root, identity, final, manifest, progress, check)


def _skip(store, state, progress, protected, identity, delay, reason) -> None:
    state.defer(identity, delay, reason)
    progress["deferred"][identity] = reason
    protected.add(identity)
    _release(store, identity)


def _release(store: Store, identity: str) -> None:
    with store.transaction("retention.released", job_id=identity) as conn:
        conn.execute("DELETE FROM leases WHERE lease_key=? AND holder=?",
                     (LEASE_PREFIX + identity, f"retention:{identity}"))


def _finish(store, root, identity, final, manifest, progress, check) -> None:
    """After the rows are gone: delete what the archive holds, keep the rest, release."""
    directory = root / "jobs" / identity
    kept = archive.delete_archived(directory, manifest, check=check)
    if kept:
        _to_conflicts(store, root, identity, directory, kept, progress)
    _release(store, identity)


def _to_conflicts(store, root, identity, directory, kept, progress) -> None:
    """Move what deletion kept out of `jobs/`, never deleting it, and say so."""
    moved = None
    if os.path.lexists(directory):
        conflicts = root / "retention-conflicts"
        conflicts.mkdir(mode=0o700, exist_ok=True)
        target, suffix = conflicts / identity, 0
        while os.path.lexists(target):
            suffix += 1
            target = conflicts / f"{identity}.{suffix}"
        try:
            os.rename(directory, target)
            moved = str(target)
        except OSError:
            moved = None
    record = {"job_id": identity, "kept": kept[:50], "kept_count": len(kept), "moved_to": moved}
    progress["conflicts"].append(record)
    store.add_event("retention.conflict", job_id=identity, data=record)


def _recover(store, root, state, progress, cancel) -> None:
    """Finish or undo what an interrupted pass left (design 6.3)."""
    check = _cancel_only(cancel)
    archive_root = root / "archive"
    for lease in store.query("SELECT lease_key,holder FROM leases WHERE lease_key LIKE ? OR "
                             "(lease_key LIKE 'worktree:%' AND holder LIKE 'retention:%')", (LEASE_PREFIX + "%",)):
        check()
        key, holder = lease["lease_key"], lease["holder"]
        if not key.startswith(LEASE_PREFIX):
            # The installed code's removal lease. This code never takes one, so
            # nothing of it is left to finish; the job is retired normally.
            with store.transaction("retention.legacy_lease_released", data={"lease": key}) as conn:
                conn.execute("DELETE FROM leases WHERE lease_key=? AND holder=?", (key, holder))
            continue
        identity = key[len(LEASE_PREFIX):]
        if Path(identity).name != identity or identity in (".", "..") or holder != f"retention:{identity}":
            continue
        row = store.get_job(identity)
        final = archive_root / identity
        partial = archive_root / (archive.PARTIAL_PREFIX + identity)
        if os.path.lexists(partial):
            archive.discard(partial)
        if row is not None:
            # Never committed, so the job directory is untouched. An archive at the
            # final name is this retirement's only if it names this request.
            if os.path.lexists(final):
                try:
                    if archive.load(final).get("request_id") == row["request_id"]:
                        archive.discard(final)
                except (OSError, ValueError):
                    pass
            _release(store, identity)
            progress["recovered"].append({"job_id": identity, "action": "undone"})
            continue
        directory = root / "jobs" / identity
        try:
            manifest = archive.verify(final, check=check)
        except archive.ArchiveCorrupt as exc:
            if os.path.lexists(directory):
                _to_conflicts(store, root, identity, directory, [""], progress)
            store.add_event("retention.recovery_error", job_id=identity, data={"error": str(exc)})
            _release(store, identity)
            continue
        _finish(store, root, identity, final, manifest, progress, check)
        progress["recovered"].append({"job_id": identity, "action": "finished"})
        progress["made_progress"] = True
    if archive_root.is_dir():
        held = {row["lease_key"][len(LEASE_PREFIX):] for row in
                store.query("SELECT lease_key FROM leases WHERE lease_key LIKE ?", (LEASE_PREFIX + "%",))}
        for name in os.listdir(archive_root):
            if name.startswith(archive.PARTIAL_PREFIX) and name[len(archive.PARTIAL_PREFIX):] not in held:
                archive.discard(archive_root / name)   # an unfinished copy of an untouched directory
