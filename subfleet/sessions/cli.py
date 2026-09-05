"""`subfleet-sessions` — the sessions kit's entry point and verb handlers.

Reached three ways, all of them the same code:

  subfleet sessions <verb>    the permanent verb (C-17.1)
  subfleet-sessions <verb>    the console script the verb dispatches to
  subfleet tickle|muster|revive|mirror|handoff    v1's spellings, via `compat`

The verbs:

  sessions list                       live sessions, duplicates named
  sessions continue --scope S         nudge (interrupted), roll call (idle), or
                                      recover (cold — handoff unless --revive)
  sessions tickle | muster | revive   the same three scopes by their v1 names
  sessions mirror [--once]            one desktop sidebar pass (C-23.28)
  sessions retire | unretire <ID>     the durable operator flag (C-23.35)
  sessions handoff | subfleet handoff a bounded brief, dispatched (C-23.14)

Everything durable goes through the daemon (`client.Sessions`): this process
reads Claude Code's own files and decides, and the daemon records, delivers and
launches.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Sequence

from ..contracts import Exit
from . import handoff as handoff_module
from . import mirror as mirror_module
from . import nudge as nudge_module
from . import registry
from . import revive as revive_module
from . import transcripts
from .client import Sessions, SessionsUnsupported

PROG = "subfleet sessions"
SCOPE_CHOICES = ("interrupted", "idle", "cold")
HANDOFF_TARGETS = ("fable", "opus", "sonnet", "haiku", "astra", "terra", "sol")
TASK_CHOICES = ("lookup", "research", "sweep", "review", "build",
                "authored-prose", "strategy", "adjudication")
TIER_CHOICES = ("trivial", "easy", "standard", "hard")
SANDBOX_CHOICES = ("read-only", "workspace-write")


# --- output, shaped exactly as `subfleet/cli.py` shapes it --------------------

def out(text: str = "") -> None:
    print(text, flush=True)


def note(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def emit(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, default=str), flush=True)


def fail(code: Exit | int, message: str, fix: str | None = None) -> int:
    note(f"subfleet: {message}")
    if fix:
        note(f"  fix: {fix}")
    return int(code)


# --- shared plumbing ---------------------------------------------------------

def _cli():
    """`subfleet/cli.py`, imported lazily so the two modules can import freely."""
    from .. import cli
    return cli


def _sessions(args: argparse.Namespace) -> Sessions:
    cli = _cli()
    return Sessions(cli._client(args))


def _policy(args: argparse.Namespace) -> dict[str, Any]:
    """The daemon's policy if there is one beside the store, else the default.

    The kit reads caps, not routing, so an unreadable policy is a reason to use
    the shipped defaults rather than to refuse: a nudge held back because
    `policy.json` has a typo is a worse outcome than a nudge sent on the
    defaults.
    """
    from ..policy import DEFAULT_POLICY_PATH, PolicyError, load_policy
    root = _cli()._root(args)
    for path in (root / "policy.json", DEFAULT_POLICY_PATH):
        try:
            return load_policy(path)
        except (PolicyError, OSError):
            continue
    return {}


def _stage(args: argparse.Namespace, request_id: str):
    """Stage a prompt exactly where `subfleet run` stages one (C-2.3, C-23.54)."""
    cli = _cli()
    root = cli._root(args)
    return lambda text: cli.stage_prompt(text, request_id, root)


def _guard(handler):
    """The error block every daemon-touching verb repeats (C-17.3)."""
    def wrapped(args: argparse.Namespace) -> int:
        cli = _cli()
        from ..client import DaemonError, DaemonUnavailable
        from ..protocol import ProtocolError
        try:
            return handler(args)
        except DaemonUnavailable as exc:
            return cli._daemon_down(exc)
        except SessionsUnsupported as exc:
            return fail(Exit.DAEMON_UNAVAILABLE, str(exc), exc.fix)
        except DaemonError as exc:
            return cli._daemon_error(exc)
        except ProtocolError as exc:
            return fail(exc.code, str(exc))
        except handoff_module.HandoffError as exc:
            return fail(getattr(exc, "code", Exit.INVALID_INPUT),
                        f"handoff: {exc}", getattr(exc, "fix", None))
    return wrapped


# --- sessions list (C-23.30, C-23.31) ----------------------------------------

@_guard
def cmd_list(args: argparse.Namespace) -> int:
    sessions = _sessions(args)
    facts = sessions.state(None)
    lane_ids = set(facts.get("lane_sessions") or [])
    retired = {key for key, value in (facts.get("sessions") or {}).items()
               if value.get("retired")}
    listing = registry.sessions(lane_ids=lane_ids, include_lanes=bool(args.all),
                                live_only=not args.all)
    rows = []
    for item in listing:
        if item.session_id in retired and not args.all:
            # C-23.35: a retired session is absent from every listing.
            continue
        path = transcripts.transcript_path(item.session_id)
        state = transcripts.turn_state(path)
        rows.append({**item.to_dict(), "state": state.state, "detail": state.detail,
                     "age_s": state.age_s, "transcript": str(path) if path else None,
                     "retired": item.session_id in retired})
    if args.json:
        for row in rows:
            emit(row)
        return int(Exit.OK)
    if not rows:
        out("no live Claude Code sessions are registered")
        return int(Exit.OK)
    out(f"{'session':<10}{'pid':>8}  {'inbox':<6}{'state':<12}{'name'}")
    for row in rows:
        inbox = "yes" if row["socket_present"] else "no"
        mark = " (lane)" if row["lane"] else " (retired)" if row["retired"] else ""
        out(f"{row['session_id'][:8]:<10}{row['pid'] or '-':>8}  {inbox:<6}"
            f"{row['state']:<12}{(row['name'] or '-')[:44]}{mark}")
    for line in registry.duplicate_report(listing):
        note(f"subfleet sessions: duplicate {line}")
    return int(Exit.OK)


# --- sessions continue / tickle / muster / revive ------------------------------

def _scope_of(args: argparse.Namespace) -> str:
    return getattr(args, "scope", None) or "interrupted"


@_guard
def cmd_continue(args: argparse.Namespace) -> int:
    scope = _scope_of(args)
    if getattr(args, "handoff", False):
        if scope != "cold":
            return fail(Exit.INVALID_INPUT,
                        "sessions continue: --handoff applies to --scope cold",
                        "a live session is nudged, not handed off")
        if not getattr(args, "target", None):
            return fail(Exit.INVALID_INPUT,
                        "sessions continue: --handoff needs --to <model>",
                        "one of " + ", ".join(HANDOFF_TARGETS))
    if scope == "cold":
        return _continue_cold(args)
    sessions = _sessions(args)
    policy = _policy(args)
    named = [value for value in (getattr(args, "sessions", None) or []) if value]
    if getattr(args, "session", None):
        named.append(args.session)
    source = getattr(args, "source", None)
    # v1's `cmd_tickle`: with neither `--all` nor `--session`, it surveyed and
    # sent nothing. That is not a safety flourish — it is "you named no target",
    # and `subfleet tickle` is a command agents run to LOOK. `muster` and
    # `revive` had no such flag and did act, so only this scope has the rule.
    survey = (scope == "interrupted" and not named and not source
              and not getattr(args, "all", False))
    report = nudge_module.sweep(
        sessions, policy, scope=scope, only=named,
        transcript=getattr(args, "transcript", None), source=source,
        force=bool(getattr(args, "force", False)),
        dry_run=bool(getattr(args, "dry_run", False)) or survey,
        delay_s=getattr(args, "delay", None), manual=source is None,
        caller=_cli().session_id())
    # C-23.31: a request naming a headless lane run is refused with the reason.
    # A sweep that merely passed over one reports 0 — its exit code says whether
    # the sweep ran, not whether every session qualified — but a person who
    # named one asked a question that has a refusal for an answer.
    lanes = [item for item in report.outcomes if item.session_id in set(named)
             and "headless lane run" in item.reason]
    if args.json:
        emit(report.to_dict())
        return int(Exit.REFUSED if named and len(lanes) == len(named) else Exit.OK)
    out(nudge_module.render(report))
    if survey:
        note("subfleet sessions: a survey, because no session was named "
             "(`--all` nudges every interrupted session)")
    if named and len(lanes) == len(named):
        return fail(Exit.REFUSED,
                    "sessions continue: " + ", ".join(
                        f"{item.session_id[:8]} is a headless lane run" for item in lanes),
                    "a lane's deliverable is its last message; "
                    "`subfleet runs show <job>` for what it produced")
    return int(Exit.OK)


def _continue_cold(args: argparse.Namespace) -> int:
    """Plan decision 7: a cold sweep recovers, it does not resurrect.

    Recovery of a cold session is an *explicit* handoff, because a lease
    subfleet takes cannot exclude a desktop restart. So a bare
    `--scope cold` decides nothing and dispatches nothing: it lists what is
    recoverable and says which of the two recoveries applies. `--revive`
    launches headless continuations; `--handoff --to <model>` dispatches a brief
    per candidate instead. Neither is the default, because both spend a lane and
    both write somebody else's worktree.
    """
    sessions = _sessions(args)
    policy = _policy(args)
    named = [value for value in (getattr(args, "sessions", None) or []) if value]
    if getattr(args, "session", None):
        named.append(args.session)
    candidates = revive_module.cold_candidates(sessions, policy, only=named)
    if getattr(args, "handoff", False):
        return _continue_cold_by_handoff(args, sessions, policy, candidates)
    opt_in = bool(getattr(args, "revive", False))
    cap = getattr(args, "max", None)
    batch = int(cap if cap is not None
                else policy.get("sessions", {}).get("revive_max_batch", 8))
    attempts: list[revive_module.Attempted] = []
    launched = 0
    for candidate in candidates:
        if launched >= batch:
            attempts.append(revive_module.Attempted(
                session_id=candidate.session_id,
                reason=f"batch cap reached ({batch})", candidate=candidate))
            continue
        request_id = str(uuid.uuid4())
        attempt = revive_module.revive(
            sessions, policy, candidate.session_id,
            stage_prompt=_stage(args, request_id), opt_in=opt_in,
            force=bool(getattr(args, "force", False)),
            model=getattr(args, "model", None),
            dry_run=bool(getattr(args, "dry_run", False)),
            request_id=request_id)
        attempts.append(attempt)
        launched += int(attempt.admitted)
    if args.json:
        emit({"scope": "cold", "revive": opt_in,
              "sessions": [item.to_dict() for item in attempts]})
        return int(Exit.OK)
    out(revive_module.render(attempts))
    if not opt_in and any(not item.admitted and item.fix == revive_module.OPT_IN_FIX
                          for item in attempts):
        note("subfleet sessions: automatic revival of desktop-owned sessions is off "
             "(sessions.auto_revive_desktop_owned)")
        note(f"  fix: {revive_module.OPT_IN_FIX}")
    return int(Exit.OK)


def _continue_cold_by_handoff(args: argparse.Namespace, sessions, policy,
                              candidates) -> int:
    """`--scope cold --handoff --to <model>`: one brief per cold session.

    The recovery plan decision 7 calls the default, made explicit. Each brief is
    an ordinary submission (C-23.54) whose completion notice comes back to the
    caller, so the operator sees them in `subfleet runs --mine`.
    """
    cli = _cli()
    cap = getattr(args, "max", None)
    batch = int(cap if cap is not None
                else policy.get("sessions", {}).get("revive_max_batch", 8))
    rows: list[dict[str, Any]] = []
    for candidate in candidates[:batch]:
        if candidate.lane or candidate.retired:
            rows.append({"session_id": candidate.session_id, "job_id": None,
                         "reason": ("headless lane run" if candidate.lane
                                    else "retired by the operator")})
            continue
        request_id = str(uuid.uuid4())
        result = handoff_module.handoff(
            sessions, policy, session_id=candidate.session_id, last=False,
            model=args.target, stage_prompt=_stage(args, request_id),
            workdir=candidate.cwd, task=getattr(args, "task", None),
            tier=getattr(args, "tier", None), caller_session=cli.session_id(),
            caller_pid=cli.caller_pid(), request_id=request_id,
            dry_run=bool(getattr(args, "dry_run", False)))
        rows.append({"session_id": candidate.session_id, "job_id": result.job_id,
                     "reason": f"handed off to {args.target}",
                     "redactions": result.brief.redactions,
                     "transcript": result.brief.transcript})
    dropped = max(0, len(candidates) - batch)
    if args.json:
        emit({"scope": "cold", "handoff": args.target, "sessions": rows,
              "not_attempted": dropped})
        return int(Exit.OK)
    for row in rows:
        out(f"  {row['job_id'] or '-':<24} {row['session_id'][:8]}  {row['reason']}")
    if dropped:
        note(f"subfleet sessions: {dropped} more cold sessions were not attempted "
             f"(--max {batch})")
    return int(Exit.OK)


@_guard
def cmd_revive(args: argparse.Namespace) -> int:
    """`sessions revive <ID>`: one named session, refused unless it qualifies."""
    sessions = _sessions(args)
    policy = _policy(args)
    request_id = str(uuid.uuid4())
    attempt = revive_module.revive(
        sessions, policy, args.session,
        stage_prompt=_stage(args, request_id),
        opt_in=bool(getattr(args, "revive", False)),
        force=bool(getattr(args, "force", False)),
        model=getattr(args, "model", None),
        workdir=getattr(args, "C", None),
        dry_run=bool(getattr(args, "dry_run", False)),
        request_id=request_id)
    if args.json:
        emit(attempt.to_dict())
        if attempt.admitted or getattr(args, "dry_run", False):
            return int(Exit.OK)
        return int(Exit.REFUSED)        # C-17.3: --json does not change the verdict
    if attempt.admitted:
        out(attempt.job_id or "")
        note(f"subfleet sessions revive: {attempt.job_id} continues "
             f"{args.session[:8]} on {attempt.model or 'the routed tier'} "
             f"({attempt.reason})")
        return int(Exit.OK)
    if getattr(args, "dry_run", False):
        out(attempt.reason)
        return int(Exit.OK)
    return fail(Exit.REFUSED, f"sessions revive: {args.session[:8]}: {attempt.reason}",
                attempt.fix)


# --- sessions retire / unretire (C-23.35) ------------------------------------

@_guard
def cmd_retire(args: argparse.Namespace) -> int:
    result = _sessions(args).retire(args.session, args.reason)
    if args.json:
        emit(result)
    else:
        out(f"retired {args.session[:8]}"
            + (f": {args.reason}" if args.reason else ""))
        note("subfleet sessions: a retired session is absent from every listing "
             "and is never a revive candidate until it is unretired")
    return int(Exit.OK)


@_guard
def cmd_unretire(args: argparse.Namespace) -> int:
    result = _sessions(args).unretire(args.session)
    if args.json:
        emit(result)
    else:
        out(f"unretired {args.session[:8]}")
    return int(Exit.OK)


# --- sessions mirror (C-23.28) ------------------------------------------------

def cmd_mirror(args: argparse.Namespace) -> int:
    """One sidebar pass, or the sidecar's health. Never calls a provider."""
    cli = _cli()
    policy = _policy(args)
    engine = mirror_module.Mirror(cli._root(args), policy)
    if getattr(args, "list_accounts", False):
        return _mirror_list(args, engine)
    if getattr(args, "status", False):
        health = engine.health()
        if args.json:
            emit(health)
        else:
            out(f"mirror {health['status']}: {health['detail']}")
        return int(Exit.OK if health["status"] in ("healthy", "running", "absent")
                   else Exit.OPERATIONAL)
    options = mirror_module.options_from(
        policy,
        dry_run=bool(getattr(args, "dry_run", False)),
        prune=bool(getattr(args, "prune", False)),
        dead_home=getattr(args, "dead_home", None),
        exclude=tuple(getattr(args, "exclude", None) or ()),
        flag_sync=not bool(getattr(args, "no_flag_sync", False)),
        restore=not bool(getattr(args, "no_restore", False)),
        archive=getattr(args, "archive", None))
    result = engine.run_once(options)
    if args.json:
        emit(result.to_dict())
        return int(Exit.OK)
    changed = any((result.added, result.repaired, result.revived, result.pruned,
                   result.flag_synced, result.retitled, result.transcript_retitled))
    if getattr(args, "quiet", False):
        # v1's launchd cadence: silent on a no-op pass, which is exactly why
        # C-23.28 judges health from the sidecar and never from log recency.
        if changed:
            out(f"subfleet mirror: {result.summary}")
        return int(Exit.OK if result.state != "error" else Exit.OPERATIONAL)
    verb = "Would " if options.dry_run else ""
    out(f"{verb}mirror across {result.accounts} account folders, "
        f"{result.sessions} openable sessions: {result.summary}")
    if result.error:
        note(f"subfleet sessions mirror: {result.error}")
    if (result.added or result.repaired) and not options.dry_run:
        note("  restart the Claude app (⌘Q + reopen) to refresh the sidebar")
    return int(Exit.OK if result.state != "error" else Exit.OPERATIONAL)


