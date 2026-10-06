"""Bounded job retention by archive, with filesystem work outside store transactions (C-8.4).

Detached jobs and conversation turn jobs are pruned against separate budgets
(C-8.4, C-26.12): each pool retires its own oldest unpinned jobs until its own
count and byte limits hold. A turn job is also kept for `turn_keep_days` after
it ends, and while the conversation service still needs it (its message is not
terminal, its conversation is blocked, or a live runner reads its attempt):
the service answers that through `pins`, which is asked again inside each
delete transaction, like every other pin.

Retiring a job archives it first (design d635, `retention_archive`): nothing is
deleted that the archive does not hold byte for byte, or that a network remote
does not hold, and the archive is read back before the job's rows go. A pass:

1. finishes what earlier passes committed (verified deletion, resumable);
2. picks the oldest unpinned jobs of each pool over its budget, a bounded batch,
   without sizing the whole tree first (sizes are measured lazily, cached, and
   only a lower bound is needed to know a pool is over);
3. quarantines them and checks, with one process listing for the batch, that
   nothing holds them;
4. archives each within a time slice (a job that needs more is parked with its
   progress and continues next pass; the others go on);
5. checks holders again, re-validates every signature, commits, and deletes.

A busy, changed or failing job is put back and deferred, so the queue keeps
moving; a pass that made progress never raises.
"""

from __future__ import annotations

import json
import os
import shutil  # noqa: F401  (kept for callers that patch it)
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import retention_archive as rarch
from . import retention_git as rgit
from . import retention_qos as rqos
from .contracts import (
    RETENTION_MAX_BYTES, RETENTION_MAX_JOBS, RETENTION_REMOTE_LESS_HISTORY_BYTES, TURN_RETENTION_KEEP_DAYS,
    TURN_RETENTION_MAX_BYTES, TURN_RETENTION_MAX_JOBS,
)
from . import folders
from .retention_holders import ScanFailed, lsof_holders
from .store import Store

_TERMINAL = {"succeeded", "failed", "cancelled", "lost"}

#: Jobs one pass takes into its batch (in flight included).
BATCH = 32
#: Seconds of archiving a job gets per pass before it is parked.
SLICE_S = 120.0
#: A measured size is trusted this long; stale sizes are never a safety input.
SIZE_TTL_S = 6 * 3600.0
#: Seconds a pass spends measuring sizes it lacks, when it needs them.
MEASURE_S = 30.0
#: A resume's fence on its source (`retire:` held by `resume:`) older than this
#: was left by a submit that returned early; the daemon's submit lock means no
#: live submit holds one this long.
FENCE_STALE_S = 600.0
#: A job whose size could not be measured is not measured again for this long,
#: doubling on each failure up to `SIZE_ERROR_MAX_S` (review of a9a6cbf4, N2).
SIZE_ERROR_S = 3600.0
SIZE_ERROR_MAX_S = 24 * 3600.0


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
    if job.get("worktree") and job.get("sandbox") == "workspace-write" and not job.get("in_place") \
            and os.path.islink(job["worktree"]):
        raise ValueError("allocated worktree path is a symlink")
    return rarch.owned_worktree(job, state_root)


def _pool(job: dict[str, Any]) -> str:
    return "turn" if job.get("kind") == "turn" else "detached"


def nested_hosts(jobs: Iterable[dict[str, Any]], state_root: Path) -> dict[str, set[str]]:
    """host job id -> the jobs whose worktree is registered in a repository
    inside the host's allocated worktree (final review of e50716e8, N4).

    Where a job's registration lives is read from its tree's gitfile. A job
    whose tree is gone counts as hosted by the tree its source directory is
    inside, which may pin a host it does not need (a directory of the host's
    own checkout, whose registrations are in the host's repository, outside
    the tree): when in doubt, keep the host. The host is kept while any of
    them has rows (`nested-host`), so a hosted job always retires first, while
    its registration is readable, with its own anchor and bundle.
    """
    jobs = list(jobs)
    owned: dict[str, Path] = {}
    for job in jobs:
        try:
            worktree = _owned_worktree(job, state_root)
        except ValueError:
            continue
        if worktree is not None:
            owned[job["job_id"]] = worktree
    trees = {str(path): job_id for job_id, path in owned.items()}
    hosted: dict[str, set[str]] = {}
    for job in jobs:
        worktree = owned.get(job["job_id"])
        if worktree is None:
            continue
        where = None
        if os.path.lexists(worktree):
            admin = rgit.gitfile_admin(worktree)[0]
            where = admin
        elif job.get("workdir"):
            where = Path(os.path.realpath(job["workdir"]))
        if where is None:
            continue
        host = next((trees[str(p)] for p in where.parents if str(p) in trees), None)
        if host is not None and host != job["job_id"]:
            hosted.setdefault(host, set()).add(job["job_id"])
    return hosted


# --- pins -------------------------------------------------------------------------

