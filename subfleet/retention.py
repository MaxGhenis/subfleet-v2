"""Bounded job retention, with filesystem work outside store transactions (C-8.4)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from .contracts import DEFAULT_CAPS, RETENTION_MAX_BYTES, RETENTION_MAX_JOBS
from .store import Store, utc_now

_TERMINAL = {"succeeded", "failed", "cancelled", "lost"}
#: C-13.4: the fence holders of the two collectors of an unused allocation.
#: Each yields to a fence the other holds; any other holder refuses removal.
COLLECTORS = ("retention:", "admission-collect:")


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


def _owned_worktree(job: dict[str, Any], state_root: Path, *, used: bool = True) -> Path | None:
    """C-13.4: only daemon-allocated paths strictly below worktrees are owned.

    `jobs.worktree` records the allocation when the first attempt is reserved,
    but admission cuts it earlier (C-6.8). A writable job that is not in place
    and that no attempt has used (`used` false) owns `worktrees/<job id>/` by
    its path alone, and only that exact directory, never one it links to.
    """
    if job.get("in_place") or job.get("sandbox") != "workspace-write":
        return None
    if not job.get("worktree") and used:
        return None
    allocated_root = state_root / "worktrees"
    # A symlinked container must not turn an external directory into our owner
    # boundary. Individual paths are resolved to reject escapes the same way.
    if allocated_root.resolve() != allocated_root:
        raise ValueError("allocated worktree root is a symlink")
    if not job.get("worktree"):
        identity = job["job_id"]
        if Path(identity).name != identity or identity in (".", ".."):
            raise ValueError("invalid job directory name")
        worktree = (allocated_root / identity).resolve()
        if worktree != allocated_root / identity:
            raise ValueError("unused allocated worktree is a symlink")
        return worktree
    worktree = Path(job["worktree"]).resolve()
    if worktree == allocated_root or allocated_root not in worktree.parents:
        raise ValueError("worktree path is outside the daemon's allocated worktrees")
    return worktree


def _git(worktree: Path, *args: str, env: dict[str, str] | None = None,
         cancel: threading.Event | None = None, deadline: float | None = None,
         timeout: float = 15, input: str | None = None, raw: bool = False) -> str:
    _checkpoint(cancel, deadline)
    if deadline is not None:
        timeout = max(.001, min(timeout, deadline - time.monotonic()))
    result = subprocess.run(["git", "-C", str(worktree), *args], env=env, input=input,
                            capture_output=True, text=True, timeout=timeout, check=False)
    _checkpoint(cancel, deadline)
    if result.returncode:
        raise OSError(f"git {args[0]} failed while inspecting or removing allocated worktree")
    return result.stdout if raw else result.stdout.strip()


def _remove_worktree(job: dict[str, Any], state_root: Path,
                     salvage_artifacts: list[dict[str, Any]], expected_worktree: Path | None, *,
                     used: bool = True, cancel: threading.Event | None = None,
                     deadline: float | None = None,
                     remove_timeout_s: float = DEFAULT_CAPS["worktree_add_timeout_s"]) -> None:
    """Verify preservation, then remove both the owned tree and Git registration.

    A forced removal additionally requires a recorded, existing salvage ref
    whose tree exactly matches the current files. This prevents later operator
    edits from being discarded merely because an earlier salvage row exists.
    All Git commands and temporary-index work run outside store transactions.
    """
    _checkpoint(cancel, deadline)
    git = partial(_git, cancel=cancel, deadline=deadline)
    worktree = _owned_worktree(job, state_root, used=used)
    if worktree != expected_worktree:
        raise ValueError("allocated worktree path changed during retention")
    if worktree is None:
        return
    if not used and not job.get("worktree"):
        _remove_unused(job, worktree, cancel=cancel, deadline=deadline, remove_timeout_s=remove_timeout_s)
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


def _registration(listing: str, worktree: Path) -> dict[str, str] | None:
    """`worktree`'s entry in `git worktree list --porcelain`: {"head": sha, "locked": reason}, each when shown."""
    for block in listing.split("\n\n"):
        lines = block.splitlines()
        if lines and lines[0].startswith("worktree ") and Path(lines[0][9:]).resolve() == worktree:
            entry = {}
            for line in lines[1:]:
                if line.startswith("HEAD "):
                    entry["head"] = line[5:]
                elif line == "locked" or line.startswith("locked "):
                    entry["locked"] = line[7:]
            return entry
    return None


