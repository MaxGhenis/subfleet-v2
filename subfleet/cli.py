"""subfleet CLI: a thin client over the daemon socket, with an offline reader.

  subfleet [status]              lanes, readings, closures, running jobs
  subfleet run ...               submit a job; detached by default in a Claude session
  subfleet runs [--mine ...]     the job ledger, newest first     (alias: jobs)
  subfleet runs show <id>        one job's metadata and artifacts (alias: show)
  subfleet runs reap             reconcile jobs whose runner is gone
  subfleet wait <id>...          long-poll until terminal; rc = the job's rc
  subfleet kill <id>             cancel a job (offline: signal the recorded pgid)
  subfleet resume <id> [PROMPT]  continue a job on its own lane (alias: resume-codex)
  subfleet lanes [list|probe|enroll|hold|release|transfer]
  subfleet why <id> | --task T --tier X
  subfleet daemon [start|stop|status|logs|install]
  subfleet doctor [--live]      pass/fail/unknown checks, each with a fix line
  subfleet hook <event>         a Claude Code hook entry point (JSON on stdin)
  subfleet ping [--session ID] TEXT                              (alias: notify)

The verb spellings are v1's and are permanent (plan amendment 1). Stdout carries
the contract, stderr the prose, and `--json` emits JSON objects only (C-17.4).
Exit codes are the one table in C-17.3.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

from . import capacity, ids, protocol
from .client import (
    LOG_NAME,
    SOCKET_NAME,
    Client,
    DaemonError,
    DaemonUnavailable,
    same_process,
    state_root,
)
from .contracts import JobState, Sandbox, WAIT_POLL_MAX_S, Exit
from .offline import (KNOWN_SCHEMA_VERSION, Offline, OfflineUnavailable,
                      SchemaTooNew, age_adjusted_label)
from .protocol import ProtocolError

PROG = "subfleet"
START_DAEMON = "subfleet daemon start"
EXIT_CODES = {int(code) for code in Exit}
TERMINAL_STATES = {state.value for state in JobState if state.terminal}
LIVE_STATES = {state.value for state in JobState if not state.terminal}

# Deprecated but accepted through milestone 8 with a stderr note (C-17.2).
RETIRED_MODELS = {"sol": "astra"}
LEGACY_TASK_CLASSES = {"review": "review", "build": "build", "sweep": "sweep"}
LEGACY_MODEL_CLASSES = {"fable": "fable"}

MODEL_CHOICES = ("fable", "opus", "sonnet", "haiku", "astra", "terra", "sol")
TASK_CHOICES = ("lookup", "research", "sweep", "review", "build",
                "authored-prose", "strategy", "adjudication")
TIER_CHOICES = ("trivial", "easy", "standard", "hard")
SANDBOX_CHOICES = tuple(item.value for item in Sandbox)
LANE_ACTIONS = ("list", "probe", "enroll", "hold", "release", "transfer")

AF_UNIX_PATH_MAX = 103          # sun_path is 104 bytes including the NUL
WAIT_BACKOFF_MAX_S = 5.0        # cap on the pause after an immediate long poll
PLIST_LABEL = "com.subfleet.daemon"
PLIST_PATH = "~/Library/LaunchAgents/com.subfleet.daemon.plist"


# --- output ------------------------------------------------------------------

def out(text: str = "") -> None:
    """The contract goes to stdout (C-17.4)."""
    print(text)


def note(text: str) -> None:
    """Human prose goes to stderr (C-17.4)."""
    print(text, file=sys.stderr)


def emit(obj: Any) -> None:
    """One JSON object on one line, no prose (C-17.4)."""
    print(json.dumps(obj, sort_keys=True, default=str))


def fail(code: Exit | int, message: str, fix: str | None = None) -> int:
    note(f"{PROG}: {message}")
    if fix:
        note(f"  fix: {fix}")
    return int(code)


# --- environment -------------------------------------------------------------

def session_id(env: dict[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    return (env.get("CLAUDE_CODE_SESSION_ID") or "").strip() or None


def in_claude_session(env: dict[str, str] | None = None) -> bool:
    """True inside a Claude Code session's tool shell, exactly as v1 decides.

    The harness exports `CLAUDECODE=1` and `CLAUDE_CODE_SESSION_ID` to every
    Bash tool process, and both are inherited by anything it spawns; that is
    the process tree that dies on an account switch (v1 `notify.in_claude_session`).
    """
    env = os.environ if env is None else env
    return bool((env.get("CLAUDECODE") or "").strip()) or bool(
        (env.get("CLAUDE_CODE_SESSION_ID") or "").strip())


def caller_pid(env: dict[str, str] | None = None) -> int | None:
    env = os.environ if env is None else env
    try:
        value = int((env.get("CLAUDE_PID") or "").strip())
    except ValueError:
        return None
    return value if value > 0 else None


def launch_mode(args: argparse.Namespace,
                env: dict[str, str] | None = None) -> tuple[str, str]:
    """('detached' | 'sync', why), following v1's precedence (C-17.6).

    Inside a Claude Code session the tool shell's process tree dies on an
    account switch or the desktop app's idle SIGTERM, so `run` returns as soon
    as the daemon has the job and the caller re-joins with `subfleet wait`.
    Outside a session (a terminal, a launchd lane) `run` blocks as v1 did.
    """
    env = os.environ if env is None else env
    if getattr(args, "dry_run", False) or getattr(args, "why", False):
        return "sync", "dry-run"
    if getattr(args, "detach", False):
        return "detached", "-d"
    override = (env.get("SUBFLEET_RUN_DETACH") or "").strip().lower()
    if override in {"0", "false", "no", "off"}:
        return "sync", "SUBFLEET_RUN_DETACH=0"
    if override in {"1", "true", "yes", "on"}:
        return "detached", "SUBFLEET_RUN_DETACH=1"
    if in_claude_session(env):
        if getattr(args, "attach", False):
            return "detached", "inside a Claude session; --attach waits inline"
        return "detached", "inside a Claude session — the job must outlive it"
    return "sync", "outside a Claude session"


# --- exit-code mapping (C-17.3) ----------------------------------------------

# An outcome class that names exactly one code in C-17.3. `limited` is left
# out: it is exit 3 or exit 4 depending on whether the lane was pinned, and the
# daemon is the one that knows.
CLASS_EXITS = {"auth-dead": int(Exit.AUTH_DEAD),
               "cli-too-old": int(Exit.CLI_TOO_OLD)}


def exit_for_job(job: dict[str, Any], *, quiet: bool = False) -> int:
    """The job's rc mapped onto the one exit-code table (C-17.3)."""
    state = job.get("state")
    if state == JobState.CANCELLED.value:
        return int(Exit.CANCELLED)
    if state == JobState.LOST.value:
        return int(Exit.JOB_LOST)
    if state == JobState.SUCCEEDED.value:
        return int(Exit.OK)
    rc = job.get("rc")
    outcome = CLASS_EXITS.get(job_row(job).get("outcome_class"))
    if isinstance(rc, bool) or not isinstance(rc, int) or rc == 0:
        return outcome or int(Exit.OPERATIONAL)
    if rc in EXIT_CODES:
        return rc
    if outcome:
        return outcome
    if not quiet:
        note(f"{PROG}: {job.get('job_id') or job.get('id')} "
             f"failed with provider rc {rc}")
    return int(Exit.OPERATIONAL)


# --- row normalising ---------------------------------------------------------