_PIN_QUERIES = (
    ("live-attempt", "SELECT DISTINCT job_id FROM attempts WHERE state IN ('reserved','starting','running','finalizing')"),
    ("quarantined", "SELECT DISTINCT job_id FROM attempts WHERE state='quarantined'"),
    # C-8.4, IR-17: an unread notice pins its job only when some session can
    # still read it; a notice with no session is never delivered, so it would
    # pin its job for ever.
    ("unread-notice", "SELECT DISTINCT job_id FROM notices WHERE state IN ('pending','offered') "
                      "AND session_id IS NOT NULL AND session_id <> ''"),
    ("parent", "SELECT DISTINCT parent_job_id AS job_id FROM jobs WHERE parent_job_id IS NOT NULL"),
    ("job-lease", "SELECT j.job_id FROM jobs j JOIN leases l ON l.holder=j.job_id"),
    ("attempt-lease", "SELECT a.job_id FROM attempts a JOIN leases l ON l.holder=a.attempt_id"),
    ("worktree-lease", "SELECT j.job_id FROM jobs j JOIN leases l ON l.lease_key='worktree:' || j.worktree "
                       "WHERE l.holder != 'retention:' || j.job_id"),
    ("retire-lease", "SELECT substr(lease_key, 8) AS job_id FROM leases WHERE lease_key LIKE 'retire:%' "
                     "AND holder != 'retention:' || substr(lease_key, 8)"),
    # A job not yet ended whose directory is this job's allocated worktree or
    # inside it (a job an agent submitted from its worktree, still queued):
    # the tree must still be there when it runs (design review, Opus 9).
    ("worktree-in-use", "SELECT a.job_id FROM jobs a JOIN jobs b ON b.job_id <> a.job_id "
                        "AND b.state NOT IN ('succeeded','failed','cancelled','lost') "
                        "AND (b.worktree = a.worktree OR b.workdir = a.worktree "
                        "OR b.workdir LIKE a.worktree || '/%' OR b.worktree LIKE a.worktree || '/%') "
                        "WHERE a.worktree IS NOT NULL AND a.in_place = 0 AND a.sandbox = 'workspace-write'"),
)
#: `worktree-in-use` for a tree `_workspace` allocated that no row names yet
#: (`jobs.worktree` NULL, `rarch.owned_worktree`); ?1 is `<state>/worktrees/`.
_UNRECORDED_IN_USE = ("SELECT a.job_id FROM jobs a JOIN jobs b ON b.job_id <> a.job_id "
                      "AND b.state NOT IN ('succeeded','failed','cancelled','lost') "
                      "AND (b.worktree = ?1 || a.job_id OR b.workdir = ?1 || a.job_id "
                      "OR b.workdir LIKE ?1 || a.job_id || '/%' OR b.worktree LIKE ?1 || a.job_id || '/%') "
                      "WHERE a.worktree IS NULL AND a.in_place = 0 AND a.sandbox = 'workspace-write'")


def _pin_reasons(store: Store, explicit: set[str], landed_salvage: set[int] | None, *,
                 pins: Callable[[], Iterable[str]] | None = None, turn_keep_s: float = 0,
                 only: str | None = None, hosted: dict[str, set[str]] | None = None,
                 root: Path | None = None) -> dict[str, str]:
    """job id -> why retention must keep it. Reads only database evidence (and
    the conversation service's, and `hosted`, which `nested_hosts` read from the
    trees before the pass), so it is as safe inside the delete transaction.
    With the state `root`, a tree allocated before its row recorded it counts
    for `worktree-in-use` like a recorded one.

    `landed_salvage` is the set of salvage artifact ids whose commit is (or, at
    selection, will be) in the job's verified archive bundle: C-8.4 pins a job
    whose salvage refs are referenced nowhere else. None means "a salvage
    artifact lands when its job has an allocated worktree", the tentative rule
    used before a job is archived; the commit transaction passes the verified set.
    """
    reasons: dict[str, str] = {}

    def add(job_id: str | None, reason: str) -> None:
        if job_id and (only is None or job_id == only):
            reasons.setdefault(job_id, reason)

    for job_id in explicit:
        add(job_id, "explicit")
    if pins is not None:
        for job_id in pins():
            add(job_id, "conversation")
    kept_since = (datetime.now(UTC) - timedelta(seconds=turn_keep_s)).isoformat(timespec="milliseconds")
    where, params = (" WHERE job_id=?", (only,)) if only else ("", ())
    jobs = store.query(f"SELECT job_id,kind,state,finished_at,worktree,sandbox,in_place FROM jobs{where}", params)
    for row in jobs:
        if row["state"] not in _TERMINAL:
            add(row["job_id"], "active")
        elif row["kind"] == "gate-review":
            add(row["job_id"], "gate-review")
        elif row["kind"] == "turn" and turn_keep_s > 0 and not _ended_before(row["finished_at"], kept_since):
            add(row["job_id"], "turn-keep-days")   # C-26.12: kept for days after it ends
    for reason, sql in _PIN_QUERIES:
        for row in store.query(sql):
            add(row["job_id"], reason)
    owned = {row["job_id"] for row in jobs if row["worktree"] and row["sandbox"] == "workspace-write"
             and not row["in_place"]}
    # I5 (C-8.4, C-13.4): turns and readers have per-turn rows rather than
    # `worktree:` leases. A row on a job's own worktree, or on a folder inside
    # it (a conversation in a repository nested there keys its row on that
    # repository), keeps the job; an in-place job's folder is never removed, so
    # a row there keeps nothing (review of 599af189, P3-1 and P3-2). Compare
    # recorded spellings only: this function also runs inside the archive
    # commit transaction, where filesystem work is forbidden.
    in_use = folders.turn_folders(store.query)
    for row in jobs:
        if row["job_id"] in owned and any(folders.within(folder, row["worktree"]) for folder in in_use):
            add(row["job_id"], "turn-folder")
    if root is not None:
        for row in store.query(_UNRECORDED_IN_USE, (str(Path(root) / "worktrees") + "/",)):
            add(row["job_id"], "worktree-in-use")
    # A job registered in a repository inside another job's tree keeps that
    # tree while it has rows: it retires first, with its own anchor (N4).
    for host in ([only] if only else sorted(hosted or ())):
        guests = sorted((hosted or {}).get(host) or ())
        live = [row["job_id"] for row in store.query(
            f"SELECT job_id FROM jobs WHERE job_id IN ({','.join('?' * len(guests))}) ORDER BY job_id LIMIT 3",
            tuple(guests))] if guests else []
        if live:
            add(host, "nested-host: " + ", ".join(live))      # names the jobs it waits for
    for row in store.query("SELECT r.artifact_id,a.job_id FROM artifacts r JOIN attempts a USING(attempt_id) "
                           "WHERE r.role='salvage'" + (" AND a.job_id=?" if only else ""), params):
        landed = row["job_id"] in owned if landed_salvage is None else row["artifact_id"] in landed_salvage
        if not landed:
            add(row["job_id"], "salvage")
    actions = store.query("SELECT subject,request_json,result_json FROM actions WHERE kind LIKE '%gate%' OR kind LIKE '%merge%'")
    ids = [row["job_id"] for row in jobs] if only is None else [only]
    for action in actions:
        evidence = [action["subject"]]
        for key in ("request_json", "result_json"):
            try:
                evidence.append(json.loads(action[key] or "null"))
            except (TypeError, ValueError):
                # Unknown evidence remains a pin for all jobs until reconciled.
                for job_id in ids:
                    add(job_id, "gate-evidence")
        for job_id in ids:
            if _contains(evidence, job_id):
                add(job_id, "gate-evidence")
    return reasons


