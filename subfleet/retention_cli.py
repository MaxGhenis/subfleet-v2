"""`subfleet retention archives | restore | survey | delete` (C-17.1, C-8.7, design d635).

The first three run offline, without the daemon: `archives` lists what
retention has archived, `restore` puts an archive back (it touches only its
destinations), and `survey` is a read-only dry run of retention over the state
root. `delete` asks the daemon to remove the kept trees of an operator-approved
list of finished jobs (`retention_delete`, decision d2660): `--dry-run` says
what it would do, `--apply` does it, `--status` and `--stop` read and end it.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .client import state_root
from .contracts import Exit

#: How often a followed `delete` reads the daemon's record.
DELETE_POLL_S = 2.0


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
    delete = verbs.add_parser("delete", help="delete the kept trees of an approved list of finished jobs (C-8.7)")
    delete.add_argument("--request", type=Path, metavar="FILE",
                        help="the request: ruling, jobs, age_exempt, min_age_days, exclude_repositories, min_free_gb")
    action = delete.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", dest="delete_action", action="store_const", const="dry-run",
                        help="gate every job and say what would be deleted and kept; writes nothing")
    action.add_argument("--apply", dest="delete_action", action="store_const", const="apply",
                        help="delete, one job at a time; the daemon keeps going after a restart")
    action.add_argument("--status", dest="delete_action", action="store_const", const="status",
                        help="the request, what each job came to so far, and totals")
    action.add_argument("--stop", dest="delete_action", action="store_const", const="stop",
                        help="end the running request at the next job boundary")
    delete.add_argument("--limit", type=int, metavar="N", help="with --apply: end the run after N deletions")
    delete.add_argument("--detach", action="store_true",
                        help="start it and return at once instead of following it; read it with --status")
    delete.add_argument("--json", action="store_true", default=argparse.SUPPRESS)


def cmd_retention(args: argparse.Namespace) -> int:
    from . import retention_archive as rarch
    from .cli import emit, fail, out
    command = args.retention_command
    as_json = getattr(args, "json", False)
    if command == "delete":
        return _delete(args, as_json)
    root = state_root()
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


def _delete(args: argparse.Namespace, as_json: bool) -> int:
    """`retention delete` (C-8.7): check the request here, as the daemon will,
    send it, and by default follow it: one JSON line per job as it finishes,
    then one summary line."""
    from . import protocol
    from .cli import RESTART_DAEMON, _client, _daemon_down, _daemon_error, _protocol_failure, emit, fail, note, out
    from .client import DaemonError, DaemonUnavailable
    from .retention_delete import RequestError, read_request
    action = args.delete_action
    payload: dict = {"action": action}
    if action in ("dry-run", "apply"):
        if args.request is None:
            return fail(Exit.INVALID_INPUT, f"retention delete: --{action} needs --request FILE")
        if args.limit is not None and action != "apply":
            return fail(Exit.INVALID_INPUT, "retention delete: --limit goes with --apply")
        if args.limit is not None and args.limit < 1:
            return fail(Exit.INVALID_INPUT, "retention delete: --limit must be a positive integer")
        try:
            request = read_request(args.request)
        except RequestError as exc:
            return fail(Exit.INVALID_INPUT, f"retention delete: {exc}")
        payload.update(request=request.as_json(), limit=args.limit)
    elif args.request is not None or args.limit is not None or args.detach:
        return fail(Exit.INVALID_INPUT, f"retention delete: --{action} takes no --request, --limit or --detach")
    client = _client(args)
    try:
        result = client.call("retention.delete", payload)
        if action in ("status", "stop") or args.detach:
            if as_json:
                emit(result)
            else:
                out(json.dumps(result, indent=2, sort_keys=True))
            return int(Exit.OK)
        key = "dry_run" if action == "dry-run" else "apply"
        run = result.get("run")
        shown: set[str] = set()
        while True:
            record = client.call("retention.delete", {"action": "status"}).get(key) or {}
            for line in record.get("results") or ():
                if line.get("run") == run and line.get("job_id") not in shown:
                    shown.add(line.get("job_id"))
                    out(json.dumps(line, sort_keys=True))
            if record.get("status") not in ("running", "stopping"):
                break
            time.sleep(DELETE_POLL_S)
        out(json.dumps({"summary": record.get("totals"), "mode": record.get("mode"), "run": record.get("run"),
                        "status": record.get("status"), "error": record.get("error")}, sort_keys=True))
        return int(Exit.OK) if record.get("status") in ("done", "done-limit", "stopped") else int(Exit.OPERATIONAL)
    except KeyboardInterrupt:
        note("retention delete: the request keeps running in the daemon; read it with --status, end it with --stop")
        return int(Exit.CANCELLED)
    except DaemonUnavailable as exc:
        return _daemon_down(exc)
    except DaemonError as exc:
        if protocol.UNKNOWN_OP in str(exc):
            return fail(Exit.DAEMON_UNAVAILABLE, "the daemon has no retention delete path; it is older than this CLI",
                        RESTART_DAEMON)
        return _daemon_error(exc)
    except protocol.ProtocolError as exc:
        return _protocol_failure(exc)