def _blob(object_format: str, data: bytes) -> str:
    """The object id git gives `data` as a blob, with no filters applied."""
    return hashlib.new(object_format, b"blob %d\0" % len(data) + data).hexdigest()


def _file_blob(object_format: str, path: str, size: int, cancel, deadline) -> str:
    digest = hashlib.new(object_format, b"blob %d\0" % size)
    with open(path, "rb") as stream:
        while chunk := stream.read(1 << 20):
            _checkpoint(cancel, deadline)
            digest.update(chunk)
    return digest.hexdigest()


def _missing_from_commit(worktree: Path, source: Path, head: str, git: Callable[..., str], *,
                         linked: bool, cancel, deadline, timeout: float) -> bool:
    """Walk the tree; raise if it holds anything `head` does not. True if any of its files are missing.

    The filesystem, not `git status`, is the inventory: every regular file and
    link below the worktree must be the blob `head` has at that path (a file
    checked out through a filter such as `eol` or LFS may match after git's
    own clean filter, which needs the `.git` link), and there may be no other
    kind of entry and no `.git` below the top. Directories hold nothing
    themselves, and a file of the commit may be missing.
    """
    object_format = git(source, "rev-parse", "--show-object-format")
    blobs, links = {}, {}
    for line in git(source, "ls-tree", "-r", "-z", "--full-tree", head, timeout=timeout, raw=True).split("\0"):
        if line:
            meta, _, path = line.partition("\t")
            mode, kind, sha = meta.split()
            if kind == "blob":
                (links if mode == "120000" else blobs)[path] = sha
    present, filtered = set(), []

    def refuse(path: str) -> None:
        raise ValueError(f"unused allocated worktree has something its commit does not ({path})")

    def unreadable(error: OSError) -> None:
        raise error

    for directory, dirnames, filenames in os.walk(worktree, followlinks=False, onerror=unreadable):
        _checkpoint(cancel, deadline)
        base = Path(directory).relative_to(worktree).as_posix()
        base = "" if base == "." else base + "/"
        for name in [*dirnames, *filenames]:
            path, full = base + name, os.path.join(directory, name)
            if path == ".git":
                continue                        # the link itself, checked by the caller
            if name == ".git":
                refuse(path)                    # a repository or another worktree inside
            info = os.lstat(full)
            if stat.S_ISDIR(info.st_mode):
                continue
            if stat.S_ISLNK(info.st_mode):
                if links.get(path) != _blob(object_format, os.readlink(os.fsencode(full))):
                    refuse(path)
            elif stat.S_ISREG(info.st_mode) and path in blobs:
                if _file_blob(object_format, full, info.st_size, cancel, deadline) != blobs[path]:
                    filtered.append(path)
            else:
                refuse(path)
            present.add(path)
    if filtered:
        if not linked or any("\n" in path for path in filtered):
            refuse(filtered[0])
        hashes = git(worktree, "hash-object", "--stdin-paths", input="\n".join(filtered) + "\n",
                     timeout=timeout).split()
        for path, sha in zip(filtered, hashes, strict=True):
            if blobs[path] != sha:
                refuse(path)
    return bool((blobs.keys() | links.keys()) - present)


def _moved(entry: dict[str, str] | None, head: str | None) -> bool:
    """Does a registration name a commit other than `head`? An unborn one (all zeros) names none."""
    shown = (entry or {}).get("head")
    return bool(shown) and set(shown) != {"0"} and shown != head


def _deregister(repository: Path, worktree: Path, head: str | None, git: Callable[..., str]) -> None:
    """Drop the caller's registration of our allocation once its directory is gone. Best effort.

    Only this path's entry, and only while it is at the cut commit (or at no
    commit) under no lock but git's own `initializing`: never a repository-wide
    `git worktree prune`, which would also drop other worktrees' records.
    """
    if not repository.is_dir():
        return
    try:
        entry = _registration(git(repository, "worktree", "list", "--porcelain"), worktree)
        if entry is None or _moved(entry, head) or entry.get("locked") not in (None, "initializing"):
            return
        forced = ("--force", "--force") if "locked" in entry else ("--force",)
        git(repository, "worktree", "remove", *forced, "--", str(worktree))
    except (OSError, subprocess.SubprocessError):
        pass