def _pins(store: Store, explicit: set[str], landed_salvage: set[int], *,
          pins: Callable[[], Iterable[str]] | None = None, turn_keep_s: float = 0) -> set[str]:
    """The pinned job ids (the set form of `_pin_reasons`, for callers of the old API)."""
    return set(_pin_reasons(store, explicit, landed_salvage, pins=pins, turn_keep_s=turn_keep_s))


# --- state kept across passes ---------------------------------------------------

#: What a pass reports about the disk, apart from the pools' live-tree bytes
#: (d635 accounting): deleted without a copy (`freed_bytes`: omitted tracked
#: files and regenerable output; `freed_disk_bytes`: their blocks no clone
#: shares) versus moved into the archive (`archived_bytes`), and the space the
#: archives added (`added_bytes`: bundles, manifests, rows and byte copies;
#: final review of e50716e8, N1). The net on disk is `freed_disk_bytes -
#: added_bytes`.
FREED_KEYS = ("freed_bytes", "freed_disk_bytes", "archived_bytes", "added_bytes")


@dataclass
class RetentionState:
    """What a daemon remembers between passes: deferrals and measured sizes.

    Losing it (a restart) is harmless: a deferred job is tried again, a size is
    measured again. In-flight retirements live in their journals, not here.
    """
    deferred: dict[str, tuple[float, str]] = field(default_factory=dict)
    sizes: dict[str, tuple[int, float]] = field(default_factory=dict)
    #: job id -> (retry at, backoff seconds, error): a size that could not be
    #: measured. Such a job is decided (its size is unknown), not waiting to be
    #: measured, so it never keeps a pass reporting more work.
    unmeasurable: dict[str, tuple[float, float, str]] = field(default_factory=dict)
    #: Jobs whose deferral this state already knows (it deferred them, or took
    #: the deferral an idle journal kept across a restart, N8).
    recalled: set[str] = field(default_factory=set)
    #: job id -> the clock time of its last archive slice. In-flight jobs are
    #: sliced least recently sliced first, so one that parks pass after pass
    #: does not keep the others waiting behind it (review of a9a6cbf4, N3).
    sliced: dict[str, float] = field(default_factory=dict)

    def defer(self, job_id: str, seconds: float, reason: str, now: float) -> None:
        self.deferred[job_id] = (now + seconds, reason)
        self.recalled.add(job_id)

    def deferral(self, job_id: str, now: float) -> str | None:
        found = self.deferred.get(job_id)
        if found is None:
            return None
        if now >= found[0]:
            del self.deferred[job_id]
            return None
        return found[1]


# --- the pass --------------------------------------------------------------------