def _mirror_list(args: argparse.Namespace, engine: "mirror_module.Mirror") -> int:
    """v1's `--list`: how many openable and dead sessions each account holds."""
    stems = engine.transcript_stems()
    rows = []
    for account, org, path in engine.folders(getattr(args, "exclude", None) or ()):
        try:
            entries = sorted(path.glob("local_*.json"))
        except OSError:
            entries = []
        openable = sum(1 for entry in entries
                       if (mirror_module._load(entry).get("cliSessionId") or "") in stems
                       and (mirror_module._load(entry).get("cliSessionId") or ""))
        rows.append({"account": account, "org": org, "path": str(path),
                     "openable": openable, "dead": len(entries) - openable,
                     "total": len(entries)})
    if args.json:
        for row in rows:
            emit(row)
        return int(Exit.OK)
    out("Claude Code account folders (openable / dead):")
    for row in rows:
        out(f"  {row['openable']:4d} openable + {row['dead']:4d} dead = "
            f"{row['total']:4d}   {row['account'][:8]}…/{row['org'][:8]}…")
    return int(Exit.OK)


# --- handoff (C-23.14, C-23.36, C-23.54) --------------------------------------

@_guard
def cmd_handoff(args: argparse.Namespace) -> int:
    cli = _cli()
    sessions = _sessions(args)
    policy = _policy(args)
    request_id = (getattr(args, "request_id", None) or str(uuid.uuid4()))[:128]
    result = handoff_module.handoff(
        sessions, policy,
        session_id=getattr(args, "session_id", None),
        last=bool(getattr(args, "last", False)),
        model=args.target,
        stage_prompt=_stage(args, request_id),
        workdir=getattr(args, "workdir", None),
        task=getattr(args, "task", None),
        tier=getattr(args, "tier", None),
        sandbox=getattr(args, "s", None),
        caller_session=cli.session_id(),
        caller_pid=cli.caller_pid(),
        out_path=(str(Path(args.o).expanduser().absolute())
                  if getattr(args, "o", None) else None),
        current_session=os.environ.get("CLAUDE_CODE_SESSION_ID"),
        request_id=request_id,
        lane_ids=sessions.state([]).get("lane_sessions") or [],
        dry_run=bool(getattr(args, "dry_run", False)))
    if args.json:
        emit(result.to_dict())
        return int(Exit.OK)
    if result.job_id is None:
        out(result.brief.text)
        return int(Exit.OK)
    out(result.job_id)
    note(f"subfleet handoff: {result.job_id} continues "
         f"{result.brief.session_id[:8]} on {args.target} in {result.brief.workdir}")
    note(f"  source transcript: {result.brief.transcript}")
    note(f"  credential/binary redactions: {result.brief.redactions}")
    return int(Exit.OK)


