"""`subfleet retention archives | restore | survey | retire` (C-17.1, design d635, C-8.6).

The first three run offline, without the daemon: `archives` lists what retention
has archived, `restore` puts an archive back (it touches only its destinations),
and `survey` is a read-only dry run of retention over the state root. `retire`
asks the daemon, which alone writes the store (C-3.4), to retire finished jobs
ahead of their pool's budget, or to take that request back.
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
    retire = verbs.add_parser("retire", help="retire finished jobs ahead of the budget (asks the daemon)")
    retire.add_argument("ids", nargs="+", metavar="JOB")
    retire.add_argument("--withdraw", action="store_true", help="take the request back")
    retire.add_argument("--dry-run", action="store_true", help="say what a request would do; record nothing")
    retire.add_argument("--reason", help="recorded with the request")
    retire.add_argument("--json", action="store_true", default=argparse.SUPPRESS)


#: C-8.6: outcomes that leave nothing undone; any other names a job the request could not take.
SETTLED = {"requested", "already-requested", "withdrawn", "not-requested", "retired"}


def _retire_line(row: dict) -> str:
    outcome = row["outcome"]
    words = {"requested": "requested", "already-requested": f"already requested at {row.get('requested_at')}",
             "withdrawn": "request withdrawn", "not-requested": "no request stands",
             "not-finished": f"not finished ({row.get('state')}): nothing recorded",
             "retired": f"retired at {row.get('retired_at')}" + (f" (archive {row['archive']})"
                                                                 if row.get("archive") else ""),
             "unknown": "unknown job"}.get(outcome, outcome)
    held = [*(["retiring now: retention has it in flight"] if row.get("retiring") else []),
            *([f"kept while pinned: {row['kept']}"] if row.get("kept") else []),
            *([f"deferred {row['deferred']['for_s']} s more: {row['deferred']['reason']}"]
              if row.get("deferred") else []),
            *([f"pins not read ({row['pins_unread']}); a pass reads them"] if row.get("pins_unread") else [])]
    return f"{row['job_id']}  {words}" + ("; " + "; ".join(held) if held else "")


def _retire(args: argparse.Namespace) -> int:
    """C-8.6: the one verb that asks the daemon, since a request is a row it writes."""
    from . import protocol
    from .cli import RESTART_DAEMON, START_DAEMON, _daemon_error, emit, fail, note, out, session_id
    from .client import Client, DaemonError, DaemonUnavailable, OutcomeUnknown, state_root
    if len(set(args.ids)) > protocol.RETIRE_MAX_JOBS:
        return fail(Exit.INVALID_INPUT, f"retention retire: at most {protocol.RETIRE_MAX_JOBS} jobs a request")
    if args.reason is not None and len(args.reason) > protocol.RETIRE_REASON_MAX:
        return fail(Exit.INVALID_INPUT,
                    f"retention retire: --reason holds at most {protocol.RETIRE_REASON_MAX} characters")
    request = {"job_ids": list(dict.fromkeys(args.ids)), "withdraw": bool(args.withdraw),
               "dry_run": bool(args.dry_run), "reason": args.reason, "session_id": session_id()}
    try:
        # Safe to send again: a second request finds the first standing, a second
        # withdrawal finds none, and neither records anything more.
        result = Client(state_root()).call_settled("retire", request)
    except DaemonUnavailable as exc:
        return fail(Exit.DAEMON_UNAVAILABLE, f"retention retire: only the daemon records a request ({exc})",
                    getattr(exc, "fix", None) or START_DAEMON)
    except OutcomeUnknown as exc:
        # C-16.3: not refused and not failed; the daemon may have recorded it.
        if getattr(args, "json", False):
            emit({"jobs": None, "outcome": "unknown", "error": str(exc)})
        return fail(Exit.OPERATIONAL, f"retention retire: no answer says whether the request was recorded "
                                      f"({'; then '.join(exc.reasons)})",
                    "run the same command again, which is safe: it records nothing twice")
    except DaemonError as exc:
        if protocol.UNKNOWN_OP in str(exc):
            return fail(Exit.DAEMON_UNAVAILABLE, "retention retire: this daemon predates retire requests (C-8.6); "
                                                 "it is older than this CLI", RESTART_DAEMON)
        return _daemon_error(exc)
    rows = result.get("jobs") or []
    if getattr(args, "json", False):
        emit(result)
    else:
        for row in rows:
            out(_retire_line(row))
        if result.get("dry_run"):
            note("retention retire: dry run: nothing was recorded")
        elif result.get("recorded") and not result.get("withdraw"):
            note("retention retire: retention takes requested jobs on its next pass, which starts now; "
                 "re-run with --dry-run to follow them")
        if (result.get("retention") or {}).get("dormant"):
            note("retention retire: retention is dormant (SUBFLEET_RETENTION_DORMANT=1): no pass acts on a "
                 "request until the daemon runs without it")
    return int(Exit.OK) if all(row.get("outcome") in SETTLED for row in rows) else int(Exit.REFUSED)


def cmd_retention(args: argparse.Namespace) -> int:
    from . import retention_archive as rarch
    from .cli import emit, fail, out
    root = state_root()
    command = args.retention_command
    as_json = getattr(args, "json", False)
    if command == "retire":
        return _retire(args)
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