def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def as_number(value: Any) -> float | None:
    """A number, or None; never an exception inside a formatter."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def rows_of(value: Any) -> list[dict[str, Any]]:
    """Only the dict rows of whatever the daemon sent.

    The daemon is built by another lane; a result whose shape drifts should
    render as much as it can rather than raise inside a formatter.
    """
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def job_row(row: dict[str, Any]) -> dict[str, Any]:
    """One ledger row, however the daemon or the store spelled its keys."""
    attempt = row.get("attempt") if isinstance(row.get("attempt"), dict) else {}
    merged = {**attempt, **row}
    return {
        "id": _first(merged, "job_id", "id"),
        "state": merged.get("state"),
        "family": _first(merged, "family", "provider"),
        "model": _first(merged, "model", "model_served", "model_requested"),
        "lane": _first(merged, "lane", "lane_id"),
        "rc": merged.get("rc"),
        "out_bytes": _first(merged, "out_bytes", "bytes") or 0,
        "duration_s": merged.get("duration_s"),
        "notice": _first(merged, "notice", "notify", "notice_state"),
        "caller_session": merged.get("caller_session"),
        "workdir": merged.get("workdir"),
        "outcome_class": merged.get("outcome_class"),
        "receipts": merged.get("receipts") or {},
        "attestation": merged.get("attestation"),
        "name": merged.get("name"),
    }


def format_runs(rows: Sequence[dict[str, Any]]) -> str:
    """v1's `runs` table, one row per job.

    The rc column keeps v1's contract: pollers read a bare state word for a job
    that has not finished and an integer for one that has.
    """
    normalised = [job_row(row) for row in rows_of(rows)]
    if not normalised:
        return "no recorded jobs"
    id_width = max(24, *(len(str(row["id"] or "-")) for row in normalised))
    lines = [
        f"{'id':<{id_width}} {'family':<7} {'model':<16} {'lane':<12} "
        f"{'rc':>9} {'bytes':>9} {'seconds':>8} {'notice':<10} {'caller':<10} workdir"
    ]
    for row in normalised:
        state = row["state"]
        if state in LIVE_STATES and "exit" in (row["receipts"] or {}):
            # C-17.5: the guardian's receipt says it finished; the row has not
            # caught up because no daemon has finalized it yet.
            rc = "EXITED"
        elif state in LIVE_STATES:
            rc = str(state).upper()
        elif isinstance(row["rc"], int) and not isinstance(row["rc"], bool):
            rc = str(row["rc"])
        else:
            rc = str(state or "-").upper()
        seconds = as_number(row["duration_s"])
        duration = "-" if seconds is None else f"{seconds:.1f}"
        caller = row["caller_session"] or ""
        lines.append(
            f"{str(row['id'] or '-'):<{id_width}} "
            f"{str(row['family'] or '-'):<7.7} "
            f"{str(row['model'] or '-'):<16.16} "
            f"{str(row['lane'] or '-'):<12.12} "
            f"{rc:>9.9} {int(as_number(row['out_bytes']) or 0):>9} {duration:>8} "
            f"{str(row['notice'] or '-'):<10.10} {(caller[:8] or '-'):<10.10} "
            f"{row['workdir'] or '-'}"
        )
    return "\n".join(lines)


def _percent(value: Any) -> str:
    number = as_number(value)
    if number is None:
        return "?"
    return f"{number * 100:.0f}%" if number <= 1.0 else f"{number:.0f}%"


def format_status(data: dict[str, Any]) -> str:
    """Lanes with their newest readings, live closures, and running jobs."""
    lines: list[str] = []
    lanes = rows_of(data.get("lanes"))
    readings = rows_of(data.get("readings"))
    closures = rows_of(data.get("closures"))
    running = rows_of(data.get("running") or data.get("jobs"))
    by_lane: dict[str, list[dict[str, Any]]] = {}
    for reading in readings:
        by_lane.setdefault(str(reading.get("lane_id")), []).append(reading)
        reading["label"] = age_adjusted_label(reading.get("label"),
                                              reading.get("observed_at"))

    if not lanes:
        lines.append("no lanes enrolled — subfleet lanes enroll <credential>")
    else:
        lines.append(f"{'lane':<12} {'provider':<8} {'account':<28} {'owner':<6} "
                     f"{'flight':>6}  windows")
        for lane in lanes:
            marks = []
            for reading in sorted(by_lane.get(str(lane.get("lane_id")), []),
                                  key=lambda r: str(r.get("window"))):
                label = reading.get("label")
                stale = " stale" if label == "stale-provider" else ""
                if label in {"provider", "stale-provider"}:
                    marks.append(f"{reading.get('window')} "
                                 f"{_percent(reading.get('utilization'))}{stale}")
                else:
                    marks.append(f"{reading.get('window')} {label}")
            flags = []
            if lane.get("desktop"):
                flags.append("desktop")
            if not lane.get("enabled", 1):
                flags.append("disabled")
            # C-10.6: no percentage for this lane, and the reason beside it.
            if lane.get("identity_status") in ("mismatch", "unverified"):
                flags.append(f"identity-{lane['identity_status']}")
            lines.append(
                f"{str(lane.get('lane_id') or '-'):<12.12} "
                f"{str(lane.get('provider') or '-'):<8.8} "
                f"{str(lane.get('account_key') or '-'):<28.28} "
                f"{str(lane.get('owner') or '-'):<6.6} "
                f"{int(as_number(lane.get('in_flight')) or 0):>6}  "
                f"{' · '.join(marks) or 'no reading'}"
                + (f"  [{', '.join(flags)}]" if flags else "")
            )
    if closures:
        lines.append("")
        lines.append("closures")
        for closure in closures:
            lines.append(f"  {closure.get('lane_id')} {closure.get('scope')} "
                         f"until {closure.get('until_at')} "
                         f"({closure.get('reason')}, {closure.get('clock_source')})")
    lines.append("")
    lines.append(f"running jobs: {len(running)}")
    if running:
        lines.append(format_runs(running))
    return "\n".join(lines)


# --- daemon / offline plumbing ------------------------------------------------

def _root(args: argparse.Namespace) -> Path:
    return state_root()


def _client(args: argparse.Namespace, *, timeout: float | None = None) -> Client:
    root = _root(args)
    return Client(root) if timeout is None else Client(root, timeout=timeout)


def _offline(args: argparse.Namespace) -> Offline:
    return Offline(_root(args))


def _note_schema(store: Offline) -> None:
    """C-3.5: say so when the store was written by a newer subfleet."""
    if store.newer_schema:
        note(f"{PROG}: the store is at schema version {store.newer_schema}; this "
             f"CLI knows version {KNOWN_SCHEMA_VERSION} — some columns may be "
             f"missing from what is shown")


def _daemon_down(exc: Exception) -> int:
    return fail(Exit.DAEMON_UNAVAILABLE, str(exc), getattr(exc, "fix", START_DAEMON))


def _daemon_error(exc: DaemonError) -> int:
    note(f"{PROG}: {exc}")
    if exc.fix:
        note(f"  fix: {exc.fix}")
    return int(exc.code)


def _asdict(args_obj: Any) -> dict[str, Any]:
    from dataclasses import asdict as _dc_asdict
    return _dc_asdict(args_obj)


# --- status (C-17.1) ----------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    """Lanes, readings, closures, and running jobs; from the store when down."""
    try:
        client = _client(args)
        data = client.call("daemon.status", {})
        if "lanes" not in data:
            data = dict(data)
            data["lanes"] = (client.call("lanes", _asdict(protocol.LanesArgs()))
                             .get("lanes", []))
        if "readings" not in data:
            data["readings"] = client.call(
                "readings", _asdict(protocol.ReadingsArgs())).get("readings", [])
        if "running" not in data and "jobs" not in data:
            data["running"] = client.call(
                "list", _asdict(protocol.ListArgs(running=True, last=50))
            ).get("jobs", [])
    except DaemonUnavailable:
        store = _offline(args)
        try:
            data = store.status()
        except OfflineUnavailable as exc:
            return _daemon_down(exc)
        _note_schema(store)
    except DaemonError as exc:
        return _daemon_error(exc)
    except ProtocolError as exc:
        return fail(exc.code, str(exc))
    if args.json:
        emit(data)
        return int(Exit.OK)
    if data.get("offline"):
        note(f"{PROG} status: offline — read from the store; "
             f"readings are as of the last daemon write")
    out(format_status(data))
    return int(Exit.OK)


# --- run (C-17.2, C-17.6) -----------------------------------------------------

def _apply_deprecations(args: argparse.Namespace) -> None:
    """Accept v1 spellings with a stderr note (C-17.2)."""
    legacy = getattr(args, "t", None)
    if legacy:
        note(f"{PROG} run: -t {legacy} is deprecated; use --task/--tier "
             f"(accepted through milestone 8)")
        if legacy in LEGACY_MODEL_CLASSES and not args.m:
            args.m = LEGACY_MODEL_CLASSES[legacy]
        elif legacy in LEGACY_TASK_CLASSES and not args.task:
            args.task = LEGACY_TASK_CLASSES[legacy]
            args.tier = args.tier or "standard"
    if getattr(args, "overflow", False):
        note(f"{PROG} run: --overflow is deprecated and ignored; the daemon "
             f"walks the chain upward on its own")
    if args.m in RETIRED_MODELS:
        replacement = RETIRED_MODELS[args.m]
        note(f"{PROG} run: -m {args.m} is retired; dispatching {replacement}")
        args.m = replacement


INBOX_KEEP_S = 7 * 24 * 3600


def stage_prompt(text: str, request_id: str, root: Path) -> Path:
    """Write inline prompt text where the daemon can read it.

    `submit` carries a path, not bytes (C-16.2), so prompt text typed on the
    command line is staged inside the state root (C-2.1) and the daemon copies
    it to the job's own `prompt.md` (C-2.3). Stale stagings are pruned here
    because nothing else owns this directory.
    """
    inbox = root / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    os.chmod(inbox, 0o700)
    cutoff = time.time() - INBOX_KEEP_S
    for stale in inbox.glob("*.md"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            pass
    # A caller may submit the same request id concurrently (C-6.2), including
    # with a different payload. Each submission needs its own immutable input
    # until the daemon has read it and compared the digest. Reusing a filename
    # here can silently replace the first submission's prompt before that read.
    # The digest is only a private filename prefix, never the caller's path.
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
    handle, filename = tempfile.mkstemp(prefix=f"{digest}-", suffix=".md", dir=inbox)
    path = Path(filename)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(text if text.endswith("\n") else text + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _prompt_path(args: argparse.Namespace, request_id: str,
                 root: Path) -> tuple[str | None, int | None]:
    """The prompt file to send, staging inline text when there is no `-p`."""
    if args.p:
        path = Path(args.p).expanduser()
        try:
            with open(path, "rb"):
                pass
        except OSError as exc:
            return None, fail(Exit.INVALID_INPUT, f"run: cannot read prompt file: {exc}")
        return str(path.resolve()), None
    if args.prompt is None:
        return None, fail(Exit.INVALID_INPUT,
                          "run: one of -p PROMPTFILE or PROMPT_TEXT is required")
    try:
        return str(stage_prompt(args.prompt, request_id, root)), None
    except OSError as exc:
        return None, fail(Exit.OPERATIONAL, f"run: cannot stage the prompt: {exc}")


def _validate_run(args: argparse.Namespace) -> tuple[str | None, int | None]:
    """Client-side admission checks that never need a daemon (C-6.1, C-6.5)."""
    if args.task and not args.tier:
        return None, fail(Exit.INVALID_INPUT, "run: --tier is required with --task")
    if args.tier and not args.task:
        return None, fail(Exit.INVALID_INPUT, "run: --tier requires --task")
    if not (args.task or args.m or args.a or args.H):
        return None, fail(Exit.INVALID_INPUT,
                          "run: name the work or pin the lane",
                          "--task <task> --tier <tier>, or -m <model>, or -a/-H")
    if getattr(args, "isolated_review", False):
        from .gate.cli import MANAGED
        if args.s != "read-only" or not args.review_root:
            return None, fail(Exit.REFUSED, "run -I requires -s read-only and -D REVIEW_ROOT (C-23.2)")
        for key in MANAGED:
            if key in os.environ:
                return None, fail(Exit.REFUSED, f"isolated review inherits {key} (C-23.3)", "review the managed policy before retrying")
    workdir = Path(args.C).expanduser()
    try:
        resolved = workdir.resolve()
    except OSError as exc:
        return None, fail(Exit.INVALID_INPUT, f"run: -C {workdir}: {exc}")
    if not resolved.is_dir():
        return None, fail(Exit.INVALID_INPUT, f"run: -C {resolved} is not a directory")
    parts = resolved.parts
    if not args.allow_tmp and (parts[:2] == ("/", "tmp") or parts[:3] == ("/", "private", "tmp")):
        return None, fail(Exit.REFUSED,
                          f"run: a workdir under /tmp is refused: {resolved} (C-2.4)",
                          "pass --allow-tmp, or use a directory under $HOME")
    if args.o:
        parent = Path(args.o).expanduser().absolute().parent
        if not parent.is_dir():
            return None, fail(Exit.INVALID_INPUT,
                              f"run: -o directory does not exist: {parent}")
    return str(resolved), None


def _hint_paths(root: Path, job_id: str, out_path: str | None,
                result: dict[str, Any]) -> tuple[str, str]:
    """Where the deliverable and the lane log will be (C-2.3)."""
    job_dir = root / "jobs" / job_id
    deliverable = (result.get("out_path") or out_path
                   or str(job_dir / "a1" / "deliverable.md"))
    log = result.get("log_path") or str(job_dir / "a1" / "lane.log")
    return str(deliverable), str(log)


def cmd_run(args: argparse.Namespace) -> int:
    _apply_deprecations(args)
    workdir, error = _validate_run(args)
    if error is not None:
        return error
    request_id = args.request_id or str(uuid.uuid4())
    root = _root(args)
    prompt_path, error = _prompt_path(args, request_id, root)
    if error is not None:
        return error
    sandbox = args.s or Sandbox.READ_ONLY.value
    submit = protocol.SubmitArgs(
        request_id=request_id,
        kind="dispatch",
        workdir=workdir,
        prompt_path=prompt_path,
        sandbox=sandbox,
        task=args.task,
        tier=args.tier,
        pinned_model=args.m,
        pinned_lane=args.a or args.H,
        out_path=str(Path(args.o).expanduser().absolute()) if args.o else None,
        name=args.name,
        exclusions=list(args.exclude or []),
        allow_desktop=bool(args.allow_desktop),
        allow_tmp=bool(args.allow_tmp),
        in_place=bool(args.in_place),
        independent=bool(args.independent),
        parent_job_id=args.parent,
        caller_session=session_id(),
        caller_pid=caller_pid(),
        no_preamble=bool(args.no_preamble),
        dry_run=bool(args.dry_run or args.why),
        isolated_review=bool(getattr(args, "isolated_review", False)),
        review_root=str(Path(args.review_root).expanduser().resolve()) if getattr(args, "review_root", None) else None,
    )
    try:
        client = _client(args)
        result = client.call("submit", _asdict(submit), request_id=request_id)
    except DaemonUnavailable as exc:
        return _daemon_down(exc)
    except DaemonError as exc:
        return _daemon_error(exc)
    except ProtocolError as exc:
        return fail(exc.code, str(exc))

    if submit.dry_run:
        decision = result.get("decision", result)
        if args.json:
            emit(result)
        elif args.why:
            out(json.dumps(decision, indent=1, sort_keys=True, default=str))
        else:
            out(_format_decision(decision))
        return int(Exit.OK)

    job_id = result.get("job_id") or result.get("id") or ""
    if not job_id:
        return fail(Exit.OPERATIONAL, "run: the daemon returned no job id")
    mode, reason = launch_mode(args)
    wait_inline = bool(args.attach) or mode == "sync"
    deliverable, log = _hint_paths(root, job_id, submit.out_path, result)

    if args.json:
        emit({
            "job_id": job_id, "run_id": job_id, "request_id": request_id,
            "created": bool(result.get("created", True)),
            "state": result.get("state"), "model": result.get("model"),
            "lane": result.get("lane") or result.get("lane_id"),
            "detached": mode == "detached", "wait_inline": wait_inline,
            "reason": reason, "out": deliverable, "log": log,
        })
    else:
        out(job_id)
        note(f"{PROG} run: dispatched job={job_id} request={request_id}"
             + ("" if result.get("created", True) else " (existing job for this request id)")
             + f" ({mode} — {reason})")
        note(f"  out: {deliverable}")
        note(f"  log: {log}")
        if wait_inline:
            note(f"  waiting inline; if this session restarts: {PROG} wait {job_id}")
        else:
            note(f"  done → {PROG} wait {job_id}   (blocks; ok under run_in_background)")
        note(f"  status: {PROG} runs --mine · details: {PROG} runs show {job_id}"
             f" · cancel: {PROG} kill {job_id}")

    if not wait_inline:
        if args.json and args.no_wait_queue and result.get("state") == JobState.QUEUED.value:
            return int(Exit.QUEUED)
        return int(Exit.OK)
    return wait_jobs(args, [job_id], timeout=None, quiet=args.json)


def _format_decision(decision: dict[str, Any]) -> str:
    if not isinstance(decision, dict):
        return json.dumps(decision, default=str)
    lines = [f"chain: {' → '.join(decision.get('chain') or []) or '-'}"]
    for evaluation in rows_of(decision.get("evaluations")):
        lines.append(f"  {evaluation.get('model')}: "
                     f"{evaluation.get('reason') or evaluation.get('result') or ''}")
        for rejected in rows_of(evaluation.get("rejections", evaluation.get("rejected"))):
            lines.append(f"    - {rejected.get('lane_id')}: {rejected.get('reason')}")
    lines.append(f"chosen: {decision.get('chosen_model') or '-'} on "
                 f"{decision.get('chosen_lane') or '-'} — {decision.get('reason') or '-'}")
    if decision.get("policy_hash"):
        lines.append(f"policy: {decision['policy_hash']}")
    return "\n".join(lines)


# --- wait (C-15.4, C-17.3) ----------------------------------------------------

def _jobs_from_wait(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalise whatever shape the daemon used for the terminal-state map."""
    payload = result.get("jobs", result.get("states"))
    jobs: dict[str, dict[str, Any]] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, dict):
                jobs[str(key)] = {"job_id": str(key), **value}
            elif isinstance(value, str):
                jobs[str(key)] = {"job_id": str(key), "state": value}
    elif isinstance(payload, list):
        for value in payload:
            if isinstance(value, dict):
                key = value.get("job_id") or value.get("id")
                if key:
                    jobs[str(key)] = value
    elif result.get("job_id"):
        jobs[str(result["job_id"])] = result
    return jobs