def maintenance(store: Store, state_root: str | Path, *, max_jobs: int = RETENTION_MAX_JOBS,
                max_bytes: int = RETENTION_MAX_BYTES, turn_max_jobs: int = TURN_RETENTION_MAX_JOBS,
                turn_max_bytes: int = TURN_RETENTION_MAX_BYTES,
                turn_keep_s: float = TURN_RETENTION_KEEP_DAYS * 86400,
                pins: Callable[[], Iterable[str]] | None = None, referenced_job_ids: Iterable[str] = (),
                salvage_referenced_elsewhere: Callable[[dict[str, Any]], bool] | None = None,
                cancel: threading.Event | None = None, deadline: float | None = None,
                state: RetentionState | None = None,
                holders: Callable[..., dict[str, list[str]]] | None = None,
                clock: Callable[[], float] = time.monotonic, batch: int = BATCH,
                slice_s: float = SLICE_S, measure_s: float | None = None,
                remote_less_history_bytes: int | None = RETENTION_REMOTE_LESS_HISTORY_BYTES) -> dict[str, Any]:
    """Retire the oldest unpinned terminal jobs of each pool over its budget.

    Detached jobs are held to `max_jobs`/`max_bytes` (C-8.4) and turn jobs to
    `turn_max_jobs`/`turn_max_bytes` (C-26.12); a pool over its budget never
    prunes the other. `deadline` bounds when new work may start (a started
    job finishes its slice); `cancel` stops at the next checkpoint and leaves
    every retirement in its journal for the next pass. `holders` lists the
    processes holding a batch's trees (default: `lsof`). `pins` is asked again
    inside every delete transaction. `salvage_referenced_elsewhere` is kept for
    callers of the old API: an artifact it vouches for does not pin its ref;
    every file, including paths salvage skipped, still needs archive verification. A job
    whose baseline no network remote holds, and whose bundle would carry more
    than `remote_less_history_bytes` of history, is kept (`remote-less-history`;
    None: no limit).
    """
    budgets = {"detached": (max_jobs, max_bytes), "turn": (turn_max_jobs, turn_max_bytes)}
    if any(limit < 0 for pair in budgets.values() for limit in pair) or turn_keep_s < 0:
        raise ValueError("retention limits must be nonnegative")
    state = state if state is not None else RetentionState()
    progress: dict[str, Any] = {"pruned": [], "errors": [], "bytes_before": None, "bytes_after": None,
                                "deferred": {}, "in_flight": [], "reclaimed": [], "conflicts": [],
                                **dict.fromkeys(FREED_KEYS, 0)}
    try:
        _checkpoint(cancel, deadline)
    except _Interrupted as exc:
        jobs = store.list_jobs()
        return {**progress, "protected": sorted(job["job_id"] for job in jobs), "jobs_after": len(jobs),
                "interrupted": str(exc), "more": True}
    run = _Pass(store, Path(state_root).resolve(), budgets=budgets, turn_keep_s=turn_keep_s, pins=pins,
                explicit=set(referenced_job_ids), salvage_referenced_elsewhere=salvage_referenced_elsewhere,
                cancel=cancel, deadline=deadline, state=state, holders=holders or lsof_holders, clock=clock,
                batch=batch, slice_s=slice_s, measure_s=measure_s, progress=progress,
                remote_less_history_bytes=remote_less_history_bytes)
    try:
        return run.run()
    except (_Interrupted, rarch.Interrupted, rgit.Cancelled) as exc:
        jobs = store.list_jobs()
        return {**progress, "protected": sorted(job["job_id"] for job in jobs), "bytes_after": None,
                "jobs_after": len(jobs), "interrupted": "cancelled" if "cancel" in str(exc) else str(exc),
                "more": True}


