"""One job's retirement by archive: a journaled state machine (C-8.4, C-13.4, design d635).

Before anything of a finished job is deleted, retention puts everything that
exists nowhere else into an archive it owns, verifies the archive by reading it
back, and deletes only entries whose signature is still the archived one:

- every file of the job's allocated worktree and job directory, byte for byte,
  as an APFS clone (a copy on other volumes) — uncommitted, untracked and
  ignored files alike — except a tracked file whose raw bytes are a blob a
  network remote's refs reach in a repository that is not scratch, and
  regenerable output (in a directory whose structure says a virtualenv's
  creator, a package manager, Python's bytecode compiler or a tool cache
  wrote it and which git ignores entirely, the files a rule proves the tool
  makes again: `retention_fs.RegenerableWalk`), which is deleted with the
  tree, not archived;
- the worktree's admin directory, byte for byte, and every object it names,
  with the job's salvage commits and baseline, as one synthetic anchor commit,
  the only head of a bundle of every commit no network remote holds;
- the job's database rows, as ``rows.json``.

Layout (all on the state root's volume):

    <state>/retention/<job>/journal.json   the retirement's state, written before each step
    <state>/retention/<job>/worktree/      the quarantined worktree
    <state>/retention/<job>/job/           the quarantined job directory
    <state>/retention/<job>/archive/       the archive while it is built (resumable)
    <state>/archive/<job>/                 the verified archive once the rows are gone
    <state>/retention-conflicts/<job>/     anything deletion found new or changed

While a job's rows exist, none of its bytes has been deleted. After the commit
transaction deletes them, deletion resumes from the journal until it is done.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import folders
from . import retention_fs as rfs
from . import retention_git as rgit
from . import retention_qos as rqos
from .retention_holders import Watch

SCHEMA = 1
ARCHIVE_TABLES = ("jobs", "attempts", "artifacts", "readings", "notices", "decisions")
IN_FLIGHT = ("selected", "locking", "locked", "quarantining", "quarantined", "archived", "committing")
COMMITTED = ("committed", "published", "reclaiming")
#: How long a job waits after a rollback, by reason.
DEFER_BUSY_S = 3600
DEFER_CHANGED_S = 3600
DEFER_SCAN_FAILED_S = 900
DEFER_ERROR_S = 6 * 3600
#: An error that repeats doubles its deferral, up to this (N10).
DEFER_ERROR_MAX_S = 24 * 3600
DEFER_PERMANENT_S = 24 * 3600
DEFER_PINNED_S = 3600
#: A cache left by a rolled-back attempt is dropped after this long.
CACHE_KEEP_S = 2 * 86400
#: A manifest's totals. Apparent sizes (the sum of `st_size`): `archived_bytes`
#: moved into the archive (clones share their blocks, so deleting the originals
#: frees nothing), `omitted_bytes` of tracked files a network remote holds and
#: `regenerable_bytes` of regenerable output, both deleted without a copy;
#: `freed_bytes` is their sum. `freed_disk_bytes` is what deleting those two
#: gives back on disk, measured at archive time: blocks no clone or other link
#: shares (a virtualenv cloned from uv's cache frees little).
#: `copied_bytes` are the stored files the archive holds as byte copies (a
#: volume without clones): new space, counted in `added_bytes`.
TOTALS = ("entries", "archived_bytes", "omitted_bytes", "stored_files", "clones", "copies",
          "regenerable_bytes", "regenerable_entries", "freed_disk_bytes", "copied_bytes")
#: The archive's resumable progress log while it is built; removed at publish.
PROGRESS = "progress.jsonl"
#: The files a published archive adds on disk besides its stored files
#: (final review of e50716e8, N1): the bundle and the metadata.
ADDED = ("commits.bundle", "manifest.json", "summary.json", "rows.json")


class Defer(Exception):
    """Put the job back and try it again after `seconds`."""

    def __init__(self, reason: str, seconds: float, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.seconds = seconds
        self.detail = detail


class Interrupted(Exception):
    """Cancellation (daemon shutdown): stop where we are; the journal resumes it."""


def _require_file_bytes(entry: dict[str, Any], label: str) -> None:
    """A salvage ref never proves preservation of a file its snapshot skipped."""
    name = entry.get("store")
    if entry["sig"]["t"] == "f" and not name and not entry.get("blob") and not entry.get("regen"):
        raise Defer("unarchived path", DEFER_ERROR_S, f"{label}/{entry['p']}")


@dataclass
class Context:
    root: Path
    store: Any
    cancel: threading.Event | None = None
    clock: Callable[[], float] = time.monotonic
    git_timeout_s: float = 600
    #: (job id, landed salvage artifact ids) -> the reason it is pinned, or None;
    #: called inside the commit transaction.
    pinned: Callable[[str, set[int]], str | None] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    #: A job whose source repository has no network remote is kept when its
    #: bundle would carry more history than this (N1); None: no limit.
    remote_less_history_bytes: int | None = None
    #: (repository, heads) -> history bytes, measured once per pass.
    history: dict[tuple[str, tuple[str, ...]], int] = field(default_factory=dict)
    #: (repository, baseline, held refs) -> whether a network remote holds the baseline, once a pass.
    baselines: dict[tuple[str, str | None, tuple[str, ...]], bool] = field(default_factory=dict)
    #: repository -> the history bytes that put one of its jobs over the limit
    #: this pass: the others are kept on it, not measured again each (their
    #: histories are the same one, give or take their own commits).
    history_over: dict[str, int] = field(default_factory=dict)
    #: The repositories the store's jobs are in, read once a pass when a job
    #: whose tree and workdir are both gone needs them (`known_repositories`).
    repositories: list[Path] | None = None
    #: repository -> its salvage refs, read once a pass for the same jobs.
    salvage_listings: dict[str, dict[str, str]] = field(default_factory=dict)

    def check(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise Interrupted("cancelled")

    def known_repositories(self) -> list[Path]:
        if self.repositories is None:
            self.repositories = known_repositories(self.store.list_jobs(), self.root, cancel=self.cancel)
        return self.repositories

    def salvage_refs(self, common: Path) -> dict[str, str]:
        key = str(common)
        if key not in self.salvage_listings:
            try:
                self.salvage_listings[key] = rgit.refs_under(common, "refs/subfleet-salvage/", cancel=self.cancel)
            except (rgit.GitError, OSError):
                self.salvage_listings[key] = {}
        return self.salvage_listings[key]


def journal_path(root: Path, job_id: str) -> Path:
    return root / "retention" / job_id / "journal.json"


def load_journal(root: Path, job_id: str) -> dict[str, Any] | None:
    path = journal_path(root, job_id)
    try:
        data = json.loads(rfs.read_regular(path, limit=64 << 20))
    except FileNotFoundError:
        return None
    if not isinstance(data, dict) or data.get("job_id") != job_id or data.get("schema") != SCHEMA:
        raise ValueError(f"retention journal for {job_id} is not ours")
    return data


def journals(root: Path) -> dict[str, dict[str, Any] | Exception]:
    """Every retirement journal under the state root (a broken one as its error)."""
    found: dict[str, dict[str, Any] | Exception] = {}
    base = root / "retention"
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return found
    for name in names:
        if Path(name).name != name or name.startswith("."):
            continue
        try:
            journal = load_journal(root, name)
        except (OSError, ValueError) as exc:
            found[name] = exc
            continue
        if journal is not None:
            found[name] = journal
    return found


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def job_rows(conn_or_store: Any, job_id: str) -> dict[str, list[dict[str, Any]]]:
    """Every row retention deletes for a job, for ``rows.json``."""
    def q(sql: str, params: tuple) -> list[dict[str, Any]]:
        if hasattr(conn_or_store, "query"):
            return conn_or_store.query(sql, params)
        return [dict(row) for row in conn_or_store.execute(sql, params).fetchall()]
    by_attempt = "attempt_id IN (SELECT attempt_id FROM attempts WHERE job_id=?)"
    return {
        "jobs": q("SELECT * FROM jobs WHERE job_id=?", (job_id,)),
        "attempts": q("SELECT * FROM attempts WHERE job_id=? ORDER BY attempt_id", (job_id,)),
        "artifacts": q(f"SELECT * FROM artifacts WHERE {by_attempt} ORDER BY artifact_id", (job_id,)),
        "readings": q(f"SELECT * FROM readings WHERE {by_attempt} ORDER BY rowid", (job_id,)),
        "notices": q("SELECT * FROM notices WHERE job_id=? ORDER BY notice_id", (job_id,)),
        "decisions": q("SELECT * FROM decisions WHERE job_id=? ORDER BY decision_id", (job_id,)),
    }


def owned_worktree(job: dict[str, Any], state_root: Path) -> Path | None:
    """C-13.4: only daemon-allocated paths strictly below worktrees are owned.

    That is `jobs.worktree`, or, while it is NULL, the `worktrees/<job id>`
    that `_workspace` allocated before the reserving transaction recorded it
    (C-6.12): a job that ended in between, cancelled while it waited, has that
    tree and no row naming it (four live jobs on 2026-09-29; #81's note on
    #76), and without this it was never archived or freed. A committed
    baseline means admission could have allocated that path: keep its lease
    and journal identity even while it is absent, so a moved checkout, an
    existing registration, or a returning allocation cannot be overlooked.
    No bytes at an absent path are touched."""
    if job.get("in_place") or job.get("sandbox") != "workspace-write":
        return None
    allocated_root = state_root / "worktrees"
    path = job.get("worktree")
    if not path:
        unrecorded = allocated_root / job["job_id"]
        if not os.path.lexists(unrecorded) and tree_away(unrecorded) is None and not job.get("workdir_head"):
            return None
        path = str(unrecorded)
    # A symlinked container must not turn an external directory into our owner
    # boundary. Individual paths are resolved to reject escapes the same way,
    # and a link in the job's own place (to another job's tree) is refused.
    if allocated_root.resolve() != allocated_root:
        raise ValueError("allocated worktree root is a symlink")
    if os.path.islink(path):
        raise ValueError("allocated worktree path is a symlink")
    worktree = Path(path).resolve()
    if worktree == allocated_root or allocated_root not in worktree.parents:
        raise ValueError("worktree path is outside the daemon's allocated worktrees")
    return worktree


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


class Retirement:
    """One job's retirement. Every method is safe to call again after a crash."""

    def __init__(self, ctx: Context, job_id: str, journal: dict[str, Any] | None = None):
        if Path(job_id).name != job_id or job_id in (".", "..") or "/" in job_id:
            raise ValueError("invalid job id for retention")
        self.ctx = ctx
        self.job_id = job_id
        self.root = ctx.root
        self.work = ctx.root / "retention" / job_id
        self.q_worktree = self.work / "worktree"
        self.q_job = self.work / "job"
        self.building = self.work / "archive"
        self.conflicts = ctx.root / "retention-conflicts" / job_id
        self.journal = journal if journal is not None else load_journal(ctx.root, job_id)
        #: Progress records the last `archive` call wrote (0: it only re-walked).
        self.last_work = 0

    # --- journal ------------------------------------------------------------------

    @property
    def state(self) -> str | None:
        return None if self.journal is None else self.journal["state"]

    def save(self, **changes: Any) -> None:
        assert self.journal is not None
        self.journal.update(changes, updated_at=_now())
        rfs.write_atomic(self.work / "journal.json", _canonical(self.journal))

    def _drop_journal(self) -> None:
        try:
            (self.work / "journal.json").unlink()
        except FileNotFoundError:
            pass
        rfs.sync_path(self.work)
        self.journal = None

    @property
    def registration(self) -> rgit.Registration | None:
        """The job's registration; None without one, and for a remnant
        (`rgit.is_remnant`), whose bytes are archived but which git cannot use."""
        j = self.journal or {}
        if not j.get("admin") or j.get("admin_remnant"):
            return None
        return rgit.Registration(Path(j["admin"]), Path(j["common"]), b"")

    # --- step 1: begin (after the selection transaction) ------------------------------

    def begin(self, job: dict[str, Any], pool: str) -> None:
        self.ctx.check()
        base = self.root / "retention"
        base.mkdir(mode=0o700, exist_ok=True)
        if base.is_symlink() or self.work.is_symlink():
            raise Defer("retention folder is a symlink", DEFER_PERMANENT_S)
        self.work.mkdir(mode=0o700, exist_ok=True)
        if self.journal is not None and self.journal["state"] != "idle":
            raise RuntimeError(f"{self.job_id} is already being retired ({self.journal['state']})")
        cache = self.journal or {}
        worktree = owned_worktree(job, self.root)
        job_dir = self.root / "jobs" / self.job_id
        reg = None
        remnant = None
        common = None
        gitfile = None
        tree_st = _lstat(worktree) if worktree is not None else None
        present = tree_st is not None
        if worktree is not None and not present:
            away = tree_away(worktree)
            if away is not None:
                # #81's note on #76: `disk-guard` and `worktree-archive-sweep`
                # `git worktree move` a tree to `.disk-guard-removing.<name>`,
                # check it there, and move it back when a check fails. Retired
                # meanwhile, the job's rows would go and the tree come back with
                # no row naming it, never archived or freed.
                raise Defer("tree away", DEFER_CHANGED_S,
                            f"{worktree} is at {away}, another tool's quarantine; kept until it is back or gone")
        salvage_rows = self.ctx.store.query(
            "SELECT r.artifact_id,r.path,r.sha256 FROM artifacts r JOIN attempts a USING(attempt_id) "
            "WHERE a.job_id=? AND r.role='salvage' ORDER BY r.artifact_id", (self.job_id,))
        lost = None
        inferred = False           # the repository was found without the job's workdir
        if worktree is not None:
            if present:
                # One read of the gitfile decides the registration, and its
                # digest is what the tree is held to once it is in quarantine
                # (`_identity_changed`): read while another tool had the tree
                # away, it finds none, and the tree moved back has one.
                read = rgit.gitfile_admin(worktree)
                reg, why = rgit.registration(worktree, read)
                if why != "no-gitfile":
                    gitfile = hashlib.sha256(read[1]).hexdigest()
                if why == "admin-remnant":
                    # Only an index and logs/ are left of the registration:
                    # there is none, and its bytes go into the archive with the
                    # tree instead of keeping the job for ever (N5).
                    remnant = read[0]
                elif why == "admin-missing":
                    self._not_without_host(read[0], worktree)
                elif reg is None and why != "no-gitfile":
                    raise Defer("registration", DEFER_PERMANENT_S, why or "")
            elif job.get("workdir"):
                workdir_common = (rgit.common_dir(Path(job["workdir"]), cancel=self.ctx.cancel)
                                  if os.path.isdir(job["workdir"]) else None)
                inferred = workdir_common is None
                reg, common, lost = source_of_gone_tree(
                    job, worktree, self.ctx.known_repositories, self.ctx.salvage_refs,
                    {row["path"]: row["sha256"] for row in salvage_rows if isinstance(row["path"], str)},
                    cancel=self.ctx.cancel, workdir_common=workdir_common)
                if reg is None and inferred:
                    self._not_without_host(Path(os.path.realpath(job["workdir"])), worktree)
                if lost and lost.startswith("tree away:"):
                    raise Defer("tree away", DEFER_CHANGED_S, lost.removeprefix("tree away: "))
                if lost and not salvage_rows:
                    # No source means no way to bundle a private HEAD still
                    # held by an undiscovered registration. Keep the rows so
                    # a later pass can retire it once its source is available.
                    raise Defer("repository not found", DEFER_PERMANENT_S, lost)
            if reg is not None:
                common = reg.common
            elif common is None and job.get("workdir") and os.path.isdir(job["workdir"]):
                common = rgit.common_dir(Path(job["workdir"]), cancel=self.ctx.cancel)
        fmt = None
        if common is not None:
            try:
                fmt = rgit.object_format(common, cancel=self.ctx.cancel)
            except (rgit.GitError, OSError) as exc:
                raise Defer("repository unreadable", DEFER_ERROR_S, str(exc)) from exc
        salvage = []
        for row in salvage_rows:
            ref = row["path"]
            commit = None
            if common is not None and isinstance(ref, str) and ref.startswith("refs/subfleet-salvage/"):
                commit = rgit.resolve(common, ref, cancel=self.ctx.cancel)
            if commit is None:
                # C-8.4: a salvage ref that cannot be put into the archive pins its job, as before.
                raise Defer("salvage not archivable", DEFER_PERMANENT_S, f"{ref}: {lost}" if lost else str(ref))
            if inferred and salvage_digest(commit) != row["sha256"]:
                # A repository found without the workdir must hold the commit
                # the row recorded, not only a ref of the same name.
                raise Defer("salvage not archivable", DEFER_PERMANENT_S,
                            f"{ref}: in {common} it names {commit[:12]}, not the commit its row recorded")
            salvage.append({"artifact_id": row["artifact_id"], "ref": ref, "commit": commit})
        if common is not None:
            self._remote_less_history(common, reg, job.get("workdir_head"),
                                      [job.get("workdir_head"), *[s["commit"] for s in salvage]],
                                      int(cache.get("history_bytes") or 0))
        device = os.lstat(self.work).st_dev
        for path in (worktree, job_dir):
            st = _lstat(path) if path is not None else None
            if st is not None and st.st_dev != device:
                raise Defer("cross-device", DEFER_PERMANENT_S, str(path))
        admin = reg.admin if reg is not None else remnant
        admin_st = _lstat(admin) if admin is not None else None
        if admin is not None and (admin_st is None or not stat.S_ISDIR(admin_st.st_mode)):
            raise Defer("changed", DEFER_CHANGED_S, f"{admin} went while it was read")
        self.journal = {
            "schema": SCHEMA, "job_id": self.job_id, "state": "selected", "pool": pool,
            "worktree": str(worktree) if worktree else None, "job_dir": str(job_dir),
            "workdir": job.get("workdir"), "baseline": job.get("workdir_head"),
            "admin": str(reg.admin) if reg else (str(remnant) if remnant else None),
            "admin_remnant": remnant is not None, "common": str(common) if common else None,
            "object_format": fmt, "lock": None, "moved": {"worktree": False, "job": False},
            "worktree_present": present if worktree is not None else None,
            # What `_identity_changed` holds the tree and its registration to.
            "identity": {"tree": [tree_st.st_dev, tree_st.st_ino] if tree_st is not None else None,
                         "gitfile": gitfile,
                         "admin": [admin_st.st_dev, admin_st.st_ino] if admin_st is not None else None},
            "salvage": salvage, "archive": None, "started_at": _now(),
            "attempts": int(cache.get("attempts", 0)) + 1, "failures": int(cache.get("failures", 0)),
            "check1": False,
        }
        self.save()

    def _remote_less_history(self, common: Path, reg: rgit.Registration | None, baseline: str | None,
                             heads: list[str | None], seen: int = 0) -> None:
        """Keep a job whose baseline no network remote holds (the repository
        has none, or its remote-tracking refs do not reach the commit the job
        started from) when its bundle would carry more than
        `remote_less_history_bytes`: the bundle then carries the shared
        history too, every object HEAD, the baseline and the salvage commits
        reach that the remotes do not, paid again by each job (about 460 MB per
        `~/chief-of-staff` job; final review of e50716e8, N1). It is measured
        as the bundle is made, against the held refs. `seen` is a bundle an
        earlier attempt measured. Once one job of a repository is over the
        limit in a pass, its others are kept on that measure. Smaller ones
        proceed. A measure that fails keeps the job too."""
        limit = self.ctx.remote_less_history_bytes
        if limit is None:
            return
        try:
            held = rgit.held_arguments(rgit.network_remotes(common, cancel=self.ctx.cancel))
            if self.baseline_held(common, baseline, held):
                return
            if reg is not None:
                heads = [rgit.resolve(reg.admin, "HEAD", cancel=self.ctx.cancel), *heads]
            size = self.ctx.history_over.get(str(common))
            if size is None:
                key = (str(common), tuple(sorted({h for h in heads if h})))
                if key not in self.ctx.history:
                    self.ctx.history[key] = rgit.history_bytes(common, key[1], held, timeout=self.ctx.git_timeout_s,
                                                               cancel=self.ctx.cancel)
                size = self.ctx.history[key]
                if size > limit:
                    self.ctx.history_over[str(common)] = size
            size = max(size, seen)
        except rgit.GitError as exc:
            raise Defer("remote-less-history", DEFER_PERMANENT_S, f"size unknown: {exc}") from exc
        if size > limit:
            raise Defer("remote-less-history", DEFER_PERMANENT_S,
                        f"{size} bytes of history no network remote holds, over {limit} "
                        "(retention.remote_less_history_bytes)")

    def baseline_held(self, common: Path, baseline: str | None, held: list[str]) -> bool:
        """`rgit.baseline_held`, once a pass for each repository, baseline and held refs."""
        key = (str(common), baseline, tuple(held))
        if key not in self.ctx.baselines:
            self.ctx.baselines[key] = rgit.baseline_held(common, baseline, held, timeout=self.ctx.git_timeout_s,
                                                         cancel=self.ctx.cancel)
        return self.ctx.baselines[key]

    def _not_without_host(self, where: Path | None, worktree: Path) -> None:
        found = host_absent(self.root, where, worktree, lambda host: _host_live(self.ctx.store, self.root, host))
        if found is not None:
            raise Defer("nested-host", *found)

    # --- step 2: lock the registration ---------------------------------------------------

    def lock(self) -> None:
        self.ctx.check()
        reg = self.registration
        if reg is None:
            self.save(state="locked")
            return
        path = reg.admin / "locked"
        text = f"{rgit.LOCK_MARKER}{self.job_id}\n"
        existing = _read_small(path)
        if existing is not None:
            if existing != text:
                raise Defer("registration locked by someone else", DEFER_PERMANENT_S, existing.strip()[:200])
            self.save(state="locked", lock={"path": str(path), "text": text, "created": True})
            return
        self.save(state="locking", lock={"path": str(path), "text": text, "created": True})
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
        except FileExistsError:
            if _read_small(path) != text:
                self.save(lock={"path": str(path), "text": text, "created": False})
                raise Defer("registration locked by someone else", DEFER_PERMANENT_S) from None
        else:
            try:
                os.write(fd, text.encode())
                rfs.fullsync(fd)
            finally:
                os.close(fd)
        rfs.sync_path(reg.admin)
        self.save(state="locked")

    # --- step 3: move into quarantine ------------------------------------------------------

    def quarantine(self) -> None:
        self.ctx.check()
        j = self.journal
        assert j is not None
        present = j.get("worktree_present")
        if j["worktree"] is not None and not j["moved"]["worktree"] and present is not None \
                and os.path.lexists(j["worktree"]) != present:
            # The registration was read with the tree there (or gone); a tree
            # moved away since (another tool's `git worktree move`) would have
            # its registration archived and removed while it is elsewhere, and
            # one moved back would go without its registration (#81's note on
            # #76). Read again next time.
            raise Defer("changed", DEFER_CHANGED_S,
                        f"{j['worktree']} {'left' if present else 'came back'} after its registration was read")
        self.save(state="quarantining")
        moves = (("worktree", j["worktree"], self.q_worktree), ("job", j["job_dir"], self.q_job))
        for name, original, target in moves:
            if original is None or j["moved"][name]:
                continue
            if name == "worktree" and present is False:
                continue           # gone at `begin`: never moved in, so one back now is the final check's
            if os.path.lexists(target):
                raise RuntimeError(f"quarantine {target} is occupied")
            if os.path.islink(original):
                raise Defer("job path is a symlink", DEFER_PERMANENT_S, original)
            try:
                os.rename(original, target)
            except FileNotFoundError:
                if name == "worktree":
                    # There at `begin` and at the check above, gone now (the
                    # sweep's move away). Skipped, the job would retire without
                    # it, and the sweep move back a tree no row names.
                    raise Defer("changed", DEFER_CHANGED_S,
                                f"{original} left as it was moved into quarantine") from None
                continue
            j["moved"][name] = True
            self.save()
        rfs.sync_path(self.work)
        changed = self._identity_changed()
        if changed is not None:
            raise Defer("changed", DEFER_CHANGED_S, changed)
        self.save(state="quarantined")

    def _identity_changed(self) -> str | None:
        """Why the tree or its registration is not the one `begin` read, or None.

        `disk-guard` and `worktree-archive-sweep` `git worktree move` a tree to
        `.disk-guard-removing.<name>` and back, at any moment of a retirement
        until retention's lock (or, for a tree with no registration retention
        saw, its quarantine) stops them. Moved away while `begin` read the
        gitfile and back before `quarantine`, the tree was there both times
        and passed the presence check, while the journal named no
        registration (the review of 43f09ea4). So nothing here depends on when
        the sweep moved: the tree, once renamed into quarantine where no other
        tool reaches it by path, must be the directory `begin` saw (device and
        inode) with the gitfile `begin` read (its sha256, or none), and the
        registration the journal names must be the directory `begin` read
        (device and inode) with a backlink that still names the tree's
        original path (`git worktree move` rewrites it to the tree's new
        place). A tree gone at `begin` must still be gone, and not in another
        tool's quarantine beside it. Called after the renames into quarantine
        and as the last step of the final check, the last moment before the
        commit; a journal from before this check has no identity and is read
        again."""
        j = self.journal
        assert j is not None
        if j.get("worktree") is None:
            return None
        identity = j.get("identity")
        if not isinstance(identity, dict):
            return "the journal holds no identity of the tree (written before 2026-10-03); read again"
        original = Path(j["worktree"])
        if j.get("worktree_present"):
            if not j["moved"]["worktree"]:
                return f"{original} was there when its registration was read, and is not in quarantine"
            st = _lstat(self.q_worktree)
            if st is None or not stat.S_ISDIR(st.st_mode) or [st.st_dev, st.st_ino] != identity.get("tree"):
                return f"the tree moved in from {original} is not the directory whose registration was read"
            gitfile = gitfile_digest(self.q_worktree)
            if gitfile != identity.get("gitfile"):
                if identity.get("gitfile") is None:
                    return (f"{original} has a .git now and had none when its registration was read "
                            "(another tool had it away?)")
                return f"{original}/.git is not the one its registration was read from"
        else:
            if os.path.lexists(original):
                return f"{original} came back"
            away = tree_away(original)
            if away is not None:
                return f"{original} is at {away}, another tool's quarantine"
        if j.get("admin"):
            admin = Path(j["admin"])
            st = _lstat(admin)
            if st is None or not stat.S_ISDIR(st.st_mode) or [st.st_dev, st.st_ino] != identity.get("admin"):
                return f"its registration {admin} is not the directory that was read"
            if self.registration is not None:
                names = rgit.backlink(admin)
                if names != Path(os.path.realpath(original / ".git")):
                    return f"its registration {admin} names {names}, not {original}"
        return None

    def _reconcile_moves(self) -> None:
        """After a crash inside `quarantine`: which renames happened."""
        j = self.journal
        assert j is not None
        for name, original, target in (("worktree", j["worktree"], self.q_worktree), ("job", j["job_dir"], self.q_job)):
            if original is not None and not j["moved"][name] and os.path.lexists(target) and not os.path.lexists(original):
                j["moved"][name] = True
        self.save()

    # --- watches for the holder check ------------------------------------------------

    def watch(self, inodes: bool) -> Watch:
        j = self.journal
        assert j is not None
        prefixes = [str(self.work)]
        for key in ("worktree", "job_dir", "admin"):
            if j.get(key):
                prefixes.append(j[key])
        found: set[tuple[int, int]] = set()
        if inodes:
            manifest = self.manifest()
            for tree in manifest["trees"].values():
                for entry in tree["entries"]:
                    if entry["sig"]["t"] in ("f", "d"):
                        found.add((entry["sig"]["dev"], entry["sig"]["ino"]))
        return Watch(prefixes=prefixes, inodes=found)

    # --- step 4: the archive ----------------------------------------------------------

    def trees(self) -> list[tuple[str, Path]]:
        j = self.journal
        assert j is not None
        out: list[tuple[str, Path]] = []
        if j["moved"]["worktree"]:
            out.append(("worktree", self.q_worktree))
        if j["moved"]["job"]:
            out.append(("job", self.q_job))
        if j.get("admin") and os.path.isdir(j["admin"]):
            out.append(("admin", Path(j["admin"])))
        return out

    def archive(self, slice_end: float) -> str:
        """Build and verify the archive; "done", or "parked" when the slice ran out.

        Resumable: every file's hashes and clone are recorded under its
        signature key in ``progress.jsonl``, so a later call re-walks the trees
        (metadata only) and reads again only what changed.
        """
        builder = _Builder(self, slice_end, self.root)
        try:
            with rqos.throttled_io():
                return builder.run()
        finally:
            self.last_work = builder.work

    def manifest(self) -> dict[str, Any]:
        base = self.building
        if self.state not in IN_FLIGHT and self.journal and self.journal.get("archive") \
                and os.path.lexists(self.published_dir()):
            base = self.published_dir()
        return json.loads(rfs.read_regular(base / "manifest.json", limit=4 << 30))

    def published_dir(self) -> Path:
        assert self.journal is not None and self.journal.get("archive")
        return self.root / "archive" / self.journal["archive"]

    # --- step 5: the final check ---------------------------------------------------------

    def final_check(self) -> None:
        """Every archived entry is still there, unchanged, and nothing was added."""
        with rqos.throttled_io():
            self._final_check()

    def _final_check(self) -> None:
        manifest = self.manifest()
        j = self.journal or {}
        if j.get("worktree") and not j["moved"]["worktree"] and os.path.lexists(j["worktree"]):
            # A tree that was gone came back (moved back by another tool):
            # retired now, the job's rows would go and the tree stay with no
            # row naming it (#81's note on #76).
            raise Defer("changed after archive", DEFER_CHANGED_S, f"{j['worktree']} came back")
        for label, path in self.trees():
            tree = manifest["trees"].get(label)
            if tree is None:
                raise Defer("changed after archive", DEFER_CHANGED_S, f"{label} appeared")
            entries = {e["p"]: e for e in tree["entries"]}
            seen = 0
            fd = rfs.open_dir(path)
            try:
                for rel, st, parent, name in rfs.walk(fd, self.ctx.check):
                    entry = entries.get(rel)
                    if entry is None or not rfs.still_archived(entry, st, parent, name, self.ctx.check):
                        raise Defer("changed after archive", DEFER_CHANGED_S, f"{label}/{rel}")
                    # Recovery of an archived journal bypasses the builder;
                    # its coverage proof must still precede row deletion.
                    _require_file_bytes(entry, label)
                    if stat.S_ISLNK(st.st_mode) and os.fsdecode(os.readlink(name, dir_fd=parent)) != entry.get("link"):
                        raise Defer("changed after archive", DEFER_CHANGED_S, f"{label}/{rel}")
                    seen += 1
            except rfs.TreeError as exc:
                raise Defer("changed after archive", DEFER_CHANGED_S, str(exc)) from exc
            finally:
                os.close(fd)
            if seen != len(entries):
                raise Defer("changed after archive", DEFER_CHANGED_S, f"{label}: {len(entries) - seen} entries vanished")
        for label in manifest["trees"]:
            if label not in dict(self.trees()):
                raise Defer("changed after archive", DEFER_CHANGED_S, f"{label} vanished")
        # Last: the archive holds the tree as it is, which a `.git` rewritten
        # before the archive read it passes; the tree and its registration
        # must also still be the ones `begin` read.
        changed = self._identity_changed()
        if changed is not None:
            raise Defer("changed after archive", DEFER_CHANGED_S, changed)

    # --- step 6: commit ------------------------------------------------------------------

    def commit(self, pool_bytes: int) -> str | None:
        """Delete the rows (the point of no return). Returns why it did not, or None."""
        self.ctx.check()
        j = self.journal
        assert j is not None
        manifest = self.manifest()
        name = self._archive_name()
        for _ in range(3):
            rows = job_rows(self.ctx.store, self.job_id)
            data = _canonical({"schema": SCHEMA, "job_id": self.job_id, "rows": rows})
            rfs.write_atomic(self.building / "rows.json", data)
            if rfs.read_regular(self.building / "rows.json") != data:
                raise Defer("rows.json did not read back", DEFER_ERROR_S)
            digest = hashlib.sha256(data).hexdigest()
            added = added_bytes(self.building, manifest["totals"])
            self.save(state="committing", rows_sha256=digest, archive=name, added_bytes=added)
            landed = landed_salvage(manifest, j["salvage"])
            data_event = {"pool": j["pool"], "pool_bytes": pool_bytes, "archive": str(self.root / "archive" / name),
                          **accounting(manifest["totals"], added),
                          "entries": manifest["totals"]["entries"], "anchor": manifest.get("git", {}).get("anchor")}
            outcome = None
            with self.ctx.store.transaction("retention.pruned", job_id=self.job_id, data=data_event) as conn:
                self.ctx.check()
                current = _canonical({"schema": SCHEMA, "job_id": self.job_id, "rows": job_rows(conn, self.job_id)})
                if current != data:
                    outcome = "rows changed"
                else:
                    reason = self.ctx.pinned(self.job_id, landed) if self.ctx.pinned else None
                    # The pins compare `jobs.worktree` as recorded; the journal holds
                    # the canonical spelling the selecting transaction checked. A turn
                    # row on it or on a folder inside it (admission fences only a
                    # turn's own folder, so one may register in a repository nested in
                    # the tree after selection) keeps the job whatever the recorded
                    # spelling, or with none recorded (review of 31048e67, F1).
                    if not reason and j["worktree"] and folders.turn_holds(
                            lambda sql, params: conn.execute(sql, params).fetchall(), j["worktree"], inside=True):
                        reason = "turn-folder"
                    held = {row["lease_key"]: row["holder"] for row in conn.execute(
                        "SELECT lease_key,holder FROM leases WHERE lease_key IN (?,?)",
                        (f"retire:{self.job_id}", f"worktree:{j['worktree']}")).fetchall()}
                    if reason:
                        outcome = f"pinned: {reason}"
                    elif held.get(f"retire:{self.job_id}") != f"retention:{self.job_id}":
                        outcome = "retire lease lost"
                    elif j["worktree"] and held.get(f"worktree:{j['worktree']}") != f"retention:{self.job_id}":
                        outcome = "worktree lease lost"
                    else:
                        attempts = "attempt_id IN (SELECT attempt_id FROM attempts WHERE job_id=?)"
                        conn.execute(f"DELETE FROM artifacts WHERE {attempts}", (self.job_id,))
                        conn.execute(f"DELETE FROM readings WHERE {attempts}", (self.job_id,))
                        for table in ("notices", "decisions", "attempts", "jobs"):
                            conn.execute(f"DELETE FROM {table} WHERE job_id=?", (self.job_id,))
                        conn.execute("DELETE FROM leases WHERE holder=?", (f"retention:{self.job_id}",))
            if outcome == "rows changed":
                continue
            if outcome is not None:
                self.save(state="archived")
                return outcome
            self.save(state="committed")
            return None
        self.save(state="archived")
        return "rows kept changing"

    def _archive_name(self) -> str:
        if self.journal and self.journal.get("archive"):
            return self.journal["archive"]
        base = self.root / "archive"
        name = self.job_id
        n = 1
        while os.path.lexists(base / name):
            n += 1
            name = f"{self.job_id}.{n}"
        return name

    # --- step 7: publish ---------------------------------------------------------------

    def publish(self) -> None:
        """Move the verified archive into place. Its progress log goes: a
        published archive never resumes, and the manifest says everything the
        log did (final review of e50716e8, N6: about 357 bytes per stored file)."""
        j = self.journal
        assert j is not None and j.get("archive")
        target = self.root / "archive" / j["archive"]
        if not os.path.lexists(target):
            (self.root / "archive").mkdir(mode=0o700, exist_ok=True)
            os.rename(self.building, target)
            rfs.sync_path(self.root / "archive")
            rfs.sync_path(self.work)
        try:
            (target / PROGRESS).unlink()
            rfs.sync_path(target)
        except FileNotFoundError:
            pass
        self.save(state="published")

    # --- step 8: verified deletion ---------------------------------------------------------

    def reclaim(self) -> dict[str, Any]:
        """Verified deletion of the quarantined trees, then of the admin directory.

        The admin directory goes last, right after its own late check: a git
        command in the quarantined tree during the tree's deletion (a commit,
        an index write) shows up there, and then the registration is kept,
        locked, with what it names anchored under a `late-` ref. `done` is
        False while anything could not be removed or set aside; the journal
        keeps the retirement for the next pass.
        """
        with rqos.throttled_io():
            return self._reclaim()

    def _reclaim(self) -> dict[str, Any]:
        j = self.journal
        assert j is not None
        self.save(state="reclaiming")
        manifest = self.manifest()
        trees = manifest["trees"]
        report: dict[str, Any] = {"deleted": 0, "bytes": 0, "kept": [], "errors": [], "late_anchor": None,
                                  "admin_kept": False, "done": False, "totals": manifest["totals"],
                                  "added_bytes": added_bytes(self.published_dir(), manifest["totals"])}
        if not isinstance(j.get("identity"), dict) and os.path.lexists(self.q_worktree):
            # An older pass may already have committed the lookup race. Its
            # rows cannot be rolled back, but its remaining tree and private
            # history must not be destroyed by resuming that incomplete
            # archive. Keep the journal and quarantine for recovery. Normal
            # older retirements that archived the registration can finish.
            admin, _, _ = rgit.gitfile_admin(self.q_worktree)
            if admin is not None and admin.is_dir() and not rgit.is_remnant(admin) \
                    and manifest.get("git", {}).get("admin") != str(admin):
                report["errors"].append({"path": str(self.q_worktree),
                                         "error": "registration absent from committed archive; kept for recovery"})
                return report

        def delete(label: str, path: Path) -> None:
            entries = {e["p"]: e for e in trees[label]["entries"]}
            deleter = rfs.Reclaim(entries, self.conflicts, label, check=self.ctx.check)
            deleter.run(path)
            report["deleted"] += deleter.deleted
            report["bytes"] += deleter.bytes
            report["kept"].extend({**k, "tree": label} for k in deleter.kept)
            report["errors"].extend({**e, "tree": label} for e in deleter.errors)

        for label, path in (("worktree", self.q_worktree), ("job", self.q_job)):
            if label in trees:
                delete(label, path)
        admin = j.get("admin")
        claimed = self._claimed(Path(admin)) if admin and "admin" in trees and os.path.isdir(admin) else None
        if claimed is not None:
            # A tree outside the quarantine names the registration again: put
            # back by hand after the final check (the sweep cannot move a
            # tree retention locked). Deleted, the registration would leave
            # that tree a `.git` naming nothing; it stays, unlocked, the
            # tree's again.
            self._remove_lock()
            report["admin_kept"] = True
            report["kept"].append({"path": "", "tree": "admin", "reason": "a tree names it", "where": admin,
                                   "files": [str(claimed)]})
        elif admin and "admin" in trees and os.path.isdir(admin):
            late = self._late_admin(trees["admin"], Path(admin))
            if late is not None:
                report["late_anchor"] = late.get("anchor")
                report["admin_kept"] = True
                report["kept"].append({"path": "", "tree": "admin", "reason": "changed after archive",
                                       "where": admin, "files": late["files"][:50]})
            else:
                delete("admin", Path(admin))
        leftovers = [p for p in (self.q_worktree, self.q_job) if os.path.lexists(p)]
        if leftovers:
            report["errors"].append({"path": str(leftovers[0]), "error": "could not be removed or set aside"})
            return report
        for extra in ("verify.git", "archive"):
            rfs.remove_own_tree(self.work / extra)
        self._drop_journal()
        try:
            os.rmdir(self.work)
        except OSError:
            pass
        report["done"] = True
        return report

    def _claimed(self, admin: Path) -> Path | None:
        """A tree outside the quarantine whose `.git` names `admin`, or None:
        at the tree's original path, in another tool's quarantine beside it,
        or where the admin directory's backlink now says it is."""
        j = self.journal or {}
        if not j.get("worktree"):
            return None
        original = Path(j["worktree"])
        wanted = Path(os.path.realpath(admin))
        names = rgit.backlink(admin)
        for place in (original, tree_away(original), names.parent if names is not None else None):
            if place is not None and os.path.lexists(place) and rgit.gitfile_admin(place)[0] == wanted:
                return place
        return None

    def _remove_lock(self) -> None:
        """Remove the registration's lock if retention wrote it and it is still ours."""
        lock = (self.journal or {}).get("lock")
        if lock and lock.get("created"):
            path = Path(lock["path"])
            if _read_small(path) == lock["text"]:
                path.unlink()
                rfs.sync_path(path.parent)

    def _late_admin(self, tree: dict[str, Any], admin: Path) -> dict[str, Any] | None:
        """Admin entries new or changed since the archive: anchor what they name,
        and keep the registration (locked) instead of deleting it."""
        entries = {e["p"]: e for e in tree["entries"]}
        changed: list[str] = []
        data: list[bytes] = []
        fd = rfs.open_dir(admin)
        try:
            for rel, st, parent, name in rfs.walk(fd, self.ctx.check):
                entry = entries.get(rel)
                if entry is not None and rfs.still_archived(entry, st, parent, name, self.ctx.check):
                    continue
                changed.append(rel)
                if stat.S_ISREG(st.st_mode) and rel != "index" and st.st_size < 64 << 20:
                    try:
                        data.append(rfs.read_regular(admin / rel, limit=64 << 20))
                    except OSError:
                        pass
        except rfs.TreeError as exc:
            changed.append(f"(walk: {exc})")
        finally:
            os.close(fd)
        present = set()
        fd = rfs.open_dir(admin)
        try:
            present = {rel for rel, _, _, _ in rfs.walk(fd)}
        except rfs.TreeError:
            pass
        finally:
            os.close(fd)
        vanished = [p for p in entries if p not in present]
        if not changed and not vanished:
            return None
        anchor = None
        j = self.journal or {}
        if j.get("common") and j.get("object_format"):
            common = Path(j["common"])
            if "index" in changed:
                try:
                    out = rgit.run(["ls-files", "--stage", "-z"], git_dir=admin, cancel=self.ctx.cancel).stdout
                    data.append(b" ".join(r.partition(b"\t")[0] for r in out.split(b"\0")))
                except rgit.GitError:
                    pass
            try:
                anchor = rgit.late_anchor(common, self.job_id, data, j["object_format"], cancel=self.ctx.cancel)
            except rgit.GitError as exc:
                self.ctx.events.append({"kind": "retention.late_anchor_failed", "job_id": self.job_id,
                                        "error": str(exc)})
        return {"files": changed + [f"{p} (vanished)" for p in vanished], "anchor": anchor}

    # --- rollback ------------------------------------------------------------------------

    def rollback(self, reason: str, *, keep_cache: bool, failures: int = 0,
                 defer_until: float | None = None) -> dict[str, Any]:
        """Put everything back where it was (design 5.6). Never deletes job bytes.

        With `keep_cache`, the archive built so far stays with an idle journal
        that records the consecutive error count and, in wall-clock time, when
        the job may be tried again, so a daemon restart does not retry it at
        once and the next attempt reads only what changed (review of a9a6cbf4,
        N8, N10)."""
        j = self.journal
        report: dict[str, Any] = {"reason": reason, "conflicts": []}
        if j is None:
            return report
        if j["state"] in COMMITTED:
            raise RuntimeError("a committed retirement cannot be rolled back")
        if j["state"] == "quarantining":
            self._reconcile_moves()
        keep_lock = False
        for name, original, target in (("worktree", j["worktree"], self.q_worktree), ("job", j["job_dir"], self.q_job)):
            if original is None or not os.path.lexists(target):
                continue
            if os.path.lexists(original):
                # The original path is occupied: ours goes to conflicts, and a
                # lock we wrote stays, because the registration's backlink no
                # longer names a tree git can see (Astra finding 4).
                self.conflicts.mkdir(mode=0o700, parents=True, exist_ok=True)
                aside = self.conflicts / f"{name}-rollback-{int(time.time())}"
                os.rename(target, aside)
                report["conflicts"].append(str(aside))
                if name == "worktree":
                    keep_lock = True
            else:
                os.rename(target, original)
            j["moved"][name] = False
            self.save()
        if not keep_lock:
            self._remove_lock()
        rfs.remove_own_tree(self.work / "verify.git")
        if not keep_cache:
            rfs.remove_own_tree(self.building)
        with self.ctx.store.transaction("retention.rolled_back", job_id=self.job_id,
                                        data={"reason": reason, **({"conflicts": report["conflicts"]} if report["conflicts"] else {})}) as conn:
            conn.execute("DELETE FROM leases WHERE holder=?", (f"retention:{self.job_id}",))
        if keep_cache and os.path.isdir(self.building):
            self.save(state="idle", lock=None, check1=False, reason=reason, idle_since=time.time(),
                      failures=failures, defer_until=defer_until)
        else:
            self._drop_journal()
            try:
                os.rmdir(self.work)
            except OSError:
                pass
        return report

    def drop_cache(self) -> None:
        """Remove what an idle (rolled-back) journal left: our own files only."""
        if self.journal is not None and self.journal["state"] != "idle":
            raise RuntimeError("only an idle cache can be dropped")
        rfs.remove_own_tree(self.building)
        rfs.remove_own_tree(self.work / "verify.git")
        if self.journal is not None:
            self._drop_journal()
        try:
            os.rmdir(self.work)
        except OSError:
            pass


def added_bytes(directory: Path, totals: dict[str, Any]) -> int:
    """The space an archive adds on disk (final review of e50716e8, N1): its
    bundle, manifest, summary and rows (`ADDED`, apparent sizes, as found in
    `directory`) and its byte copies (`copied_bytes`). Clones add nothing
    until the files they replace are gone, which is `archived_bytes`. What
    deleting a byte copy's original gives back is not in `freed_disk_bytes`,
    so the net (`freed_disk_bytes - added_bytes`) errs low."""
    total = int(totals.get("copied_bytes") or 0)
    for name in ADDED:
        try:
            st = os.lstat(directory / name)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
    return total


def entry_bytes(entry: dict[str, Any]) -> int:
    """The bytes one entry takes in `manifest.json` (with its separator)."""
    return len(_canonical(entry)) + 1


def rows_bytes(store: Any, job_id: str) -> int:
    """The size `rows.json` would have for the job's rows now."""
    return len(_canonical({"schema": SCHEMA, "job_id": job_id, "rows": job_rows(store, job_id)}))


def accounting(totals: dict[str, Any], added: int = 0) -> dict[str, int]:
    """What a retirement frees, moves into the archive, and adds to it
    (TOTALS, `added_bytes`). An archive of an earlier layout lacks the
    regenerable figures (0)."""
    omitted = int(totals.get("omitted_bytes") or 0)
    regenerable = int(totals.get("regenerable_bytes") or 0)
    return {"archived_bytes": int(totals.get("archived_bytes") or 0), "omitted_bytes": omitted,
            "regenerable_bytes": regenerable, "freed_bytes": omitted + regenerable,
            "freed_disk_bytes": int(totals.get("freed_disk_bytes") or 0), "added_bytes": int(added)}


def known_repositories(jobs: Iterable[dict[str, Any]], root: Path, *,
                       cancel: threading.Event | None = None) -> list[Path]:
    """The common directories of the repositories the jobs' trees and workdirs
    that are still there are in: where a job whose tree and workdir are both
    gone (a lane checkout removed, a folder deleted) may still have its
    registration and its salvage refs (#81's note on #76: 62 of 371 terminal
    owned jobs on 2026-09-29)."""
    found: dict[str, Path] = {}
    jobs = list(jobs)
    for job in jobs:
        try:
            tree = owned_worktree(job, root)
        except ValueError:
            continue
        if tree is not None and os.path.lexists(tree):
            try:
                reg, _ = rgit.registration(tree)
            except OSError:
                continue           # another job's unreadable registration answers nothing here
            if reg is not None:
                found.setdefault(str(reg.common), reg.common)
    for workdir in sorted({job["workdir"] for job in jobs if job.get("workdir")}):
        if cancel is not None and cancel.is_set():
            raise Interrupted("cancelled")
        if os.path.isdir(workdir):
            common = rgit.common_dir(Path(workdir), cancel=cancel)
            if common is not None:
                found.setdefault(str(common), common)
    return list(found.values())


def salvage_digest(commit: str) -> str:
    """What the daemon records as a salvage artifact's `sha256`: the sha256
    of its commit id (`Daemon._salvage`)."""
    return hashlib.sha256(commit.encode()).hexdigest()


_UNRESOLVED_COMMON = object()


def source_of_gone_tree(job: dict[str, Any], worktree: Path, known: Callable[[], list[Path]],
                        salvage_refs: Callable[[Path], dict[str, str]], wanted: dict[str, str], *,
                        cancel: threading.Event | None = None,
                        workdir_common: Any = _UNRESOLVED_COMMON) -> tuple[rgit.Registration | None, Path | None, str | None]:
    """(registration, repository, None) for a job whose tree is gone, or with
    None in place of what was not found, and why the repository was not.

    With its workdir there, the repository is the workdir's and the
    registration the one there whose backlink names the tree, as always. With
    the workdir gone too, the registration is looked for, by its name and its
    backlink, in the repository the workdir's nearest existing ancestor is in
    (`rgit.repository_near`), then in every repository `known` names; without
    one, a repository is the job's only when every salvage ref the job's rows
    name (`wanted`: ref -> the digest its row recorded) is there at the commit
    the row recorded. A ref's name alone proves nothing: it is the branch, the
    reservation's second and the attempt (`refs/subfleet-salvage/detached-
    20260929T120000Z-a1`), which two jobs of different repositories reserved
    in the same second share. Before, such a job retired with no anchor while
    its registration stayed behind, or was kept for ever as `salvage not
    archivable` (#81's note on #76). A caller that already resolved the
    workdir's repository passes `workdir_common` (None when unreadable), so
    git is run only once for that resolution. An existing but broken linked
    workdir uses the same fallback as a missing one."""
    workdir = job.get("workdir")
    if workdir and os.path.isdir(workdir):
        common = (rgit.common_dir(Path(workdir), cancel=cancel)
                  if workdir_common is _UNRESOLVED_COMMON else workdir_common)
        if common is not None:
            reg = rgit.registration_in(common, worktree)
            if reg is not None:
                return reg, common, None
            away = rgit.moved_tree(common, worktree)
            if away is not None:
                return None, None, f"tree away: {worktree} is registered at {away}; kept until it is back or gone"
            admin = rgit.named_admin(common, worktree)
            if admin is not None:
                return None, None, f"tree away: {worktree} may still belong to {admin}; read its registration again"
            return None, common, None
    candidates: list[Path] = []
    near = rgit.repository_near(Path(workdir), cancel=cancel) if workdir else None
    for common in ([near] if near is not None else []) + list(known()):
        if common not in candidates:
            candidates.append(common)
    for common in candidates:
        reg = rgit.registration_in(common, worktree, named=True)
        if reg is not None:
            return reg, common, None
    for common in candidates:
        away = rgit.moved_tree(common, worktree)
        if away is not None:
            return None, None, f"tree away: {worktree} is registered at {away}; kept until it is back or gone"
        admin = rgit.named_admin(common, worktree)
        if admin is not None:
            return None, None, f"tree away: {worktree} may still belong to {admin}; read its registration again"
    if wanted:
        for common in candidates:
            listing = salvage_refs(common)
            if all(ref in listing and salvage_digest(listing[ref]) == digest for ref, digest in wanted.items()):
                return None, common, None
    return None, None, f"repository not found (workdir {workdir} is gone)"


def gitfile_digest(tree: Path) -> str | None:
    """The sha256 of the tree's `.git` file; None without one. One that cannot
    be read as a small regular file (a directory, a link) answers a reason,
    which no digest equals."""
    try:
        return hashlib.sha256(rfs.read_regular(tree / ".git", limit=65536)).hexdigest()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"unreadable: {exc}"


def tree_away(worktree: Path) -> Path | None:
    """Where another tool keeps a job's tree it moved aside, or None: an entry
    beside it named ``<anything>.<its name>``. `~/chief-of-staff/bin/disk-guard`
    and `worktree-archive-sweep` `git worktree move` a tree to
    ``.disk-guard-removing.<name>`` (which rewrites its registration's backlink),
    check it there, and either remove it or move it back. Job ids hold no dots,
    so no job's own tree has such a name."""
    suffix = "." + worktree.name
    try:
        names = sorted(os.listdir(worktree.parent))
    except OSError:
        return None
    return next((worktree.parent / name for name in names if name.endswith(suffix)), None)


def host_absent(root: Path, where: Path | None, worktree: Path,
                live: Callable[[Path], bool]) -> tuple[float, str] | None:
    """(deferral, why) when a job's registration (`where`: its gitfile's admin
    directory, or the source directory of a job whose tree is gone) lies
    strictly inside another job's allocated tree and that tree is not there;
    None when it may go on (final review of e50716e8, N4).

    Retired then, the job would have no anchor and no bundle of its own, its
    commits only in the host's archive. The host is pinned while this job has
    rows (`nested-host`), so its tree is normally there. While the host is
    retiring (it has rows or a journal), the job waits an hour; when the host
    is gone for good, a day at a time, saying where its registration went.
    A directory that is a tree's root registers its worktrees in its own
    repository, outside the tree, and a host tree that is there but lacks
    the registration has nothing to wait for: both go on."""
    allocated = Path(os.path.realpath(root / "worktrees"))
    if where is None:
        return None
    try:
        parts = where.relative_to(allocated).parts
    except ValueError:
        return None
    host = allocated / parts[0] if len(parts) >= 2 else None
    if host is None or host == worktree or worktree in where.parents or os.path.lexists(host):
        return None
    if live(host):
        return DEFER_CHANGED_S, f"its registration ({where}) is inside {host}, which is being retired; it goes once that tree is back or archived"
    return DEFER_PERMANENT_S, (f"its registration ({where}) was inside {host}, which is gone; kept rather than "
                               "retired without an anchor of its own (restoring that tree's archive brings the "
                               "registration back)")


def _host_live(store: Any, root: Path, host: Path) -> bool:
    """Whether a job whose allocated tree is `host` still has rows, or is in a retirement."""
    for row in store.query("SELECT worktree FROM jobs WHERE worktree LIKE ?", (f"%/{host.name}",)):
        if row["worktree"] and os.path.realpath(row["worktree"]) == str(host):
            return True
    return any(isinstance(j, dict) and j.get("worktree") and os.path.realpath(j["worktree"]) == str(host)
               for j in journals(root).values())


def landed_salvage(manifest: dict[str, Any], salvage: list[dict[str, Any]]) -> set[int]:
    """Salvage artifacts the verified archive holds: their commit is an ancestor
    of the anchor, and the anchor is the verified bundle's head or a network
    remote already reaches it. Their pins are released at commit (C-8.4)."""
    git = manifest.get("git") or {}
    anchor, ref = git.get("anchor"), git.get("anchor_ref")
    if not anchor or not (git.get("anchor_held") or (git.get("bundle_heads") or {}).get(ref) == anchor):
        return set()
    inside = set(git.get("salvage_in_anchor") or ())
    return {s["artifact_id"] for s in salvage if s["commit"] in inside}


def _read_small(path: Path) -> str | None:
    try:
        return rfs.read_regular(path, limit=65536).decode("utf-8", "replace")
    except FileNotFoundError:
        return None


# --- building the archive ----------------------------------------------------------------

class _Parked(Exception):
    pass


class _Builder:
    def __init__(self, retirement: Retirement, slice_end: float, state_root: Path):
        self.r = retirement
        self.ctx = retirement.ctx
        self.slice_end = slice_end
        self.state_root = state_root
        self.j = retirement.journal
        self.dir = retirement.building
        self.files = self.dir / "files"
        self.progress_path = self.dir / PROGRESS
        self.progress: dict[str, dict[str, Any]] = {}
        self.pending: list[dict[str, Any]] = []
        #: New progress records this call: a slice parks only after at least one,
        #: so a tree whose cached re-walk or one file's read outlasts the slice
        #: still moves forward every pass (review of a9a6cbf4, N3).
        self.work = 0

    # progress --------------------------------------------------------------------------

    def _load(self) -> None:
        self.dir.mkdir(mode=0o700, exist_ok=True)
        self.files.mkdir(mode=0o700, exist_ok=True)
        try:
            text = rfs.read_regular(self.progress_path, limit=4 << 30).decode()
        except FileNotFoundError:
            return
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue       # a torn last line after a crash
            if isinstance(record, dict) and "k" in record:
                self.progress.setdefault(record["k"], {}).update(record)

    def _note(self, record: dict[str, Any]) -> None:
        self.work += 1
        self.progress.setdefault(record["k"], {}).update(record)
        self.pending.append(record)
        if len(self.pending) >= 256:
            self._flush()

    def _flush(self) -> None:
        if not self.pending:
            return
        data = b"".join(_canonical(r) + b"\n" for r in self.pending)
        fd = os.open(self.progress_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        self.pending = []

    def _tick(self) -> None:
        self.ctx.check()
        if self.work and self.ctx.clock() >= self.slice_end:
            raise _Parked()

    # the run -------------------------------------------------------------------------

    def run(self) -> str:
        self._load()
        try:
            return self._run()
        except _Parked:
            return "parked"
        finally:
            self._flush()

    def _run(self) -> str:
        j = self.j
        fmt = j.get("object_format")
        common = Path(j["common"]) if j.get("common") else None
        reg = self.r.registration
        omit: dict[str, dict[str, int]] = {}
        ignored: rgit.Ignored | None = None
        git_info: dict[str, Any] = {"common": j.get("common"), "object_format": fmt, "admin": j.get("admin")}
        if j.get("admin_remnant"):
            git_info["admin_remnant"] = True       # archived as bytes; git cannot read it (N5)
        if reg is not None and j["moved"]["worktree"]:
            remotes = rgit.network_remotes(common, cancel=self.ctx.cancel)
            scratch = rgit.scratch_reason(common, self.state_root, remotes, cancel=self.ctx.cancel)
            git_info.update(remotes=remotes, scratch=scratch)
            if scratch is None:
                head = rgit.resolve(reg.admin, "HEAD", cancel=self.ctx.cancel)
                omit, used = rgit.omission_map(common, [head, j.get("baseline")], rgit.held_arguments(remotes),
                                               cancel=self.ctx.cancel)
                git_info["omission_commits"] = used
            try:
                ignored = rgit.Ignored.of(reg.admin, self.r.q_worktree, timeout=self.ctx.git_timeout_s,
                                          cancel=self.ctx.cancel)
            except rgit.GitError as exc:
                git_info["regenerable_off"] = str(exc)[:300]      # when in doubt, the bytes are archived
        elif common is not None:
            remotes = rgit.network_remotes(common, cancel=self.ctx.cancel)
            git_info.update(remotes=remotes, scratch=rgit.scratch_reason(common, self.state_root, remotes,
                                                                         cancel=self.ctx.cancel))
        trees: dict[str, dict[str, Any]] = {}
        totals = dict.fromkeys(TOTALS, 0)
        regenerable: list[dict[str, Any]] = []
        files_fd = rfs.open_dir(self.files)
        try:
            for label, path in self.r.trees():
                # A root found to hold a repository is archived; the finding is
                # progress, so a later slice does not walk into it again.
                prefix = f"nr:{label}:"
                denied = {k[len(prefix):] for k in self.progress if k.startswith(prefix)}
                while True:
                    counts = dict.fromkeys(TOTALS, 0)
                    try:
                        entries, records = self._walk(label, path, omit if label == "worktree" else {}, fmt,
                                                      files_fd, counts, ignored if label == "worktree" else None,
                                                      denied)
                        break
                    except rfs.NotRegenerable as exc:
                        denied.add(exc.root)
                        self._note({"k": prefix + exc.root})
                for key, value in counts.items():
                    totals[key] += value
                regenerable.extend(records)
                trees[label] = {"original": self._original(label), "entries": entries}
            if regenerable and reg is not None:
                # The ignore rules were read before the walk; an index or
                # .gitignore the walk recorded may have changed in between.
                # What changes after the walk, the final check sees.
                again = rgit.Ignored.of(reg.admin, self.r.q_worktree, timeout=self.ctx.git_timeout_s,
                                        cancel=self.ctx.cancel)
                moved = [r["p"] for r in regenerable if not again.clear(r["p"])]
                if moved:
                    raise Defer("changed", DEFER_CHANGED_S, f"ignore rules changed for {moved[0]}")
            self._verify_omissions(trees, common, fmt, files_fd, totals)
        finally:
            os.close(files_fd)
        if common is not None:
            git_info.update(self._git(reg, common, fmt))
        totals["regenerable_bytes"] = sum(r["bytes"] for r in regenerable)
        totals["freed_disk_bytes"] += sum(r["freed_disk_bytes"] for r in regenerable)
        totals["freed_bytes"] = totals["omitted_bytes"] + totals["regenerable_bytes"]
        manifest = {"schema": SCHEMA, "job_id": self.r.job_id, "created_at": _now(), "trees": trees,
                    "git": git_info, "totals": totals, "salvage": j["salvage"], "regenerable": regenerable,
                    "restore": "subfleet retention restore " + self.r.job_id}
        data = _canonical(manifest)
        rfs.write_atomic(self.dir / "manifest.json", data)
        rfs.write_atomic(self.dir / "summary.json", _canonical({
            "job_id": self.r.job_id, "created_at": manifest["created_at"], **totals,
            "worktree": j["worktree"], "job_dir": j["job_dir"], "anchor": git_info.get("anchor"),
            "bundle_bytes": git_info.get("bundle_bytes", 0), "scratch": git_info.get("scratch"),
            "regenerable_dirs": len(regenerable),
            "manifest_sha256": hashlib.sha256(data).hexdigest()}))
        self._verify_store(trees)
        if rfs.read_regular(self.dir / "manifest.json", limit=4 << 30) != data:
            raise Defer("manifest did not read back", DEFER_ERROR_S)
        self._drop_unreferenced(trees)
        rfs.sync_path(self.files)
        rfs.sync_path(self.dir)
        self.r.save(state="archived", manifest_sha256=hashlib.sha256(data).hexdigest())
        return "done"

    def _original(self, label: str) -> str | None:
        j = self.j
        return {"worktree": j["worktree"], "job": j["job_dir"], "admin": j.get("admin")}[label]

    def _walk(self, label: str, path: Path, omit: dict[str, dict[str, int]], fmt: str | None,
              files_fd: int, totals: dict[str, int], ignored: rgit.Ignored | None = None,
              denied: set[str] | frozenset[str] = frozenset()) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Every entry of one tree, archived, omitted or (the worktree only)
        regenerable (`rfs.RegenerableWalk`): listed with its signature, which
        the final check and verified deletion need. Multi-link regenerable
        files also carry a digest to distinguish a sibling's unlink from a
        write; their bytes are never stored (d635 disk relief). Returns the
        entries and the regenerable records."""
        entries: list[dict[str, Any]] = []
        links: dict[tuple[int, int], str] = {}
        admin = self.j.get("admin")
        try:
            fd = rfs.open_dir(path)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise Defer("tree vanished", DEFER_CHANGED_S, f"{label}: {exc}") from exc
        regen = rfs.RegenerableWalk(fd, ignored.clear, denied) if ignored is not None else None
        try:
            for rel, st, parent, name in rfs.walk(fd, self._tick):
                entry: dict[str, Any] = {"p": rel, "sig": rfs.signature(st)}
                t = entry["sig"]["t"]
                totals["entries"] += 1
                verdict = regen.classify(rel, st, parent, name) if regen is not None else False
                if isinstance(verdict, rfs.Verify):
                    # A file an installed distribution's RECORD lists: dropped
                    # only if its bytes are the ones the RECORD names (N3).
                    digest = self._digest(entry, st, parent, name)
                    verdict = digest == verdict.sha256
                    if verdict:
                        assert regen is not None and name is not None
                        regen.confirm(rel, st, parent, name)
                        entry["sha256"] = digest
                if verdict:
                    entry["regen"] = True
                    totals["regenerable_entries"] += 1
                    if t == "l":
                        entry["link"] = os.fsdecode(os.readlink(name, dir_fd=parent))
                    elif t == "f":
                        entry["size"] = st.st_size
                        if st.st_nlink > 1 and "sha256" not in entry:
                            entry["sha256"] = self._digest(entry, st, parent, name)
                    entries.append(entry)
                    continue
                if t == "l":
                    entry["link"] = os.fsdecode(os.readlink(name, dir_fd=parent))
                elif t == "f" and rel:
                    if label == "worktree" and name.lower() == ".git" and "/" in rel:
                        self._nested_gitfile(parent, name, rel, admin)
                    self._file(entry, st, parent, name, omit.get(rel), fmt, files_fd, links, totals)
                elif t == "d" and rel and label == "worktree" and name.lower() == ".git":
                    pass       # a nested repository: archived byte for byte like everything else
                entries.append(entry)
        except rfs.TreeError as exc:
            seconds = (DEFER_CHANGED_S if exc.reason in ("changed", "swapped", "low-space")
                       else DEFER_PERMANENT_S)
            raise Defer(exc.reason, seconds, f"{label}: {exc.detail}") from exc
        finally:
            os.close(fd)
        if regen is None:
            return entries, []
        # A directory under a tool's entry whose every entry was dropped goes too.
        emptied = regen.finish()
        for entry in entries:
            if entry["p"] in emptied:
                entry["regen"] = True
                totals["regenerable_entries"] += 1
        return entries, regen.summary()

    def _digest(self, entry: dict[str, Any], st: os.stat_result, parent: int, name: str | None) -> str:
        """The sha256 of the file as the walk saw it (recorded in the progress
        log, so a later slice does not read it again)."""
        key = rfs.sig_key(st)
        cached = self.progress.get(key, {}).get("sha256")
        if cached:
            return cached
        self._tick()
        try:
            fd = os.open(name, rfs.O_FILE, dir_fd=parent)
        except PermissionError as exc:
            raise rfs.TreeError("unreadable", f"{entry['p']}: {exc.strerror}") from None
        except FileNotFoundError:
            raise rfs.TreeError("changed", f"{entry['p']} vanished") from None
        try:
            before = os.fstat(fd)
            if not rfs.same_content_signature(st, before) or not stat.S_ISREG(before.st_mode):
                raise rfs.TreeError("changed", f"{entry['p']} changed before it was read")
            digest, _ = rfs.read_hashes(fd, before.st_size, None, self.ctx.check)
            if not rfs.same_content_signature(before, os.fstat(fd)):
                raise rfs.TreeError("changed", f"{entry['p']} changed while it was read")
        finally:
            os.close(fd)
        self._note({"k": key, "sha256": digest})
        return digest

    def _nested_gitfile(self, parent: int, name: str, rel: str, admin: str | None) -> None:
        """A linked worktree inside the job's tree keeps its admin directory in
        another repository, which retention does not archive: keep the job.
        A submodule's gitfile points into our own admin directory, which is."""
        try:
            fd = os.open(name, rfs.O_FILE, dir_fd=parent)
            try:
                text = os.read(fd, 65536).decode("utf-8", "surrogateescape").strip()
            finally:
                os.close(fd)
        except OSError as exc:
            raise rfs.TreeError("unreadable", f"{rel}: {exc}") from exc
        if not text.startswith("gitdir:"):
            return
        target = text[len("gitdir:"):].strip()
        original = Path(self.j["worktree"]) / rel
        resolved = os.path.normpath(target if os.path.isabs(target) else str(original.parent / target))
        if admin and (resolved == admin or resolved.startswith(admin.rstrip("/") + "/")):
            return
        raise rfs.TreeError("nested-linked-worktree", f"{rel} -> {target}")

    def _file(self, entry: dict[str, Any], st: os.stat_result, parent: int, name: str,
              candidates: dict[str, int] | None, fmt: str | None, files_fd: int,
              links: dict[tuple[int, int], str], totals: dict[str, int]) -> None:
        key = rfs.sig_key(st)
        cached = self.progress.get(key, {})
        entry["size"] = st.st_size
        if st.st_nlink > 1 and (st.st_dev, st.st_ino) in links:
            entry["hl"] = links[(st.st_dev, st.st_ino)]
            first = self.progress.get(links[(st.st_dev, st.st_ino)], {})
            entry["store"], entry["sha256"] = first["store"], first["sha256"]
            return
        omittable = (candidates is not None and fmt is not None and st.st_nlink == 1
                     and st.st_size in candidates.values())
        if omittable and cached.get("blob") and cached["blob"] in candidates:
            entry["blob"], entry["sha256"] = cached["blob"], cached["sha256"]
            totals["omitted_bytes"] += st.st_size
            totals["freed_disk_bytes"] += rfs.private_bytes(parent, name, st)
            return
        if cached.get("store") and self._stored_ok(files_fd, cached["store"], st.st_size):
            entry["store"], entry["sha256"] = cached["store"], cached["sha256"]
            self._count_store(entry, st, links, key, totals, cached.get("method", "clone"))
            return
        self._tick()
        try:
            fd = os.open(name, rfs.O_FILE, dir_fd=parent)
        except PermissionError as exc:
            raise rfs.TreeError("unreadable", f"{entry['p']}: {exc.strerror}") from None
        except FileNotFoundError:
            raise rfs.TreeError("changed", f"{entry['p']} vanished") from None
        try:
            before = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (st.st_dev, st.st_ino) or not stat.S_ISREG(before.st_mode):
                raise rfs.TreeError("changed", f"{entry['p']} was replaced")
            if omittable:
                digest, blob = rfs.read_hashes(fd, before.st_size, fmt, self.ctx.check)
                after = os.fstat(fd)
                if not rfs.same_content_signature(before, after):
                    raise rfs.TreeError("changed", f"{entry['p']} changed while it was read")
                self._note({"k": key, "sha256": digest, "blob": blob})
                if blob in candidates:
                    entry["blob"], entry["sha256"] = blob, digest
                    totals["omitted_bytes"] += st.st_size
                    totals["freed_disk_bytes"] += rfs.private_bytes(parent, name, st)
                    return
            self._store(entry, fd, before, key, files_fd, links, totals)
        finally:
            os.close(fd)

    def _store(self, entry: dict[str, Any], fd: int, before: os.stat_result, key: str, files_fd: int,
               links: dict[tuple[int, int], str], totals: dict[str, int]) -> None:
        try:
            os.unlink(key, dir_fd=files_fd)       # a clone of a crashed attempt, unrecorded
        except FileNotFoundError:
            pass
        method = rfs.clone_or_copy(fd, files_fd, key, self.ctx.check)
        digest, _ = rfs.read_hashes(fd, before.st_size, None, self.ctx.check)
        after = os.fstat(fd)
        if not rfs.same_content_signature(before, after):
            os.unlink(key, dir_fd=files_fd)
            raise rfs.TreeError("changed", f"{entry['p']} changed while it was archived")
        os.chmod(key, 0o600, dir_fd=files_fd, follow_symlinks=False)
        entry["store"], entry["sha256"] = key, digest
        self._note({"k": key, "store": key, "sha256": digest, "method": method})
        self._count_store(entry, before, links, key, totals, method)

    def _count_store(self, entry: dict[str, Any], st: os.stat_result, links: dict[tuple[int, int], str],
                     key: str, totals: dict[str, int], method: str) -> None:
        totals["archived_bytes"] += st.st_size
        totals["stored_files"] += 1
        totals["clones" if method == "clone" else "copies"] += 1
        if method != "clone":
            totals["copied_bytes"] += st.st_size
        if st.st_nlink > 1:
            links[(st.st_dev, st.st_ino)] = key

    def _stored_ok(self, files_fd: int, name: str, size: int) -> bool:
        try:
            st = os.stat(name, dir_fd=files_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return stat.S_ISREG(st.st_mode) and st.st_size == size

    def _verify_omissions(self, trees: dict[str, dict[str, Any]], common: Path | None, fmt: str | None,
                          files_fd: int, totals: dict[str, int]) -> None:
        """Every omitted blob must read back whole and hash to its id; a file whose
        blob does not is archived after all (Astra finding 1)."""
        omitted = [e for e in trees.get("worktree", {}).get("entries", []) if e.get("blob")]
        if not omitted:
            return
        assert common is not None and fmt is not None
        bad: set[str] = set()
        with rgit.ObjectReader(common, fmt, self.ctx.cancel) as reader:
            for oid in sorted({e["blob"] for e in omitted}):
                if self.progress.get("obj:" + oid, {}).get("ok"):
                    continue
                self._tick()
                if reader.verify(oid):
                    self._note({"k": "obj:" + oid, "ok": True})
                else:
                    bad.add(oid)
        if not bad:
            return
        root = rfs.open_dir(self.r.q_worktree)
        try:
            for entry in omitted:
                if entry["blob"] not in bad:
                    continue
                parent, name = _open_parent(root, entry["p"])
                try:
                    fd = os.open(name, rfs.O_FILE, dir_fd=parent)
                    try:
                        st = os.fstat(fd)
                        if not rfs.unchanged(entry["sig"], st):
                            raise Defer("changed", DEFER_CHANGED_S, entry["p"])
                        totals["omitted_bytes"] -= st.st_size
                        totals["freed_disk_bytes"] -= rfs.private_bytes(parent, name, st)
                        del entry["blob"]
                        self._store(entry, fd, st, rfs.sig_key(st), files_fd, {}, totals)
                    finally:
                        os.close(fd)
                finally:
                    if parent != root:
                        os.close(parent)
        finally:
            os.close(root)
        self.ctx.events.append({"kind": "retention.object_untrusted", "job_id": self.r.job_id,
                                "objects": sorted(bad)[:20], "count": len(bad)})

    def _git(self, reg: rgit.Registration | None, common: Path, fmt: str) -> dict[str, Any]:
        """The anchor, its ref, and the bundle, each read back before it counts.

        The anchor is built whenever the repository is known. Its parents are
        every commit the admin directory names (when the job has a
        registration), the salvage commits and the baseline; its tree holds the
        staged blobs and the trees and blobs the admin directory names. The
        anchor's ref is the bundle's only head: `git bundle` leaves out any
        head a network remote already holds, so a salvage ref pushed as a
        branch made the bundle lack it, or be empty (review of a9a6cbf4, B1).
        A salvage commit is recorded instead, and counts as archived when it is
        an ancestor of the anchor, which puts it in the bundle or on a remote.
        """
        j = self.j
        self._tick()
        info: dict[str, Any] = {}
        remotes = rgit.network_remotes(common, cancel=self.ctx.cancel)
        held = rgit.held_arguments(remotes)
        objects = rgit.admin_objects(reg.admin, fmt, cancel=self.ctx.cancel) if reg is not None else rgit.no_objects()
        extra = [s["commit"] for s in j["salvage"]]
        baseline = j.get("baseline")
        if baseline and rgit.classify(common, [baseline], cancel=self.ctx.cancel)["commit"]:
            extra.append(baseline)
        if not extra and not any(objects[kind] for kind in ("commit", "tree", "blob", "tag")):
            return info            # nothing names a commit or object: nothing to anchor
        anchor = rgit.make_anchor(common, self.r.job_id, objects, extra, cancel=self.ctx.cancel)
        ref = rgit.anchor_ref(self.r.job_id, anchor)
        rgit.create_ref(common, ref, anchor, cancel=self.ctx.cancel)
        info.update(anchor=anchor, anchor_ref=ref)
        if reg is not None:
            info.update(admin_objects={k: len(v) for k, v in objects.items()},
                        admin_missing=sorted(objects["missing"])[:100],
                        head=rgit.resolve(reg.admin, "HEAD", cancel=self.ctx.cancel))
            if objects["tag"]:
                info["admin_tags_peeled"] = sorted(objects["tag"])
        info["salvage_in_anchor"] = sorted({s["commit"] for s in j["salvage"]
                                            if rgit.is_ancestor(common, s["commit"], anchor, cancel=self.ctx.cancel)})
        expected = {ref: anchor}
        state = rgit.held_state(common, remotes, cancel=self.ctx.cancel)
        info.update(held=held, held_remotes=remotes)
        if rgit.held_commit(common, anchor, held, cancel=self.ctx.cancel):
            # A remote already reaches the anchor itself (its ref was pushed):
            # every commit it reaches is held, and a bundle would be empty.
            (self.dir / "commits.bundle").unlink(missing_ok=True)     # an earlier attempt's
            info.update(bundle=None, anchor_held=True)
            return info
        bundle = self.dir / "commits.bundle"
        cached = self.progress.get("bundle", {})
        if cached.get("expected") == expected and cached.get("held") == held and bundle.exists() \
                and cached.get("state") == state and cached.get("size") == bundle.stat().st_size:
            # A bundle an earlier slice or attempt made is checked against the
            # limit as it stands now (it may have been lowered since).
            self._over_the_limit(bundle, common, held)
        else:
            temporary = self.dir / "commits.bundle.tmp"
            temporary.unlink(missing_ok=True)
            fd = os.open(self.dir, rfs.O_DIR)
            try:
                if rfs.free_bytes(fd) < rfs.LOW_SPACE_FLOOR:
                    raise Defer("low-space", DEFER_BUSY_S, "not enough free space for the bundle")
            finally:
                os.close(fd)
            rgit.create_bundle(common, temporary, [ref], held, timeout=self.ctx.git_timeout_s * 6,
                               cancel=self.ctx.cancel)
            self._over_the_limit(temporary, common, held)
            rgit.verify_bundle(common, temporary, expected, fmt, self.r.work / "verify.git",
                               timeout=self.ctx.git_timeout_s * 6, cancel=self.ctx.cancel)
            os.replace(temporary, bundle)
            rfs.sync_path(self.dir)
            self._note({"k": "bundle", "expected": expected, "held": held, "state": state,
                        "size": bundle.stat().st_size})
        info.update(bundle="commits.bundle", bundle_heads=expected, bundle_bytes=bundle.stat().st_size)
        return info

    def _over_the_limit(self, bundle: Path, common: Path, held: list[str]) -> None:
        """The bundle decides (N1): one over `remote_less_history_bytes` for a
        job whose baseline no network remote holds is dropped, the job put
        back, and its size kept for the next attempt's check, although git's
        measure before the bundle said less (the bundle also carries the anchor
        and a pack's own overhead) or the limit was lowered since it was made."""
        limit = self.ctx.remote_less_history_bytes
        size = bundle.stat().st_size
        if limit is None or size <= limit or self.r.baseline_held(common, self.j.get("baseline"), held):
            return
        bundle.unlink()
        self.r.save(history_bytes=size)
        raise Defer("remote-less-history", DEFER_PERMANENT_S,
                    f"a {size}-byte bundle no network remote holds, over {limit} "
                    "(retention.remote_less_history_bytes)")

    def _verify_store(self, trees: dict[str, dict[str, Any]]) -> None:
        """Read every stored file back and check its size and sha256. Every copy
        that does not read back is removed, so a kept cache never offers it
        again and the next attempt clones those files anew (N10)."""
        fd = rfs.open_dir(self.files)
        bad: list[str] = []
        try:
            checked: set[str] = set()
            for label, tree in trees.items():
                for entry in tree["entries"]:
                    name = entry.get("store")
                    _require_file_bytes(entry, label)
                    if not name or name in checked:
                        continue
                    checked.add(name)
                    if self.progress.get("v:" + name, {}).get("sha256") == entry["sha256"]:
                        continue
                    self._tick()
                    handle = os.open(name, rfs.O_FILE, dir_fd=fd)
                    try:
                        st = os.fstat(handle)
                        digest = None
                        if st.st_size == entry["size"]:
                            digest, _ = rfs.read_hashes(handle, st.st_size, None, self.ctx.check)
                    except rfs.TreeError:
                        digest = None
                    finally:
                        os.close(handle)
                    if digest != entry["sha256"]:
                        os.unlink(name, dir_fd=fd)
                        bad.append(entry["p"])
                        continue
                    self._note({"k": "v:" + name, "sha256": digest})
        finally:
            os.close(fd)
        if bad:
            raise Defer("archive did not read back", DEFER_ERROR_S,
                        ", ".join(bad[:5]) + (f" and {len(bad) - 5} more" if len(bad) > 5 else ""))

    def _drop_unreferenced(self, trees: dict[str, dict[str, Any]]) -> None:
        """Clones of file versions an earlier attempt saw and this one does not."""
        wanted = {e["store"] for t in trees.values() for e in t["entries"] if e.get("store")}
        fd = rfs.open_dir(self.files)
        try:
            for name in os.listdir(fd):
                if name not in wanted:
                    os.unlink(name, dir_fd=fd)
        finally:
            os.close(fd)


def _open_parent(root: int, rel: str) -> tuple[int, str]:
    parts = rel.split("/")
    fd = root
    for part in parts[:-1]:
        child = rfs.open_dir(part, dir_fd=fd)
        if fd != root:
            os.close(fd)
        fd = child
    return fd, parts[-1]


# --- listing and restoring archives ----------------------------------------------------------

def list_archives(root: Path) -> list[dict[str, Any]]:
    base = root / "archive"
    out: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return out
    for name in names:
        if name.startswith("."):
            continue
        try:
            summary = json.loads(rfs.read_regular(base / name / "summary.json", limit=1 << 20))
        except (OSError, ValueError):
            out.append({"archive": name, "error": "summary unreadable"})
            continue
        out.append({"archive": name, **summary, "added_bytes": added_bytes(base / name, summary)})
    return out


class RestoreError(Exception):
    pass


def check_archive(root: Path, name: str) -> dict[str, Any]:
    """Re-verify a published archive: every stored file, the bundle's heads."""
    base = root / "archive" / name
    manifest = json.loads(rfs.read_regular(base / "manifest.json", limit=4 << 30))
    problems: list[str] = []
    fd = rfs.open_dir(base / "files")
    try:
        seen: set[str] = set()
        for label, tree in manifest["trees"].items():
            for entry in tree["entries"]:
                store = entry.get("store")
                if not store or store in seen:
                    continue
                seen.add(store)
                try:
                    handle = os.open(store, rfs.O_FILE, dir_fd=fd)
                    try:
                        digest, _ = rfs.read_hashes(handle, os.fstat(handle).st_size, None)
                    finally:
                        os.close(handle)
                except (OSError, rfs.TreeError) as exc:
                    problems.append(f"{label}/{entry['p']}: {exc}")
                    continue
                if digest != entry["sha256"]:
                    problems.append(f"{label}/{entry['p']}: sha256 mismatch")
    finally:
        os.close(fd)
    git = manifest.get("git", {})
    if git.get("bundle"):
        try:
            heads = rgit.bundle_heads(base / git["bundle"])
            for ref, oid in git.get("bundle_heads", {}).items():
                if heads.get(ref) != oid:
                    problems.append(f"bundle lacks {ref}")
        except rgit.GitError as exc:
            problems.append(f"bundle: {exc}")
    return {"archive": name, "ok": not problems, "problems": problems, "manifest": manifest}


def restore(root: Path, name: str, *, to: Path | None = None, repository: Path | None = None) -> dict[str, Any]:
    """Put an archive back (design 5.9). Touches only its destinations.

    Without `to`, each tree goes back to its original path, which must not
    exist, and the admin directory back into its repository, re-registering the
    worktree. With `to`, the trees go to ``to/worktree``, ``to/job`` and
    ``to/admin``. Omitted files come from the source repository, or from
    `repository` (any clone of the same project), each checked against its blob
    id. The bundle's objects are fetched into the repository used.
    """
    checked = check_archive(root, name)
    if not checked["ok"]:
        raise RestoreError("archive failed verification: " + "; ".join(checked["problems"][:5]))
    manifest = checked["manifest"]
    base = root / "archive" / name
    git = manifest.get("git", {})
    fmt = git.get("object_format") or "sha1"
    sources = [p for p in (repository, Path(git["common"]) if git.get("common") else None) if p]
    source = next((p for p in sources if p.exists()), None)
    report: dict[str, Any] = {"archive": name, "restored": {}, "fetched": None, "refs": {}}
    if source is not None and (git.get("bundle") or manifest.get("salvage")):
        target = _git_dir(source)
        prefix = f"refs/subfleet-restored/{manifest['job_id']}/"
        if git.get("bundle"):
            rgit.run(["fetch", "--no-write-fetch-head", str(base / git["bundle"]), f"+refs/*:{prefix}*"],
                     git_dir=target, timeout=3600)
            report["fetched"] = str(target)
        # Salvage refs are recorded in the manifest, not bundle heads (their
        # commits are ancestors of the anchor): recreate each under the prefix.
        for s in manifest.get("salvage") or ():
            ref = s.get("ref") or ""
            if not ref.startswith("refs/") or not s.get("commit"):
                continue
            restored = prefix + ref[len("refs/"):]
            if rgit.resolve(target, s["commit"]) != s["commit"]:
                report["refs"][restored] = f"missing: {s['commit']} is not in {target}"
                continue
            rgit.run(["update-ref", "--no-deref", restored, s["commit"]], git_dir=target)
            report["refs"][restored] = s["commit"]
    for label, tree in manifest["trees"].items():
        original = Path(tree["original"]) if tree.get("original") else None
        destination = (to / label) if to is not None else original
        if destination is None:
            continue
        if os.path.lexists(destination):
            if to is None and label == "admin":
                report["restored"][label] = f"kept existing {destination}"
                continue
            raise RestoreError(f"{destination} exists; restore will not overwrite it")
        _restore_tree(base, tree["entries"], destination, source, fmt,
                      skip_lock=(label == "admin"), job_id=manifest["job_id"])
        report["restored"][label] = str(destination)
    # Regenerable output (a virtualenv, node_modules, bytecode, tool caches) was
    # deleted, not archived: the project's own tools make it again.
    report["not_restored"] = [{"path": r["p"], "kind": r["kind"], "bytes": r.get("bytes", 0),
                               "dropped": r.get("roots", [])} for r in manifest.get("regenerable") or ()]
    return report


def _git_dir(path: Path) -> Path:
    out = rgit.run(["rev-parse", "--git-common-dir"], cwd=path if path.is_dir() else path.parent).stdout.decode().strip()
    return Path(os.path.realpath((path if path.is_dir() else path.parent) / out))


def _restore_tree(base: Path, entries: list[dict[str, Any]], destination: Path, source: Path | None,
                  fmt: str, *, skip_lock: bool, job_id: str) -> None:
    by_path = {e["p"]: e for e in entries}
    root = by_path.get("")
    if root is None or root["sig"]["t"] != "d":
        raise RestoreError("archive has no root directory")
    if not destination.parent.parent.is_dir():
        raise RestoreError(f"{destination.parent.parent} does not exist")
    destination.parent.mkdir(mode=0o755, exist_ok=True)     # git removes an empty .git/worktrees
    destination.mkdir(mode=0o700)
    reader = None
    first_link: dict[str, str] = {}
    entries = [e for e in entries if not e.get("regen")]     # deleted, not archived: never recreated
    try:
        for entry in entries:
            rel = entry["p"]
            if not rel:
                continue
            if skip_lock and rel == "locked":
                continue       # retention's own lock on the registration
            sig = entry["sig"]
            path = destination / rel
            t = sig["t"]
            if t == "d":
                path.mkdir(mode=0o700)
            elif t == "l":
                os.symlink(entry["link"], path)
            elif t == "p":
                os.mkfifo(path, 0o600)
            elif t == "f":
                if entry.get("hl") and entry["hl"] in first_link:
                    os.link(first_link[entry["hl"]], path)
                    continue
                if entry.get("store"):
                    src = os.open(base / "files" / entry["store"], rfs.O_FILE)
                    parent = os.open(path.parent, rfs.O_DIR)
                    try:
                        rfs.clone_or_copy(src, parent, path.name)
                    finally:
                        os.close(src)
                        os.close(parent)
                elif entry.get("blob"):
                    if source is None:
                        raise RestoreError(f"{rel}: its blob {entry['blob']} needs the source repository "
                                           "or --repository <any clone of the project>")
                    if reader is None:
                        reader = _BlobReader(_git_dir(source))
                    data = reader.read(entry["blob"])
                    if data is None or rgit.blob_id(data, fmt) != entry["blob"]:
                        raise RestoreError(f"{rel}: blob {entry['blob']} is missing or damaged in {source}")
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    try:
                        os.write(fd, data) if len(data) < (1 << 30) else _write_all(fd, data)
                    finally:
                        os.close(fd)
                else:
                    raise RestoreError(f"{rel}: no content recorded")
                if entry.get("store") and sig.get("nlink", 1) > 1:
                    first_link[entry["store"]] = str(path)
            # sockets and devices are recorded, not recreated
        for entry in entries:
            sig = entry["sig"]
            if sig["t"] in ("f", "p") and os.path.lexists(destination / entry["p"]):
                os.chmod(destination / entry["p"], sig["mode"])
                os.utime(destination / entry["p"], ns=(sig["mtime"], sig["mtime"]))
            elif sig["t"] == "l" and entry["p"]:
                try:
                    os.utime(destination / entry["p"], ns=(sig["mtime"], sig["mtime"]), follow_symlinks=False)
                except (NotImplementedError, OSError):
                    pass
        for entry in sorted((e for e in entries if e["sig"]["t"] == "d"), key=lambda e: e["p"].count("/"), reverse=True):
            path = destination / entry["p"] if entry["p"] else destination
            os.chmod(path, entry["sig"]["mode"])
            os.utime(path, ns=(entry["sig"]["mtime"], entry["sig"]["mtime"]))
    finally:
        if reader is not None:
            reader.close()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


class _BlobReader:
    def __init__(self, git_dir: Path):
        import subprocess
        self.process = subprocess.Popen(rqos.argv(["git", *rgit.GIT_CONFIG, f"--git-dir={git_dir}", "cat-file", "--batch"]),
                                        env=rgit.environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)

    def read(self, oid: str) -> bytes | None:
        assert self.process.stdin and self.process.stdout
        self.process.stdin.write(oid.encode() + b"\n")
        self.process.stdin.flush()
        header = self.process.stdout.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            return None
        size = int(header[2])
        data = self.process.stdout.read(size)
        self.process.stdout.read(1)
        return data

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        self.process.wait()


def total_archive_bytes(root: Path) -> int:
    return sum(a.get("archived_bytes", 0) + a.get("added_bytes", 0) for a in list_archives(root) if "error" not in a)


def iter_entries(manifest: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for label, tree in manifest["trees"].items():
        for entry in tree["entries"]:
            yield label, entry