# --- the parser ---------------------------------------------------------------

def add_continue_flags(parser: argparse.ArgumentParser) -> None:
    """Every flag v1's `tickle`, `muster` and `revive` accepted, on one verb.

    `compat` rewrites those spellings into `sessions continue --scope ...` and
    appends the caller's remaining tokens, so each one must parse here.
    """
    parser.add_argument("sessions", nargs="*", metavar="SESSION",
                        help="only these sessions (interrupted defaults to a survey)")
    parser.add_argument("--scope", choices=SCOPE_CHOICES, default="interrupted",
                        help="interrupted (tickle), idle (muster), cold (recover)")
    parser.add_argument("--session", metavar="ID", help="one session id")
    parser.add_argument("--transcript", metavar="PATH",
                        help="transcript override, with a single --session")
    parser.add_argument("--all", action="store_true",
                        help="nudge every eligible live session")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the verdicts, send and launch nothing")
    parser.add_argument("--force", action="store_true",
                        help="ignore the age cap, the dedupe, and the cooldown")
    parser.add_argument("--delay", type=float, default=None, metavar="S",
                        help="seconds to wait before the transcript re-check")
    parser.add_argument("--revive", action="store_true",
                        help="with --scope cold: launch a headless continuation "
                             "of a desktop-owned session (off by default)")
    parser.add_argument("--model", metavar="M",
                        help="with --scope cold: revive on this model instead of "
                             "the session's own recorded tier (C-23.39)")
    parser.add_argument("--max", type=int, default=None, metavar="N",
                        help="with --scope cold: cap concurrent revives")
    parser.add_argument("--handoff", action="store_true",
                        help="with --scope cold: dispatch a continuity brief per "
                             "session instead of reviving (needs --to)")
    parser.add_argument("--to", dest="target", choices=HANDOFF_TARGETS,
                        help="with --handoff: the model to continue the work on")
    parser.add_argument("--task", choices=TASK_CHOICES,
                        help="with --handoff: what kind of work this is")
    parser.add_argument("--tier", choices=TIER_CHOICES,
                        help="with --handoff: the minimum capability for --task")
    parser.add_argument("--source", metavar="SOURCE",
                        help="the SessionStart source that woke this sweep "
                             "(startup|resume|compact|clear); passing one marks "
                             "the sweep a hook wake rather than a hand-started "
                             "one, which shortens the quiet window (C-23.34)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="subfleet-sessions", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true",
                        help="include lane runs, retired and dead rows")
    parser.set_defaults(handler=cmd_list, json=False, sessions_command=None)
    sub = parser.add_subparsers(dest="sessions_command")
    add_verbs(sub, nested=False)
    return parser