def _remove_unused(job: dict[str, Any], worktree: Path, *, cancel: threading.Event | None,
                   deadline: float | None, remove_timeout_s: float) -> None:
    """C-13.4: remove the allocation of a job no attempt used, if it holds nothing its commit does not.

    No attempt means no salvage ref, so what may go is only what the cut
    commit (`jobs.workdir_head`) already has: HEAD, the index, and every file
    and link are checked against it (`_missing_from_commit`). Git answers
    through the `.git` link when it can. When it cannot (no link, or no HEAD
    behind it: an add or a removal stopped partway), the caller's repository
    answers for the commit and for the registration; the directory is deleted
    and only its own registration dropped. A clean tree goes with a plain
    `git worktree remove`; one missing files or index entries needs `--force`,
    and `-f -f` under git's own `initializing` lock. Any other lock is kept.
    Nothing on disk means nothing to check: only a stale registration is
    dropped, if the repository is there to ask.
    """
    git = partial(_git, cancel=cancel, deadline=deadline)
    repository, head = Path(job["workdir"]), job.get("workdir_head")
    if not os.path.lexists(worktree):
        _deregister(repository, worktree, head, git)
        return
    if not head:
        raise ValueError("unused allocated worktree has no recorded commit")
    link = worktree / ".git"
    if os.path.lexists(link) and (link.is_symlink() or not link.is_file()):
        raise ValueError("unused allocated path is not a linked worktree")
    linked = os.path.lexists(link)
    if linked:
        try:
            current = git(worktree, "rev-parse", "--verify", "HEAD")
        except OSError:
            linked = False                      # a link with no HEAD behind it: an add stopped early
    source = worktree if linked else repository
    if not linked and not repository.is_dir():
        raise ValueError("unused allocated worktree cannot be checked: its repository is gone")
    entry = _registration(git(source, "worktree", "list", "--porcelain"), worktree)
    if linked and entry is None:
        raise ValueError("allocated path is not a registered Git worktree")
    lock = (entry or {}).get("locked")
    if lock is not None and lock != "initializing":
        raise ValueError(f"unused allocated worktree is locked ({lock or 'no reason given'})")
    if (current != head) if linked else _moved(entry, head):
        raise ValueError("unused allocated worktree is not at the commit it was cut from")
    staged = False
    if linked:
        fields = git(worktree, "diff-index", "--cached", "-z", "HEAD", timeout=remove_timeout_s, raw=True).split("\0")
        for meta, path in zip(fields[::2], fields[1::2]):
            if meta.split()[-1] != "D":
                raise ValueError(f"unused allocated worktree has a staged change ({path})")
            staged = True
    missing = _missing_from_commit(worktree, source, head, git, linked=linked, cancel=cancel,
                                   deadline=deadline, timeout=remove_timeout_s)
    if linked:
        forced = 2 if lock is not None else 1 if missing or staged else 0
        git(worktree, "worktree", "remove", *("--force",) * forced, "--", str(worktree), timeout=remove_timeout_s)
        return
    _checkpoint(cancel, deadline)
    shutil.rmtree(worktree)
    _deregister(repository, worktree, head, git)


def _unused(store: Store, job_id: str) -> dict[str, Any] | None:
    """The job, when it is terminal and no attempt has used its allocation (C-13.4).

    Both facts are final once true: a terminal job is never reserved again, and
    `jobs.worktree` is written in the transaction that inserts its first attempt.
    """
    job = store.get_job(job_id)
    if (job is None or job["state"] not in _TERMINAL or job["worktree"]
            or store.one("SELECT 1 FROM attempts WHERE job_id=?", (job_id,))):
        return None
    return dict(job)


def _user(conn, job_id: str, worktree: Path) -> str | None:
    """Another job that works in or below `worktree` (an in-place job run there), in any state."""
    path, below = str(worktree), str(worktree) + "/"
    row = conn.execute("SELECT job_id FROM jobs WHERE job_id!=? AND (workdir=? OR substr(workdir,1,?)=? "
                       "OR worktree=? OR substr(worktree,1,?)=?) LIMIT 1",
                       (job_id, path, len(below), below, path, len(below), below)).fetchone()
    return row["job_id"] if row else None


