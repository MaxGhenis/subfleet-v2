"""A read-only dry run of retention by archive over a live state root (d635).

It applies the same eligibility as a retention pass (pins and their reasons,
pool budgets, oldest first) and the same per-job preflight (registration,
scratch sources, salvage refs, nested linked worktrees, holders) without writing
anything: SQLite is opened read-only, git runs with ``GIT_OPTIONAL_LOCKS=0`` and
only reading commands, and ``lsof`` lists processes. The conversation service's
in-memory pins are not visible from outside the daemon; the report says so.

Byte figures are apparent sizes (the sum of ``st_size``). What a retirement
frees is the omitted tracked files and the regenerable output; archived files
are APFS clones, which share their blocks with the files they replace.
`sample` estimates both from a few jobs, with the blocks no clone shares.
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


#: The manifest's bytes besides its entries (the git section, totals, salvage,
#: the restore line) and a summary's: estimates, from archives made in tests
#: (1 to 3 KiB of git section with remotes and held refs).
MANIFEST_FIXED = 4096
SUMMARY_BYTES = 1024
_HASH = "0" * 64


def _entry_bytes(rel: str, st: os.stat_result, parent: int, name: str | None, how: str,
                 fmt: str | None = None) -> int:
    """What the manifest spends on this entry, as `_Builder` writes it (`how`:
    "store", "omit", "regen" or "dir"); a hash not read here stands in with
    one of the same length."""
    entry: dict[str, Any] = {"p": rel, "sig": rfs.signature(st)}
    t = entry["sig"]["t"]
    if how == "regen":
        entry["regen"] = True
    if t == "l" and name is not None:
        try:
            entry["link"] = os.fsdecode(os.readlink(name, dir_fd=parent))
        except OSError:
            entry["link"] = ""
    elif t == "f" and rel:
        entry["size"] = st.st_size
        if how == "omit":
            entry.update(blob="0" * (64 if fmt == "sha256" else 40), sha256=_HASH)
        elif how == "store":
            entry.update(store=rfs.sig_key(st), sha256=_HASH)
    return rarch.entry_bytes(entry)


def _walk_sizes(path: Path, omit: dict[str, dict[str, int]] | None) -> dict[str, Any]:
    """Bytes, file count, the tracked bytes omission could leave out (an upper
    bound: same path and size as a held blob), nested gitfiles, and the bytes
    the manifest would spend on the tree's entries (every file as stored)."""
    out = {"bytes": 0, "files": 0, "omittable_upper": 0, "nested_gitfiles": [], "error": None,
           "manifest_bytes": 0}
    try:
        fd = rfs.open_dir(path)
    except FileNotFoundError:
        return out
    except OSError as exc:
        out["error"] = str(exc)
        return out
    try:
        for rel, st, parent, name in rfs.walk(fd):
            out["manifest_bytes"] += _entry_bytes(rel, st, parent, name, "store")
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


def _budget(root: Path) -> dict[str, Any]:
    """The `retention` policy section the daemon would use (defaults filled in)."""
    policy: dict[str, Any] = {}
    try:
        policy = json.loads((root / "policy.json").read_text()).get("retention") or {}
    except (OSError, ValueError):
        pass
    return {**RETENTION_DEFAULTS, **policy}