class _Pass:
    def __init__(self, store, root, *, budgets, turn_keep_s, pins, explicit, salvage_referenced_elsewhere,
                 cancel, deadline, state, holders, clock, batch, slice_s, measure_s, progress,
                 remote_less_history_bytes=RETENTION_REMOTE_LESS_HISTORY_BYTES):
        self.store = store
        self.root = root
        self.budgets = budgets
        self.turn_keep_s = turn_keep_s
        self.pins = pins
        self.explicit = explicit
        self.vouched = salvage_referenced_elsewhere
        self.cancel = cancel
        self.deadline = deadline
        self.state = state
        self.holders = holders
        self.clock = clock
        self.batch = max(1, batch)
        self.slice_s = slice_s
        self.measure_s = measure_s
        self.progress = progress
        self.errors: list[dict[str, str]] = progress["errors"]
        self.ctx = rarch.Context(root, store, cancel=cancel, clock=clock, pinned=self._pinned_at_commit,
                                 remote_less_history_bytes=remote_less_history_bytes)
        self.sizes: dict[str, int] = {}
        #: Jobs whose size could not be measured: decided, size unknown (N2).
        self.unknown: set[str] = set()
        #: What this pass changed (a size measured, a job started, archived with
        #: new work, deferred, retired or reclaimed). A pass that reports more
        #: work but changed nothing tells the daemon to back off.
        self.acted = 0
        #: pool -> what this pass's reclaims freed and moved into the archive (d635 accounting).
        self.freed_by_pool: dict[str, dict[str, int]] = {}
        #: host job -> jobs registered in a repository inside its tree (`nested_hosts`, N4).
        self.hosted: dict[str, set[str]] = {}

    # pins ------------------------------------------------------------------------

    def _vouched(self) -> set[int]:
        if self.vouched is None:
            return set()
        return {a["artifact_id"] for a in self.store.query("SELECT * FROM artifacts WHERE role='salvage'")
                if self.vouched(a)}

    def _reasons(self, only: str | None = None) -> dict[str, str]:
        vouched = self._vouched()
        reasons = _pin_reasons(self.store, self.explicit, None, pins=self.pins, turn_keep_s=self.turn_keep_s,
                               only=only, hosted=self.hosted, root=self.root)
        if vouched:
            # The caller vouches for these salvage artifacts; recompute those jobs.
            strict = _pin_reasons(self.store, self.explicit, vouched | self._owned_salvage(), pins=self.pins,
                                  turn_keep_s=self.turn_keep_s, only=only, hosted=self.hosted, root=self.root)
            reasons = {k: v for k, v in reasons.items() if k in strict}
        return reasons

    def _owned_salvage(self) -> set[int]:
        return {row["artifact_id"] for row in self.store.query(
            "SELECT r.artifact_id FROM artifacts r JOIN attempts a USING(attempt_id) JOIN jobs j ON j.job_id=a.job_id "
            "WHERE r.role='salvage' AND j.worktree IS NOT NULL AND j.sandbox='workspace-write' AND j.in_place=0")}

    def _pinned_at_commit(self, job_id: str, landed: set[int]) -> str | None:
        reasons = _pin_reasons(self.store, self.explicit, landed | self._vouched(), pins=self.pins,
                               turn_keep_s=self.turn_keep_s, only=job_id, hosted=self.hosted, root=self.root)
        return reasons.get(job_id)

    # the pass ----------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        in_flight = self._recover()
        jobs = list(reversed(self.store.list_jobs()))        # oldest first
        by_id = {job["job_id"]: job for job in jobs}
        self.hosted = nested_hosts(jobs, self.root)
        reasons = self._reasons()
        for job_id in in_flight:
            reasons.pop(job_id, None)
        protected = set(reasons)
        for error in self.errors:
            protected.add(error["job_id"])
        counts = {name: 0 for name in self.budgets}
        for job in jobs:
            counts[_pool(job)] += 1
        self._measure(jobs, in_flight, counts, protected)
        totals = {name: 0 for name in self.budgets}
        unmeasured = {name: 0 for name in self.budgets}
        unknown = {name: 0 for name in self.budgets}
        for job in jobs:
            pool = _pool(job)
            if job["job_id"] in self.sizes:
                totals[pool] += self.sizes[job["job_id"]]
            elif job["job_id"] in self.unknown:
                unknown[pool] += 1            # could not be measured: decided, not waiting
            else:
                unmeasured[pool] += 1
        before = sum(totals.values())
        self.progress["bytes_before"] = before
        pools = {name: {"jobs_before": counts[name], "bytes_before": totals[name], "unmeasured": unmeasured[name],
                        "unknown_size": unknown[name],
                        "max_jobs": self.budgets[name][0], "max_bytes": self.budgets[name][1]} for name in self.budgets}
        self.progress["pools"] = pools

        def over(pool: str) -> bool:
            max_jobs, max_bytes = self.budgets[pool]
            return counts[pool] > max_jobs or totals[pool] > max_bytes

        # In-flight jobs are already leaving their pools; then pick more, oldest first.
        for job_id in in_flight:
            job = by_id.get(job_id)
            if job is not None:
                counts[_pool(job)] -= 1
                totals[_pool(job)] -= self.sizes.get(job_id, 0)
        now = self.clock()
        waiting = False
        chosen: list[str] = []
        for job in jobs:
            job_id = job["job_id"]
            pool = _pool(job)
            if job_id in in_flight or job_id in protected or not over(pool):
                continue
            deferral = self.state.deferral(job_id, now)
            if deferral is not None:
                self.progress["deferred"].setdefault(job_id, deferral)
                continue
            if Path(job_id).name != job_id or job_id in (".", ".."):
                self.errors.append({"job_id": job_id, "error": "invalid job directory name"})
                protected.add(job_id)
                continue
            if len(in_flight) + len(chosen) >= self.batch:
                waiting = True          # an eligible job waits for the next pass
                break
            chosen.append(job_id)
            counts[pool] -= 1
            totals[pool] -= self.sizes.get(job_id, 0)
        retirements = {job_id: rarch.Retirement(self.ctx, job_id) for job_id in in_flight}
        for n, job_id in enumerate(chosen):
            self.ctx.check()
            if self.deadline is not None and self.clock() >= self.deadline and retirements:
                waiting = True
                break
            retirement = self._start(by_id[job_id], protected)
            if retirement is not None:
                retirements[job_id] = retirement
        self._check_holders(retirements, second=False)
        archived: dict[str, rarch.Retirement] = {}
        # Least recently sliced first (never sliced first of all, oldest first
        # among them): the pass's time goes round the in-flight jobs (N3).
        for job_id in sorted(retirements, key=lambda job_id: self.state.sliced.get(job_id, float("-inf"))):
            retirement = retirements[job_id]
            self.ctx.check()
            if retirement.state == "archived":
                archived[job_id] = retirement
                continue
            if self.deadline is not None and self.clock() >= self.deadline + self.slice_s:
                self.progress["in_flight"].append(job_id)
                continue
            self.state.sliced[job_id] = self.clock()
            try:
                outcome = retirement.archive(self.clock() + self.slice_s)
            except rarch.Defer as exc:
                self._rollback(retirement, exc.reason, exc.seconds, exc.detail)
                continue
            except (OSError, ValueError, rgit.GitError, subprocess.SubprocessError) as exc:
                self._rollback(retirement, "error", rarch.DEFER_ERROR_S, f"{type(exc).__name__}: {exc}")
                continue
            if outcome == "parked":
                self.progress["in_flight"].append(job_id)
                if retirement.last_work:
                    self.acted += 1       # a slice that only re-walked cached entries changed nothing
            else:
                self.acted += 1
                archived[job_id] = retirement
        self._check_holders(archived, second=True)
        for job_id, retirement in archived.items():
            self.ctx.check()
            if retirement.state == "archived":
                self._finish(retirement, protected)
        after = {name: pools[name]["bytes_before"] for name in pools}
        jobs_after = {name: pools[name]["jobs_before"] for name in pools}
        for job_id in self.progress["pruned"]:
            job = by_id.get(job_id)
            if job is not None:
                after[_pool(job)] -= self.sizes.get(job_id, 0)
                jobs_after[_pool(job)] -= 1
        for name in pools:
            pools[name].update(jobs_after=jobs_after[name], bytes_after=after[name])
            # `bytes_*` count live trees against the budget; what leaving them
            # did to the disk is apart: freed (deleted without a copy) versus
            # moved into the archive (clones, which free nothing until removed).
            pools[name].update(self.freed_by_pool.get(name) or dict.fromkeys(FREED_KEYS, 0))
        # More work is waiting when a job was parked or held back by the batch or
        # the deadline, or when a pool's byte total is still only a lower bound
        # within its budget (its unmeasured jobs may put it over). A job whose
        # size could not be measured is decided, not unmeasured (N2).
        undecided = any(unmeasured[p] and counts[p] <= self.budgets[p][0] and totals[p] <= self.budgets[p][1]
                        for p in self.budgets)
        more = waiting or bool(self.progress["in_flight"]) or undecided
        for job_id in self.progress["deferred"]:
            protected.add(job_id)
        remaining = [job["job_id"] for job in jobs if job["job_id"] not in self.progress["pruned"]]
        return {**self.progress, "protected": sorted(protected - set(self.progress["pruned"])),
                "bytes_before": before, "bytes_after": sum(after.values()),
                "jobs_after": len(remaining), "pools": pools, "more": more, "progressed": self.acted > 0,
                "pin_reasons": reasons}

    # recovery --------------------------------------------------------------------

    def _recover(self) -> list[str]:
        in_flight: list[str] = []
        now = time.time()
        found = rarch.journals(self.root)
        # A `retention:` lease with no journal was taken by a selection that
        # died before its journal, or by the old proof-based retention killed
        # mid-removal (design 5.6): nothing of the job has moved; release it.
        for row in self.store.query("SELECT lease_key,holder FROM leases WHERE holder LIKE 'retention:%'"):
            job_id = row["holder"][len("retention:"):]
            if job_id not in found:
                with self.store.transaction("retention.lease_released", job_id=job_id,
                                            data={"lease_key": row["lease_key"], "reason": "no journal"}) as conn:
                    conn.execute("DELETE FROM leases WHERE lease_key=? AND holder=?", (row["lease_key"], row["holder"]))
        stale = (datetime.now(UTC) - timedelta(seconds=FENCE_STALE_S)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.store.transaction("retention.fence_released", data={"reason": "stale"}) as conn:
            conn.execute("DELETE FROM leases WHERE lease_key LIKE 'retire:%' AND holder LIKE 'resume:%' "
                         "AND acquired_at<?", (stale,))
        for job_id, journal in found.items():
            self.ctx.check()
            if isinstance(journal, Exception):
                self.errors.append({"job_id": job_id, "error": f"retention journal unreadable: {journal}"})
                continue
            retirement = rarch.Retirement(self.ctx, job_id, journal)
            state = journal["state"]
            try:
                if state == "idle":
                    row = self.store.get_job(job_id)
                    if row is None or now - float(journal.get("idle_since") or 0) > rarch.CACHE_KEEP_S:
                        retirement.drop_cache()
                    elif job_id not in self.state.recalled:
                        # A restart forgot the deferral; the idle journal kept it (N8).
                        self.state.recalled.add(job_id)
                        if float(journal.get("defer_until") or 0) > now:
                            self.state.defer(job_id, float(journal["defer_until"]) - now,
                                             str(journal.get("reason") or "deferred"), self.clock())
                    continue
                if state == "committing":
                    retirement.save(state="archived" if self.store.get_job(job_id) else "committed")
                    state = retirement.state
                if state in rarch.COMMITTED:
                    self._complete(retirement)
                    continue
                leases = {row["lease_key"] for row in self.store.query(
                    "SELECT lease_key FROM leases WHERE holder=?", (f"retention:{job_id}",))}
                if f"retire:{job_id}" not in leases or self.store.get_job(job_id) is None:
                    self._rollback(retirement, "recovered without its leases", rarch.DEFER_CHANGED_S, "")
                    continue
                if state == "quarantining":
                    retirement._reconcile_moves()
                    retirement.quarantine()          # moves whatever the crash left unmoved
                elif state in ("selected", "locking"):
                    # Nothing has moved: finish the steps before the move.
                    retirement.lock()
                    retirement.quarantine()
                elif state == "locked":
                    retirement.quarantine()
                in_flight.append(job_id)
            except rarch.Defer as exc:
                self._rollback(retirement, exc.reason, exc.seconds, exc.detail)
            except (OSError, ValueError, RuntimeError, rgit.GitError) as exc:
                self.errors.append({"job_id": job_id, "error": f"recovery: {type(exc).__name__}: {exc}"})
        return in_flight

    # sizes -----------------------------------------------------------------------

    def _measure(self, jobs: list[dict[str, Any]], in_flight: list[str], counts: dict[str, int],
                 protected: set[str]) -> None:
        """Sizes, lazily: cached ones are used; others are measured oldest first
        while a pool's byte total could still decide something and time remains.
        A pool already over its job count needs no sizes to act (design 7)."""
        with rqos.throttled_io():
            self._measure_sizes(jobs, in_flight, counts, protected)

    def _measure_sizes(self, jobs: list[dict[str, Any]], in_flight: list[str], counts: dict[str, int],
                       protected: set[str]) -> None:
        now = self.clock()
        budget = self.measure_s
        if budget is None and self.deadline is not None:
            budget = MEASURE_S
        stop = None if budget is None else now + budget
        known = {name: 0 for name in self.budgets}
        todo: list[dict[str, Any]] = []
        for job in jobs:
            job_id = job["job_id"]
            cached = self.state.sizes.get(job_id)
            failed = self.state.unmeasurable.get(job_id)
            if cached is not None and (now - cached[1] < SIZE_TTL_S or job_id in in_flight):
                self.sizes[job_id] = cached[0]
                known[_pool(job)] += cached[0]
            elif failed is not None and now < failed[0] and job_id not in in_flight:
                # Measured and failed, not due again yet: decided, size unknown,
                # kept this pass as when it failed (review of a9a6cbf4, N2).
                self.unknown.add(job_id)
                protected.add(job_id)
            elif job_id not in in_flight:
                todo.append(job)
                if cached is not None:
                    self.sizes[job_id] = cached[0]     # stale until measured again: a scheduling input only
        for job in todo:
            pool = _pool(job)
            max_jobs, max_bytes = self.budgets[pool]
            if stop is not None and (counts[pool] > max_jobs or known[pool] > max_bytes):
                continue          # over already; measuring more decides nothing
            if stop is not None and self.clock() >= stop:
                break
            job_id = job["job_id"]
            if Path(job_id).name != job_id or job_id in (".", ".."):
                continue
            self.acted += 1
            try:
                size = _size(self.root / "jobs" / job_id, cancel=self.cancel)
                worktree = _owned_worktree(job, self.root)
                if worktree is not None:
                    size += _size(worktree, cancel=self.cancel)
            except _Interrupted:
                raise rarch.Interrupted("cancelled") from None
            except (OSError, ValueError) as exc:
                previous = self.state.unmeasurable.get(job_id)
                backoff = SIZE_ERROR_S if previous is None else min(SIZE_ERROR_MAX_S, previous[1] * 2)
                self.state.unmeasurable[job_id] = (self.clock() + backoff, backoff, str(exc))
                self.errors.append({"job_id": job_id, "error": str(exc)})
                self.unknown.add(job_id)
                protected.add(job_id)
                continue
            self.state.unmeasurable.pop(job_id, None)
            known[pool] += size - self.sizes.get(job_id, 0)
            self.sizes[job_id] = size
            self.state.sizes[job_id] = (size, self.clock())

    # one job ---------------------------------------------------------------------

    def _start(self, job: dict[str, Any], protected: set[str]) -> rarch.Retirement | None:
        job_id = job["job_id"]
        try:
            worktree = _owned_worktree(job, self.root)
            # Match admission's spelling before taking the store lock (C-6.5).
            folder = folders.canonical(worktree) if worktree is not None else None
        except ValueError as exc:
            self.errors.append({"job_id": job_id, "error": str(exc)})
            protected.add(job_id)
            return None
        holder = f"retention:{job_id}"
        keys = [f"retire:{job_id}"] + ([folders.exclusive_key(folder)] if folder is not None else [])
        with self.store.transaction("retention.selected", job_id=job_id) as conn:
            self.ctx.check()
            reason = _pin_reasons(self.store, self.explicit, None, pins=self.pins, turn_keep_s=self.turn_keep_s,
                                  only=job_id, hosted=self.hosted, root=self.root).get(job_id)
            # A turn may register after selection. Read its rows, on the tree
            # and on any folder inside it (P3-1), and acquire the fence
            # atomically; daemon reservation checks this same fence before
            # inserting either a TURN or READER row on the tree or on any
            # folder inside it (I5, `folders.retiring`).
            if reason is None and folder is not None and folders.turn_holds(
                    lambda sql, params: conn.execute(sql, params).fetchall(), folder, inside=True):
                reason = "turn-folder"
            if reason is None:
                for key in keys:
                    current = conn.execute("SELECT holder FROM leases WHERE lease_key=?", (key,)).fetchone()
                    if current and current["holder"] != holder:
                        reason = "resume in progress" if key.startswith("retire:") else "worktree lease"
                        break
            if reason is None:
                for key in keys:
                    conn.execute("INSERT OR IGNORE INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                                 (key, holder, _utc()))
        if reason is not None:
            protected.add(job_id)
            if reason == "resume in progress":
                self.acted += 1
                self.state.defer(job_id, 60, reason, self.clock())
                self.progress["deferred"][job_id] = reason
            return None
        self.acted += 1
        retirement = rarch.Retirement(self.ctx, job_id)
        try:
            # The journal's worktree and its commit-time lease check must use
            # the fence's spelling too, even for an older, differently spelled
            # recorded path. The archived database rows keep the original value.
            retirement.begin({**job, "worktree": folder} if folder is not None else job, _pool(job))
            retirement.lock()
            retirement.quarantine()
        except rarch.Defer as exc:
            self._rollback(retirement, exc.reason, exc.seconds, exc.detail)
            return None
        except (OSError, ValueError, RuntimeError, rgit.GitError, subprocess.SubprocessError) as exc:
            self._rollback(retirement, "error", rarch.DEFER_ERROR_S, f"{type(exc).__name__}: {exc}")
            return None
        return retirement

    def _check_holders(self, retirements: dict[str, rarch.Retirement], *, second: bool) -> None:
        wanted = {job_id: r for job_id, r in retirements.items()
                  if r.state == ("archived" if second else "quarantined") and (second or not r.journal.get("check1"))}
        if not wanted:
            return
        try:
            watches = {job_id: r.watch(inodes=second) for job_id, r in wanted.items()}
            busy = self.holders(watches, cancel=self.cancel)
        except ScanFailed as exc:
            if self.cancel is not None and self.cancel.is_set():
                raise rarch.Interrupted("cancelled") from exc
            for job_id, retirement in wanted.items():
                self._rollback(retirement, "holder scan failed", rarch.DEFER_SCAN_FAILED_S, str(exc))
                retirements.pop(job_id, None)
            return
        except (OSError, ValueError) as exc:
            for job_id, retirement in wanted.items():
                self._rollback(retirement, "holder scan failed", rarch.DEFER_SCAN_FAILED_S, str(exc))
                retirements.pop(job_id, None)
            return
        for job_id, retirement in wanted.items():
            if busy.get(job_id):
                self._rollback(retirement, "busy", rarch.DEFER_BUSY_S, "; ".join(busy[job_id][:3]))
                retirements.pop(job_id, None)
            elif not second:
                retirement.save(check1=True)

    def _finish(self, retirement: rarch.Retirement, protected: set[str]) -> None:
        job_id = retirement.job_id
        try:
            retirement.final_check()
            why = retirement.commit(self.sizes.get(job_id, 0))
        except rarch.Defer as exc:
            self._rollback(retirement, exc.reason, exc.seconds, exc.detail)
            return
        except (OSError, ValueError, rgit.GitError) as exc:
            if retirement.state in rarch.COMMITTED:
                self.errors.append({"job_id": job_id, "error": f"after commit: {type(exc).__name__}: {exc}"})
                return
            self._rollback(retirement, "error", rarch.DEFER_ERROR_S, f"{type(exc).__name__}: {exc}")
            return
        if why is not None:
            protected.add(job_id)
            self._rollback(retirement, why, rarch.DEFER_PINNED_S, "")
            return
        self.acted += 1
        self.progress["pruned"].append(job_id)
        self.state.sizes.pop(job_id, None)
        self.state.sliced.pop(job_id, None)
        self._complete(retirement)

    def _complete(self, retirement: rarch.Retirement) -> None:
        job_id = retirement.job_id
        pool = (retirement.journal or {}).get("pool")
        try:
            if retirement.state == "committed":
                retirement.publish()
            report = retirement.reclaim()
        except (OSError, ValueError, rgit.GitError) as exc:
            self._incomplete(retirement, f"{type(exc).__name__}: {exc}")
            return
        if not report["done"]:
            self._incomplete(retirement, "; ".join(e["error"] for e in report["errors"][:3]))
            return
        self.acted += 1
        self.progress["reclaimed"].append(job_id)
        # Truthful accounting (d635 disk relief): what the deletion gave back
        # (tracked files a remote holds and regenerable output, deleted without
        # a copy) apart from what it moved into the archive (clones: deleting
        # the originals frees nothing) and what the archive itself added (its
        # bundle and metadata, N1). `unlinked_bytes` is every file removed.
        moved = rarch.accounting(report.get("totals") or {}, report.get("added_bytes") or 0)
        by_pool = self.freed_by_pool.setdefault(pool or "unknown", dict.fromkeys(FREED_KEYS, 0))
        for key in FREED_KEYS:
            self.progress[key] += moved[key]
            by_pool[key] += moved[key]
        data = {"deleted": report["deleted"], "unlinked_bytes": report["bytes"], **moved,
                "errors": report["errors"][:20]}
        if report["kept"]:
            data["kept"] = report["kept"][:50]
            data["kept_count"] = len(report["kept"])
            data["conflicts"] = str(self.root / "retention-conflicts" / job_id)
            if report.get("late_anchor"):
                data["late_anchor"] = report["late_anchor"]
            self.progress["conflicts"].append({"job_id": job_id, "kept": len(report["kept"])})
            self.store.add_event("retention.conflict", job_id=job_id, data=data)
        self.store.add_event("retention.reclaimed", job_id=job_id, data=data)
        for event in self.ctx.events:
            self.store.add_event(event.pop("kind"), job_id=event.pop("job_id", None), data=event)
        self.ctx.events.clear()

    def _incomplete(self, retirement: rarch.Retirement, error: str) -> None:
        """Deletion could not finish (a permission, a flag): the journal keeps it
        for the next pass; the event is written once per retirement."""
        job_id = retirement.job_id
        self.errors.append({"job_id": job_id, "error": f"reclaim: {error}"[:500]})
        if retirement.journal is not None and not retirement.journal.get("incomplete_reported"):
            self.store.add_event("retention.reclaim_incomplete", job_id=job_id, data={"error": error[:500]})
            try:
                retirement.save(incomplete_reported=True)
            except OSError:
                pass

    def _rollback(self, retirement: rarch.Retirement, reason: str, seconds: float, detail: str) -> None:
        """Put the job back and defer it. The archive built so far is kept (an
        idle cache), so the next attempt reads only what changed, unless the
        job is in use again; an error defers it longer each time it repeats
        (review of a9a6cbf4, N10: a dropped cache re-archived the whole tree
        after every persistent error)."""
        job_id = retirement.job_id
        self.acted += 1
        self.state.sliced.pop(job_id, None)
        failures = int((retirement.journal or {}).get("failures") or 0)
        if seconds == rarch.DEFER_ERROR_S:
            failures += 1
            seconds = min(rarch.DEFER_ERROR_MAX_S, rarch.DEFER_ERROR_S * 2 ** (failures - 1))
        keep_cache = not reason.startswith(_IN_USE_AGAIN)
        try:
            if retirement.journal is not None:
                retirement.rollback(reason + (f": {detail}" if detail else ""), keep_cache=keep_cache,
                                    failures=failures, defer_until=time.time() + seconds)
            else:
                with self.store.transaction("retention.rolled_back", job_id=job_id, data={"reason": reason}) as conn:
                    conn.execute("DELETE FROM leases WHERE holder=?", (f"retention:{job_id}",))
                try:
                    os.rmdir(retirement.work)             # made by `begin` before it refused
                except OSError:
                    pass
        except (OSError, ValueError, RuntimeError) as exc:
            self.errors.append({"job_id": job_id, "error": f"rollback failed: {type(exc).__name__}: {exc}"})
        self.state.defer(job_id, seconds, reason, self.clock())
        self.progress["deferred"][job_id] = reason + (f": {detail}" if detail else "")


#: Rollback reasons after which the job is in use again: its cache is dropped.
_IN_USE_AGAIN = ("pinned", "rows kept changing", "retire lease lost", "worktree lease lost")


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