def _wait_summary(job: dict[str, Any]) -> str:
    row = job_row(job)
    state = str(row["state"] or "unknown").upper()
    rc = row["rc"]
    label = state if state != "FAILED" or not isinstance(rc, int) else f"FAILED rc={rc}"
    seconds = as_number(row["duration_s"])
    duration = "-" if seconds is None else f"{seconds:.0f}s"
    target = (job.get("out_path") or _artifact_path(job, "deliverable")
              or job.get("deliverable_path") or "-")
    attempt = job.get("attempt") or {}
    detail = job.get("outcome_detail") or attempt.get("outcome_detail")
    return (f"{PROG} wait: {row['id']} {label} · {row['model'] or '-'} · "
            f"lane={row['lane'] or '-'} · {duration} · out={target}"
            + (f" · {detail}" if state == "FAILED" and detail else ""))


def wait_jobs(args: argparse.Namespace, ids: Sequence[str], *,
              timeout: float | None, mine: str | None = None,
              last: bool = False, quiet: bool = False) -> int:
    """Loop the daemon's long poll until terminal or `--timeout` (C-15.4)."""
    started = time.monotonic()
    requested = {str(job_id) for job_id in ids}
    pending = set(requested)
    finished: dict[str, dict[str, Any]] = {}
    timed_out = False
    # Only a resolver (`--mine`, `--last`) may widen the set; otherwise a daemon
    # that mentions another job must not change what this call blocks on or
    # what it exits with (C-17.3).
    adopting = bool(mine) or bool(last) or not requested
    idle_polls = 0
    try:
        client = _client(args, timeout=WAIT_POLL_MAX_S + 15)
        while True:
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                timed_out = True
                break
            deadline = WAIT_POLL_MAX_S if remaining is None else max(
                1, min(WAIT_POLL_MAX_S, int(remaining)))
            poll = protocol.WaitArgs(job_ids=sorted(pending), mine=mine,
                                     last=last, deadline_s=deadline)
            before = time.monotonic()
            # `--timeout` is a wall-clock bound: a wedged daemon must still end
            # in exit 124, not in a socket error (C-15.4, C-17.3).
            budget = deadline + 15 if remaining is None else min(
                deadline + 15, max(1.0, remaining + 1.0))
            try:
                result = client.call("wait", _asdict(poll), timeout=budget)
            except ProtocolError:
                if timeout is not None and timeout - (time.monotonic() - started) <= 0:
                    timed_out = True
                    break
                raise
            jobs = {job_id: job for job_id, job in _jobs_from_wait(result).items()
                    if adopting or job_id in requested}
            progress = False
            for job_id, job in jobs.items():
                if job.get("state") in TERMINAL_STATES:
                    if job_id not in finished:
                        progress = True
                    finished[job_id] = job
                    pending.discard(job_id)
                elif job_id not in pending:
                    pending.add(job_id)
                    progress = True
            if not pending and (finished or jobs):
                break
            if not pending and not finished and not result.get("timeout"):
                note(f"{PROG} wait: nothing to wait for")
                return int(Exit.OK)
            idle_polls = 0 if progress else idle_polls + 1
            if time.monotonic() - before < 0.2:
                # A daemon whose long poll returns at once must not be polled
                # five times a second; back off instead (C-15.4, C-16.4).
                pause = min(WAIT_BACKOFF_MAX_S, 0.2 * (2 ** min(idle_polls, 5)))
                if remaining is not None:
                    pause = min(pause, max(0.0, remaining))
                time.sleep(pause)
    except DaemonUnavailable as exc:
        return _daemon_down(exc)
    except DaemonError as exc:
        return _daemon_error(exc)
    except ProtocolError as exc:
        return fail(exc.code, str(exc))

    worst = int(Exit.OK)
    as_json = quiet or bool(getattr(args, "json", False))
    for job_id, job in sorted(finished.items()):
        if as_json:
            emit(job)
        else:
            note(_wait_summary(job))
        worst = max(worst, exit_for_job(job, quiet=as_json))
    elapsed = time.monotonic() - started
    for job_id in sorted(pending):
        if as_json:
            emit({"job_id": job_id, "state": "running", "timeout": True,
                  "waited_s": round(elapsed, 1)})
        else:
            note(f"{PROG} wait: {job_id} still running after {elapsed:.0f}s (timeout)")
        worst = max(worst, int(Exit.WAIT_TIMEOUT))
    if timed_out and not pending and not finished:
        # --mine and --last never seed `pending`, so a deadline that expires
        # before the daemon names a job is still a timeout, not success.
        if as_json:
            emit({"state": "running", "timeout": True, "waited_s": round(elapsed, 1)})
        else:
            note(f"{PROG} wait: nothing reached a terminal state in "
                 f"{elapsed:.0f}s (timeout)")
        worst = max(worst, int(Exit.WAIT_TIMEOUT))
    return worst


def cmd_wait(args: argparse.Namespace) -> int:
    ids = list(args.ids)
    mine = None
    if args.mine:
        mine = session_id()
        if mine is None:
            return fail(Exit.INVALID_INPUT,
                        "wait: --mine needs CLAUDE_CODE_SESSION_ID "
                        "(run it from a Claude session)")
    if not ids and not args.mine and not args.last:
        return fail(Exit.INVALID_INPUT, "wait: name a job id, or use --mine or --last")
    if ids and not getattr(args, "json", False):
        note(f"{PROG} wait: waiting for {len(ids)} job(s): {' '.join(ids)}")
    return wait_jobs(args, ids, timeout=args.timeout, mine=mine, last=bool(args.last))


# --- runs (C-17.1, C-17.5) ----------------------------------------------------

def cmd_runs(args: argparse.Namespace) -> int:
    if args.runs_command == "show":
        return cmd_runs_show(args)
    if args.runs_command == "reap":
        return cmd_runs_reap(args)
    if args.last < 0:
        return fail(Exit.INVALID_INPUT, "runs: --last must be non-negative")
    mine = None
    if args.mine:
        mine = session_id()
        if mine is None:
            return fail(Exit.INVALID_INPUT,
                        "runs: --mine needs CLAUDE_CODE_SESSION_ID "
                        "(run it from a Claude session)")
    offline = False
    try:
        client = _client(args)
        result = client.call("list", _asdict(protocol.ListArgs(
            mine=mine, running=bool(args.running), last=args.last or None)))
        rows = rows_of(result.get("jobs") or result.get("rows"))
    except DaemonUnavailable:
        offline = True
        store = _offline(args)
        try:
            rows = store.list_jobs(session=mine, running=bool(args.running),
                                   last=args.last)
        except OfflineUnavailable as exc:
            return _daemon_down(exc)
        _note_schema(store)
    except DaemonError as exc:
        return _daemon_error(exc)
    except ProtocolError as exc:
        return fail(exc.code, str(exc))
    if args.json:
        for row in rows:
            emit(row)
        return int(Exit.OK)
    if offline:
        note(f"{PROG} runs: offline — read from the store; "
             f"live jobs are as of the last daemon write")
    out(format_runs(rows))
    return int(Exit.OK)