def survey(root: Path, *, sizes: bool = True, holders: bool = True, sample_throughput: bool = True,
           batch: int = ret.BATCH, budgets: dict[str, tuple[int, int]] | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    started = time.monotonic()
    budget = _budget(root)
    remote_less = int(budget["remote_less_history_bytes"])
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
        reasons = ret._pin_reasons(store, set(), None, pins=None, turn_keep_s=turn_keep_s,
                                   hosted=ret.nested_hosts(jobs, root), root=root)
        journals = rarch.journals(root)
        live_trees = [job.get("worktree") for job in jobs] + [
            j.get("worktree") for j in journals.values() if isinstance(j, dict)]
        salvage: dict[str, dict[str, str]] = defaultdict(dict)       # job -> salvage ref -> recorded digest
        for row in store.query("SELECT r.artifact_id,r.path,r.sha256,a.job_id FROM artifacts r "
                               "JOIN attempts a USING(attempt_id) WHERE r.role='salvage' ORDER BY r.artifact_id"):
            salvage[row["job_id"]][row["path"]] = row["sha256"]
    finally:
        store.close()
    rows: dict[str, dict[str, Any]] = {}
    remotes_cache: dict[Any, Any] = {}
    repositories: list[list[Path]] = []

    def known() -> list[Path]:
        # Read once, and only when a job whose tree and workdir are gone needs it.
        if not repositories:
            repositories.append(rarch.known_repositories(jobs, root))
        return repositories[0]

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
            info["manifest_bytes"] = jobdir["manifest_bytes"]
        if worktree is not None and not info.get("pin"):
            seen = journals[job_id].get("history_bytes") if isinstance(journals.get(job_id), dict) else None
            _preflight(info, job, worktree, root, salvage.get(job_id, {}), remotes_cache, remote_less, seen,
                       lambda h: any(w and os.path.realpath(w) == str(h) for w in live_trees), known)
        if worktree is not None and info["worktree_exists"] and sizes:
            wt = _walk_sizes(worktree, info.pop("_omit", None))
            info.update(worktree_bytes=wt["bytes"], worktree_files=wt["files"],
                        omittable_upper=wt["omittable_upper"] if not info.get("scratch") else 0)
            info["bytes"] = info.get("bytes", 0) + wt["bytes"]
            info["manifest_bytes"] = info.get("manifest_bytes", 0) + wt["manifest_bytes"]
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
    if retire:
        # What the archives would add (N1): bundles, manifests (every file as
        # stored, an upper bound), rows and summaries.
        store = Store(root / "state.sqlite3", read_only=True)
        try:
            for r in retiring:
                r["added_estimate"] = (int(r.get("bundle_estimate") or 0) + MANIFEST_FIXED + SUMMARY_BYTES
                                       + r.get("manifest_bytes", 0) + rarch.rows_bytes(store, r["job_id"]))
        finally:
            store.close()
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
        "bundle_bytes_estimate": sum(int(r.get("bundle_estimate") or 0) for r in retiring),
        "added_bytes_estimate": sum(r.get("added_estimate", 0) for r in retiring),
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


def _source_common(job: dict[str, Any]) -> Path | None:
    """As `Retirement.begin` without a registration: the job's source
    repository, from its workdir."""
    workdir = job.get("workdir")
    if not workdir or not os.path.isdir(workdir):
        return None
    try:
        out = rgit.run(["rev-parse", "--git-common-dir"], cwd=Path(workdir), timeout=60).stdout
    except (rgit.GitError, OSError):
        return None
    return Path(os.path.realpath(Path(workdir) / out.decode("utf-8", "surrogateescape").strip()))


def _preflight(info: dict[str, Any], job: dict[str, Any], worktree: Path, root: Path, salvage_refs: dict[str, str],
               cache: dict[Any, Any], remote_less: int | None = None, seen: int | None = None,
               live: Any = None, known: Any = None) -> None:
    """The per-job checks of `Retirement.begin`, read-only, for a tree that is
    there or gone, and what the retirement's bundle would carry
    (`bundle_estimate`, N1): a job whose baseline no network remote holds, and
    whose bundle would carry more than `remote_less` bytes (or `seen`, the
    size an earlier attempt's bundle had), is kept (`remote-less-history`). `live(tree)` says whether a job whose tree that
    is still has rows or a journal (`rarch.host_absent`). `known()` lists the
    repositories the store's jobs are in (`rarch.known_repositories`), for a
    job whose tree and workdir are both gone. `salvage_refs` maps each salvage
    ref of the job to the digest its row recorded."""
    live = live or (lambda host: True)
    known = known or (lambda: [])
    head = None
    lost = None
    found = None
    inferred = False
    if os.path.lexists(worktree):
        reg, why = rgit.registration(worktree)
        where = rgit.gitfile_admin(worktree)[0] if why == "admin-missing" else None
    else:
        away = rarch.tree_away(worktree)
        if away is not None:
            # As `Retirement.begin`: another tool holds the tree aside.
            info["issue"] = f"tree away: {worktree} is at {away}"
            return
        workdir = job.get("workdir")

        def listing(common: Path) -> dict[str, str]:
            if ("salvage", str(common)) not in cache:
                try:
                    cache[("salvage", str(common))] = rgit.refs_under(common, "refs/subfleet-salvage/")
                except (rgit.GitError, OSError):
                    cache[("salvage", str(common))] = {}
            return cache[("salvage", str(common))]

        reg, found, lost = (rarch.source_of_gone_tree(job, worktree, known, listing,
                                                      {r: d for r, d in salvage_refs.items() if isinstance(r, str)})
                            if workdir else (None, None, None))
        inferred = bool(workdir) and not os.path.isdir(workdir)
        why = "tree-gone"
        where = Path(os.path.realpath(workdir)) if reg is None and workdir and not os.path.isdir(workdir) else None
        if lost and lost.startswith("tree away:"):
            info["issue"] = lost
            return
    if reg is None:
        if why not in ("no-gitfile", "admin-missing", "admin-remnant", "tree-gone"):
            info["issue"] = f"registration: {why}"
        host = rarch.host_absent(root, where, worktree, live)
        if host is not None:
            # As `Retirement._not_without_host` (N4).
            info["issue"] = f"nested-host: {host[1]}"
            return
        info["git"] = why
        common = found if found is not None else _source_common(job)
        if common is None:
            if salvage_refs and not info.get("issue"):
                info["issue"] = f"salvage not archivable: {lost}" if lost else "salvage not archivable"
            elif lost and not info.get("issue"):
                info["issue"] = f"repository not found: {lost}"
            return
    else:
        info["admin"] = str(reg.admin)
        lock = reg.admin / "locked"
        if lock.exists():
            try:
                text = lock.read_text()
            except OSError:
                text = "?"
            if not text.startswith(rgit.LOCK_MARKER):
                info["issue"] = "registration locked by someone else"
        common = reg.common
    key = str(common)
    try:
        if key not in cache:
            remotes = rgit.network_remotes(common)
            cache[key] = (remotes, rgit.scratch_reason(common, root, remotes), rgit.object_format(common))
        remotes, scratch, fmt = cache[key]
        info["scratch"] = scratch
        info["remotes"] = sorted(remotes)
        held = rgit.held_arguments(remotes)
        if reg is not None:
            head = rgit.resolve(reg.admin, "HEAD")
        if head:
            out = rgit.run(["rev-list", "--count", head, *(["--not", *held] if held else [])],
                           git_dir=common, timeout=120).stdout.decode().strip()
            info["unpushed"] = int(out or 0)
        commits = []
        for ref in salvage_refs:
            commit = rgit.resolve(common, ref) if isinstance(ref, str) and ref.startswith("refs/subfleet-salvage/") \
                else None
            if commit is None or (inferred and rarch.salvage_digest(commit) != salvage_refs[ref]):
                info.setdefault("issue", "salvage not archivable")
                break
            commits.append(commit)
        # What the anchor's bundle carries: every commit HEAD, the baseline and
        # the salvage commits reach that no network remote holds (the whole
        # history when there is none), as `rgit.history_bytes` measures it.
        heads = tuple(sorted({h for h in (head, job.get("workdir_head"), *commits) if h}))
        hkey = ("history", key, heads, tuple(held))
        if hkey not in cache:
            try:
                cache[hkey] = rgit.history_bytes(common, heads, held, timeout=600)
            except rgit.GitError as exc:
                cache[hkey] = None
                info["bundle_estimate_error"] = str(exc)[:200]
        info["bundle_estimate"] = cache[hkey]
        if remote_less is not None and not rgit.baseline_held(common, job.get("workdir_head"), held):
            size = max(cache[hkey], seen or 0) if cache[hkey] is not None else None
            if size is None:
                info.setdefault("issue", "remote-less-history: size unknown")
            elif size > remote_less:
                info.setdefault("issue", f"remote-less-history: {size} bytes of history no network remote "
                                         f"holds, over {remote_less}")
        if reg is not None and scratch is None and head:
            omit, _ = rgit.omission_map(common, [head, job.get("workdir_head")], held, timeout=120)
            info["_omit"] = omit
    except (rgit.GitError, OSError, ValueError) as exc:
        info["issue"] = f"git: {str(exc)[:200]}"


# --- a sampled estimate of what retirement frees (d635 disk relief) -----------------------

def _sample_walk(path: Path, omit: dict[str, dict[str, int]] | None, ignored: rgit.Ignored | None,
                 fmt: str | None, hash_budget: int) -> dict[str, Any]:
    """One tree, classified as a retirement would: regenerable output (the
    same `rfs.RegenerableWalk`, a root holding a repository archived), tracked
    files a network remote holds (hashed like the archive does, within
    `hash_budget` bytes; past it, same path and size count, and
    `omission_exact` is False), and everything else archived. `*_disk` figures
    are what deleting gives back (`rfs.private_bytes`)."""
    denied: set[str] = set()
    while True:
        try:
            return _sample_walk_once(path, omit, ignored, fmt, hash_budget, denied)
        except rfs.NotRegenerable as exc:
            denied.add(exc.root)


def _sample_walk_once(path: Path, omit: dict[str, dict[str, int]] | None, ignored: rgit.Ignored | None,
                      fmt: str | None, hash_budget: int, denied: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"bytes": 0, "files": 0, "archived_bytes": 0, "omitted_bytes": 0, "omitted_disk": 0,
                           "regenerable_bytes": 0, "regenerable_disk": 0, "regenerable": [], "hashed_bytes": 0,
                           "omission_exact": True, "regenerable_exact": True, "error": None, "manifest_bytes": 0}
    try:
        fd = rfs.open_dir(path)
    except FileNotFoundError:
        return out
    except OSError as exc:
        out["error"] = str(exc)
        return out
    regen = rfs.RegenerableWalk(fd, ignored.clear, denied) if ignored is not None else None
    try:
        for rel, st, parent, name in rfs.walk(fd):
            verdict = regen.classify(rel, st, parent, name) if regen is not None else False
            if isinstance(verdict, rfs.Verify):
                # As the archive: a file a RECORD lists goes only if it hashes
                # as the RECORD says; past the hash budget its size is taken
                # for a match (an upper bound: `regenerable_exact` False).
                expected, verdict = verdict.sha256, False
                if out["hashed_bytes"] + st.st_size <= hash_budget:
                    try:
                        handle = os.open(name, rfs.O_FILE, dir_fd=parent)
                        try:
                            digest, _ = rfs.read_hashes(handle, st.st_size, None)
                        finally:
                            os.close(handle)
                        out["hashed_bytes"] += st.st_size
                        verdict = digest == expected
                    except (OSError, rfs.TreeError):
                        verdict = False
                else:
                    out["regenerable_exact"], verdict = False, True
                if verdict:
                    assert regen is not None and name is not None
                    regen.confirm(rel, st, parent, name)
                    out["manifest_bytes"] += len(',"sha256":""') + 64
            if verdict:
                out["manifest_bytes"] += _entry_bytes(rel, st, parent, name, "regen")
                if stat.S_ISREG(st.st_mode):
                    out["bytes"] += st.st_size
                    out["files"] += 1
                continue
            if not rel or stat.S_ISDIR(st.st_mode):
                out["manifest_bytes"] += _entry_bytes(rel, st, parent, name, "dir")
                continue
            out["bytes"] += st.st_size
            out["files"] += 1
            if not stat.S_ISREG(st.st_mode):
                out["manifest_bytes"] += _entry_bytes(rel, st, parent, name, "store")
                continue
            size_ok = bool(omit and rel in omit and st.st_size in omit[rel].values()
                           and st.st_nlink == 1 and fmt is not None)
            if size_ok and out["hashed_bytes"] + st.st_size <= hash_budget:
                try:
                    handle = os.open(name, rfs.O_FILE, dir_fd=parent)
                    try:
                        _, blob = rfs.read_hashes(handle, st.st_size, fmt)
                    finally:
                        os.close(handle)
                    out["hashed_bytes"] += st.st_size
                    size_ok = blob in omit[rel]
                except (OSError, rfs.TreeError):
                    size_ok = False
            elif size_ok:
                out["omission_exact"] = False           # past the budget: an upper bound
            if size_ok:
                out["omitted_bytes"] += st.st_size
                out["omitted_disk"] += rfs.private_bytes(parent, name, st)
            else:
                out["archived_bytes"] += st.st_size
            out["manifest_bytes"] += _entry_bytes(rel, st, parent, name, "omit" if size_ok else "store", fmt)
    except rfs.TreeError as exc:
        out["error"] = str(exc)
    finally:
        os.close(fd)
    records = regen.summary() if regen is not None else []
    out["manifest_bytes"] += sum(rarch.entry_bytes(r) for r in records)
    out["regenerable_bytes"] = sum(r["bytes"] for r in records)
    out["regenerable_disk"] = sum(r["freed_disk_bytes"] for r in records)
    out["regenerable"] = [{k: r[k] for k in ("p", "kind", "bytes", "freed_disk_bytes", "kept")} for r in records]
    return out


def sample(root: Path, n: int = 20, *, hash_budget: int = 512 << 20,
           progress: Any = None) -> dict[str, Any]:
    """Read-only: classify `n` finished, unpinned jobs with a worktree, spread
    evenly from oldest to newest, as a retirement would, and extrapolate what
    retiring every such job frees versus what it moves into the archive. An
    estimate: the sample, not the whole tree, is walked (the full survey took
    hours on the live tree). `progress(job_report)` is called after each job."""
    root = Path(root).resolve()
    started = time.monotonic()
    remote_less = int(_budget(root)["remote_less_history_bytes"])
    store = Store(root / "state.sqlite3", read_only=True)
    try:
        jobs = list(reversed(store.list_jobs()))
        reasons = ret._pin_reasons(store, set(), None, pins=None, turn_keep_s=0, hosted=ret.nested_hosts(jobs, root),
                                   root=root)
        salvage: dict[str, dict[str, str]] = defaultdict(dict)
        for row in store.query("SELECT r.path,r.sha256,a.job_id FROM artifacts r JOIN attempts a USING(attempt_id) "
                               "WHERE r.role='salvage' ORDER BY r.artifact_id"):
            salvage[row["job_id"]][row["path"]] = row["sha256"]
    finally:
        store.close()
    candidates = []
    for job in jobs:
        if reasons.get(job["job_id"]):
            continue
        try:
            worktree = ret._owned_worktree(job, root)
        except ValueError:
            continue
        if worktree is not None and os.path.isdir(worktree):
            candidates.append((job, worktree))
    picked = [candidates[round(i * (len(candidates) - 1) / max(1, n - 1))] for i in range(min(n, len(candidates)))]
    picked = list({job["job_id"]: (job, wt) for job, wt in picked}.values())
    store = Store(root / "state.sqlite3", read_only=True)
    try:
        rows = {job["job_id"]: rarch.rows_bytes(store, job["job_id"]) for job, _ in picked}
    finally:
        store.close()
    cache: dict[Any, Any] = {}
    results: list[dict[str, Any]] = []
    for job, worktree in picked:
        t0 = time.monotonic()
        info: dict[str, Any] = {"job_id": job["job_id"], "created_at": job["created_at"], "worktree": str(worktree)}
        _preflight(info, job, worktree, root, salvage.get(job["job_id"], {}), cache, remote_less)
        omit = info.pop("_omit", None)
        ignored = fmt = None
        reg, _ = rgit.registration(worktree)
        if reg is not None:
            try:
                fmt = rgit.object_format(reg.common)
                ignored = rgit.Ignored.of(reg.admin, worktree, timeout=300)
            except (rgit.GitError, OSError) as exc:
                info["regenerable_off"] = str(exc)[:200]
        wt = _sample_walk(worktree, None if info.get("scratch") else omit, ignored, fmt, hash_budget)
        jd = _sample_walk(root / "jobs" / job["job_id"], None, None, None, 0)
        admin = _sample_walk(Path(info["admin"]), None, None, None, 0) if info.get("admin") else None
        info.update(worktree_walk=wt, job_dir_bytes=jd["bytes"], seconds=round(time.monotonic() - t0, 1))
        info["freed_bytes"] = wt["omitted_bytes"] + wt["regenerable_bytes"]
        info["freed_disk_bytes"] = wt["omitted_disk"] + wt["regenerable_disk"]
        info["archived_bytes"] = wt["archived_bytes"] + jd["bytes"]
        # What the archive adds (N1): the bundle (git's measure of the history
        # it carries), the manifest (its entries as the builder writes them,
        # plus a fixed part), the rows and the summary.
        info["manifest_bytes"] = (MANIFEST_FIXED + wt["manifest_bytes"] + jd["manifest_bytes"]
                                  + (admin["manifest_bytes"] if admin else 0))
        info["rows_bytes"] = rows[job["job_id"]]
        info["added_bytes"] = (int(info.get("bundle_estimate") or 0) + info["manifest_bytes"] + info["rows_bytes"]
                               + SUMMARY_BYTES)
        info["net_disk_bytes"] = info["freed_disk_bytes"] - info["added_bytes"]
        results.append(info)
        if progress is not None:
            progress(info)
    retirable = [r for r in results if not r.get("issue")]
    scale = len(candidates) * (len(retirable) / len(results)) if results else 0
    total = {k: sum(r[k] for r in retirable) for k in ("freed_bytes", "freed_disk_bytes", "archived_bytes",
                                                        "added_bytes", "net_disk_bytes", "manifest_bytes")}
    total["bundle_bytes"] = sum(int(r.get("bundle_estimate") or 0) for r in retirable)
    total.update(bytes=sum(r["worktree_walk"]["bytes"] + r["job_dir_bytes"] for r in retirable),
                 omitted_bytes=sum(r["worktree_walk"]["omitted_bytes"] for r in retirable),
                 regenerable_bytes=sum(r["worktree_walk"]["regenerable_bytes"] for r in retirable),
                 regenerable_disk=sum(r["worktree_walk"]["regenerable_disk"] for r in retirable),
                 omitted_disk=sum(r["worktree_walk"]["omitted_disk"] for r in retirable))
    kinds: dict[str, int] = defaultdict(int)
    kept_names: dict[str, int] = defaultdict(int)
    for r in retirable:
        for x in r["worktree_walk"]["regenerable"]:
            kinds[x["kind"]] += x["bytes"]
            for name in x["kept"]:
                kept_names[f"{x['kind']}: {name}"] += 1
    per_job = {k: (v / len(retirable) if retirable else 0) for k, v in total.items()}
    return {"state_root": str(root), "read_only": True, "estimate": True,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "candidates_with_worktree": len(candidates), "sampled": len(results), "retirable_in_sample": len(retirable),
            "kept_in_sample": {r["job_id"]: r["issue"] for r in results if r.get("issue")},
            "sample_totals": total, "regenerable_bytes_by_kind": dict(kinds),
            "kept_inside_regenerable": dict(sorted(kept_names.items(), key=lambda kv: -kv[1])[:40]),
            "extrapolated": {k: round(v * scale) for k, v in per_job.items()},
            "notes": ["an estimate from a sample, not a walk of every tree",
                      "bytes are apparent sizes; *_disk figures are blocks no clone or other link shares, "
                      "measured now (getattrlist ATTR_CMNEXT_PRIVATESIZE)",
                      "archived bytes are APFS clones: they stay on disk until the archive is removed",
                      "added_bytes is what the archives add: the bundle (git's disk usage of the history it "
                      "carries), the manifest (its entries as written, plus about 4 KiB), rows.json and "
                      "summary.json; net_disk_bytes = freed_disk_bytes - added_bytes",
                      "APFS snapshots (Time Machine) keep deleted blocks until they expire"],
            "jobs": results, "elapsed_s": round(time.monotonic() - started, 1)}