def add_verbs(sub, *, nested: bool = True) -> None:
    """Register the sessions sub-verbs on `sub`.

    Shared by `subfleet sessions` and the `subfleet-sessions` entry point so the
    two surfaces cannot drift. `nested` follows `cli._add_json`'s convention: a
    child of `subfleet sessions` must not overwrite the parent's `--json`.
    """
    def add_json(parser: argparse.ArgumentParser) -> None:
        if nested:
            parser.add_argument("--json", action="store_true",
                                default=argparse.SUPPRESS,
                                help="one JSON object per line (C-17.4)")
        else:
            parser.add_argument("--json", action="store_true",
                                help="one JSON object per line (C-17.4)")

    p_list = sub.add_parser("list", help="live Claude Code sessions and their state")
    p_list.add_argument("--all", action="store_true",
                        help="include lane runs, retired and dead rows")
    add_json(p_list)

    p_continue = sub.add_parser("continue", help="nudge, roll-call, or recover sessions")
    add_continue_flags(p_continue)
    add_json(p_continue)

    for name, scope, help_text in (
        ("tickle", "interrupted", "resume nudge for sessions a restart cut off"),
        ("muster", "idle", "roll call after a switch that killed nothing"),
    ):
        alias = sub.add_parser(name, help=help_text)
        add_continue_flags(alias)
        alias.set_defaults(scope=scope)
        add_json(alias)

    p_revive = sub.add_parser("revive", help="continue one cold session headlessly")
    p_revive.add_argument("session", metavar="SESSION",
                          help="the session id to continue")
    p_revive.add_argument("--revive", action="store_true",
                          help="launch even though the desktop app owns it")
    p_revive.add_argument("--model", metavar="M",
                          help="revive on this model instead of the session's own "
                               "recorded tier; the substitution is recorded (C-23.39)")
    p_revive.add_argument("-C", dest="C", metavar="DIR",
                          help="workdir override (default: the session's own cwd)")
    p_revive.add_argument("--force", action="store_true",
                          help="ignore the minimum age and the turn state")
    p_revive.add_argument("--dry-run", action="store_true",
                          help="print the verdict; launch nothing")
    add_json(p_revive)

    p_mirror = sub.add_parser("mirror", help="one desktop sidebar pass (C-23.28)")
    p_mirror.add_argument("--once", action="store_true",
                          help="run one pass now (the default)")
    p_mirror.add_argument("--status", action="store_true",
                          help="print the sidecar's health instead of running")
    p_mirror.add_argument("--list", dest="list_accounts", action="store_true",
                          help="per-account openable/dead counts; change nothing")
    p_mirror.add_argument("--quiet", action="store_true",
                          help="one summary line, and only when something changed")
    p_mirror.add_argument("--dry-run", action="store_true",
                          help="report what a pass would change; change nothing")
    p_mirror.add_argument("--prune", action="store_true",
                          help="also remove dead-session copies outside --dead-home")
    p_mirror.add_argument("--dead-home", metavar="ORG",
                          help="org folder to keep dead sessions in")
    p_mirror.add_argument("--exclude", action="append", default=[], metavar="UUID")
    p_mirror.add_argument("--no-restore", action="store_true",
                          help="skip reviving dead sessions from --archive")
    p_mirror.add_argument("--no-flag-sync", action="store_true",
                          help="skip isArchived/isStarred/title propagation")
    p_mirror.add_argument("--archive", metavar="GLOB",
                          help="recursive glob of archived transcripts")
    add_json(p_mirror)

    p_retire = sub.add_parser("retire", help="never list or revive this session again")
    p_retire.add_argument("session", metavar="SESSION")
    p_retire.add_argument("--reason", help="why, recorded with the flag")
    add_json(p_retire)

    p_unretire = sub.add_parser("unretire", help="clear the operator's retirement flag")
    p_unretire.add_argument("session", metavar="SESSION")
    add_json(p_unretire)

    p_handoff = sub.add_parser("handoff",
                               help="continue a session through a fresh agent")
    add_handoff_flags(p_handoff)
    add_json(p_handoff)