def _artifact_path(job: dict[str, Any], role: str) -> str | None:
    """The accepted attempt's artifact for `role` (C-8.2, C-4.3).

    A job that failed once and then succeeded has two rows for the same role;
    only the accepted attempt's is the result.
    """
    if isinstance(job.get("job"), dict):
        job = {**job, **job["job"]}
    artifacts = rows_of(job.get("artifacts"))
    accepted = job.get("accepted_attempt_id")
    for wanted in ((accepted,) if accepted else ()) + (None,):
        for artifact in artifacts:
            if artifact.get("role") != role:
                continue
            if wanted is None or artifact.get("attempt_id") == wanted:
                return artifact.get("path")
    return job.get(f"{role}_path")


def _cat(label: str, path: str | None, *, header: bool) -> int:
    """Copy one artifact to stdout; a missing one is an operational error."""
    if not path:
        note(f"{PROG} runs show: no {label} recorded")
        return int(Exit.OPERATIONAL)
    try:
        text = Path(path).read_text(errors="replace")
    except OSError as exc:
        note(f"{PROG} runs show: cannot read {label} at {path}: {exc}")
        return int(Exit.OPERATIONAL)
    if header:
        note(f"--- {label}: {path} ---")
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        out()
    return int(Exit.OK)


def _format_job(job: dict[str, Any]) -> str:
    row = job_row(job)
    lines = [
        f"job      {row['id']}",
        f"state    {row['state']}"
        + (f" · rc {row['rc']}" if isinstance(row['rc'], int) else "")
        + (f" · {row['outcome_class']}" if row.get("outcome_class") else ""),
        f"model    {row['model'] or '-'} on lane {row['lane'] or '-'}"
        + (f" ({row['attestation']})" if row.get("attestation") else ""),
        f"workdir  {row['workdir'] or '-'}",
    ]
    for key, label in (("request_id", "request"), ("task", "task"), ("tier", "tier"),
                       ("sandbox", "sandbox"), ("out_path", "-o"),
                       ("created_at", "created"), ("finished_at", "finished"),
                       ("export_error", "export error"),
                       ("cancel_requested_at", "cancel requested")):
        value = job.get(key)
        if value not in (None, ""):
            lines.append(f"{label:<8} {value}")
    attempts = rows_of(job.get("attempts"))
    if attempts:
        lines.append("attempts")
        for attempt in attempts:
            receipts = attempt.get("receipts") or {}
            receipt = ""
            if "exit" in receipts:
                receipt = (f" · exit.json rc={receipts['exit'].get('rc')}"
                           f" wall={receipts['exit'].get('wall_s')}")
            elif "start" in receipts:
                receipt = f" · start.json pgid={receipts['start'].get('pgid')}"
            lines.append(
                f"  a{attempt.get('seq')} {attempt.get('state')} "
                f"lane={attempt.get('lane_id')} model={attempt.get('model_requested')}"
                + receipt
                + (f" rc={attempt.get('rc')}" if attempt.get("rc") is not None else "")
                + (f" class={attempt.get('outcome_class')}"
                   if attempt.get("outcome_class") else "")
                + (f" quarantine={attempt.get('quarantine_reason')}"
                   if attempt.get("quarantine_reason") else ""))
    artifacts = rows_of(job.get("artifacts"))
    if artifacts:
        lines.append("artifacts")
        for artifact in artifacts:
            lines.append(f"  {str(artifact.get('role') or '-'):<12} "
                         f"{artifact.get('path')} ({artifact.get('bytes')} bytes)")
    for notice in rows_of(job.get("notices")):
        lines.append(f"notice   [{notice.get('state')}] {notice.get('text')}")
    return "\n".join(lines)


def _ack_notices(client: Client, job: dict[str, Any]) -> None:
    """C-15.3 a notice is acknowledged when its session runs `runs show <job>`.

    Best effort: the job was already shown, so a failed acknowledgement must not
    change what the caller sees or the exit code.
    """
    session = session_id()
    if not session:
        return
    ids = [notice.get("notice_id") for notice in rows_of(job.get("notices"))
           if notice.get("session_id") == session
           and notice.get("state") != "acknowledged"
           and isinstance(notice.get("notice_id"), int)]
    if not ids:
        return
    try:
        client.call("notice.ack", _asdict(protocol.NoticeArgs(
            session_id=session, notice_ids=sorted(ids))))
    except (DaemonUnavailable, DaemonError, ProtocolError):
        pass


def cmd_runs_show(args: argparse.Namespace) -> int:
    if args.json and (args.out or args.err):
        return fail(Exit.INVALID_INPUT,
                    "runs show: --json is the metadata object; it cannot also "
                    "stream an artifact",
                    "run them separately: `runs show <id> --json` and "
                    "`runs show <id> --out`")
    try:
        client = _client(args)
        job = client.call("show", _asdict(protocol.ShowArgs(job_id=args.id)))
        _ack_notices(client, job)
    except DaemonUnavailable:
        store = _offline(args)
        try:
            job = store.show_job(args.id)
        except OfflineUnavailable as exc:
            return _daemon_down(exc)
        except LookupError as exc:
            return fail(Exit.INVALID_INPUT, f"runs show: {exc}")
        _note_schema(store)
    except DaemonError as exc:
        return _daemon_error(exc)
    except ProtocolError as exc:
        return fail(exc.code, str(exc))
    if not job:
        return fail(Exit.INVALID_INPUT, f"runs show: no job {args.id!r}")
    if args.json:
        emit(job)
        return int(Exit.OK)
    if args.out or args.err:
        both = args.out and args.err
        worst = int(Exit.OK)
        if args.out:
            worst = max(worst, _cat(
                "deliverable",
                _artifact_path(job, "deliverable") or job.get("out_path"),
                header=both))
        if args.err:
            worst = max(worst, _cat("stderr", _artifact_path(job, "stderr"),
                                    header=both))
        return worst
    # C-17.1: the bare form keeps v1's shape, which agents parse today: the
    # metadata object, then `--- out.md ---` and the deliverable, and with
    # `--err` also `--- err.log ---`. `--out`, `--err` alone, and `--json`
    # are v2's single-artifact forms.
    emit(job)
    out("\n--- out.md ---")
    path = _artifact_path(job, "deliverable") or job.get("out_path")
    if path and Path(path).is_file():
        # v1 printed whatever out.md held, or nothing, and still exited 0; the
        # explicit `--out` form is the one that fails on a missing artifact.
        text = Path(path).read_text(errors="replace")
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            out()
    return int(Exit.OK)


def _same_process():
    """`procs.same_process` when the core lane has landed it, else the client's."""
    try:
        from . import procs                                # noqa: PLC0415
    except ImportError:
        return same_process, "subfleet.client"
    checker = getattr(procs, "same_process", None)
    if checker is None:
        return same_process, "subfleet.client"
    return checker, "subfleet.procs"


def cmd_runs_reap(args: argparse.Namespace) -> int:
    """Report jobs whose recorded runner is provably gone (C-4.2, C-5.3).

    The store is the daemon's to write (C-3.4), so `reap` names the orphans and
    the command that finalizes them rather than editing rows behind the daemon.
    """
    checker, source = _same_process()
    daemon_up = True
    try:
        _client(args).call("daemon.status", {})
    except (DaemonUnavailable, ProtocolError):
        daemon_up = False
    except DaemonError:
        pass                              # it answered, so it is there
    try:
        rows = _offline(args).list_jobs(running=True, last=500)
    except OfflineUnavailable as exc:
        if daemon_up:
            return fail(Exit.OPERATIONAL,
                        f"runs reap: the daemon is running but its store is not "
                        f"readable from here: {exc}")
        return _daemon_down(exc)
    orphans: list[dict[str, Any]] = []
    for row in rows:
        pid = row.get("guardian_pid")
        alive = checker(pid, row.get("boot_id"), row.get("proc_start")) if pid else None
        if alive is False:
            orphans.append({"job_id": row.get("job_id"), "state": row.get("state"),
                            "attempt_id": row.get("attempt_id"), "pid": pid,
                            "verdict": "runner is gone"})
        elif pid is None:
            orphans.append({"job_id": row.get("job_id"), "state": row.get("state"),
                            "attempt_id": row.get("attempt_id"), "pid": None,
                            "verdict": "no runner recorded yet"})
    if args.json:
        for orphan in orphans:
            emit(orphan)
        return int(Exit.OK)
    note(f"{PROG} runs reap: identity checked with {source}; "
         f"{len(rows)} live job(s), {len(orphans)} orphan(s)")
    for orphan in orphans:
        out(f"{orphan['job_id']} {orphan['state']} {orphan['verdict']}"
            + (f" (pid {orphan['pid']})" if orphan["pid"] else ""))
    if orphans:
        note(f"  the daemon owns finalization: "
             + ("it reconciles these on its next pass" if daemon_up
                else f"start it with `{START_DAEMON}`"))
    return int(Exit.OK)


# --- kill (C-7.1, C-17.5) -----------------------------------------------------

def cmd_kill(args: argparse.Namespace) -> int:
    if args.confirm_dead and args.force_release:
        return fail(Exit.INVALID_INPUT,
                    "kill: --confirm-dead and --force-release are exclusive")
    worst = int(Exit.OK)
    killed: list[str] = []
    for job_id in args.ids:
        try:
            client = _client(args)
            result = client.call("kill", _asdict(protocol.KillArgs(
                job_id=job_id, confirm_dead=bool(args.confirm_dead),
                force_release=bool(args.force_release),
                operator_note=args.note)))
            killed.append(job_id)
            if args.json:
                emit({"job_id": job_id, **result})
            else:
                out(f"{job_id} {result.get('status') or result.get('action') or 'cancel requested'}")
                if result.get("detail"):
                    note(f"  {result['detail']}")
        except DaemonUnavailable as exc:
            if args.confirm_dead or args.force_release:
                # Both resolutions release leases and record an event, and only
                # the daemon writes rows (C-3.4, C-5.7).
                worst = max(worst, fail(
                    Exit.DAEMON_UNAVAILABLE,
                    f"kill: --{'confirm-dead' if args.confirm_dead else 'force-release'}"
                    f" releases leases and records an event, which only the daemon"
                    f" does ({exc})", START_DAEMON))
                continue
            try:
                result = _offline(args).kill(job_id)
            except OfflineUnavailable as exc:
                worst = max(worst, _daemon_down(exc))
                continue
            except SchemaTooNew as exc:
                worst = max(worst, fail(exc.code, f"kill: {exc}", exc.fix))
                continue
            except LookupError as exc:
                worst = max(worst, fail(Exit.INVALID_INPUT, f"kill: {exc}"))
                continue
            if args.json:
                emit(result)
            else:
                out(f"{job_id} {result['action']}")
                note(f"  offline: {result['reason']}")
            if result["action"] in {"refused", "failed"}:
                worst = max(worst, int(Exit.OPERATIONAL))
        except DaemonError as exc:
            worst = max(worst, _daemon_error(exc))
        except ProtocolError as exc:
            worst = max(worst, fail(exc.code, str(exc)))
    if args.wait and killed:
        return max(worst, wait_jobs(args, killed, timeout=args.timeout))
    return worst


