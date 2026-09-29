"""A read-only dry run of retention by archive over a live state root (d635).

It applies the same eligibility as a retention pass (pins and their reasons,
pool budgets, oldest first) and the same per-job preflight (registration,
scratch sources, salvage refs, nested linked worktrees, holders) without writing
anything: SQLite is opened read-only, git runs with ``GIT_OPTIONAL_LOCKS=0`` and
only reading commands, and ``lsof`` lists processes. The conversation service's
in-memory pins are not visible from outside the daemon; the report says so.

Byte figures are apparent sizes (the sum of ``st_size``). What a retirement
frees on disk is the omitted tracked files; archived files are APFS clones,
which share their blocks with the files they replace.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import retention as ret
from . import retention_archive as rarch
from . import retention_fs as rfs
from . import retention_git as rgit
from .policy import RETENTION_DEFAULTS
from .retention_holders import ScanFailed, Watch, lsof_holders
from .store import Store


def _walk_sizes(path: Path, omit: dict[str, dict[str, int]] | None) -> dict[str, Any]:
    """Bytes, file count, the tracked bytes omission could leave out (an upper
    bound: same path and size as a held blob), and nested gitfiles."""
    out = {"bytes": 0, "files": 0, "omittable_upper": 0, "nested_gitfiles": [], "error": None}
    try:
        fd = rfs.open_dir(path)
    except FileNotFoundError:
        return out
    except OSError as exc:
        out["error"] = str(exc)
        return out
    try:
        for rel, st, parent, name in rfs.walk(fd):
            if not rel or stat.S_ISDIR(st.st_mode):
                continue
            out["bytes"] += st.st_size
            out["files"] += 1
            if stat.S_ISREG(st.st_mode):
                if omit and rel in omit and st.st_size in omit[rel].values() and st.st_nlink == 1:
                    out["omittable_upper"] += st.st_size
                if name.lower() == ".git" and "/" in rel:
                    out["nested_gitfiles"].append(rel)
    except rfs.TreeError as exc:
        out["error"] = str(exc)
    finally:
        os.close(fd)
    return out


def _throughput(paths: list[Path], budget_bytes: int = 256 << 20) -> float | None:
    """sha256 read throughput in bytes per second over a sample of files."""
    read = 0
    started = time.monotonic()
    for root in paths:
        for directory, _, files in os.walk(root):
            for name in files:
                path = Path(directory) / name
                try:
                    fd = os.open(path, rfs.O_FILE)
                except OSError:
                    continue
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode) or st.st_size < 65536:
                        continue
                    digest = hashlib.sha256()
                    while chunk := os.read(fd, rfs.CHUNK):
                        digest.update(chunk)
                        read += len(chunk)
                finally:
                    os.close(fd)
                if read >= budget_bytes:
                    break
            if read >= budget_bytes:
                break
        if read >= budget_bytes:
            break
    elapsed = time.monotonic() - started
    return read / elapsed if read and elapsed > 0 else None


def survey(root: Path, *, sizes: bool = True, holders: bool = True, sample_throughput: bool = True,
           batch: int = ret.BATCH, budgets: dict[str, tuple[int, int]] | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    started = time.monotonic()
    policy: dict[str, Any] = {}
    try:
        policy = json.loads((root / "policy.json").read_text()).get("retention") or {}
    except (OSError, ValueError):
        pass
    budget = {**RETENTION_DEFAULTS, **policy}
    budgets = budgets or {"detached": (int(budget["jobs"]), int(budget["bytes"])),
                          "turn": (int(budget["turn_jobs"]), int(budget["turn_bytes"]))}
    turn_keep_s = float(budget["turn_keep_days"]) * 86400
    report: dict[str, Any] = {"state_root": str(root), "read_only": True,
                              "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "budgets": {k: {"max_jobs": v[0], "max_bytes": v[1]} for k, v in budgets.items()},
                              "notes": ["the conversation service's in-memory pins are not visible offline; "
                                        "turn jobs it needs would also be kept"]}
    store = Store(root / "state.sqlite3", read_only=True)
    try:
        jobs = list(reversed(store.list_jobs()))
        reasons = ret._pin_reasons(store, set(), None, pins=None, turn_keep_s=turn_keep_s)
        journals = rarch.journals(root)
        salvage = defaultdict(list)
        for row in store.query("SELECT r.artifact_id,r.path,a.job_id FROM artifacts r JOIN attempts a USING(attempt_id) "
                               "WHERE r.role='salvage'"):
            salvage[row["job_id"]].append(row["path"])
    finally:
        store.close()
    rows: dict[str, dict[str, Any]] = {}
    remotes_cache: dict[str, tuple[dict[str, str], str | None, str | None]] = {}
    for job in jobs:
        job_id = job["job_id"]
        info: dict[str, Any] = {"job_id": job_id, "pool": ret._pool(job), "created_at": job["created_at"],
                                "state": job["state"], "kind": job["kind"], "pin": reasons.get(job_id)}
        try:
            worktree = ret._owned_worktree(job, root)
        except ValueError as exc:
            info["issue"] = f"worktree path: {exc}"
            worktree = None
        info["worktree"] = str(worktree) if worktree else None
        info["worktree_exists"] = bool(worktree and os.path.isdir(worktree))
        if job_id in journals:
            info["journal"] = str(journals[job_id]["state"]) if isinstance(journals[job_id], dict) else "unreadable"
        if sizes:
            jobdir = _walk_sizes(root / "jobs" / job_id, None)
            info["job_dir_bytes"] = jobdir["bytes"]
            info["bytes"] = jobdir["bytes"]
        if worktree is not None and info["worktree_exists"] and not info.get("pin"):
            _preflight(info, job, worktree, root, salvage.get(job_id, []), remotes_cache)
        if worktree is not None and info["worktree_exists"] and sizes:
            wt = _walk_sizes(worktree, info.pop("_omit", None))
            info.update(worktree_bytes=wt["bytes"], worktree_files=wt["files"],
                        omittable_upper=wt["omittable_upper"] if not info.get("scratch") else 0)
            info["bytes"] = info.get("bytes", 0) + wt["bytes"]
            if wt["error"]:
                info.setdefault("issue", f"walk: {wt['error']}")
            if wt["nested_gitfiles"] and not info.get("issue"):
                admin = info.get("admin")
                foreign = [g for g in wt["nested_gitfiles"] if not _into_admin(worktree / g, admin)]
                if foreign:
                    info["issue"] = f"nested-linked-worktree: {foreign[0]}"
        info.pop("_omit", None)
        rows[job_id] = info
    # Budgets, oldest first, as a pass decides them (no batch limit here).
    counts = Counter(r["pool"] for r in rows.values())
    totals = defaultdict(int)
    for r in rows.values():
        totals[r["pool"]] += r.get("bytes", 0)
    report["pools"] = {p: {"jobs": counts[p], "bytes": totals[p], "max_jobs": budgets[p][0],
                           "max_bytes": budgets[p][1]} for p in budgets}
    would: list[str] = []
    for job in jobs:
        r = rows[job["job_id"]]
        pool = r["pool"]
        over = counts[pool] > budgets[pool][0] or totals[pool] > budgets[pool][1]
        if not over:
            r["kept"] = "within budget"
            continue
        if r.get("pin"):
            r["kept"] = r["pin"]
            continue
        if r.get("issue"):
            r["kept"] = r["issue"].split(":")[0]
            continue
        would.append(job["job_id"])
        counts[pool] -= 1
        totals[pool] -= r.get("bytes", 0)
    busy: dict[str, list[str]] = {}
    scan_s = None
    if holders:
        watches = {j: Watch(prefixes=[p for p in (rows[j]["worktree"], str(root / "jobs" / j)) if p])
                   for j in would}
        t0 = time.monotonic()
        try:
            busy = lsof_holders(watches)
            scan_s = round(time.monotonic() - t0, 1)
        except ScanFailed as exc:
            report["notes"].append(f"holder scan failed: {exc}")
    retire = [j for j in would if j not in busy]
    for j in busy:
        rows[j]["kept"] = "busy"
        rows[j]["holders"] = busy[j][:3]
    kept = Counter(r["kept"] for r in rows.values() if r.get("kept"))
    kept_bytes = defaultdict(int)
    kept_worktrees = Counter()
    for r in rows.values():
        if r.get("kept"):
            kept_bytes[r["kept"]] += r.get("worktree_bytes", 0)
            if r.get("worktree_exists"):
                kept_worktrees[r["kept"]] += 1
    retiring = [rows[j] for j in retire]
    wt_retiring = [r for r in retiring if r.get("worktree_exists")]
    omittable = sum(r.get("omittable_upper", 0) for r in wt_retiring)
    worktree_bytes = sum(r.get("worktree_bytes", 0) for r in wt_retiring)
    archived = sum(r.get("bytes", 0) for r in retiring) - omittable
    report["would_retire"] = {
        "jobs": len(retire), "with_worktree": len(wt_retiring),
        "bytes": sum(r.get("bytes", 0) for r in retiring), "worktree_bytes": worktree_bytes,
        "job_dir_bytes": sum(r.get("job_dir_bytes", 0) for r in retiring),
        "omittable_bytes_upper_bound": omittable, "archived_bytes": archived,
        "scratch_sources": sum(1 for r in wt_retiring if r.get("scratch")),
        "scratch_reasons": dict(Counter(r["scratch"] for r in wt_retiring if r.get("scratch"))),
        "with_unpushed_commits": sum(1 for r in wt_retiring if r.get("unpushed")),
    }
    report["kept"] = {"jobs_by_reason": dict(kept.most_common()),
                      "worktrees_by_reason": dict(kept_worktrees.most_common()),
                      "worktree_bytes_by_reason": dict(sorted(kept_bytes.items(), key=lambda kv: -kv[1]))}
    report["holders"] = {"busy": len(busy), "scan_seconds": scan_s}
    dirs = set(os.listdir(root / "worktrees")) if (root / "worktrees").is_dir() else set()
    owned_names = {Path(r["worktree"]).name for r in rows.values() if r.get("worktree")}
    orphans = sorted(d for d in dirs if d not in owned_names)
    report["worktree_dirs"] = {"total": len(dirs), "with_job_rows": len(dirs & owned_names),
                               "orphans_never_touched": len(orphans), "orphans": orphans[:40]}
    # How long the first passes take: reading (hash, then verify), git and listings.
    rate = None
    if sample_throughput and wt_retiring:
        rate = _throughput([Path(r["worktree"]) for r in wt_retiring[:6]])
    reads = omittable + 2 * archived
    passes = max(1, math.ceil(len(retire) / max(1, batch)))
    per_job_git_s = 3.0
    report["estimate"] = {
        "hash_read_bytes_per_s": round(rate) if rate else None,
        "bytes_read": reads,
        "read_hours": round(reads / rate / 3600, 2) if rate else None,
        "passes": passes, "batch": batch,
        "listing_seconds_per_pass": (2 * scan_s) if scan_s is not None else None,
        "git_hours_assumed": round(len(wt_retiring) * per_job_git_s / 3600, 2),
        "total_hours": round((reads / rate if rate else 0) / 3600 + passes * 2 * (scan_s or 60) / 3600
                             + len(wt_retiring) * per_job_git_s / 3600, 2) if rate else None,
    }
    report["candidates"] = [rows[j] for j in retire]
    report["kept_detail"] = [r for r in rows.values() if r.get("kept") not in (None, "within budget")
                             and r.get("worktree_exists")]
    report["elapsed_s"] = round(time.monotonic() - started, 1)
    return report


def _into_admin(gitfile: Path, admin: str | None) -> bool:
    if not admin:
        return False
    try:
        text = gitfile.read_text(errors="surrogateescape").strip()
    except OSError:
        return False
    if not text.startswith("gitdir:"):
        return True
    target = text[len("gitdir:"):].strip()
    resolved = os.path.normpath(target if os.path.isabs(target) else str(gitfile.parent / target))
    return resolved == admin or resolved.startswith(admin.rstrip("/") + "/")


def _preflight(info: dict[str, Any], job: dict[str, Any], worktree: Path, root: Path, salvage_refs: list[str],
               cache: dict[str, tuple[dict[str, str], str | None, str | None]]) -> None:
    reg, why = rgit.registration(worktree)
    if reg is None:
        if why not in ("no-gitfile", "admin-missing"):
            info["issue"] = f"registration: {why}"
        info["git"] = why
        if salvage_refs and not info.get("issue"):
            # As `Retirement.begin`: the source repository anchors the salvage commits.
            common = None
            workdir = job.get("workdir")
            if workdir and os.path.isdir(workdir):
                try:
                    out = rgit.run(["rev-parse", "--git-common-dir"], cwd=Path(workdir), timeout=60).stdout
                    common = Path(os.path.realpath(Path(workdir) / out.decode("utf-8", "surrogateescape").strip()))
                except (rgit.GitError, OSError):
                    common = None
            if common is None or not all(isinstance(ref, str) and ref.startswith("refs/subfleet-salvage/")
                                         and rgit.resolve(common, ref) for ref in salvage_refs):
                info["issue"] = "salvage not archivable"
        return
    info["admin"] = str(reg.admin)
    lock = reg.admin / "locked"
    if lock.exists():
        try:
            text = lock.read_text()
        except OSError:
            text = "?"
        if not text.startswith(rgit.LOCK_MARKER):
            info["issue"] = "registration locked by someone else"
    key = str(reg.common)
    try:
        if key not in cache:
            remotes = rgit.network_remotes(reg.common)
            cache[key] = (remotes, rgit.scratch_reason(reg.common, root, remotes), rgit.object_format(reg.common))
        remotes, scratch, fmt = cache[key]
        info["scratch"] = scratch
        info["remotes"] = sorted(remotes)
        head = rgit.resolve(reg.admin, "HEAD")
        held = rgit.held_arguments(remotes)
        if head:
            out = rgit.run(["rev-list", "--count", head, *(["--not", *held] if held else [])],
                           git_dir=reg.common, timeout=120).stdout.decode().strip()
            info["unpushed"] = int(out or 0)
        for ref in salvage_refs:
            if not (isinstance(ref, str) and ref.startswith("refs/subfleet-salvage/") and rgit.resolve(reg.common, ref)):
                info["issue"] = "salvage not archivable"
                break
        if scratch is None and head:
            omit, _ = rgit.omission_map(reg.common, [head, job.get("workdir_head")], held, timeout=120)
            info["_omit"] = omit
    except (rgit.GitError, OSError, ValueError) as exc:
        info["issue"] = f"git: {str(exc)[:200]}"