def add_handoff_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("session_id", nargs="?", help="exact Claude session UUID")
    parser.add_argument("--last", action="store_true",
                        help="this session, or the newest durable transcript")
    parser.add_argument("--to", dest="target", required=True, choices=HANDOFF_TARGETS,
                        help="the model to continue the work on")
    parser.add_argument("-C", dest="workdir", metavar="DIR",
                        help="target worktree (default: the transcript's cwd)")
    parser.add_argument("--task", choices=TASK_CHOICES,
                        help="what kind of work this is; also picks the sandbox")
    parser.add_argument("--tier", choices=TIER_CHOICES)
    parser.add_argument("-s", dest="s", choices=SANDBOX_CHOICES,
                        help="sandbox (default: the task's policy permission)")
    parser.add_argument("-o", dest="o", metavar="OUT",
                        help="export the deliverable here")
    parser.add_argument("--request-id", metavar="ID", dest="request_id")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the brief; dispatch nothing")


HANDLERS = {
    "list": cmd_list,
    "continue": cmd_continue,
    "tickle": cmd_continue,
    "muster": cmd_continue,
    "revive": cmd_revive,
    "mirror": cmd_mirror,
    "retire": cmd_retire,
    "unretire": cmd_unretire,
    "handoff": cmd_handoff,
}


def dispatch(args: argparse.Namespace) -> int:
    """`subfleet sessions`' handler: one branch per sub-verb, as `cmd_lanes` does."""
    verb = getattr(args, "sessions_command", None) or "list"
    handler = HANDLERS.get(verb)
    if handler is None:
        return fail(Exit.INVALID_INPUT, f"sessions: unknown verb {verb!r}",
                    "one of " + ", ".join(sorted(HANDLERS)))
    return handler(args)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if not getattr(args, "json", False):
        args.json = False
    try:
        return int(dispatch(args))
    except KeyboardInterrupt:
        note("subfleet sessions: interrupted")
        return int(Exit.CANCELLED)
    except OSError as exc:
        return fail(Exit.OPERATIONAL, str(exc))


if __name__ == "__main__":                              # pragma: no cover
    raise SystemExit(main())
