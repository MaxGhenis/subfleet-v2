"""`subfleet retention archives | restore | survey` (C-17.1, design d635).

All three run offline, without the daemon: `archives` lists what retention has
archived, `restore` puts an archive back (it touches only its destinations),
and `survey` is a read-only dry run of retention over the state root.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .client import state_root
from .contracts import Exit


def add_verbs(sub) -> None:
    parser = sub.add_parser("retention", help="archives of retired jobs: list, restore, survey")
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
            out(f"{a['archive']}  {a.get('created_at', '?')}  archived {a.get('archived_bytes', 0):,} B  "
                f"omitted {a.get('omitted_bytes', 0):,} B  regenerable {a.get('regenerable_bytes', 0):,} B  "
                f"added {a.get('added_bytes', 0):,} B  {a.get('worktree') or a.get('job_dir') or ''}")
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
    return fail(Exit.INVALID_INPUT, f"retention: unknown verb {command}")
