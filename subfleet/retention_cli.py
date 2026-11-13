"""`subfleet retention archives | restore | survey | offload | recall` (C-17.1, design d635, C-8.8).

All of them run offline, without the daemon: `archives` lists what retention
has archived, `restore` puts an archive back (it touches only its destinations
and the archive's lock file), `survey` is a read-only dry run of retention over
the state root, `offload` moves settled archives' stored files and bundles to
another volume, and `recall` brings them back (`retention_offload`).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .client import state_root
from .contracts import Exit


def add_verbs(sub) -> None:
    parser = sub.add_parser("retention", help="archives of retired jobs: list, restore, survey, offload, recall")
    verbs = parser.add_subparsers(dest="retention_command")
    parser.set_defaults(handler=cmd_retention, retention_command=None)
    listing = verbs.add_parser("archives", help="every archive retention has written")
    listing.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    restore = verbs.add_parser("restore", help="put a retired job's trees back")
    restore.add_argument("archive", help="the archive's name (normally the job id)")
    restore.add_argument("--to", type=Path, help="restore into DIR/worktree, DIR/job and DIR/admin instead")
    restore.add_argument("--repository", type=Path,
                         help="a clone of the project to take omitted files and prerequisites from")
    restore.add_argument("--check", action="store_true", help="only verify the archive")
    restore.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    survey = verbs.add_parser("survey", help="read-only dry run: what retention would retire and keep")
    survey.add_argument("--no-sizes", action="store_true", help="skip measuring trees")
    survey.add_argument("--no-holders", action="store_true", help="skip the process listing")
    survey.add_argument("--sample", type=int, metavar="N",
                        help="estimate from N sampled jobs what retirement frees versus archives (read-only)")
    survey.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    offload = verbs.add_parser("offload", help="move settled archives' stored files and bundles to another volume")
    offload.add_argument("--to", type=Path, required=True, metavar="DIR",
                         help="a folder on an encrypted local volume; archives go to DIR/subfleet-archive/<name>")
    offload.add_argument("--limit", type=int, metavar="N", help="archives per run (largest first)")
    offload.add_argument("--min-bytes", type=int, default=0, metavar="B", help="skip archives smaller than B bytes")
    offload.add_argument("--keep-internal", action="store_true",
                         help="copy and verify only: a backup that deletes nothing")
    offload.add_argument("--dry-run", action="store_true", help="write nothing; list what would move")
    offload.add_argument("--allow-unencrypted", action="store_true", help="accept a volume that is not encrypted")
    offload.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    recall = verbs.add_parser("recall", help="bring an offloaded archive's files back")
    recall.add_argument("archive", nargs="?", help="the archive's name")
    recall.add_argument("--all-on", type=Path, metavar="DIR", help="every archive offloaded to DIR")
    recall.add_argument("--drop-external", action="store_true", help="remove the external copy once it is back")
    recall.add_argument("--json", action="store_true", default=argparse.SUPPRESS)


def cmd_retention(args: argparse.Namespace) -> int:
    from . import retention_archive as rarch
    from .cli import emit, fail, out
    root = state_root()
    command = args.retention_command
    as_json = getattr(args, "json", False)
    if command in (None, "archives"):
        archives = rarch.list_archives(root)
        if as_json:
            emit({"archives": archives})
            return int(Exit.OK)
        total = sum(a.get("archived_bytes", 0) for a in archives if "error" not in a)
        freed = sum(rarch.accounting(a)["freed_bytes"] for a in archives if "error" not in a)
        added = sum(a.get("added_bytes", 0) for a in archives if "error" not in a)
        for a in archives:
            if "error" in a:
                out(f"{a['archive']}  {a['error']}")
                continue
            offload = a.get("offload")
            where = f"  {offload.get('describe') or offload.get('error')}" if offload else ""
            out(f"{a['archive']}  {a.get('created_at', '?')}  archived {a.get('archived_bytes', 0):,} B  "
                f"omitted {a.get('omitted_bytes', 0):,} B  regenerable {a.get('regenerable_bytes', 0):,} B  "
                f"added {a.get('added_bytes', 0):,} B  {a.get('worktree') or a.get('job_dir') or ''}{where}")
        out(f"{len(archives)} archives: {total:,} bytes kept in them; {freed:,} bytes deleted without a copy "
            f"(tracked files a remote holds, regenerable output); {added:,} bytes the archives added "
            "(bundles, manifests, rows, byte copies)")
        return int(Exit.OK)
    if command == "restore":
        try:
            if args.check:
                result = rarch.check_archive(root, args.archive)
                result.pop("manifest", None)
            else:
                result = rarch.restore(root, args.archive, to=args.to, repository=args.repository)
        except (rarch.RestoreError, OSError, ValueError) as exc:
            return fail(Exit.OPERATIONAL, f"retention restore: {exc}")
        if as_json:
            emit(result)
        else:
            out(json.dumps(result, indent=2, default=str))
        return int(Exit.OK) if result.get("ok", True) else int(Exit.OPERATIONAL)
    if command == "survey" and getattr(args, "sample", None):
        from .retention_survey import sample
        result = sample(root, args.sample)
        if as_json:
            emit(result)
        else:
            out(json.dumps({k: v for k, v in result.items() if k != "jobs"}, indent=2, default=str))
        return int(Exit.OK)
    if command == "survey":
        from .retention_survey import survey
        result = survey(root, sizes=not args.no_sizes, holders=not args.no_holders)
        if as_json:
            emit(result)
        else:
            brief = {k: v for k, v in result.items() if k not in ("candidates", "kept_detail")}
            out(json.dumps(brief, indent=2, default=str))
        return int(Exit.OK)
    if command == "offload":
        return _offload(root, args, as_json)
    if command == "recall":
        return _recall(root, args, as_json)
    return fail(Exit.INVALID_INPUT, f"retention: unknown verb {command}")


def _offload(root: Path, args: argparse.Namespace, as_json: bool) -> int:
    from . import retention_offload as rof
    from .cli import emit, fail, out
    if args.limit is not None and args.limit < 1:
        return fail(Exit.INVALID_INPUT, "retention offload: --limit must be at least 1")
    try:
        report = rof.offload(root, args.to, limit=args.limit, min_bytes=args.min_bytes,
                             keep_internal=args.keep_internal, dry_run=args.dry_run,
                             allow_unencrypted=args.allow_unencrypted)
    except OSError as exc:
        return fail(Exit.OPERATIONAL, f"retention offload: {exc}")
    failed = any(r.get("outcome") == "failed" for r in report["results"])
    code = Exit.REFUSED if report["refused"] else Exit.OPERATIONAL if failed else Exit.OK
    if as_json:
        emit(report)
        return int(code)
    t = report["target"]
    if "uuid" in t:
        encrypted = {True: "encrypted", False: "not encrypted", None: "encryption unknown"}[t.get("encrypted")]
        out(f"target {t['dir']} on volume {t['name']} ({t['uuid']}), {t['fstype']}, {encrypted}, "
            f"{t['free']:,} B free")
    for reason in report["refused"]:
        out(f"refused: {reason}")
    for skip in report["skipped"]:
        out(f"skip {skip['archive']}: {skip['reason']}")
    verb = "would offload" if report["dry_run"] else "batch"
    for item in report["batch"]:
        resume = f" (resuming: {item['resume']})" if item.get("resume") else ""
        out(f"{verb} {item['archive']}: {item['files']} files, {item['bytes']:,} B{resume}")
    for r in report["results"]:
        detail = r.get("error") or r.get("reason") or ""
        out(f"{r['archive']}: {r['outcome']}{': ' + detail if detail else ''}")
    if report["not_examined"]:
        out(f"{report['not_examined']} more archives not examined (--limit)")
    out(f"{len(report['batch'])} archives, {report['batch_bytes']:,} B"
        + (" (dry run: nothing written)" if report["dry_run"] else ""))
    return int(code)


def _recall(root: Path, args: argparse.Namespace, as_json: bool) -> int:
    from . import retention_offload as rof
    from .cli import emit, fail, out
    if (args.archive is None) == (args.all_on is None):
        return fail(Exit.INVALID_INPUT, "retention recall: name one archive, or --all-on DIR")
    try:
        report = rof.recall(root, args.archive, all_on=args.all_on, drop_external=args.drop_external)
    except (rof.Failed, OSError, ValueError) as exc:
        return fail(Exit.OPERATIONAL, f"retention recall: {exc}")
    failed = any(r.get("outcome") == "failed" for r in report["results"])
    if as_json:
        emit(report)
    else:
        for skip in report["skipped"]:
            out(f"skip {skip['archive']}: {skip['reason']}")
        for r in report["results"]:
            detail = r.get("error") or r.get("reason") or r.get("external") or ""
            out(f"{r['archive']}: {r['outcome']}{': ' + detail if detail else ''}")
    return int(Exit.OPERATIONAL if failed else Exit.OK)
