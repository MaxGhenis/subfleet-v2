"""Bounded job retention, with filesystem work outside store transactions (C-8.4).

Detached jobs and conversation turn jobs are pruned against separate budgets
(C-8.4, C-26.12): each pool drops its own oldest unpinned jobs until its own
count and byte limits hold. A turn job is also kept for `turn_keep_days` after
it ends, and while the conversation service still needs it (its message is not
terminal, its conversation is blocked, or a live runner reads its attempt):
the service answers that through `pins`, which is asked again inside each
delete transaction, like every other pin.

Attachments (C-28.2) are pruned by `prune_attachments`: 30 days after their
last use, once no message still needs them, and then files left without a
row.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from .contracts import (
    ATTACHMENT_KEEP_DAYS, ATTACHMENT_STRAY_GRACE_S, RETENTION_MAX_BYTES, RETENTION_MAX_JOBS, TURN_RETENTION_KEEP_DAYS,
    TURN_RETENTION_MAX_BYTES, TURN_RETENTION_MAX_JOBS,
)
from .conversations.attachments import EXTENSIONS
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
    the next maintenance pass.
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
        # Filesystem removal may have stopped between stages, so the remaining
        # bytes are unknown until the next pass. Conservatively pin every row.
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

    def pinned() -> set[str]:
        return _pins(store, explicit, landed, pins=pins, turn_keep_s=turn_keep_s)

    protected = pinned()
    counts = {name: 0 for name in budgets}
    totals = {name: 0 for name in budgets}
    for job in jobs:
        counts[_pool(job)] += 1
        totals[_pool(job)] += sizes.get(job["job_id"], 0)
    before = sum(totals.values())
    progress["bytes_before"] = before
    progress["pools"] = {name: {"jobs_before": counts[name], "bytes_before": totals[name],
                                "max_jobs": budgets[name][0], "max_bytes": budgets[name][1]} for name in budgets}
    pruned = progress["pruned"]
    for job in reversed(jobs):
        _checkpoint(cancel, deadline)
        pool = _pool(job)
        max_jobs, max_bytes = budgets[pool]
        if all(counts[name] <= budgets[name][0] and totals[name] <= budgets[name][1] for name in budgets):
            break
        if counts[pool] <= max_jobs and totals[pool] <= max_bytes:
            continue
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
            totals[pool] += remaining - sizes.get(identity, 0)
            sizes[identity] = remaining
            with store.transaction("retention.lease_released", job_id=identity) as conn:
                _checkpoint(cancel, deadline)
                conn.execute("DELETE FROM leases WHERE lease_key=? AND holder=?", (lease_key, lease_holder))
                _checkpoint(cancel, deadline)
            _checkpoint(cancel, deadline)
            store.add_event("retention.remove_error", job_id=identity, data=error)
            continue
        with store.transaction("retention.pruned", job_id=identity,
                               data={"bytes": sizes.get(identity, 0), "pool": pool}) as conn:
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
        counts[pool] -= 1
        totals[pool] -= sizes.get(identity, 0)
    for name in budgets:
        progress["pools"][name].update(jobs_after=counts[name], bytes_after=totals[name])
    return {"pruned": pruned, "protected": sorted(protected), "bytes_before": before,
            "bytes_after": sum(totals.values()), "jobs_after": sum(counts.values()), "errors": errors,
            "pools": progress["pools"]}


# --- attachments (C-28.2) ------------------------------------------------------

#: The names `attachment.add` gives a stored copy and its temporary file (C-28.1).
#: Retention removes nothing else from `attachments/`.
_COPY = re.compile(r"([0-9a-f]{64})\.(png|jpg|gif|webp)")
_TEMPORARY = re.compile(r"\.([0-9a-f]{64})\.[0-9a-f]{8}\.tmp")


def prune_attachments(store, *, keep_s: float = ATTACHMENT_KEEP_DAYS * 86400,
                      stray_grace_s: float = ATTACHMENT_STRAY_GRACE_S, now: datetime | None = None,
                      cancel: threading.Event | None = None, deadline: float | None = None) -> dict[str, Any]:
    """Delete attachments no message needs that were last used `keep_s` ago, then
    the files under `attachments/` that no row names (C-28.2).

    `store` is the conversation store. Each deletion holds its hash's guard
    (`store.attachment_guard`, which `attachment.add` also holds) from the
    transaction that re-checks the attachment is still unused and unneeded
    (`delete_unused_attachment`) until the unlink after that transaction commits.
    Retention unlinks only the names `attachment.add` makes, directly under
    `<state root>/attachments`, never a path read from a row, and nothing at all
    when `attachments` is a symlink. A copy with no row (a crash between an add's
    copy and its row, or between a deletion and its unlink) and a temporary copy
    go once `stray_grace_s` has passed since they were last written. Cancellation
    and the deadline are checked between files; the next pass finds what this one
    left.
    """
    moment = now or datetime.now(UTC)
    cutoff = _store_time(moment - timedelta(seconds=keep_s))
    result: dict[str, Any] = {"deleted": [], "bytes": 0, "strays": [], "errors": []}
    directory = Path(store.root) / "attachments"
    try:
        _checkpoint(cancel, deadline)
        if directory.is_symlink():
            result["errors"].append({"path": str(directory), "error": "attachments is a symlink; nothing was removed"})
            return result
        needed = store.needed_attachments()
        after = None
        while page := store.attachments_used_by(cutoff, after=after):
            after = (page[-1]["last_used_at"], page[-1]["sha256"])
            for row in page:
                _checkpoint(cancel, deadline)
                if row["sha256"] not in needed:
                    _delete_attachment(store, directory, row["sha256"], row["media_type"], cutoff, result)
        _sweep_strays(store, directory, moment, stray_grace_s, cancel, deadline, result)
    except _Interrupted as exc:
        result["interrupted"] = str(exc)
    return result


def _delete_attachment(store, directory: Path, sha: str, media_type: str, cutoff: str, result: dict) -> None:
    ext = EXTENSIONS.get(media_type)
    with store.attachment_guard(sha):
        deleted = store.delete_unused_attachment(sha, cutoff)
        if deleted is None:
            return
        result["deleted"].append(sha)
        result["bytes"] += int(deleted["bytes"] or 0)
        name = f"{sha}.{ext}"
        if ext is None or not _COPY.fullmatch(name):
            # Not a name retention made; a copy left behind is a stray the sweep judges.
            result["errors"].append({"sha256": sha, "error": f"no stored copy name for {media_type!r}"})
            return
        try:
            os.unlink(directory / name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            result["errors"].append({"sha256": sha, "error": str(exc)})     # a stray now; a later sweep retries


def _sweep_strays(store, directory: Path, moment: datetime, grace_s: float,
                  cancel: threading.Event | None, deadline: float | None, result: dict) -> None:
    try:
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries)
    except FileNotFoundError:
        return
    except OSError as exc:
        result["errors"].append({"path": str(directory), "error": str(exc)})
        return
    for name in names:
        _checkpoint(cancel, deadline)
        copy = _COPY.fullmatch(name)
        match = copy or _TEMPORARY.fullmatch(name)
        if match is None:
            continue
        path = directory / name
        with store.attachment_guard(match.group(1)):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(info.st_mode) or moment.timestamp() - info.st_mtime < grace_s:
                continue
            if copy and store.attachment(match.group(1)) is not None:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                result["errors"].append({"path": str(path), "error": str(exc)})
                continue
            result["strays"].append(name)


def _store_time(moment: datetime) -> str:
    """The conversation store's time format: UTC, milliseconds, `Z`."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
