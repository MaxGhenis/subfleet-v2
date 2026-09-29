"""One job's retirement by archive: a journaled state machine (C-8.4, C-13.4, design d635).

Before anything of a finished job is deleted, retention puts everything that
exists nowhere else into an archive it owns, verifies the archive by reading it
back, and deletes only entries whose signature is still the archived one:

- every file of the job's allocated worktree and job directory, byte for byte,
  as an APFS clone (a copy on other volumes) — uncommitted, untracked and
  ignored files alike — except a tracked file whose raw bytes are a blob a
  network remote's refs reach in a repository that is not scratch;
- the worktree's admin directory, byte for byte, and every object it names,
  with the job's salvage commits, as one synthetic anchor commit in a bundle of
  every commit no network remote holds;
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

from . import retention_fs as rfs
from . import retention_git as rgit
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
DEFER_PERMANENT_S = 24 * 3600
DEFER_PINNED_S = 3600
#: A cache left by a rolled-back attempt is dropped after this long.
CACHE_KEEP_S = 2 * 86400


class Defer(Exception):
    """Put the job back and try it again after `seconds`."""

    def __init__(self, reason: str, seconds: float, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.seconds = seconds
        self.detail = detail


class Interrupted(Exception):
    """Cancellation (daemon shutdown): stop where we are; the journal resumes it."""


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

    def check(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise Interrupted("cancelled")


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
    """C-13.4: only daemon-allocated paths strictly below worktrees are owned."""
    if job.get("in_place") or job.get("sandbox") != "workspace-write" or not job.get("worktree"):
        return None
    allocated_root = state_root / "worktrees"
    # A symlinked container must not turn an external directory into our owner
    # boundary. Individual paths are resolved to reject escapes the same way,
    # and a link in the job's own place (to another job's tree) is refused.
    if allocated_root.resolve() != allocated_root:
        raise ValueError("allocated worktree root is a symlink")
    if os.path.islink(job["worktree"]):
        raise ValueError("allocated worktree path is a symlink")
    worktree = Path(job["worktree"]).resolve()
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
        j = self.journal or {}
        if not j.get("admin"):
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
        common = None
        if worktree is not None:
            if os.path.lexists(worktree):
                reg, why = rgit.registration(worktree)
                if reg is None and why not in ("no-gitfile", "admin-missing"):
                    raise Defer("registration", DEFER_PERMANENT_S, why or "")
            elif job.get("workdir"):
                reg = rgit.find_registration(Path(job["workdir"]), worktree, cancel=self.ctx.cancel)
            if reg is not None:
                common = reg.common
            elif job.get("workdir") and os.path.isdir(job["workdir"]):
                try:
                    out = rgit.run(["rev-parse", "--git-common-dir"], cwd=Path(job["workdir"]),
                                   cancel=self.ctx.cancel).stdout.decode("utf-8", "surrogateescape").strip()
                    common = Path(os.path.realpath(Path(job["workdir"]) / out))
                except (rgit.GitError, OSError):
                    common = None
        fmt = None
        if common is not None:
            try:
                fmt = rgit.object_format(common, cancel=self.ctx.cancel)
            except (rgit.GitError, OSError) as exc:
                raise Defer("repository unreadable", DEFER_ERROR_S, str(exc)) from exc
        salvage = []
        for row in self.ctx.store.query(
                "SELECT r.artifact_id,r.path FROM artifacts r JOIN attempts a USING(attempt_id) "
                "WHERE a.job_id=? AND r.role='salvage' ORDER BY r.artifact_id", (self.job_id,)):
            ref = row["path"]
            commit = None
            if common is not None and isinstance(ref, str) and ref.startswith("refs/subfleet-salvage/"):
                commit = rgit.resolve(common, ref, cancel=self.ctx.cancel)
            if commit is None:
                # C-8.4: a salvage ref that cannot be put into the archive pins its job, as before.
                raise Defer("salvage not archivable", DEFER_PERMANENT_S, str(ref))
            salvage.append({"artifact_id": row["artifact_id"], "ref": ref, "commit": commit})
        device = os.lstat(self.work).st_dev
        for path in (worktree, job_dir):
            st = _lstat(path) if path is not None else None
            if st is not None and st.st_dev != device:
                raise Defer("cross-device", DEFER_PERMANENT_S, str(path))
        self.journal = {
            "schema": SCHEMA, "job_id": self.job_id, "state": "selected", "pool": pool,
            "worktree": str(worktree) if worktree else None, "job_dir": str(job_dir),
            "workdir": job.get("workdir"), "baseline": job.get("workdir_head"),
            "admin": str(reg.admin) if reg else None, "common": str(common) if common else None,
            "object_format": fmt, "lock": None, "moved": {"worktree": False, "job": False},
            "salvage": salvage, "archive": None, "started_at": _now(),
            "attempts": int(cache.get("attempts", 0)) + 1, "check1": False,
        }
        self.save()

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
        self.save(state="quarantining")
        moves = (("worktree", j["worktree"], self.q_worktree), ("job", j["job_dir"], self.q_job))
        for name, original, target in moves:
            if original is None or j["moved"][name]:
                continue
            if os.path.lexists(target):
                raise RuntimeError(f"quarantine {target} is occupied")
            if os.path.lexists(original):
                if os.path.islink(original):
                    raise Defer("job path is a symlink", DEFER_PERMANENT_S, original)
                os.rename(original, target)
                j["moved"][name] = True
                self.save()
        rfs.sync_path(self.work)
        self.save(state="quarantined")

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
        return builder.run()

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
        manifest = self.manifest()
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
                    if entry is None or not rfs.unchanged(entry["sig"], st):
                        raise Defer("changed after archive", DEFER_CHANGED_S, f"{label}/{rel}")
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
            self.save(state="committing", rows_sha256=digest, archive=name)
            landed = landed_salvage(manifest, j["salvage"])
            data_event = {"pool": j["pool"], "bytes": pool_bytes, "archive": str(self.root / "archive" / name),
                          "archived_bytes": manifest["totals"]["archived_bytes"],
                          "omitted_bytes": manifest["totals"]["omitted_bytes"],
                          "entries": manifest["totals"]["entries"], "anchor": manifest.get("git", {}).get("anchor")}
            outcome = None
            with self.ctx.store.transaction("retention.pruned", job_id=self.job_id, data=data_event) as conn:
                self.ctx.check()
                current = _canonical({"schema": SCHEMA, "job_id": self.job_id, "rows": job_rows(conn, self.job_id)})
                if current != data:
                    outcome = "rows changed"
                else:
                    reason = self.ctx.pinned(self.job_id, landed) if self.ctx.pinned else None
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
        j = self.journal
        assert j is not None and j.get("archive")
        target = self.root / "archive" / j["archive"]
        if not os.path.lexists(target):
            (self.root / "archive").mkdir(mode=0o700, exist_ok=True)
            os.rename(self.building, target)
            rfs.sync_path(self.root / "archive")
            rfs.sync_path(self.work)
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
        j = self.journal
        assert j is not None
        self.save(state="reclaiming")
        manifest = self.manifest()
        trees = manifest["trees"]
        report: dict[str, Any] = {"deleted": 0, "bytes": 0, "kept": [], "errors": [], "late_anchor": None,
                                  "admin_kept": False, "done": False}

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
        if admin and "admin" in trees and os.path.isdir(admin):
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
                if entry is not None and rfs.unchanged(entry["sig"], st):
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

    def rollback(self, reason: str, *, keep_cache: bool) -> dict[str, Any]:
        """Put everything back where it was (design 5.6). Never deletes job bytes."""
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
        lock = j.get("lock")
        if lock and lock.get("created") and not keep_lock:
            path = Path(lock["path"])
            if _read_small(path) == lock["text"]:
                path.unlink()
                rfs.sync_path(path.parent)
        rfs.remove_own_tree(self.work / "verify.git")
        if not keep_cache:
            rfs.remove_own_tree(self.building)
        with self.ctx.store.transaction("retention.rolled_back", job_id=self.job_id,
                                        data={"reason": reason, **({"conflicts": report["conflicts"]} if report["conflicts"] else {})}) as conn:
            conn.execute("DELETE FROM leases WHERE holder=?", (f"retention:{self.job_id}",))
        if keep_cache and os.path.isdir(self.building):
            self.save(state="idle", lock=None, check1=False, reason=reason, idle_since=time.time())
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
        self.progress_path = self.dir / "progress.jsonl"
        self.progress: dict[str, dict[str, Any]] = {}
        self.pending: list[dict[str, Any]] = []

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
        if self.ctx.clock() >= self.slice_end:
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
        git_info: dict[str, Any] = {"common": j.get("common"), "object_format": fmt, "admin": j.get("admin")}
        if reg is not None and j["moved"]["worktree"]:
            remotes = rgit.network_remotes(common, cancel=self.ctx.cancel)
            scratch = rgit.scratch_reason(common, self.state_root, remotes, cancel=self.ctx.cancel)
            git_info.update(remotes=remotes, scratch=scratch)
            if scratch is None:
                head = rgit.resolve(reg.admin, "HEAD", cancel=self.ctx.cancel)
                omit, used = rgit.omission_map(common, [head, j.get("baseline")], rgit.held_arguments(remotes),
                                               cancel=self.ctx.cancel)
                git_info["omission_commits"] = used
        elif common is not None:
            remotes = rgit.network_remotes(common, cancel=self.ctx.cancel)
            git_info.update(remotes=remotes, scratch=rgit.scratch_reason(common, self.state_root, remotes,
                                                                         cancel=self.ctx.cancel))
        trees: dict[str, dict[str, Any]] = {}
        totals = {"entries": 0, "archived_bytes": 0, "omitted_bytes": 0, "stored_files": 0, "clones": 0, "copies": 0}
        files_fd = rfs.open_dir(self.files)
        try:
            for label, path in self.r.trees():
                entries = self._walk(label, path, omit if label == "worktree" else {}, fmt, files_fd, totals)
                trees[label] = {"original": self._original(label), "entries": entries}
            self._verify_omissions(trees, common, fmt, files_fd, totals)
        finally:
            os.close(files_fd)
        if common is not None:
            git_info.update(self._git(reg, common, fmt))
        manifest = {"schema": SCHEMA, "job_id": self.r.job_id, "created_at": _now(), "trees": trees,
                    "git": git_info, "totals": totals, "salvage": j["salvage"],
                    "restore": "subfleet retention restore " + self.r.job_id}
        data = _canonical(manifest)
        rfs.write_atomic(self.dir / "manifest.json", data)
        rfs.write_atomic(self.dir / "summary.json", _canonical({
            "job_id": self.r.job_id, "created_at": manifest["created_at"], **totals,
            "worktree": j["worktree"], "job_dir": j["job_dir"], "anchor": git_info.get("anchor"),
            "bundle_bytes": git_info.get("bundle_bytes", 0), "scratch": git_info.get("scratch"),
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
              files_fd: int, totals: dict[str, int]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        links: dict[tuple[int, int], str] = {}
        admin = self.j.get("admin")
        try:
            fd = rfs.open_dir(path)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise Defer("tree vanished", DEFER_CHANGED_S, f"{label}: {exc}") from exc
        try:
            for rel, st, parent, name in rfs.walk(fd, self._tick):
                entry: dict[str, Any] = {"p": rel, "sig": rfs.signature(st)}
                t = entry["sig"]["t"]
                totals["entries"] += 1
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
        return entries

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
                digest, blob = rfs.read_hashes(fd, before.st_size, fmt, self._tick)
                after = os.fstat(fd)
                if not rfs.same_content_signature(before, after):
                    raise rfs.TreeError("changed", f"{entry['p']} changed while it was read")
                self._note({"k": key, "sha256": digest, "blob": blob})
                if blob in candidates:
                    entry["blob"], entry["sha256"] = blob, digest
                    totals["omitted_bytes"] += st.st_size
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
        method = rfs.clone_or_copy(fd, files_fd, key, self._tick)
        digest, _ = rfs.read_hashes(fd, before.st_size, None, self._tick)
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
        if not (cached.get("expected") == expected and cached.get("held") == held and bundle.exists()
                and cached.get("state") == state and cached.get("size") == bundle.stat().st_size):
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
            rgit.verify_bundle(common, temporary, expected, fmt, self.r.work / "verify.git",
                               timeout=self.ctx.git_timeout_s * 6, cancel=self.ctx.cancel)
            os.replace(temporary, bundle)
            rfs.sync_path(self.dir)
            self._note({"k": "bundle", "expected": expected, "held": held, "state": state,
                        "size": bundle.stat().st_size})
        info.update(bundle="commits.bundle", bundle_heads=expected, bundle_bytes=bundle.stat().st_size)
        return info

    def _verify_store(self, trees: dict[str, dict[str, Any]]) -> None:
        """Read every stored file back and check its size and sha256."""
        fd = rfs.open_dir(self.files)
        try:
            checked: set[str] = set()
            for tree in trees.values():
                for entry in tree["entries"]:
                    name = entry.get("store")
                    if not name or name in checked:
                        continue
                    checked.add(name)
                    if self.progress.get("v:" + name, {}).get("sha256") == entry["sha256"]:
                        continue
                    self._tick()
                    handle = os.open(name, rfs.O_FILE, dir_fd=fd)
                    try:
                        st = os.fstat(handle)
                        if st.st_size != entry["size"]:
                            raise Defer("archive did not read back", DEFER_ERROR_S, entry["p"])
                        digest, _ = rfs.read_hashes(handle, st.st_size, None, self._tick)
                    except rfs.TreeError as exc:
                        raise Defer("archive did not read back", DEFER_ERROR_S, entry["p"]) from exc
                    finally:
                        os.close(handle)
                    if digest != entry["sha256"]:
                        raise Defer("archive did not read back", DEFER_ERROR_S, entry["p"])
                    self._note({"k": "v:" + name, "sha256": digest})
        finally:
            os.close(fd)

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
        out.append({"archive": name, **summary})
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
        self.process = subprocess.Popen(["git", *rgit.GIT_CONFIG, f"--git-dir={git_dir}", "cat-file", "--batch"],
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
    return sum(a.get("archived_bytes", 0) + a.get("bundle_bytes", 0) for a in list_archives(root) if "error" not in a)


def iter_entries(manifest: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for label, tree in manifest["trees"].items():
        for entry in tree["entries"]:
            yield label, entry