# --- resume (C-17.1) ----------------------------------------------------------

RESUME_PROMPT = ("Reinspect the workspace and continue the work from where the "
                 "previous attempt stopped.")


def _source_lane(source: dict[str, Any]) -> str | None:
    """The lane that owns the source job's provider-side thread (C-12.1).

    A resume is only a resume on the lane that holds the thread or session, and
    the lane id may reach the CLI on the job row, on the accepted attempt, or on
    the last attempt, depending on what the daemon put in `show`.
    """
    accepted = source.get("accepted_attempt_id")
    attempts = rows_of(source.get("attempts"))
    for attempt in attempts:
        if accepted and attempt.get("attempt_id") == accepted and attempt.get("lane_id"):
            return attempt["lane_id"]
    direct = _first(source, "pinned_lane", "lane_id", "lane")
    if direct:
        return str(direct)
    for attempt in reversed(attempts):
        if attempt.get("lane_id"):
            return attempt["lane_id"]
    return None


def cmd_resume(args: argparse.Namespace) -> int:
    """Continue a job on the lane that owns its provider-side thread."""
    try:
        client = _client(args)
        source = client.call("show", _asdict(protocol.ShowArgs(job_id=args.id)))
    except DaemonUnavailable as exc:
        return _daemon_down(exc)
    except DaemonError as exc:
        return _daemon_error(exc)
    except ProtocolError as exc:
        return fail(exc.code, str(exc))
    if not source:
        return fail(Exit.INVALID_INPUT, f"resume: no job {args.id!r}")
    # The socket's show response separates the job row from its attempts.
    # Keep the legacy flat shape for older daemons, but never substitute the
    # CLI's cwd when the real job/worktree was inside the envelope.
    if isinstance(source.get("job"), dict):
        source = {**source, **source["job"]}
    exclusions = source.get("exclusions", source.get("exclusions_json", []))
    if isinstance(exclusions, str):
        try:
            exclusions = json.loads(exclusions)
        except (TypeError, ValueError):
            return fail(Exit.OPERATIONAL, "resume: source exclusions are malformed")
    if not isinstance(exclusions, list) or any(not isinstance(item, str) for item in exclusions):
        return fail(Exit.OPERATIONAL, "resume: source exclusions are malformed")
    request_id = args.request_id or str(uuid.uuid4())
    root = _root(args)
    try:
        prompt_path = stage_prompt(args.prompt or RESUME_PROMPT, request_id, root)
    except OSError as exc:
        return fail(Exit.OPERATIONAL, f"resume: cannot stage the prompt: {exc}")
    submit = protocol.SubmitArgs(
        request_id=request_id,
        kind="resume",
        workdir=source.get("worktree") or source.get("workdir") or os.getcwd(),
        prompt_path=str(prompt_path),
        sandbox=source.get("sandbox") or Sandbox.READ_ONLY.value,
        task=source.get("task"),
        tier=source.get("tier"),
        pinned_model=source.get("pinned_model"),
        exclusions=exclusions,
        pinned_lane=_source_lane(source),
        out_path=str(Path(args.output).expanduser().absolute()) if args.output
                 else source.get("out_path"),
        name=source.get("name"),
        parent_job_id=args.id,
        independent=True,  # a continuation may follow an explicitly cancelled job
        caller_session=session_id(),
        caller_pid=caller_pid(),
    )
    try:
        result = client.call("submit", _asdict(submit), request_id=request_id)
    except DaemonUnavailable as exc:
        return _daemon_down(exc)
    except DaemonError as exc:
        return _daemon_error(exc)
    except ProtocolError as exc:
        return fail(exc.code, str(exc))
    job_id = result.get("job_id") or ""
    if not job_id:
        return fail(Exit.OPERATIONAL, "resume: the daemon returned no job id")
    if args.json:
        emit({"job_id": job_id, "run_id": job_id, "request_id": request_id,
              "resumed_from": args.id, "created": bool(result.get("created", True))})
    else:
        out(job_id)
        note(f"{PROG} resume: {job_id} continues {args.id} on lane "
             f"{submit.pinned_lane or '-'}")
    return int(Exit.OK)


# --- lanes, why, ping ---------------------------------------------------------

def _format_lanes(result: dict[str, Any]) -> str:
    if result.get("lanes") is None:
        return json.dumps(result, indent=1, sort_keys=True, default=str)
    lanes = rows_of(result.get("lanes"))
    if not lanes:
        return "no lanes enrolled — subfleet lanes enroll <credential>"
    lines = [f"{'lane':<12} {'provider':<8} {'account':<30} {'owner':<6} "
             f"{'desktop':<8} {'enabled':<8} {'identity':<12} plan"]
    for lane in lanes:
        lines.append(
            f"{str(lane.get('lane_id') or '-'):<12.12} "
            f"{str(lane.get('provider') or '-'):<8.8} "
            f"{str(lane.get('account_key') or '-'):<30.30} "
            f"{str(lane.get('owner') or '-'):<6.6} "
            f"{('yes' if lane.get('desktop') else 'no'):<8} "
            f"{('yes' if lane.get('enabled', True) else 'no'):<8} "
            f"{str(lane.get('identity_status') or '-'):<12.12} "     # C-10.6
            f"{lane.get('plan') or '-'}")
    return "\n".join(lines)


def _format_transfer(result: dict[str, Any]) -> str:
    """Plan amendment 8: one transfer, both rosters, and what is left to do."""
    lines = [f"{result.get('lane_id')}: {result.get('from')} -> {result.get('to')}"
             + ("  (dry run, nothing written)" if result.get("dry_run") else "")]
    if not result.get("changed"):
        lines.append("already owned by " + str(result.get("to")))
    for edit in result.get("edits") or []:
        mark = "edited" if edit.get("changed") else "unchanged"
        if result.get("dry_run") and edit.get("changed"):
            mark = "would edit"
        lines.append(f"  {mark}: {edit.get('path')}"
                     + (f"  (backup {edit.get('backup')})" if edit.get("backup") else ""))
    if result.get("diff"):
        lines.append(result["diff"].rstrip("\n"))
    for item in result.get("follow_up") or []:
        lines.append(f"  next: {item}")
    if result.get("blocker"):
        lines.append(f"  blocked: {result['blocker']}")
    return "\n".join(lines)


def cmd_lanes(args: argparse.Namespace) -> int:
    action = args.lanes_command or "list"
    if action == "transfer" and args.to not in ("v1", "v2"):
        return fail(Exit.INVALID_INPUT, "lanes transfer: --to must be v1 or v2")
    if action == "hold" and not args.until:
        return fail(Exit.INVALID_INPUT, "lanes hold: --until is required")
    lanes_args = protocol.LanesArgs(
        action=action,
        lane_id=getattr(args, "lane", None),
        credential=getattr(args, "credential", None),
        until=getattr(args, "until", None),
        owner=getattr(args, "to", None),
        dry_run=bool(getattr(args, "dry_run", False)),
        confirm_v1_edit=bool(getattr(args, "confirm_v1_edit", False)),
    )
    try:
        result = _client(args).call("lanes", _asdict(lanes_args))
    except DaemonUnavailable as exc:
        return _daemon_down(exc)
    except DaemonError as exc:
        return _daemon_error(exc)
    except ProtocolError as exc:
        return fail(exc.code, str(exc))
    if action == "transfer" and not isinstance(result.get("transfer"), dict):
        # C-16.2 ignores unknown request fields, so a daemon older than this verb
        # answers the `lanes` op with the roster and no transfer at all. Printing
        # that as a completed no-op would record a canary transfer that never ran.
        return fail(Exit.DAEMON_UNAVAILABLE,
                    "the daemon did not perform the transfer; it is older than this CLI",
                    "subfleet daemon stop && subfleet daemon start")
    if args.json:
        emit(result)
        return int(Exit.OK)
    if action == "transfer":
        out(_format_transfer(result["transfer"]))
        return int(Exit.OK)
    if action == "enroll":
        row = result.get("enrolled") or {}
        if not row:
            return fail(Exit.DAEMON_UNAVAILABLE, "the daemon did not enroll the lane; it is older than this CLI",
                        "subfleet daemon stop && subfleet daemon start")
        out(f"{row.get('lane_id')}  {row.get('provider')}  {row.get('account_key')}  owner={row.get('owner')}"
            f"  label={row.get('label') or '-'}  home={row.get('home') or '-'}")
        return int(Exit.OK)
    if action in ("hold", "release"):
        if not (result.get("held") or result.get("released")):
            return fail(Exit.DAEMON_UNAVAILABLE, f"the daemon did not {action} the lane; it is older than this CLI",
                        "subfleet daemon stop && subfleet daemon start")
        out(f"{action}: {result.get('held') or result.get('released')}")
        return int(Exit.OK)
    out(_format_lanes(result))
    return int(Exit.OK)


def cmd_why(args: argparse.Namespace) -> int:
    if not args.id and not args.task:
        return fail(Exit.INVALID_INPUT, "why: name a job id, or --task T --tier X")
    if args.task and not args.tier:
        return fail(Exit.INVALID_INPUT, "why: --tier is required with --task")
    why = protocol.WhyArgs(job_id=args.id, task=args.task, tier=args.tier,
                           pinned_model=args.m, exclusions=list(args.exclude or []),
                           allow_desktop=bool(args.allow_desktop))
    try:
        result = _client(args).call("why", _asdict(why))
    except DaemonUnavailable as exc:
        return _daemon_down(exc)
    except DaemonError as exc:
        return _daemon_error(exc)
    except ProtocolError as exc:
        return fail(exc.code, str(exc))
    if args.json:
        emit(result)
        return int(Exit.OK)
    out(_format_decision(result.get("decision", result)))
    return int(Exit.OK)


