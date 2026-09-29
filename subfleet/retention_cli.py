"""`subfleet retention archives|restore`: the job records retention archived (C-8.4, C-17.1).

Both run offline against the state root; neither needs the daemon, and neither
touches anything but the destination `restore` is given. Worktrees are not
here: the worktree archiver that retired them keeps its own restore recipes.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import retention_archive as archive
from .contracts import Exit


def _archives(root: Path) -> list[tuple[str, Path]]:
    folder = root / "archive"
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        return []
    return [(name, folder / name) for name in names if not name.startswith(archive.PARTIAL_PREFIX)
            and (folder / name / archive.MANIFEST).is_file()]


def _describe(name: str, path: Path) -> dict:
    manifest = archive.load(path)
    try:
        rows = json.loads((path / archive.ROWS).read_bytes())["rows"]
    except (OSError, ValueError, KeyError):
        rows = {}
    stored = sum(entry.stat().st_size for entry in os.scandir(path) if entry.is_file(follow_symlinks=False))
    tree = manifest.get("tree") or {}
    job = (rows.get("jobs") or [{}])[0]
    return {"job_id": name, "archive": str(path), "original": manifest.get("original"),
            "bytes": tree.get("bytes", 0), "archive_bytes": stored, "state": job.get("state"),
            "finished_at": job.get("finished_at"), "worktree": job.get("worktree"),
            "salvage": [row.get("path") for row in rows.get("artifacts", []) if row.get("role") == "salvage"]}


def cmd_archives(args: argparse.Namespace) -> int:
    from . import cli
    rows = []
    for name, path in _archives(cli._root(args)):
        try:
            rows.append(_describe(name, path))
        except (OSError, ValueError) as exc:
            rows.append({"job_id": name, "archive": str(path), "error": str(exc)})
    if args.json:
        cli.emit({"archives": rows})
        return int(Exit.OK)
    if not rows:
        cli.note("No archived jobs.")
        return int(Exit.OK)
    for row in rows:
        if "error" in row:
            cli.out(f"{row['job_id']}  unreadable: {row['error']}")
            continue
        salvage = f"  salvage {', '.join(row['salvage'])}" if row["salvage"] else ""
        cli.out(f"{row['job_id']}  {row['state'] or '?'}  {row['bytes']} bytes -> {row['archive_bytes']} archived"
                f"{salvage}")
    return int(Exit.OK)


def cmd_restore(args: argparse.Namespace) -> int:
    from . import cli
    root = cli._root(args)
    if Path(args.job).name != args.job or args.job in (".", "..") or args.job.startswith(archive.PARTIAL_PREFIX):
        return cli.fail(Exit.INVALID_INPUT, f"retention restore: {args.job!r} is not a job id")
    path = root / "archive" / args.job
    if not (path / archive.MANIFEST).is_file():
        return cli.fail(Exit.INVALID_INPUT, f"retention restore: no archive for {args.job}",
                        "list them with `subfleet retention archives`")
    try:
        manifest = archive.verify(path)
    except archive.ArchiveCorrupt as exc:
        return cli.fail(Exit.OPERATIONAL, f"retention restore: the archive does not verify: {exc}")
    if args.check:
        cli.out(f"{args.job}: archive verified")
        return int(Exit.OK)
    destination = Path(args.to).expanduser().resolve() / args.job if args.to else root / "jobs" / args.job
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        skipped = archive.restore(path, destination)
    except FileExistsError:
        return cli.fail(Exit.INVALID_INPUT, f"retention restore: {destination} already exists",
                        "choose another place with --to DIR")
    except (OSError, archive.ArchiveCorrupt) as exc:
        return cli.fail(Exit.OPERATIONAL, f"retention restore: {exc}")
    cli.out(str(destination) if manifest.get("tree") is not None else f"{args.job}: the job had no directory")
    if skipped:
        cli.note(f"not recreated (sockets or devices, which hold no data): {', '.join(skipped[:10])}")
    cli.note("The job's rows are in rows.json in the archive; they are not re-inserted.")
    return int(Exit.OK)


def cmd_preview(args: argparse.Namespace) -> int:
    """What the next passes would retire, read from a read-only store; nothing is written."""
    from . import cli, retention
    from .policy import DEFAULT_POLICY_PATH, RETENTION_DEFAULTS, PolicyError, load_policy
    from .store import Store
    root = cli._root(args)
    path = Path(args.policy) if args.policy else root / "policy.json"
    try:
        policy = load_policy(path if path.exists() else DEFAULT_POLICY_PATH)
    except PolicyError as exc:
        return cli.fail(Exit.INVALID_INPUT, f"retention preview: {exc}")
    budget = {**RETENTION_DEFAULTS, **(policy.get("retention") or {})}
    database = root / "state.sqlite3"
    if not database.exists():
        return cli.fail(Exit.OPERATIONAL, f"retention preview: no store at {database}")
    with Store(database, read_only=True) as store:
        result = retention.maintenance(
            store, root, max_jobs=int(budget["jobs"]), max_bytes=int(budget["bytes"]),
            turn_max_jobs=int(budget["turn_jobs"]), turn_max_bytes=int(budget["turn_bytes"]),
            turn_keep_s=float(budget["turn_keep_days"]) * 86400, min_age_s=retention.MIN_AGE_S, dry_run=True)
    result["note"] = "the conversation service's pins are not consulted offline"
    if args.json:
        cli.emit({key: result.get(key) for key in ("would_retire", "pools", "kept", "errors", "note")})
        return int(Exit.OK)
    for name, pool in result["pools"].items():
        cli.out(f"{name}: {pool['jobs_before']} jobs, {pool['bytes_before']} bytes measured"
                f" ({pool['unmeasured']} unmeasured); budget {pool['max_jobs']} jobs, {pool['max_bytes']} bytes")
    for row in result.get("would_retire", []):
        cli.out(f"would retire {row['job_id']} ({row['pool']}, {row['bytes']} bytes)")
    cli.out(f"kept while their worktree exists: {len(result.get('kept') or {})} jobs")
    cli.note(result["note"])
    return int(Exit.OK)


def add_verbs(sub) -> None:
    parser = sub.add_parser("retention", help="job records that retention archived")
    verbs = parser.add_subparsers(dest="retention_command", required=True)
    listing = verbs.add_parser("archives", help="list archived jobs")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(handler=cmd_archives)
    restore = verbs.add_parser("restore", help="recreate an archived job's directory")
    restore.add_argument("job")
    restore.add_argument("--to", metavar="DIR", help="restore into DIR/<job> instead of the state root's jobs/")
    restore.add_argument("--check", action="store_true", help="only verify the archive")
    restore.set_defaults(handler=cmd_restore, json=False)
    preview = verbs.add_parser("preview", help="what retention would retire, from a read-only store")
    preview.add_argument("--json", action="store_true")
    preview.add_argument("--policy", metavar="FILE", help="policy to read (default: the state root's)")
    preview.set_defaults(handler=cmd_preview)