def _collect_unused(store: Store, state_root: Path, job_id: str, *, holder: str,
                    cancel: threading.Event | None, deadline: float | None, remove_timeout_s: float) -> bool:
    """Remove one unused allocation under its fence; see `collect_unused_worktree`."""
    _checkpoint(cancel, deadline)
    job = _unused(store, job_id)
    worktree = _owned_worktree(job, state_root, used=False) if job else None
    if worktree is None:
        return False
    lease_key = f"worktree:{worktree}"
    if not os.path.lexists(worktree):
        # Gone already, perhaps by a collection stopped after its removal.
        with store.transaction("retention.lease_released", job_id=job_id) as conn:
            conn.execute("DELETE FROM leases WHERE lease_key=? AND holder=?", (lease_key, holder))
        return False
    with store.transaction("retention.unused_worktree_selected", job_id=job_id,
                           data={"worktree": str(worktree), "holder": holder}) as conn:
        _checkpoint(cancel, deadline)
        if _unused(store, job_id) is None:
            return False
        current = conn.execute("SELECT holder FROM leases WHERE lease_key=?", (lease_key,)).fetchone()
        if current and current["holder"] != holder:
            if current["holder"].startswith(COLLECTORS):
                return False             # the other collector has it
            raise ValueError(f"worktree lease is held by {current['holder']}")
        if user := _user(conn, job_id, worktree):
            raise ValueError(f"job {user} works in this worktree")
        conn.execute("INSERT OR IGNORE INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                     (lease_key, holder, utc_now()))
    try:
        _remove_unused(job, worktree, cancel=cancel, deadline=deadline, remove_timeout_s=remove_timeout_s)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # A refusal is the tree's answer and stands; a failure is git's, and is tried again.
        kind = "kept" if isinstance(exc, ValueError) else "failed"
        with store.transaction(f"retention.unused_worktree_{kind}", job_id=job_id,
                               data={"worktree": str(worktree), "error": str(exc)}) as conn:
            conn.execute("DELETE FROM leases WHERE lease_key=? AND holder=?", (lease_key, holder))
        raise
    # An interruption above keeps the fence, as a pruning selection does; the
    # next collection by the same holder resumes it.
    with store.transaction("retention.unused_worktree_removed", job_id=job_id,
                           data={"worktree": str(worktree)}) as conn:
        conn.execute("DELETE FROM leases WHERE lease_key=? AND holder=?", (lease_key, holder))
    return True


def collect_unused_worktree(store: Store, state_root: str | Path, job_id: str, *, holder: str,
                            cancel: threading.Event | None = None, deadline: float | None = None,
                            remove_timeout_s: float = DEFAULT_CAPS["worktree_add_timeout_s"]) -> bool:
    """C-13.4: remove the worktree admission cut for a job that ended before any attempt.

    Returns whether a worktree was removed. Nothing is done unless the job is
    terminal, `jobs.worktree` is still empty, no attempt row exists, and
    `worktrees/<job id>/` exists: a worktree any attempt used keeps the
    salvage-first rule. The removal holds the `worktree:<path>` fence as
    `holder` (a `COLLECTORS` prefix and the job id); it yields to the other
    collector's fence and is refused by anyone else's, or by another job that
    works in the directory. Raises `ValueError` for a refusal (the tree holds
    something its commit does not, a link, a lock, a user), `OSError` or
    `subprocess.SubprocessError` when git could not answer, and
    `InterruptedError` when `cancel` is set or `deadline` passes; the fence is
    then kept, and the same holder's next collection resumes.
    """
    try:
        return _collect_unused(store, Path(state_root).resolve(), job_id, holder=holder, cancel=cancel,
                               deadline=deadline, remove_timeout_s=remove_timeout_s)
    except _Interrupted as exc:
        raise InterruptedError(f"unused worktree collection stopped: {exc}") from exc


def _ended_before(job: dict[str, Any], cutoff: str) -> bool | None:
    """Did the job end before `cutoff`? None when its end time cannot be read."""
    try:
        ended = datetime.fromisoformat(job["finished_at"].replace("Z", "+00:00"))
        return ended < datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None


def maintenance(store: Store, state_root: str | Path, *, max_jobs: int = RETENTION_MAX_JOBS,
                max_bytes: int = RETENTION_MAX_BYTES, referenced_job_ids: Iterable[str] = (),
                salvage_referenced_elsewhere: Callable[[dict[str, Any]], bool] | None = None,
                cancel: threading.Event | None = None, deadline: float | None = None,
                unused_before: str | None = None,
                remove_timeout_s: float = DEFAULT_CAPS["worktree_add_timeout_s"]) -> dict[str, Any]:
    """Prune oldest unpinned terminal jobs until both C-8.4 limits hold.

    First, whatever the limits, collect each worktree admission cut for a job
    that ended before any attempt (C-13.4), and count the bytes of those that
    remain. Only jobs that ended before `unused_before` (default: now) are
    touched: the daemon passes the start of the admission pass in flight, whose
    list of queued jobs may still hold, and cut a worktree for, a job that
    ended after it. A job that ended since is neither collected nor pruned.

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
                            cancel=cancel, deadline=deadline, progress=progress,
                            unused_before=unused_before or utc_now(), remove_timeout_s=remove_timeout_s)
    except _Interrupted as exc:
        jobs = store.list_jobs()
        # Filesystem removal may have stopped between stages, so the remaining
        # bytes are unknown until the next pass. Conservatively pin every row.
        return {**progress, "protected": sorted(job["job_id"] for job in jobs),
                "bytes_after": None, "jobs_after": len(jobs), "interrupted": str(exc)}


def _maintenance(store, state_root, *, max_jobs, max_bytes, referenced_job_ids,
                 salvage_referenced_elsewhere, cancel, deadline, progress, unused_before, remove_timeout_s):
    if max_jobs < 0 or max_bytes < 0:
        raise ValueError("retention limits must be nonnegative")
    _checkpoint(cancel, deadline)
    state_root = Path(state_root).resolve()
    root = state_root / "jobs"
    errors = progress["errors"]
    # C-13.4: unused allocations first, whatever the limits, in at most half of
    # the time left, so a slow tree never keeps pruning from running. A tree
    # refused once is left to a start of the daemon or to pruning, not re-read
    # every hour.
    sweep_deadline = None if deadline is None else time.monotonic() + (deadline - time.monotonic()) / 2
    refused = {row["job_id"] for row in store.query(
        "SELECT DISTINCT job_id FROM events WHERE kind='retention.unused_worktree_kept'")}
    for job in store.list_jobs():
        if (job["state"] in _TERMINAL and job["sandbox"] == "workspace-write" and not job["in_place"]
                and not job["worktree"] and job["job_id"] not in refused and _ended_before(job, unused_before)):
            try:
                if _collect_unused(store, state_root, job["job_id"], holder=f"retention:{job['job_id']}",
                                   cancel=cancel, deadline=sweep_deadline, remove_timeout_s=remove_timeout_s):
                    progress.setdefault("unused_worktrees", []).append(job["job_id"])
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                errors.append({"job_id": job["job_id"], "error": str(exc)})
            except _Interrupted:
                if cancel is not None and cancel.is_set():
                    raise
                errors.append({"job_id": job["job_id"], "error": "unused worktree collection reached its time"})
                break
    jobs = store.list_jobs()
    used = {row["job_id"] for row in store.query("SELECT DISTINCT job_id FROM attempts")}
    sizes = {}
    for job in jobs:
        _checkpoint(cancel, deadline)
        identity = job["job_id"]
        if Path(identity).name != identity or identity in (".", ".."):
            errors.append({"job_id": identity, "error": "invalid job directory name"})
            continue
        try:
            sizes[identity] = _size(root / identity, cancel=cancel, deadline=deadline)
            worktree = _owned_worktree(job, state_root, used=identity in used)
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
        worktree = _owned_worktree(job, state_root, used=identity in used)
        lease_key = f"worktree:{worktree}" if worktree is not None else None
        lease_holder = f"retention:{identity}"
        with store.transaction("retention.selected", job_id=identity) as conn:
            _checkpoint(cancel, deadline)
            if identity in _pins(store, explicit, landed):
                protected.add(identity)
                continue
            # C-13.4: ownership was read before the pins; a job reserved since
            # then records another answer and waits for the next pass.
            current = conn.execute("SELECT worktree,finished_at FROM jobs WHERE job_id=?", (identity,)).fetchone()
            reserved = bool(conn.execute("SELECT 1 FROM attempts WHERE job_id=?", (identity,)).fetchone())
            if current is None or current["worktree"] != job["worktree"] or reserved != (identity in used):
                protected.add(identity)
                continue
            if worktree is not None and identity not in used and not job["worktree"]:
                # C-13.4: an admission pass may still cut this job's worktree
                # unless the job ended, as the row says now, before that pass
                # began; and one that another job works in is not this job's.
                if _ended_before(dict(current), unused_before) is not True:
                    protected.add(identity)
                    continue
                if user := _user(conn, identity, worktree):
                    protected.add(identity)
                    errors.append({"job_id": identity, "error": f"job {user} works in this worktree"})
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
            _remove_worktree(job, state_root, salvage_artifacts, worktree, used=identity in used,
                             cancel=cancel, deadline=deadline, remove_timeout_s=remove_timeout_s)
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
            "bytes_after": total, "jobs_after": count, "errors": errors,
            "unused_worktrees": progress.get("unused_worktrees", [])}