def cmd_ping(args: argparse.Namespace) -> int:
    target = args.session or session_id()
    if not target:
        return fail(Exit.INVALID_INPUT,
                    "ping: --session ID is required outside a Claude session")
    if args.text:
        text = " ".join(args.text)
    else:
        try:
            text = sys.stdin.buffer.read().decode("utf-8", "replace")
        except (OSError, ValueError) as exc:
            return fail(Exit.INVALID_INPUT, f"ping: cannot read the message: {exc}")
    try:
        result = _client(args).call(
            "ping", _asdict(protocol.PingArgs(text=text, session_id=target)))
    except DaemonUnavailable as exc:
        return _daemon_down(exc)
    except DaemonError as exc:
        return _daemon_error(exc)
    except ProtocolError as exc:
        return fail(exc.code, str(exc))
    if args.json:
        emit(result)
        return int(Exit.OK)
    if result.get("delivered"):
        out(f"delivered to {result.get('name') or target}")
        return int(Exit.OK)
    out(f"parked for {result.get('name') or target}")
    note(f"  {result.get('reason') or 'the session has no live inbox'}")
    return int(Exit.OK)


# --- daemon control (C-5.8) ---------------------------------------------------

def _daemond_argv(root: Path) -> list[str]:
    """How to start `subfleetd`, honouring an explicit override for tests."""
    override = (os.environ.get("SUBFLEET_DAEMON_BIN") or "").strip()
    if override:
        return [override, "--state-root", str(root)]
    # `-E -P`: the daemon must import this package whatever PYTHON* variables the
    # installing shell inherited (v1's wrapper exports PYTHONPATH=<v1 checkout>
    # to everything it launches; decisions memo 2026-09-05 §3). A console script
    # found on PATH or beside the interpreter is run through the interpreter for
    # the same reason instead of being exec'd on its own shebang.
    isolated = [sys.executable, "-E", "-P"]
    found = shutil.which("subfleetd")
    if found:
        return [*isolated, found, "--state-root", str(root)]
    sibling = Path(sys.executable).parent / "subfleetd"
    if sibling.exists():
        return [*isolated, str(sibling), "--state-root", str(root)]
    return [*isolated, "-m", "subfleet.daemon", "--state-root", str(root)]


# The daemon outlives the shell that starts it, and by C-5.1 every guardian and
# provider child inherits its environment. An API key or a session id picked up
# from one terminal must not become the fleet's ambient environment (C-14.4).
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "CODEX_API_KEY", "OPENAI_API_KEY",
                "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
                "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PID", "SUBFLEET_RUN_DETACH")


def daemon_env(root: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
    env["SUBFLEET_HOME"] = str(root)
    return env


def _log_tail(path: Path, lines: int = 20) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return f"(no {path})"
    tail = text.splitlines()[-lines:]
    return "\n".join(f"  | {line}" for line in tail) or f"(empty {path})"


def _daemon_alive(client: Client) -> bool:
    try:
        client.call("daemon.status", {}, timeout=3.0)
    except (DaemonUnavailable, ProtocolError):
        return False
    except DaemonError:
        return True                      # it answered, so it is there
    return True


def cmd_daemon_start(args: argparse.Namespace) -> int:
    root = _root(args)
    client = Client(root)
    if _daemon_alive(client):
        info = client.lock_info() or {}
        note(f"{PROG} daemon: already running"
             + (f" (pid {info.get('pid')})" if info.get("pid") else ""))
        return int(Exit.OK)
    try:
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
    except OSError as exc:
        return fail(Exit.OPERATIONAL, f"daemon start: cannot create {root}: {exc}")
    argv = _daemond_argv(root)
    log_path = root / LOG_NAME
    try:
        log = open(log_path, "ab")
    except OSError as exc:
        return fail(Exit.OPERATIONAL, f"daemon start: cannot open {log_path}: {exc}")
    try:
        with open(os.devnull, "rb") as devnull:
            child = subprocess.Popen(argv, stdin=devnull, stdout=log, stderr=log,
                                     start_new_session=True, cwd=str(root),
                                     env=daemon_env(root))
    except OSError as exc:
        log.close()
        return fail(Exit.OPERATIONAL,
                    f"daemon start: cannot launch {' '.join(argv)}: {exc}")
    log.close()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _daemon_alive(Client(root)):
            info = Client(root).lock_info() or {}
            out(str(info.get("pid") or child.pid))
            note(f"{PROG} daemon: started (pid {info.get('pid') or child.pid}), "
                 f"socket {root / SOCKET_NAME}")
            return int(Exit.OK)
        if child.poll() not in (None, 0):
            break                        # a real failure; a double fork exits 0
        time.sleep(0.05)
    note(f"{PROG} daemon start: {root / SOCKET_NAME} did not appear within 10s"
         + (f"; {' '.join(argv)} exited {child.returncode}"
            if child.poll() not in (None, 0) else ""))
    note(_log_tail(log_path))
    return int(Exit.DAEMON_UNAVAILABLE)


def cmd_daemon_stop(args: argparse.Namespace) -> int:
    import signal as _signal
    root = _root(args)
    client = Client(root)
    info = client.lock_info()
    if info is None:
        note(f"{PROG} daemon: not running (no {client.lock_path})")
        return int(Exit.OK)
    pid = info.get("pid")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return fail(Exit.OPERATIONAL,
                    f"daemon stop: {client.lock_path} records no usable pid")
    # One snapshot: re-reading the lock could verify a NEW daemon's identity and
    # then signal the pid from the old read (C-5.4).
    alive = same_process(pid, info.get("boot_id"), info.get("proc_start"))
    if alive is False:
        note(f"{PROG} daemon: not running (stale lock for pid {pid})")
        return int(Exit.OK)
    if alive is None:
        return fail(Exit.OPERATIONAL,
                    f"daemon stop: cannot verify that pid {pid} is the recorded "
                    f"daemon (C-5.3); refusing to signal it")
    try:
        os.kill(pid, _signal.SIGTERM)
    except OSError as exc:
        return fail(Exit.OPERATIONAL, f"daemon stop: SIGTERM to {pid} failed: {exc}")
    note(f"{PROG} daemon: SIGTERM sent to pid {pid}")
    # Wait on the identity we signalled, not on daemon.lock: a daemon that
    # cleans up removes the lock, and a missing lock is not evidence of an exit.
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if same_process(pid, info.get("boot_id"), info.get("proc_start")) is False:
            note(f"{PROG} daemon: stopped")
            return int(Exit.OK)
        time.sleep(0.1)
    note(f"{PROG} daemon: pid {pid} has not exited after 15s")
    return int(Exit.OPERATIONAL)


def cmd_daemon_status(args: argparse.Namespace) -> int:
    root = _root(args)
    client = Client(root)
    info = client.lock_info()
    alive = client.lock_holder_alive()
    started = time.monotonic()
    reachable, detail = False, ""
    try:
        client.call("daemon.status", {}, timeout=5.0)
        reachable = True
    except (DaemonUnavailable, DaemonError, ProtocolError) as exc:
        detail = str(exc)
    elapsed_ms = (time.monotonic() - started) * 1000
    payload = {"state_root": str(root), "socket": str(client.socket_path),
               "socket_present": client.socket_path.exists(), "lock": info,
               "lock_holder_alive": alive, "ping": reachable,
               "ping_ms": round(elapsed_ms, 1), "detail": detail or None}
    if args.json:
        emit(payload)
        return int(Exit.OK) if reachable else int(Exit.DAEMON_UNAVAILABLE)
    out(f"state root  {root}")
    out(f"socket      {client.socket_path}"
        f"  ({'present' if client.socket_path.exists() else 'absent'})")
    if info is None:
        out(f"lock        absent ({client.lock_path})")
    else:
        out(f"lock        {json.dumps(info, sort_keys=True)}")
        out(f"holder      {'alive' if alive else ('dead' if alive is False else 'unverifiable')}")
    out(f"ping        {'ok' if reachable else 'unreachable'} ({elapsed_ms:.1f} ms)")
    if detail:
        note(f"  {detail}")
    return int(Exit.OK) if reachable else int(Exit.DAEMON_UNAVAILABLE)


def cmd_daemon_logs(args: argparse.Namespace) -> int:
    path = _root(args) / LOG_NAME
    if not path.exists():
        return fail(Exit.OPERATIONAL, f"daemon logs: no {path}")
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        return fail(Exit.OPERATIONAL, f"daemon logs: {exc}")
    if args.lines < 0:
        return fail(Exit.INVALID_INPUT, "daemon logs: --lines must be non-negative")
    for line in (text.splitlines()[-args.lines:] if args.lines else []):
        out(line)
    if not args.follow:
        return int(Exit.OK)
    with open(path, "r", errors="replace") as stream:
        stream.seek(0, os.SEEK_END)
        try:
            while True:
                line = stream.readline()
                if line:
                    out(line.rstrip("\n"))
                else:
                    time.sleep(0.25)
        except KeyboardInterrupt:
            return int(Exit.OK)


def _plist(root: Path) -> bytes:
    argv = _daemond_argv(root)
    return plistlib.dumps({
        "Label": PLIST_LABEL,
        "ProgramArguments": argv,
        "EnvironmentVariables": {"SUBFLEET_HOME": str(root),
                                 "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(root),
        "StandardOutPath": str(root / LOG_NAME),
        "StandardErrorPath": str(root / LOG_NAME),
        "ProcessType": "Background",
    }, sort_keys=True)


def cmd_daemon_install_hooks(args: argparse.Namespace) -> int:
    """`daemon install --hooks`: the three Claude Code hook entries (C-15.2).

    The diff is printed first, always — with `--dry-run` instead of writing and
    without it before writing. `~/.claude/settings.json` is a file the user
    edits by hand and that v1's own hooks live in, so no version of this command
    changes it silently, and v1's entries are reported but never rewritten.
    """
    from . import hooks
    report = hooks.plan()
    if not report.get("ok"):
        return fail(Exit.OPERATIONAL,
                    f"daemon install --hooks: {report['path']}: {report['error']}",
                    f"fix or move {report['path']} and try again")
    v1 = report.get("v1_entries") or {}
    if v1:
        note(f"{PROG} daemon install --hooks: v1 bin/subfleet-hook entries for "
             f"{', '.join(sorted(v1))} are left in place (v1 still owns the "
             f"PreToolUse front-door guard)")
    if not report["changed_events"]:
        note(f"{PROG} daemon install --hooks: {report['path']} already matches; "
             f"nothing to write")
        return int(Exit.OK)
    sys.stdout.write(report["diff"])
    if args.dry_run:
        note(f"{PROG} daemon install --hooks --dry-run: would write "
             f"{report['path']} ({', '.join(report['changed_events'])})")
        return int(Exit.OK)
    written = hooks.apply()
    if not written.get("written"):
        return fail(Exit.OPERATIONAL,
                    f"daemon install --hooks: {written.get('error', 'nothing written')}")
    out(written["path"])
    note(f"{PROG} daemon install --hooks: wrote {written['path']}"
         + (f" (backup {written['backup']})" if written.get("backup") else ""))
    return int(Exit.OK)


def cmd_daemon_install(args: argparse.Namespace) -> int:
    if getattr(args, "hooks", False):
        return cmd_daemon_install_hooks(args)
    root = _root(args)
    already_running = _daemon_alive(Client(root))
    plist = _plist(root)
    target = Path(PLIST_PATH).expanduser()
    if args.dry_run:
        sys.stdout.write(plist.decode())
        note(f"{PROG} daemon install --dry-run: would write {target} and load it")
        return int(Exit.OK)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(plist)
    except OSError as exc:
        return fail(Exit.OPERATIONAL, f"daemon install: cannot write {target}: {exc}")
    out(str(target))
    subprocess.run(["launchctl", "unload", str(target)],
                   capture_output=True, text=True, check=False)
    loaded = subprocess.run(["launchctl", "load", "-w", str(target)],
                            capture_output=True, text=True, check=False)
    if loaded.returncode != 0:
        return fail(Exit.OPERATIONAL,
                    f"daemon install: launchctl load failed: "
                    f"{(loaded.stderr or loaded.stdout).strip()}")
    note(f"{PROG} daemon install: loaded {PLIST_LABEL} (KeepAlive, RunAtLoad)")
    if already_running:
        note(f"  a daemon was already running; launchd owns the next one — "
             f"`{PROG} daemon stop` when you want it to take over")
        return int(Exit.OK)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _daemon_alive(Client(root)):
            note(f"  socket {root / SOCKET_NAME} is up")
            return int(Exit.OK)
        time.sleep(0.05)
    note(f"{PROG} daemon install: {PLIST_LABEL} is loaded but "
         f"{root / SOCKET_NAME} did not appear within 10s")
    note(_log_tail(root / LOG_NAME))
    return int(Exit.DAEMON_UNAVAILABLE)


def cmd_daemon(args: argparse.Namespace) -> int:
    return {
        "start": cmd_daemon_start, "stop": cmd_daemon_stop,
        "status": cmd_daemon_status, "logs": cmd_daemon_logs,
        "install": cmd_daemon_install,
    }[args.daemon_command or "status"](args)


# --- hook (C-15.2) ------------------------------------------------------------

def cmd_hook(args: argparse.Namespace) -> int:
    """`subfleet hook <event>`: one Claude Code hook invocation, JSON on stdin.

    The exit codes here are the harness's, not C-17.3's: this verb is not called
    by a person, it is called by Claude Code, which reads 0 and 2 as "nothing to
    say" and "show this to Claude" (`docs/reference/claude-hooks.md` section 3).
    `subfleet/hooks.py` documents which event gets which and why.
    """
    from . import hooks
    return hooks.run(args.event, root=_root(args))


# --- doctor (C-17.1) ----------------------------------------------------------

def doctor_checks(root: Path, *,
                  live: bool = False,
                  claude_settings: Path | None = None) -> list[dict[str, Any]]:
    """The one check table, owned by `subfleet/doctor.py`.

    Each row is `{check, status, detail, fix}` with `status` one of `pass`,
    `fail`, `unknown` — `unknown` meaning the check could not look, which is
    never reported as a pass and never decides the exit code.
    """
    from . import doctor
    return doctor.checks(root, live=live, settings=claude_settings)


def _lane_rows(root: Path) -> list[dict[str, Any]] | None:
    """Lane rows without a daemon, or None when the store cannot be read."""
    try:
        return Offline(root).lanes()
    except (OfflineUnavailable, SchemaTooNew, OSError, ValueError):
        return None


def _identity_roster_check(root: Path) -> dict[str, Any]:
    """C-10.7, C-23.45: at most one enabled lane may hold an account.

    Identity, not email, is what joins two credentials to one account: a desktop
    credential and an enrolled inference token are distinct lanes even when their
    labels match, and two lanes that share an identity are double-counting one
    account's quota.
    """
    check = "one identity per enabled lane"
    lanes = _lane_rows(root)
    if lanes is None:
        return {"check": check, "status": "pass",
                "detail": "no readable store yet (the daemon has not run here)"}
    shared: dict[str, list[str]] = {}
    for lane in lanes:
        identity = lane.get("identity")
        if identity and lane.get("enabled", 1):
            shared.setdefault(str(identity), []).append(str(lane.get("lane_id")))
    clashes = {identity: ids for identity, ids in shared.items() if len(ids) > 1}
    mismatched = [str(lane.get("lane_id")) for lane in lanes
                  if lane.get("identity_status") == "mismatch"]
    if clashes:
        detail = "; ".join(f"{identity} is held by {', '.join(sorted(ids))}"
                           for identity, ids in sorted(clashes.items()))
        return {"check": check, "status": "fail",
                "detail": f"{detail} — disable all but one: "
                          f"{PROG} lanes transfer <lane> --to v1 (C-10.7)"}
    bound = sum(1 for lane in lanes if lane.get("identity"))
    unbound = [str(lane.get("lane_id")) for lane in lanes
               if lane.get("provider") == "claude" and not lane.get("identity")
               and not lane.get("label")]
    detail = f"{bound} bound, {len(lanes)} lanes"
    if mismatched:
        return {"check": check, "status": "unknown",
                "detail": f"{detail}; not a candidate until re-enrolled: "
                          f"{', '.join(sorted(mismatched))} — "
                          f"{PROG} lanes enroll <credential> (C-10.6)"}
    if unbound:
        return {"check": check, "status": "unknown",
                "detail": f"{detail}; no identity recorded, so no reading of "
                          f"theirs is capacity: {', '.join(sorted(unbound))} — "
                          f"{PROG} lanes enroll <credential> (C-10.6)"}
    return {"check": check, "status": "pass", "detail": detail}


# --- doctor --live: the checks that need the credential and the network -------

def live_checks(root: Path, *, claude_json: Path | None = None,
                profile: Callable[[], Any] | None = None) -> list[dict[str, Any]]:
    """C-10.3: does the cached desktop login agree with the credential itself?

    `~/.claude.json` says who the desktop app believes it is signed in as; the
    profile endpoint, asked with the desktop app's own keychain item, says who
    that credential actually belongs to. On 2026-09-05 those two disagreed and
    v1 believed the file, attributing one account's usage to another. This check
    exists to make that disagreement visible in words before it is believed.
    """
    checks: list[dict[str, Any]] = []
    cached = capacity.cached_desktop_identity(claude_json)
    cached_pair = (f"{cached.get('account_uuid')}:{cached.get('org_uuid')}"
                   if cached.get("account_uuid") and cached.get("org_uuid") else None)
    if profile is None:
        from .adapters.claude import ClaudeAdapter
        profile = ClaudeAdapter().probe_desktop_profile
    try:
        answer = profile()
    except Exception as exc:                     # noqa: BLE001 — a doctor never raises
        answer = None
        detail = f"the desktop credential could not be read ({type(exc).__name__})"
    else:
        detail = None
    observed = getattr(answer, "identity", None)
    observed_email = getattr(answer, "email", None)
    status_name = getattr(answer, "status", None)
    check = "cached ~/.claude.json agrees with the desktop credential"
    if detail is not None:
        row = {"status": "unknown", "detail": f"{detail} — unverified; "
                                           f"{PROG} doctor --live again once it is readable"}
    elif observed is None:
        row = {"status": "unknown",
               "detail": f"the profile endpoint did not answer ({status_name}); the "
                         f"cached login {cached.get('email') or 'unknown'} is "
                         f"unverified — lanes fall back to matching that label "
                         f"(C-10.3); retry when it answers"}
    elif not cached:
        row = {"status": "unknown",
               "detail": f"no cached oauthAccount to compare; the desktop "
                         f"credential belongs to {observed_email} ({observed})"}
    elif cached_pair == observed:
        row = {"status": "pass",
               "detail": f"both say {observed_email or cached.get('email')} ({observed})"}
    else:
        row = {"status": "fail",
               "detail": f"they disagree: ~/.claude.json says "
                         f"{cached.get('email')} ({cached_pair or 'no uuids'}) but the "
                         f"credential itself belongs to {observed_email} ({observed}) "
                         f"— trust the credential, and re-enrol any lane recorded "
                         f"under the cached identity: {PROG} lanes enroll "
                         f"claude-quota-{observed_email or '<email>'} (C-10.3, C-10.6)"}
    checks.append({"check": check, **row})
    return checks


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor
    checks = doctor_checks(_root(args), live=args.live)
    if args.json:
        for check in checks:
            emit(check)
    else:
        out(doctor.render(checks))
    return doctor.exit_code(checks)


# --- parser (C-17.1, C-17.2) --------------------------------------------------

def cmd_sessions(args: argparse.Namespace) -> int:
    """`subfleet sessions <verb>` (C-17.1); the kit's own module holds the verbs."""
    from .sessions import cli as sessions_cli
    return sessions_cli.dispatch(args)


def cmd_handoff(args: argparse.Namespace) -> int:
    """`subfleet handoff <session> --to <model>` (C-17.1, C-23.14, C-23.54)."""
    from .sessions import cli as sessions_cli
    return sessions_cli.cmd_handoff(args)


def _add_json(parser: argparse.ArgumentParser, *, nested: bool = False) -> None:
    """`--json`; nested parsers suppress their default so the parent's survives."""
    kwargs: dict[str, Any] = {"action": "store_true",
                              "help": "machine-readable: JSON objects only (C-17.4)"}
    if nested:
        kwargs["default"] = argparse.SUPPRESS
    parser.add_argument("--json", **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-V", "--version", action="store_true",
                        help="print the subfleet version")
    parser.set_defaults(handler=cmd_status, json=False)
    sub = parser.add_subparsers(dest="command")

    from .gate.cli import configure as configure_gate
    configure_gate(sub.add_parser("gate", help="main/peer agreement for an exact revision"))

    p_status = sub.add_parser("status", help="lanes, readings, closures, running jobs")
    _add_json(p_status)
    p_status.set_defaults(handler=cmd_status)

    p_run = sub.add_parser("run", help="submit a job to the daemon")
    task = p_run.add_mutually_exclusive_group()
    task.add_argument("--task", choices=TASK_CHOICES, help="what kind of work this is")
    task.add_argument("-t", dest="t", choices=("fable", "review", "build", "sweep"),
                      help=argparse.SUPPRESS)          # deprecated (C-17.2)
    p_run.add_argument("--tier", choices=TIER_CHOICES,
                       help="minimum capability for --task")
    p_run.add_argument("-m", dest="m", choices=MODEL_CHOICES,
                       help="pin one model; never falls back (sol is retired → astra)")
    pins = p_run.add_mutually_exclusive_group()
    pins.add_argument("-a", dest="a", metavar="EMAIL", help="pin a Claude lane account")
    pins.add_argument("-H", dest="H", metavar="CODEX_HOME", help="pin a Codex lane home")
    p_run.add_argument("-C", dest="C", default=os.getcwd(), metavar="DIR",
                       help="workdir (default: the current directory)")
    p_run.add_argument("-I", "--independent-review", dest="isolated_review", action="store_true")
    p_run.add_argument("-D", "--review-root", dest="review_root")
    p_run.add_argument("-o", dest="o", metavar="OUT", help="export the deliverable here")
    p_run.add_argument("-n", "--name", dest="name", metavar="NAME",
                       help="short label for the job id")
    p_run.add_argument("-s", dest="s", choices=SANDBOX_CHOICES, help="sandbox")
    p_run.add_argument("-x", "--exclude", action="append", default=[], metavar="EMAIL",
                       help="never pick this account (repeatable)")
    p_run.add_argument("--allow-desktop", action="store_true",
                       help="allow the desktop app's own login as a lane (C-10.3)")
    p_run.add_argument("--allow-tmp", action="store_true",
                       help="allow a workdir under /tmp (C-2.4)")
    p_run.add_argument("--in-place", action="store_true",
                       help="write in the caller's directory instead of a worktree")
    p_run.add_argument("--independent", action="store_true",
                       help="a parent's cancel does not cancel this child (C-7.3)")
    p_run.add_argument("--parent", metavar="JOB", help="parent job id")
    p_run.add_argument("--request-id", metavar="ID", type=ids.request_id,
                       help="idempotency key (default: a UUID4, printed back)")
    p_run.add_argument("--wait", "--attach", dest="attach", action="store_true",
                       help="block until the job is terminal and return its rc")
    p_run.add_argument("-d", "--detach", dest="detach", action="store_true",
                       help="return at once (the default inside a Claude session)")
    p_run.add_argument("--no-wait-queue", action="store_true",
                       help="with --json, exit 75 when the job is still queued")
    p_run.add_argument("--no-preamble", action="store_true",
                       help="do not prepend the workspace-write template (C-6.7)")
    p_run.add_argument("--dry-run", action="store_true",
                       help="evaluate routing and print the decision; dispatch nothing")
    p_run.add_argument("--why", action="store_true",
                       help="print the full routing decision; dispatch nothing (C-11.5)")
    p_run.add_argument("--overflow", action="store_true", help=argparse.SUPPRESS)
    _add_json(p_run)
    source = p_run.add_mutually_exclusive_group()
    source.add_argument("-p", dest="p", metavar="PROMPTFILE")
    source.add_argument("prompt", nargs="?")
    p_run.set_defaults(handler=cmd_run)

    p_runs = sub.add_parser("runs", help="the job ledger, newest first")
    p_runs.add_argument("--last", type=int, default=20, help="how many (default 20)")
    p_runs.add_argument("--mine", action="store_true",
                        help="only this session's jobs (CLAUDE_CODE_SESSION_ID)")
    p_runs.add_argument("--running", action="store_true", help="only unfinished jobs")
    _add_json(p_runs)
    p_runs.set_defaults(handler=cmd_runs, runs_command=None)
    runs_sub = p_runs.add_subparsers(dest="runs_command")
    p_show = runs_sub.add_parser("show", help="one job's metadata and artifacts")
    p_show.add_argument("id")
    p_show.add_argument("--out", action="store_true", help="print the deliverable")
    p_show.add_argument("--err", action="store_true", help="print the saved stderr")
    _add_json(p_show, nested=True)
    p_reap = runs_sub.add_parser("reap", help="reconcile jobs whose runner is gone")
    _add_json(p_reap, nested=True)

    p_wait = sub.add_parser("wait", help="long-poll until jobs are terminal")
    p_wait.add_argument("ids", nargs="*")
    p_wait.add_argument("--mine", action="store_true", help="this session's jobs")
    p_wait.add_argument("--last", action="store_true", help="the most recent job")
    p_wait.add_argument("--timeout", type=float, default=None,
                        help="seconds before giving up (exit 124)")
    _add_json(p_wait)
    p_wait.set_defaults(handler=cmd_wait)

    p_kill = sub.add_parser("kill", help="cancel a job and contain its process tree")
    p_kill.add_argument("ids", nargs="+")
    p_kill.add_argument("--wait", action="store_true", help="block until terminal")
    p_kill.add_argument("--timeout", type=float, default=None)
    resolution = p_kill.add_mutually_exclusive_group()
    resolution.add_argument("--confirm-dead", action="store_true",
                            help="re-run containment and release on verified empty (C-5.7)")
    resolution.add_argument("--force-release", action="store_true",
                            help="record an operator override and release (C-5.7)")
    p_kill.add_argument("--note", help="operator note recorded with the resolution")
    _add_json(p_kill)
    p_kill.set_defaults(handler=cmd_kill)

    p_resume = sub.add_parser("resume", help="continue a job on the lane that owns it")
    p_resume.add_argument("id")
    p_resume.add_argument("prompt", nargs="?")
    p_resume.add_argument("-o", "--output", help="also write the final response here")
    p_resume.add_argument("--request-id", metavar="ID", type=ids.request_id)
    _add_json(p_resume)
    p_resume.set_defaults(handler=cmd_resume)

    p_lanes = sub.add_parser("lanes", help="the lane roster and its health")
    _add_json(p_lanes)
    p_lanes.set_defaults(handler=cmd_lanes, lanes_command=None)
    lanes_sub = p_lanes.add_subparsers(dest="lanes_command")
    l_list = lanes_sub.add_parser("list")
    _add_json(l_list, nested=True)
    l_probe = lanes_sub.add_parser("probe")
    l_probe.add_argument("lane", nargs="?")
    _add_json(l_probe, nested=True)
    l_enroll = lanes_sub.add_parser("enroll")
    l_enroll.add_argument("credential")
    _add_json(l_enroll, nested=True)
    l_hold = lanes_sub.add_parser("hold")
    l_hold.add_argument("lane")
    l_hold.add_argument("--until", required=True, help="ISO 8601 UTC instant")
    _add_json(l_hold, nested=True)
    l_release = lanes_sub.add_parser("release")
    l_release.add_argument("lane")
    _add_json(l_release, nested=True)
    l_transfer = lanes_sub.add_parser("transfer")
    l_transfer.add_argument("lane")
    l_transfer.add_argument("--to", required=True, choices=("v1", "v2"))
    l_transfer.add_argument("--i-understand-v1-edit", dest="confirm_v1_edit",
                            action="store_true",
                            help="allow the one write this repo makes to a v1 file")
    l_transfer.add_argument("--dry-run", action="store_true",
                            help="print the roster diff and write nothing")
    _add_json(l_transfer, nested=True)

    p_why = sub.add_parser("why", help="the routing decision for a job or a shape")
    p_why.add_argument("id", nargs="?")
    p_why.add_argument("--task", choices=TASK_CHOICES)
    p_why.add_argument("--tier", choices=TIER_CHOICES)
    p_why.add_argument("-m", dest="m", choices=MODEL_CHOICES)
    p_why.add_argument("-x", "--exclude", action="append", default=[], metavar="EMAIL")
    p_why.add_argument("--allow-desktop", action="store_true")
    _add_json(p_why)
    p_why.set_defaults(handler=cmd_why)

    p_daemon = sub.add_parser("daemon", help="supervisor control")
    p_daemon.set_defaults(handler=cmd_daemon, daemon_command=None)
    _add_json(p_daemon)
    daemon_sub = p_daemon.add_subparsers(dest="daemon_command")
    daemon_sub.add_parser("start")
    daemon_sub.add_parser("stop")
    d_status = daemon_sub.add_parser("status")
    _add_json(d_status, nested=True)
    d_logs = daemon_sub.add_parser("logs")
    d_logs.add_argument("-n", "--lines", type=int, default=40)
    d_logs.add_argument("-f", "--follow", action="store_true")
    d_install = daemon_sub.add_parser("install")
    d_install.add_argument("--dry-run", action="store_true",
                           help="print instead of writing (the plist, or with "
                                "--hooks the settings diff)")
    d_install.add_argument("--hooks", action="store_true",
                           help="install the three Claude Code hook entries into "
                                "~/.claude/settings.json instead of the plist "
                                "(prints the diff first, always)")

    p_hook = sub.add_parser("hook", help=argparse.SUPPRESS)
    p_hook.add_argument("event", help="SessionStart | UserPromptSubmit | PostToolUse")
    p_hook.set_defaults(handler=cmd_hook)

    p_doctor = sub.add_parser("doctor", help="offline checks of the local install")
    p_doctor.add_argument("--live", action="store_true",
                          help="also ping the daemon (C-16.2)")
    _add_json(p_doctor)
    p_doctor.set_defaults(handler=cmd_doctor)

    p_ping = sub.add_parser("ping", help="push a message into a session inbox")
    p_ping.add_argument("--session", metavar="ID")
    p_ping.add_argument("text", nargs="*", help="the message (quoting optional)")
    _add_json(p_ping)
    p_ping.set_defaults(handler=cmd_ping)

    # The sessions kit (C-17.1: `sessions` and `handoff` are permanent verbs and
    # dispatch to the `subfleet-sessions` entry point). The sub-verbs are
    # registered by that module so the two surfaces cannot drift.
    from .sessions import cli as sessions_cli
    p_sessions = sub.add_parser(
        "sessions", help="live sessions: list, continue, revive, mirror, handoff")
    # A bare `subfleet sessions` is `sessions list`, so the parent carries that
    # verb's flags — v1's `subfleet sessions --all` is a spelling C-17.1 keeps.
    p_sessions.add_argument("--all", action="store_true",
                            help="include lane runs and dead rows")
    _add_json(p_sessions)
    p_sessions.set_defaults(handler=cmd_sessions, sessions_command=None)
    sessions_cli.add_verbs(p_sessions.add_subparsers(dest="sessions_command"))

    p_handoff = sub.add_parser(
        "handoff", help="continue a Claude session through a freshly dispatched agent")
    sessions_cli.add_handoff_flags(p_handoff)
    _add_json(p_handoff)
    p_handoff.set_defaults(handler=cmd_handoff)
    return parser


ALIASES = {"jobs": ["runs"], "show": ["runs", "show"], "capacity": ["status"],
           "notify": ["ping"], "resume-codex": ["resume"]}
PASSTHROUGH = {"-h", "--help", "-V", "--version"}


def rewrite_aliases(argv: Sequence[str]) -> list[str]:
    """v1 spellings reach the v2 verb (C-17.1); a bare `subfleet` is `status`."""
    argv = list(argv)
    if not argv:
        return ["status"]
    first = argv[0]
    if first in PASSTHROUGH:
        return argv
    if first in ALIASES:
        return [*ALIASES[first], *argv[1:]]
    if first.startswith("-"):
        return ["status", *argv]
    return argv


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    parser = build_parser()
    try:
        args = parser.parse_args(rewrite_aliases(argv))
    except SystemExit as exc:
        # argparse exits 2 on a usage error and 0 on --help; main() returns codes.
        return int(exc.code or 0)
    if getattr(args, "version", False):
        from . import __version__
        out(f"{PROG} {__version__}")
        return int(Exit.OK)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        note(f"{PROG}: interrupted")
        return int(Exit.CANCELLED)
    except BrokenPipeError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return int(Exit.OK)
    except OSError as exc:
        return fail(Exit.OPERATIONAL, str(exc))


if __name__ == "__main__":                              # pragma: no cover
    raise SystemExit(main())
