"""Durable job ownership, asynchronous execution, and the local socket API.

The scheduler reserves rows before launch. Guardians own provider waits;
bounded workers do process inspection and filesystem publication. SQLite
transactions contain only SQL (C-3.3, C-16.4).
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import dataclasses
import errno
import faulthandler
import itertools
import fcntl
import functools
import hashlib
import json
import logging
import os
from pathlib import Path
import resource
import signal
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import weakref
from uuid import uuid4
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import __version__
from . import capacity, descriptors, folders, ids, lanes_transfer, machine, procs, protocol, render, route_check, scheduler
from .adapters import claude_mcp
from .descriptors import busy_answer, send_reply  # noqa: F401 - busy_answer: the tests' busy line
from .adapters.base import AdapterError
from .adapters.registry import get_adapter
from .alerts import operator_session
from .contracts import (
    CAPACITY_RECHECK_CEILING_S, EXIT_SETTLE_S, PIN_NOTICE_AFTER_S, Exit, HEADLESS_MARKER, IDENTITY_STATUS_BY_EVIDENCE, INSPECT_INTERVAL_S, KILL_SETTLE_S,
    OWNED_CENSUS_INTERVAL_S, START_GRACE_S, STOP_DUMP_MARGIN_S, STOP_GRACE_S, TERM_GRACE_S,
    WAIT_POLL_MAX_S, WAIT_RECHECK_S, WORKSPACE_RETRY_BASE_S, WORKSPACE_RETRY_CEILING_S, Attestation, ClockSource, Closure, ClosureReason, Credential,
    ExitInfo, IdentityStatus, JobSpec, Lane, LaneOwner, Launch, Outcome, OutcomeClass,
    Reading, ReadingLabel, Sandbox, attempt_dir,
)
from .credentials import resolve_credential
from .guardian import atomic_publish
from .lockwatch import LockWatch
from .waits import WaitHub
from .policy import (RETENTION_DEFAULTS, PolicyError, admission_settings, cap as policy_cap, load_policy,
                     policy_hash, resolve_model, turn_cap)
from .retention import RetentionState, maintenance
from .retention_git import discard_registration
from .salvage import (
    SalvageError, _failure, _git_env, _transient_git, git_head, git_toplevel, git_tree, path_text, pin_baseline, salvage, transient_os_error,
    utf8_text, validate_writable_workdir, working_tree,
)
from .sessions import registry
from .sessions.registry import CONVERSATION_FIX
from .sessions.transcripts import NotRegularFile, open_regular, read_regular
from .store import Store, _pin_notice_key, notice_fingerprint, notice_rows, pin_notice_jobs

#: "not asked yet", distinct from "asked, and there was no answer".
_UNSET = object()

TERMINAL = ("succeeded", "failed", "cancelled", "lost")

LIVE_ATTEMPTS = ("SELECT * FROM attempts WHERE state IN "
                 "('reserved','starting','running','finalizing')")
#: C-5.11: what a tick reads of every live attempt, in one statement: where its
#: receipts are, its state, and its job's cancel request and wall limit. It
#: names no column SQLite keeps on overflow pages: `evidence_json` averaged
#: 8.4 KB for a running attempt on 2026-10-02, and `SELECT *` read it for every
#: live attempt twenty times a second.
LIVE_TICK = ("SELECT a.attempt_id,a.job_id,a.seq,a.state,j.cancel_requested_at,"
             "j.started_at AS job_started_at,j.max_wall_s "
             "FROM attempts a LEFT JOIN jobs j USING(job_id) "
             "WHERE a.state IN ('reserved','starting','running','finalizing')")
#: C-11, C-6.4: what a route evaluation reads of attempts and jobs. Only an
#: active attempt occupies a lane slot or counts against a parent's cap; only a
#: job with a parent extends an ancestry (`scheduler._parent_blocks`); and on
#: this line an active attempt's job says by its `kind` whether it counts as a
#: turn (C-26.9). Every other attempt and job the store keeps changes no route:
#: on 2026-10-02 the live store held 2,722 attempts (23 MB, most of it
#: `evidence_json`) and 2,625 jobs, read and turned into dicts on every
#: evaluation, against 38 active attempts and 26 jobs with a parent.
ROUTE_ATTEMPTS = ("SELECT attempt_id,job_id,seq,lane_id,model_requested,state,reserved_at FROM attempts "
                  "WHERE state IN ('reserved','starting','running','finalizing') ORDER BY reserved_at,seq")
#: `state` is carried for C-6.15's host-pressure hold, which leaves out the
#: attempts of any ancestor of a job that has not started; every such job has a
#: parent, so it is among these rows.
#: `+created_at` keeps `jobs_created` (C-3.7) from serving the order: with it the
#: planner scanned every job (2.3 ms on the live store, 2026-10-06) instead of the
#: two index lookups and an in-memory sort of the few rows found (0.05 ms).
ROUTE_JOBS = ("SELECT job_id,parent_job_id,state,kind FROM jobs WHERE parent_job_id > '' OR job_id IN "
              "(SELECT job_id FROM attempts WHERE state IN ('reserved','starting','running','finalizing')) "
              "ORDER BY +created_at,rowid")
PENDING_EXPORTS = ("SELECT job_id FROM jobs WHERE accepted_attempt_id IS NOT NULL "
                   "AND job_id IN (SELECT holder FROM leases) ORDER BY rowid")
#: C-3.7: a holder's newest probe record, newest first: the newest JSON payload
#: naming it (`hit`, one step of `events_probe_holder`), and every probe.state
#: payload SQLite does not read as JSON (`events_not_json`, normally none),
#: which `_probe_record` parses as the old walk did. A NaN or an Infinity is one
#: such payload: json.dumps writes it, json.loads reads it, json_valid refuses it.
PROBE_RECORD = (
    "SELECT event_id,hit,data_json FROM (SELECT event_id,1 AS hit,data_json FROM events "
    "WHERE kind='probe.state' AND json_valid(data_json) AND json_extract(data_json,'$.holder')=? "
    "ORDER BY event_id DESC LIMIT 1) "
    "UNION ALL SELECT event_id,0,data_json FROM events WHERE kind='probe.state' AND NOT json_valid(data_json) "
    "ORDER BY event_id DESC")

#: C-3.7: read connections the daemon's store keeps beside its one writer. A
#: read holds one for a single statement (or a `snapshot` block, which only
#: reads), so a few serve every pool. Snapshots may hold all but
#: `store.STATEMENT_RESERVE` (2) of them; a read that finds none free waits at
#: most `store.READ_WAIT_S` (1 s), then opens one of its own, and says so.
READ_CONNECTIONS = 6
#: d635: seconds a retention pass may start new work; a started job gets its
#: archive slice (`retention.SLICE_S`) and the batch's two holder listings.
RETENTION_PASS_S = 180
#: C-8.4: an idle, cancelled or non-advancing pass waits an hour from its end.
RETENTION_INTERVAL_S = 3600
#: Set to "1" in the daemon's environment, retention selects, archives and deletes
#: nothing (2.1.11 ships it dormant; see `_retention`).
RETENTION_DORMANT_ENV = "SUBFLEET_RETENTION_DORMANT"
#: d635: seconds between passes while a backlog is being worked off.
RETENTION_CATCH_UP_S = 5
#: C-16.5: the ops a PostToolUse or prompt hook sends, which only read the store.
#: They have their own pool, so they never queue behind a view build or a write
#: waiting for the store lock on the general request pool.
LOOKUP_OPS = frozenset({"list", "show", "notice.pending"})

#: C-6.3: how many times one job's route is evaluated (off the store lock) and
#: checked inside its reserving transaction in one pass. A check that refuses
#: the decision rolls the transaction back and the route is evaluated again,
#: off the lock; after this many the job keeps its place and waits for the
#: next pass (`route-moved`). No route is ever evaluated with the lock held.
ROUTE_TRIES = 3

#: C-16.6: `accept` failures that say the process or the system is short of
#: something for now, not that the socket is gone. The daemon waits and accepts
#: again; any other error still ends `serve_forever`.
ACCEPT_TRANSIENT = frozenset({errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM,
                              errno.ECONNABORTED, errno.EINTR, errno.EAGAIN})
ACCEPT_RETRY_BASE_S = .05
ACCEPT_RETRY_CEILING_S = 2.0
#: C-16.6: `accept` failing without a break for this long is not a moment's
#: shortage but a leak outside the capped connections (a runner's relay socket, a
#: child's pipes): the daemon ends `serve_forever`, so launchd starts a fresh one,
#: rather than stay alive and deaf holding the lock.
ACCEPT_GIVE_UP_S = 300
#: C-16.7: how long `close()` waits, in all, for connection readers to return.
READER_JOIN_S = 2.0

#: C-6.11: how long admission may place nothing while jobs are pending before
#: `daemon.log` says so, and how often it repeats while that lasts.
ADMISSION_IDLE_LOG_S = 60
ADMISSION_IDLE_REPEAT_S = 600
ADMISSION_IDLE_REPEAT_EXPECTED_S = 3600
#: C-6.11: waits that are a person's or a retry's to end, not admission's. A
#: turn held for its conversation (C-24.5, C-30.4) is not admission's to place.
NOT_ADMISSIONS_TO_PLACE = ("approval", "uncertain", "workspace", "attempt-live", "conversation-blocked",
                           "message-settled")
#: C-6.11: ordinary queueing. A fleet at its cap with lanes to spare is working.
EXPECTED_HOLDS = frozenset({"fleet-full", "slot-kept", "parent-cap", "no-slot", "lease-held",
                            "probe-pending", "behind-older-job", "route-moved", "machine-busy",
                            "lane-proving"})
#: C-6.9, C-10.3: how long one read of Claude Code's session registry serves:
#: which callers are live, and whether Claude Code uses the desktop login.
REGISTRY_READ_TTL_S = 2
#: C-10.3: a reservation treats an in-use answer older than this as use (its
#: registry read began that long ago; the reservation refreshed it just before).
DESKTOP_IN_USE_MAX_AGE_S = 10
#: C-5.10: a worker that raised is tried again this long after, doubling to the ceiling.
WORKER_RETRY_BASE_S = .5
WORKER_RETRY_CEILING_S = 60
#: C-26.14: tries at a turn's end snapshot while git fails transiently (C-6.8's
#: kinds), before finalization records the failure and goes on without it, so a
#: turn's finalization waits on its diff for at most a few capped git calls.
TURN_TREE_TRIES = 3
#: C-13.1: tries at a finalizing job's salvage while git fails transiently,
#: before finalization records the failure and goes on without a salvage ref.
#: Any other failure is recorded at once: retrying it could only fail the same
#: way, and until finalization ends the attempt holds its lane (2026-09-27: two
#: finished attempts held Codex lanes for hours on a snapshot that could not
#: succeed). The worktree is kept either way (C-13.4).
SALVAGE_TRIES = 3
#: C-6.8: how an attempt ends whose launch-time main/master re-check git gave no answer
#: to (`_launch`), and how `_defer_after_launch` counts those in a row.
LAUNCH_CHECK_UNFINISHED = "workdir-branch-check-unfinished:"
#: C-13.1: how many of the paths a salvage left out the attempt's evidence and the
#: job's notice name; both give the count of all of them.
SALVAGE_SKIPPED_SHOWN = 5
#: C-6.14: the event that records a model answering on a lane (`_record_answer`),
#: and how many of the newest a starting daemon reads to remember what was proven.
ANSWER_EVENT = "lane.answered"
ANSWER_SEED_ROWS = 5000
#: C-6.14: a running attempt's stream is read for its model's first answer at most
#: this often, this many bytes a look, holding at most this much of one line.
ANSWER_READ_INTERVAL_S = 1.0
ANSWER_READ_CHUNK = 256 * 1024
ANSWER_LINE_MAX = 4 * 1024 * 1024


def _skipped(paths: list[str]) -> dict:
    """C-13.1: what the evidence records of the paths a snapshot left out: how
    many, and the first `SALVAGE_SKIPPED_SHOWN`."""
    return {"count": len(paths), "paths": paths[:SALVAGE_SKIPPED_SHOWN]}


def _left_out(skipped: dict) -> str:
    """C-13.1: `_skipped`'s record as a notice names it."""
    count, paths = skipped["count"], skipped["paths"]
    shown = ", ".join(f"'{path}'" for path in paths) + (", ..." if count > len(paths) else "")
    return f"{count} nested repositor{'y' if count == 1 else 'ies'} with no commit, kept only in the worktree: {shown}"


def _lane_fault_of(evidence_json: str | None) -> dict | None:
    """C-4.5: the lane fault an attempt's evidence records (`Daemon._lane_fault`), or None."""
    try:
        evidence = json.loads(evidence_json or "{}")
    except (TypeError, ValueError):
        return None
    fault = evidence.get("lane_fault") if isinstance(evidence, dict) else None
    return fault if isinstance(fault, dict) else None


def _evidence_key(evidence_json: str | None, key: str) -> Any:
    """`key` of an attempt's evidence, or None when there is none or it does not parse."""
    try:
        evidence = json.loads(evidence_json or "{}")
    except (TypeError, ValueError):
        return None
    return evidence.get(key) if isinstance(evidence, dict) else None


def _epoch(stamp: str | None) -> float | None:
    """`stamp` (as `utcnow` writes one) in epoch seconds, or None when it does not parse."""
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _held_event(data_json: str | None) -> dict | None:
    """C-13.1: a `salvage.baseline_held` event's data as `_pin_baseline` writes it, else
    None: a ref that is one line of valid UTF-8 (`path_text`), a commit in hex, a seq
    that is a number, and `skipped`, when there is one, as `_skipped` makes it from
    `path_text` paths. A notice is written from it inside the transaction that cancels
    or fails a job, so an event that is anything else is left out, never a reason that
    transaction cannot commit: a `skipped` that was not checked raised out of `kill`, and
    so would a commit holding a lone surrogate, at its `encode()` (review of the P2 fix)."""
    try:
        data = json.loads(data_json or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ref, commit, seq, skipped = data.get("ref"), data.get("commit"), data.get("seq"), data.get("skipped", {})
    text = lambda value: isinstance(value, str) and value == path_text(value)   # noqa: E731
    if not (text(ref) and ref and isinstance(commit, str) and len(commit) in (40, 64)
            and not commit.strip("0123456789abcdef") and type(seq) is int):
        return None
    if skipped != {} and not (isinstance(skipped, dict) and set(skipped) == {"count", "paths"}
                              and type(skipped["count"]) is int and isinstance(skipped["paths"], list)
                              and all(text(path) for path in skipped["paths"])):
        return None
    return data


#: C-6.12: what evaluating one job's route may raise without ending the pass
#: (`PolicyError` is a ValueError), whether from the job's own fields, a policy
#: that `load_policy` does not fully validate (a `reserve` that is not a mapping
#: raises AttributeError in `evaluate`), or a defect; one in the shared capacity
#: view or policy puts every evaluated job on a route wait, visibly, instead of
#: ending the pass. A store error (sqlite3, OSError) is not here: it is the
#: pass's, and C-5.10 retries it.
ROUTE_ERRORS = (ValueError, KeyError, TypeError, AttributeError, IndexError)
#: C-6.12: a route that could not be evaluated is looked at again this long
#: after, doubling per consecutive failure to the ceiling, as C-6.8's are.
ROUTE_RETRY_BASE_S = 5
ROUTE_RETRY_CEILING_S = 300
#: C-23.26: how old a delivered service notice is before retention prunes it.
SERVICE_NOTICE_RETENTION_S = 14 * 86400
#: C-15.3's delivery ladder, lowest first. The session hooks surface `pending`
#: and `offered` rows; `surfaced` and `acknowledged` have reached the session.
NOTICE_LADDER = ("pending", "offered", "surfaced", "acknowledged")


class _RouteMoved(Exception):
    """C-6.3: the reserving transaction's check refused its early decision, or raised; raised to roll it back."""

    def __init__(self, why: str, judged: int, *, error: BaseException | None = None):
        super().__init__(why)
        self.why = why if why in ("old", "error") else "moved"
        self.judged, self.error = judged, error


class Unroutable(Exception):
    """C-6.12: evaluating this job's route raised `cause`; the pass settles the job and goes on."""

    def __init__(self, cause: BaseException):
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cause = cause

#: The sessions kit's durable facts, as `events` kinds (C-23.33, C-23.35). They
#: are events rather than a table because each is an append-only record of one
#: operator or worker decision, and the latest row for a session is the answer.
NUDGE_EVENT = "session.nudged"
REVIVE_EVENT = "session.revived"
RETIRE_EVENT = "session.retired"
UNRETIRE_EVENT = "session.unretired"

#: `store.transaction` writes its own audit event under the kind it is given,
#: and `_session_events` reads the NEWEST row of each kind. Naming the audit
#: event after the record would therefore shadow the record with a summary that
#: carries no dedupe key — so the two are deliberately different kinds.
def audit_kind(event: str) -> str:
    return event.rsplit(".", 1)[0] + ".recorded"

#: C-23.55: one live revive per session. The key is session-scoped, not
#: caller-scoped, and its holder is the revive job id so every existing
#: holder-keyed release site frees it. It is the only lease in the `session:`
#: namespace: C-6.5 tells a session's instances apart at submit and leases the
#: worktree, not the session. Nothing reads the namespace by prefix.
def revive_lease_key(session_id: str) -> str:
    return f"session:{session_id}:revive"


def native_session_lease_key(lane_id: str, session_id: str) -> str:
    """A read-only continuation still writes its provider's native transcript."""
    return f"native-session:{lane_id}:{session_id}"


def imported_external(attempt: dict) -> bool:
    """docs/migration.md principle 3: this attempt belongs to a v1 run, not to v2.

    A v1 run still running at import time is recorded as a live attempt carrying
    `imported_external` (subfleet/importer.py), and "v2 never adopts, kills, or
    finalizes it". Recovery therefore skips it: without this the first control
    tick would contain, kill or lose a run v1 is still executing. The parse is
    defensive because a control loop that raises stops recovering everything.
    """
    try:
        return bool(json.loads(attempt.get("evidence_json") or "{}").get("imported_external"))
    except (TypeError, ValueError):
        return False
LIVE = ("reserved", "starting", "running", "finalizing")
WRITE_PREAMBLE = (
    "<!-- subfleet:write -->\n"
    "Work only in the assigned workspace. Preserve existing changes. "
    "Commit each coherent step on the assigned branch; never rewrite history "
    "or push to main or master. Leave a concise report of changes and tests.\n\n"
)
#: What a writable job that is not in place is told about where it works
#: (C-6.6, C-13.1). Without it a brief's absolute paths lead a writer back into
#: the caller's checkout, which the job must never touch (review of d261).
WORKSPACE_NOTE = (
    "Your workspace is {worktree}: a detached checkout of {top} at commit {head}, made "
    "for this job. It holds only what is committed there, none of the caller's uncommitted, "
    "untracked or ignored files (dependencies such as .venv or node_modules may need "
    "installing). The checkout at {top} is the caller's: where the task names a path in it, "
    "use the same relative path in your workspace, and never write under {top}.{place} When the "
    "job ends, Subfleet keeps what you changed here as a git ref under refs/subfleet-salvage/ in "
    "that repository.\n\n"
)
#: The caller ran the job from a directory below the repository's top (review B-4).
WORKSPACE_PLACE = (" The caller ran this job from {top}/{prefix}: a path the task gives relative to "
                   "that directory is relative to {worktree}/{prefix} here, so work from there.")
WORKSPACE_PLACE_UNCOMMITTED = (" The caller ran this job from {top}/{prefix}, which commit {head} does not "
                               "hold, so your workspace has no {prefix}: create what the task needs there, "
                               "under {worktree}.")
#: Git could not say whether the commit holds the caller's directory.
WORKSPACE_PLACE_UNCHECKED = (" The caller ran this job from {top}/{prefix}: a path the task gives relative to "
                             "that directory is relative to {worktree}/{prefix} here (if commit {head} does not "
                             "hold it, you start at {worktree}; create it there).")
HEADLESS_PREAMBLE = (
    HEADLESS_MARKER + "\nThis is a delegated, headless job. Complete the task "
    "autonomously, preserve the caller's work, and return a final deliverable.\n\n"
)

#: C-5.10, C-5.11: what `_process_attempt` returns when it is the retry of an
#: inspection that raised and could not inspect this time (another attempt's
#: `ps` read was running, or the table, its boot identity or the guardian could
#: not be read). `_schedule` counts it as neither a success, which would clear
#: the key's failure count, nor a failure.
DEFERRED = object()


def worker_retry_delay(failures: int) -> float:
    """C-5.10: seconds before a worker that has raised `failures` times in a row is tried again."""
    return min(WORKER_RETRY_CEILING_S, WORKER_RETRY_BASE_S * 2 ** min(max(failures, 1) - 1, 16))


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def utcnow_ms() -> str:
    """The conversation store's clock format (milliseconds), for C-26.14's turn windows."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _later(stamp: str, seconds: float) -> str:
    """`stamp` (as `utcnow` writes one) plus `seconds`, in the same form (C-11.8)."""
    return (datetime.fromisoformat(stamp.replace("Z", "+00:00")) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _seconds_between(earlier: str, later: str) -> float:
    return (datetime.fromisoformat(later.replace("Z", "+00:00"))
            - datetime.fromisoformat(earlier.replace("Z", "+00:00"))).total_seconds()


def age(timestamp: str | None) -> float:
    if timestamp is None:
        return 0.0
    return (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp.replace("Z", "+00:00"))).total_seconds()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _text(path: Path) -> str:
    """A probe's output as `read_text` gives it (universal newlines), only as a
    regular file (never waiting in open())."""
    return read_regular(path).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")

class DaemonUnavailable(RuntimeError):
    code = 69


def _outside(prefix: str) -> bool:
    """Whether a relative path leaves its directory: `..` or `../x`, never a name
    that merely starts with dots (`..data`, review of 5aa2718), nor an absolute one."""
    return prefix == os.pardir or prefix.startswith(os.pardir + os.sep) or os.path.isabs(prefix)


def _written_by_policy(exc: AdapterError, task: str | None) -> AdapterError:
    """d261: a writer's refusal of a job the caller did not ask to write: say who
    did, and the way out."""
    return AdapterError(f"{exc} ({task or 'these'} jobs write by policy)", code=exc.code,
                        fix=(exc.fix + "; or " if exc.fix else "") + "pass -s read-only")


def _git_prefix(workdir: str, cap: float) -> str | None:
    """Where `workdir` is in its repository as git spells it (`pkg/sub`; `.` at the
    top), which is how a worktree cut from it spells it. Git resolves a directory
    named in another case, in decomposed Unicode or through the
    `/System/Volumes/Data` firmlink to its committed name; `os.path.relpath` of
    the caller's spelling does not (review, 2026-09-25). None when git cannot say."""
    try:
        shown = subprocess.run(["git", "-C", workdir, "rev-parse", "--show-prefix"], capture_output=True,
                               env=_git_env(None), timeout=cap)
    except (OSError, subprocess.SubprocessError):
        return None
    if shown.returncode:
        return None
    return os.fsdecode(shown.stdout.rstrip(b"\n")).rstrip("/") or "."


def _commit_holds_dir(top: str, commit: str, prefix: str, cap: float) -> bool | None:
    """Whether `commit` holds the directory `prefix` (relative to `top`), so a
    worktree cut at it will: None when git cannot say. Under salvage's environment
    (`_git_env`): `--literal-pathspecs` with `GIT_GLOB_PATHSPECS` set is a fatal
    error, and the daemon's own scrub (`cli.STRIPPED_ENV`) holds only when
    `daemon start` started it (review of the P3-3 fix)."""
    try:
        found = subprocess.run(["git", "--literal-pathspecs", "-C", top, "ls-tree", "-d", "-z", commit, "--", prefix],
                               capture_output=True, env=_git_env(None), timeout=cap)
    except (OSError, subprocess.SubprocessError):
        return None
    if found.returncode:
        return None
    entries = [entry.split(b"\t", 1) for entry in found.stdout.split(b"\0") if b"\t" in entry]
    return any(meta.split()[1:2] == [b"tree"] and path == os.fsencode(prefix) for meta, path in entries)


def _released_on_failure(init: Callable[..., None]) -> Callable[..., None]:
    """Run `__init__`; if it raises, give back what it had acquired, then re-raise.

    `main` exits when construction fails, but an embedded daemon (the tests)
    would otherwise keep `daemon.log`'s descriptor, its handler on a logger
    whose `id()`-based name a later daemon may reuse, the store and the lock.
    """
    @functools.wraps(init)
    def wrapper(self, *args, **kwargs):
        try:
            init(self, *args, **kwargs)
        except BaseException:
            self._release_partial_init()
            raise
    return wrapper


class Daemon:
    @_released_on_failure
    def __init__(self, state_root: str | Path, *, tick_s: float = .05,
                 start_grace_s: float = START_GRACE_S, term_grace_s: float = TERM_GRACE_S,
                 kill_settle_s: float = KILL_SETTLE_S, exit_settle_s: float = EXIT_SETTLE_S,
                 inspect_interval_s: float = INSPECT_INTERVAL_S,
                 guardian_start_delay_s: float = 0,
                 stop_grace_s: float = STOP_GRACE_S,
                 crash_hook: Callable[[str, str, str | None], None] | None = None,
                 publish_hook: Callable[[str, Path], None] | None = None,
                 desktop_prober: Callable[[], Any] | None = None,
                 max_connections: int | None = None,
                 connection_idle_s: float = descriptors.CONNECTION_IDLE_S):
        self.root = Path(state_root).expanduser().resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.tick_s, self.start_grace_s, self.term_grace_s = tick_s, start_grace_s, term_grace_s
        self.kill_settle_s, self.exit_settle_s = kill_settle_s, exit_settle_s
        self.inspect_interval_s = inspect_interval_s
        # C-5.9: attempt id -> when a post-receipt census first found the table
        # still draining; the exit settle window is measured from there.
        self._exit_settle: dict[str, float] = {}
        # C-26.14: attempt id -> transient failures of its end snapshot so far.
        self._tree_failures: dict[str, int] = {}
        # C-13.1: attempt id -> transient failures of its salvage so far.
        self._salvage_failures: dict[str, int] = {}
        self.guardian_start_delay_s = guardian_start_delay_s
        # C-5.8a: how long `watch_stop` lets a stopping process live, and what
        # `close()` calls first, on its own thread, to start that bound. Only
        # `main` sets it: a daemon built in a test process ends nothing.
        self.stop_grace_s = stop_grace_s
        self.on_stop: Callable[[], bool | None] | None = None
        self.crash_hook, self.publish_hook = crash_hook, publish_hook
        # C-10.3: who asks the desktop app's own credential who it is. `None`
        # means the Claude adapter's keychain reader; a harness that must not
        # touch a real login passes its own, and one that returns nothing keeps
        # every recorded desktop flag exactly as it was.
        self.desktop_prober = desktop_prober
        self.stopping = threading.Event()
        self.changed = threading.Condition()
        self._submit_lock = threading.Lock()
        self._enroll_lock = threading.Lock()
        self._busy_lock = threading.Lock()
        self._busy: set[str] = set()
        self._launches: dict[str, Launch] = {}
        self._children: dict[str, subprocess.Popen] = {}
        self._starting_deadlines: dict[str, float] = {}
        self._pending_launches: set[str] = set()
        self._export_locks: dict[str, threading.Lock] = {}
        # C-5.12: attempt id -> when its processes are next inspected, and the
        # one process table those inspections share.
        self._inspect_next: dict[str, float] = {}
        # C-5.11: live attempts known to be this daemon's own. An attempt is
        # recorded `imported_external` when it is imported or never, so "not
        # imported" is read once; one that is imported is read again each tick,
        # because the importer clears the flag when it settles the run.
        self._native: set[str] = set()
        # Consecutive ticks on which an attempt's ownership could not be read.
        self._v1_unread: dict[str, int] = {}
        # C-5.11: attempts whose last inspection raised and has not yet been
        # repeated to its end; until it has, a pass that cannot inspect is DEFERRED.
        self._inspect_retry: set[str] = set()
        # The last table read (None if the read failed) and when it expires,
        # replaced whole; the lock is held while `ps` runs.
        self._table: tuple[procs.ProcessTable | None, float] = (None, 0.0)
        self._table_lock = threading.Lock()
        # C-6.8: job id -> consecutive transient workspace failures. In memory on
        # purpose: a restart forgives the count, and the events keep the record.
        self._workspace_deferrals: dict[str, int] = {}
        self._reset_admission_state()
        # C-5.10: worker key -> consecutive failures, and the earliest next try.
        self._worker_failures: dict[str, int] = {}
        self._worker_retry_at: dict[str, float] = {}
        self._last_maintenance = time.monotonic()
        self._retention_dormant_logged = False
        # d635: deferrals and measured sizes carried between retention passes.
        self._retention_state = RetentionState()
        # C-16.7: every client connection held, from `accept` until its last
        # reply has been written; what the cap counts, and what `close()` shuts.
        self._connections: set[socket.socket] = set()
        self._connection_lock = threading.Lock()
        # C-15.5: `wait` requests between dispatch and their answer being sent, so
        # `close` can let each answer before it shuts the connections down.
        self._waits_answering = 0
        self._waits_answered = threading.Condition()
        # C-16.7: the most client connections held at once is derived, at each
        # `accept`, from the open-file limit this process has then (main raises
        # it first) and the conversation turns running (`max_connections`); a
        # caller may pin it. How long one with nothing outstanding may stay
        # silent. Counts are for daemon.status.
        self._pinned_max_connections = None if max_connections is None else max(1, int(max_connections))
        self.connection_idle_s = connection_idle_s
        self._connection_counts = {"accepted": 0, "refused": 0, "idle_closed": 0,
                                   "abandoned": 0, "accept_failures": 0, "unscheduled": 0}
        self._closed = False
        self._socket: socket.socket | None = None
        self._lock_fd = os.open(self.root / "daemon.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._lock_fd)
            raise DaemonUnavailable("another daemon holds daemon.lock") from None
        self._lock_finalizer = weakref.finalize(self, os.close, self._lock_fd)
        try:
            ident = {"pid": os.getpid(), "boot_id": procs.boot_id(),
                     "proc_start": procs.proc_start(os.getpid()), "version": __version__}
            if not ident["proc_start"]:
                raise procs.InspectionError("daemon identity is absent")
        except BaseException:
            self._lock_finalizer()
            raise
        self._ident = ident
        self.log = logging.getLogger(f"subfleet.daemon.{id(self)}")
        # A daemon collected with its close cut short left its handler on this
        # logger, whose `id()`-based name this one now has: its lines would go to
        # that daemon's log too. Let it go (the stream stays open while
        # faulthandler or `_HELD_STREAMS` holds it; review of 65dcb6d, P3).
        for stale in list(self.log.handlers):
            self.log.removeHandler(stale)
        log_fd = os.open(self.root / "daemon.log", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        self._log_handler = logging.StreamHandler(os.fdopen(log_fd, "a"))
        self.log.addHandler(self._log_handler)
        self.log.setLevel(logging.INFO)
        # C-3.6: the identity is written only now that SIGUSR1 has its handler, and
        # says so: `daemon stacks` signals only a daemon whose lock says
        # `stack_dumps`, never one that would die of the signal. A daemon saying so
        # is already one of `_ADVERTISED`, and stays until its lock stops saying so;
        # one that could not make the signal safe does not say so.
        advertise = self._enable_stack_dumps()
        # From here the lock may say `stack_dumps` (a write cut short may have said
        # it): only now can a failed rewrite keep this daemon in `_ADVERTISED`.
        self._dumps.may_advertise = advertise
        self._write_lock(stack_dumps=advertise)
        soft, hard = descriptors.open_file_limits()
        self.log.info("open-file limit %s (hard %s); up to %d client connections with no turn running, "
                      "idle ones closed after %g s",
                      descriptors.limit_for_display(soft) or "unlimited",
                      descriptors.limit_for_display(hard) or "unlimited",
                      self.max_connections, self.connection_idle_s)
        for directory in ("jobs", "lanes", "worktrees"):
            (self.root / directory).mkdir(mode=0o700, exist_ok=True)
        policy_path = self.root / "policy.json"
        if not policy_path.exists():
            atomic_publish(policy_path, Path(__file__).with_name("default_policy.json").read_bytes())
        self.policy = load_policy(policy_path)
        self.policy_digest = policy_hash(policy_path)
        # C-3.7: reads outside a transaction take a read connection, not the store lock.
        self.store = Store(self.root / "state.sqlite3", readers=READ_CONNECTIONS)
        self._pin_episodes = self._load_pin_episodes()          # C-11.8
        self._lane_answers, self._attempt_answers = self._load_answers()      # C-6.14
        # C-15.5: one reader answers every `wait`; its thread starts with the first.
        self.wait_hub = WaitHub(self.store, recheck_s=WAIT_RECHECK_S, on_error=lambda exc: self.log.warning(
            "wait hub: %s: %s (its waiters read for themselves)", type(exc).__name__, exc))
        self._seed_lanes()
        # 13: retention's pass (d635) can hold one worker for minutes; the other
        # twelve are what attempts, admission and exports had before.
        self.workers = ThreadPoolExecutor(max_workers=13, thread_name_prefix="subfleet-io")
        self.requests = ThreadPoolExecutor(max_workers=16, thread_name_prefix="subfleet-api")
        self.lookups = ThreadPoolExecutor(max_workers=8, thread_name_prefix="subfleet-read")   # C-16.5
        # C-16.7: each connection the daemon holds has a reader thread of its own
        # (`_hold_connection`), so none waits for one; `close()` joins them.
        self._readers: set[threading.Thread] = set()
        # A `wait` holds its thread for up to WAIT_POLL_MAX_S on its hub event (C-15.5), so
        # there is one for every connection the daemon may hold (review of the
        # descriptor hotfix: at 16, a 17th wait queued until its client gave up).
        self.waiters = ThreadPoolExecutor(max_workers=descriptors.CONNECTIONS_CEILING,
                                          thread_name_prefix="subfleet-wait")
        # Milestone 9: desktop conversations (C-24 to C-30). Its own store and pools.
        from .conversations.service import ConversationService
        self.conversations = ConversationService(self)
        # C-3.6: a long hold of either store's lock or of the conversation store's
        # file-write guard (`conversation-files`, held across a write's fsyncs), or
        # a long wait for one, is written to daemon.log with the holder's stack.
        # Waiters report on their own; the thread that samples long holds starts
        # with serve_forever.
        self.lock_watch = LockWatch(lambda text: self.log.warning("%s", text))
        self.lock_watch.add(self.store._lock)
        self.lock_watch.add(self.conversations.store._lock)
        self.lock_watch.add(self.conversations.store._writes)
        self.lock_watch.watch_reads("store", self.store.read_holds)       # C-3.7
        # C-16.7: connections this daemon shut down after a reply failed part
        # way; their end of stream is not a client leaving.
        self._shut_down: set[socket.socket] = set()
        self._control_thread: threading.Thread | None = None
        from .timers import Timers
        self.timers = Timers(self.store, self.root, self.policy, turn=self._timer_turn,
                             deliver=self._timer_notice)
        self.timers.desktop_in_use = self._desktop_in_use        # C-10.3: status.json as admission sees it
        self.timers.probe_record = self._probe_record            # C-18.1: the probe holding a lane, by name
        self._recovery_complete = threading.Event()
        # C-10.3: the desktop profile answer, at most one per reading window.
        self._desktop_cache: tuple[float, Any] = (0.0, _UNSET)

    def _release_partial_init(self) -> None:
        """Undo a construction that raised part way (see `_released_on_failure`).

        Each step runs on its own: one that fails must neither stop the rest nor
        replace the error that made construction fail.
        """
        def closing_log():
            if (handler := self.__dict__.get("_log_handler")) is not None:
                self.log.removeHandler(handler)
                # C-3.6: only a stream nothing may hold closes, whatever the step
                # below met or where the loop's swallowed exception landed (reviews
                # of 9a514a7 and 674b99b, P2: a flag defaulting to "may close").
                if self._may_close_log():
                    handler.stream.close()

        def no_stack_dumps():
            # C-3.6, as at a normal close (`_disable_stack_dumps`): `daemon.lock`
            # stops saying `stack_dumps` first, then SIGUSR1 is handed on; a lock
            # that cannot stop keeps the handler and its stream (review of
            # 3c8fe55, P0, and of 02c6320). The lock fd is still open here: its
            # finalizer runs last.
            if "_dumps_token" in self.__dict__:
                self._disable_stack_dumps()
        steps = [no_stack_dumps, closing_log]
        steps += [functools.partial(executor.shutdown, wait=False)
                  for executor in (self.__dict__.get(name)
                                   for name in ("workers", "requests", "lookups", "waiters"))
                  if executor is not None]
        if (conversations := self.__dict__.get("conversations")) is not None:
            steps.append(conversations.close)           # its pools and its store
        if (wait_hub := self.__dict__.get("wait_hub")) is not None:
            steps.append(wait_hub.stop)
        if (store := self.__dict__.get("store")) is not None:
            steps.append(store.close)
        if (finalizer := self.__dict__.get("_lock_finalizer")) is not None:
            steps.append(finalizer)                    # closing the fd releases the flock
        for step in steps:
            with contextlib.suppress(Exception):
                step()

    def _reset_admission_state(self) -> None:
        """What admission remembers between passes; all of it in memory (C-6.10, C-6.11)."""
        # C-6.14: what the daemon has seen of models answering. Lane id -> the epoch
        # seconds of the last answer on it, and attempt id -> when an attempt in
        # flight first answered (it is then no pilot): replaced whole under
        # `_answer_lock`, and seeded from the `lane.answered` events once the store
        # opens (`_load_answers`). Attempt id -> how far its stream has been read for
        # that answer. `_answer_news` says an answer came since the detached pass looked.
        self._answer_lock = threading.Lock()
        self._lane_answers: dict[str, float] = {}
        self._attempt_answers: dict[str, float] = {}
        self._answer_reads: dict[str, dict] = {}
        self._answer_news = False
        # C-6.10: job id -> the verdict a capacity wait keeps reaching, and how
        # often. In memory as C-6.8's count is: a restart forgives the count and
        # costs one decision row per waiting job.
        self._capacity_waits: dict[str, dict] = {}
        # C-6.12: job id -> its consecutive route evaluation failures and the last
        # one's error, replaced whole on each. In memory as C-6.8's count is.
        self._route_deferrals: dict[str, dict] = {}
        # C-4.5, C-6.9: job id -> (its last attempt id, whether the last due look
        # kept its transient retry on that attempt's lane). A pin that look let go
        # is not the job's demand while its clock runs; the next due look
        # evaluates the pair again, so a restart, which forgets this, changes nothing.
        self._retry_verdicts: dict[str, tuple[str, bool]] = {}
        # C-11.8: job id -> its pin episode: whether its pinned lane can never admit
        # it now (`open`), since when, when it fails, and whether its one notice
        # went. Loaded from the episode events once the store opens
        # (`_load_pin_episodes`), so a restart keeps the grace's clock and never
        # sends a second notice.
        self._pin_episodes: dict[str, dict] = {}
        # C-6.10: the leases the last pass of each kind saw that no probe holds.
        # One that has gone since is capacity that came free.
        self._leases_seen: dict[str, frozenset[tuple[str, str]]] = {}
        # C-6.12, C-24.5: turn job id -> the error type its conversation check last
        # raised, so the log says so once per change rather than once a pass.
        self._turn_check_errors: dict[str, str] = {}
        # C-10.3: (monotonic time read, in use); `_desktop_use_lock` orders the
        # reads that record a change, so each change is one `desktop.in_use` event.
        self._desktop_use_recorded: bool | None = None
        self._desktop_use_lock = threading.Lock()
        # C-3.7: recording writes an event, so it has its own lock: a read op that
        # publishes a registry read never waits for a store writer behind it.
        self._desktop_record_lock = threading.Lock()
        # C-6.9, C-10.3: the last registry read (`_registry_read`): the instant it
        # began, and the rows and desktop answer it found; reused for a moment.
        self._registry: tuple[float, dict | None] = (0.0, None)
        # C-6.11: why the last pass did not place each job it left, and when
        # admission last placed anything. Replaced whole at the end of a pass:
        # C-26.9's turn pass and the detached pass each replace their own, and
        # `_holds` is the two together.
        self._holds: dict[str, dict] = {}
        self._holds_by_kind: dict[str, dict[str, dict]] = {"turn": {}, "detached": {}}
        self._note_error: str | None = None       # C-24.4: the last failure to note turn waits
        self._pass_seq = {"turn": 0, "detached": 0}   # passes begun, per kind, under their pass lock
        # C-26.9: one pass of each kind at a time; the two kinds' passes run side
        # by side (`_admit_turns` beside `_admit`), so a turn never waits for a
        # detached job's evaluation, workspace or probe. `_admission_lock` guards
        # what both write: the holds, `_admission` and the route counts.
        self._pass_locks = {"turn": threading.Lock(), "detached": threading.Lock()}
        self._admission_lock = threading.Lock()
        # One pass at a time records what admission left (`_note_admission`),
        # with the placements every pass made since, counted as each is made, so
        # a long detached pass that is placing never reads as idle to the turn
        # pass's notes; the other pass does not wait.
        self._note_lock = threading.Lock()
        self._placed_unnoted = 0
        # Never held while another lock is taken: a reservation counts with
        # the store lock held (`_count_route`).
        self._route_count_lock = threading.Lock()
        # C-6.3: job id -> the last evaluation `_prepare_route` made for it and the
        # rows it rests on, taken by the reservation that follows.
        self._early_routes: dict[str, tuple[Any, dict]] = {}
        # C-6.3: reservations whose early decision stood (`reused`) and those
        # whose check chose again from the lanes that changed (`rechosen`); the
        # evaluations made again off the lock after a check refused one, and why
        # (`moved`: the job's pin names another lane now, or an evaluation now
        # would raise, or what the check rests on is gone; `old`: a clock earlier
        # than its view's; `error`: the check raised); jobs left for the next pass
        # after ROUTE_TRIES; and the lanes checks judged again: for their rows,
        # their own clocks, or a cap that began or ended.
        self._route_evaluations = {"reused": 0, "rechosen": 0, "again": 0, "moved": 0, "old": 0, "error": 0,
                                   "deferred": 0, "rejudged": 0}
        self._admission: dict[str, Any] = {"pending": 0, "placed_at": None, "idle_since": None,
                                           "idle_since_at": None, "checked_at": None, "logged_at": None,
                                           "reasons": {}}

    # --- lanes: enroll, hold, release (C-10.2, C-9.6) --------------------------

    def _next_lane_id(self, provider: str) -> str:
        """C-1.3: `<provider>-<n>`, the next free n after every lane the store knows."""
        used = []
        for row in self.store.query("SELECT lane_id FROM lanes WHERE provider=?", (provider,)):
            prefix, _, number = row["lane_id"].rpartition("-")
            if prefix == provider and number.isdigit():
                used.append(int(number))
        return f"{provider}-{max(used, default=0) + 1}"

    def _append_lanes_json(self, lane: Lane) -> None:
        """Keep the seed file (`lanes.json`, C-2.2) in step so a rebuilt store gets the lane back."""
        row = {"lane_id": lane.lane_id, "provider": lane.provider, "account_key": lane.account_key,
               "credential_ref": lane.credential.ref, "credential_kind": lane.credential.kind,
               "credential_epoch": lane.credential.epoch, "home": lane.home, "desktop": lane.desktop,
               "enabled": lane.enabled, "identity": lane.identity, "label": lane.label}
        edit = lanes_transfer._v2_roster_edit(self.root, row, lane.owner.value)
        if edit.changed:
            lanes_transfer._publish(edit.path, edit.after)

    def _enroll_lane(self, a: protocol.LanesArgs) -> dict:
        with self._enroll_lock:
            return self._enroll_lane_locked(a)

    def _enroll_lane_locked(self, a: protocol.LanesArgs) -> dict:
        """`lanes enroll <credential>` (C-10.2): a Claude home directory (a config
        directory holding a `claude auth login`), a Codex home (holds `auth.json`),
        or a `claude-quota-<email>` keychain item. The adapter's enrol turn decides
        that the credential authenticates and names the account (C-1.4, C-10.6);
        the lane is stored, its enrol readings with it, and `lanes.json` follows.
        `owner` defaults to v2; an account v1 still dispatches on is enrolled `v1`
        or enrolled `v2` and held (`lanes hold`) until its transfer.
        """
        text = (a.credential or "").strip()
        if not text:
            raise protocol.ProtocolError("lanes enroll: name a credential (a home directory or a "
                                         "claude-quota-<email> keychain item)", Exit.INVALID_INPUT)
        path = Path(text).expanduser()
        if path.is_dir():
            path = path.resolve()
            provider = "codex" if (path / "auth.json").is_file() else "claude"
            credential = Credential(provider, str(path), "home")
        elif text.startswith("claude-quota-"):
            credential = Credential("claude", text, "keychain-token")
        else:
            raise protocol.ProtocolError(
                f"lanes enroll: {text!r} is neither a home directory nor a claude-quota-<email> item",
                Exit.INVALID_INPUT)
        try:
            owner = LaneOwner(a.owner or "v2")
        except ValueError:
            raise protocol.ProtocolError("lanes enroll: owner must be v1 or v2", Exit.INVALID_INPUT) from None
        bindings = self.store.query("SELECT * FROM lanes WHERE credential_ref=? ORDER BY created_at,rowid", (credential.ref,))
        existing = bindings[-1] if bindings else None
        enrollment_holder = None
        if existing:
            if any(row['enabled'] for row in bindings):
                raise protocol.ProtocolError(
                    f"lanes enroll: {credential.ref} is already lane {existing['lane_id']}",
                    Exit.INVALID_INPUT, "subfleet lanes list")
            if existing['desktop'] or existing['owner'] != 'v2':
                raise protocol.ProtocolError('re-enrollment requires a non-desktop v2-owned lane', Exit.REFUSED)
            if a.owner is not None and a.owner != existing['owner']:
                raise protocol.ProtocolError('re-enrollment cannot change ownership; use lanes transfer', Exit.REFUSED)
            owner = LaneOwner(existing['owner'])
            credential = dataclasses.replace(credential, epoch=max(row['credential_epoch'] for row in bindings) + 1)
            enrollment_holder = 'probe:timer:enroll:' + str(uuid4())
            self.timers.active_holders.add(enrollment_holder)
            try:
                with self.store.transaction('lane.reenroll-reserved', lane_id=existing['lane_id']):
                    for row in bindings:
                        current = self.store.get_lane(row['lane_id'])
                        if current.enabled or current.desktop or current.owner != owner:
                            raise protocol.ProtocolError('lane changed during re-enrollment', Exit.REFUSED)
                        if self.store.one("SELECT 1 FROM attempts WHERE lane_id=? AND state IN "
                                          "('reserved','starting','running','finalizing','quarantined')", (row['lane_id'],)):
                            raise protocol.ProtocolError('lane still has a live or quarantined attempt', Exit.REFUSED)
                        if self.store.one("SELECT 1 FROM leases WHERE lease_key LIKE ?", (f"lane:{row['lane_id']}:slot:%",)):
                            raise protocol.ProtocolError('lane still has an execution lease', Exit.REFUSED)
                    for row in bindings:
                        self.store.acquire_lease(f"lane:{row['lane_id']}:slot:0", enrollment_holder)
            except BaseException:
                self.timers.active_holders.discard(enrollment_holder)
                raise
        try:
            try:
                adapter = get_adapter(credential.provider)
                if enrollment_holder:
                    from .adapters.claude import ClaudeAdapter
                    if isinstance(adapter, ClaudeAdapter):
                        adapter._runner = enrollment_runner(
                            adapter._runner, lambda argv, **kwargs: self._enrollment_turn(
                                existing['lane_id'], enrollment_holder, argv, **kwargs))
                info = adapter.enroll(credential)
            except AdapterError as exc:
                raise protocol.ProtocolError(str(exc), exc.code, exc.fix) from None
            if existing and existing.get('identity') and (
                    info.identity_status != 'verified' or info.identity != existing['identity']):
                raise protocol.ProtocolError('re-enrollment could not verify the existing account identity', Exit.REFUSED)
            if existing and info.account_key != existing['account_key']:
                raise protocol.ProtocolError('re-enrollment found a different account; use a separate credential reference', Exit.REFUSED)
            lane_id = self._next_lane_id(credential.provider)
            lane = Lane(lane_id, credential.provider, info.account_key, credential,
                        info.home or (str(path) if credential.kind == "home" else None), owner, False, True,
                        info.identity, info.label)
            with self.store.transaction("lane.enrolled", lane_id=lane_id, data={
                    "account_key": info.account_key, "kind": credential.kind, "owner": owner.value,
                    "label": info.label, "identity_status": info.identity_status,
                    "supersedes": existing['lane_id'] if existing else None}):
                # Keep the old binding fenced through publication, and re-read
                # facts under the same transaction that creates its successor.
                for row in bindings:
                    current = self.store.get_lane(row['lane_id'])
                    if (current is None or current.enabled or current.desktop or current.owner != owner
                            or current.account_key != row['account_key'] or current.identity != row['identity']
                            or current.credential.ref != credential.ref):
                        raise protocol.ProtocolError('lane changed during re-enrollment', Exit.REFUSED)
                    if self.store.one("SELECT 1 FROM attempts WHERE lane_id=? AND state IN "
                                      "('reserved','starting','running','finalizing','quarantined')", (row['lane_id'],)):
                        raise protocol.ProtocolError('lane still has a live or quarantined attempt', Exit.REFUSED)
                    if self.store.one("SELECT 1 FROM leases WHERE lease_key LIKE ? AND holder!=?",
                                      (f"lane:{row['lane_id']}:slot:%", enrollment_holder)):
                        raise protocol.ProtocolError('lane acquired an execution lease during re-enrollment', Exit.REFUSED)
                self.store.put_lane(lane, plan=info.plan, identity_status=info.identity_status)
                for reading in info.readings:
                    self.store.add_reading(dataclasses.replace(reading, lane_id=lane_id, attempt_id=None))
                # Re-authentication clears only the authentication latch. A new
                # binding to the same account cannot erase a hold or limit.
                for old in bindings:
                    for closure in self.store.list_closures(old['lane_id'], active_at=utcnow()):
                        if closure['reason'] != 'auth-dead':
                            self.store.add_closure(Closure(
                                lane_id, closure['scope'], closure['until_at'],
                                ClosureReason(closure['reason']), ClockSource(closure['clock_source']),
                                closure['source_event']))
            self._append_lanes_json(lane)
            self._notify()
            return {"enrolled": self.store.one("SELECT * FROM lanes WHERE lane_id=?", (lane_id,)),
                    "lanes": self.store.query("SELECT * FROM lanes ORDER BY lane_id")}
        finally:
            if enrollment_holder:
                self.timers.active_holders.discard(enrollment_holder)
                record = self._probe_record(enrollment_holder)
                if not record or record['state'] in ('contained', 'completed', 'reserved'):
                    self.store.release_leases(enrollment_holder)
                    if record:
                        shutil.rmtree(record['directory'], ignore_errors=True)

    def _enrollment_turn(self, lane_id, holder, argv, *, cwd, env, timeout, **_):
        """Run re-authentication through the recoverable guardian process fence.

        The adapter still parses its real stream. A restart contains the same
        recorded process before releasing its probe lease; no timer admission
        reading or lane identity is published from an incomplete enrollment.
        """
        directory = self.root / 'lanes' / lane_id / 'probes' / holder.rsplit(':', 1)[-1]
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        stdout, stderr = directory / 'stdout.txt', directory / 'stderr.txt'
        record = {'holder': holder, 'job_id': None, 'lane_id': lane_id,
                  'timer_kind': 'enroll', 'model_id': 'enrollment',
                  'directory': str(directory), 'state': 'reserved', 'created_at': utcnow(),
                  'owned_identities': {}, 'deadline_at': after(timeout)}
        self._save_probe(record)
        package_root = str(Path(__file__).resolve().parent.parent)
        env = {**env, 'SUBFLEET_ATTEMPT': holder, 'SUBFLEET_ROOT': str(self.root), 'SUBFLEET_PROBE': '1'}
        env['PYTHONPATH'] = package_root + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        read_fd, write_fd = procs.pipe_above_stdio()
        command = [sys.executable, '-m', 'subfleet.guardian', '--attempt-dir', str(directory),
                   '--cwd', cwd, '--stdout-path', str(stdout), '--stderr-path', str(stderr),
                   '--launch-fd', str(read_fd), '--', *argv]
        child = None
        try:
            child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     pass_fds=(read_fd,), close_fds=True, cwd=package_root)
            identity_deadline = time.monotonic() + 2
            started = procs.proc_start(child.pid)
            while not started and child.poll() is None and time.monotonic() < identity_deadline:
                time.sleep(.01)
                started = procs.proc_start(child.pid)
            if not started:
                raise procs.InspectionError('enrollment guardian identity is absent')
            record.update(state='starting', guardian_pid=child.pid, pgid=child.pid,
                          boot_id=procs.boot_id(), proc_start=started)
            self._save_probe(record)
            if not self.stopping.is_set():
                os.write(write_fd, b'1')
        finally:
            os.close(read_fd)
            os.close(write_fd)
        safe, receipt = self._await_probe(record, child)
        if not safe:
            raise AdapterError('enrollment containment is quarantined; its lane remains fenced', code=7)
        if not receipt:
            raise AdapterError('enrollment ended without an exit receipt', code=5)
        rc = receipt.get('rc')
        if rc is None:
            rc = -receipt['signal'] if receipt.get('signal') else 1
        return subprocess.CompletedProcess(argv, rc, _text(stdout), _text(stderr))

    def _hold_lane(self, a: protocol.LanesArgs) -> dict:
        """`lanes hold <lane> --until <iso>` records an operator closure on the account
        scope (C-9.6 `operator-hold`, clock `reported`); `lanes release <lane>` releases
        every open operator hold. A held lane is never a candidate (C-11.2) but is
        still probed, so a held account keeps being measured."""
        if not a.lane_id or not self.store.get_lane(a.lane_id):
            raise protocol.ProtocolError(f"lanes {a.action}: unknown lane {a.lane_id!r}",
                                         Exit.INVALID_INPUT, "subfleet lanes list")
        if a.action == "hold":
            until = (a.until or "").strip()
            try:
                instant = datetime.fromisoformat(until.replace("Z", "+00:00"))
            except ValueError:
                raise protocol.ProtocolError("lanes hold: --until must be an ISO 8601 instant",
                                             Exit.INVALID_INPUT) from None
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            until_utc = instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with self.store.transaction("lane.held", lane_id=a.lane_id, data={"until": until_utc}):
                self.store.add_closure(Closure(a.lane_id, "account", until_utc, ClosureReason.OPERATOR_HOLD,
                                               ClockSource.REPORTED, "operator"))
        else:
            with self.store.transaction("lane.released", lane_id=a.lane_id) as tx:
                tx.execute("UPDATE closures SET released_at=? WHERE lane_id=? AND reason='operator-hold' "
                           "AND released_at IS NULL", (utcnow(), a.lane_id))
        self._notify()
        view = self._capacity_view(self._desktop_identity())
        return {"held": a.lane_id if a.action == "hold" else None,
                "released": a.lane_id if a.action == "release" else None,
                "lanes": view["lanes"], "closures": view["closures"]}

    def _seed_lanes(self) -> None:
        path = self.root / "lanes.json"
        if not path.exists():
            atomic_publish(path, b"[]\n")
        roster = json.loads(path.read_bytes())
        if isinstance(roster, dict):
            roster = roster.get("lanes", [])
        for row in roster:
            if self.store.get_lane(row["lane_id"]):
                continue
            credential = row.get("credential") or {
                "provider": row["provider"], "ref": row["credential_ref"],
                "kind": row["credential_kind"], "epoch": row.get("credential_epoch", 1),
            }
            self.store.put_lane(Lane(
                row["lane_id"], row["provider"], row["account_key"], Credential(**credential),
                row.get("home"), LaneOwner(row.get("owner", "v2")),
                bool(row.get("desktop", False)), bool(row.get("enabled", True)),
                row.get("identity"), row.get("label"),   # C-10.6
            ), identity_status=row.get("identity_status"))

    def _boundary(self, name: str, job_id: str, attempt_id: str | None = None) -> None:
        if self.crash_hook:
            self.crash_hook(name, job_id, attempt_id)

    def _publish(self, role: str, path: Path, contents: bytes) -> None:
        if self.publish_hook:
            self.publish_hook(role, path)
        atomic_publish(path, contents)

    def _notify(self) -> None:
        self.wait_hub.poke()                # C-15.5: the waits' one reader looks now
        with self.changed:
            self.changed.notify_all()

    def _job(self, job_id: str) -> dict:
        row = self.store.get_job(job_id)
        if row is None:
            raise protocol.ProtocolError(f"unknown job {job_id}")
        return row

    def _spec(self, job: dict, **overrides: Any) -> JobSpec:
        names = {f.name for f in dataclasses.fields(JobSpec)}
        values = {k: v for k, v in job.items() if k in names}
        values.update(sandbox=Sandbox(job["sandbox"]), exclusions=tuple(json.loads(job["exclusions"])))
        values.update(network=bool((self.policy.get("network") or {}).get("codex_workspace_write", False)))
        # C-12.9: every attempt of the job, its first and each retry, starts the
        # servers its row names, from the copy submit kept beside it.
        servers = tuple(json.loads(job.get("mcp_servers") or "[]"))
        values.update(mcp_servers=servers, mcp_config=(
            str(self.root / "jobs" / job["job_id"] / claude_mcp.JOB_CONFIG_NAME) if servers else None))
        values.update(overrides)
        return JobSpec(**values)

    def _capacity_rows(self, *, route: bool = False) -> dict:
        """C-3.7: every row a capacity view is built from, read in one committed
        state off the store lock (inside a transaction, the transaction's own).

        `route` is for a route evaluation (`_pick`): it reads only the attempts
        and jobs a route can depend on (`ROUTE_ATTEMPTS`, `ROUTE_JOBS`, C-11.2),
        and `scheduler.evaluate` reaches the same decision over them as over
        every row (a differential property test). Status and the operator's
        views read every row.

        Only the reads: the view is built after the snapshot ends, so building
        it holds no read connection. Six views building at once used to hold
        all six, and every other read waited (review of 5841d8b, finding 2)."""
        with self.store.snapshot():
            lanes = self.store.lane_rows()
            rows = {"lanes": lanes, "readings": self.store.latest_reading_candidates(),
                    "closures": self.store.list_closures(),
                    "attempts": self.store.query(ROUTE_ATTEMPTS) if route else self.store.list_attempts(),
                    "jobs": self.store.query(ROUTE_JOBS if route else "SELECT * FROM jobs ORDER BY created_at,rowid")}
            if not route:
                rows["weekly_samples"] = self.store.weekly_projection_samples()
            # Probe reservations are explicit leases, not invented in-flight attempt
            # counts. A recovered probe keeps its lane unavailable until containment.
            leases = self.store.query(capacity.PROBE_LEASES)
            records = {row["holder"]: self._probe_record(row["holder"]) for row in leases}
            # C-6.3: the newest reading in this state. Every reading added after it
            # has a greater id while it stands (`_route_rows`).
            mark = self.store.one("SELECT * FROM readings ORDER BY reading_id DESC LIMIT 1")
            return {"view": rows, "probe_leases": leases, "probe_records": records,
                    "timers": self.timers.view_rows(lanes), "reading_mark": mark}

    def _capacity_view(self, desktop=None, rows: dict | None = None, now: datetime | None = None,
                       desktop_in_use: Any = _UNSET):
        rows = rows or self._capacity_rows()
        if desktop_in_use is _UNSET:
            desktop_in_use = self._desktop_in_use()
        view = capacity.build_view(**rows["view"], reading_ttl_s=self.policy["caps"]["reading_ttl_s"],
                                   desktop=desktop, now=now, desktop_in_use=desktop_in_use)
        view["desktop_in_use"] = desktop_in_use
        # `status.json` lays the same leases over the timer's snapshot (C-18.1).
        capacity.mark_probe_leases(view, rows["probe_leases"], rows["probe_records"].get)
        view = self.timers.enrich_view(view, rows["timers"])
        # C-6.14: then each lane's pilot, at the view's clock, as `_route_rows` lays them.
        turns = {row["job_id"] for row in view.get("jobs", ()) if row.get("kind") == "turn"}
        attempts = [{**row, "kind": "turn" if row.get("job_id") in turns else "detached"}
                    for row in view.get("attempts", ()) if row.get("state") in capacity.ACTIVE_ATTEMPT_STATES]
        return capacity.mark_pilots(view, self._pilot_marks(attempts, capacity._time(view["now"])))

    # --- lane answers and pilots (C-6.14) --------------------------------------

    def _pilot_marks(self, attempts, instant: datetime) -> dict[str, str]:
        """C-6.14: `capacity.pilot_marks` over `attempts` (rows with `attempt_id`,
        `lane_id`, `state` and `kind`) at `instant`, from what has answered by now."""
        settings = admission_settings(self.policy)
        return capacity.pilot_marks(attempts, answered=self._attempt_answers, lane_answers=self._lane_answers,
                                    now=instant.timestamp(), idle_s=settings["prove_idle_s"],
                                    wait_s=settings["prove_wait_s"])

    def _load_answers(self) -> tuple[dict[str, float], dict[str, float]]:
        """C-6.14: what the newest `lane.answered` events say: each lane's last answer,
        and which attempts still in flight have answered. A restart keeps what was
        proven, so it neither holds a lane that answered a minute ago nor takes an
        attempt that answered before it for a pilot."""
        lanes: dict[str, float] = {}
        attempts: dict[str, float] = {}
        live = {row["attempt_id"] for row in self.store.query(
            "SELECT attempt_id FROM attempts WHERE state IN ('reserved','starting','running','finalizing')")}
        for row in self.store.query("SELECT ts,lane_id,attempt_id FROM events WHERE kind=? "
                                    "ORDER BY event_id DESC LIMIT ?", (ANSWER_EVENT, ANSWER_SEED_ROWS)):
            when = _epoch(row["ts"])
            if when is None or not row["lane_id"]:
                continue
            lanes[row["lane_id"]] = max(lanes.get(row["lane_id"], when), when)
            if row["attempt_id"] in live:
                attempts.setdefault(row["attempt_id"], when)
        return lanes, attempts

    def _record_answer(self, lane_id: str, source: str, *, attempt: dict | None = None) -> None:
        """C-6.14: a model answered on `lane_id`, so the lane is proven now, and an
        attempt that answered is no pilot. Remembered first, then recorded as a
        `lane.answered` event (`source`: the attempt's `stream`, its `finalization`,
        an admission `probe`, a timer's `keepalive` or `heal`); an attempt's answer
        is recorded once. A store error is logged, never raised: the memory is
        what admission reads."""
        now = datetime.now(timezone.utc).timestamp()           # the clock views are built on
        attempt_id = attempt["attempt_id"] if attempt else None
        idle = admission_settings(self.policy)["prove_idle_s"]
        with self._answer_lock:
            if attempt_id is not None and attempt_id in self._attempt_answers:
                return
            lanes = dict(self._lane_answers)
            last = lanes.get(lane_id)
            lanes[lane_id] = now if last is None else max(last, now)
            self._lane_answers = lanes
            if attempt_id is not None:
                self._attempt_answers = {**self._attempt_answers, attempt_id: now}
            # C-6.10: only an answer on a lane that was not proven can free one. A
            # proven lane's every start answers too, and a look at every backed-off
            # wait for each would be the cost C-6.10 keeps timer probes out of.
            freed = idle is not None and (last is None or now - last >= idle)
            self._answer_news = self._answer_news or freed
        self._answer_reads.pop(attempt_id, None)
        try:
            self.store.add_event(ANSWER_EVENT, job_id=attempt["job_id"] if attempt else None,
                                 attempt_id=attempt_id, lane_id=lane_id, data={"source": source})
        except (sqlite3.Error, OSError) as exc:
            self.log.warning("lane %s answered (%s) but the record could not be written: %s",
                             lane_id, source, type(exc).__name__)
        if freed:
            self._notify()

    def _take_answer_news(self) -> bool:
        """C-6.10, C-6.14: whether a model answered since the detached pass last asked.
        A pilot's answer frees its lane as a released lease frees a slot, so the
        jobs waiting for it are looked at on the next pass, not on their backed-off
        clocks."""
        with self._answer_lock:
            news, self._answer_news = self._answer_news, False
        return news

    def _read_answer(self, a: dict, adir: Path) -> None:
        """C-6.14: read what a live detached attempt's stream added since the last look,
        at most every `ANSWER_READ_INTERVAL_S` and `ANSWER_READ_CHUNK` bytes at a time,
        and stop at the first event its adapter says shows the model answering
        (`Adapter.model_answered`). Only the stream file, opened only as a regular
        file; nothing here raises."""
        aid = a["attempt_id"]
        if aid in self._attempt_answers:
            return
        try:
            state = self._answer_reads.get(aid)
            now = time.monotonic()
            if state is None:
                lane = self.store.get_lane(a["lane_id"])
                state = {"path": self._saved_launch(a).stdout_path, "offset": 0, "tail": b"", "skip": False,
                         "next": 0.0, "adapter": get_adapter(lane.provider) if lane else None}
                self._answer_reads[aid] = state
            if state["adapter"] is None or now < state["next"]:
                return
            state["next"] = now + ANSWER_READ_INTERVAL_S
            settings = admission_settings(self.policy)
            wait, idle = settings["prove_wait_s"], settings["prove_idle_s"]
            if wait is not None and idle is not None and not state.get("lapsed") and age(a["reserved_at"]) >= wait:
                # C-6.14: said once per attempt, and only of a lane still unproven;
                # `capacity.pilot_marks` is what lets the lane go.
                state["lapsed"] = True
                last = self._lane_answers.get(a["lane_id"])
                if last is None or datetime.now(timezone.utc).timestamp() - last >= idle:
                    self.log.warning("attempt %s on lane %s has shown no model answering for %d s; it no longer "
                                     "holds the lane, which takes one more attempt (C-6.14)",
                                     aid, a["lane_id"], wait)
                    with self._answer_lock:                # the lane is free: the jobs held for it look now
                        self._answer_news = True
                    self._notify()
            with open_regular(state["path"]) as stream:
                size = os.fstat(stream.fileno()).st_size
                if size <= state["offset"]:
                    return
                stream.seek(state["offset"])
                data = stream.read(min(size - state["offset"], ANSWER_READ_CHUNK))
            state["offset"] += len(data)
            if state["skip"]:
                # The rest of a line longer than `ANSWER_LINE_MAX`, which is no event read whole.
                cut = data.find(b"\n")
                if cut < 0:
                    return
                data, state["skip"] = data[cut + 1:], False
            lines = (state["tail"] + data).split(b"\n")
            state["tail"] = lines.pop()
            if len(state["tail"]) > ANSWER_LINE_MAX:
                state["tail"], state["skip"] = b"", True
            for line in lines:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if state["adapter"].model_answered(event):
                    self._record_answer(a["lane_id"], "stream", attempt=a)
                    return
        except (FileNotFoundError, NotRegularFile):
            return                      # nothing written yet, or no stream to read
        except Exception as exc:        # never the attempt's trouble: its finalization still decides
            self.log.debug("attempt %s: its stream could not be read for an answer: %s", aid, type(exc).__name__)

    def _forget_answers(self, live: set[str]) -> None:
        """C-6.14: drop what was read and heard of attempts no longer in flight."""
        for aid in [aid for aid in self._answer_reads.copy() if aid not in live]:
            self._answer_reads.pop(aid, None)
        with self._answer_lock:
            if any(aid not in live for aid in self._attempt_answers):
                self._attempt_answers = {aid: when for aid, when in self._attempt_answers.items() if aid in live}

    @staticmethod
    def _answered(outcome: Outcome, provider_class: str) -> bool:
        """C-6.14: does a finished attempt's outcome show its model answering? Its
        adapter's `model_answered`, or an adapter verdict of `ok`, which no adapter
        reaches without an answer: a stream shape the predicate does not know must
        not leave a lane that serves every attempt forever one attempt wide."""
        return (outcome.evidence or {}).get("model_answered") is True or provider_class == OutcomeClass.OK.value

    def _session_rows(self) -> dict | None:
        """Claude Code's live-session registry, read at most every `REGISTRY_READ_TTL_S`.

        `rows` are the readable rows, each alive only while its pid is still the
        process that wrote it (`registry.validated`, against one process table
        read: a reused pid is not the session, review of the uncap plan);
        `unreadable` the live pids of row files that could not be read; `groups`
        each live process's group. None when the registry directory exists and
        cannot be listed: whoever reads that must treat it as unknown. An absent
        directory is no rows.
        """
        return self._registry_read()[1]["found"]

    def _desktop_in_use(self) -> bool:
        """C-10.3: whether Claude Code is using the desktop login now, as the latest
        registry read (`_registry_read`) found. Never written from here: read ops
        (`status`, `why`, `lanes`) ask too. A registry that cannot be read is use:
        this answer only ever keeps the desktop lane out."""
        return self._registry_read()[1]["in_use"]

    def _registry_read(self) -> tuple[float, dict]:
        """The registry as last read: the monotonic instant the read began, and
        what it found (`found`, see `_session_rows`) with the desktop answer it
        gives (`in_use`, `evidence`).

        One cache, one generation: the rows the priority classes read and the
        answer the desktop lane is judged by always come from the same read, and
        a read is kept only if none that began later was kept meanwhile, so a
        slow read never replaces a newer one, whatever either found (review of
        PR #72: with two caches, an older idle answer could be published after
        a newer read that found the registry unreadable)."""
        read_at, reading = self._registry
        # Reused for `REGISTRY_READ_TTL_S` after it finished, so a slow read (a
        # slow `ps`) is reused like any other; it is still aged from when it
        # began (`_desktop_answer`). Review of PR #72.
        if reading is not None and time.monotonic() - reading["finished"] < REGISTRY_READ_TTL_S:
            return read_at, reading
        started = time.monotonic()
        listed = registry.listing()
        if listed is None:
            found = None
        elif not listed.rows and not listed.unreadable:
            found = {"rows": [], "unreadable": [], "groups": {}}      # nothing to validate: no `ps`
        else:
            try:
                table = procs.snapshot()
                starts = {pid: row[3] for pid, row in table.rows.items() if table.live(pid)}
                rows = registry.validated(listed.rows, starts)
                unreadable = [pid for pid in listed.unreadable if pid in starts]
                groups = {pid: row[1] for pid, row in table.rows.items() if pid in starts}
            except procs.InspectionError:
                # No table: each row is judged on its pid alone, as read.
                rows = list(listed.rows)
                unreadable = [pid for pid in listed.unreadable if registry.pid_alive(pid)]
                groups = {}
            found = {"rows": rows, "unreadable": unreadable, "groups": groups}
        if found is None:
            in_use, evidence = True, {"error": "the Claude Code session registry could not be read"}
        else:
            owned_pids, owned_sessions = self._subfleet_processes(found)
            in_use, evidence = registry.desktop_login_in_use(
                found["rows"], now_ms=time.time() * 1000,
                recent_s=admission_settings(self.policy)["desktop_recent_s"],
                owned_pids=owned_pids, owned_sessions=owned_sessions, unreadable=len(found["unreadable"]))
        reading = {"found": found, "in_use": in_use, "evidence": evidence, "finished": time.monotonic()}
        with self._desktop_use_lock:
            if started >= self._registry[0]:
                self._registry = (started, reading)         # replaced whole: other threads read it
            else:
                started, reading = self._registry          # a read that began later was kept meanwhile
        return started, reading

    def _subfleet_processes(self, found: dict) -> tuple[frozenset[int], frozenset[str]]:
        """C-10.3: the registry rows' pids that belong to a live Subfleet attempt (its
        process group, or its child), and the sessions live attempts run. Those
        run on their lanes' own tokens, never the desktop login."""
        attempts = self.store.query("SELECT pgid,child_pid,native_session_id FROM attempts "
                                    "WHERE state IN ('reserved','starting','running','finalizing')")
        pgids = {row["pgid"] for row in attempts if row["pgid"]}
        children = {row["child_pid"] for row in attempts if row["child_pid"]}
        sessions = frozenset(str(row["native_session_id"]).lower() for row in attempts if row["native_session_id"])
        pids = frozenset(row.pid for row in found["rows"] if row.pid is not None
                         and (row.pid in children or found["groups"].get(row.pid) in pgids))
        return pids, sessions

    def _desktop_answer(self) -> bool | None:
        """C-10.3: the in-use answer a reservation reads inside its transaction.

        None before any read; True once the read it comes from began more than
        `DESKTOP_IN_USE_MAX_AGE_S` ago (the reservation refreshed it just
        before, so an answer that old means the refresh could not replace it):
        this answer only ever keeps the desktop lane out."""
        read_at, reading = self._registry
        if reading is None:
            return None
        return True if time.monotonic() - read_at > DESKTOP_IN_USE_MAX_AGE_S else reading["in_use"]

    def _record_desktop_use(self) -> None:
        """C-10.3: each change of the in-use answer, and the first after a start, is
        one `desktop.in_use` event carrying the rows that decided it, so a job
        placed on the desktop lane can be explained. Written by admission, which
        writes anyway, after each refresh it places by; never by a read op. Under
        its own lock, never the one `_registry_read` publishes under (C-3.7)."""
        with self._desktop_record_lock:
            # Read under the lock: two passes record, and a reading taken before
            # waiting for it could be older than one recorded meanwhile, a stale
            # flip on record (review of PR #72). Published readings only move
            # forward (`_registry_read`), so each record is the latest.
            _, reading = self._registry
            in_use, evidence = (reading["in_use"], reading["evidence"]) if reading else (None, None)
            if in_use is None or in_use == self._desktop_use_recorded:
                return
            self.store.add_event("desktop.in_use", data={"in_use": in_use, "was": self._desktop_use_recorded,
                                                         **(evidence or {}), "observed_at": utcnow()})
            self._desktop_use_recorded = in_use

    def _ancestors(self, job: dict | str, known: dict[str, frozenset[str]]) -> frozenset[str]:
        """C-6.9: a job's ancestors, its parent first, read by id and kept in `known`."""
        job_id = job if isinstance(job, str) else job["job_id"]
        if job_id in known:
            return known[job_id]
        found: list[str] = []
        row = self.store.one("SELECT parent_job_id FROM jobs WHERE job_id=?", (job_id,)) if isinstance(job, str) else job
        parent = row["parent_job_id"] if row else None
        while parent and parent not in found:
            found.append(parent)
            row = self.store.one("SELECT parent_job_id FROM jobs WHERE job_id=?", (parent,))
            parent = row["parent_job_id"] if row else None
        known[job_id] = frozenset(found)
        return known[job_id]

    def _priority_jobs(self, jobs: list[dict]) -> dict[str, dict]:
        """C-6.16: caller ancestry, including finished parents, read once per pass.

        No queries when priority is off. Fetch only named parents in indexed
        batches; missing rows and cycles terminate without scanning the store.
        """
        if not admission_settings(self.policy)["priority_callers"]:
            return {}
        family = {job["job_id"]: job for job in jobs}
        seen = set(family)
        pending = {job["parent_job_id"] for job in jobs if job.get("parent_job_id")} - seen
        while pending:
            parents = sorted(pending)
            seen.update(parents)
            pending = set()
            for start in range(0, len(parents), 500):
                chunk = parents[start:start + 500]
                for row in self.store.query(
                        "SELECT job_id,caller_session,parent_job_id FROM jobs "
                        f"WHERE job_id IN ({','.join('?' * len(chunk))})", tuple(chunk)):
                    family[row["job_id"]] = row
                    if row["parent_job_id"] and row["parent_job_id"] not in seen:
                        pending.add(row["parent_job_id"])
        return family

    def _liveness(self, jobs: list[dict]) -> scheduler.Liveness | None:
        """C-6.9: who is waiting on these jobs now (`scheduler.priority_class`).

        The Claude Code sessions a validated registry row names (its pid still the
        process that wrote it), and the parents that are not finished. Read once
        per pass, off the store lock but for one indexed query. A caller's pid
        alone is not asked: a live pid proves a process, not the caller (review of
        the uncap plan).
        """
        found = self._session_rows()
        if found is None:
            # Unknown, not empty: with no liveness to read every detached job is
            # `session` (C-6.9), rather than each live caller's job `background`
            # (review of PR #72).
            return None
        sessions = frozenset(row.session_id.lower() for row in found["rows"] if row.alive)
        parents = sorted({job["parent_job_id"] for job in jobs if job.get("parent_job_id")})
        live_jobs: set[str] = set()
        for start in range(0, len(parents), 500):
            chunk = parents[start:start + 500]
            live_jobs.update(row["job_id"] for row in self.store.query(
                f"SELECT job_id FROM jobs WHERE job_id IN ({','.join('?' * len(chunk))}) "
                "AND state IN ('queued','running','waiting')", tuple(chunk)))
        return scheduler.Liveness(sessions=sessions, jobs=frozenset(live_jobs))

    def _priority_callers_status(self) -> list[dict]:
        """C-6.16: configured callers, with registry names when available."""
        callers = admission_settings(self.policy)["priority_callers"] or ()
        if not callers:
            return []
        found = self._session_rows()
        rows = sorted((found or {}).get("rows", ()), key=lambda row: row.rank, reverse=True)
        names: dict[str, str] = {}
        for row in rows:
            if row.name:
                names.setdefault(row.session_id.strip().lower(), row.name)
        return [{"session_id": caller.strip().lower(), "name": names.get(caller.strip().lower())}
                for caller in callers]

    def _cached_desktop_identity(self) -> capacity.DesktopIdentity:
        """Read-only advisory identity: a warm profile or conservative cached hints."""
        last = capacity.last_desktop_identity(self.store.query(
            "SELECT * FROM events WHERE kind=? ORDER BY event_id DESC LIMIT 8",
            (capacity.DESKTOP_IDENTITY_EVENT,))) or {}
        cached_at, cached = self._desktop_cache
        profile = (cached if cached is not _UNSET and
                   time.monotonic() - cached_at <= self.policy['caps']['reading_ttl_s'] else None)
        return capacity.desktop_identity(profile, cached_label=capacity.read_desktop_account(),
                                         last_label=last.get('label'))

    def _desktop_identity(self) -> capacity.DesktopIdentity:
        """C-10.3: who the Claude desktop app is, asked of its own credential.

        The profile endpoint is asked at most once per reading window, and only
        when a desktop login is actually recorded for this `HOME` or one was
        verified here before: an install with no desktop app never reaches for a
        keychain item it has no reason to read. `~/.claude.json` rides along as
        the hint C-10.3 calls it, never as the authority, and the last verified
        identity is kept in the store so an unanswerable profile still has
        something to compare a lane's label against. With no Claude lane enrolled
        there is nothing for a desktop identity to decide, and none is asked for.
        """
        hint = capacity.read_desktop_account()   # the cached ~/.claude.json hint (C-10.3), never the authority
        last = capacity.last_desktop_identity(self.store.query(
            "SELECT * FROM events WHERE kind=? ORDER BY event_id DESC LIMIT 8",
            (capacity.DESKTOP_IDENTITY_EVENT,))) or {}
        desktop = capacity.desktop_identity(self._desktop_profile(hint or last),
                                            cached_label=hint, last_label=last.get("label"))
        if desktop.verified and (last.get("identity") != desktop.identity
                                 or last.get("label") != desktop.label):
            self.store.add_event(capacity.DESKTOP_IDENTITY_EVENT,
                                 data={"identity": desktop.identity, "label": desktop.label,
                                       "observed_at": utcnow()})
        return desktop

    def _desktop_profile(self, wanted: Any) -> Any:
        """The desktop credential's profile, asked at most once per window.

        Only the network answer is cached: `~/.claude.json` is re-read every
        cycle, so an operator switching the desktop app's login is seen at once
        (C-10.3) even while the profile answer is still warm.
        """
        window = self.policy["caps"]["reading_ttl_s"]
        cached_at, cached = self._desktop_cache
        if cached is not _UNSET and time.monotonic() - cached_at <= window:
            return cached
        profile = None
        if wanted and self.store.one("SELECT 1 FROM lanes WHERE provider='claude' LIMIT 1"):
            try:
                probe = self.desktop_prober
                if probe is None:
                    probe = getattr(get_adapter("claude"), "probe_desktop_profile", None)
                profile = probe() if probe is not None else None
            except (AdapterError, OSError, subprocess.SubprocessError) as exc:
                self.log.debug("desktop profile unavailable: %s", type(exc).__name__)
        self._desktop_cache = (time.monotonic(), profile)
        return profile

    def _record_identity(self, lane_id: str, outcome: Outcome | None) -> None:
        """C-10.6: keep the adapter's identity finding on the lane row.

        The adapter decides; the daemon only remembers, so the scheduler can
        refuse a lane whose own credential proved to hold another account and an
        operator can see why in `subfleet lanes`.
        """
        finding = (outcome.evidence or {}).get("identity") if outcome else None
        status = IDENTITY_STATUS_BY_EVIDENCE.get((finding or {}).get("status") or "")
        if status is None:
            return
        row = self.store.one("SELECT identity,label,identity_status FROM lanes WHERE lane_id=?",
                             (lane_id,))
        if row is None or row["identity_status"] == IdentityStatus.MISMATCH.value:
            # C-10.6: a lane whose credential proved to hold another account is
            # not a candidate "until an operator re-enrols it". No later probe
            # clears that, however the endpoint answers next time; only
            # enrolment does, and C-1.3 gives that a new lane id.
            return
        values: dict[str, Any] = {}
        if row["identity_status"] != status.value:
            values["identity_status"] = status.value
        observed = (finding or {}).get("identity") or {}
        pair = (f"{observed['account_uuid']}:{observed['org_uuid']}"
                if observed.get("account_uuid") and observed.get("org_uuid") else None)
        if status is IdentityStatus.VERIFIED and pair and not row["identity"]:
            # C-1.4: a lane that carried only a label learns the identity its own
            # credential reported, once, so the next cycle compares uuids.
            values["identity"] = pair
            if observed.get("email") and not row["label"]:
                values["label"] = observed["email"]
        if values:
            self.store.update_lane(lane_id, **values)

    @staticmethod
    def _identity_binds(outcome: Outcome | None) -> bool:
        """C-10.6: may this outcome's evidence become capacity for its lane?"""
        finding = (outcome.evidence or {}).get("identity") if outcome else None
        status = IDENTITY_STATUS_BY_EVIDENCE.get((finding or {}).get("status") or "")
        return status not in (IdentityStatus.MISMATCH, IdentityStatus.UNVERIFIED)

    def _pick(self, job: dict, *, extra_exclusions: tuple[str, ...] = (), desktop=None,
              basis: dict | None = None):
        # C-6.3, C-11: one pure evaluation, on rows read in one snapshot off the
        # store lock (C-3.7). Desktop file I/O and the desktop profile request
        # happen before it (C-3.3, C-10.3). `basis`, when given, is told what the
        # reserving transaction needs to check the decision without evaluating it
        # again (`_route_stands`): the policy, the job as evaluated, the view, the
        # rows it was built from, the overrides held out, each lane's horizon
        # (`capacity.lane_horizons`: the first instant the clock alone could
        # change how that lane is judged) and the desktop.
        policy = self.policy
        exclusions = job.get("exclusions") or ()
        if isinstance(exclusions, str):
            exclusions = json.loads(exclusions)
        rows = self._capacity_rows(route=True)
        # The instant the view is built at: it keeps closures and labels readings
        # on it, and gives `evaluate` its whole second (C-6.3's clock check).
        instant = datetime.now(timezone.utc)
        view = self._capacity_view(desktop, rows, now=instant)
        context = rows["timers"]["overrides"]          # read once, in the view's snapshot (C-3.7)
        # At the view's clock, as `enrich_view` decided them: two clocks could put
        # an override's end between them, its readings relabelled stale there and
        # not held out here (review of 4f4edcd).
        overrides = {lane["lane_id"]: found for lane in view["lanes"]
                     if (found := self.timers.actions.confirmed_override(lane["lane_id"], now=view["now"],
                                                                         context=context))}
        view["readings"] = [row for row in view["readings"] if row["lane_id"] not in overrides]
        route_job = {**job, "exclusions": tuple(exclusions) + extra_exclusions, "policy_hash": self.policy_digest}
        decision = scheduler.evaluate(policy, view, route_job)
        if basis is not None:
            # Off the lock, so the check inside it compares one clock per lane.
            clocks = capacity.lane_horizons(view, reading_ttl_s=policy["caps"]["reading_ttl_s"])
            basis.update(policy=policy, job=route_job, view=view, rows=rows, clocks=clocks, desktop=desktop,
                         desktop_in_use=view.get("desktop_in_use"), instant=instant, overrides={lane_id: (found["action_id"], found["weekly_reset_at"])
                                                     for lane_id, found in overrides.items()})
        return decision

    def _route(self, job: dict, **options):
        """C-6.12: `_pick` for admission. An evaluation that raises is this job's, not the pass's."""
        try:
            return self._pick(job, **options)
        except ROUTE_ERRORS as exc:
            raise Unroutable(exc) from exc

    def _needs_probe(self, decision, job: dict) -> bool:
        """C-6.12: `scheduler.probe_required`, which checks the job's authorization, for
        admission; and C-4.5, C-6.14: a job that has moved on from an `auth-dead` lane is
        never a pilot. On a lane not proven it waits for Subfleet's own probe (C-11.4),
        which runs on the lane's credential with none of the job's settings, so its
        `auth-dead` is the lane's (it disables the lane, C-23.44) and its answer proves
        the lane. Then a second `auth-dead` on a proven lane points at the job."""
        try:
            if scheduler.probe_required(decision, job):
                return True
        except ROUTE_ERRORS as exc:
            raise Unroutable(exc) from exc
        return bool(decision.chosen_lane and job.get("kind") != "turn"
                    and admission_settings(self.policy)["prove_idle_s"] is not None
                    and not self._lane_proven(decision.chosen_lane)
                    and self._moved_on_from(job["job_id"]))

    def _lane_proven(self, lane_id: str) -> bool:
        """C-6.14: has a model answered on `lane_id` within `admission.prove_idle_s`, now?"""
        idle = admission_settings(self.policy)["prove_idle_s"]
        last = self._lane_answers.get(lane_id)
        return idle is None or (last is not None and datetime.now(timezone.utc).timestamp() - last < idle)

    def _moved_on_from(self, job_id: str) -> int:
        """C-4.5: how many lane faults the job has moved on from (at most one)."""
        return sum(1 for row in self.store.query(
            "SELECT evidence_json FROM attempts WHERE job_id=? AND outcome_class='auth-dead'", (job_id,))
            if _lane_fault_of(row["evidence_json"]))

    def _retry_pin(self, job: dict) -> tuple[list[dict], tuple[str, ...], dict | None]:
        """C-4.5: a job's attempts, the lanes they exclude, and the one-time retry pin.

        A `limited` attempt excludes its lane, and so does a second `transient`
        one. After a first `transient` attempt the job is tried once more on the
        same lane and model (C-9.5), while that pair can run at all
        (`_retry_pair_routable`); the pin is a job dict carrying the pair.
        """
        previous = self.store.list_attempts(job["job_id"])
        roster = self._pin_roster() if previous else []
        follow = not job.get("unmeasured_reserve_reason")
        # Attempts on a lane and on its re-enrolled successor count as one lane.
        lane_of = {a["lane_id"]: scheduler.current_lane_id(roster, a["lane_id"], follow=follow) for a in previous}
        exclusions = tuple(dict.fromkeys(lane for a in previous if a["outcome_class"] == "limited"
                                         for lane in (a["lane_id"], lane_of[a["lane_id"]])))
        transient: dict[str, int] = {}
        for a in previous:
            if a["outcome_class"] == "transient":
                transient[lane_of[a["lane_id"]]] = transient.get(lane_of[a["lane_id"]], 0) + 1
        exclusions += tuple(dict.fromkeys(lane for a in previous if transient.get(lane_of[a["lane_id"]], 0) >= 2
                                          for lane in (a["lane_id"], lane_of[a["lane_id"]])))
        last = previous[-1] if previous else None
        pin = ({**job, "pinned_lane": last["lane_id"], "pinned_model": last["model_requested"]}
               if last and last["outcome_class"] == "transient" and transient[lane_of[last["lane_id"]]] == 1
               and self._retry_pair_routable(last) else None)
        return previous, exclusions, pin

    def _retry_waits_on_a_slot(self, retry: dict, exclusions: tuple[str, ...], desktop) -> bool:
        """C-4.5, C-6.12: is the retry's lane refusing it only for want of a slot?

        The pinned pair is evaluated as admission would. A lane that would take
        it, or that is only full (in flight, the fleet or a parent at its cap, a
        probe holding it), keeps the retry. Anything else (the lane closed, the
        desktop login, excluded by the job, a latched credential, the floor, the
        reserve, a pair that cannot be evaluated) is not something a slot will
        end, and the pair was the daemon's choice, so the job routes as submitted.
        """
        try:
            decision = self._route(retry, extra_exclusions=exclusions, desktop=desktop)
        except Unroutable:
            return False
        if decision.chosen_lane:
            return True
        rejections = [row for evaluation in decision.evaluations for row in evaluation["rejections"]]
        return len(rejections) == 1 and set(rejections[0]["reasons"]) == {"no-slot"} \
            and rejections[0].get("slot_block") != "credential-latched"

    def _earlier_transients(self, conn, job_id: str, attempt: dict) -> int:
        """C-4.5: the job's earlier transient attempts on this attempt's lane.

        A lane and its re-enrolled successor are one lane here (C-11.2), so a
        retry that followed a re-enrolment is not a first transient again.
        """
        roster = self._pin_roster()
        here = scheduler.current_lane_id(roster, attempt["lane_id"])
        return sum(1 for (lane_id,) in conn.execute(
            "SELECT lane_id FROM attempts WHERE job_id=? AND outcome_class='transient' AND attempt_id!=?",
            (job_id, attempt["attempt_id"])) if scheduler.current_lane_id(roster, lane_id) == here)

    def _retry_pair_routable(self, attempt: dict) -> bool:
        """C-4.5, C-6.12: could a transient attempt's lane and model be tried once more at all?

        A cheap roster and policy check, made on every pass because the pin sets
        the job's C-6.9 demand; `_retry_waits_on_a_slot` evaluates the pair when
        the job is due.

        The running policy must still resolve the model id (a current id or a
        `retired` alias), and the lane (or the lane a re-enrolment bound to its
        credential) must be enabled, v2-owned, not identity-blocked, and of that
        model's provider. A lane disabled since (auth-dead, a mismatch) is never
        re-enabled, and an id renamed or retired to another provider's model
        never resolves back: a retry pinned to either would never be placed.
        """
        try:
            short = resolve_model(self.policy, attempt["model_requested"], note=False)
            lane = scheduler.resolve_lane(self._pin_roster(), attempt["lane_id"])
        except ROUTE_ERRORS:                    # a PolicyError is a ValueError; the pass must not end here
            return False
        return bool(lane) and bool(lane.get("enabled", True)) and lane.get("owner") == "v2" \
            and not capacity.identity_blocked(lane) and self.policy["models"][short]["provider"] == lane["provider"]

    def _resume_lane(self, recorded: str, lane: Lane) -> bool:
        """C-12.3, C-11.2: may a resume recorded on `recorded` run on `lane`?

        Its own lane, or the lane a re-enrolment bound to the same credential
        (`resolve_lane` follows a disabled lane id there, as admission did): the
        native session lives under the credential's home, not under a lane id.
        """
        if recorded == lane.lane_id:
            return True
        try:
            found = scheduler.resolve_lane(self._pin_roster(), recorded, lane.provider)
        except scheduler.RouteError:
            return False
        return bool(found) and found["lane_id"] == lane.lane_id

    def _pin_roster(self) -> list[dict]:
        """C-11.2: the lanes a pin is resolved against, at submit, at recovery and in admission.

        The lanes `_pick` evaluates, as names go: each store row with its latest
        probe verdict merged in, exactly as `Timers.enrich_view` merges it into
        the capacity view, so a Codex lane carries the email its usage probe
        reported. On 2026-09-22 submit resolved against the bare store rows while
        admission resolved against the view. The readings are left out: a pin
        needs none, and a view costs about 50 ms on the live store, which every
        admission pass would pay.
        """
        return [{**row, **self.timers.metadata.get(row["lane_id"], {})} for row in self.store.lane_rows()]

    def submit(self, args: protocol.SubmitArgs, *, turn: dict | None = None) -> dict:
        # Called on a filesystem worker, never on the socket reader pool.
        # `turn` is the dispatcher's own block (C-26.1); nothing else passes it.
        if (args.kind == "turn") != (turn is not None):
            raise AdapterError("a turn job needs its conversation's turn block", code=7)
        with self._submit_lock:
            reason = args.unmeasured_reserve_reason
            if reason is not None:
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                    raise protocol.ProtocolError("unmeasured_reserve_reason must contain 1 to 2000 characters of reason/evidence")
                if args.kind != "dispatch" or not args.pinned_lane or not args.pinned_model:
                    raise protocol.ProtocolError("unmeasured reserve authorization requires a fresh dispatch with explicit pinned_lane and pinned_model")
            batch = self._batch_label(args.batch)
            resume = None
            # C-26.13: a resume or revive continues a native session, and a
            # conversation's session is continued only by its conversation, so
            # the twin the `native:` lease would merely serialize is refused
            # here. A resume's session is its source attempt's; a revive's is
            # `caller_session`.
            fence: tuple[str | None, str] | None = None
            resume_workspace = None
            retire_fence = None
            if args.kind == "resume":
                # d635: while the resume reads its source's job directory, and
                # until its own row pins the source (C-8.4 `parent`), retention
                # may not start retiring the source (review Astra 9). Not
                # `fence`, which is C-26.13's session fence just above.
                retire_fence = self._fence_resume(args)
                args, resume = self._resume_submission(args)
                # Where the resume starts is the source's, not the request's: it
                # stays out of the digest, so a resume retried across an upgrade
                # that began recording it is still the same request (C-6.2).
                resume_workspace = resume.pop("workspace", None)
                fence = (resume["native_session_id"], "resume")
            elif args.kind == "revive":
                fence = (args.caller_session, "revive")
            # C-6.2 before C-26.13: a session can become a conversation's after
            # its resume or revive was accepted (`conversation.open` with
            # `native` binds it at once), so a retry of that request is
            # answered from its job below, as C-6.2 answers every retry, and
            # admission fails the job if it has not launched. Only a request
            # the daemon has not accepted is refused here, before the rest of
            # validation, so the refusal is what a new request hears.
            accepted = bool(fence) and self._accepted_request(args.request_id)
            if fence and not accepted:
                self._refuse_conversation_session(*fence)
            # An empty task or tier is none: `evaluate` would reject '' on every
            # pass of a job submit had accepted (C-6.12).
            args = dataclasses.replace(args, task=args.task or None, tier=args.tier or None)
            from_policy = args.sandbox == protocol.POLICY_SANDBOX
            if from_policy:
                args = dataclasses.replace(args, sandbox=self._policy_sandbox(args))
            try:
                ids.request_id(args.request_id)
                sandbox = Sandbox(args.sandbox)
                workdir = Path(args.workdir).expanduser().resolve(strict=True)
                if not workdir.is_dir():
                    raise ValueError("workdir must be a directory")
                review_root = None
                if args.isolated_review:
                    from .adapters.isolation import validate_isolated_review
                    validate_isolated_review(sandbox, args.review_root)
                    review_root = str(Path(args.review_root).expanduser().resolve(strict=True))
                    if not Path(review_root).is_dir():
                        raise ValueError("review_root must be a directory")
                elif args.review_root:
                    raise ValueError("review_root requires isolated_review (-I)")
                if args.kind == "gate-review":
                    import re
                    if not args.pinned_model or not args.isolated_review:
                        raise ValueError("gate-review requires a pinned model and isolated_review")
                    if not isinstance(args.round_lease, str) or not re.fullmatch(
                            r"gate:[A-Za-z0-9][A-Za-z0-9._-]{0,127}:round:[1-9][0-9]*", args.round_lease):
                        raise ValueError("gate-review requires gate:<gate id>:round:<n> round_lease")
                    if self.root not in workdir.parents or git_head(workdir, timeout_s=self.policy["caps"]["workspace_git_timeout_s"]) is not None:
                        raise AdapterError("gate review cwd must be a neutral directory under the state root",
                                           fix="allocate the peer cwd under SUBFLEET_HOME outside any repository")
                elif args.round_lease:
                    raise ValueError("round_lease is reserved for gate-review jobs")
                if any(workdir == p or p in workdir.parents for p in (Path("/tmp"), Path("/private/tmp"))) and not args.allow_tmp:
                    raise AdapterError("workdir is under /tmp", fix="pass --allow-tmp or use a durable workdir")
                # Only a regular file, never waiting in open(): a FIFO (or a device, which
                # never ends) named here held this submit, `_submit_lock` and every submit after.
                prompt = read_regular(Path(args.prompt_path).expanduser())
                out = str(Path(args.out_path).expanduser().resolve()) if args.out_path else None
                if out and not Path(out).parent.is_dir():
                    raise ValueError("output directory must exist")
                if sandbox == Sandbox.WORKSPACE_WRITE and args.in_place and not (turn and turn.get("allow_main")):
                    # C-13.2: the refusal is about where the job writes. A job that
                    # is not in place writes in a detached worktree the daemon cuts
                    # for it (C-6.6), wherever its caller happens to stand. A
                    # conversation a person allowed on main is exempt (C-26.10).
                    validate_writable_workdir(workdir, timeout_s=self.policy["caps"]["workspace_git_timeout_s"])
                head = git_head(workdir, timeout_s=self.policy["caps"]["workspace_git_timeout_s"])
                if sandbox == Sandbox.WORKSPACE_WRITE and head is None and turn is None:
                    # C-26.10: an attended conversation may work outside git.
                    if from_policy:
                        # d261: never downgraded silently; the caller chooses.
                        where = ("this repository has no commit yet"
                                 if git_toplevel(str(workdir), timeout_s=self.policy["caps"]["workspace_git_timeout_s"])
                                 else "this directory is not a git repository")
                        raise AdapterError(f"{args.task or 'these'} jobs write by policy, and {where}",
                                           fix="commit a baseline or run it from a repository, or pass -s read-only "
                                               "to run it here without writing")
                    raise AdapterError("writable jobs require a committed git repository", fix="initialize a feature branch and commit a baseline")
                # C-6.5: an in-place job's hold is its checkout, not the directory
                # named by -C, so `/repo` and `/repo/sub` are one place to write. It is
                # spelled one way (`folders.canonical`, as a conversation's workspace
                # is): a folder outside git kept the case it was typed in, so
                # `~/Scratch` and `~/scratch` were two keys for one folder.
                write_target = (folders.canonical(git_toplevel(workdir, timeout_s=self.policy["caps"]["workspace_git_timeout_s"]) or workdir)
                                if sandbox == Sandbox.WORKSPACE_WRITE and args.in_place else None)
                # C-8.4, C-13.4: a read-only turn's folder, the same place a writable one's
                # target is, so retention can tell it is in use (`folders.READER`).
                read_folder = (folders.canonical(git_toplevel(workdir, timeout_s=self.policy["caps"]["workspace_git_timeout_s"]) or workdir)
                               if turn is not None and write_target is None else None)
                model = args.pinned_model
                if model:
                    model = resolve_model(self.policy, model)
                if args.task and args.task not in self.policy["chains"]:
                    raise ValueError(f"unknown task {args.task}")
                if args.tier and args.tier not in self.policy["tiers"]:
                    raise ValueError(f"unknown tier {args.tier}")
                if not model and not args.task and not args.pinned_lane:
                    raise ValueError("submit requires pinned_model, pinned_lane or task")
                # C-12.9: the MCP servers first, so the pin and the chain below see
                # a job that runs only where a launch starts them, on Claude.
                mcp_servers = claude_mcp.validate_names(args.mcp_servers or ())
                # C-11.2: resolved as admission resolves it (same lanes, and the
                # job's provider narrows a name two providers share), then kept as
                # the lane id, so no later roster change can make it ambiguous.
                lane = self._resolve_pin(args, model, reason) if args.pinned_lane else None
                pinned_lane = lane.lane_id if lane else None
                # The request digest keeps the pin as the caller wrote it, so a
                # retry is compared with what it asked for, also across a roster
                # change or this upgrade. Operator authorization was always bound
                # to the enrolled lane's immutable id, and its digest still is.
                digest_pin = pinned_lane if reason is not None else args.pinned_lane
                if model:
                    task_model = model
                elif args.task:
                    chain = scheduler.mcp_chain(self.policy, self.policy["chains"][args.task][
                        self.policy["tiers"].index(args.tier or "standard"):], {"mcp_servers": mcp_servers})
                    task_model = chain[0] if chain else None
                else:
                    task_model = next((k for k, v in self.policy["models"].items() if v["provider"] == lane.provider), None)
                if mcp_servers:
                    self._refuse_mcp(mcp_servers, sandbox, task_model, lane, turn)
                if task_model is None:
                    raise ValueError(f"pinned_lane: policy has no model for provider {lane.provider}")
                provider = self.policy["models"][task_model]["provider"]
                if lane and lane.provider != provider:
                    raise ValueError("pinned lane and model providers disagree")
                if lane:
                    self._validate_home(lane)
                mcp_found = self._mcp_entries(args, mcp_servers, workdir, resume) if mcp_servers else None
                caps = self.policy["caps"]
                max_attempts = args.max_attempts if args.max_attempts is not None else caps["max_attempts"]
                max_wall_s = args.max_wall_s if args.max_wall_s is not None else caps["max_wall_s"]
                if not isinstance(max_attempts, int) or not 1 <= max_attempts <= caps["max_attempts"]:
                    raise ValueError("max_attempts must be positive and within policy caps")
                if not isinstance(max_wall_s, (int, float)) or not 0 < max_wall_s <= caps["max_wall_s"]:
                    raise ValueError("max_wall_s must be positive and within policy caps")
                digest = ids.payload_digest(prompt, workdir=str(workdir), workdir_head=head,
                    task=args.task, tier=args.tier, pinned_model=model, pinned_lane=digest_pin,
                    sandbox=sandbox.value, exclusions=args.exclusions, out_path=out,
                    allow_desktop=args.allow_desktop, policy_hash=self.policy_digest,
                    isolated_review=args.isolated_review, review_root=review_root,
                    round_lease=args.round_lease, resume=resume,
                    unmeasured_reserve_reason=reason, mcp_servers=mcp_servers)
                if turn is not None:
                    # C-6.2 for turns: the message digest, not HEAD or the policy
                    # hash, so a restart can always re-bind the job (review IR-1).
                    digest = turn["digest"]
            except SalvageError as exc:
                # C-6.8: nothing was submitted, so the caller retries; a timed-out
                # `rev-parse` must not read as "not a repository" or "not on main".
                self.log.warning("submit could not inspect %s: %s", args.workdir, exc)
                raise AdapterError(f"could not inspect the workdir: {exc}", code=int(Exit.OPERATIONAL),
                                   fix="submit again; raise caps.workspace_git_timeout_s if this repository is slow") from exc
            except (OSError, ValueError, TypeError) as exc:
                raise protocol.ProtocolError(str(exc)) from exc
            existing = self.store.one("SELECT * FROM jobs WHERE request_id=?", (args.request_id,))
            if existing:
                if existing["payload_digest"] != digest:
                    # Named, so a caller settling a lost answer (C-16.3) learns
                    # which job holds the id without another round trip.
                    raise protocol.ProtocolError("request id already used with a different "
                                                 f"payload by job {existing['job_id']}")
                return {"job_id": existing["job_id"], "request_id": args.request_id, "created": False,
                        **self._where_it_writes(existing["job_id"], existing["sandbox"])}
            if fence and accepted:
                # The accepted job is gone (retention pruned it since the check
                # above), so this request makes a new job after all.
                self._refuse_conversation_session(*fence)
            job_id = ids.job_id(args.name or args.task or model,
                                existing=[r["job_id"] for r in self.store.query("SELECT job_id FROM jobs")])
            jobdir = self.root / "jobs" / job_id
            values = dataclasses.asdict(args)
            for k in ("allow_tmp", "no_preamble", "dry_run", "batch", "pinned_provider"):
                values.pop(k)
            values.update(job_id=job_id, state="queued", payload_digest=digest,
                          workdir=str(workdir), workdir_head=head, out_path=out,
                          pinned_model=model, pinned_lane=pinned_lane, prompt_path=str(jobdir / "prompt.md"),
                          exclusions=json.dumps(sorted(args.exclusions)), policy_hash=self.policy_digest,
                          max_attempts=max_attempts, max_wall_s=max_wall_s, created_at=utcnow(),
                          mcp_servers=json.dumps(list(mcp_servers)))
            values["review_root"] = review_root
            if args.dry_run:
                return {"dry_run": True, "decision": dataclasses.asdict(self._pick(values, desktop=self._desktop_identity()))}
            # C-3.3: the instance and worktree questions need `ps` and `git`, so
            # they are answered here, under the submit lock and outside the
            # transaction; the transaction below re-checks with SQL alone.
            instance = (self._caller_instance(args.caller_pid)
                        if sandbox == Sandbox.WORKSPACE_WRITE and args.caller_session else None)
            if turn is None:
                by_policy = from_policy and sandbox == Sandbox.WORKSPACE_WRITE
                try:
                    cleared = self._writable_precheck(values, instance, write_target)
                except AdapterError as exc:
                    if not by_policy:
                        raise
                    raise _written_by_policy(exc, args.task) from exc
                try:
                    self._validate_conflicts(values, cleared, write_target)
                except AdapterError as exc:
                    # Only the writers' refusals: a read-only job would meet the
                    # others (a cancelled parent, a held output path) all the same.
                    if not by_policy or not self._read_only_clears(values, write_target):
                        raise
                    raise _written_by_policy(exc, args.task) from exc
            else:
                # C-26.1, IR-12: a turn waits for its workspace at admission (a
                # detached writer's `worktree:` lease, C-24.5), it is never refused here.
                cleared = None
            if mcp_servers and mcp_found is None:
                # Only a retry of an accepted request gets here without entries
                # (`_mcp_entries`), and only when its job was pruned meanwhile.
                raise AdapterError(f"--mcp {', '.join(mcp_servers)}: the workdir no longer offers them",
                                   code=int(Exit.INVALID_INPUT), fix="submit with a new request id")
            jobdir.mkdir(mode=0o700)
            self._publish("prompt", jobdir / "prompt.md", prompt)
            if mcp_servers:
                # C-12.9: the entries every attempt and resume of this job starts.
                self._publish("mcp-config", jobdir / claude_mcp.JOB_CONFIG_NAME,
                              json_bytes(mcp_found["document"]))
            manifest = {"job": values}
            if mcp_servers:
                manifest["mcp"] = mcp_found["sources"]
            if batch:
                manifest["batch"] = batch
            if resume:
                manifest["resume"] = resume
            if turn is not None:
                manifest["turn"] = turn
            note = b""
            if resume_workspace:
                # Review B-2: a resume starts where its source started (`_launch_dir`).
                manifest["workspace"] = resume_workspace
            if sandbox == Sandbox.WORKSPACE_WRITE and not args.in_place and turn is None and head is not None:
                # C-6.6: the worktree is cut at admission; where it will be and where
                # in it the job starts (the caller's place in the repository) are
                # known now.
                cap = self.policy["caps"]["workspace_git_timeout_s"]
                try:
                    top = git_toplevel(str(workdir), timeout_s=cap) or str(workdir)
                except SalvageError as exc:
                    # C-6.8: exit 1 with nothing stored, as the checks above; only
                    # the prompt has been written, under a job id nobody was given.
                    shutil.rmtree(jobdir, ignore_errors=True)
                    self.log.warning("submit could not inspect %s: %s", args.workdir, exc)
                    raise AdapterError(f"could not inspect the workdir: {exc}", code=int(Exit.OPERATIONAL),
                                       fix="submit again; raise caps.workspace_git_timeout_s if this repository is slow") from exc
                prefix = _git_prefix(str(workdir), cap) or os.path.relpath(os.path.realpath(workdir),
                                                                            os.path.realpath(top))
                if _outside(prefix):
                    prefix = "."        # outside the checkout as far as can be told: the top
                worktree = self.root / "worktrees" / job_id
                place = ""
                if prefix != ".":
                    held = _commit_holds_dir(top, head, prefix, cap)
                    template = {True: WORKSPACE_PLACE, False: WORKSPACE_PLACE_UNCOMMITTED}.get(held,
                                                                                            WORKSPACE_PLACE_UNCHECKED)
                    place = template.format(top=top, prefix=prefix, worktree=worktree, head=head[:12])
                    if held is False:
                        # Review B-4: the worktree will not hold it; start at the top.
                        prefix = "."
                manifest["workspace"] = {"worktree": str(worktree), "prefix": prefix}
                note = WORKSPACE_NOTE.format(worktree=worktree, top=top, head=head[:12], place=place).encode()
            preamble = sandbox == Sandbox.WORKSPACE_WRITE and not args.no_preamble
            if sandbox == Sandbox.WORKSPACE_WRITE:
                manifest["preamble"] = preamble
            if preamble or note:
                # Review B-3: `--no-preamble` drops the template, never the note on
                # where the job may write.
                prepared_path = jobdir / "prompt.prepared.md"
                self._publish("prompt-prepared", prepared_path,
                              (WRITE_PREAMBLE.encode() if preamble else b"") + note + prompt)
                manifest["prepared_prompt_path"] = str(prepared_path)
            self._publish("manifest", jobdir / "manifest.json", json_bytes(manifest))
            authorization = ({"unmeasured_reserve_authorization": {
                "lane_id": pinned_lane, "model_id": self.policy["models"][model]["id"],
                "reason": reason}} if reason is not None else None)
            submitted = {**(authorization or {}),
                         **({"pin": {"requested": args.pinned_lane, "lane_id": pinned_lane}}
                            if args.pinned_lane and args.pinned_lane != pinned_lane else {}),
                         **({"caller_instance": instance} if instance else {}),
                         **({"write_target": write_target} if write_target else {}),
                         **({"folder": read_folder} if read_folder else {}),
                         **({"batch": batch} if batch else {}),
                         **({"mcp": mcp_found["sources"]} if mcp_servers else {})}
            with self.store.transaction("job.submitted", job_id=job_id, data=submitted or None) as tx:
                # Recheck the parent in the same transaction as insertion so a
                # concurrent parent cancellation cannot leave an uncancelled child.
                if turn is None:
                    self._validate_conflicts(values, cleared, write_target)
                columns = ",".join(values)
                tx.execute(f"INSERT INTO jobs ({columns}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
                if retire_fence is not None:
                    tx.execute("DELETE FROM leases WHERE lease_key=? AND holder=?", retire_fence)
            self._notify()
            return {"job_id": job_id, "request_id": args.request_id, "created": True,
                    **self._where_it_writes(job_id, sandbox.value)}

    def _resolve_pin(self, args: protocol.SubmitArgs, model: str | None, reason: str | None) -> Lane:
        """C-11.2: the lane a submitted pin names, resolved once, as admission would.

        The job's provider (its model's, else its task's first model at its tier)
        narrows a name both providers answer to; with neither, the flag's does
        (`-a` a Claude account, `-H` a Codex home). A name given with `-a` or
        `-H` names a lane of that flag's provider only, so a model of the other
        is refused, as v1 refused `-a` with a Codex model; a lane id says its
        own provider. A retry of an accepted request (C-6.2) whose name no
        longer resolves to one lane is answered from the lane it was accepted
        on, and its digest then decides.
        """
        job_provider = scheduler.pin_provider(self.policy, {"pinned_model": model, "task": args.task, "tier": args.tier,
                                                            "mcp_servers": list(args.mcp_servers or ())})
        roster = self._pin_roster()
        flag = args.pinned_provider if args.pinned_provider in ("claude", "codex") else None
        if flag and not any(lane["lane_id"] == args.pinned_lane for lane in roster):
            if job_provider and job_provider != flag:
                raise ValueError(f"pinned_lane: {args.pinned_lane!r} was given as a {flag} lane "
                                 f"({'-a' if flag == 'claude' else '-H'}), and this job's model runs on "
                                 f"{job_provider}; pin a {job_provider} lane or its lane id")
            roster = [lane for lane in roster if lane["provider"] == flag]
        try:
            found = scheduler.resolve_lane(roster, args.pinned_lane, job_provider or flag,
                                           follow=reason is None)
        except scheduler.RouteError:
            found = None
            if not self._accepted_pin(args.request_id):
                raise
        found = found or self._accepted_pin(args.request_id)
        lane = self.store.get_lane(found["lane_id"]) if found else None
        if not lane:
            raise ValueError(f"unknown lane {args.pinned_lane}")
        return lane

    def _policy_sandbox(self, args: protocol.SubmitArgs) -> str:
        """d261, C-11.1: the sandbox a submit that named none gets. The policy's
        `permissions` entry for the task, else its `*` entry, else read-only; an
        isolated review and a gate round read only, whatever the policy says."""
        if args.isolated_review or args.kind == "gate-review":
            return Sandbox.READ_ONLY.value
        permissions = self.policy.get("permissions") or {}
        chosen = permissions.get(args.task or "") or permissions.get("*") or Sandbox.READ_ONLY.value
        return chosen if chosen in {s.value for s in Sandbox} else Sandbox.READ_ONLY.value

    def _accepted_request(self, request_id: Any) -> bool:
        """C-6.2: whether a job already holds `request_id`, so this submit is a retry."""
        return isinstance(request_id, str) and self.store.one(
            "SELECT 1 FROM jobs WHERE request_id=?", (request_id,)) is not None

    def _refuse_mcp(self, names: tuple[str, ...], sandbox: Sandbox, task_model: str | None,
                    lane: Lane | None, turn: dict | None) -> None:
        """C-12.9, C-6.5: a job that names MCP servers is refused unless a launch
        will start them: a writable Claude launch. It is never accepted without
        them, and a read-only job never gains them."""
        listed = ", ".join(names)
        if turn is not None:
            raise ValueError("a conversation turn names no MCP servers; its settings decide them (C-12.9)")
        if sandbox != Sandbox.WORKSPACE_WRITE:
            raise AdapterError(f"--mcp {listed}: a read-only job starts no MCP servers (C-12.9)",
                               fix="pass -s workspace-write for a job that needs them, or drop --mcp")
        provider = lane.provider if lane else (self.policy["models"][task_model]["provider"] if task_model else None)
        if provider != scheduler.MCP_PROVIDER:
            where = (f"this job runs on {provider}" if provider
                     else "this job's chain from its tier has no Claude model")
            raise AdapterError(f"--mcp {listed}: only a Claude launch starts MCP servers, and {where} (C-12.9)",
                               fix="pin a Claude model (-m opus, -m sonnet) or a --tier whose chain has one, "
                                   "or drop --mcp")

    def _mcp_entries(self, args: protocol.SubmitArgs, names: tuple[str, ...], workdir: Path,
                     resume: dict | None) -> dict | None:
        """C-12.9: the entries of the named servers and where each came from.

        Found as Claude Code would find them for the workdir (`claude_mcp`); a
        resume takes its source's entries unchanged instead. An unknown name is
        exit 2. A retry of an accepted request (C-6.2) needs no source lookup:
        its digest still gates the existing response, and its job already has
        its own entries, whatever its source files say now. None then.
        """
        if self._accepted_request(args.request_id):
            return None
        if resume is not None:
            return self._resumed_mcp(resume["source_job_id"], names)
        found = claude_mcp.resolve(workdir, names)
        return {"document": claude_mcp.config_document(found), "sources": claude_mcp.sources(found)}

    def _resumed_mcp(self, source_id: str, names: tuple[str, ...]) -> dict:
        """C-12.9: a resume starts its source's MCP servers, from the source's own copy."""
        path = self.root / "jobs" / source_id / claude_mcp.JOB_CONFIG_NAME
        try:
            servers = json.loads(read_regular(path, 4 << 20))["mcpServers"]
            entries = {name: servers[name] for name in names}
            if not all(isinstance(entry, dict) for entry in entries.values()):
                raise ValueError("an entry is not an object")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AdapterError(f"resume: the MCP servers {source_id} named ({', '.join(names)}) cannot be read "
                               f"from {path}: {exc}", fix="submit a fresh job naming them with --mcp") from None
        recorded = (self._read_json(self.root / "jobs" / source_id / "manifest.json") or {}).get("mcp")
        recorded = recorded if isinstance(recorded, dict) else {}
        return {"document": claude_mcp.config_document(entries),
                "sources": {name: recorded.get(name) or {"source": str(path)} for name in names}}

    def _accepted_pin(self, request_id: str) -> dict | None:
        """The lane an already accepted request was pinned to, when that is a lane id (C-6.2)."""
        row = self.store.one("SELECT pinned_lane FROM jobs WHERE request_id=?", (request_id,))
        return {"lane_id": row["pinned_lane"]} if row and row["pinned_lane"] and self.store.get_lane(row["pinned_lane"]) else None

    def _fence_resume(self, args: protocol.SubmitArgs) -> tuple[str, str] | None:
        """Hold `retire:<source>` for this resume, or refuse it while retention is
        retiring the source (d635). The job's insert releases the fence; one a
        refused or retried submit leaves is stale, and any resume fence found here
        is (submits are serialized by the submit lock), as is one retention finds
        older than `retention.FENCE_STALE_S`."""
        if not args.parent_job_id:
            return None
        key, holder = f"retire:{args.parent_job_id}", f"resume:{args.request_id}"
        with self.store.transaction("retention.fenced", data={"lease_key": key}) as tx:
            tx.execute("DELETE FROM leases WHERE lease_key LIKE 'retire:%' AND holder LIKE 'resume:%'")
            row = tx.execute("SELECT holder FROM leases WHERE lease_key=?", (key,)).fetchone()
            if row:
                raise AdapterError(f"resume refused: {args.parent_job_id} is being archived by retention",
                                   code=int(Exit.OPERATIONAL), fix="retry in a minute")
            tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)", (key, holder, utcnow()))
        return key, holder

    def _resume_submission(self, args: protocol.SubmitArgs) -> tuple[protocol.SubmitArgs, dict]:
        """Resolve the native session on its original lane before persisting a resume."""
        if not args.parent_job_id:
            raise protocol.ProtocolError("resume requires its source parent_job_id")
        source = self._job(args.parent_job_id)
        if source["kind"] == "turn":
            # C-26.3, C-26.13: a turn's session is its conversation's; the next
            # turn, not a detached resume, is how it continues.
            raise AdapterError(
                f"resume refused: {source['job_id']} is a conversation turn, and a "
                "conversation's session continues only in its conversation",
                code=7, fix=CONVERSATION_FIX)
        if source["state"] not in TERMINAL or self.store.one(
                "SELECT 1 FROM attempts WHERE job_id=? AND state IN "
                "('reserved','starting','running','finalizing','quarantined')", (source["job_id"],)):
            raise AdapterError("source job is still active or quarantined",
                               fix="wait for its completion or resolve containment before resuming")
        if source["isolated_review"]:
            raise AdapterError("isolated review cannot resume a contextual session",
                               fix="submit a fresh isolated review job")
        attempt = (self.store.get_attempt(source["accepted_attempt_id"])
                   if source["accepted_attempt_id"] else self.store.one(
                       "SELECT * FROM attempts WHERE job_id=? ORDER BY seq DESC LIMIT 1", (source["job_id"],)))
        if not attempt:
            raise AdapterError("source job has no provider attempt", fix="submit a fresh job")
        native = attempt["native_session_id"] or self._legacy_resume_identity(attempt)
        if not native:
            raise AdapterError("source attempt has no recorded native session", fix="submit a fresh job")
        # C-26.13: a detached job's session a person has since opened as a
        # conversation (`conversation.open` with `native`) is the conversation's.
        # `submit` refuses it, after C-6.2's retry check (`_accepted_request`).
        # A continuation belongs to the source execution workspace, even when
        # that was an allocated worktree containing uncommitted provider work.
        # Independent allows continuing a cancelled source without reviving its
        # parent's old cancellation request (C-7.3).
        # C-12.9: a resume starts exactly the MCP servers its source did (none
        # unless it named some); it cannot add, drop or swap one.
        recorded = tuple(json.loads(source.get("mcp_servers") or "[]"))
        try:
            asked = claude_mcp.validate_names(args.mcp_servers or ())
        except ValueError as exc:
            raise protocol.ProtocolError(str(exc)) from exc
        if asked and asked != recorded:
            raise AdapterError(f"a resume starts its source's MCP servers ({', '.join(recorded) or 'none'}), "
                               f"not {', '.join(asked)} (C-12.9)",
                               fix="resume without --mcp, or submit a fresh job naming the servers it needs")
        source_manifest = self._read_json(self.root / "jobs" / source["job_id"] / "manifest.json") or {}
        preamble = source_manifest.get("preamble")
        if preamble is None:
            # A source submitted before the manifest said: its prepared prompt did.
            preamble = (self.root / "jobs" / source["job_id"] / "prompt.prepared.md").is_file()
        args = dataclasses.replace(args, workdir=source["worktree"] or source["workdir"],
            sandbox=source["sandbox"], in_place=source["sandbox"] == "workspace-write",
            pinned_lane=attempt["lane_id"], pinned_model=attempt["model_requested"],
            task=source["task"], tier=source["tier"], allow_desktop=bool(source["allow_desktop"]),
            exclusions=json.loads(source["exclusions"] or "[]"),
            independent=True, allow_tmp=True, no_preamble=not preamble, mcp_servers=list(recorded))
        resume = {"source_job_id": source["job_id"], "source_attempt_id": attempt["attempt_id"],
                  "native_session_id": native, "lane_id": attempt["lane_id"],
                  "model_id": attempt["model_requested"]}
        prefix = self._source_launch_prefix(source, attempt, source_manifest)
        if prefix not in (None, "."):
            # Review B-2: the session was made in the source's place in its
            # worktree; Claude finds it, and its transcript, from that directory.
            resume["workspace"] = {"worktree": source["worktree"], "prefix": prefix}
        return args, resume

    def _source_launch_prefix(self, source: dict, attempt: dict, source_manifest: dict) -> str | None:
        """Where in its worktree the resumed attempt started: the `cwd` its launch
        recorded, or, for an attempt launched before that was kept, the place its
        manifest named. None outside an allocated worktree."""
        if not source["worktree"]:
            return None
        launch = self._read_json(self.root / "jobs" / attempt["attempt_id"] / "launch.json") or {}
        if isinstance(launch.get("cwd"), str):
            prefix = os.path.relpath(os.path.realpath(launch["cwd"]), os.path.realpath(source["worktree"]))
            return None if _outside(prefix) else prefix
        return (source_manifest.get("workspace") or {}).get("prefix")

    def _legacy_resume_identity(self, attempt: dict) -> str | None:
        """C-23.32: recover old Codex identity in memory, never rewriting imported evidence."""
        import re
        evidence = json.loads(attempt["evidence_json"] or "{}")
        lane = self.store.get_lane(attempt["lane_id"])
        if not evidence.get("imported") or not lane or lane.provider != "codex":
            return None
        uuid = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
        found = set()
        for artifact in self.store.list_artifacts(attempt["attempt_id"]):
            if artifact["role"] != "stderr":
                continue
            try:
                with open_regular(artifact["path"]) as stream:
                    text = stream.read(4_000_000).decode("utf-8", "replace")
                found.update(re.findall(r"(?m)^session id:\s*(" + uuid + r")\s*$", text))
            except OSError:
                continue
        if attempt["transcript_path"]:
            match = re.fullmatch(r"rollout-.+-(" + uuid + r")\.jsonl", Path(attempt["transcript_path"]).name)
            if match:
                found.add(match[1])
        return next(iter(found)) if len(found) == 1 else None

    @staticmethod
    def _batch_label(value: Any) -> dict | None:
        """C-17.7: what `run --batch` says about one of its jobs, validated."""
        if value is None:
            return None
        if not isinstance(value, dict):
            raise protocol.ProtocolError("batch must be an object with id, label, index and size")
        ident, label = value.get("id"), value.get("label")
        index, size = value.get("index"), value.get("size")
        if not isinstance(ident, str) or not 1 <= len(ident) <= 128:
            raise protocol.ProtocolError("batch.id must contain 1 to 128 characters")
        if not isinstance(label, str) or not 1 <= len(label) <= 80:
            raise protocol.ProtocolError("batch.label must contain 1 to 80 characters")
        if (not all(isinstance(n, int) and not isinstance(n, bool) for n in (index, size))
                or not 1 <= index <= size <= 256):
            raise protocol.ProtocolError("batch.index and batch.size must satisfy 1 <= index <= size <= 256")
        return {"id": ident, "label": label, "index": index, "size": size}

    def _batches(self, job_ids: list[str]) -> dict[str, dict]:
        """job id -> batch label, for the jobs that have one (C-17.7)."""
        if not job_ids:
            return {}
        marks = ",".join("?" for _ in job_ids)
        # `+kind`: look the jobs up by id (events_job). By kind, SQLite walked
        # every job.submitted event ever written on each `list` (C-3.7).
        rows = self.store.query(f"SELECT job_id,data_json FROM events WHERE +kind='job.submitted' "
                                f"AND job_id IN ({marks}) AND data_json LIKE '%\"batch\"%'", job_ids)
        found = {row["job_id"]: json.loads(row["data_json"]).get("batch") for row in rows}
        return {job_id: batch for job_id, batch in found.items() if batch}

    def _submitted(self, job_id: str) -> dict:
        """What `submit` recorded beside the job row: the caller instance and write target."""
        row = self.store.one("SELECT data_json FROM events WHERE job_id=? AND +kind='job.submitted' "
                             "ORDER BY event_id LIMIT 1", (job_id,))      # by job id, as `_batches` (C-3.7)
        return json.loads(row["data_json"] or "{}") if row else {}

    @staticmethod
    def _caller_instance(pid: Any) -> dict | None:
        """C-6.5: the submitting process as pid, boot id and start time (C-5.3),
        or None when that cannot be established; a bare pid can be reused."""
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        try:
            found = procs.identity(pid)
        except procs.InspectionError:
            return None
        return dataclasses.asdict(found) if found else None

    @staticmethod
    def _instance_alive(recorded: dict) -> bool | None:
        """True while the recorded process still runs, False once it is provably
        gone (no such pid, or the pid now belongs to a later process), None when
        the operating system would not say."""
        try:
            state = procs.liveness(int(recorded["pid"]), recorded["boot_id"], recorded["proc_start"])
        except (procs.InspectionError, KeyError, TypeError, ValueError):
            return None
        return True if state == "alive" else False if state == "dead" else None

    def _write_target(self, job: dict, workspace: str) -> str:
        """The worktree lease's subject: the checkout for an in-place job, the
        allocated worktree otherwise (C-6.5, C-6.6)."""
        if not job.get("in_place"):
            return workspace
        return self._submitted(job["job_id"]).get("write_target") or workspace

    def _writable_precheck(self, job: dict, instance: dict | None, write_target: str | None) -> frozenset[str]:
        """C-6.5: refuse a second writer in one worktree and a second live
        instance of one session; permit one instance any number of writable jobs
        when their worktrees differ (up to `caps.max_writable_per_session` when a
        policy sets it; none by default, C-6.4).

        Returns the session's live writable jobs this submission was cleared
        against, for the SQL-only re-check inside the inserting transaction.
        The 2026-09-04 incident this keeps refusing: a second live instance of a
        session re-dispatching the first one's work. Anything that cannot be
        identified is treated as that second instance.
        """
        if job["sandbox"] != "workspace-write":
            return frozenset()
        self._refuse_second_revive(job)      # the more specific refusal names itself first
        live = self.store.query(
            "SELECT job_id,kind,workdir,worktree,in_place,caller_session FROM jobs WHERE sandbox='workspace-write' "
            "AND state NOT IN ('succeeded','failed','cancelled','lost') AND job_id!=? ORDER BY created_at,rowid",
            (job["job_id"],))
        recorded = {row["job_id"]: self._submitted(row["job_id"]) for row in live}
        if write_target:
            cap = self.policy["caps"]["workspace_git_timeout_s"]
            for row in live:
                try:
                    held = {recorded[row["job_id"]].get("write_target"),
                            os.path.realpath(row["worktree"]) if row["worktree"] else None}
                    if row["in_place"] and not recorded[row["job_id"]].get("write_target"):
                        # Submitted before targets were recorded: ask git once.
                        held.add(git_toplevel(row["worktree"] or row["workdir"], timeout_s=cap) or row["workdir"])
                except SalvageError as exc:
                    raise AdapterError(f"could not inspect the worktree of {row['job_id']}: {exc}",
                                       code=int(Exit.OPERATIONAL), fix="submit again") from exc
                if write_target in held:
                    raise AdapterError(f"worktree {write_target} is held by a live writable job ({row['job_id']})",
                                       fix="wait for the current writer or choose another worktree")
        session = job.get("caller_session")
        if not session:
            return frozenset()
        mine = [row for row in live if row["caller_session"] == session]
        for row in mine:
            holder = row["job_id"]
            if job.get("kind") == "revive" or row["kind"] == "revive":
                # C-23.54: a revive IS another instance of the session it continues.
                raise AdapterError(f"session {session} has a live writable job ({holder}) and a revive is a second instance of it",
                                   fix=f"wait for {holder}, or kill it before reviving or dispatching")
            theirs = recorded[holder].get("caller_instance")
            if instance is not None and theirs == instance:
                continue
            if instance is None:
                raise AdapterError(f"session {session} has a live writable job ({holder}) and the submitting instance cannot be identified",
                                   fix=f"submit from the session's own shell (CLAUDE_PID), or wait for {holder}")
            alive = self._instance_alive(theirs) if theirs else None
            if (alive is True and instance.get("pid") == theirs.get("pid")
                    and instance.get("proc_start") == theirs.get("proc_start")):
                continue        # the same live caller, including a legacy boot timestamp
            if alive is False:
                continue        # its instance is gone: a resumed session is one instance, not two
            who = f"another live instance (pid {theirs['pid']})" if alive else "an instance that cannot be identified"
            raise AdapterError(f"session {session} already has a live writable job ({holder}) from {who}",
                               fix=f"dispatch from that instance, or wait for or kill {holder}")
        limit = policy_cap(self.policy["caps"], "max_writable_per_session")       # C-6.4: none by default
        if limit is not None and len(mine) >= limit:
            raise AdapterError(f"session {session} already holds {len(mine)} live writable jobs (caps.max_writable_per_session {limit})",
                               fix="wait for one to finish, or raise the cap in policy.json")
        return frozenset(row["job_id"] for row in mine)

    def _refuse_second_revive(self, job: dict) -> None:
        if job.get("kind") == "revive" and job.get("caller_session"):
            # C-23.55: one live revive per session. The lease below is taken in
            # the admission transaction, but admission treats a lease conflict as
            # a wait, and a revive that waits for its own twin is exactly the
            # 2026-09-04 incident dressed as patience. Refuse at submit instead,
            # so the second attempt is skipped rather than queued (C-6.5).
            session = job["caller_session"]
            lease = self.store.one("SELECT holder FROM leases WHERE lease_key=?",
                                   (revive_lease_key(session),))
            live = self.store.one(
                "SELECT job_id FROM jobs WHERE kind='revive' AND caller_session=? "
                "AND state NOT IN ('succeeded','failed','cancelled','lost')", (session,))
            if lease or live:
                holder = (lease or {}).get("holder") or (live or {}).get("job_id")
                raise AdapterError(
                    f"session {session} already has a live revive ({holder})", code=7,
                    fix=f"subfleet runs show {holder}, or kill it before reviving again")

    def _read_only_clears(self, job: dict, write_target: str | None) -> bool:
        """Whether the same job, read-only, would pass `_validate_conflicts`."""
        try:
            self._validate_conflicts({**job, "sandbox": Sandbox.READ_ONLY.value}, frozenset(), write_target)
        except AdapterError:
            return False
        return True

    def _validate_conflicts(self, job: dict, cleared: frozenset[str] = frozenset(),
                            write_target: str | None = None) -> None:
        if job.get("round_lease"):
            prefix = job["round_lease"].rsplit(":", 1)[0] + ":"
            lease = self.store.one("SELECT holder FROM leases WHERE substr(lease_key,1,?)=?",
                                   (len(prefix), prefix))
            active = self.store.one(
                "SELECT job_id FROM jobs WHERE substr(round_lease,1,?)=? AND job_id!=? "
                "AND state IN ('queued','waiting','running')",
                (len(prefix), prefix, job["job_id"]))
            if (lease and lease["holder"] != f"gate-round:{job['job_id']}") or active:
                raise AdapterError("gate already has a reserved peer round",
                                   fix="wait for or explicitly abandon the existing gate round")
        if job.get("parent_job_id"):
            parent = self._job(job["parent_job_id"])
            if parent["cancel_requested_at"] and not job.get("independent"):
                raise AdapterError("parent is cancelled", fix="submit an independent job")
            limit = policy_cap(self.policy["caps"], "max_child_jobs")                 # C-6.4: none by default
            count = (self.store.one("SELECT count(*) AS n FROM jobs WHERE parent_job_id=?", (parent["job_id"],))["n"]
                     if limit is not None else 0)
            if limit is not None and count >= limit:
                raise AdapterError("parent child budget exhausted (caps.max_child_jobs)",
                                   fix="use a new parent job, or raise or remove the cap in policy.json")
        self._refuse_second_revive(job)
        conflicts: list[tuple[str, Any, str]] = []
        if job.get("out_path"):
            conflicts.append(("out_path", job["out_path"], "use a different -o path or wait for its owner"))
            lease = self.store.one("SELECT * FROM leases WHERE lease_key=?", (f"out:{job['out_path']}",))
            if lease:
                raise AdapterError("output path is held by another job", fix="use a different -o path or resolve quarantine")
        if job["sandbox"] == "workspace-write":
            if job.get("in_place"):
                conflicts.append(("workdir", job["workdir"], "wait for the current writer or choose another worktree"))
                for folder in {job["workdir"], write_target or job["workdir"]}:
                    if self.store.one("SELECT * FROM leases WHERE lease_key=?", (folders.exclusive_key(folder),)):
                        raise AdapterError("worktree has a lease", fix="resolve its owner before reusing the workspace")
                    # C-6.5, C-24.5: a writable conversation turn holds its folder with a
                    # row of its own (one live after its job ended, while its attempt is
                    # quarantined, too); a detached writer never joins it there.
                    turns = folders.turn_holds(self.store.query, folder, (folders.TURN,))
                    if turns:
                        raise AdapterError(f"worktree {folder} is being written by a conversation turn ({turns[0][1]})",
                                           fix="wait for the conversation's turn to end, or run the job in its own "
                                               "worktree (without --in-place)")
            if job.get("caller_session"):
                # C-6.5, SQL only (C-3.3): `_writable_precheck` judged every job
                # in `cleared`; one that is not there was never judged.
                for row in self.store.query(
                        "SELECT job_id FROM jobs WHERE caller_session=? AND sandbox='workspace-write' AND job_id!=? "
                        "AND state NOT IN ('succeeded','failed','cancelled','lost')",
                        (job["caller_session"], job["job_id"])):
                    if row["job_id"] not in cleared:
                        raise AdapterError(f"session {job['caller_session']} has a live writable job ({row['job_id']}) this submission was not checked against",
                                           fix="submit again")
        for column, value, fix in conflicts:
            writable = " AND sandbox='workspace-write'" if column != "out_path" else ""
            if self.store.one(f"SELECT job_id FROM jobs WHERE {column}=? AND state NOT IN "
                              f"('succeeded','failed','cancelled','lost'){writable}", (value,)):
                raise AdapterError(f"{column} is held by a live job", fix=fix)

    @staticmethod
    def _validate_home(lane: Lane) -> None:
        if lane.provider != "codex" or lane.credential.kind != "home":
            return
        path = Path(lane.credential.ref).expanduser() / "auth.json"
        try:
            auth = json.loads(read_regular(path))                  # never waiting in open()
        except (FileNotFoundError, NotRegularFile):
            auth = None                                            # none there, as `is_file()` had said
        if auth is not None:
            if auth.get("OPENAI_API_KEY") or auth.get("auth_mode") in ("api_key", "apikey"):
                raise AdapterError("API-key home refused", fix="log this lane into a subscription account")

    @staticmethod
    def _guard_override(adapter, lane: Lane, workdir: str, recorder: Callable | None = None,
                        state_root: Path | None = None, full: bool = False):
        """C-14.2: run the Codex guard preflight for this launch.

        ``recorder`` (see ``_guard_recorder``) receives the verdict before it is
        judged, so a refusal is diagnosable from the attempt directory alone.
        ``state_root`` places the scratch home and the verdict markers under the
        daemon's own root (C-2.1) rather than whatever ``$SUBFLEET_HOME`` says.
        """
        # The Codex adapter lane exposes its binary as codex_bin. Registered
        # fake adapters launch their Python fixtures and do not expose it.
        binary = getattr(adapter, "codex_bin", None)
        if lane.provider != "codex" or binary is None:
            return None
        try:
            from .guard.preflight import preflight
        except ImportError:
            raise AdapterError("Codex guard preflight is not installed", code=7,
                               fix="install the Codex adapter and reviewed guard files") from None
        options = {"state_root": state_root} if state_root is not None else {}
        result = preflight(binary, home=lane.home or lane.credential.ref, workdir=workdir, **options)
        if recorder is not None:
            recorder(result)
        if not result.ok or not result.override:
            raise AdapterError(result.message, code=7, fix=result.fix or "rerun subfleet doctor")
        return result if full else result.override

    def _guard_recorder(self, lane: Lane, workdir: str, attempt_dir: Path) -> Callable:
        """Keep a preflight verdict's diagnostics beside the attempt (C-14.2).

        The verdict's kind, cached flag, elapsed time, probe pid, deadline, the
        request/response lines and the app-server stderr tail are published to
        ``<attempt dir>/guard-preflight.json`` and summarised in one daemon.log
        line. The record never carries credentials: the probe home holds only
        config.toml and hooks.json (C-10.5).
        """
        def record(result) -> None:
            data = result.record()
            data.update(lane_id=lane.lane_id, workdir=str(workdir), recorded_at=utcnow(),
                        attempt_dir=str(attempt_dir))
            self.log.info(
                "guard preflight %s lane=%s attempt=%s kind=%s ok=%s cached=%s elapsed=%ss pid=%s "
                "deadline=%ss version=%r executable=%s: %s",
                utcnow(), lane.lane_id, attempt_dir.parent.name + "/" + attempt_dir.name,
                result.kind or "-", result.ok, result.cached, result.elapsed_s, result.probe_pid,
                result.timeout_s, result.version, result.executable, result.message)
            try:
                attempt_dir.mkdir(mode=0o700, exist_ok=True)
                self._publish("guard-preflight", attempt_dir / "guard-preflight.json", json_bytes(data))
            except OSError as exc:
                self.log.error("guard preflight record for %s not written: %s",
                               lane.lane_id, type(exc).__name__)
        return record

    def dispatch(self, op: str, args: dict, arrived: float | None = None, *,
                 client_gone: Callable[[], bool] | None = None) -> dict:
        """Answer one op (C-16.2). `arrived` is when its request was read off the
        socket (`time.monotonic()`), for a `wait`, whose deadline runs from then;
        `client_gone`, from the socket, lets a long read stop early (C-16.7)."""
        if op == "pick":
            from . import picker
            a = protocol.coerce_args(protocol.PickArgs, args)
            view = self._capacity_view(self._cached_desktop_identity())
            view["lane_leases"] = self.store.query("SELECT lease_key,holder FROM leases WHERE lease_key LIKE 'lane:%'")
            return picker.rank(self.policy, view, **dataclasses.asdict(a))
        if op == "operations":
            from . import operations
            return operations.dispatch(self, protocol.coerce_args(protocol.OperationsArgs, args))
        if op in ("gate.start", "gate.poll", "gate.continue"):
            from .gate.service import dispatch
            return dispatch(self, op, args)
        if op == "submit":
            if (args.get("kind") == "turn" or str(args.get("request_id") or "").startswith("turn:")
                    or "turn" in args):
                raise AdapterError("conversation turns are created by the daemon, not submitted", code=7,
                                   fix="send a message with message.submit")
            return self.submit(protocol.coerce_args(protocol.SubmitArgs, args))
        if op == "list":
            a = protocol.coerce_args(protocol.ListArgs, args)
            sql, params = "SELECT * FROM jobs WHERE 1", []
            if a.mine is not None:
                sql += " AND caller_session=?"; params.append(a.mine)
            if a.running:
                sql += " AND state NOT IN ('succeeded','failed','cancelled','lost')"
            if a.request_id is not None:
                sql += " AND request_id=?"; params.append(a.request_id)   # C-16.3
            # C-26.12: a turn job is its conversation's, not detached work; it is
            # listed only when asked for, so `runs`, `wait --mine` and `wait --last`
            # never take one for a job this caller dispatched. A request id is not a
            # listing but a lookup of the one job carrying it, whatever its kind
            # (C-16.3), so the turn default does not apply to it.
            if a.kind is not None:
                if not isinstance(a.kind, str) or not a.kind:
                    raise protocol.ProtocolError("kind must name a job kind")
                sql += " AND kind=?"; params.append(a.kind)
            elif not isinstance(a.include_turns, bool):
                raise protocol.ProtocolError("include_turns must be true or false")
            elif not a.include_turns and a.request_id is None:
                sql += " AND kind<>'turn'"
            sql += " ORDER BY created_at DESC, rowid DESC"
            if a.last is not None:
                if not isinstance(a.last, int) or a.last < 0:
                    raise protocol.ProtocolError("last must be a nonnegative integer")
                sql += " LIMIT ?"; params.append(a.last)
            jobs = self.store.query(sql, params)
            batches = self._batches([row["job_id"] for row in jobs])
            return {"jobs": [{**row, "batch": batches[row["job_id"]]} if row["job_id"] in batches else row
                             for row in jobs]}
        if op == "show":
            a = protocol.coerce_args(protocol.ShowArgs, args)
            job = self._job(a.job_id)
            # Show also serves resume and inspection by unrelated sessions. The
            # CLI sends notice.ack for its own session after displaying a result;
            # this sessionless read must not consume another caller's notice.
            workspace = self.store.one(
                "SELECT ts,kind,data_json FROM events WHERE job_id=? AND kind IN "
                "('job.workspace_deferred','job.workspace_failed') ORDER BY event_id DESC LIMIT 1", (a.job_id,))
            # C-12.9: the row names the job's MCP servers; where each came from is in its manifest.
            mcp = ((self._read_json(self.root / "jobs" / a.job_id / "manifest.json") or {}).get("mcp")
                   if job.get("mcp_servers") not in (None, "[]") else None)
            return {"job": job, "batch": self._submitted(a.job_id).get("batch"), "mcp": mcp,
                    "workspace": ({"at": workspace["ts"], "event": workspace["kind"],
                                   **json.loads(workspace["data_json"])} if workspace else None),
                    "attempts": self.store.query("SELECT * FROM attempts WHERE job_id=? ORDER BY seq", (a.job_id,)),
                    "artifacts": self.store.query("SELECT artifacts.* FROM artifacts JOIN attempts USING(attempt_id) WHERE job_id=?", (a.job_id,)),
                    "notices": self.store.query("SELECT * FROM notices WHERE job_id=?", (a.job_id,))}
        if op == "wait":
            return self.wait(protocol.coerce_args(protocol.WaitArgs, args), arrived=arrived,
                             client_gone=client_gone)
        if op == "kill":
            return self.kill(protocol.coerce_args(protocol.KillArgs, args))
        if op == "lanes":
            a = protocol.coerce_args(protocol.LanesArgs, args)
            if a.action == "transfer":
                # Ownership changes only here, and it records an event (C-10.4,
                # plan amendment 8). The roster edits belong to the daemon
                # because the daemon owns the store (C-3.4).
                try:
                    result = lanes_transfer.transfer(
                        self.store, self.root, a.lane_id, a.owner,
                        dry_run=bool(a.dry_run), confirm_v1_edit=bool(a.confirm_v1_edit))
                except lanes_transfer.TransferError as exc:
                    raise protocol.ProtocolError(str(exc), exc.code, exc.fix) from None
                return {"transfer": result,
                        "lanes": self.store.query("SELECT * FROM lanes ORDER BY lane_id")}
            if a.action == "enroll":
                return self._enroll_lane(a)
            if a.action in ("hold", "release"):
                return self._hold_lane(a)
            return {"lanes": self._capacity_view(self._desktop_identity())["lanes"],
                    "leases": self.store.query("SELECT * FROM leases WHERE lease_key LIKE 'lane:%'")}
        if op == "readings":
            view = self._capacity_view(self._desktop_identity())
            return {"readings": view["readings"], "closures": view["closures"], "status": render.status(view)}
        if op == "why":
            a = protocol.coerce_args(protocol.WhyArgs, args)
            if a.job_id:
                return self._why_job(self._job(a.job_id))
            decision = dataclasses.asdict(self._pick(dataclasses.asdict(a), desktop=self._desktop_identity()))
            return {"decision": decision, "text": render.why(decision)}
        if op == "notice.list":
            a = protocol.coerce_args(protocol.NoticeListArgs, args)
            return {"notices": notice_rows(self.store.query, a.session_id, resolved=a.resolved)}
        if op == "notice.withdraw":
            return self._withdraw_notices(protocol.coerce_args(protocol.NoticeWithdrawArgs, args))
        if op.startswith("notice."):
            a = protocol.coerce_args(
                protocol.NoticeMarkArgs if op == "notice.mark" else
                protocol.NoticeAckArgs if op == "notice.ack" else protocol.NoticeArgs,
                args)
            # C-15.3: a negated id is a service notice, on every op that takes
            # ids back (`protocol.notice_row`), and `acknowledged` is terminal
            # in both tables: a notice is acknowledged once.
            targets = [protocol.notice_row(notice_id) for notice_id in a.notice_ids]
            answered: dict = {}
            if op == "notice.ack":
                # C-15.8: with fingerprints (`notices --ack`), a row is acknowledged
                # only while it is still the one listed, since ids are reused; the
                # answer says which were and which were kept.
                if a.fingerprints and len(a.fingerprints) != len(a.notice_ids):
                    raise protocol.ProtocolError(
                        f"notice.ack: {len(a.fingerprints)} fingerprints for {len(a.notice_ids)} ids; "
                        "give one per id, or none")
                if any(not isinstance(fingerprint, str) for fingerprint in a.fingerprints):
                    raise protocol.ProtocolError("notice.ack: every fingerprint is a string, as the listing gives it")
                unique: dict = {}                       # a repeated id: once, with its first fingerprint
                for index, notice_id in enumerate(a.notice_ids):
                    unique.setdefault(notice_id, a.fingerprints[index] if a.fingerprints else None)
                stamp, acknowledged = utcnow(), []
                with self.store.transaction("notice.acknowledged") as tx:
                    for notice_id, fingerprint in unique.items():
                        table, row_id = protocol.notice_row(notice_id)
                        if fingerprint is not None:
                            row = tx.execute(f"SELECT text, created_at FROM {table} WHERE notice_id=? AND session_id=?",
                                             (row_id, a.session_id)).fetchone()
                            if row is None or notice_fingerprint({"text": row[0], "created_at": row[1]}) != fingerprint:
                                continue
                        if tx.execute(f"UPDATE {table} SET state='acknowledged',acknowledged_at=? "
                                      "WHERE notice_id=? AND session_id=? AND state!='acknowledged'",
                                      (stamp, row_id, a.session_id)).rowcount:
                            acknowledged.append(notice_id)
                answered = {"acknowledged": acknowledged,
                            "kept": [notice_id for notice_id in unique if notice_id not in acknowledged]}
            if op == "notice.mark":
                # C-15.3's non-terminal states, for the delivery layers that are
                # not an acknowledgement: `offered` (a transport accepted the
                # bytes) and `surfaced` (a hook printed it). A mark never moves
                # a notice down `NOTICE_LADDER`: `acknowledged` is terminal, and
                # an offer read before a hook surfaced the notice cannot put it
                # back where the next hook would surface it again.
                if a.state not in ("offered", "surfaced", "acknowledged"):
                    raise protocol.ProtocolError(f"unknown notice state {a.state!r}")
                below = NOTICE_LADDER[:NOTICE_LADDER.index(a.state) + 1]
                movable = tuple(state for state in below if state != "acknowledged")
                stamp = utcnow()
                with self.store.transaction("notice." + a.state) as tx:
                    for table, row_id in targets:
                        tx.execute(
                            f"UPDATE {table} SET state=?,transport=COALESCE(?,transport),"
                            "offered_at=COALESCE(offered_at,?),"
                            "acknowledged_at=CASE WHEN ?='acknowledged' THEN ? ELSE acknowledged_at END "
                            f"WHERE notice_id=? AND session_id=? AND state IN ({','.join('?' * len(movable))})",
                            (a.state, a.transport, stamp, a.state, stamp, row_id, a.session_id, *movable))
            notices = self.store.query("SELECT * FROM notices WHERE session_id=? AND state IN ('pending','offered') ORDER BY notice_id", (a.session_id,))
            service = self.store.query("SELECT * FROM service_notices WHERE session_id=? AND state IN ('pending','offered') ORDER BY notice_id", (a.session_id,))
            about = self._pin_notice_jobs(service) if service else {}
            notices += [{**protocol.service_notice_on_wire(row), "job_id": about.get(row["notice_id"])} for row in service]
            return {"notices": notices, **answered}
        if op == "ping":
            text = args.get("text")
            text = "" if text is None else text
            if not isinstance(text, str):
                raise protocol.ProtocolError(f"ping: text must be a string, not {type(text).__name__} (C-15.8)")
            # C-15.8: a notice goes to the session named, else to the configured
            # operator session; there is no default inbox nobody reads. A blank
            # or non-string session names none, and whitespace is no text.
            named = args.get("session_id")
            named = named.strip() if isinstance(named, str) and named.strip() else None
            session = named or operator_session(self.policy)
            notice_id = None
            if text.strip():
                if not session:
                    raise protocol.ProtocolError(
                        "ping: no session named, and alerts.operator_session is not set; "
                        "a notice addressed to no session is never read (C-15.8)",
                        fix="name the session: subfleet ping --session <id> TEXT")
                with self.store.transaction("notice.pending", data={"session_id": session}) as tx:
                    cursor = tx.execute("INSERT INTO service_notices(session_id,text,state,created_at) VALUES(?,?,'pending',?)",
                                        (session, text, utcnow()))
                    notice_id = -cursor.lastrowid
                self._notify()
            return {"pong": True, "version": __version__, "session_id": session, "text": text, "notice_id": notice_id}
        if op == "sessions":
            return self.sessions(protocol.coerce_args(protocol.SessionsArgs, args))
        if op == "daemon.status":
            view = self._capacity_view(self._desktop_identity())
            return {**view, "status": render.status(view), "pid": os.getpid(), "version": __version__, "state_root": str(self.root),
                    "timers": self.timers.status(), "alerts": self.timers.alerts.active(),  # C-18.4
                    "claude_cards": self.timers.cards_view(),  # C-9.10
                    "active_attempts": self.store.one("SELECT count(*) n FROM attempts WHERE state IN ('reserved','starting','running','finalizing')")["n"],
                    "admission": self._admission_status(view),
                    "priority_callers": self._priority_callers_status(),
                    "descriptors": self._descriptor_status(),       # C-16.6, C-16.7
                    "read_pool": self.store.read_pool(),             # C-3.7
                    "wait_hub": self.wait_hub.status()}              # C-15.5
        raise protocol.ProtocolError(f"unknown op {op}")

    def _why_job(self, job: dict) -> dict:
        """C-6.11: `why <job>` always says where the job stands, decision or not.

        A job that admission has not evaluated has no decision row: it was held
        behind an older job, the fleet was full, or no pass has reached it. It
        is evaluated here instead, marked `evaluated-now`, so the answer is the
        routing it would get and the reason it has not had it.
        """
        row = self.store.one("SELECT decision_json,evaluated_at FROM decisions WHERE job_id=? ORDER BY decision_id DESC LIMIT 1", (job["job_id"],))
        decision, source = (json.loads(row["decision_json"]), "recorded") if row else (None, None)
        pending = job["state"] in ("queued", "waiting") and not job["cancel_requested_at"]
        route_error = refused = None
        if job["state"] == "failed":
            event = self.store.one("SELECT ts,kind,data_json FROM events WHERE kind IN "
                                   "('job.route_refused','job.pin_refused') AND job_id=? "
                                   "ORDER BY event_id DESC LIMIT 1", (job["job_id"],))
            if event and (row is None or event["ts"] >= row["evaluated_at"]):
                record = json.loads(event["data_json"])
                # C-11.8: a job failed for a pin no lane could ever admit says which and why.
                refused = (render.pin_failure(record, record.get("since")) if event["kind"] == "job.pin_refused"
                           else f"{record.get('error_type')}: {record.get('error')}")
        if decision is None and pending:
            try:
                decision, source = dataclasses.asdict(self._pick(job, desktop=self._desktop_identity())), "evaluated-now"
            except ROUTE_ERRORS as exc:
                # C-6.12: on 2026-09-22 this was swallowed and the answer read
                # "No decision recorded." for five jobs that stopped admission.
                route_error = f"{type(exc).__name__}: {exc}"
                self.log.debug("why %s: evaluation failed: %s", job["job_id"], type(exc).__name__)
        hold = self._holds.get(job["job_id"]) if pending else None
        wait = self._capacity_waits.get(job["job_id"]) if pending else None
        recheck = ({key: wait[key] for key in ("rechecks", "since", "checked_at", "label")} if wait else None)
        standing = {"job_id": job["job_id"], "kind": job["kind"], "state": job["state"], "tier": job["tier"],
                    "class": scheduler.priority_class(
                        job, None if job["kind"] == "turn" else self._liveness([job]), policy=self.policy,
                        jobs={} if job["kind"] == "turn" else self._priority_jobs([job])),
                    # C-26.12: a turn is named `turn-<conversation id>` (design §3).
                    "conversation_id": (job["name"][len("turn-"):] if job["kind"] == "turn"
                                        and str(job["name"] or "").startswith("turn-") else None),
                    "wait_reason": job["wait_reason"], "next_check_at": job["next_check_at"],
                    "hold": hold, "recheck": recheck, "decision_source": source,
                    "decided_at": row["evaluated_at"] if row else None}
        queue = render.why_queue(standing)
        # C-6.12: a refusal comes first; a decision recorded before it is the last walk it had.
        lines = [*queue, *([f"Refused at admission: {refused}"] if refused else [])]
        if decision:
            lines.append(render.why(decision))
        elif route_error:
            lines.append(f"Decision: none; this job's route could not be evaluated: {route_error}")
        elif not refused:
            lines.append("No decision recorded.")
        return {"decision": decision, "decision_source": source, "job": standing, "queue": queue,
                "route_error": route_error, "refused": refused, "text": "\n".join(lines)}

    def _admission_status(self, view: dict) -> dict:
        """C-6.11: what admission is holding and for how long, for `status`."""
        state = self._admission                       # one read: admission replaces it whole
        since = state["idle_since"]
        idle = None if since is None else round(time.monotonic() - since)
        return {"pending": state["pending"], "placed_at": state["placed_at"], "idle_for_s": idle,
                "idle_since": state["idle_since_at"], "reasons": dict(state["reasons"]),
                "open_lanes": capacity.open_lanes(view, self.policy["caps"]),
                # C-6.3, since the daemon started: reservation checks that kept the
                # early decision's lane (`reused`) or chose again from the lanes
                # that changed (`rechosen`); evaluations made again, off the lock,
                # after a check refused one (`again`), and why: the job's pin names
                # another lane now, an evaluation now would raise, or what the
                # check rests on is gone (`moved`), the clock was earlier than its
                # view's (`old`: it stepped back), or the check raised (`error`);
                # jobs left for the next pass after ROUTE_TRIES (`deferred`); and
                # the lanes checks judged again, whose rows changed, whose own
                # clock reached its horizon, or every lane of the walk when a
                # fleet or parent cap began or ended (`rejudged`).
                "route_evaluations": dict(self._route_evaluations)}

    # --- the sessions kit's store seam (C-23.33, C-23.35, C-23.55) ------------

    def _session_events(self, kinds: tuple[str, ...],
                        session_ids: set[str] | None) -> dict[str, dict]:
        """The newest event of each kind per session id, keyed `<kind>:<id>`.

        `event_id` rides along because the store stamps `ts` to the second, and
        retiring and unretiring a session inside one second is a thing an
        operator does; the row order is the only tiebreak that is always right.

        """
        marks = ",".join("?" for _ in kinds)
        latest: dict[str, dict] = {}
        # C-3.7: SQLite picks the rows. Every event of these kinds used to be
        # fetched and parsed in Python, once inside the `nudged` transaction.
        # For named sessions, only theirs; for all, only the newest per kind and
        # session. The loop below still applies every rule it always did. A
        # payload json_valid refuses (a NaN or an Infinity, which json.dumps
        # writes and json.loads reads, or no JSON at all) cannot be filtered in
        # SQL, so every such row of these kinds rides along (`events_not_json`,
        # normally none) and the loop decides it, as the old walk did.
        # CASE, not a WHERE term, keeps json_extract off a payload json_valid
        # refuses: SQLite does not promise to test WHERE terms in written order.
        session = "CASE WHEN json_valid(data_json) THEN json_extract(data_json,'$.session_id') END"
        unread = (f"UNION ALL SELECT event_id,kind,ts,data_json FROM events WHERE kind IN ({marks}) "
                  "AND NOT json_valid(data_json) ")
        if session_ids is not None:
            if not session_ids:
                return latest
            wanted = sorted(session_ids)
            sql = (f"SELECT event_id,kind,ts,data_json FROM events WHERE kind IN ({marks}) "
                   f"AND json_valid(data_json) AND {session} IN ({','.join('?' for _ in wanted)}) "
                   + unread + "ORDER BY event_id DESC")
            params = (*kinds, *wanted, *kinds)
        else:
            sql = (f"SELECT event_id,kind,ts,data_json FROM events WHERE event_id IN "
                   f"(SELECT max(event_id) FROM events WHERE kind IN ({marks}) AND json_valid(data_json) "
                   f"GROUP BY kind,{session}) " + unread + "ORDER BY event_id DESC")
            params = (*kinds, *kinds)
        for row in self.store.query(sql, params):
            try:
                data = json.loads(row["data_json"])
            except (TypeError, ValueError):
                continue
            session = data.get("session_id")
            if not isinstance(session, str) or (session_ids is not None
                                                and session not in session_ids):
                continue
            latest.setdefault(f"{row['kind']}:{session}",
                              {**data, "at": row["ts"], "event_id": row["event_id"]})
        return latest

    def _lane_session_ids(self) -> list[str]:
        """Every session id subfleet itself CREATED as a headless lane (C-23.31).

        A revive's attempt records the session it continued, not one it created —
        `resume_launch` is handed the operator's own session id. Counting those
        would mark every revived session a lane run permanently, and C-23.31
        makes a lane run un-nudgeable, un-listable and un-revivable: one revive
        would retire the session from the fleet for good.

        A turn is left out for the same reason and one more. A conversation
        opened on an existing session (`conversation.open` with `native`) runs
        its turns with `--resume <that session>`, so a turn's attempt can
        record a session Subfleet did not create; and a turn's session is not a
        headless lane run, so C-23.31's label, its reason ("headless lane run")
        and its fix (`subfleet runs show`) would all be wrong for it. Turn
        sessions are reported apart, as `conversation_sessions` (C-26.13), which
        the kit excludes with its own reason. Every other kind launches under a
        `--session-id` this daemon minted, so every other kind belongs here.
        """
        return sorted({row["native_session_id"] for row in self.store.query(
            "SELECT DISTINCT a.native_session_id FROM attempts a "
            "JOIN jobs j USING(job_id) "
            "WHERE a.native_session_id IS NOT NULL AND j.kind NOT IN ('revive','turn')")
            if row["native_session_id"]})

    def _conversation_session_ids(self) -> list[str]:
        """C-26.13: every session a conversation binds or a turn job ran.

        Both halves are needed. A conversation records its session only when
        its first turn settles (`ConversationService._on_outcome`), so until
        then the turn attempt is the only record of it; and a conversation
        opened on an existing session binds it before any turn has run. Nothing
        deletes a conversation row, so a bound session stays the conversation's;
        a turn's attempt row lasts until retention prunes its job (C-26.12).
        """
        ids = {row["native_session_id"] for row in self.store.query(
            "SELECT DISTINCT a.native_session_id FROM attempts a "
            "JOIN jobs j USING(job_id) "
            "WHERE a.native_session_id IS NOT NULL AND j.kind='turn'")}
        ids |= self.conversations.store.bound_sessions()
        # Review L1: one session, one spelling. A UUID is listed in the lower
        # case Claude Code names its transcript with, as well as as recorded, so
        # a reader comparing either way finds it.
        from .conversations.store import canonical_native
        ids |= {canonical_native(item) for item in ids if item}
        return sorted(item for item in ids if item)

    def _conversation_binding(self, session_id: str | None) -> str | None:
        """What makes `session_id` a conversation's (C-26.13), or None.

        Called with no main-store transaction open. `ConversationStore` holds
        its own lock only inside its own methods and has no reference to the
        main store, so taking it here, under `_submit_lock` or in the admission
        pass, adds no lock order.
        """
        if not session_id:
            return None
        conversation = self.conversations.store.binding(session_id)
        if conversation:
            return f"conversation {conversation}"
        # Review L1 and the review of 3c1a34e (finding 5): a turn attempt may have
        # recorded a UUID in upper case and the request name it in lower, or the
        # reverse; both sides are compared without regard to case.
        from .conversations.store import native_any_case
        match, params = native_any_case("a.native_session_id", session_id)
        row = self.store.one(
            f"SELECT a.job_id FROM attempts a JOIN jobs j USING(job_id) "
            f"WHERE {match} AND j.kind='turn' ORDER BY a.reserved_at LIMIT 1", params)
        return f"turn job {row['job_id']}" if row else None

    def _refuse_conversation_session(self, session_id: str | None, verb: str) -> None:
        """C-26.13: a resume or revive never continues a conversation's session."""
        binding = self._conversation_binding(session_id)
        if binding:
            raise AdapterError(
                f"{verb} refused: session {session_id} belongs to {binding}, and a "
                "conversation's session continues only in its conversation",
                code=7, fix=CONVERSATION_FIX)

    def sessions(self, args: protocol.SessionsArgs) -> dict:
        action = args.action or "state"
        if action == "state":
            wanted = {s for s in args.session_ids if isinstance(s, str) and s} or None
            latest = self._session_events(
                (NUDGE_EVENT, REVIVE_EVENT, RETIRE_EVENT, UNRETIRE_EVENT), wanted)
            leases = {row["lease_key"]: row["holder"] for row in
                      self.store.query("SELECT lease_key,holder FROM leases "
                                       "WHERE lease_key LIKE 'session:%:revive'")}
            state: dict[str, dict] = {}
            for session in sorted(wanted or {key.split(":", 1)[1] for key in latest}):
                retired = latest.get(f"{RETIRE_EVENT}:{session}")
                cleared = latest.get(f"{UNRETIRE_EVENT}:{session}")
                # Retirement is durable until the operator clears it, and both
                # halves are append-only, so the later ROW wins (C-23.35) —
                # by event_id, not by a second-precision timestamp.
                if retired and cleared and cleared["event_id"] > retired["event_id"]:
                    retired = None
                state[session] = {
                    "retired": retired,
                    "last_nudge": latest.get(f"{NUDGE_EVENT}:{session}"),
                    "last_revive": latest.get(f"{REVIVE_EVENT}:{session}"),
                    "revive_holder": leases.get(revive_lease_key(session)),
                }
            # C-26.3, D-17: listed, never nudged, revived or cold-swept.
            return {"sessions": state, "lane_sessions": self._lane_session_ids(),
                    "conversation_sessions": self._conversation_session_ids()}
        if action == "revived":
            if not args.session_id:
                raise protocol.ProtocolError("sessions revived: session_id is required")
            # C-23.39: retain the operator's model substitution as history.
            # Admission remains governed by the live lease (C-23.55).
            data = {"session_id": args.session_id, "dedupe_key": args.dedupe_key,
                    **args.detail}
            with self.store.transaction(audit_kind(REVIVE_EVENT), data=data) as tx:
                tx.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                           (utcnow(), REVIVE_EVENT, json.dumps(data, sort_keys=True)))
            return {"session_id": args.session_id, "recorded": True}
        if action in ("retire", "unretire"):
            if not args.session_id:
                raise protocol.ProtocolError(f"sessions {action}: session_id is required")
            kind = RETIRE_EVENT if action == "retire" else UNRETIRE_EVENT
            data = {"session_id": args.session_id, "reason": args.reason, **args.detail}
            with self.store.transaction(audit_kind(kind), data=data) as tx:
                tx.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                           (utcnow(), kind, json.dumps(data, sort_keys=True)))
            return {"session_id": args.session_id, "action": action, "recorded": True}
        if action == "nudged":
            if not args.session_id:
                raise protocol.ProtocolError("sessions nudged: session_id is required")
            if self._conversation_binding(args.session_id):
                # C-26.3, C-26.13, D-17: a nudge would be a second writer in the
                # session of a conversation, or of a turn that ran there before its
                # conversation recorded the session, in either spelling.
                return {"recorded": False, "session_id": args.session_id,
                        "reason": "bound to a Subfleet conversation, which continues it (C-26.3)"}
            # C-23.33's dedupe and cooldown are re-checked HERE, inside the
            # transaction that records the nudge, so two sweeps racing over one
            # session cannot both reserve it. The worker has already decided
            # eligibility against the transcript (C-23.34); this is the lock.
            with self.store.transaction(audit_kind(NUDGE_EVENT),
                                        data={"session_id": args.session_id}) as tx:
                previous = self._session_events((NUDGE_EVENT,), {args.session_id}).get(
                    f"{NUDGE_EVENT}:{args.session_id}")
                if previous and not args.force:
                    if args.dedupe_key and previous.get("dedupe_key") == args.dedupe_key:
                        return {"recorded": False, "session_id": args.session_id,
                                "reason": "already nudged at this interruption point",
                                "last_nudge": previous}
                    cooldown = args.cooldown_s
                    if cooldown and previous.get("at"):
                        elapsed = age(previous["at"])
                        if elapsed < float(cooldown):
                            return {"recorded": False, "session_id": args.session_id,
                                    "reason": (f"nudged {int(elapsed)}s ago "
                                               f"(cooldown {int(float(cooldown))}s)"),
                                    "last_nudge": previous}
                data = {"session_id": args.session_id, "dedupe_key": args.dedupe_key,
                        "kind": args.kind, **({"forced": True} if args.force else {}),
                        **args.detail}
                tx.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                           (utcnow(), NUDGE_EVENT, json.dumps(data, sort_keys=True)))
            return {"recorded": True, "session_id": args.session_id,
                    "dedupe_key": args.dedupe_key, "kind": args.kind}
        raise protocol.ProtocolError(f"unknown sessions action {action!r}")

    def wait(self, args: protocol.WaitArgs, arrived: float | None = None, *,
             client_gone: Callable[[], bool] | None = None) -> dict:
        """C-15.4's long poll. The deadline runs from `arrived`, when the request was
        read: one that waited for a thread past its client's deadline looks once and
        answers (review of the descriptor hotfix, F1). With `client_gone` it also
        ends, within `CLIENT_POLL_S`, when its client has closed its socket (C-16.7)."""
        try:
            deadline = (time.monotonic() if arrived is None else arrived) + max(
                0, min(float(args.deadline_s), WAIT_POLL_MAX_S))
        except (TypeError, ValueError):
            raise protocol.ProtocolError("deadline_s must be a number") from None
        job_ids = args.job_ids
        if not job_ids:
            job_ids = [j["job_id"] for j in self.dispatch("list", {"mine": args.mine, "last": 1 if args.last else None})["jobs"]]
        # C-15.5: this waiter reads the store when it starts and when the hub,
        # which reads once for every waiter after each commit, says its jobs may
        # be done. It registers before its first read, so no commit is missed; a
        # waiter used to re-read every job on every wake-up of every waiter.
        with self.wait_hub.watching(job_ids) as ready:
            read = True
            while True:
                if read:
                    ready.clear()
                    answer = self._wait_answer(job_ids)
                    if answer is not None:
                        return answer
                    read_at = time.monotonic()
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.stopping.is_set():
                    # C-15.5: once more when the deadline passes, or the daemon
                    # stops, always: a job that ended after the hub's last look is
                    # answered, never reported as a timeout (review of 3c8fe55, P0).
                    # Even after a read on this pass: its snapshot may predate a
                    # commit that has not woken this waiter yet, since the hub wakes
                    # only after its own read (reviews r2 and r3, P2). It costs one
                    # read per timed-out poll.
                    answer = self._wait_answer(job_ids)
                    return answer if answer is not None else {"timeout": True}
                if client_gone is not None and client_gone():
                    # C-16.7: no one is left to read the answer. Stop now, not at
                    # the deadline, and free this thread and the client's descriptor.
                    # A stream this daemon ended itself is not a client leaving.
                    if not getattr(client_gone, "ended_here", lambda: False)():
                        self._count_connection("abandoned", "wait")
                    return {"timeout": True}
                read = ready.wait(remaining if client_gone is None
                                  else min(remaining, descriptors.CLIENT_POLL_S))
                # A hub that cannot run (no thread to be had) leaves each waiter
                # to read for itself, every `recheck_s` (C-15.5).
                read = read or (not self.wait_hub.running
                                and time.monotonic() - read_at >= self.wait_hub.recheck_s)

    def _wait_answer(self, job_ids: list[str]) -> dict | None:
        """The answer to a `wait` if every job has ended and every export is done."""
        with self.store.snapshot():         # one committed state for the whole answer
            jobs = [self._job(j) for j in job_ids]
            pending_exports = any(self.store.one("SELECT 1 FROM leases WHERE holder=? AND lease_key LIKE 'out:%'", (j["job_id"],)) for j in jobs if j["state"] == "succeeded")
            if not all(j["state"] in TERMINAL for j in jobs) or pending_exports:
                return None
            for job in jobs:
                job["attempt"] = self.store.one(
                    "SELECT * FROM attempts WHERE job_id=? ORDER BY seq DESC LIMIT 1", (job["job_id"],))
                job["notices"] = self.store.query("SELECT * FROM notices WHERE job_id=? ORDER BY notice_id", (job["job_id"],))
        return {"jobs": jobs, "timeout": False}

    def kill(self, args: protocol.KillArgs) -> dict:
        job = self._job(args.job_id)
        quarantine = self.store.one("SELECT * FROM attempts WHERE job_id=? AND state='quarantined' ORDER BY seq DESC LIMIT 1", (args.job_id,))
        if args.confirm_dead or args.force_release:
            if not quarantine:
                return {"job_id": args.job_id, "status": "already finished" if job["state"] in TERMINAL else "not quarantined"}
            self._schedule("resolve:" + args.job_id, self._resolve_quarantine, quarantine, args)
            return {"job_id": args.job_id, "status": "resolution requested"}
        with self.store.transaction("job.cancel_requested", job_id=args.job_id) as tx:
            job = self._job(args.job_id)
            if job["state"] in TERMINAL:
                return {"job_id": args.job_id, "status": "already finished"}
            rows = tx.execute("WITH RECURSIVE family(job_id) AS (SELECT ? UNION ALL SELECT j.job_id FROM jobs j JOIN family f ON j.parent_job_id=f.job_id WHERE j.independent=0) SELECT j.* FROM jobs j JOIN family f USING(job_id)", (args.job_id,)).fetchall()
            now = utcnow()      # C-7.3: one cancel, one instant, for the whole family
            for raw in rows:
                row = dict(raw)
                if row["state"] in TERMINAL:
                    continue
                tx.execute("UPDATE jobs SET cancel_requested_at=COALESCE(cancel_requested_at,?) WHERE job_id=?", (now, row["job_id"]))
                active = tx.execute("SELECT 1 FROM attempts WHERE job_id=? AND state IN ('reserved','starting','running','finalizing','quarantined')", (row["job_id"],)).fetchone()
                if not active:
                    tx.execute("UPDATE jobs SET state='cancelled',rc=130,finished_at=?,wait_reason=NULL,next_check_at=NULL WHERE job_id=?", (now, row["job_id"]))
                    tx.execute("DELETE FROM leases WHERE holder=?", (row["job_id"],))
                    # C-13.1, C-15.1: a job with an attempt behind it was waiting to try again.
                    earlier = self._earlier_attempt(tx, row)
                    self._notice(tx, row, "cancelled while waiting to retry" + earlier if earlier
                                 else "cancelled before launch")
        self._notify()
        return {"job_id": args.job_id, "status": "cancel requested"}

    def _notice(self, tx, job: dict, summary: str, *, again: bool = False) -> None:
        """C-15.1: the notice of a job the same transaction has just made terminal.

        The header is read here, from the job row as this transaction left it,
        and never from what the caller believes: `render.notice_header` names
        the job's state and rc and, only for an accepted job, the deliverable
        and the `-o` path. A caller that has not written the terminal state yet
        is a defect this refuses, not a notice to write early. `summary` is the
        caller's line(s): the final attempt's class, rc and detail, or why the
        job ended without one (incident: 2026-09-24, the header came from the
        attempt, so cancelled jobs were announced `ok; rc=0` with an `-o` path
        that was never written).

        `again` writes one more notice for a job that already has one: a
        quarantine's release whose salvage failed or left paths out (C-13.1),
        found after the job's notice went out at the quarantine.
        """
        row = tx.execute("SELECT job_id,state,rc,out_path,accepted_attempt_id,caller_session,kind "
                         "FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
        if row is not None and row["kind"] == "turn":
            return      # C-26.12: the conversation, not a notice, carries a turn's end
        if row is None or row["state"] not in TERMINAL:
            raise RuntimeError(f"notice for {job['job_id']} before its terminal state "
                               f"({row['state'] if row else 'no job row'})")
        if not again and tx.execute("SELECT 1 FROM notices WHERE job_id=?", (job["job_id"],)).fetchone():
            return
        text = render.notice_header(dict(row), self.root) + "\n" + summary + self._moved_on(tx, row["job_id"])
        tx.execute("INSERT INTO notices(job_id,session_id,text,state,created_at) VALUES(?,?,?,'pending',?)",
                   (row["job_id"], row["caller_session"], text, utcnow()))
        # C-11.8: a pin notice nobody was shown says the job waits and how to fix it;
        # at the job's end, however it ended, this notice says what happened instead.
        self._withdraw_pin_notice(tx, row["job_id"], why="ended")

    def _schedule(self, key: str, fn: Callable, *args, paced: bool = False) -> None:
        """Run `fn` on the worker pool unless `key` is already running.

        C-5.10: `paced` is for the keys the control loop offers again every tick.
        A one-shot request (an operator's `kill --confirm-dead`) is never paced:
        nothing would offer it again, so holding it back would drop it after the
        caller was told it was accepted.
        """
        with self._busy_lock:
            if (key in self._busy or self.stopping.is_set()
                    or (paced and time.monotonic() < self._worker_retry_at.get(key, 0))):
                return
            self._busy.add(key)
        generation = self.store.generation
        future = self.workers.submit(fn, *args)
        def done(f):
            try:
                deferred = f.result() is DEFERRED
                with self._busy_lock:
                    if not deferred:            # C-5.11: a deferred retry keeps its count
                        self._worker_failures.pop(key, None)
                    self._worker_retry_at.pop(key, None)
            except Exception as exc:
                # Provider/keychain errors can contain secrets; log the error
                # type only. Safe details belong in structured outcome rows.
                if not paced:
                    self.log.error("worker %s failed: %s", key, type(exc).__name__)
                    return
                # C-5.10: the control loop offers every live key again each tick,
                # so a worker that raises at once would otherwise be retried, and
                # logged, twenty times a second for as long as the cause lasts.
                with self._busy_lock:
                    count = self._worker_failures[key] = self._worker_failures.get(key, 0) + 1
                    delay = worker_retry_delay(count)
                    self._worker_retry_at[key] = time.monotonic() + delay
                if key == "retention":
                    self.timers.mark("retention", error=type(exc).__name__, next_due=after(delay))
                if count & (count - 1) == 0:     # 1, 2, 4, 8, ...: the log stays bounded
                    self.log.error("worker %s failed: %s (%d in a row, next try in %g s)",
                                   key, type(exc).__name__, count, delay)
            finally:
                # Waiters read only the store, so a pass during which nothing
                # was committed (a running attempt's tick, an idle admission
                # pass) has nothing to wake them for; each used to wake every
                # waiter, up to twenty times a second per live key (C-5.11).
                # The generation is global, so a concurrent commit elsewhere
                # can still wake them, which costs a waiter one cheap check.
                # Decided before the key is released, so whoever sees the key
                # free also sees this pass's wake-up.
                if self.store.generation != generation:
                    self._notify()
                with self._busy_lock:
                    self._busy.discard(key)
        future.add_done_callback(done)

    def _pending_exports(self) -> list[str]:
        """Jobs whose accepted attempt still holds a lease: an export to finish.

        One statement per tick. The sweep used to read every job that had ever
        been accepted and ask about its leases one by one: 266 statements a
        tick behind the store lock with 265 retained jobs (C-5.11, 2026-09-24).
        """
        return [row["job_id"] for row in self.store.query(PENDING_EXPORTS)]

    def _forget_paced(self, live: set[str]) -> None:
        """Drop pacing state for attempts that are no longer live (C-5.11).

        An attempt usually becomes terminal inside its own worker pass, after
        which the control loop never offers it again, so this is where its
        entries go.

        Worker threads write these dicts while this runs on the control loop,
        so it walks a copy: `dict.copy()` is one C call (atomic under the GIL,
        and taken under the dict's own lock without it), where walking the dict
        itself raised "dictionary changed size during iteration" whenever a
        worker added an entry mid-walk (review of 5841d8b). Popping is safe.
        """
        for pacing in (self._inspect_next,):
            for aid in [aid for aid in pacing.copy() if aid not in live]:
                pacing.pop(aid, None)
        for aid in [aid for aid in self._inspect_retry.copy() if aid not in live]:
            self._inspect_retry.discard(aid)
        self._native &= live
        for aid in [aid for aid in self._v1_unread.copy() if aid not in live]:
            self._v1_unread.pop(aid, None)

    def _note_ownership_unread(self, aid: str, exc: BaseException) -> None:
        """Log an attempt whose v1 ownership could not be read, on the 1st, 2nd,
        4th, ... consecutive tick (as C-5.10 logs a failing worker), type only."""
        count = self._v1_unread[aid] = self._v1_unread.get(aid, 0) + 1
        if count & (count - 1) == 0:
            self.log.error("attempt %s: whether v1 owns it could not be read: %s (%d ticks in a row); "
                           "no pass is given until it can (principle 3)", aid, type(exc).__name__, count)

    def _v1_owned(self, aid: str) -> bool:
        """Whether v1 still executes this live attempt (`imported_external`), read
        from its row until the answer is no (C-5.11)."""
        if aid in self._native:
            return False
        row = self.store.one("SELECT evidence_json FROM attempts WHERE attempt_id=?", (aid,))
        if row is not None and imported_external(row):
            return True
        self._native.add(aid)
        return False

    def _has_work(self, a: dict) -> bool:
        """C-5.11: whether a pass over this live attempt could do anything this tick.

        `_process_attempt` on a running attempt reads its exit receipt, its job's
        cancel request and its wall limit, and then inspects its processes if an
        inspection is due (C-5.12). When there is no receipt, no cancel request,
        the wall limit is not reached and no inspection is due, it returns having
        done nothing. That is what this answers, from the tick's one statement
        and one `stat`, so that only an attempt with something to do costs a
        worker. It decides nothing: the pass reads everything again for itself.
        Every doubt is a yes.

        `_worker_failures` is a yes for the pacing, not for any action: a pass
        whose last run raised may act on nothing, but its success is what clears
        C-5.10's count, and withheld, a stale count would back the next real
        failure off longer than it should.
        """
        aid = a["attempt_id"]
        if a["state"] != "running" or a["max_wall_s"] is None:
            return True                     # launching, starting, finalizing; or a job row to miss
        if aid in self._inspect_retry or aid in self._worker_failures:
            return True                     # C-5.10: a pass that raised is repeated on its clock
        if time.monotonic() >= self._inspect_next.get(aid, 0):
            return True                     # C-5.12: an inspection is due
        if a["cancel_requested_at"] or age(a["job_started_at"]) >= a["max_wall_s"]:
            return True
        child = self._children.get(aid)
        if child is not None and child.poll() is not None:
            return True                     # the guardian ended: the pass lets go of it
        try:
            os.stat(attempt_dir(self.root, a["job_id"], a["seq"]) / "exit.json")
        except FileNotFoundError:
            return False
        except OSError:
            pass                            # unreadable is the pass's to report
        return True

    def _control(self) -> None:
        # Recovery uses the same idempotent workers as normal execution. A
        # reserved row absent from this process's launch set was never granted
        # permission to run by this daemon instance.
        while not self.stopping.is_set():
            try:
                live = self.store.query(LIVE_TICK)
                self._forget_paced({a["attempt_id"] for a in live})
                self._forget_answers({a["attempt_id"] for a in live})        # C-6.14
                for a in live:
                    # Migration principle 3: a doubt about whether v1 still owns
                    # the run is a no. No pass is given; it is asked again next
                    # tick, and an error never ends the tick for other keys.
                    try:
                        owned = self._v1_owned(a["attempt_id"])
                    except Exception as exc:        # noqa: BLE001 - logged, bounded
                        self._note_ownership_unread(a["attempt_id"], exc)
                        continue
                    self._v1_unread.pop(a["attempt_id"], None)
                    if owned:
                        continue                    # v1 still owns it (principle 3)
                    # C-5.11: every live attempt is looked at each tick; the pool
                    # is given those a pass could do something for. Every doubt
                    # about what a pass would do is a yes, an error in the look
                    # included: the pass raises it, keyed to this attempt, and
                    # C-5.10 paces it. Raised here, it would end the tick for
                    # every other key.
                    try:
                        offer = self._has_work(a)
                    except Exception:               # noqa: BLE001 - the pass reports it
                        offer = True
                    if offer:
                        self._schedule(a["attempt_id"], self._process_attempt, a["attempt_id"], paced=True)
                for job_id in self._pending_exports():
                    self._schedule("export:" + job_id, self._export, job_id, paced=True)
                if self._recovery_complete.is_set():
                    self._schedule("conversations", self.conversations.tick, paced=True)
                    self._schedule("admission", self._admit, paced=True)
                    # C-26.9: turns also have a pass of their own, so a person's
                    # turn never waits for a detached pass to reach it.
                    self._schedule("admission:turns", self._admit_turns, paced=True)
                    self.timers.tick()
                else:
                    self._schedule("timer-recovery", self._recover_then_start_timers, paced=True)
                if time.monotonic() - self._last_maintenance >= RETENTION_INTERVAL_S:
                    self._schedule("retention", self._retention, paced=True)
            except Exception as exc:
                self.log.error("control iteration failed: %s", type(exc).__name__)
            self.stopping.wait(self.tick_s)

    def _timer_notice(self, notice: dict) -> bool:
        """C-15.8, C-18.1: an alert is a notice for `alerts.operator_session` when
        one is set. With none it is delivered by being shown: `status` and
        `status.json` list every alert in force (C-18.4), and no inbox parks it."""
        session = operator_session(self.policy)
        if session is None:
            return True
        result = self.dispatch("ping", {"session_id": session,
                                       "text": notice["subject"] + "\n" + notice["body"]})
        return result.get("notice_id") is not None

    def _withdraw_notices(self, a: protocol.NoticeWithdrawArgs) -> dict:
        """C-15.8, C-23.26: an operator withdraws a session's undelivered service notices.

        Only the rows named, only that session's, and only while still `pending`
        or `offered`: a notice a hook printed or a session acknowledged is not
        withdrawn, and a job's notice (a positive id) is its terminal record and
        is refused. The rows are deleted in one transaction with one
        `notice.withdrawn` event naming each id, the session, the reason, the
        creation span and how many of each subject went, so the withdrawal
        claims no delivery and loses no record of what was withdrawn."""
        jobs = sorted(notice_id for notice_id in a.notice_ids if notice_id >= 0)
        if jobs:
            raise protocol.ProtocolError(
                f"notice.withdraw: {len(jobs)} job notice(s) named (ids {jobs[:5]}); a job's notice is its "
                "terminal record and is acknowledged, never withdrawn (C-15.8)",
                fix=f"subfleet notices --session {a.session_id} --ack")
        if a.fingerprints and len(a.fingerprints) != len(a.notice_ids):
            raise protocol.ProtocolError(
                f"notice.withdraw: {len(a.fingerprints)} fingerprints for {len(a.notice_ids)} ids; "
                "give one per id, or none")
        if any(not isinstance(fingerprint, str) for fingerprint in a.fingerprints):
            raise protocol.ProtocolError("notice.withdraw: every fingerprint is a string, as the listing gives it")
        listed: dict = {}
        for index, notice_id in enumerate(a.notice_ids):          # a repeated id: its first fingerprint
            if a.fingerprints:
                listed.setdefault(-notice_id, a.fingerprints[index])
        wanted = sorted({-notice_id for notice_id in a.notice_ids})
        record: dict = {"session_id": a.session_id,
                        "reason": a.reason if a.reason is not None else "withdrawn by the operator",
                        "service_notice_ids": [], "count": 0, "first_created_at": None,
                        "last_created_at": None, "subjects": {}}
        with self.store.transaction("notice.withdrawn", data=record) as tx:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start:start + 500]
                marks = ",".join("?" * len(chunk))
                where = (f"session_id=? AND state IN ('pending','offered') AND notice_id IN ({marks})")
                rows = [row for row in tx.execute(
                            f"SELECT notice_id, text, created_at FROM service_notices WHERE {where} "
                            "ORDER BY notice_id", (a.session_id, *chunk)).fetchall()
                        if not listed or listed.get(row[0]) == notice_fingerprint(
                            {"text": row[1], "created_at": row[2]})]
                if rows:                            # at most one chunk's worth
                    tx.execute(f"DELETE FROM service_notices WHERE notice_id IN ({','.join('?' * len(rows))})",
                               [row[0] for row in rows])
                for notice_id, text, created_at in rows:
                    record["service_notice_ids"].append(notice_id)
                    subject = str(text).split("\n", 1)[0]
                    record["subjects"][subject] = record["subjects"].get(subject, 0) + 1
                    record["first_created_at"] = min(filter(None, (record["first_created_at"], created_at)))
                    record["last_created_at"] = max(filter(None, (record["last_created_at"], created_at)))
            record["count"] = len(record["service_notice_ids"])
        withdrawn = set(record["service_notice_ids"])
        return {"session_id": a.session_id, "withdrawn": [-notice_id for notice_id in sorted(withdrawn)],
                "kept": [-notice_id for notice_id in wanted if notice_id not in withdrawn]}

    def _retention(self):
        # C-8.4, C-26.12: detached and turn jobs each have their own budget; the
        # conversation service pins the turn jobs it still needs (IR-17). d635:
        # retirement archives before it deletes; a pass retires a bounded batch,
        # oldest first, and says when more is waiting, so a backlog is worked
        # off in catch-up passes seconds apart instead of timing out hourly.
        # Once per retention call (one bounded batch), including interruptions.
        try:
            self._prune_service_notices()
        except Exception as exc:
            self.log.warning("retention: service notices were not pruned: %s", type(exc).__name__)
        if os.environ.get(RETENTION_DORMANT_ENV) == "1":
            # 2.1.11 ships retention by archive dormant (hub, 2026-10-06): its
            # fences match a job's own tree exactly, so a turn or a lease on a
            # folder inside a finished job's tree is not seen (retention-archive.md
            # §15, fixed by the stack on #134). The installer sets this in the
            # launchd plist, never in policy.json (d574); unset, nothing changes.
            if not self._retention_dormant_logged:
                self.log.warning("retention: dormant (%s=1): no job is selected, archived or deleted",
                                 RETENTION_DORMANT_ENV)
                self._retention_dormant_logged = True
            self.timers.mark("retention", next_due=after(RETENTION_INTERVAL_S))
            self._last_maintenance = time.monotonic()
            return
        budget = {**RETENTION_DEFAULTS, **(self.policy.get("retention") or {})}
        result = maintenance(self.store, self.root, max_jobs=int(budget["jobs"]), max_bytes=int(budget["bytes"]),
                             turn_max_jobs=int(budget["turn_jobs"]), turn_max_bytes=int(budget["turn_bytes"]),
                             turn_keep_s=float(budget["turn_keep_days"]) * 86400,
                             pins=self.conversations.retention_pins,
                             cancel=self.timers.cancel, deadline=time.monotonic() + RETENTION_PASS_S,
                             state=self._retention_state,
                             remote_less_history_bytes=int(budget["remote_less_history_bytes"]))
        pruned = len(result.get("pruned") or ())
        progressed = bool(pruned or result.get("progressed"))
        interrupted = result.get("interrupted")
        if interrupted == "cancelled":
            self.timers.mark("retention", error="CancelledError", next_due=after(RETENTION_INTERVAL_S))
            self._last_maintenance = time.monotonic()
            return
        if result.get("holder_scan_failed"):
            # Quarantining and rolling back is action, but another batch cannot
            # retire jobs while the shared process listing is unavailable.
            self.timers.mark("retention", error="ScanFailed", next_due=after(RETENTION_INTERVAL_S))
            self.log.warning("retention: holder scan failed; %d jobs in the store; next pass in an hour",
                             result.get("jobs_after", 0))
            self._last_maintenance = time.monotonic()
            return
        if interrupted:
            if not progressed:
                self.timers.mark("retention", error="TimeoutError", next_due=after(RETENTION_INTERVAL_S))
                self.log.warning("retention: deadline reached before it pruned a job or advanced; "
                                 "%d jobs in the store; next pass in an hour", result.get("jobs_after", 0))
                self._last_maintenance = time.monotonic()
                return
            self.log.warning("retention: deadline reached after progress (%d jobs pruned); "
                             "%d jobs in the store; retrying on the worker clock", pruned, result.get("jobs_after", 0))
            # Stay due. _schedule records the error and applies C-5.10.
            raise TimeoutError("retention deadline reached after progress")
        for error in (result.get("errors") or [])[:5]:
            self.log.warning("retention: %s: %s", error.get("job_id"), str(error.get("error"))[:300])
        if result.get("more") and progressed:
            delay = RETENTION_CATCH_UP_S
            self.log.info("retention catch-up: retired %d jobs (freed %d bytes, %d on disk; moved %d bytes into the "
                          "archive, which added %d bytes; net %d on disk), %d in flight, %d deferred; "
                          "continuing in %g seconds",
                          len(result.get("pruned") or ()), result.get("freed_bytes") or 0,
                          result.get("freed_disk_bytes") or 0, result.get("archived_bytes") or 0,
                          result.get("added_bytes") or 0,
                          (result.get("freed_disk_bytes") or 0) - (result.get("added_bytes") or 0),
                          len(result.get("in_flight") or ()), len(result.get("deferred") or {}), delay)
            self.timers.mark("retention", next_due=after(delay))
            self._last_maintenance = time.monotonic() - RETENTION_INTERVAL_S + delay
            return
        if result.get("pruned"):
            self.log.info("retention: retired %d jobs; freed %d bytes (%d on disk), moved %d bytes into the archive, "
                          "which added %d bytes (bundles, manifests, rows); net %d on disk",
                          len(result["pruned"]), result.get("freed_bytes") or 0, result.get("freed_disk_bytes") or 0,
                          result.get("archived_bytes") or 0, result.get("added_bytes") or 0,
                          (result.get("freed_disk_bytes") or 0) - (result.get("added_bytes") or 0))
        self.timers.mark("retention", next_due=after(RETENTION_INTERVAL_S))
        # A raising pass remains due so the worker retry clock can re-offer it.
        # Completed and non-advancing passes rearm the hourly interval last.
        self._last_maintenance = time.monotonic()

    def _prune_service_notices(self) -> int:
        """C-23.26: a service notice is pruned 14 days after it was written, once delivered.

        Only `surfaced` and `acknowledged` rows go; a `pending` or `offered` one
        is never pruned by age. A job notice is pruned with its job (C-8.4).
        """
        with self.store.transaction("service-notice.retention") as tx:
            return tx.execute("DELETE FROM service_notices WHERE state IN ('acknowledged','surfaced') AND created_at<?",
                              (after(-SERVICE_NOTICE_RETENTION_S),)).rowcount

    def _recover_then_start_timers(self):
        # d635: a resume's fence on its source lives only while its submit runs.
        with self.store.transaction("retention.fence_released", data={"reason": "restart"}) as tx:
            tx.execute("DELETE FROM leases WHERE lease_key LIKE 'retire:%' AND holder LIKE 'resume:%'")
        # HTTP reservations have no provider process and can be released on restart.
        for lease in self.store.query("SELECT * FROM leases WHERE holder LIKE 'probe:timer:%'"):
            if lease["holder"] not in self.timers.active_holders and not self._probe_record(lease["holder"]):
                self.store.release_leases(lease["holder"])
        self._recover_probes()
        self.timers.actions.recover()
        from .gate.merge import MergeActions
        MergeActions(self.store).recover()
        self._canonicalize_pins()
        self._recover_capacity_waits()
        self.timers.start()
        self._recovery_complete.set()

    def _canonicalize_pins(self) -> None:
        """C-11.2: an unfinished job's pin is the lane id its name resolved to.

        Submit has stored the lane id since 2026-09-22; a job accepted before
        kept the name its caller typed, resolved against the store's lane rows,
        where a Codex lane has no email, while admission resolved it against the
        view, where five such names matched a Claude lane and a Codex lane at
        once. Each start resolves those against the roster submit uses, narrowed
        by the job's provider (its model's, else its task's). The flag that
        pinned it (`-a`, `-H`) was never stored, so a name with neither model
        nor task that both providers answer to still names several lanes here,
        though today's submit would take the flag's lane. A name that resolves to one lane becomes its id (event
        `job.pin_canonicalized`); one that names several lanes or none is left
        for admission to refuse or report (C-6.12), and `daemon.log` says so. A
        job carrying an unmeasured-reserve authorization is never rewritten:
        that authorization names the lane id it was granted for (C-11.7).
        """
        try:
            self._canonicalize_pins_once()
        except Exception as exc:
            # Advisory: admission settles whatever this leaves (C-6.12), and a
            # recovery that raised would keep timers and admission from starting.
            self.log.warning("pin repair skipped: %s", type(exc).__name__)

    def _canonicalize_pins_once(self) -> None:
        lanes = self._pin_roster()
        known = {lane["lane_id"] for lane in lanes}
        for job in self.store.query("SELECT * FROM jobs WHERE state IN ('queued','waiting','running') "
                                    "AND pinned_lane IS NOT NULL AND unmeasured_reserve_reason IS NULL "
                                    "ORDER BY created_at,rowid"):
            pin = job["pinned_lane"]
            if pin in known:
                continue
            try:
                lane = scheduler.resolve_lane(lanes, pin, scheduler.pin_provider(self.policy, job))
            except scheduler.RouteError as exc:
                self.log.warning("job %s pin %r was left as it is: %s", job["job_id"], pin, exc)
                continue
            if lane is None:
                self.log.warning("job %s pin %r was left as it is: it names no lane", job["job_id"], pin)
                continue
            with self.store.transaction("job.pin_canonicalized", job_id=job["job_id"],
                                        data={"from": pin, "to": lane["lane_id"]}) as tx:
                tx.execute("UPDATE jobs SET pinned_lane=? WHERE job_id=? AND pinned_lane=? "
                           "AND state IN ('queued','waiting','running')", (lane["lane_id"], job["job_id"], pin))
            self.log.info("job %s pin %r is now %s (C-11.2)", job["job_id"], pin, lane["lane_id"])

    def _recover_capacity_waits(self) -> None:
        """C-6.10: after a restart every capacity wait is looked at once, on the first pass.

        The wait records are in memory, so nothing could bring a persisted wait
        forward when capacity came free, and what each job was waiting for may
        have changed while no daemon ran. A route wait (C-6.12) is looked at too:
        a restart is how the fix for what it met arrives.
        """
        now = utcnow()
        with self.store.transaction("admission.recovered") as tx:
            tx.execute("UPDATE jobs SET next_check_at=? WHERE state='waiting' AND wait_reason IN ('capacity','route') "
                       "AND next_check_at>?", (now, now))

    def _timer_turn(self, lane: Lane, purpose: str, holder: str, *, cancel, deadline) -> Outcome:
        if cancel.is_set() or time.monotonic() >= deadline:
            return Outcome(OutcomeClass.UNKNOWN, "timer cancelled", evidence={"timed_out": True})
        token = holder.rsplit(":", 1)[-1]
        directory = self.root / "lanes" / lane.lane_id / "probes" / token
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        model = self.policy["models"]["haiku" if lane.provider == "claude" else "terra"]
        record = {"holder": holder, "job_id": None, "lane_id": lane.lane_id,
                  "timer_kind": purpose, "model_id": model["id"], "directory": str(directory),
                  "state": "reserved", "created_at": utcnow(), "owned_identities": {},
                  "deadline_at": after(max(0, deadline - time.monotonic()))}
        self._save_probe(record)
        job = {"job_id": "timer-" + token, "request_id": token, "kind": "probe",
               "workdir": str(directory), "prompt_path": str(directory / "prompt.md"),
               "sandbox": "read-only", "exclusions": "[]",
               # _spec builds a JobSpec from a job row; a timer turn has no routing fields.
               "task": None, "tier": None, "pinned_model": None, "pinned_lane": None,
               "name": None, "out_path": None}
        try:
            outcome = self._execute_probe(job, lane, model, holder)
        except Exception:
            current = self._probe_record(holder)
            safe = self._contain_probe(current)
            if not safe:
                return Outcome(OutcomeClass.UNKNOWN, "timer quarantined", evidence={"probe_quarantined": True})
            raise
        requested = (self._read_json(directory / "request.json") or {}).get("requested_at")
        if requested:
            self.store.add_event("timer.request", lane_id=lane.lane_id,
                                 data={"requested_at": requested, "purpose": purpose,
                                       "rc": outcome.evidence.get("rc"), "native_session_id": outcome.native_session_id})
        evidence = {**outcome.evidence, "requested_at": requested,
                    "timed_out": time.monotonic() >= deadline}
        if not evidence.get("probe_quarantined"):
            record = self._probe_record(holder)
            record.update(state="completed")
            self._save_probe(record)
            shutil.rmtree(directory, ignore_errors=True)
        if outcome.cls == OutcomeClass.OK:
            self._record_answer(lane.lane_id, purpose)              # C-6.14: a keepalive or heal turn answered
        return dataclasses.replace(outcome, evidence=evidence)

    def _workspace(self, job: dict) -> tuple[str, str | None, str | None, list[str]]:
        """C-6.8: every git call here is capped by policy, and a call that did
        not finish raises rather than answering "no HEAD" or "no branch"."""
        cap = self.policy["caps"]["workspace_git_timeout_s"]
        workdir = job.get("worktree") or job["workdir"]
        turn_allow_main = job.get("kind") == "turn" and bool(
            ((self._read_json(self.root / "jobs" / job["job_id"] / "manifest.json") or {}).get("turn") or {}).get("allow_main"))
        if job["sandbox"] == "workspace-write" and (job["in_place"] or job.get("worktree")) and not turn_allow_main:
            # Submission may have waited for capacity while the caller changed
            # branches. Refuse again at admission, including writable retries,
            # for the directory the job writes in (C-13.2): the caller's checkout
            # when in place, else the worktree an earlier attempt already has.
            validate_writable_workdir(workdir, timeout_s=cap)
        if job["sandbox"] == "workspace-write" and not job["in_place"] and not job.get("worktree"):
            workdir = str(self.root / "worktrees" / job["job_id"])
            if Path(workdir).exists() and (not (Path(workdir) / ".git").is_file()
                                           or git_head(workdir, timeout_s=cap) is None):
                # A `worktree add` killed at its cap can leave a directory that is
                # not a worktree. `.git` is checked first because git run in a
                # bare directory answers for whatever repository encloses it.
                # Nothing has run here (the job has no attempt), so it is rebuilt
                # rather than handed to a provider.
                self._discard_worktree(job["workdir"], workdir, cap)
            if not Path(workdir).exists():
                try:
                    # `backslashreplace`: git's messages can quote a file name that is
                    # not UTF-8, which a strict read raised as `UnicodeDecodeError`
                    # out of the admission pass on every try (review of cda4c161, N1).
                    result = subprocess.run(["git", "-C", job["workdir"], "worktree", "add", "--detach", workdir, job["workdir_head"]],
                                            capture_output=True, text=True, errors="backslashreplace",
                                            env=_git_env(None),
                                            timeout=self.policy["caps"]["worktree_add_timeout_s"])
                except (OSError, subprocess.SubprocessError):
                    self._discard_worktree(job["workdir"], workdir, cap)
                    raise
                if result.returncode:
                    self._discard_worktree(job["workdir"], workdir, cap)
                    # A git killed by a signal did not finish, as a timed-out one did not.
                    killed = _failure("worktree", result) if result.returncode < 0 else None
                    error = "could not allocate worktree: " + (
                        str(killed) if killed else result.stderr.strip()[-300:] or f"git exited {result.returncode}")
                    if killed or _transient_git(result.stderr):
                        raise SalvageError(error, transient=True)
                    raise AdapterError(error, fix="check repository and state-root permissions")
            os.chmod(workdir, 0o700)
        head = git_head(workdir, timeout_s=cap)
        baseline = None
        skipped: list[str] = []
        if head and job["sandbox"] == "workspace-write":
            baseline = working_tree(workdir, head, timeout_s=cap, left_out=skipped)
        elif head:
            baseline = git_tree(workdir, head, timeout_s=cap)
        return workdir, head, baseline, [path_text(path) for path in skipped]

    def _pin_baseline(self, job: dict, previous: list, workspace: str, head: str | None,
                      baseline: str | None, skipped: list[str] | tuple[str, ...] = ()) -> dict | None:
        """C-13.1: the salvage artifact holding the next attempt's start snapshot, when
        the job's last attempt's salvage failed; else None.

        That attempt's work is then only in this snapshot (`baseline`, from
        `_workspace`): a tree object no ref holds, which `gc` may prune and the
        next attempt goes on to edit, with nobody told (review of cda4c161, N2).
        So it is held under `refs/subfleet-salvage/<job id>-a<seq>-baseline`
        before the attempt is reserved, and recorded as that attempt's artifact
        (role `salvage`), which retention keeps like any salvage ref (C-13.4).
        What the snapshot left out (`skipped`, nested repositories with no
        commit, which no ref can hold) is named in the `salvage.baseline_held`
        event, as the attempt's evidence names it (`baseline_skipped`).
        A failure to hold it is C-6.8's, as the snapshot's own is: the job waits
        or fails, and nothing runs in the worktree meanwhile.

        A job that ends before its retry is reserved records the ref as its last
        attempt's artifact and names it in its notice (`_earlier_attempt`). One
        cancelled while this pass wrote the ref, before its event, had its notice
        written without it; so when the event is new and the job has ended, the
        ref is recorded here and told in one more notice (review of ceacf18b, P2).
        """
        if job["sandbox"] != "workspace-write" or job["kind"] == "turn" or not previous:
            return None
        if not json.loads(previous[-1]["evidence_json"] or "{}").get("salvage_error"):
            return None
        if not head or not baseline:
            raise SalvageError(f"attempt a{previous[-1]['seq']}'s salvage failed and {workspace} "
                               "has no commit to hold its work on")
        seq = len(previous) + 1
        ref, commit = pin_baseline(workspace, job["job_id"], seq, baseline, head,
                                   timeout_s=self.policy["caps"]["workspace_git_timeout_s"])
        artifact = {"role": "salvage", "path": ref, "sha256": hashlib.sha256(commit.encode()).hexdigest(), "bytes": 0}
        # The ref may be written before capacity becomes available. Record it
        # then, once, so it remains discoverable even if no retry is reserved.
        with self.store.transaction("salvage.baseline_recorded", job_id=job["job_id"]) as tx:
            held = tx.execute("SELECT data_json FROM events WHERE job_id=? AND kind='salvage.baseline_held'",
                              (job["job_id"],)).fetchall()
            if not any(json.loads(row[0]).get("ref") == ref for row in held):
                data = {"ref": ref, "commit": commit, "seq": seq, "after": previous[-1]["attempt_id"],
                        **({"skipped": _skipped(skipped)} if skipped else {})}
                tx.execute("INSERT INTO events(ts,kind,job_id,attempt_id,data_json) VALUES (?,?,?,?,?)",
                           (utcnow(), "salvage.baseline_held", job["job_id"], previous[-1]["attempt_id"],
                            json.dumps(data)))
                current = dict(tx.execute("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone())
                if current["state"] in TERMINAL:
                    self.store.add_artifact(previous[-1]["attempt_id"], **artifact)
                    self._notice(tx, current, "as the job ended, " + self._held_line(current, data), again=True)
        return artifact

    def _where_it_writes(self, job_id: str, sandbox: str) -> dict:
        """For the caller (review of d261): the sandbox the job got, and for a
        writable job that is not in place, the worktree it will write in."""
        workspace = (self._read_json(self.root / "jobs" / job_id / "manifest.json") or {}).get("workspace") or {}
        return {"sandbox": sandbox, "worktree": workspace.get("worktree")}

    def _launch_dir(self, job: dict, provider: str | None = None) -> str:
        """Where the provider starts: the caller's directory, or in a worktree the
        same place relative to the repository, recorded at submit (a Claude job run
        from `repo/pkg` starts in `<worktree>/pkg`, and so does a resume of it).

        A Codex job starts at the worktree's top: Codex's workspace-write sandbox
        lets it write only under its working directory, so started in `pkg/` it
        could not touch the rest of its worktree (review B-1; `codex sandbox -P
        :workspace` from a subdirectory refused a write to its parent, 2026-09-25).
        Its note names the caller's place instead."""
        root = job.get("worktree") or job["workdir"]
        workspace = (self._read_json(self.root / "jobs" / job["job_id"] / "manifest.json") or {}).get("workspace") or {}
        prefix = workspace.get("prefix")
        if provider == "codex" or not prefix or prefix == "." or _outside(prefix):
            return root
        if workspace.get("worktree") and os.path.realpath(workspace["worktree"]) != os.path.realpath(root):
            return root
        target = Path(root) / prefix
        return str(target) if target.is_dir() else root

    @staticmethod
    def _discard_worktree(repository: str, workdir: str, cap: float) -> None:
        """Best effort: a failure here is reported by the add that follows it.

        Only this path's registration is removed. A repository-wide `git
        worktree prune` would also drop every other registration whose tree is
        missing at that moment (d635: never run a repository-wide prune)."""
        shutil.rmtree(workdir, ignore_errors=True)
        discard_registration(repository, workdir, timeout=cap)

    def _probe_record(self, holder: str) -> dict | None:
        # C-8.4: probe state and results live in events, never synthetic jobs.
        # C-3.7: the newest record for this holder, found by SQLite; every
        # probe.state event (thousands, never pruned) used to be fetched and
        # parsed in Python, once per probe lease per capacity view.
        # The holder is checked in Python for the index's hit too: a payload
        # with the key twice, which no writer makes (every one is json.dumps of
        # a dict), is indexed under SQLite's first value and read by Python
        # under the last, and must never be returned as another holder's. Being
        # the newest indexed row under that first value, it also hides that
        # holder's older records (the hit is LIMIT 1); C-3.7 names this.
        for row in self.store.query(PROBE_RECORD, (holder,)):
            try:
                record = json.loads(row["data_json"])
            except (TypeError, ValueError):
                continue        # not JSON at all; no writer makes one (the old walk raised here)
            if isinstance(record, dict) and record.get("holder") == holder:
                return record
        return None

    def _save_probe(self, record: dict) -> None:
        self.store.add_event("probe.state", job_id=record["job_id"], lane_id=record["lane_id"], data=record)

    def _probe_census(self, record: dict):
        return procs.containment(record.get("pgid"), record.get("guardian_pid"),
                                 record.get("child_pid"), record["holder"], root=str(self.root))

    def _contain_probe(self, record: dict) -> bool:
        """C-5.4–7: terminate only recorded identities and retain uncertain leases."""
        census = self._probe_census(record)
        owned = {int(pid): procs.ProcessIdentity(**value)
                 for pid, value in record.get("owned_identities", {}).items()}
        pid = record.get("guardian_pid")
        leader_live = pid and procs.same_process(pid, record.get("boot_id"), record.get("proc_start"))
        if leader_live:
            owned.update({p: ident for p, ident in census.identities.items() if p in census.group_pids})
            owned[pid] = procs.ProcessIdentity(pid, record["boot_id"], record["proc_start"])
        record["owned_identities"] = {str(p): dataclasses.asdict(ident) for p, ident in owned.items()}
        if not census.verified_empty and record.get("state") != "quarantined":
            record["state"] = "containing"
            self._save_probe(record)  # Authority precedes every signal, including recovery.
            if leader_live:
                procs.signal_group(record["pgid"], signal.SIGTERM,
                                   boot_id=record["boot_id"], proc_start=record["proc_start"])
            deadline = time.monotonic() + self.term_grace_s
            while not census.verified_empty and time.monotonic() < deadline:
                time.sleep(.05)
                census = self._probe_census(record)
            if not census.verified_empty:
                if leader_live:
                    procs.signal_group(record["pgid"], signal.SIGKILL,
                                       boot_id=record["boot_id"], proc_start=record["proc_start"])
                for target in census.live_pids:
                    if target in owned:
                        procs.signal_process(owned[target], signal.SIGKILL)
                time.sleep(.05)
                census = self._probe_census(record)
        record.update(state="contained" if census.verified_empty else "quarantined",
                      containment=census.to_dict())
        self._save_probe(record)
        if not census.verified_empty:
            with self.store.transaction("probe.quarantined", job_id=record["job_id"],
                                        lane_id=record["lane_id"], data={"containment": census.to_dict()}) as tx:
                tx.execute("UPDATE jobs SET state='waiting',wait_reason='uncertain',next_check_at=? WHERE job_id=? AND state IN ('queued','waiting')",
                           (after(60), record["job_id"]))
        return census.verified_empty

    def _await_probe(self, record: dict, child=None) -> tuple[bool, dict | None]:
        """Re-adopt the same gated guardian, bounded by its durable deadline."""
        directory = Path(record["directory"])
        next_census = next_liveness = 0.0
        while not self.stopping.is_set():
            if child:
                child.poll()  # Reap our own guardian when it finishes.
            receipt = self._read_json(directory / "exit.json")
            if receipt:
                record["child_pid"] = receipt.get("child_pid")
                break
            job = self.store.get_job(record["job_id"])
            if not record.get("timer_kind") and (not job or job["cancel_requested_at"] or job["state"] in TERMINAL):
                break
            if record.get("timer_kind") and self.timers.cancel.is_set():
                break
            if record.get("state") in ("reserved", "quarantined", "containing", "contained"):
                break
            if record["deadline_at"] <= utcnow():
                break
            # C-5.11, as for a running attempt: the receipt, the job and the
            # deadline are read every pass; `ps` is asked about the guardian at
            # most every liveness interval, and the owned-member record reads
            # the group source alone. The full census decides containment.
            if time.monotonic() >= next_liveness:
                next_liveness = time.monotonic() + self.inspect_interval_s
                if not procs.same_process(record["guardian_pid"], record["boot_id"], record["proc_start"]):
                    break
                if time.monotonic() >= next_census:
                    recorded = dict(record.get("owned_identities", {}))
                    fresh = self._new_group_identities(record.get("pgid"), recorded)
                    if fresh:
                        if not procs.same_process(record["guardian_pid"], record["boot_id"], record["proc_start"]):
                            break
                        owned = {**recorded, **fresh}
                        if owned != recorded:
                            record["owned_identities"] = owned
                            self._save_probe(record)
                    next_census = time.monotonic() + OWNED_CENSUS_INTERVAL_S
            self.stopping.wait(.05)
        safe = self._contain_probe(record)
        if child:
            child.poll()
        return safe, self._read_json(directory / "exit.json")

    def _execute_probe(self, job: dict, lane: Lane, model: dict, holder: str) -> Outcome:
        """C-11.4, C-5.1: run a read-only requested-model probe through the guardian.

        This is the process seam for tests. A normal return guarantees containment
        unless the durable probe record is quarantined; no secret enters a receipt.
        """
        record = self._probe_record(holder)
        directory = Path(record["directory"])
        adapter = get_adapter(lane.provider)
        self._validate_home(lane)
        credential_env = resolve_credential(lane.credential)
        prompt = directory / "prompt.md"
        self._publish("probe-prompt", prompt, b"Reply with exactly OK. Do not use tools.\n")
        spec = self._spec(job, kind="probe", workdir=str(directory), prompt_path=str(prompt),
                          sandbox=Sandbox.READ_ONLY, out_path=None, mcp_servers=(), mcp_config=None)
        launch = adapter.build_launch(spec, holder, directory, lane, credential_env,
                                      model["id"], model.get("effort"), prompt,
                                      self._guard_override(adapter, lane, str(directory),
                                                           self._guard_recorder(lane, str(directory), directory),
                                                           self.root))
        safe_launch = dataclasses.asdict(launch)
        safe_launch.pop("env_add")
        self._publish("probe-launch", directory / "launch.json", json_bytes(safe_launch))
        env = {**os.environ, **launch.env_add}
        for key in (*launch.env_remove, "CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            env.pop(key, None)
        env.update(SUBFLEET_JOB=job["job_id"], SUBFLEET_ATTEMPT=holder, SUBFLEET_ROOT=str(self.root), SUBFLEET_PROBE="1")  # C-5.1, C-11.4
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        read_fd, write_fd = procs.pipe_above_stdio()
        command = [sys.executable, "-m", "subfleet.guardian", "--attempt-dir", str(directory),
                   "--cwd", launch.cwd, "--stdout-path", launch.stdout_path,
                   "--stderr-path", launch.stderr_path, "--launch-fd", str(read_fd)]
        if launch.stdin_path:
            command += ["--stdin-path", launch.stdin_path]
        command += ["--", *launch.argv]
        child = None
        try:
            child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     pass_fds=(read_fd,), close_fds=True, cwd=package_root)
            identity_deadline = time.monotonic() + 2
            started = procs.proc_start(child.pid)
            while not started and child.poll() is None and time.monotonic() < identity_deadline:
                time.sleep(.01)
                started = procs.proc_start(child.pid)
            if not started:
                raise procs.InspectionError("probe guardian identity is absent")
            record.update(state="starting", guardian_pid=child.pid, pgid=child.pid,
                          boot_id=procs.boot_id(), proc_start=started)
            self._save_probe(record)
            if not record.get("timer_kind") or not self.timers.cancel.is_set():
                os.write(write_fd, b"1")  # Committed ownership is required to open the gate.
        except (OSError, procs.InspectionError) as exc:
            record["launch_error"] = type(exc).__name__
        finally:
            os.close(read_fd)
            os.close(write_fd)
        if child is None:
            return Outcome(OutcomeClass.UNKNOWN, "probe guardian could not be spawned")
        safe, receipt = self._await_probe(record, child)
        if not safe:
            return Outcome(OutcomeClass.UNKNOWN, "probe containment is quarantined",
                           evidence={"probe_quarantined": True})
        if not receipt:
            return Outcome(OutcomeClass.UNKNOWN, "probe ended without an exit receipt")
        outcome = adapter.classify(directory, launch, ExitInfo(**{key: receipt.get(key) for key in
                                ("rc", "signal", "wall_s", "child_pid", "spawn_error")}))
        request = self._read_json(directory / "request.json") or {}
        return dataclasses.replace(outcome, evidence={**outcome.evidence, **request,
                                   "rc": receipt.get("rc"), "signal": receipt.get("signal")})

    def _finish_probe(self, record: dict, outcome: Outcome) -> None:
        if outcome.cls == OutcomeClass.LIMITED and outcome.closure is None:
            outcome = dataclasses.replace(outcome, closure=Closure(
                record["lane_id"], record["model_id"], after(3600), ClosureReason.PROVIDER_LIMIT,
                ClockSource.GUESSED, None))
        if record.get("timer_kind"):
            sent = (self._read_json(Path(record["directory"]) / "request.json") or {}).get("requested_at")
            if sent:
                self.store.add_event("timer.request", lane_id=record["lane_id"], data={"requested_at": sent, "rc": outcome.evidence.get("rc"),
                                                                      "native_session_id": outcome.native_session_id})
            if record["timer_kind"] == "keepalive" and sent and outcome.cls == OutcomeClass.OK:
                self.store.add_reading(Reading(record["lane_id"], record["model_id"], "admission", None, None,
                                              ReadingLabel.ADMISSION_OBSERVED, "keepalive", sent))
            record.update(state="completed")
            self._save_probe(record)
            self.store.release_leases(record["holder"])
            shutil.rmtree(record["directory"], ignore_errors=True)
            if outcome.cls == OutcomeClass.OK:
                self._record_answer(record["lane_id"], record["timer_kind"])     # C-6.14
            return
        with self.store.transaction("probe.completed", job_id=record["job_id"], lane_id=record["lane_id"],
                                    data={"model": record["model_id"], "class": outcome.cls.value,
                                          "evidence": outcome.evidence}) as tx:
            if outcome.cls == OutcomeClass.AUTH_DEAD:
                self.store.update_lane(record["lane_id"], enabled=0)
                self.timers.record_auth_dead(record["lane_id"])
            self._record_identity(record["lane_id"], outcome)
            for reading in outcome.readings:
                self.store.add_reading(dataclasses.replace(reading, attempt_id=None))
            if outcome.closure:
                self.store.add_closure(outcome.closure)
            if outcome.cls == OutcomeClass.OK and self._identity_binds(outcome):
                # C-9.1, C-10.6: "this model was admitted on this lane" is a claim
                # about the lane, and it is only true if the credential is its own.
                self.store.add_reading(Reading(record["lane_id"], record["model_id"], "admission", None, None,
                                              ReadingLabel.ADMISSION_OBSERVED, "probe", utcnow()))
            record.update(state="completed", outcome=dataclasses.asdict(outcome))
            self._save_probe(record)
            tx.execute("DELETE FROM leases WHERE holder=?", (record["holder"],))
            tx.execute("UPDATE jobs SET wait_reason='capacity',next_check_at=? WHERE job_id=? AND state='waiting' AND wait_reason='uncertain'",
                       (utcnow(), record["job_id"]))
        shutil.rmtree(record["directory"], ignore_errors=True)
        if outcome.cls == OutcomeClass.OK:
            self._record_answer(record["lane_id"], "probe")        # C-6.14: C-11.4's probe is a model turn

    def _recover_probes(self) -> None:
        """C-5.3–7, C-8.4: recover each durable probe before admitting more work."""
        for lease in self.store.query("SELECT * FROM leases WHERE holder LIKE 'probe:%'"):
            if lease["holder"] in self.timers.active_holders:
                continue
            record = self._probe_record(lease["holder"])
            if not record:
                continue  # No recorded identity grants no authority to release or kill.
            safe, receipt = self._await_probe(record)
            if not safe:
                continue
            outcome = Outcome(OutcomeClass.UNKNOWN, "probe recovered without an exit receipt")
            if receipt and (value := self._read_json(Path(record["directory"]) / "launch.json")):
                value.update(env_add=self._relaunch_env(record["lane_id"]),
                             argv=tuple(value["argv"]), env_remove=tuple(value["env_remove"]))
                adapter = get_adapter(self.store.get_lane(record["lane_id"]).provider)
                outcome = adapter.classify(Path(record["directory"]), Launch(**value), ExitInfo(**{
                    key: receipt.get(key) for key in ("rc", "signal", "wall_s", "child_pid", "spawn_error")}))
            self._finish_probe(record, outcome)

    def _probe_candidate(self, job: dict, decision, holder: str) -> Outcome:
        lane = self.store.get_lane(decision.chosen_lane)
        model = self.policy["models"][decision.chosen_model]
        try:
            outcome = self._execute_probe(job, lane, model, holder)
        except (AdapterError, OSError, subprocess.SubprocessError) as exc:
            outcome = Outcome(OutcomeClass.UNKNOWN, f"probe unavailable: {type(exc).__name__}")
            self._contain_probe(self._probe_record(holder))
        record = self._probe_record(holder)
        if record["state"] not in ("reserved", "contained", "quarantined"):
            self._contain_probe(record)
            record = self._probe_record(holder)
        if record["state"] == "quarantined":
            return Outcome(OutcomeClass.UNKNOWN, "probe containment is quarantined",
                           evidence={"probe_quarantined": True})
        self._finish_probe(record, outcome)
        return outcome

    def _prepare_route(self, job: dict, decision_job: dict, exclusions: tuple[str, ...]):
        # The admission worker serializes probes; a distinct durable holder and
        # a gated guardian prevent a restart from launching a duplicate probe.
        approved = set()
        for _ in range(len(self.store.list_lanes()) * len(self.policy["models"]) + 1):
            desktop = self._desktop_identity()
            current = self._job(job["job_id"])
            if current["cancel_requested_at"] or current["state"] in TERMINAL:
                return None, desktop
            basis = {}
            decision = self._route(decision_job, extra_exclusions=exclusions, desktop=desktop, basis=basis)
            pair = (decision.chosen_lane, decision.chosen_model)
            if not self._needs_probe(decision, job) or pair in approved:
                # C-6.3: this evaluation is the one the reservation checks and
                # reserves on; the job is not evaluated a second time before it.
                self._early_routes[job["job_id"]] = (decision, basis)
                return approved, desktop
            # C-10.3: refreshed off the lock, and read inside it as `_route_rows`
            # reads it, so a probe never runs a turn on the desktop login that
            # Claude Code began using after the evaluation (review of PR #72).
            # Before the directory exists, so a store error here leaves none behind.
            self._desktop_in_use()
            self._record_desktop_use()
            token = os.urandom(12).hex()
            holder = f"probe:{token}"
            directory = self.root / "lanes" / decision.chosen_lane / "probes" / token
            directory.mkdir(mode=0o700, parents=True)
            record = {"holder": holder, "job_id": job["job_id"], "lane_id": decision.chosen_lane,
                      "model_id": self.policy["models"][decision.chosen_model]["id"],
                      "directory": str(directory), "state": "reserved", "created_at": utcnow(),
                      "deadline_at": after(60), "owned_identities": {}}
            reserved = False
            try:
                with self.store.transaction("probe.reserved", job_id=job["job_id"], lane_id=decision.chosen_lane):
                    # Selection precedes this transaction. Ownership transfer and
                    # probe admission must serialize on the same current lane row.
                    lane = self.store.get_lane(decision.chosen_lane)
                    if not lane or lane.owner != "v2" or not lane.enabled:
                        return None, desktop
                    # C-23.44, C-23.47: a credential a timer's read found revoked or
                    # unusable since the evaluation is not probed (review of PR #72).
                    row = self.store.one("SELECT * FROM lanes WHERE lane_id=?", (decision.chosen_lane,))
                    if row and capacity.credential_latched(self.timers.merge_lane(dict(row))):
                        return None, desktop
                    is_desktop = lane.desktop
                    if lane.provider == "claude" and desktop.decisive:
                        is_desktop = desktop.owns(dataclasses.asdict(lane))
                    # C-10.3: refused only while Claude Code uses the desktop login,
                    # as the decision it probes for was judged (`basis`).
                    in_use = self._desktop_answer()
                    if (is_desktop and (basis.get("desktop_in_use") if in_use is None else in_use) is not False
                            and not decision_job.get("allow_desktop")):
                        return None, desktop
                    if not self.store.acquire_lease(f"lane:{decision.chosen_lane}:slot:0", holder):
                        return None, desktop
                    self._save_probe(record)
                    reserved = True
            finally:
                if not reserved:
                    # No record names this directory, so recovery would never
                    # collect it, and a held slot is tried again at every recheck.
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
            outcome = self._probe_candidate(job, decision, holder)
            if outcome.cls == OutcomeClass.OK and self._identity_binds(outcome):
                approved.add(pair)
            elif outcome.cls not in (OutcomeClass.LIMITED, OutcomeClass.AUTH_DEAD):
                # A `limited` probe closed its lane and an `auth-dead` one disabled it
                # (C-23.44), so the next evaluation goes elsewhere at once; anything
                # else waits.
                # C-6.10: this wait keeps its own 60 s clock and is never brought
                # forward (a released lease must not re-probe the provider), but a
                # probe that ends the same way adds no second decision row.
                repeat = self._capacity_wait(job["job_id"], "probe-wait:" + scheduler.verdict_signature(decision),
                                             {"reason": "probe-pending"}, expedite=False)
                with self.store.transaction("job.probe_waiting", job_id=job["job_id"]) as tx:
                    if not repeat:
                        self.store.add_decision(job["job_id"], decision)
                    tx.execute("UPDATE jobs SET state='waiting',wait_reason=?,next_check_at=? WHERE job_id=? AND state IN ('queued','waiting')",
                               ("uncertain" if outcome.evidence.get("probe_quarantined") else "capacity",
                                after(60), job["job_id"]))
                return None, desktop
        return None, desktop

    def _admit(self) -> None:
        """One admission pass over turns, then one over detached jobs (C-26.9).

        The control loop also runs the turn pass alone (`_admit_turns`), beside
        this one, so a turn submitted while a long detached pass runs is placed
        by the next turn pass, not after it. A turn pass already running on the
        other worker is not waited for: that pass is looking at the turns."""
        try:
            self._admit_kind("turn", wait=False)
        finally:
            # A store error in the turn half is the pass's (C-6.12), raised for
            # C-5.10 to retry, but only once the detached half has run: a turn's
            # trouble never stops detached jobs.
            self._admit_kind("detached")

    def _admit_turns(self) -> None:
        """C-26.9: the turn pass alone, on its own worker: a person is waiting."""
        self._admit_kind("turn", wait=False)

    def _admit_kind(self, kind: str, *, wait: bool = True) -> None:
        """One admission pass over one kind of job, then the record of what it left unplaced (C-6.11)."""
        if kind == "turn" and not self._holds_by_kind["turn"] and not self.store.one(
                "SELECT 1 FROM jobs WHERE state IN ('queued','waiting') AND kind='turn' "
                "AND cancel_requested_at IS NULL LIMIT 1"):
            # No turn to look at and none held by the last turn pass: nothing to
            # place or report. The control loop offers this pass every tick, and
            # a daemon held to a few percent of a core has no tick to spare.
            return
        lock = self._pass_locks[kind]
        if not lock.acquire(blocking=wait):
            return
        try:
            self._pass_seq[kind] += 1                 # C-24.4: notes of an older pass never land last
            seq = self._pass_seq[kind]
            holds: dict[str, dict] = {}
            tally = {"placed": 0}
            # A pass that raises leaves both as the last whole pass left them: half
            # a hold set would read as "nothing left pending" and end the idle
            # stretch with the queue untouched. C-5.10 logs and paces the failure.
            self._admit_pass(holds, tally, kind=kind)
            with self._admission_lock:
                self._holds_by_kind = {**self._holds_by_kind, kind: holds}
                self._holds = {**self._holds_by_kind["detached"], **self._holds_by_kind["turn"]}
        finally:
            lock.release()
        if kind == "turn":
            # C-24.4 (I3): every message a turn pass left waiting says why, and one it
            # placed says it is starting. Its failure is logged, never the pass's.
            try:
                self.conversations.note_holds(holds, placed=tally.get("placed_jobs", ()), seq=seq)
                self._note_error = None
            except Exception as exc:                      # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"[:300]
                if error != self._note_error:             # said once, not every tick
                    self.log.warning("admission: could not note why turns wait: %s", error)
                self._note_error = error
        # C-6.11 over both kinds' holds. The other pass never waits for the note
        # lock, and its placements are noted next time. `_note_admission` builds a
        # view at most once per ten minutes of idleness, which delays this pass's
        # next run by that one build.
        if self._note_lock.acquire(blocking=False):
            try:
                with self._admission_lock:
                    placed, self._placed_unnoted = self._placed_unnoted, 0
                    holds = self._holds
                self._note_admission({"placed": placed}, holds)
            finally:
                self._note_lock.release()

    def _count_route(self, **counts: int) -> None:
        """C-6.3: add to `daemon.status`'s `route_evaluations`; both passes count."""
        with self._route_count_lock:
            totals = dict(self._route_evaluations)
            for key, value in counts.items():
                totals[key] = totals.get(key, 0) + value
            self._route_evaluations = totals

    def _capacity_wait(self, job_id: str, signature: str, hold: dict, *, expedite: bool = True) -> int:
        """C-6.10: how many times in a row this job's wait has reached this verdict.

        The count follows the verdict; what is reported follows the latest look
        (C-6.11). A probe's reservation can make one verdict read `fleet-full`
        on one look and `reserve:fable:unmeasured` on the next, and the hold
        carries details (`leases`, `max_active_attempts`) that the passes between
        looks must still be able to give `why`. `expedite` is whether freed
        capacity may bring the next look forward; a probe's own 60 s wait may
        not be, or every released lease would re-probe the provider.
        """
        now = utcnow()
        wait = self._capacity_waits.get(job_id)
        same = bool(wait) and wait["signature"] == signature
        # Replaced whole, never updated in place: `why` reads it from another thread.
        self._capacity_waits[job_id] = {
            "signature": signature, "rechecks": wait["rechecks"] + 1 if same else 0,
            "since": wait["since"] if same else now, "checked_at": now,
            "label": hold["reason"], "hold": dict(hold), "expedite": expedite}
        return self._capacity_waits[job_id]["rechecks"]

    def _refresh_hold(self, job_id: str, hold: dict) -> None:
        """C-6.11: a look that changed no verdict and no clock still reports what it found."""
        wait = self._capacity_waits.get(job_id)
        if wait:
            self._capacity_waits[job_id] = {**wait, "checked_at": utcnow(), "label": hold["reason"], "hold": dict(hold)}

    def _note_admission(self, tally: dict, holds: dict[str, dict]) -> None:
        """C-6.11: say so in `daemon.log` when jobs are pending and nothing is placed.

        `self._admission` is replaced whole at the end: `daemon.status` reads it
        from another thread and must never see half of an update.
        """
        mine = {job_id: hold for job_id, hold in holds.items()
                if hold["reason"] not in NOT_ADMISSIONS_TO_PLACE}
        reasons: dict[str, int] = {}
        for hold in mine.values():
            reasons[hold["reason"]] = reasons.get(hold["reason"], 0) + 1
        state = {**self._admission, "pending": len(mine), "reasons": reasons}
        now = time.monotonic()
        try:
            if tally["placed"] or not mine:
                if state["logged_at"] is not None:
                    self.log.info("admission: placing again after %d s idle" if tally["placed"] else
                                  "admission: nothing left pending after %d s idle", now - state["idle_since"])
                if tally["placed"]:
                    state["placed_at"] = utcnow()
                state.update(idle_since=None, idle_since_at=None, checked_at=None, logged_at=None)
                return
            if state["idle_since"] is None:
                state.update(idle_since=now, idle_since_at=utcnow())
            # The first look is a minute in; after that the fleet is looked at
            # every ten minutes, so a wait that was expected when it was logged is
            # a warning within ten minutes of becoming one, not within the hour.
            wait = ADMISSION_IDLE_LOG_S if state["checked_at"] is None else ADMISSION_IDLE_REPEAT_S
            if now - (state["checked_at"] or state["idle_since"]) < wait:
                return
            state["checked_at"] = now
            try:
                lanes = capacity.open_lanes(self._capacity_view(self._desktop_identity()), self.policy["caps"])
            except Exception as exc:                      # the line matters more than its lane count
                lanes = None
                self.log.debug("admission: open lanes unreadable: %s", type(exc).__name__)
            # Open lanes, nothing placed, and a job held for a reason that is not
            # ordinary queueing is the case worth a warning: either every lane
            # refuses it for the reason named here, or admission is wrong. With no
            # lane open, or a fleet at its cap, waiting is the expected answer.
            warn = bool(lanes) and not set(reasons) <= EXPECTED_HOLDS
            if (state["logged_at"] is not None and not warn
                    and now - state["logged_at"] < ADMISSION_IDLE_REPEAT_EXPECTED_S):
                return
            state["logged_at"] = now
            summary = ", ".join(f"{reason} x{n}" for reason, n in
                                sorted(reasons.items(), key=lambda item: (-item[1], item[0])))
            self.log.log(logging.WARNING if warn else logging.INFO,
                         "admission: %d jobs pending, none placed for %d s; %s lanes open (%s); %s; first in line %s",
                         len(mine), now - state["idle_since"], "?" if lanes is None else len(lanes),
                         " ".join(lanes or ()) or "-", summary, next(iter(mine)))
        finally:
            self._admission = state

    def _admit_pass(self, holds: dict[str, dict], tally: dict, *, kind: str = "detached") -> None:
        """One pass over the queued jobs of one kind: `turn` or `detached` (C-26.9)."""
        if kind == "detached":
            self._recover_probes()
        desktop_account = self._desktop_identity()
        if kind == "detached":
            self._desktop_in_use()
            self._record_desktop_use()
        queued = self.store.query("SELECT * FROM jobs WHERE state IN ('queued','waiting') AND cancel_requested_at IS NULL ORDER BY created_at,rowid")
        # What admission remembers of a job goes when the job is no longer queued,
        # whichever pass notices. Each dict is walked as a copy: the other kind's
        # pass writes its own jobs' entries meanwhile (`dict.copy` is one C call).
        pending = {job["job_id"] for job in queued}
        tables = (self._capacity_waits, self._route_deferrals, self._retry_verdicts, self._turn_check_errors,
                  self._pin_episodes)
        gone = {job_id for table in tables for job_id in table.copy() if job_id not in pending}
        if gone:
            # A job submitted after the read above, whose entry the other pass has
            # just written, is not gone: asked again, by id.
            marks = ",".join("?" * len(gone))
            gone -= {row["job_id"] for row in self.store.query(
                f"SELECT job_id FROM jobs WHERE job_id IN ({marks}) AND state IN ('queued','waiting') "
                "AND cancel_requested_at IS NULL", tuple(gone))}
        for table in tables:
            for job_id in gone:
                table.pop(job_id, None)
        queued = [job for job in queued if self._in_pass(job, kind)]
        # C-6.10: a lease that was held at the last pass and is not now is capacity
        # that came free (an attempt ended, a job let go of its worktree or its
        # output path), so backed-off capacity waits are looked at on this pass
        # rather than up to 30 s later. Timer probes come and go every cycle and
        # free nothing a job was waiting for. A detached job's admission probe
        # (C-11.4) runs in the detached pass, beside the turn pass (C-26.9), and
        # holds its lane from every job while it runs: a turn held off that lane
        # waits for exactly that lease, so the turn pass counts it, and looks
        # again as soon as the probe ends rather than on its backed-off clock.
        probes = "holder NOT LIKE 'probe:timer:%'" if kind == "turn" else "holder NOT LIKE 'probe:%'"
        leases_now = frozenset((row["lease_key"], row["holder"]) for row in self.store.query(
            f"SELECT lease_key,holder FROM leases WHERE {probes}"))
        with self._admission_lock:                  # the other pass replaces its own entry meanwhile
            freed = bool(self._leases_seen.get(kind, frozenset()) - leases_now)
            self._leases_seen = {**self._leases_seen, kind: leases_now}
        if kind == "detached" and self._take_answer_news():
            freed = True                            # C-6.14: a pilot answered; its lane is free
        cap = policy_cap(self.policy["caps"], "max_active_attempts")
        # C-6.9: who is waiting on each detached job, and C-6.13: how busy the
        # machine is, each read once for the pass. A turn is always `attended` and
        # never held by the guard, so the turn pass reads neither and stays cheap.
        liveness = self._liveness(queued) if kind == "detached" else None
        priority_jobs = self._priority_jobs(queued) if kind == "detached" else {}
        reading = machine.read() if kind == "detached" else None
        # C-6.9: FIFO within a tier holds among jobs that compete for a model. An
        # older job that cannot be placed holds back the later jobs that could run
        # where it could, and nothing else: on 2026-09-20 an Opus review with no
        # admissible lane kept three Fable-pinned jobs queued for hours beside
        # eleven free Fable lanes.
        # Each waiter carries the leases it waits for another holder to release:
        # a job never waits behind a waiter for a lease the job itself holds.
        waiters: dict[str, list[tuple[str, frozenset[str] | None, frozenset[str] | None, frozenset[str]]]] = {}
        # C-6.9: job id -> its ancestors, for holds scoped to a family (`hold_scope`).
        ancestry: dict[str, frozenset[str]] = {job["job_id"]: frozenset() for job in queued
                                               if not job.get("parent_job_id")}
        # C-6.9, C-26.9: FIFO on a lease. A job held waiting for a lease (a writable
        # checkout, an output path, a conversation, a native session) queues for it
        # here, first in the pass's order, and no later job of this pass takes it:
        # without this, a lease released after the pass began went to whichever job
        # looked next, and with no count cap nothing else keeps a later job behind
        # an earlier one (review of 1b38d641 for turns; detached jobs lost C-6.9's
        # hold-back with their caps on 2026-09-27, so two writable jobs in one
        # checkout raced the same way).
        lease_queue: dict[str, str] = {}

        def queue_for(keys, job_id):
            # A lane slot is never queued for: each job takes the lowest free one.
            for key in keys:
                if not key.startswith("lane:"):
                    lease_queue.setdefault(key, job_id)
        roster = self._pin_roster()              # C-6.9: lane pins are compared by lane id (C-11.2)
        pin_views: list[dict] = []
        for_good_memo: dict = {}                 # C-11.8: `scheduler.unadmittable`'s, for this pass

        def pin_view() -> dict:
            """C-11.8: read once per pass, and only by a pass that needs it."""
            if not pin_views:
                pin_views.append(self._pin_view(desktop_account, refresh=kind == "detached"))
            return pin_views[0]
        # C-26.9: turns and detached jobs fill separate pools, so one being full
        # holds back only its own kind. Neither pool has a cap unless the policy
        # sets one (`conversations.max_active_turns`, `caps.max_active_attempts`):
        # an uncapped pool is never full.
        saturated: dict[str, bool] = {}
        turns_cap = turn_cap(self.policy.get("conversations"), "max_active_turns")
        # C-6.9, C-6.16: attended, priority, session, background. Priority is
        # FIFO across tiers; the other classes keep tier then FIFO.
        for job in scheduler.ordered_jobs(self.policy, queued, liveness, ancestors=priority_jobs):
            tier = job["tier"] or ("standard" if "standard" in self.policy["tiers"] else self.policy["tiers"][0])
            tier = scheduler.waiter_class(job, tier)     # C-26.9: turns queue apart from detached jobs
            if job["started_at"] and age(job["started_at"]) >= job["max_wall_s"]:
                self.kill(protocol.KillArgs(job["job_id"]))
                continue
            if job["wait_reason"] == "route" and job["next_check_at"] and job["next_check_at"] > utcnow():
                # C-6.12: not a capacity wait, so it holds nobody back (C-6.9) and
                # no released lease brings it forward. It says what it met.
                holds[job["job_id"]] = self._route_hold(job["job_id"], job["next_check_at"])
                continue
            if job["kind"] == "turn":
                # C-24.5, C-30.4: a turn job whose conversation became blocked after
                # it was created (the legacy import holds one while the daemon is
                # down) places nothing and holds nobody back until both blocks clear.
                try:
                    hold = self.conversations.admission_hold(job)
                    if hold and hold.get("error_type"):
                        # A turn manifest that cannot be read: held, and said once.
                        if self._turn_check_errors.get(job["job_id"]) != hold["error_type"]:
                            self.log.warning("admission: job %s held: %s", job["job_id"], hold.get("error"))
                        self._turn_check_errors[job["job_id"]] = hold["error_type"]
                    else:
                        self._turn_check_errors.pop(job["job_id"], None)
                except (sqlite3.Error, OSError):
                    raise                           # C-6.12: a store error is the pass's; C-5.10 retries it
                except Exception as exc:            # C-6.12: anything else is this job's, never the pass's
                    if self._turn_check_errors.get(job["job_id"]) != type(exc).__name__:
                        self.log.warning("admission: job %s: its conversation could not be checked: %s",
                                         job["job_id"], type(exc).__name__)
                    self._turn_check_errors[job["job_id"]] = type(exc).__name__
                    try:
                        manifest = self._read_json(self.root / "jobs" / job["job_id"] / "manifest.json")
                    except (OSError, ValueError):
                        manifest = None
                    turn = manifest.get("turn") if isinstance(manifest, dict) else None
                    hold = {"reason": "conversation-blocked",
                            "conversation_id": turn.get("conversation_id") if isinstance(turn, dict) else None,
                            "error_type": type(exc).__name__, "error": str(exc)[:200]}
                if hold:
                    holds[job["job_id"]] = hold
                    continue
            if job["pinned_lane"] and job["wait_reason"] not in ("approval", "uncertain"):
                # C-11.8: a job pinned to a lane that can never admit it is held here,
                # every pass, before it could wait behind or ahead of anyone (C-6.9):
                # it holds no other job back, its caller is told once, and it fails
                # once `admission.pin_grace_s` has passed with the lane still so.
                stuck = scheduler.pin_unadmittable(self.policy, pin_view(), job)
                if stuck:
                    self._stuck_pin(job, stuck, holds)
                    continue
                job = self._unstuck_pin(job)
            due = not (job["next_check_at"] and job["next_check_at"] > utcnow())
            if (job["kind"] != "turn" and job["wait_reason"] not in ("approval", "uncertain")
                    and (due or job["wait_reason"] != "workspace")):
                # C-6.13: at the door, before any git: a saturated machine holds the
                # detached jobs of the classes its guard names. Never a turn or
                # priority job. A due workspace retry is held too, before its git
                # (review of PR #72); one whose clock runs still reports `workspace`
                # (C-6.11).
                busy = scheduler.machine_hold(self.policy, reading, scheduler.priority_class(
                    job, liveness, policy=self.policy, jobs=priority_jobs))
                if busy:
                    holds[job["job_id"]] = busy
                    continue
            # C-4.5, C-6.9: while a transient retry is pinned to its last pair, the
            # job can run only there, and its demand is that one model on that lane.
            previous, extra_exclusions, retry = self._retry_pin(job)
            verdict = self._retry_verdicts.get(job["job_id"])
            let_go = bool(retry and verdict and verdict[0] == previous[-1]["attempt_id"] and not verdict[1]
                          and job["next_check_at"] and job["next_check_at"] > utcnow())
            models = scheduler.demand_models(self.policy, job if let_go else retry or job)
            lanes = scheduler.demand_lanes(roster, job if let_go else retry or job, self.policy)
            pool = "turn" if job["kind"] == "turn" else "detached"
            pool_cap = turns_cap if pool == "turn" else cap
            # C-6.9's FIFO exists so a later job cannot take the slot an older one
            # waits for. It holds only in a pool with a count one job could take
            # from another (`scheduler.pool_capped`): with no cap (the default) no
            # job waits for a slot, and an older job that cannot be placed (its lane
            # closed, its checkout leased) would otherwise keep every later one
            # waiting while lanes were free. A lease keeps its FIFO below.
            scope = scheduler.hold_scope(self.policy, job)
            family = self._ancestors(job, ancestry) if scope == "family" else frozenset()

            own_here: list[frozenset[str]] = []

            def own_leases():
                """The leases this job holds itself: a retry keeps its job-held ones."""
                if not own_here:
                    own_here.append(frozenset(row["lease_key"] for row in self.store.query(
                        "SELECT lease_key FROM leases WHERE holder=?", (job["job_id"],))))
                return own_here[0]

            def blocks(waiter):
                """Whether a waiter of this tier may keep a slot from this job: never
                one waiting for a lease this job holds, which moves only once this
                job has run (review of PR #72)."""
                return not (waiter[3] and waiter[3] & own_leases())

            def ahead(models, lanes):
                """The oldest waiter this job may not pass (C-6.9), or None. Never one
                waiting for a lease this job holds: that waiter moves only once this
                job has run, so holding this job behind it holds both until
                `max_wall_s` (review of PR #72: a retrying resume and a newer one
                of the same native session, the newer ordered first by class)."""
                if scope is None:
                    return None
                return next((waiter[0] for waiter in waiters.get(tier, ())
                             if scheduler.competes(models, waiter[1], lanes, waiter[2])
                             and (scope == "pool" or family & self._ancestors(waiter[0], ancestry))
                             and blocks(waiter)), None)
            behind = ahead(models, lanes)
            if saturated.get(pool) or behind:
                holds[job["job_id"]] = ({"reason": job["wait_reason"]} if job["wait_reason"] in NOT_ADMISSIONS_TO_PLACE else
                                        {"reason": "fleet-full", "max_active_attempts": pool_cap} if saturated.get(pool) else
                                        {"reason": "behind-older-job", "behind": behind, "tier": tier})
                continue
            if job["wait_reason"] in ("approval", "uncertain"):
                holds[job["job_id"]] = {"reason": job["wait_reason"]}
                continue
            known = self._capacity_waits.get(job["job_id"])
            if known and job["wait_reason"] != "capacity":
                # C-6.10: the wait is no longer a capacity wait. A workspace
                # deferral (C-6.8) took it over, and that clock counts retries: a
                # look brought forward by an unrelated lease would spend one, and
                # eight of them end the job.
                self._capacity_waits.pop(job["job_id"], None)
                known = None
            hurried = bool(freed and known and known["expedite"])
            if job["next_check_at"] and job["next_check_at"] > utcnow() and not hurried:
                # C-11.8: a job every lane refuses now for a reason no wait ends waits
                # for nothing another job could take, however its clock was set (a
                # transient retry's 60 s, or lanes that turned since its last look).
                # Asked only where a hold can follow (`hold_scope`), and once per
                # model and job shape per pass.
                lease_held = bool(known and known["hold"].get("reason") == "lease-held")
                asked = job["wait_reason"] == "capacity" and (scope is not None or lease_held)
                for_good = scheduler.unadmittable(self.policy, pin_view(), job, memo=for_good_memo) if asked else None
                if job["wait_reason"] == "capacity" and not for_good:
                    waiting_for = (frozenset(known["hold"].get("leases") or ()) if lease_held else frozenset())
                    waiters.setdefault(tier, []).append((job["job_id"], models, lanes, waiting_for))
                if lease_held and not for_good:
                    # C-6.9, C-11.8: nor does such a job keep a lease's place in its queue.
                    queue_for([*(known["hold"].get("leases") or ()), *(known["hold"].get("queued") or ())],
                              job["job_id"])
                last = dict(known["hold"]) if known else {"reason": job["wait_reason"] or "waiting"}
                if asked:
                    last.pop("for_good", None)          # C-6.11: this pass's answer, not the last look's
                holds[job["job_id"]] = {**last, **({"for_good": for_good} if for_good else {}),
                                        "next_check_at": job["next_check_at"]}
                continue
            if self.store.one("SELECT 1 FROM attempts WHERE job_id=? AND state IN ('reserved','starting','running','finalizing','quarantined')", (job["job_id"],)):
                holds[job["job_id"]] = {"reason": "attempt-live"}
                continue
            try:
                baseline_at = utcnow_ms()           # C-26.14: before the start snapshot
                workspace, head, baseline, skipped = self._workspace(job)
                pinned = self._pin_baseline(job, previous, workspace, head, baseline, skipped)
                native_session = job["caller_session"] if job["kind"] == "revive" else None
                if job["kind"] == "resume":
                    manifest = self._read_json(self.root / "jobs" / job["job_id"] / "manifest.json") or {}
                    native_session = (manifest.get("resume") or {}).get("native_session_id")
                    if not native_session:
                        raise AdapterError("resume source identity is missing",
                                           fix="resubmit the resume from the original job")
                if native_session and job["kind"] in ("resume", "revive"):
                    # C-26.13: the session became a conversation's after this job
                    # was submitted (a person opened it in the app). Refused, not
                    # held: waiting on the `native:` lease would only take turns
                    # with the conversation.
                    self._refuse_conversation_session(native_session, job["kind"])
            except AdapterError as exc:
                self._fail_queued(job, str(exc) + (f"; fix: {exc.fix}" if exc.fix else ""), rc=exc.code)
                continue
            except (OSError, subprocess.SubprocessError, SalvageError) as exc:
                self._workspace_failed(job, exc)
                self._capacity_waits.pop(job["job_id"], None)      # C-6.10: the wait is C-6.8's now
                holds[job["job_id"]] = {"reason": "workspace", "error_type": type(exc).__name__,
                                        "error": str(exc)[:200]}
                continue
            self._workspace_deferrals.pop(job["job_id"], None)
            write_target = self._write_target(job, workspace) if job["sandbox"] == "workspace-write" else None
            # C-8.4: the folder a read-only turn works in, which retention leaves alone while it runs.
            read_folder = (self._submitted(job["job_id"]).get("folder") or workspace
                           if job["kind"] == "turn" and write_target is None else None)
            if job["wait_reason"] == "workspace":
                # The workspace is ready; what the job waits for next is not it.
                with self.store.transaction("job.workspace_ready", job_id=job["job_id"]) as tx:
                    tx.execute("UPDATE jobs SET state='queued',wait_reason=NULL,next_check_at=NULL "
                               "WHERE job_id=? AND state='waiting' AND wait_reason='workspace'", (job["job_id"],))
                job = {**job, "state": "queued", "wait_reason": None, "next_check_at": None}
            decision_job = job
            turn_block = None
            if job["kind"] == "turn":
                turn_block = (self._read_json(self.root / "jobs" / job["job_id"] / "manifest.json") or {}).get("turn") or {}
                if turn_block.get("affinity_lane"):
                    decision_job = {**job, "affinity_lane": turn_block["affinity_lane"]}   # C-26.2
            if retry:
                retry = {**job, "pinned_lane": retry["pinned_lane"], "pinned_model": retry["pinned_model"]}
                kept = self._retry_waits_on_a_slot(retry, extra_exclusions, desktop_account)
                self._retry_verdicts[job["job_id"]] = (previous[-1]["attempt_id"], kept)
                if kept:
                    decision_job = retry
                    models = scheduler.demand_models(self.policy, retry)
                    lanes = scheduler.demand_lanes(roster, retry, self.policy)
                else:
                    # C-4.5 "then next candidate": the pair's lane refuses it for
                    # something a slot will not end, so the job routes as submitted,
                    # and as submitted it keeps C-6.9's place behind older jobs.
                    models = scheduler.demand_models(self.policy, job)
                    lanes = scheduler.demand_lanes(roster, job, self.policy)
                    behind = ahead(models, lanes)
                    if behind:
                        # C-6.10: held after a look, so on a clock like every other
                        # such hold; until it is due the job is held at the top of
                        # the pass with its own demand, with no git and no scoring.
                        hold = {"reason": "behind-older-job", "behind": behind, "tier": tier}
                        rechecks = self._capacity_wait(job["job_id"], f"retry-let-go:behind:{behind}", hold)
                        next_check = after(scheduler.capacity_recheck_delay(rechecks))
                        with self.store.transaction("job.retry_let_go", job_id=job["job_id"]) as tx:
                            tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=? "
                                       "WHERE job_id=? AND state IN ('queued','waiting') AND cancel_requested_at IS NULL",
                                       (next_check, job["job_id"]))
                        holds[job["job_id"]] = {**hold, "next_check_at": next_check}
                        continue
            try:
                approved, desktop_account = self._prepare_route(job, decision_job, extra_exclusions)
            except Unroutable as exc:
                self._early_routes.pop(job["job_id"], None)
                self._unroutable(job, exc, holds)
                continue
            early = self._early_routes.pop(job["job_id"], None)
            # C-6.12: this pass could evaluate the route, so the next failure starts the count again.
            self._route_deferrals.pop(job["job_id"], None)
            if approved is None:
                waiters.setdefault(tier, []).append((job["job_id"], models, lanes, frozenset()))
                holds[job["job_id"]] = {"reason": "probe-pending"}
                current = self._job(job["job_id"])
                clocked = current["next_check_at"] and current["next_check_at"] > utcnow()
                if current["state"] not in TERMINAL and not current["cancel_requested_at"] and not clocked:
                    # C-6.10: the route could not be prepared and nothing set a
                    # clock (the probe's slot is held, the lane changed hands). The
                    # job would otherwise be prepared, and a probe directory made,
                    # on every 50 ms tick for as long as that lasts.
                    rechecks = self._capacity_wait(job["job_id"], "probe-pending", holds[job["job_id"]])
                    next_check = after(scheduler.capacity_recheck_delay(rechecks))
                    with self.store.transaction("job.probe_deferred", job_id=job["job_id"]) as tx:
                        tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=? "
                                   "WHERE job_id=? AND state IN ('queued','waiting') AND cancel_requested_at IS NULL",
                                   (next_check, job["job_id"]))
                    holds[job["job_id"]]["next_check_at"] = next_check
                elif clocked:
                    holds[job["job_id"]]["next_check_at"] = current["next_check_at"]
                    self._refresh_hold(job["job_id"], holds[job["job_id"]])
                continue
            # C-6.3, C-3.7: the route is evaluated off the store lock, on rows read
            # in one snapshot (`_capacity_rows`): `_prepare_route`'s evaluation is
            # the one reserved on. The reserving transaction never evaluates a
            # route. It checks the decision against the rows it reads, a few by
            # index (`_route_stands`), at its own clock: the lanes the decision
            # looks at whose rows changed, or whose own clock reached its horizon
            # (a reading aged out, a closure ended), are judged again, and every
            # lane of the walk when a fleet or parent cap began or ended. It goes
            # on with exactly the decision an evaluation of those rows would make
            # at that clock, the same lane or another. Only a pin that names
            # another lane now (or an evaluation that would raise) rolls it back;
            # the route is evaluated again, off the lock, and checked again, up
            # to ROUTE_TRIES times in this pass. A clock on a lane the job could
            # never take (another provider's, one its pin does not name) is never
            # looked at: sixty Claude lanes' staggered readings refused every
            # check of a Codex job for as long as they were refreshed (review of
            # d04b8b3), and a cap that flipped between each evaluation and its
            # check refused them as long as it flipped (review of 5d14f98). Evaluating a whole capacity
            # view inside this transaction held the store lock for 13.5 s with the
            # daemon held to 5% of a core, and for 7.7 to 12.6 s on 2026-09-26
            # (load 110-170), when some commit landed between the two evaluations
            # nearly every time.
            if early is None:
                # Nothing handed over (a `_prepare_route` that stops early): evaluate here, off the lock.
                basis = {}
                try:
                    early = (self._route(decision_job, extra_exclusions=extra_exclusions, desktop=desktop_account,
                                         basis=basis), basis)
                except Unroutable as exc:
                    self._unroutable(job, exc, holds)
                    continue
            decision, basis = early
            status, route, last, probed = "moved", {"failed": False}, None, frozenset()
            for tries in range(1, ROUTE_TRIES + 1):
                # C-10.3: refreshed off the lock, at most `REGISTRY_READ_TTL_S` old; the
                # check inside reads the answer without touching the registry. A
                # change it places by is recorded (review of PR #72).
                self._desktop_in_use()
                self._record_desktop_use()
                try:
                    # C-6.12: outside the transaction, so a route that fails here rolls it back first.
                    with self._isolated_route(job, holds) as route, \
                            self.store.transaction("attempt.reserved", job_id=job["job_id"]) as tx:
                        job = self._job(job["job_id"])
                        if job["cancel_requested_at"] or job["state"] in TERMINAL:
                            status = "gone"
                            break
                        if tx.execute("SELECT 1 FROM attempts WHERE job_id=? AND state IN "
                                      "('reserved','starting','running','finalizing','quarantined')",
                                      (job["job_id"],)).fetchone():
                            # Checked again here: one job, one attempt at a time, whichever pass got there.
                            holds[job["job_id"]] = {"reason": "attempt-live"}
                            status = "held"
                            break
                        why, judged, standing = self._route_stands(basis, decision)
                        if why is not None:
                            raise _RouteMoved(why, judged)
                        # The decision as an evaluation now makes it, from the lanes
                        # whose rows changed: the same lane, or the one that now
                        # ranks first; a wait recorded on it is clocked by now's
                        # verdict (C-6.10).
                        same = (standing.chosen_lane, standing.chosen_model) == (decision.chosen_lane,
                                                                                decision.chosen_model)
                        decision = standing
                        self._count_route(**{"reused" if same else "rechosen": 1}, rejudged=judged)
                        if extra_exclusions:
                            job["exclusions"] = json.dumps(sorted(set(json.loads(job["exclusions"])) | set(extra_exclusions)))
                            tx.execute("UPDATE jobs SET exclusions=? WHERE job_id=?", (job["exclusions"], job["job_id"]))
                        needs_probe = self._needs_probe(decision, job)
                        live = tx.execute("SELECT count(*) FROM attempts a JOIN jobs j USING(job_id) "
                                          "WHERE a.state IN ('reserved','starting','running','finalizing') "
                                          "AND (j.kind = 'turn') = ?", (pool == "turn",)).fetchone()[0]
                        saturated[pool] = pool_cap is not None and live >= pool_cap
                        # A job that passes an older waiting job of its tier leaves one
                        # active slot free, so the older job can start the moment its
                        # capacity appears instead of waiting out the jobs that passed it.
                        # An uncapped pool (C-26.9) has no last slot to keep.
                        kept = [waiter for waiter in waiters.get(tier, ()) if blocks(waiter)]
                        limit = None if pool_cap is None else pool_cap - 1 if kept else pool_cap
                        at_limit = limit is not None and live >= limit
                        if not decision.chosen_lane or at_limit:
                            # C-11.8: a job every lane refuses for a reason no wait ends
                            # (the pinned lane turned so since this pass's check) waits
                            # for no slot, so it holds no later job back (C-6.9).
                            for_good = (None if decision.chosen_lane
                                        else scheduler.refused_for_good(self.policy, decision, decision_job,
                                                                        pin_view()["lanes"]))
                            if not for_good:
                                waiters.setdefault(tier, []).append((job["job_id"], models, lanes, frozenset()))
                            # C-6.10: a wait that reaches the verdict it reached last time
                            # is rechecked later each time and adds no decision row. On
                            # 2026-09-20 three such jobs were each re-evaluated every
                            # second for hours: 2.7 rows of 22 KB a second, 681 MB of a
                            # 709 MB store, and a daemon at a full core doing it.
                            if not decision.chosen_lane:
                                label = scheduler.dominant_rejection(decision)
                            else:
                                # A lane would take it. Either the fleet is at its cap, or
                                # C-6.9 keeps the last slot for an older job of this tier.
                                label = "fleet-full" if saturated[pool] else "slot-kept"
                            hold = {"reason": label, **({"for_good": for_good} if for_good else {}),
                                    **({"max_active_attempts": pool_cap} if label == "fleet-full" else {}),
                                    **({"kept_for": kept[0][0], "tier": tier, "live": live,
                                        "max_active_attempts": pool_cap} if label == "slot-kept" else {})}
                            if kind == "turn" and not decision.chosen_lane:
                                # C-24.4, C-29.11: what each lane said, for the message's
                                # reason (`conversations.waits`), never just "capacity".
                                from .conversations import waits as turn_waits
                                hold["lanes"] = turn_waits.lane_summary(decision)
                            rechecks = self._capacity_wait(
                                job["job_id"], f"{scheduler.verdict_signature(decision)}:{at_limit}", hold)
                            waiting = scheduler.waiting_metadata(decision, rechecks=rechecks)
                            if not rechecks:
                                self.store.add_decision(job["job_id"], decision)
                            tx.execute("UPDATE jobs SET state='waiting',wait_reason=?,next_check_at=? WHERE job_id=?",
                                       (waiting["wait_reason"], waiting["next_check_at"], job["job_id"]))
                            holds[job["job_id"]] = {**hold, "next_check_at": waiting["next_check_at"]}
                            status = "held"
                            if kind == "turn":
                                probed = self._probes_holding(decision)
                            break
                        if needs_probe and (decision.chosen_lane, decision.chosen_model) not in approved:
                            # The chosen identity changed after its probe; a later pass
                            # probes the new pair (`_prepare_route`). C-6.10: on a clock,
                            # or a lane whose state keeps moving is probed every tick.
                            waiters.setdefault(tier, []).append((job["job_id"], models, lanes, frozenset()))
                            hold = {"reason": "probe-pending"}
                            rechecks = self._capacity_wait(job["job_id"], "probe-pending", hold)
                            next_check = after(scheduler.capacity_recheck_delay(rechecks))
                            tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=? WHERE job_id=?", (next_check, job["job_id"]))
                            holds[job["job_id"]] = {**hold, "next_check_at": next_check}
                            status = "held"
                            break
                        seq = len(previous) + 1
                        aid = ids.attempt_id(job["job_id"], seq)
                        lane_id = decision.chosen_lane
                        leases = [(self._slot_lease(tx, lane_id, pool == "turn"), aid)]
                        if native_session:
                            # C-12.3/4, C-12.6: filesystem read-only permissions do not
                            # isolate a provider transcript. Resume and revive share
                            # this job-held lease through retry, export and quarantine.
                            leases.append((native_session_lease_key(lane_id, native_session), job["job_id"]))
                            # C-26.3: one writer per native session whichever lane, so a
                            # resume or revive and a conversation turn exclude each other.
                            provider = self.policy["models"][decision.chosen_model]["provider"]
                            leases.append((f"native:{provider}:{native_session}", job["job_id"]))
                        if turn_block is not None:
                            leases.append((f"conversation:{turn_block['conversation_id']}", job["job_id"]))
                            if turn_block.get("native_session_id") or turn_block.get("new_session_id"):
                                sid = turn_block.get("native_session_id") or turn_block.get("new_session_id")
                                leases.append((f"native:{turn_block['provider']}:{sid}", job["job_id"]))
                                held = tx.execute("SELECT lease_key FROM leases WHERE lease_key LIKE ? AND holder<>?",
                                                  (f"native-session:%:{sid}", job["job_id"])).fetchone()
                                if held:
                                    leases.append((held[0], job["job_id"]))   # contested: waits for the resume/revive
                        if job.get("round_lease"):
                            leases.append((job["round_lease"], f"gate-round:{job['job_id']}"))
                        if job["out_path"]:
                            leases.append((f"out:{job['out_path']}", job["job_id"]))
                        # Keys this job needs free but does not take (C-6.5, C-24.5): a turn
                        # shares its folder with other turns, so it never holds the
                        # exclusive key, yet it may not write beside a detached writer.
                        blockers: list[str] = []
                        read = lambda sql, params: tx.execute(sql, params).fetchall()   # noqa: E731
                        if job["sandbox"] == "workspace-write" and job["kind"] == "turn":
                            # C-24.5 (the owner's ruling of 2026-09-28, "nothing should be
                            # queued"): conversations that share a folder run at once, as
                            # sessions of the Claude app do. Each turn holds its own row, so
                            # retention and a detached writer still see the folder in use.
                            leases.append((folders.turn_key(write_target, job["job_id"], writable=True), job["job_id"]))
                            blockers.append(folders.exclusive_key(write_target))
                        elif job["sandbox"] == "workspace-write":
                            # C-6.5: the hold is where the job writes. A session is not a
                            # place, so it takes no lease; its instances are told apart
                            # at submit. A detached writer still writes alone: it waits
                            # while a conversation turn writes there.
                            leases.append((f"worktree:{write_target}", job["job_id"]))
                            blockers.extend(key for key, _ in folders.turn_holds(read, write_target, (folders.TURN,)))
                        elif job["kind"] == "turn" and read_folder:
                            # C-8.4, C-13.4: a read-only turn excludes no writer, but
                            # retention never removes a folder a turn is working in.
                            leases.append((folders.turn_key(read_folder, job["job_id"], writable=False), job["job_id"]))
                            fence = tx.execute("SELECT holder FROM leases WHERE lease_key=?",
                                               (folders.exclusive_key(read_folder),)).fetchone()
                            if fence and str(fence[0]).startswith("retention:"):
                                blockers.append(folders.exclusive_key(read_folder))
                        revive_key = (revive_lease_key(job["caller_session"])
                                      if job["kind"] == "revive" and job["caller_session"] else None)
                        if revive_key:
                            # C-23.55: the census the sweep skips on is the lease rows,
                            # read inside the admitting transaction, not a snapshot taken
                            # at the start of the pass.
                            held = tx.execute("SELECT holder FROM leases WHERE lease_key=?",
                                              (revive_key,)).fetchone()
                            if held and held[0] != job["job_id"]:
                                # Skipped, not queued: waiting for the other revive to
                                # end would launch the twin the moment it did.
                                self._skip_revive(tx, job, held[0])
                                status = "held"
                                break
                            leases.append((revive_key, job["job_id"]))
                        current = {key: r[0] for key, _ in leases
                                   if (r := tx.execute("SELECT holder FROM leases WHERE lease_key=?", (key,)).fetchone())}
                        contested = [key for key, holder in leases if key in current and current[key] != holder]
                        blocked = [key for key in dict.fromkeys(blockers)
                                   if (r := tx.execute("SELECT holder FROM leases WHERE lease_key=?", (key,)).fetchone())
                                   and r[0] != job["job_id"]]
                        # A lease this job already holds (a retry keeps its job-held
                        # ones) is never queued behind a job waiting for it: that job
                        # waits for this one to run and release it (review of PR #72).
                        queued = [key for key, holder in leases if key not in current
                                  and lease_queue.get(key, job["job_id"]) != job["job_id"]]
                        if contested or blocked or queued:
                            waiters.setdefault(tier, []).append((job["job_id"], models, lanes, frozenset(contested + blocked)))
                            # `leases` are held by another job; `queued` are free but kept for
                            # an older job waiting for them (C-6.9, C-26.9), named by `queued_behind`.
                            # A key it only needs free (`blocked`) is never queued for: turns
                            # share their folder, so no turn takes it from another, and no
                            # detached job takes a turn's row. It is among the keys the waiter
                            # waits for, so a job holding it is never held behind the waiter.
                            hold = {"reason": "lease-held", "leases": contested + blocked,
                                    **({"queued": queued, "queued_behind": sorted({lease_queue[key] for key in queued})}
                                       if queued else {})}
                            queue_for(contested + queued, job["job_id"])
                            rechecks = self._capacity_wait(job["job_id"], "lease-held:" + ",".join(sorted(contested + blocked + queued)), hold)
                            next_check = after(scheduler.capacity_recheck_delay(rechecks))
                            tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=? WHERE job_id=?", (next_check, job["job_id"]))
                            holds[job["job_id"]] = {**hold, "next_check_at": next_check}
                            status = "held"
                            break
                        for key, holder in leases:
                            tx.execute("INSERT OR IGNORE INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)", (key, holder, utcnow()))
                        evidence = {"baseline_commit": head, "model_short": decision.chosen_model,
                                    **({"baseline_ref": pinned["path"]} if pinned else {}),
                                    **({"baseline_skipped": _skipped(skipped)} if skipped else {})}
                        if job["kind"] == "turn":
                            # C-26.14: the turn's window opens before its start snapshot, and
                            # its folder is where another turn's window may overlap it.
                            evidence.update(baseline_at=baseline_at, folder=write_target or read_folder)
                        tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,baseline_tree,evidence_json,reserved_at) VALUES(?,?,?,?,?,'reserved',?,?,?)",
                                   (aid, job["job_id"], seq, lane_id, self.policy["models"][decision.chosen_model]["id"], baseline,
                                    json.dumps(evidence), utcnow()))
                        if pinned:
                            self.store.add_artifact(aid, **pinned)      # C-13.1: kept, as a salvage ref is
                        tx.execute("INSERT INTO decisions(job_id,attempt_id,evaluated_at,policy_hash,decision_json) VALUES(?,?,?,?,?)",
                                   (job["job_id"], aid, utcnow(), self.policy_digest, json.dumps(dataclasses.asdict(decision))))
                        tx.execute("UPDATE jobs SET state='running',wait_reason=NULL,next_check_at=NULL,worktree=?,started_at=COALESCE(started_at,?) WHERE job_id=?",
                                   (workspace if job["sandbox"] == "workspace-write" else None, utcnow(), job["job_id"]))
                        with self._busy_lock:
                            self._busy.add(aid)
                        status = "placed"
                except _RouteMoved as moved:
                    # C-6.3: rolled back, nothing written. Evaluated again off the
                    # lock, on a new snapshot, and checked again.
                    self._count_route(**{moved.why: 1}, rejudged=moved.judged)
                    last = moved
                    if tries == ROUTE_TRIES:
                        break
                    self._count_route(again=1)
                    basis = {}
                    try:
                        decision = self._route(decision_job, extra_exclusions=extra_exclusions,
                                               desktop=desktop_account, basis=basis)
                    except Unroutable as exc:
                        self._unroutable(job, exc, holds)
                        status = "settled"
                        break
                    continue
                break
            if probed:
                # C-6.10, C-26.9: a turn held off a lane by a detached job's admission
                # probe waits for exactly that lease, whenever the probe began: it is
                # counted as seen, so the next turn pass finds it gone the moment the
                # probe ends, and looks at once (review of 5d14f98: a probe that began
                # after this pass read its leases went unseen, and the turn waited out
                # its backed-off clock).
                with self._admission_lock:
                    self._leases_seen = {**self._leases_seen,
                                         kind: self._leases_seen.get(kind, frozenset()) | probed}
            if route["failed"]:
                continue
            if status == "moved" and last is not None and last.error is not None:
                self._unroutable(job, Unroutable(last.error), holds)     # C-6.12: named, backed off, warned
                continue
            if status == "moved":
                # C-6.3: ROUTE_TRIES evaluations in a row were overtaken by commits
                # before they could be reserved. The job keeps its place: the later
                # jobs it competes with wait behind it (C-6.9), and the next pass,
                # which follows this one at once, looks at it again.
                self._count_route(deferred=1)
                if not scheduler.refused_for_good(self.policy, decision, decision_job, pin_view()["lanes"]):  # C-11.8
                    waiters.setdefault(tier, []).append((job["job_id"], models, lanes, frozenset()))
                holds[job["job_id"]] = {"reason": "route-moved", "tries": ROUTE_TRIES}
                self._refresh_hold(job["job_id"], holds[job["job_id"]])     # C-6.11: the last look's finding
                continue
            if status != "placed":
                continue
            tally["placed"] += 1
            tally.setdefault("placed_jobs", []).append(job["job_id"])
            self._capacity_waits.pop(job["job_id"], None)
            # C-6.10: taken after this pass's snapshot. If the attempt ends before
            # the next one, that is a release the next pass must still see.
            with self._admission_lock:
                self._placed_unnoted += 1
                self._leases_seen = {**self._leases_seen,
                                     kind: self._leases_seen.get(kind, frozenset()) | frozenset(leases)}
            try:
                self._boundary("reserved", job["job_id"], aid)
                self._pending_launches.add(aid)
            finally:
                with self._busy_lock:
                    self._busy.discard(aid)
            self._notify()

    @staticmethod
    def _probes_holding(decision) -> frozenset[tuple[str, str]]:
        """C-11.4, C-26.9: the admission probes' leases (`lane:<id>:slot:0`) that kept a
        lane from `decision`, as its rejections name them (`slot_block`). Timer probes
        are left out: they come and go every cycle."""
        return frozenset((f"lane:{row['lane_id']}:slot:0", row["slot_block"])
                         for evaluation in decision.evaluations for row in evaluation["rejections"]
                         if str(row.get("slot_block") or "").startswith("probe:")
                         and not str(row["slot_block"]).startswith("probe:timer:"))

    @staticmethod
    def _in_pass(job: dict, kind: str) -> bool:
        """C-26.9: whether `job` is the `kind` pass's: turns are the turn pass's, every other job the detached pass's."""
        return (job["kind"] == "turn") == (kind == "turn")

    @staticmethod
    def _slot_lease(tx, lane_id: str, turn: bool) -> str:
        """C-6.3, C-26.9: the lowest free slot lease of the job's pool on `lane_id`,
        read inside the reserving transaction.

        A detached attempt takes `lane:<lane id>:slot:<n>`, a turn
        `lane:<lane id>:slot:turn-<n>`: the two pools are counted apart
        (`scheduler.prepare`, C-26.9), and a turn never holds `slot:0`, the lease
        a detached job's admission probe takes (C-11.4). Numbered together, the
        turn pass, which runs first, gave each new turn `slot:0`, and an older
        writable job waiting on a probe of that lane stayed `probe-pending` for as
        long as turns kept coming (review of d04b8b3: ten in a row, and no
        probe). A detached attempt never holds `slot:0` either (2026-09-27): with
        no per-lane cap a busy lane nearly always had an attempt on `slot:0`, and
        an older writable job waiting to probe it lost the slot to each later job
        that needed no probe (review of the uncap plan). So a probe waits only for
        another probe. Both keep the `lane:<lane id>:slot:` prefix every lane
        fence reads: re-enrolment, `lanes transfer`, `login`, the timers' probes."""
        prefix = f"lane:{lane_id}:slot:" + ("turn-" if turn else "")
        slot = 0 if turn else 1
        while tx.execute("SELECT 1 FROM leases WHERE lease_key=?", (f"{prefix}{slot}",)).fetchone():
            slot += 1
        return f"{prefix}{slot}"

    def _route_stands(self, basis: dict, decision) -> tuple[str | None, int, Any]:
        """C-6.3: inside the reserving transaction, the decision as an evaluation now makes it.

        (None, lanes judged again, that decision): exactly what
        `scheduler.evaluate` over the rows this transaction sees, at this clock,
        would return (`route_check.still_stands`). Otherwise the reason not
        (`moved`, `old`), and the transaction is rolled back. It reads a few rows
        by index and judges again, in memory, only the lanes the decision looks
        at whose rows changed, whose override began or ended, or whose own clock
        reached its horizon (`capacity.lane_horizons`, recorded off the lock), and
        every lane of the walk when a fleet or parent cap began or ended: it
        never builds a capacity view, reads the readings table whole, or calls
        `evaluate`. It refuses what that comparison cannot see: a pin that names
        another lane now, a policy loaded since, a clock earlier than the view's,
        and readings the snapshot held that are gone (`_route_rows`)."""
        if not basis or basis.get("policy") is not self.policy:
            return "moved", 0, None
        now = datetime.now(timezone.utc)
        if now < basis["instant"]:
            return "old", 0, None                  # a clock that stepped back
        try:
            rows = self._route_rows(basis, now)
            if rows is None:
                return "moved", 0, None
            return route_check.still_stands(basis["policy"], basis["job"], decision, view=basis["view"],
                                            candidates=basis["rows"]["view"]["readings"],
                                            overridden=set(basis["overrides"]), clocks=basis["clocks"],
                                            now=now, **rows)
        except route_check.ROUTE_ERRORS as exc:
            # C-6.12: a row the check cannot read (a timestamp that does not parse)
            # is the job's, never the pass's: the route is evaluated again off the
            # lock, where the same row settles this one job as `Unroutable`. A
            # check that raises on every try settles it so too, with its error
            # named, never as a silent `route-moved` (review of this change).
            raise _RouteMoved("error", 0, error=exc) from exc

    def _route_rows(self, basis: dict, now: datetime) -> dict | None:
        """C-6.3: what `route_check.still_stands` reads now, inside the reservation.

        Every read is a few rows by index: the lane rows (marked and merged as a
        view does), the attempts in flight (`attempts_live`), the probe leases,
        the readings added since the snapshot (by id), the closures not released
        (`closures_active`), the lanes a reset-credit override covers now
        (`holding`), and the parents of the jobs whose caps are counted (the
        lane, probe-lease and reset-credit tables are small and read whole).
        None when a reading the snapshot held was deleted: then the rows it
        rests on cannot be rebuilt from these, and the route is evaluated
        again. Retention deletes readings only with a pruned job's attempts, and
        a reading id is reused only after the newest reading is deleted, so
        while the snapshot's newest reading stands every reading added since has
        a greater id."""
        store, rows = self.store, basis["rows"]
        lane_rows = store.query("SELECT * FROM lanes ORDER BY lane_id")
        # C-23.17: which lanes a confirmed reset-credit override covers, now, at a
        # view's clock. Whether one covers a lane turns on the lane's own row
        # (`Actions._belongs_to_lane`: its account key, its home), so a lane
        # enrolled or moved since the snapshot can gain or lose one with no
        # action changing (review of 9dd4f35: a hard job was placed without its
        # probe on a lane enrolled meanwhile, its readings not held out). A lane
        # whose override began or ended is judged again with its readings held
        # out or put back, as a view built now holds them; one the decision does
        # not look at changes nothing (review of d04b8b3).
        context = {**self.timers.actions.override_context(),
                   "lanes": {row["lane_id"]: Store.lane_from_row(row) for row in lane_rows}}
        clock = capacity._iso(now)
        holding = {row["lane_id"] for row in lane_rows
                   if self.timers.actions.confirmed_override(row["lane_id"], now=clock, context=context)}
        mark = rows["reading_mark"]
        if mark is not None and store.one("SELECT * FROM readings WHERE reading_id=?", (mark["reading_id"],)) != mark:
            return None
        held = [row["reading_id"] for row in rows["view"]["readings"]]
        for start in range(0, len(held), 500):
            chunk = held[start:start + 500]
            found = store.one(f"SELECT count(*) AS n FROM readings WHERE reading_id IN ({','.join('?' * len(chunk))})",
                              chunk)
            if found["n"] != len(chunk):
                return None
        # C-10.3: whether Claude Code uses the desktop login now, as last read off
        # the lock (`_desktop_in_use`, refreshed before each reservation try): a
        # change since the early view changes the desktop lane's facts, and the
        # check judges that lane again (review of the uncap plan: reusing the
        # view's answer reserved the lane after Claude Code had become active).
        in_use = self._desktop_answer()
        lanes = [self.timers.merge_lane(capacity.mark_desktop(
                     dict(row), desktop=basis["desktop"],
                     desktop_in_use=basis.get("desktop_in_use") if in_use is None else in_use))
                 for row in lane_rows]
        probes = store.query("SELECT lease_key,holder FROM leases WHERE holder LIKE 'probe:%'")
        unavailable = {row["lease_key"].split(":")[1]: row["holder"] for row in probes}
        unavailable.update({lane["lane_id"]: "credential-latched" for lane in lanes if capacity.credential_latched(lane)})
        attempts = store.query("SELECT a.attempt_id,a.job_id,a.lane_id,a.state,a.reserved_at,j.kind,j.parent_job_id "
                               "FROM attempts a JOIN jobs j USING(job_id) "
                               "WHERE a.state IN ('reserved','starting','running','finalizing')")
        # C-6.14: each lane's pilot, from the attempts in flight now and what has
        # answered by now, as `_capacity_view` lays them: an attempt placed or an
        # answer heard since the early view changes the lane, which is judged again.
        for lane_id, pilot in self._pilot_marks(attempts, capacity._time(capacity._iso(now))).items():
            unavailable.setdefault(lane_id, pilot)
        # C-6.9's parent cap counts the attempts under each of the job's parents:
        # every job on those ancestries, from the snapshot or, if newer, by id.
        known = {row["job_id"]: row for row in rows["view"]["jobs"]}
        jobs: dict[str, dict] = {}
        unseen = [basis["job"]["job_id"], *(row["job_id"] for row in attempts)]
        while unseen:
            job_id = unseen.pop()
            if job_id is None or job_id in jobs:
                continue
            row = known.get(job_id) or store.one("SELECT job_id,kind,parent_job_id FROM jobs WHERE job_id=?", (job_id,))
            if row is not None:
                jobs[job_id] = {"job_id": row["job_id"], "kind": row["kind"], "parent_job_id": row["parent_job_id"]}
                unseen.append(row["parent_job_id"])
        return {"lanes": lanes, "attempts": attempts, "jobs": list(jobs.values()), "unavailable": unavailable,
                "reserved_probes": len(probes), "holding": holding,
                "readings": store.query("SELECT * FROM readings WHERE reading_id>? ORDER BY reading_id",
                                        (mark["reading_id"] if mark is not None else 0,)),
                "closures": self._open_closures(basis)}

    def _open_closures(self, basis: dict) -> list[dict]:
        """C-6.3: the closures a view counts as open, by index: those with no
        `released_at`, and any the snapshot's view held with an empty one (which
        only a hand edit writes, and which a view counts as open too), by id."""
        rows = self.store.query("SELECT * FROM closures WHERE released_at IS NULL ORDER BY closure_id")
        odd = [row["closure_id"] for row in basis["view"].get("closures", ()) if row.get("released_at") is not None]
        if odd:
            rows += self.store.query(f"SELECT * FROM closures WHERE closure_id IN ({','.join('?' * len(odd))}) "
                                     "AND released_at IS NOT NULL", odd)
            rows.sort(key=lambda row: row["closure_id"])
        return rows

    @contextlib.contextmanager
    def _isolated_route(self, job: dict, holds: dict[str, dict]):
        """C-6.12: an `Unroutable` from the block settles this job and ends the block, not the pass.

        Entered outside the reserving transaction, so the transaction has rolled
        back before the job is settled; the caller reads `failed` after the block.
        """
        route = {"failed": False}
        try:
            yield route
        except Unroutable as exc:
            route["failed"] = True
            self._unroutable(job, exc, holds)

    def _discard_fresh_worktree(self, job_id: str) -> None:
        """C-6.12: the worktree admission cut for a job it then refused.

        `_workspace` allocates `worktrees/<job id>/` before the route is
        evaluated; `jobs.worktree` is set only when an attempt is reserved, so
        retention would never collect one that no attempt used.
        """
        job = self.store.get_job(job_id)
        path = self.root / "worktrees" / job_id
        if (job and job["state"] in TERMINAL and job["sandbox"] == "workspace-write" and not job["in_place"]
                and not job["worktree"] and path.exists() and not self.store.list_attempts(job_id)):
            self._discard_worktree(job["workdir"], str(path), self.policy["caps"]["workspace_git_timeout_s"])

    def _load_pin_episodes(self) -> dict[str, dict]:
        """C-11.8: each unfinished job's pin episode as its events left it.

        The latest of a job's `job.pin_unadmittable` and `job.pin_admittable`
        events says whether an episode is open, since when, and when it fails.
        Read once, at start, by the kinds' index. Whether its one notice went is
        asked of the store when it is due (`_pin_notice`), whatever this held."""
        episodes: dict[str, dict] = {}
        rows = self.store.query(
            "SELECT e.job_id,e.kind,e.data_json FROM events e JOIN jobs j ON j.job_id=e.job_id "
            "WHERE e.kind IN ('job.pin_unadmittable','job.pin_admittable','job.pin_noticed',"
            "'job.pin_notice_withdrawn') "
            "AND j.state IN ('queued','waiting') ORDER BY e.event_id")
        for row in rows:
            try:
                data = json.loads(row["data_json"] or "{}")
            except ValueError:
                data = {}
            data = data if isinstance(data, dict) else {}
            previous = episodes.get(row["job_id"], {})
            if row["kind"] == "job.pin_noticed":
                episodes[row["job_id"]] = {**previous, "noticed": True}
            elif row["kind"] == "job.pin_notice_withdrawn":
                episodes[row["job_id"]] = {**previous, "noticed": False}
            elif row["kind"] == "job.pin_unadmittable":
                episodes[row["job_id"]] = {**previous, "open": True, "since": data.get("since"),
                                           "fail_at": data.get("fail_at"), "lane_id": data.get("lane_id")}
            else:
                episodes[row["job_id"]] = {**previous, "open": False}
        return episodes

    def _pin_notice_jobs(self, rows: list[dict]) -> dict[int, str]:
        """C-11.8, C-15.2: service notice id -> the job a pin's notice is about
        (`store.pin_notice_jobs`), so `notice.pending` names the job and a session
        Subfleet launched surfaces it too (C-26.13), as a message (C-15.3)."""
        return pin_notice_jobs(self.store.query, rows)

    def _pin_view(self, desktop, *, refresh: bool = True) -> dict:
        """C-11.8: what `scheduler.pin_unadmittable` reads, as a view has it: every lane
        row marked and merged as a view marks and merges it (`capacity.mark_desktop`
        with whether Claude Code uses the desktop login, `Timers.merge_lane`), as
        C-6.3's check inside a reservation does, the closures open now, and the lanes
        whose credential latched. No reading and no attempt: a pin's standing
        refusals need neither. Whether Claude Code uses the desktop login is asked
        only when a lane is the desktop's, and without `refresh` (the turn pass) it
        is the last answer read (`_desktop_answer`, unknown reading as in use), so
        this never lists the registry for a pass that places only Codex turns."""
        now = utcnow()
        lanes = [capacity.mark_desktop(dict(row), desktop=desktop) for row in self.store.lane_rows()]
        if any(lane.get("desktop") for lane in lanes):
            in_use = self._desktop_in_use() if refresh else self._desktop_answer()
            lanes = [capacity.mark_desktop(lane, desktop=desktop, desktop_in_use=in_use) for lane in lanes]
        lanes = [self.timers.merge_lane(lane) for lane in lanes]
        return {"now": now, "lanes": lanes, "closures": self.store.list_closures(active_at=now),
                "unavailable_lanes": {lane["lane_id"]: "credential-latched" for lane in lanes
                                      if capacity.credential_latched(lane)}}

    def _stuck_pin(self, job: dict, stuck: dict, holds: dict[str, dict]) -> None:
        """C-11.8: hold a job whose pinned lane can never admit it, say so once, and
        fail it once `admission.pin_grace_s` has passed with the lane still so.

        An episode begins on the first pass that finds it: one `job.pin_unadmittable`
        event, and the job waiting on `capacity` with its failure as its next check.
        Once the episode has lasted `PIN_NOTICE_AFTER_S`, the job's one notice goes
        (`_pin_notice`). A conversation turn is held and nothing more: its message is
        the person's, and the conversation carries its end (C-26.12). A job with an
        attempt still live or quarantined is never failed here: it is held until that
        attempt settles. Every clock here is read once, from `now`."""
        job_id, now = job["job_id"], utcnow()
        turn = job["kind"] == "turn"
        episode = self._pin_episodes.get(job_id) or {}
        if not episode.get("open"):
            grace = admission_settings(self.policy)["pin_grace_s"]
            fail_at = None if turn or grace is None else _later(now, grace)
            record = {"lane_id": stuck["lane_id"], "reasons": stuck["reasons"], "closures": stuck["closures"],
                      "since": now, "fail_at": fail_at}
            with self.store.transaction("job.pin_unadmittable", job_id=job_id, lane_id=stuck["lane_id"],
                                        data=record) as tx:
                tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=? "
                           "WHERE job_id=? AND state IN ('queued','waiting')",
                           (fail_at or _later(now, CAPACITY_RECHECK_CEILING_S), job_id))
            episode = {**episode, "open": True, "since": now, "fail_at": fail_at, "lane_id": stuck["lane_id"]}
            self._pin_episodes[job_id] = episode
            self._capacity_waits.pop(job_id, None)          # C-6.10: not a wait for capacity now
            self.log.warning("job %s: pinned lane %s can never admit it (%s); %s", job_id, stuck["lane_id"],
                             ", ".join(stuck["reasons"]), render.pin_ends(fail_at))
            self._notify()
        fail_at = episode.get("fail_at")
        failing = bool(fail_at) and now >= fail_at
        if (not failing and not turn and not episode.get("noticed")
                and _seconds_between(episode.get("since") or now, now) >= PIN_NOTICE_AFTER_S):
            episode = self._pin_notice(job, stuck, episode, now)
        if failing and not self.store.one(
                "SELECT 1 FROM attempts WHERE job_id=? AND state IN "
                "('reserved','starting','running','finalizing','quarantined')", (job_id,)):
            self.log.warning("job %s failed: pinned lane %s could never admit it (%s)", job_id, stuck["lane_id"],
                             ", ".join(stuck["reasons"]))
            self._fail_queued(job, render.pin_failure(stuck, episode.get("since")), rc=int(Exit.NO_LANE),
                              kind="job.pin_refused", data={**stuck, "since": episode.get("since")})
            self._discard_fresh_worktree(job_id)
            self._pin_episodes.pop(job_id, None)
            return
        holds[job_id] = {"reason": "pin-unadmittable", "lane_id": stuck["lane_id"], "reasons": stuck["reasons"],
                         "closures": stuck["closures"], "since": episode.get("since"), "fail_at": fail_at,
                         **({"probe_status": stuck["probe_status"]} if stuck.get("probe_status") else {})}

    def _pin_notice(self, job: dict, stuck: dict, episode: dict, now: str) -> dict:
        """C-11.8: the job's one notice, to its caller's session (else the configured
        operator session, as `ping` addresses one), unless the store says one went
        already and was not withdrawn unread: a job that ran and came back, or
        outlived a restart, is not told twice. A `job.pin_noticed` event names the
        notice (id, session, creation time). With neither session there is no one to
        tell (C-15.8): the event is recorded with no notice, so the job is still
        told once, and `why` and `status.json` show its hold. The episode, as it is now."""
        job_id = job["job_id"]
        counts = self.store.one(
            "SELECT sum(kind='job.pin_noticed') AS noticed, sum(kind='job.pin_notice_withdrawn') AS withdrawn "
            "FROM events WHERE job_id=? AND kind IN ('job.pin_noticed','job.pin_notice_withdrawn')", (job_id,))
        if not (counts and (counts["noticed"] or 0) > (counts["withdrawn"] or 0)):
            session = job["caller_session"] or operator_session(self.policy)
            record = {"lane_id": stuck["lane_id"], "reasons": stuck["reasons"], "session_id": session,
                      "since": episode.get("since"), "fail_at": episode.get("fail_at"), "service_notice_id": None,
                      "created_at": now}
            if session is None:
                self.store.add_event("job.pin_noticed", job_id=job_id, lane_id=stuck["lane_id"], data=record)
            else:
                with self.store.transaction("job.pin_noticed", job_id=job_id, lane_id=stuck["lane_id"],
                                            data=record) as tx:
                    cursor = tx.execute("INSERT INTO service_notices(session_id,text,state,created_at) "
                                        "VALUES(?,?,'pending',?)",
                                        (session, render.pin_notice(job_id, stuck, episode.get("fail_at")), now))
                    record["service_notice_id"] = cursor.lastrowid
                self._notify()
        episode = {**episode, "noticed": True}
        self._pin_episodes[job_id] = episode
        return episode

    def _withdraw_pin_notice(self, tx, job_id: str, *, why: str) -> None:
        """C-11.8: inside the caller's transaction, withdraw the job's pin notice if
        nobody has been shown it yet (still `pending`): at the job's end, however it
        ended, and when its lane can admit it again, the notice is no longer true.
        Matched by id, session and creation time, so a row that reused its id is
        left alone. A `job.pin_notice_withdrawn` event records it, and a notice
        withdrawn unread does not count as the job's one (`_pin_notice`)."""
        keys = [key for (data,) in tx.execute("SELECT data_json FROM events WHERE job_id=? AND kind='job.pin_noticed'",
                                              (job_id,)) if (key := _pin_notice_key(data))]
        withdrawn = [key[0] for key in keys if tx.execute(
            "DELETE FROM service_notices WHERE notice_id=? AND session_id=? AND created_at=? AND state='pending'",
            key).rowcount]
        if withdrawn:
            tx.execute("INSERT INTO events(ts,kind,job_id,attempt_id,lane_id,data_json) VALUES (?,?,?,?,?,?)",
                       (utcnow(), "job.pin_notice_withdrawn", job_id, None, None,
                        json.dumps({"service_notice_ids": withdrawn, "why": why})))
            episode = self._pin_episodes.get(job_id)
            if episode:
                self._pin_episodes[job_id] = {**episode, "noticed": False}

    def _unstuck_pin(self, job: dict) -> dict:
        """C-11.8: the pinned lane could admit the job again (capacity allowing): its
        episode ends with a `job.pin_admittable` event and the job is due now, not at
        the failure its episode had set. The job, as the rest of the pass sees it."""
        episode = self._pin_episodes.get(job["job_id"])
        if not episode or not episode.get("open"):
            return job
        now = utcnow()
        with self.store.transaction("job.pin_admittable", job_id=job["job_id"], lane_id=episode.get("lane_id"),
                                    data={"lane_id": episode.get("lane_id"), "since": episode.get("since"),
                                          "ended_at": now}) as tx:
            tx.execute("UPDATE jobs SET next_check_at=CASE WHEN state='waiting' THEN ? ELSE next_check_at END "
                       "WHERE job_id=? AND state IN ('queued','waiting')", (now, job["job_id"]))
            self._withdraw_pin_notice(tx, job["job_id"], why="admittable")
        self._pin_episodes[job["job_id"]] = {**self._pin_episodes.get(job["job_id"], episode), "open": False}
        self.log.info("job %s: pinned lane %s can admit it again", job["job_id"], episode.get("lane_id"))
        return {**job, "next_check_at": now} if job["state"] == "waiting" else job

    def _route_hold(self, job_id: str, next_check_at: str | None) -> dict:
        """C-6.11, C-6.12: what a route wait reports, also on passes that do not look at it."""
        record = self._route_deferrals.get(job_id) or {}
        return {"reason": "route", **{key: record[key] for key in ("error_type", "error", "deferrals") if key in record},
                "next_check_at": next_check_at}

    def _unroutable(self, job: dict, exc: Unroutable, holds: dict[str, dict]) -> None:
        """C-6.12: settle one job whose route could not be evaluated; the pass goes on.

        A `RouteError` is the job's own (its pin names several lanes, a lane and
        a model of different providers, an authorization that is not whole): no
        wait fixes it, so the job fails with the message and exit 2, as submit
        would have refused it, unless the error is one a policy edit can cause
        and the job was accepted under another policy. Anything else (bad
        capacity data, a policy that no longer knows the job's task or model, a
        defect) is not the job's and may be fixed by a restart, so the job waits
        on `route`, never terminally, rechecked 5 s out doubling to 300 s, and
        holds back no other job.
        """
        cause, job_id = exc.cause, job["job_id"]
        record = {"error_type": type(cause).__name__, "error": str(cause)[:500]}
        self._capacity_waits.pop(job_id, None)
        # A provider conflict under a policy other than the one the job was
        # accepted with is the policy edit's, not the job's: it waits.
        if isinstance(cause, scheduler.RouteError) and not (
                cause.policy_dependent and job.get("policy_hash") != self.policy_digest):
            self._route_deferrals.pop(job_id, None)
            self.log.warning("job %s refused at admission: %s", job_id, cause)
            self._fail_queued(job, f"refused at admission: {cause}", rc=int(Exit.INVALID_INPUT),
                              kind="job.route_refused", data=record)
            self._discard_fresh_worktree(job_id)
            return
        count = (self._route_deferrals.get(job_id) or {}).get("deferrals", 0) + 1
        delay = min(ROUTE_RETRY_CEILING_S, ROUTE_RETRY_BASE_S * 2 ** min(count - 1, 16))
        next_check = after(delay)
        record.update(deferrals=count, next_check_at=next_check)
        # Replaced whole, never updated in place: `why` reads it from another thread.
        self._route_deferrals[job_id] = record
        with self.store.transaction("job.route_deferred", job_id=job_id, data=record) as tx:
            tx.execute("UPDATE jobs SET state='waiting',wait_reason='route',next_check_at=? "
                       "WHERE job_id=? AND state IN ('queued','waiting') AND cancel_requested_at IS NULL",
                       (next_check, job_id))
        # The type only, as C-5.10 logs a worker's: the message is in the event and in `why`.
        self.log.warning("job %s route could not be evaluated (%d in a row, next check in %d s): %s",
                         job_id, count, delay, record["error_type"])
        holds[job_id] = self._route_hold(job_id, next_check)
        self._notify()

    def _skip_revive(self, tx, job: dict, holder: str) -> None:
        """C-23.55: a session that already holds the revive lease is skipped.

        Terminal, not queued. A revive that waits for its twin to finish would
        launch a second continuation the moment the first ended, which is the
        2026-09-04 twin with a delay.

        `failed` with rc 7 rather than `cancelled`: nobody asked to cancel it,
        and `exit_for_job` reports a cancelled job as 130 whatever its rc, which
        would hide the refusal C-17.3 numbers 7. Submit refuses the ordinary
        case; this path is the submit/admission race, and it says the same thing.
        """
        tx.execute("UPDATE jobs SET state='failed',rc=7,wait_reason=NULL,"
                   "next_check_at=NULL,finished_at=? WHERE job_id=?",
                   (utcnow(), job["job_id"]))
        tx.execute("DELETE FROM leases WHERE holder=?", (job["job_id"],))
        earlier = self._earlier_attempt(tx, job)      # C-13.1: a retry of a revive
        self._notice(tx, job, ("failed while preparing the retry: " if earlier else "")
                     + f"skipped: session {job['caller_session']} already has a live "
                       f"revive ({holder}) holding {revive_lease_key(job['caller_session'])}" + earlier)

    @staticmethod
    def _workspace_error(exc: BaseException) -> tuple[bool, dict]:
        """(transient, record) for a workspace preparation failure (C-6.8).

        The text is git's verb, cap and stderr, or the `OSError`: a path and an
        errno, never a credential, so unlike a provider error it is recorded.
        """
        cause = exc.__cause__ if isinstance(exc, SalvageError) and exc.__cause__ else exc
        transient = (isinstance(exc, subprocess.TimeoutExpired) or transient_os_error(exc)
                     or (isinstance(exc, SalvageError) and exc.transient))
        if isinstance(exc, subprocess.TimeoutExpired):
            command = exc.cmd if isinstance(exc.cmd, (list, tuple)) else [str(exc.cmd)]
            verb = next((part for part in command[3:] if not str(part).startswith("-")), "git")
            message = f"git {verb} timed out after {exc.timeout:g} s"
        else:
            # Valid UTF-8, as C-13.1's: the record reaches an event and the job's notice,
            # which SQLite writes as strict UTF-8 (review of cda4c161, N1; a `SalvageError`
            # already is, and this holds for any other error's text).
            message = utf8_text(str(exc)) or type(exc).__name__
        return transient, {"error_type": type(cause).__name__, "error": message[:500]}

    def _workspace_failed(self, job: dict, exc: BaseException) -> None:
        """C-6.8: a transient failure waits with backoff; anything else, or a
        transient one past `caps.workspace_retry_max`, fails with its cause."""
        transient, record = self._workspace_error(exc)
        count = self._workspace_deferrals.get(job["job_id"], 0) + 1
        limit = self.policy["caps"]["workspace_retry_max"]
        detail = f"{record['error_type']}: {record['error']}"
        if transient and count <= limit:
            self._workspace_deferrals[job["job_id"]] = count
            delay = min(WORKSPACE_RETRY_CEILING_S, WORKSPACE_RETRY_BASE_S * 2 ** (count - 1))
            next_check = after(delay)
            record.update(deferrals=count, retry_max=limit, next_check_at=next_check)
            with self.store.transaction("job.workspace_deferred", job_id=job["job_id"], data=record) as tx:
                tx.execute("UPDATE jobs SET state='waiting',wait_reason='workspace',next_check_at=? "
                           "WHERE job_id=? AND state IN ('queued','waiting') AND cancel_requested_at IS NULL",
                           (next_check, job["job_id"]))
            self.log.warning("job %s workspace preparation deferred %d/%d, next check in %d s: %s",
                             job["job_id"], count, limit, delay, detail)
            self._notify()
            return
        self._workspace_deferrals.pop(job["job_id"], None)
        record.update(transient=transient, deferrals=count - 1)
        self.log.error("job %s workspace preparation failed: %s", job["job_id"], detail)
        tail = f" after {count - 1} retries" if transient else ""
        self._fail_queued(job, f"workspace preparation failed{tail}: {detail}",
                          kind="job.workspace_failed", data=record)

    def _fail_queued(self, job: dict, detail: str, *, rc: int = 1, kind: str = "job.failed",
                     data: dict | None = None) -> None:
        with self.store.transaction(kind, job_id=job["job_id"], data=data) as tx:
            job = self._job(job["job_id"])
            if job["state"] in TERMINAL:
                return
            state, rc = ("cancelled", 130) if job["cancel_requested_at"] else ("failed", rc)
            tx.execute("UPDATE jobs SET state=?,rc=?,finished_at=?,wait_reason=NULL,next_check_at=NULL WHERE job_id=?",
                       (state, rc, utcnow(), job["job_id"]))
            tx.execute("DELETE FROM leases WHERE holder=?", (job["job_id"],))
            # C-13.1, C-15.1: a job with an attempt behind it was preparing its retry.
            earlier = self._earlier_attempt(tx, job)
            self._notice(tx, job, (f"{state} while preparing the retry: " if earlier else "") + detail + earlier)
        self._notify()

    def _launch(self, a: dict) -> None:
        job = self._job(a["job_id"])
        self._pending_launches.discard(a["attempt_id"])
        if job["cancel_requested_at"]:
            self._unlaunched(a, "cancelled-before-launch")
            return
        adir = attempt_dir(self.root, a["job_id"], a["seq"])
        adir.mkdir(mode=0o700, exist_ok=True)
        lane = self.store.get_lane(a["lane_id"])
        adapter = get_adapter(lane.provider)
        evidence = json.loads(a["evidence_json"] or "{}")
        model = self.policy["models"][evidence["model_short"]]
        prompt_path = Path(job["prompt_path"])
        prepared_path = prompt_path.with_name("prompt.prepared.md")
        if prepared_path.is_file():
            prompt_path = prepared_path
        if a["seq"] > 1 and job["sandbox"] == "workspace-write":
            refs = self.store.query("SELECT path FROM artifacts JOIN attempts USING(attempt_id) WHERE job_id=? AND role='salvage' ORDER BY seq", (job["job_id"],))
            suffix = f"\n\nContinue from checkpoint {evidence.get('baseline_commit')}. Preserved snapshots: {', '.join(r['path'] for r in refs) or 'none'}.\n"
            prompt = read_regular(prompt_path) + suffix.encode()
            prompt_path = adir / "prompt.md"
            self._publish("prompt", prompt_path, prompt)
        turn_block = ((self._read_json(self.root / "jobs" / job["job_id"] / "manifest.json") or {}).get("turn")
                      if job["kind"] == "turn" else None)
        try:
            if job["sandbox"] == "workspace-write" and not (turn_block and turn_block.get("allow_main")):
                # Close the reservation-to-launch window as well: the caller
                # can switch an in-place checkout after its attempt is reserved.
                validate_writable_workdir(job.get("worktree") or job["workdir"], timeout_s=self.policy["caps"]["workspace_git_timeout_s"])
            self._validate_home(lane)
            credential_env = resolve_credential(lane.credential)
            spec = self._spec(job, workdir=self._launch_dir(job, lane.provider), prompt_path=str(prompt_path))
            guard_override = None if spec.isolated_review or job["kind"] == "turn" else self._guard_override(
                adapter, lane, spec.workdir, self._guard_recorder(lane, spec.workdir, adir), self.root)
            resume = None
            if turn_block is not None:
                guard = None
                if lane.provider == "codex":
                    guard = self._guard_override(adapter, lane, spec.workdir, self._guard_recorder(lane, spec.workdir, adir),
                                                 self.root, full=True)
                launch = self.conversations.launch(job, a, lane, credential_env, adir, model["id"], guard)
            elif job["kind"] == "resume":
                manifest = self._read_json(self.root / "jobs" / job["job_id"] / "manifest.json") or {}
                resume = manifest.get("resume")
                if not resume or not self._resume_lane(resume["lane_id"], lane) or resume["model_id"] != model["id"]:
                    raise AdapterError("resume source identity is missing or does not match this attempt",
                                       fix="resubmit the resume from the original job")
            if turn_block is not None:
                pass
            elif resume or (job["kind"] == "revive" and job["caller_session"]):
                # C-23.54: a revive is an ordinary submission, but the launch it
                # asks for is `--resume <session id>` — continuing the session
                # named by `caller_session`, which for a revive IS the session
                # being revived. `build_launch` would start a NEW conversation
                # under a fresh `--session-id`, which looks like a revive and is
                # not one.
                launch = adapter.resume_launch(spec, a["attempt_id"], adir, lane,
                                               credential_env, resume["native_session_id"] if resume else job["caller_session"],
                                               prompt_path, guard_override, model["id"])
                if launch is None:
                    raise AdapterError(
                        f"{lane.provider} cannot resume a session in place", code=7,
                        fix="use `subfleet sessions handoff` to continue this session")
            else:
                launch = adapter.build_launch(spec, a["attempt_id"], adir, lane, credential_env,
                                              model["id"], model.get("effort"), prompt_path, guard_override)
        except AdapterError as exc:
            self._launch_failure(a, str(exc) + (f"; fix: {exc.fix}" if exc.fix else ""), rc=exc.code)
            return
        except SalvageError as exc:
            # C-6.8: the main/master re-check did not finish. Launching anyway
            # would skip it, so nothing is launched. A transient failure (a
            # timeout, EMFILE or ENOMEM, a git killed by a signal) says nothing
            # about the checkout, so the attempt ends as one whose guardian could
            # not be started does, and the job is queued again while it has
            # attempts left: its next admission asks again, after C-6.8's backoff
            # (`_defer_after_launch`).
            # As a spawn error it was classified `unknown`, which is never
            # retried, and ended the job (review of ceacf18b, P3-5).
            self.log.error("attempt %s workdir branch check failed: %s", a["attempt_id"], exc)
            if exc.transient:
                self._unlaunched(a, f"{LAUNCH_CHECK_UNFINISHED} {exc}", deferred=exc)
            else:
                self._launch_failure(a, f"workdir branch check failed: {exc}", rc=int(Exit.OPERATIONAL))
            return
        self._launches[a["attempt_id"]] = launch
        safe_launch = dataclasses.asdict(launch)
        safe_launch.pop("env_add")
        self._publish("launch", adir / "launch.json", json_bytes(safe_launch))
        self._publish("lane-log", adir / "lane.log", b"")
        env = dict(os.environ)
        env.update(launch.env_add)
        for key in (*launch.env_remove, "CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            env.pop(key, None)
        env.update(SUBFLEET_JOB=a["job_id"], SUBFLEET_ATTEMPT=a["attempt_id"], SUBFLEET_ROOT=str(self.root))  # C-5.1 markers
        # The package path is explicit: provider cwd is deliberately unrelated
        # to the daemon's installation or test checkout.
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        read_fd, write_fd = procs.pipe_above_stdio()
        command = [sys.executable, "-m", "subfleet.guardian", "--attempt-dir", str(adir),
                   "--cwd", launch.cwd, "--stdout-path", launch.stdout_path,
                   "--stderr-path", launch.stderr_path, "--launch-fd", str(read_fd)]
        if launch.stdin_path:
            command += ["--stdin-path", launch.stdin_path]
        if turn_block is not None:
            from .relay import socket_path
            command += ["--control-socket", str(socket_path(self.root, a["attempt_id"])),
                        "--relay-peer-lock", str(self.root / "daemon.lock")]
        if self.guardian_start_delay_s:
            command += ["--start-delay-s", str(self.guardian_start_delay_s)]
        command += ["--", *launch.argv]
        try:
            child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     pass_fds=(read_fd,), close_fds=True, cwd=package_root)
            self._children[a["attempt_id"]] = child
            # ps can miss a pid for a few milliseconds after fork, and it is
            # slow under load; retry briefly while the guardian is alive. The
            # gate byte is not written until identity is recorded (C-5.2, C-5.3).
            started = procs.proc_start_retry(child.pid, tries=8, delay_s=0.25,
                                             alive=lambda: child.poll() is None)
            if not started:
                rc = child.poll()
                raise procs.InspectionError(
                    f"guardian exited before identity (rc={rc})" if rc is not None
                    else "guardian identity is absent after 2 s")
            boot = procs.boot_id()
            with self.store.transaction("attempt.starting", job_id=a["job_id"], attempt_id=a["attempt_id"]) as tx:
                if job["kind"] == "turn" and tx.execute("SELECT cancel_requested_at FROM jobs WHERE job_id=?",
                                                        (a["job_id"],)).fetchone()[0]:
                    # Review IR-2: a message withdrawn during launch never starts.
                    raise procs.InspectionError("cancelled during launch")
                tx.execute("UPDATE attempts SET state='starting',guardian_pid=?,pgid=?,boot_id=?,proc_start=?,native_session_id=? WHERE attempt_id=? AND state='reserved'",
                           (child.pid, child.pid, boot, started, launch.native_session_id, a["attempt_id"]))
            os.write(write_fd, b"1")
        except (OSError, procs.InspectionError) as exc:
            # Closing the gate guarantees an unrecorded guardian cannot launch.
            os.close(write_fd)
            write_fd = -1
            self._unlaunched(a, f"guardian-identity-unavailable: {type(exc).__name__}: {exc}")
            return
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
        now = time.monotonic()
        self._starting_deadlines[a["attempt_id"]] = now + self.start_grace_s
        # C-5.12: first inspected with a table read after its guardian started,
        # which shows the guardian. Due as it first asked, it was usually given
        # a table another attempt had read before the guardian existed, and paid
        # for a fresh `liveness` and a table of its own to record its group.
        self._inspect_next[a["attempt_id"]] = now + self.inspect_interval_s
        self._boundary("starting", a["job_id"], a["attempt_id"])

    def _launch_failure(self, a: dict, detail: str, *, rc: int = 127) -> None:
        adir = attempt_dir(self.root, a["job_id"], a["seq"])
        adir.mkdir(mode=0o700, exist_ok=True)
        self._publish("exit", adir / "exit.json", json_bytes({"rc": rc, "signal": None, "wall_s": 0, "child_pid": None,
                      "finished_at": utcnow(), "spawn_error": detail}))
        self._begin_finalizing(a, self._read_json(adir / "exit.json"))

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        """An attempt's or a job's JSON record, or None when there is none. Only a
        regular file is read: a FIFO put where one belongs (a running agent can name
        its attempt's directory) had held `_process_attempt`, and the workers pool
        Daemon.close() waits for, in open(); it is no record."""
        try:
            return json.loads(read_regular(path))
        except (FileNotFoundError, NotRegularFile):
            return None

    def _process_attempt(self, aid: str) -> object:
        """One pass over a live attempt; `DEFERRED` when it is the retry of an
        inspection that raised and could not inspect this time (C-5.11), else None."""
        a = self.store.get_attempt(aid)
        if not a or a["state"] not in LIVE:
            self._inspect_next.pop(aid, None)
            self._inspect_retry.discard(aid)
            return None
        if imported_external(a):
            return None                     # v1 still owns it (principle 3): the loop's check, again
        child = self._children.get(aid)
        if child and child.poll() is not None:
            self._children.pop(aid, None)
        if a["state"] == "reserved":
            if aid in self._pending_launches:
                self._launch(a)
            else:
                self._unlaunched(a, "reserved-no-launch")
            return
        job = self._job(a["job_id"])
        adir = attempt_dir(self.root, a["job_id"], a["seq"])
        if a["state"] == "finalizing":
            self._finalize(a)
            return
        start = self._read_json(adir / "start.json")
        receipt = self._read_json(adir / "exit.json")
        if start and a["state"] == "starting":
            with self.store.transaction("attempt.running", job_id=a["job_id"], attempt_id=aid) as tx:
                tx.execute("UPDATE attempts SET state='running',guardian_pid=?,pgid=?,boot_id=?,proc_start=?,started_at=? WHERE attempt_id=? AND state='starting'",
                           (start["guardian_pid"], start["pgid"], start["boot_id"], start["proc_start"], start["started_at"], aid))
            self._starting_deadlines.pop(aid, None)
            self._boundary("running", a["job_id"], aid)
            a = self.store.get_attempt(aid)
        if job["kind"] != "turn" and a["state"] in ("starting", "running"):
            self._read_answer(a, adir)          # C-6.14: files only, paced; never raises
        if receipt:
            self._begin_finalizing(a, receipt)
            return
        if job["cancel_requested_at"] or age(job["started_at"]) >= job["max_wall_s"]:
            if job["kind"] == "turn" and self.conversations.stop(
                    a, job, "wall-limit" if not job["cancel_requested_at"] else "operator-kill"):
                return      # D-13, IR-4: the provider is stopped first; containment follows
            if not job["cancel_requested_at"]:
                with self.store.transaction("job.wall_limit", job_id=job["job_id"]) as tx:
                    tx.execute("UPDATE jobs SET cancel_requested_at=? WHERE job_id=? AND cancel_requested_at IS NULL", (utcnow(), job["job_id"]))
                    tx.execute("UPDATE attempts SET killed_by='max_wall_s' WHERE attempt_id=?", (aid,))
                a = self.store.get_attempt(aid)
            self._kill_attempt(a)
            return
        if a["state"] == "starting":
            deadline = self._starting_deadlines.setdefault(aid, time.monotonic() + self.start_grace_s)
            if time.monotonic() < deadline:
                return
            census = self._contain(a)
            # The guardian writes start.json, runs the provider, writes exit.json
            # and exits, so the receipts can land while the census runs, and a
            # census that then finds nothing is an attempt that finished. Read
            # before them, it released a provider that had exited 0 to run again
            # (C-4.2). The next tick takes them from the top, and takes only a
            # receipt that reads as a value (as above): one that merely exists,
            # holding `{}` or `null`, left the attempt here, taking a census on
            # every tick.
            if self._read_json(adir / "start.json") or self._read_json(adir / "exit.json"):
                return
            if census.verified_empty:
                self._unlaunched(a, "starting-no-receipt")
            else:
                self._quarantine(a, census, "start grace expired without a receipt")
            return
        # C-5.12: everything above is files and rows, read on every pass the
        # control loop offers (C-5.11 says which: every tick it has work). What
        # follows asks the operating system, so a healthy attempt is inspected
        # once per interval, from one process table shared by every attempt.
        # It falls due again when the table it was given expires, which is when
        # a new one may be read: timed from before the read, the next inspection
        # fell just short of that and took the same table again.
        now = time.monotonic()
        due = self._inspect_next.get(aid, now)
        # C-5.11: until an inspection that raised has been repeated to its end,
        # a pass that does not inspect is no recovery, so C-5.10's count stands.
        retry = aid in self._inspect_retry
        if now < due:
            return DEFERRED if retry else None
        try:
            inspected = self._inspect_running(a, adir, due)
        except BaseException:
            # A pass that raised must be retried in full, not skipped at the
            # gate: a skipped pass returns normally, and C-5.10 would count it
            # as recovery and start its backoff over.
            self._inspect_next.pop(aid, None)
            self._inspect_retry.add(aid)
            raise
        if inspected:
            self._inspect_retry.discard(aid)
            return None
        return DEFERRED if retry else None

    def _inspect_running(self, a: dict, adir: Path, due: float) -> bool:
        """The paced half of `_process_attempt` (C-5.12): is the guardian still ours?

        False when it could not say this time (another attempt's read is running,
        or the table, its boot identity or the guardian cannot be read), which a
        retry after a raise must not count as recovery (C-5.11)."""
        aid = a["attempt_id"]
        shared = self._process_table(due)
        if shared is None:
            # Another attempt's `ps` is running: ask again next tick, still due
            # from when it fell due (a recovered attempt, from now), so that it
            # may be given that read.
            self._inspect_next.setdefault(aid, due)
            return False
        table, self._inspect_next[aid] = shared
        if table is None:
            # This interval's read failed. Asking about the guardian singly would
            # cost a capped read per running attempt, which is the outage cost
            # the shared read exists to ration, and a guardian that cannot be
            # inspected decides nothing anyway (C-4.2, C-5.5).
            self.log.debug("process table unreadable; %s not inspected this interval", aid)
            return False
        try:
            # C-5.3's legacy match too: a guardian recorded with `kern.boottime`
            # seconds (its UUID `sysctl` failed once at start) is shown alive when
            # the table's one read of those seconds matches, as `liveness` would
            # say, and is recorded from this table, not asked about singly.
            shown = table.is_process(a["guardian_pid"], a["boot_id"], a["proc_start"], legacy=True)
        except procs.InspectionError:
            # The table's boot identity (or the seconds a legacy record needs)
            # could not be read, once for every attempt that asks; asked singly,
            # the guardian would need the same read.
            self.log.debug("boot identity unreadable; %s not inspected this interval", aid)
            return False
        if shown:
            self._record_owned(a, table)
            return True  # Re-adopted solely by receipt identity, not parentage.
        # A shared table can say "alive" and nothing else: a guardian it does
        # not show is asked about afresh before anything is decided from it.
        alive = procs.liveness(a["guardian_pid"], a["boot_id"], a["proc_start"])
        if alive == "alive":
            try:
                self._record_owned(a, procs.snapshot())
            except procs.InspectionError:
                return False
            return True
        if alive == "unknown":
            # ps failed or timed out (load, or an inspection outage). A guardian
            # that cannot be inspected is neither dead nor an escape; nothing is
            # decided from it this tick (C-4.2, C-5.5).
            self.log.debug("guardian liveness of %s unknown this tick", aid)
            return False
        # The guardian writes exit.json and then exits, so a receipt can appear
        # between the read above and the liveness check: a dead guardian with a
        # receipt is the normal end of an attempt, not a loss (C-4.2).
        receipt = self._read_json(adir / "exit.json")
        if receipt:
            self._begin_finalizing(a, receipt)
            return True
        census = self._contain(a)
        if not census.verified_empty:
            self._kill_attempt(a, lost=True)
        else:
            self._lost(a)
        return True

    def _contain(self, a: dict):
        return procs.containment(a.get("pgid"), a.get("guardian_pid"), a.get("child_pid"), a["attempt_id"], root=str(self.root))

    @staticmethod
    def _new_group_identities(pgid: int | None, recorded: dict) -> dict[str, dict]:
        """C-5.11: identities for the group members `recorded` lacks.

        One group snapshot names the members and their start times. A member
        recorded under the same start and the current boot identity is not
        asked again. A new pid, a pid a new process now holds, and a member
        recorded under another boot identity (legacy `kern.boottime` seconds
        from before the boot UUID could be read, or seconds a clock correction
        has since moved) are captured by `procs.identity` (C-5.3), as the full
        census refreshed them. The caller re-checks the leader before recording
        anything (C-5.4).
        """
        try:
            members = procs.group_members(pgid or 0)
            booted = procs.boot_id()
        except procs.InspectionError:
            return {}
        fresh = {}
        for pid, started in sorted(members.items()):
            known = recorded.get(str(pid))
            if known and known.get("proc_start") == started and known.get("boot_id") == booted:
                continue
            try:
                ident = procs.identity(pid)
            except procs.InspectionError:
                continue
            if ident is not None:
                fresh[str(pid)] = dataclasses.asdict(ident)
        return fresh

    def _process_table(self, due: float) -> tuple[procs.ProcessTable | None, float] | None:
        """C-5.12: a table for an inspection that fell due at `due`, and when that
        table expires (the table is None if its read failed); None when another
        inspection's read is running and no table is new enough, in which case
        the caller asks again on its next tick.

        An inspection is given the last table if it expires after the inspection
        fell due: for an attempt already inspected, one read after the table it
        was last given; for one this daemon launched, one read after its guardian
        started (it falls due an interval after that); and for one it recovered,
        one read less than an interval before it first asked. Otherwise it reads a table, and that is the only time a
        read begins, so reads begin at least `inspect_interval_s` apart however
        many attempts ask and however long `ps` takes. A read is `ps` and then the
        table's boot identity (`sysctl`, unless the module remembers the UUID).
        Only the inspection that reads waits for them: one that finds a read
        running returns at once, so a slow or hung `ps` or `sysctl` holds one
        worker of the pool, not one per running attempt, and every other
        attempt's receipts, cancel and clock are still read each tick. A read
        that failed is rationed like one that worked.
        """
        if due < self._table[1]:
            return self._table
        if not self._table_lock.acquire(blocking=False):
            return None
        try:
            if due < self._table[1]:                 # another read ended while this one asked
                return self._table
            began = time.monotonic()
            try:
                table = procs.snapshot()
            except procs.InspectionError:
                table = None
            try:
                if table is not None:
                    # The reader waits for `sysctl` too, before any attempt is
                    # given the table: an attempt that read it lazily held every
                    # other attempt given the table on its lock for as long.
                    table.boot()
            except procs.InspectionError:
                pass                             # kept by the table: each attempt sees it
            finally:
                # Published however the boot read ends, so that anything else it
                # raises costs this reader's pass, never the interval's ration.
                self._table = (table, began + self.inspect_interval_s)
            return self._table
        finally:
            self._table_lock.release()

    def _record_owned(self, a: dict, table: procs.ProcessTable) -> None:
        """C-5.6: remember the group's members while the recorded guardian leads it.

        Both facts come from the one table, so the leader is known to be ours at
        the instant its members were listed. Only group members are recorded,
        which is why the marker scan of a full census is not run here. The
        leader is ours by C-5.3's rule, the one `same_process` applies before
        the kill protocol records members, so a legacy boot timestamp that
        matches counts.
        """
        guardian = a["guardian_pid"]
        if (not table.is_process(guardian, a["boot_id"], a["proc_start"], legacy=True)
                or table.rows[guardian][1] != a["pgid"]):
            return
        members = {pid: table.identity(pid) for pid in table.group(a["pgid"])}
        evidence = json.loads(a["evidence_json"] or "{}")
        before = dict(evidence.get("owned_identities", {}))
        owned = dict(before)
        owned.update({str(pid): dataclasses.asdict(ident) for pid, ident in members.items() if ident})
        if owned != before:
            evidence["owned_identities"] = owned
            with self.store.transaction("attempt.processes_recorded", job_id=a["job_id"], attempt_id=a["attempt_id"]) as tx:
                tx.execute("UPDATE attempts SET evidence_json=? WHERE attempt_id=?", (json.dumps(evidence), a["attempt_id"]))

    def _unlaunched(self, a: dict, detail: str, *, deferred: SalvageError | None = None) -> None:
        """An attempt that ended before its provider could run: `failed`, or
        `interrupted` when the job was cancelled, its leases released, and the job
        queued again while it has attempts left.

        `deferred` is the transient failure of the launch-time main/master re-check
        (C-6.8). A job queued again after one waits under C-6.8's backoff before its
        next admission asks git again, 5 s doubling per consecutive attempt that
        ended so to the 300 s ceiling, and the wait is a `job.workspace_deferred`
        event, as admission's are. Queued at once, a launch-time check that kept
        getting no answer while admission's got one used up the job's attempts in
        as many passes (review of the P3-5 fix).
        """
        with self.store.transaction("attempt.no_launch", job_id=a["job_id"], attempt_id=a["attempt_id"], data={"detail": detail}) as tx:
            job = self._job(a["job_id"])
            cancel = bool(job["cancel_requested_at"])
            tx.execute("UPDATE attempts SET state=?,outcome_class='unknown',outcome_detail=?,finished_at=? WHERE attempt_id=?",
                       ("interrupted" if cancel else "failed", detail, utcnow(), a["attempt_id"]))
            tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (a["attempt_id"], job["job_id"]))
            retry = not cancel and self._attempts_left(tx, job, a)
            state = "queued" if retry else "cancelled" if cancel else "failed"
            rc = None if retry else 130 if cancel else 1
            tx.execute("UPDATE jobs SET state=?,rc=?,finished_at=?,wait_reason=NULL,next_check_at=NULL WHERE job_id=?", (state, rc, None if retry else utcnow(), job["job_id"]))
            if retry and deferred is not None:
                self._defer_after_launch(tx, job, deferred)
            if not retry:
                # C-13.1, C-15.1: a retry that never launched names the attempt before it.
                earlier = self._earlier_attempt(tx, job, before=a["seq"])
                self._notice(tx, job, (f"attempt a{a['seq']}: " if earlier else "") + detail + earlier)
        self._notify()

    def _defer_after_launch(self, tx, job: dict, exc: SalvageError) -> None:
        """C-6.8: `_unlaunched`'s wait after a launch-time main/master re-check that git
        gave no answer to. The count is of the job's latest attempts that ended so, in a
        row, read from the store, so the admission pass that succeeds between two of them
        (and resets admission's own count) does not reset it."""
        count = 0
        for (detail,) in tx.execute("SELECT outcome_detail FROM attempts WHERE job_id=? ORDER BY seq DESC",
                                    (job["job_id"],)).fetchall():
            if not (detail or "").startswith(LAUNCH_CHECK_UNFINISHED):
                break
            count += 1
        delay = min(WORKSPACE_RETRY_CEILING_S, WORKSPACE_RETRY_BASE_S * 2 ** (max(count, 1) - 1))
        next_check = after(delay)
        _, record = self._workspace_error(exc)
        record.update(stage="launch", deferrals=count, next_check_at=next_check)
        with self.store.transaction("job.workspace_deferred", job_id=job["job_id"], data=record) as inner:
            inner.execute("UPDATE jobs SET state='waiting',wait_reason='workspace',next_check_at=? "
                          "WHERE job_id=? AND state='queued'", (next_check, job["job_id"]))
        self.log.warning("job %s launch-time branch check got no answer (%d in a row), next check in %d s: %s: %s",
                         job["job_id"], count, delay, record["error_type"], record["error"])

    def _begin_finalizing(self, a: dict, receipt: dict) -> None:
        with self.store.transaction("attempt.finalizing", job_id=a["job_id"], attempt_id=a["attempt_id"]) as tx:
            tx.execute("UPDATE attempts SET state='finalizing',rc=?,signal=?,child_pid=?,finished_at=? WHERE attempt_id=? AND state IN ('reserved','starting','running')",
                       (receipt.get("rc"), receipt.get("signal"), receipt.get("child_pid"), receipt.get("finished_at", utcnow()), a["attempt_id"]))
        self._boundary("finalizing", a["job_id"], a["attempt_id"])
        self._notify()

    def _kill_attempt(self, a: dict, *, lost: bool = False) -> None:
        census = self._contain(a)
        evidence = json.loads(a["evidence_json"] or "{}")
        # Only group members observed while the recorded leader is still ours
        # may become additional signal targets. Escaped/new marker pids remain
        # evidence for quarantine, never authority inferred from a PID alone.
        owned = {int(pid): procs.ProcessIdentity(**value) for pid, value in evidence.get("owned_identities", {}).items()}
        leader_live = a["guardian_pid"] and procs.same_process(a["guardian_pid"], a["boot_id"], a["proc_start"])
        if leader_live:
            owned.update({pid: ident for pid, ident in census.identities.items() if pid in census.group_pids})
        evidence["owned_identities"] = {str(pid): dataclasses.asdict(ident) for pid, ident in owned.items()}
        with self.store.transaction("attempt.kill_started", job_id=a["job_id"], attempt_id=a["attempt_id"]) as tx:
            tx.execute("UPDATE attempts SET killed_by=COALESCE(killed_by,?),evidence_json=? WHERE attempt_id=?", ("recovery" if lost else "operator", json.dumps(evidence), a["attempt_id"]))
        if a.get("pgid"):
            procs.signal_group(a["pgid"], signal.SIGTERM, boot_id=a["boot_id"], proc_start=a["proc_start"])
        deadline = time.monotonic() + self.term_grace_s
        while not census.verified_empty and time.monotonic() < deadline:
            if self.stopping.wait(min(.1, max(0, deadline - time.monotonic()))):
                return
            census = self._contain(a)
        escalated = not census.verified_empty
        if escalated and a.get("pgid"):
            procs.signal_group(a["pgid"], signal.SIGKILL, boot_id=a["boot_id"], proc_start=a["proc_start"])
        census = self._contain(a)
        for pid in census.live_pids:
            if pid in owned:
                procs.signal_process(owned[pid], signal.SIGKILL)
        # Signalled processes leave the process table only when the kernel has
        # finished tearing them down, and under load that takes longer than one
        # read. Re-enumerate for a bounded settle window (C-5.6, kill_settle_s).
        # The loop ends early only on a verified-empty census; the last census,
        # never a guess about a pid, decides. No SQLite transaction is open.
        settle_until = time.monotonic() + self.kill_settle_s
        while True:
            if self.stopping.wait(.05):
                return
            census = self._contain(a)
            if census.verified_empty or time.monotonic() >= settle_until:
                break
        if not census.verified_empty:
            self._quarantine(a, census, "termination could not verify containment")
            return
        adir = attempt_dir(self.root, a["job_id"], a["seq"])
        receipt = self._read_json(adir / "exit.json")
        if lost and receipt is None:
            self._lost(a)
            return
        if receipt is None:
            sig = signal.SIGKILL if escalated else signal.SIGTERM
            receipt = {"rc": -int(sig), "signal": int(sig), "child_pid": a["child_pid"],
                       "wall_s": age(a["started_at"]), "finished_at": utcnow(), "killed_by": a.get("killed_by") or "operator"}
            self._publish("exit", adir / "exit.json", json_bytes(receipt))
        self._begin_finalizing(a, receipt)

    def _quarantine(self, a: dict, census, reason: str) -> None:
        detail = json.dumps({"reason": reason, **census.to_dict()}, sort_keys=True)
        with self.store.transaction("attempt.quarantined", job_id=a["job_id"], attempt_id=a["attempt_id"], data={"containment": census.to_dict()}) as tx:
            job = self._job(a["job_id"])
            tx.execute("UPDATE attempts SET state='quarantined',quarantine_reason=?,finished_at=? WHERE attempt_id=?", (detail, utcnow(), a["attempt_id"]))
            tx.execute("DELETE FROM leases WHERE holder=? AND lease_key LIKE 'lane:%'", (a["attempt_id"],))
            state, rc = ("cancelled", 130) if job["cancel_requested_at"] else ("lost", 125)
            tx.execute("UPDATE jobs SET state=?,rc=?,finished_at=? WHERE job_id=?", (state, rc, utcnow(), a["job_id"]))
            # C-13.1, C-15.1: a quarantined retry names the attempt before it.
            earlier = self._earlier_attempt(tx, job, before=a["seq"])
            self._notice(tx, job, (f"attempt a{a['seq']} " if earlier else "") + "quarantined: " + detail + earlier)
        self._notify()

    def _resolve_quarantine(self, a: dict, args: protocol.KillArgs) -> None:
        census = self._contain(a)
        if not args.force_release and not census.verified_empty:
            with self.store.transaction("quarantine.still_live", job_id=a["job_id"], attempt_id=a["attempt_id"], data=census.to_dict()) as tx:
                tx.execute("UPDATE attempts SET quarantine_reason=? WHERE attempt_id=?", (json.dumps(census.to_dict()), a["attempt_id"]))
            return
        artifacts, salvage_evidence = [], {}
        job = self._job(a["job_id"])
        if job["kind"] == "turn":
            # C-26.10: a turn writes no salvage ref; its end snapshot is taken only
            # when nothing can still be writing (C-26.14). An operator's one-shot
            # request is never retried, so a git failure is recorded, not raised.
            self._turn_trees(job, a, retry=False, error=None if census.verified_empty else
                             "released from quarantine with writers still live; no end snapshot")
        elif census.verified_empty:
            artifacts, _, salvage_evidence = self._salvage(job, a, retry=False)
        with self.store.transaction("quarantine.force_release" if args.force_release else "quarantine.confirmed_dead", job_id=a["job_id"], attempt_id=a["attempt_id"], data={"operator_note": args.operator_note, "containment": census.to_dict(), "override": args.force_release, **salvage_evidence}) as tx:
            for artifact in artifacts:
                self.store.add_artifact(a["attempt_id"], **artifact)
            tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (a["job_id"], a["attempt_id"]))
            actual = self.store.get_attempt(a["attempt_id"])
            if salvage_evidence and actual["state"] == "quarantined":
                # C-13.1: `kill --confirm-dead` answered before this ran, and the
                # job's notice went out when it was quarantined, so what salvage
                # could not save is told in the evidence and in one more notice.
                evidence = {**json.loads(actual["evidence_json"] or "{}"), **salvage_evidence}
                tx.execute("UPDATE attempts SET evidence_json=? WHERE attempt_id=?", (json.dumps(evidence), a["attempt_id"]))
                self._notice(tx, job, f"released from quarantine (attempt a{a['seq']})"
                             + self._salvage_summary(job, artifacts, salvage_evidence), again=True)
            tx.execute("UPDATE attempts SET state=? WHERE attempt_id=?", ("interrupted" if self._job(a["job_id"])["cancel_requested_at"] else "lost", a["attempt_id"]))
        self._notify()

    def _lost(self, a: dict) -> None:
        self._finalize(a, lost=True)

    def _relaunch_env(self, lane_id: str) -> dict[str, str]:
        """C-10.5, C-10.6: the credential a launch rebuilt after a restart needs.

        `launch.json` never holds a secret, so a launch read back from disk has
        none — and the identity check must ask the profile endpoint with exactly
        the credential that produced the reading. It is resolved again from the
        lane's own reference, kept in memory, and written nowhere. A credential
        that cannot be resolved leaves the check unverified, which is the honest
        answer: nobody can say whose that reading was.
        """
        env = {"SUBFLEET_LANE": lane_id}
        lane = self.store.get_lane(lane_id)
        if lane is None:
            return env
        try:
            env.update(resolve_credential(lane.credential))
        except (AdapterError, OSError, subprocess.SubprocessError) as exc:
            self.log.debug("credential unavailable for %s: %s", lane_id, type(exc).__name__)
        return env

    def _saved_launch(self, a: dict) -> Launch:
        if a["attempt_id"] in self._launches:
            return self._launches[a["attempt_id"]]
        adir = attempt_dir(self.root, a["job_id"], a["seq"])
        value = self._read_json(adir / "launch.json")
        if value:
            value.update(env_add=self._relaunch_env(a["lane_id"]), argv=tuple(value["argv"]), env_remove=tuple(value["env_remove"]))
            return Launch(**value)
        job = self._job(a["job_id"])
        return Launch((), {}, (), job.get("worktree") or job["workdir"], job["prompt_path"], str(adir / "stdout"), str(adir / "stderr"), None, None)

    def _salvage(self, job: dict, a: dict, *, retry: bool = True) -> tuple[list[dict], str | None, dict]:
        """C-13.1: a writable job's salvage at finalization; the artifacts, the
        checkpoint (HEAD after) and what the attempt's evidence records of it:
        `salvage_error` when salvage failed, and `salvage_skipped` (how many, and
        the first `SALVAGE_SKIPPED_SHOWN`) when the snapshot left out nested
        repositories with no commit, which then exist only in the worktree.

        The receipt `salvage.json` makes a replayed finalization take nothing
        twice. A transient git failure raises, so the worker tries again with
        its backoff, until `SALVAGE_TRIES` tries in all have failed (the count is
        in memory, so a restart starts it again); that failure, or any other, is
        recorded and finalization goes on without a salvage ref, which ends the
        attempt and frees its lane. Retention then keeps the dirty worktree
        (C-13.4). A quarantine's release passes `retry=False`: an operator's
        one-shot request is never offered again, so it records at once.
        """
        if job["sandbox"] != "workspace-write":
            return [], None, {}
        adir = attempt_dir(self.root, a["job_id"], a["seq"])
        receipt_path = adir / "salvage.json"
        receipt = self._read_json(receipt_path)
        if receipt is None:
            workspace = job.get("worktree") or job["workdir"]
            baseline = json.loads(a["evidence_json"] or "{}").get("baseline_commit") or job["workdir_head"]
            cap = self.policy["caps"]["workspace_git_timeout_s"]
            result = checkpoint = error = None
            skipped: list[str] = []
            try:
                result = salvage(workspace, baseline, a["seq"],
                                 writable=True, state="finalizing", timestamp=a["reserved_at"],
                                 baseline_tree=a.get("baseline_tree"), timeout_s=cap, left_out=skipped)
                checkpoint = git_head(workspace, timeout_s=cap)
            except (SalvageError, OSError) as exc:
                failures = self._salvage_failures[a["attempt_id"]] = self._salvage_failures.get(a["attempt_id"], 0) + 1
                transient = getattr(exc, "transient", False) or transient_os_error(exc)
                if retry and transient and failures < SALVAGE_TRIES:
                    raise
                # git's own words: paths and refs, never a credential. A snapshot
                # already written stands when only reading HEAD after it failed.
                # Valid UTF-8 whatever it quotes, or the receipt could not be
                # written, on this try or any other (N1; a `SalvageError` already
                # is, and this holds for any other error's text).
                stage = "checkpoint" if result else "salvage"
                error = utf8_text(f"{stage} failed: {exc}")[:500]
                self.log.warning("attempt %s %s", a["attempt_id"], error)
            self._salvage_failures.pop(a["attempt_id"], None)
            # Every path as valid UTF-8: `json_bytes` cannot encode the surrogates
            # `os.fsdecode` carries a name that is not UTF-8 in, and a receipt that
            # cannot be written is a finalization that raises on every try.
            skipped = [path_text(path) for path in skipped]
            receipt = {"result": {**dataclasses.asdict(result), "skipped": skipped} if result else None,
                       "checkpoint": checkpoint, "error": error, "skipped": skipped}
            self._publish("salvage", receipt_path, json_bytes(receipt))
            self._boundary("salvage", job["job_id"], a["attempt_id"])
        result = receipt["result"]
        evidence = {"salvage_error": receipt["error"]} if receipt.get("error") else {}
        # A receipt written before `skipped` had its own key lists them in the result.
        skipped = receipt.get("skipped", (result or {}).get("skipped")) or []
        if skipped:
            evidence["salvage_skipped"] = _skipped(skipped)
            self.log.warning("attempt %s salvage left out %d nested repositor%s with no commit, first %r",
                             a["attempt_id"], len(skipped), "y" if len(skipped) == 1 else "ies", skipped[0])
        if not result:
            return [], receipt["checkpoint"], evidence
        ref = result.get("ref") or result.get("ref_name")
        commit = result.get("commit") or result.get("commit_sha")
        return ([{"role": "salvage", "path": ref, "sha256": hashlib.sha256(commit.encode()).hexdigest(), "bytes": 0}],
                receipt["checkpoint"], evidence)

    @staticmethod
    def _salvage_summary(job: dict, artifacts: list[dict], evidence: dict) -> str:
        """C-13.1: the notice's lines for what salvage could not save ("" when it saved
        everything): its error, the paths it left out, and where they still are."""
        lines = [evidence["salvage_error"]] if evidence.get("salvage_error") else []
        skipped = evidence.get("salvage_skipped")
        if skipped:
            lines.append(f"salvage left out {_left_out(skipped)}")
        if lines and (skipped or not artifacts):
            lines[-1] += f"; the worktree is kept: {job.get('worktree') or job['workdir']}"
        return "".join("\n" + line for line in lines)

    @staticmethod
    def _held_line(job: dict, held: dict) -> str:
        """C-13.1: a start snapshot admission held after a failed salvage (a
        `salvage.baseline_held` event's data), as one line of a notice."""
        line = f"the worktree after attempt a{held['seq'] - 1} is held under {held['ref']}"
        if held.get("skipped"):
            line += (f", which left out {_left_out(held['skipped'])}; "
                     f"the worktree is kept: {job.get('worktree') or job['workdir']}")
        return line

    def _earlier_attempt(self, tx, job: dict, *, before: int | None = None) -> str:
        """C-13.1, C-15.1: the notice's lines for a job's last attempt before `before`
        (any, when None), for a job that ends without finalizing another; "" when it
        has none.

        That is a job cancelled while it waits to try again, or failed, refused or
        skipped while its retry is prepared (`_fail_queued`, `_skip_revive`), and
        a retry that never launched or was quarantined. The attempt's line, as C-15.1 gives it
        (`attempt a<seq>: <class>, rc=<rc>: <detail>`), what its salvage could not
        save (`_salvage_summary`), and the start snapshot admission held after it
        when that salvage failed (`_pin_baseline`, named by its event), which no
        attempt records when no retry was reserved (the job was cancelled while
        it waited for capacity): it is recorded here as the job's last attempt's
        salvage artifact, so retention keeps it (C-13.4) and `runs show` lists it.
        Before, such a job's notice said "cancelled before launch" or gave only
        the retry's workspace error, and nothing of the failed salvage or the ref
        (review of ceacf18b, P2).

        An attempt that never launched (`_unlaunched`: its guardian could not be
        started, or the main/master re-check got no answer from git) changed
        nothing in the worktree, so the attempt before it is named too, back to
        one that ran: a retry reserved on a1's held snapshot and queued again at
        launch, then cancelled, had named only itself, and nothing of a1's failed
        salvage or of the ref that holds a1's work.
        """
        attempts = [dict(row) for row in tx.execute("SELECT * FROM attempts WHERE job_id=? ORDER BY seq",
                                                    (job["job_id"],)).fetchall()]
        earlier = [attempt for attempt in attempts if before is None or attempt["seq"] < before]
        if not earlier:
            return ""
        unlaunched = {row[0] for row in tx.execute(
            "SELECT attempt_id FROM events WHERE job_id=? AND kind='attempt.no_launch'", (job["job_id"],)).fetchall()}
        recorded = {row[0] for row in tx.execute(
            "SELECT r.path FROM artifacts r JOIN attempts a USING(attempt_id) WHERE a.job_id=? AND r.role='salvage'",
            (job["job_id"],)).fetchall()}
        events = [held for (data,) in tx.execute(
            "SELECT data_json FROM events WHERE job_id=? AND kind='salvage.baseline_held' ORDER BY event_id",
            (job["job_id"],)).fetchall() if (held := _held_event(data)) is not None]
        lines = ""
        for last in reversed(earlier):
            evidence = json.loads(last["evidence_json"] or "{}")
            # The attempt's own start snapshot (`baseline_ref`) holds the work of the
            # attempt before it, never its own: counted as its salvage, a retry whose
            # own salvage failed lost "the worktree is kept" (review of the P2 fix).
            own = [dict(row) for row in tx.execute("SELECT * FROM artifacts WHERE attempt_id=? AND role='salvage'",
                                                   (last["attempt_id"],)).fetchall()
                   if row["path"] != evidence.get("baseline_ref")]
            held = []
            for data in events:
                if data.get("after") != last["attempt_id"]:
                    continue
                artifact = {"role": "salvage", "path": data["ref"],
                            "sha256": hashlib.sha256(data["commit"].encode()).hexdigest(), "bytes": 0}
                if data["ref"] not in recorded:
                    self.store.add_artifact(attempts[-1]["attempt_id"], **artifact)
                    recorded.add(data["ref"])
                held.append((data, artifact))
            rc = "-" if last["rc"] is None else last["rc"]
            lines += (f"\nattempt a{last['seq']}: {last['outcome_class'] or 'unknown'}, rc={rc}: "
                      f"{last['outcome_detail'] or '-'}"
                      + self._salvage_summary(job, own + [artifact for _, artifact in held], evidence)
                      + "".join("\n" + self._held_line(job, data) for data, _ in held))
            if last["attempt_id"] not in unlaunched:
                break
        return lines

    def _turn_trees(self, job: dict, a: dict, *, retry: bool = True, error: str | None = None) -> dict:
        """C-26.10, C-26.14 (design D-25): a turn's end, taken while its leases are held.

        HEAD after, and for a writable turn whose admission took a start snapshot
        (the attempt's `baseline_tree`, C-6.8), the end snapshot through the same
        temporary index: a tree object, no ref, the real index and files untouched.
        The receipt `trees.json` makes a replayed finalization take nothing twice.
        A transient git failure raises, so the worker tries again with its
        backoff, until `TURN_TREE_TRIES` tries in all have failed (the first and
        two retries; the count is in memory, so a restart starts it again); that
        failure, or any other, is recorded and the turn ends without an end
        snapshot. A quarantine's
        release passes `retry=False` (an operator's one-shot request is never
        offered again, so it records at once), and `error` when writers may still
        be live: then no snapshot is taken and the error is what is recorded.
        """
        from .conversations import diff as turn_diff
        adir = attempt_dir(self.root, a["job_id"], a["seq"])
        path = adir / "trees.json"
        receipt = self._read_json(path)
        if receipt is None:
            writable = job["sandbox"] == "workspace-write"
            evidence = json.loads(a["evidence_json"] or "{}")
            receipt = {"workspace": job.get("worktree") or job["workdir"], "writable": writable,
                       "head_before": evidence.get("baseline_commit"),
                       "start_tree": a.get("baseline_tree") if writable else None,
                       "head_after": None, "end_tree": None, "skipped": [], "error": error}
            if error is None:
                try:
                    receipt.update(turn_diff.end_snapshot(
                        receipt["workspace"], head_before=receipt["head_before"], start_tree=receipt["start_tree"],
                        timeout_s=self.policy["caps"]["workspace_git_timeout_s"]))
                except (SalvageError, OSError) as exc:
                    failures = self._tree_failures[a["attempt_id"]] = self._tree_failures.get(a["attempt_id"], 0) + 1
                    transient = getattr(exc, "transient", False) or transient_os_error(exc)
                    if retry and transient and failures < TURN_TREE_TRIES:
                        raise
                    receipt["error"] = utf8_text(f"end snapshot failed: {exc}")[:500]     # as `_salvage`'s
            self._tree_failures.pop(a["attempt_id"], None)
            receipt["at"] = utcnow()
            self._publish("trees", path, json_bytes(receipt))
            self._boundary("trees", job["job_id"], a["attempt_id"])
        turn = (self._read_json(self.root / "jobs" / job["job_id"] / "manifest.json") or {}).get("turn")
        if turn:
            self.conversations.record_trees(turn, a, receipt)
        return receipt

    @staticmethod
    def _artifact(path: Path, role: str) -> dict | None:
        try:
            contents = read_regular(path)          # only a regular file is an artifact
        except (FileNotFoundError, NotRegularFile):
            return None
        return {"role": role, "path": str(path), "sha256": hashlib.sha256(contents).hexdigest(), "bytes": len(contents)}

    @staticmethod
    def _restore_outcome(data: dict) -> Outcome:
        data = dict(data)
        data["cls"] = OutcomeClass(data["cls"])
        data["readings"] = tuple(Reading(**{**row, "label": ReadingLabel(row["label"])}) for row in data.get("readings", ()))
        if data.get("closure"):
            closure = data["closure"]
            data["closure"] = Closure(**{**closure, "reason": ClosureReason(closure["reason"]),
                                         "clock_source": ClockSource(closure["clock_source"])})
        return Outcome(**data)

    def _finalize(self, a: dict, *, lost: bool = False) -> None:
        job = self._job(a["job_id"])
        actual = self.store.get_attempt(a["attempt_id"])
        current = self.store.one("SELECT MAX(seq) n FROM attempts WHERE job_id=?", (a["job_id"],))["n"]
        if job["state"] in TERMINAL or job["accepted_attempt_id"] or current != a["seq"] or actual["state"] not in ("starting", "running", "finalizing"):
            return
        adir = attempt_dir(self.root, a["job_id"], a["seq"])
        adir.mkdir(mode=0o700, exist_ok=True)
        census = self._contain(a)
        if not census.verified_empty:
            # The guardian writes the receipt just before it exits, and processes
            # it already reaped can still be leaving the process table under
            # load. Allow a bounded settle window (C-5.9, exit_settle_s), re-running
            # the census on each tick, before declaring that writers remain.
            since = self._exit_settle.setdefault(a["attempt_id"], time.monotonic())
            if time.monotonic() - since < self.exit_settle_s:
                return
            self._exit_settle.pop(a["attempt_id"], None)
            self._quarantine(a, census, "writers remain after exit receipt")
            return
        self._exit_settle.pop(a["attempt_id"], None)
        launch = self._saved_launch(a)
        # Both stream-json CLIs write their raw protocol to stdout. Freeze that
        # stream once after containment when no separate raw file was supplied.
        if launch.raw_stream_path and not Path(launch.raw_stream_path).exists():
            try:
                data = read_regular(Path(launch.stdout_path))       # never waiting in open()
            except (FileNotFoundError, NotRegularFile):
                data = None                                        # none there, as `is_file()` had said
            if data is not None:
                self._publish("raw-stream", Path(launch.raw_stream_path), data)
        lane = self.store.get_lane(a["lane_id"])
        adapter = self.conversations.adapter(lane.provider) if job["kind"] == "turn" else get_adapter(lane.provider)
        if job["kind"] == "turn":
            self.conversations.release_socket(a["attempt_id"])
        receipt = self._read_json(adir / "exit.json")
        # The receipt on disk decides, not the verdict the caller reached before
        # reading it: a guardian found dead a moment after it published exit.json
        # completed its attempt (C-4.2). A missing receipt or missing return code
        # is a loss; the existence of a receipt alone cannot prove completion.
        lost = receipt is None or receipt.get("rc") is None
        if receipt and actual["state"] != "finalizing":
            self._begin_finalizing(a, receipt)
        rc = None if lost else receipt["rc"]
        if lost:
            outcome = Outcome(OutcomeClass.UNKNOWN, "guardian lost without exit receipt" if receipt is None
                              else "guardian exit receipt has no return code")
            attest_status, served_model = "unattested", None
        else:
            result_path = adir / "finalization.json"
            result = self._read_json(result_path)
            if result is None:
                exit_info = ExitInfo(**{k: receipt.get(k) for k in ("rc", "signal", "wall_s", "child_pid", "spawn_error")})
                outcome = adapter.classify(adir, launch, exit_info)
                if exit_info.spawn_error:
                    outcome = dataclasses.replace(outcome, detail=exit_info.spawn_error)
                attest = adapter.attest(adir, launch, outcome, a["model_requested"])
                result = {"outcome": dataclasses.asdict(outcome), "attestation": dataclasses.asdict(attest)}
                self._publish("finalization", result_path, json_bytes(result))
            outcome = self._restore_outcome(result["outcome"])
            attest_status = Attestation(result["attestation"]["status"]).value
            served_model = result["attestation"]["served_model"]
        deliverable_path = adir / "deliverable.md"
        if not deliverable_path.exists() and not lost:
            contents = adapter.deliverable(adir, launch, outcome)
            self._publish("deliverable", deliverable_path, contents or b"")
        deliverable = self._artifact(deliverable_path, "deliverable")
        # The daemon's two overrides of the adapter's `ok`. finalization.json
        # keeps the adapter's verdict; both are recomputed from the attempt row
        # and the captured deliverable on every (re)finalization, so a replay
        # reaches the same class (C-4.3), and the adapter's verdict is kept in
        # the attempt's evidence beside the class that decided.
        provider_verdict = {"class": outcome.cls.value, "detail": outcome.detail}
        if outcome.cls == OutcomeClass.OK and actual.get("killed_by") and job["kind"] != "turn":
            # C-9.2: an attempt the daemon signalled (an operator's kill, the
            # wall limit, recovery) did not finish, whatever its rc and its
            # deliverable say. Codex exits 0 on SIGTERM and leaves its last
            # interim message in last.md, which classify reads as a completed
            # turn (incident: 2026-09-24, three cancelled Codex jobs recorded
            # `ok` with rc 0 and deliverables of 32 to 61 words such as "I am
            # checking the newer validation code before finalizing").
            # A conversation turn is exempt: its `ok` is the driver's recorded
            # `complete` (turn.json, the provider's own end-of-turn event), never
            # an exit status or a last message, so a signal after it (a stop
            # that came too late, or containment of a process that lingered)
            # does not undo it (C-24.4, merge review 2026-09-25).
            outcome = dataclasses.replace(
                outcome, cls=OutcomeClass.UNKNOWN,
                detail=f"stopped by {actual['killed_by']}: exit {rc} after the daemon's signal "
                       f"is not a finished deliverable")
        if outcome.cls == OutcomeClass.OK and job["kind"] != "turn" and (not deliverable or not deliverable["bytes"]):
            outcome = dataclasses.replace(outcome, cls=OutcomeClass.UNKNOWN, detail="empty deliverable with rc 0")
        if not lost and self._answered(outcome, provider_verdict["class"]):
            # C-6.14: before the attempt leaves flight, so a pilot that answered and
            # ended at once frees its lane rather than handing the hold to another.
            self._record_answer(a["lane_id"], "finalization", attempt=a)
        artifacts = [x for x in [deliverable,
                     self._artifact(Path(launch.stdout_path), "stdout"),
                     self._artifact(Path(launch.stderr_path), "stderr"),
                     self._artifact(adir / "launch.json", "launch"),
                     self._artifact(Path(launch.stdin_path), "prompt-sent")
                     if launch.stdin_path and Path(launch.stdin_path).name == "prompt.sent.md" else None,
                     self._artifact(adir / "lane.log", "lane-log"),
                     self._artifact(Path(launch.raw_stream_path), "raw-stream") if launch.raw_stream_path else None,
                     self._artifact(self.root / "jobs" / job["job_id"] / "manifest.json", "manifest")] if x]
        # C-26.10: a turn works in its conversation's workspace and writes no salvage ref;
        # its receipt records HEAD after and its end snapshot (C-26.14).
        trees, salvage_evidence = None, {}
        if job["kind"] == "turn":
            salvage_artifacts, checkpoint = [], None
            trees = self._turn_trees(job, a)
        else:
            salvage_artifacts, checkpoint, salvage_evidence = self._salvage(job, a)
        artifacts.extend(salvage_artifacts)
        with self.store.transaction("attempt.accepted", job_id=a["job_id"], attempt_id=a["attempt_id"]) as tx:
            job = self._job(a["job_id"])
            current = tx.execute("SELECT MAX(seq) FROM attempts WHERE job_id=?", (job["job_id"],)).fetchone()[0]
            actual = self.store.get_attempt(a["attempt_id"])
            eligible_states = ("starting", "running", "finalizing") if lost else ("finalizing",)
            if current != a["seq"] or actual["state"] not in eligible_states or job["accepted_attempt_id"] or job["state"] in TERMINAL:
                return
            cancel = bool(job["cancel_requested_at"])
            ok = not lost and rc == 0 and outcome.cls == OutcomeClass.OK
            if job["kind"] == "turn":
                # C-24.4: the provider's success decides a turn, whatever the exit
                # status or a stop that came too late (review F4).
                ok = not lost and outcome.cls == OutcomeClass.OK
                cancel = cancel and not ok
            previous_transient = self._earlier_transients(tx, job["job_id"], a)
            # C-4.5: a lane fault (an `auth-dead` lane, on an unpinned job whose
            # workspace it left as it found it) moves the job on to the next
            # candidate as `limited` does, and `max_attempts` does not count it.
            moved = self._uncharged(tx, job["job_id"], before=a["seq"])
            fault = None if lost or cancel or moved else self._lane_fault(job, a, outcome, checkpoint,
                                                                           salvage_artifacts, salvage_evidence)
            # C-23.44: a job that already moved on from one auth-dead lane and meets
            # another is itself the common factor. It ends, and this lane stays
            # enabled for the next attempt there to judge.
            again = not lost and outcome.cls == OutcomeClass.AUTH_DEAD and moved > 0
            retry = (not cancel and (fault is not None or self._attempts_left(tx, job, a) and
                     ((lost and job["sandbox"] == "read-only") or
                      (outcome.cls == OutcomeClass.LIMITED and not job["pinned_lane"]) or
                      (outcome.cls == OutcomeClass.TRANSIENT and not (job["pinned_lane"] and previous_transient)))))
            attempt_state = "interrupted" if cancel else "lost" if lost else "succeeded" if ok else "failed"
            job_state = "cancelled" if cancel else "waiting" if retry else "lost" if lost else "succeeded" if ok else "failed"
            evidence = json.loads(a["evidence_json"] or "{}")
            evidence.update(classification=outcome.evidence, checkpoint=checkpoint, **salvage_evidence)
            if trees is not None:
                evidence["turn_trees"] = {k: trees.get(k) for k in ("head_before", "head_after", "start_tree",
                                                                     "end_tree", "skipped", "error")}
            if provider_verdict["class"] != outcome.cls.value:
                evidence["provider_verdict"] = {**provider_verdict, "killed_by": actual.get("killed_by")}
            if fault is not None:
                evidence["lane_fault"] = fault
            if again:
                evidence["auth_dead_again"] = {"lane_id": a["lane_id"], "lane_left_enabled": True}
            tx.execute("UPDATE attempts SET state=?,rc=?,outcome_class=?,outcome_detail=?,evidence_json=?,attestation=?,model_served=?,native_session_id=COALESCE(?,native_session_id),transcript_path=?,finished_at=COALESCE(finished_at,?) WHERE attempt_id=?",
                       (attempt_state, rc, outcome.cls.value, outcome.detail, json.dumps(evidence), attest_status, served_model,
                        outcome.native_session_id, outcome.transcript_path, utcnow(), a["attempt_id"]))
            for artifact in artifacts:
                self.store.add_artifact(a["attempt_id"], **artifact)
            if outcome.cls == OutcomeClass.AUTH_DEAD and not again:
                self.store.update_lane(a["lane_id"], enabled=0)
                self.timers.record_auth_dead(a["lane_id"])
            self._record_identity(a["lane_id"], outcome)   # C-10.6
            for reading in outcome.readings:
                self.store.add_reading(reading)
            if outcome.closure:
                self.store.add_closure(outcome.closure)
            tx.execute("DELETE FROM leases WHERE holder=? AND lease_key LIKE 'lane:%'", (a["attempt_id"],))
            job_rc = 130 if cancel else None if retry else 125 if lost else rc
            if not cancel and not retry and not lost:
                job_rc = {OutcomeClass.LIMITED: 4,  # C-17.3: a final limited attempt reports 4, pinned or not
                          OutcomeClass.AUTH_DEAD: 5, OutcomeClass.CLI_TOO_OLD: 6}.get(outcome.cls, rc)
            accepted = a["attempt_id"] if ok and not cancel else None
            next_check = after(60 if outcome.cls == OutcomeClass.TRANSIENT else 0) if retry else None
            tx.execute("UPDATE jobs SET state=?,rc=?,accepted_attempt_id=?,finished_at=?,wait_reason=?,next_check_at=? WHERE job_id=?",
                       (job_state, job_rc, accepted, None if retry else utcnow(), "capacity" if retry else None, next_check, job["job_id"]))
            if not retry:
                if not accepted:
                    tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (job["job_id"], a["attempt_id"]))
                uncertainty = f"; {attest_status}" if attest_status != "attested" else ""
                # C-15.1: the header is the job's (see `_notice`); the summary is
                # the final attempt's evidence, and says what became of an output
                # the job did not accept, so nobody reads it as the result.
                summary = (f"attempt a{a['seq']}: {outcome.cls.value}, rc={'-' if rc is None else rc}: "
                           f"{outcome.detail}{uncertainty}")
                if not accepted and deliverable and deliverable["bytes"]:
                    summary += f"\noutput kept, not accepted: {deliverable_path}"
                    if job["out_path"]:
                        summary += f"; -o {job['out_path']} was not written"
                summary += self._salvage_summary(job, salvage_artifacts, salvage_evidence)
                if receipt and receipt.get("spawn_error") and receipt.get("child_pid") is None:
                    # C-13.1, C-15.1: no provider ran (refused on main at launch, a home
                    # or credential that could not be resolved, a binary that could not
                    # be spawned), so nothing changed in the worktree, and the attempt
                    # before it is named as for one that never launched (review of the
                    # P2 fix: a1's failed salvage and the ref holding its work were in
                    # no notice).
                    summary += self._earlier_attempt(tx, job, before=a["seq"])
                self._notice(tx, job, summary)
        if fault is not None:
            self.log.warning("job %s: lane %s went auth-dead on attempt a%d (%s); the lane is disabled and the "
                             "job moves on to the next lane, this attempt not counted (C-4.5)",
                             job["job_id"], a["lane_id"], a["seq"], outcome.detail)
        if again:
            self.log.warning("job %s: attempt a%d met auth-dead on a second lane, %s (%s); two lanes refusing one "
                             "job points at the job, so %s is left enabled and the job ends (C-23.44)",
                             job["job_id"], a["seq"], a["lane_id"], outcome.detail, a["lane_id"])
        if not retry:
            self._boundary("terminal", a["job_id"], a["attempt_id"])
            self._boundary("notice", a["job_id"], a["attempt_id"])
        if accepted:
            self._export(job["job_id"])
        self._notify()

    @staticmethod
    def _lane_fault(job: dict, a: dict, outcome: Outcome, checkpoint: str | None,
                    salvage_artifacts: list[dict], salvage_evidence: dict) -> dict | None:
        """C-4.5: what makes an attempt's `auth-dead` its lane's fault and not the
        job's, as its evidence records it, or None.

        The lane refused the credential (C-9.3: an organisation block, a revoked
        token, an explicit refusal, all from the CLI's own words, never the
        model's), so the next lane can run the job; the job pinned no lane, so it
        may move; it is no conversation turn (its conversation decides its
        failover, C-26.7); and the attempt changed nothing the next one would
        start from: the job is read-only, or its salvage found the end tree equal
        to the start snapshot (no ref written, none left out, no error) and HEAD
        where it was, and its model never answered (`model_answered` False: nothing
        ran, so nothing outside the worktree changed either). A job the lane could
        have changed waits for reconciliation (C-13.3) as before. The caller gives a
        job one lane fault: a second `auth-dead` ends it (`_finalize`). 2026-09-30: an organisation disabled claude-5's
        Claude Code access, and 37 jobs from about 20 sessions failed there with
        rc 5, after one attempt each, while other lanes were coming back.
        """
        if outcome.cls != OutcomeClass.AUTH_DEAD or job["pinned_lane"] or job["kind"] == "turn":
            return None
        answered = (outcome.evidence or {}).get("model_answered")
        fault = {"class": outcome.cls.value, "lane_id": a["lane_id"], "seq": a["seq"], "model_answered": answered}
        if job["sandbox"] == "read-only":
            return {**fault, "workspace": "read-only"}
        if answered is not False:
            # A writable attempt whose model answered may have pushed, commented or
            # written outside its worktree, which no tree shows; where the adapter
            # cannot say, it is taken to have.
            return None
        baseline = json.loads(a["evidence_json"] or "{}").get("baseline_commit") or job["workdir_head"]
        if salvage_artifacts or salvage_evidence or not checkpoint or checkpoint != baseline:
            return None
        return {**fault, "workspace": "unchanged", "head": checkpoint}

    @staticmethod
    def _uncharged(conn, job_id: str, *, before: int) -> int:
        """C-4.5: the job's attempts before `before` that were lane faults (at most
        one: a job moves on once), which `max_attempts` does not count."""
        return sum(1 for (data,) in conn.execute(
            "SELECT evidence_json FROM attempts WHERE job_id=? AND seq<? AND outcome_class='auth-dead'",
            (job_id, before)) if _lane_fault_of(data))

    def _attempts_left(self, conn, job: dict, a: dict) -> bool:
        """C-4.5: may the job have an attempt after `a`, which is no lane fault?
        `max_attempts` counts every attempt but the one lane fault a job may have
        had before it: that attempt ran nothing (a writable job's), or only read."""
        return a["seq"] - self._uncharged(conn, job["job_id"], before=a["seq"]) < job["max_attempts"]

    @staticmethod
    def _moved_on(conn, job_id: str) -> str:
        """C-4.5, C-15.1: the notice's line for each lane fault the job moved on from."""
        lines = ""
        for seq, lane_id, detail, data in conn.execute(
                "SELECT seq,lane_id,outcome_detail,evidence_json FROM attempts "
                "WHERE job_id=? AND outcome_class='auth-dead' ORDER BY seq", (job_id,)).fetchall():
            if _lane_fault_of(data):
                lines += (f"\nattempt a{seq}: lane {lane_id} went auth-dead ({detail or '-'}); it is disabled until "
                          f"`subfleet lanes enroll` rebinds its credential, and the job moved on to the next lane "
                          f"without counting the attempt")
            elif _evidence_key(data, "auth_dead_again"):
                lines += (f"\nattempt a{seq}: lane {lane_id} answered auth-dead too; two lanes refusing one job "
                          f"points at the job, so {lane_id} was left enabled")
        return lines

    def _export(self, job_id: str) -> None:
        with self._busy_lock:
            lock = self._export_locks.setdefault(job_id, threading.Lock())
        with lock:
            self._export_locked(job_id)

    def _export_locked(self, job_id: str) -> None:
        job = self._job(job_id)
        if not job["accepted_attempt_id"]:
            return
        if job["out_path"]:
            lease = self.store.one("SELECT holder FROM leases WHERE lease_key=?", (f"out:{job['out_path']}",))
            if not lease or lease["holder"] != job_id:
                return  # A replay cannot overwrite a newer owner's output.
        a = self.store.get_attempt(job["accepted_attempt_id"])
        artifact = self.store.one("SELECT * FROM artifacts WHERE attempt_id=? AND role='deliverable'", (a["attempt_id"],))
        export_error = None
        exported = None
        if job["out_path"] and not self.store.one("SELECT 1 FROM artifacts WHERE attempt_id=? AND role='export'", (a["attempt_id"],)):
            try:
                contents = read_regular(artifact["path"])          # only a regular file, never waiting in open()
                destination = Path(job["out_path"])
                if hashlib.sha256(contents).hexdigest() != artifact["sha256"]:
                    raise OSError("accepted deliverable digest changed")
                self._publish("export", destination, contents)
                self._boundary("export", job_id, a["attempt_id"])
                exported = {"role": "export", "path": str(destination), "sha256": artifact["sha256"], "bytes": artifact["bytes"]}
            except OSError as exc:
                export_error = f"export failed: {type(exc).__name__} (errno={exc.errno})"
        with self.store.transaction("job.export_failed" if export_error else "job.exported", job_id=job_id, attempt_id=a["attempt_id"]) as tx:
            if exported:
                self.store.add_artifact(a["attempt_id"], **exported)
            if export_error:
                tx.execute("UPDATE jobs SET export_error=? WHERE job_id=?", (export_error, job_id))
                tx.execute("UPDATE notices SET text=text || ? WHERE job_id=?", ("\n" + export_error, job_id))
            tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (job_id, a["attempt_id"]))
        self._notify()

    def _respond(self, conn: socket.socket, write_lock: threading.Lock, req: protocol.Request,
                 arrived: float | None = None) -> None:
        def gone() -> bool:
            return descriptors.client_gone(conn)

        def ended_here() -> bool:
            return self._ended_here(conn)
        gone.ended_here = ended_here
        if descriptors.read_only(req.op, req.args) and gone():
            # C-16.7: its client timed out and hung up while this waited for a
            # thread. A read has no one to answer; a write still runs, because a
            # client disconnect cannot cancel its durable job. A stopping daemon
            # has shut the socket down itself, and so has one that ended the
            # stream after a broken reply: neither is a client leaving.
            if not self.stopping.is_set() and not ended_here():
                self._count_connection("abandoned", req.op)
            return
        if req.op != "wait":
            return self._answer(conn, write_lock, req, arrived, gone)
        with self._waits_answered:
            self._waits_answering += 1
        try:
            self._answer(conn, write_lock, req, arrived, gone)
        finally:
            with self._waits_answered:
                self._waits_answering -= 1
                self._waits_answered.notify_all()

    def _answer(self, conn: socket.socket, write_lock: threading.Lock, req: protocol.Request,
                arrived: float | None, client_gone: Callable[[], bool]) -> None:
        try:
            response = protocol.ok(req.id, self.dispatch(req.op, req.args, arrived=arrived, client_gone=client_gone))
        except (protocol.ProtocolError, AdapterError) as exc:
            response = protocol.fail(req.id, exc.code, str(exc), exc.fix)
        except (ValueError, TypeError, KeyError) as exc:
            response = protocol.fail(req.id, 2, f"invalid arguments: {exc}")
        except Exception as exc:
            self.log.error("request %s failed: %s", req.op, type(exc).__name__)
            response = protocol.fail(req.id, 1, "operation failed; inspect daemon status")
        send_reply(conn, write_lock, response, end_stream=self._end_stream)

    def _decode(self, conn: socket.socket, write_lock: threading.Lock,
                line: bytes | descriptors.Oversized) -> protocol.Request | None:
        """One framed line as a request, or None after answering why it is not one."""
        try:
            if line is descriptors.OVERSIZED:
                raise protocol.ProtocolError("request exceeds 1 MiB")
            return protocol.decode_request(line)
        except (protocol.ProtocolError, ValueError, RecursionError) as exc:
            # C-16.7: a malformed line is answered, never left to end the reader:
            # ValueError covers bad UTF-8 and an integer past
            # sys.int_max_str_digits, RecursionError nesting that parses but is
            # too deep to render in an error message (as #55 on main).
            message = (str(exc) if isinstance(exc, (protocol.ProtocolError, UnicodeDecodeError))
                       else "malformed request: nested too deeply" if isinstance(exc, RecursionError)
                       else f"malformed request: {exc}")
            if not send_reply(conn, write_lock, protocol.fail("", 2, message), end_stream=self._end_stream):
                raise OSError("the reply to an undecodable request failed part way")
            return None

    def _start_request(self, conn: socket.socket, write_lock: threading.Lock, req: protocol.Request,
                       arrived: float, peer: int | None) -> Future | None:
        """Start one request where it runs (C-16.5, C-25.3): a future, or None when
        it was answered here, on the connection's own thread."""
        if self.conversations.owns(req.op):
            # Conversation ops run on the conversation pools: the long polls on
            # their own (C-25.4), the file work on its own (C-25.3).
            return self.conversations.pool_for(req.op).submit(
                self.conversations.respond, conn, write_lock, req, peer)
        if req.op == "ping" and not protocol.ping_writes(req.args):
            # C-16.5, C-15.8: a liveness question is answered here, never queued:
            # it reads nothing, so a slow daemon still says at once that it is alive.
            self._respond(conn, write_lock, req, arrived)
            return None
        # Submission filesystem work and long polls have separate pools;
        # ordinary read/cancel operations stay responsive.
        pool = (self.workers if req.op == "submit" or req.op.startswith("gate.") else
                self.waiters if req.op == "wait" else
                self.lookups if req.op in LOOKUP_OPS else self.requests)
        return pool.submit(self._respond, conn, write_lock, req, arrived)

    def _connection(self, conn: socket.socket) -> None:
        write_lock = threading.Lock()
        pending: list[Future] = []
        reads: set[Future] = set()              # pending futures whose request only reads
        framer = descriptors.LineFramer()
        replied = [0.0]                         # when a reply last finished (monotonic)
        from .conversations.peers import peer_pid
        peer = peer_pid(conn)                   # C-25.6: who is asking, read from the socket

        def note_reply(_future=None) -> None:
            replied[0] = time.monotonic()
        try:
            # C-16.7: reads time out instead of blocking, so a client that says
            # nothing cannot hold this reader and its descriptor for ever. The
            # same timeout bounds sending a reply to a client that stops reading.
            conn.settimeout(self.connection_idle_s)
            while not self.stopping.is_set():
                try:
                    chunk = conn.recv(65536)
                except TimeoutError:
                    pending = [f for f in pending if not f.done()]
                    reads.intersection_update(pending)
                    if pending or time.monotonic() - replied[0] < self.connection_idle_s:
                        continue           # a reply is being worked on, or went out recently
                    self._count_connection("idle_closed")
                    break
                arrived = time.monotonic()      # C-15.4: a `wait`'s deadline runs from here
                for line in framer.feed(chunk) if chunk else framer.finish():
                    req = self._decode(conn, write_lock, line)
                    if req is None:
                        continue
                    pending = [f for f in pending if not f.done()]
                    reads.intersection_update(pending)
                    try:
                        future = self._start_request(conn, write_lock, req, arrived, peer)
                    except RuntimeError as exc:
                        # The pool could not start a thread. The standard pool has
                        # queued the call before it tried, so the request may still
                        # run (and answer) when a worker frees, but it has no
                        # future to wait on here. The reader goes on, and the count
                        # says so (review of 3c8fe55, P2), as `unscheduled` as on
                        # main (#55). A pool shut down by a stopping daemon refuses
                        # the same way, and is not counted.
                        if not self.stopping.is_set():
                            self._count_connection("unscheduled", f"{req.op} ({exc})")
                        continue
                    if future is None:
                        note_reply()
                        continue
                    future.add_done_callback(note_reply)
                    pending.append(future)
                    if descriptors.read_only(req.op, req.args):
                        reads.add(future)
                if not chunk:
                    break
        except OSError:
            pass
        finally:
            # Not while stopping: close()'s own SHUT_RDWR also makes the peer look
            # gone, and close() cancels what is queued itself. Nor after this
            # daemon ended the stream itself (`_end_stream`): that is not a client
            # leaving either.
            with self._connection_lock:
                ours = conn in self._shut_down
            if not self.stopping.is_set() and (ours or descriptors.client_gone(conn)):
                # C-16.7: the client closed its whole socket, or this daemon ended
                # the stream after a broken reply. Either way no reply can reach
                # it, so its reads no thread has reached yet are cancelled now: the
                # connection and its place under the cap go at once instead of when
                # a busy pool gets to them. Its writes still run. Only a client
                # that left is counted.
                for future in pending:
                    if future in reads and future.cancel() and not ours:
                        self._count_connection("abandoned", "queued read")
            # No process waits here. Running callbacks own their response socket
            # until they finish, including after the caller closes its write half.
            def finish(_=None):
                if all(f.done() for f in pending):
                    conn.close()
                    with self._connection_lock:
                        self._connections.discard(conn)
                        self._shut_down.discard(conn)
            for f in pending:
                f.add_done_callback(finish)
            finish()

    @property
    def max_connections(self) -> int:
        """C-16.7: the most client connections held at once, now: pinned by the
        caller, or derived from the open-file soft limit, less what the running
        conversation turns hold of their own (`descriptors.TURN_DESCRIPTORS` each)."""
        if self._pinned_max_connections is not None:
            return self._pinned_max_connections
        return descriptors.max_connections(descriptors.open_file_limits()[0], live_turns=self._live_turns())

    def _live_turns(self) -> int:
        """Conversation turns whose runner is still going: each holds its relay
        socket to the guardian (C-26.4) and reads the turn's stdout."""
        service = self.__dict__.get("conversations")       # none yet while __init__ runs
        return service.live_runners() if service is not None else 0

    def _hold_connection(self, conn: socket.socket) -> None:
        """C-16.7: hold a new connection, with a reader of its own, up to the cap.

        Each admitted connection gets its own reader thread, so it is read at
        once; a pool's idle-thread count is a heuristic and could leave one
        queued behind the others (review of #43, F1). One over the cap, or one
        for which no reader thread can start, is answered busy at once and
        closed, before anything is read, instead of waiting with its descriptor
        open while its client times out.
        """
        cap = self.max_connections
        with self._connection_lock:
            held = len(self._connections)
            admitted = held < cap and not self.stopping.is_set()
            if admitted:
                self._connections.add(conn)       # close() shuts down whatever is here
                self._connection_counts["accepted"] += 1
        if not admitted:
            self._refuse(conn, f"the daemon is busy: it holds {held} client connections, its limit",
                         "connection over the cap")
            return
        reader = threading.Thread(target=self._read_connection, args=(conn,),
                                  name="subfleet-socket", daemon=True)
        with self._connection_lock:
            self._readers.add(reader)
        try:
            reader.start()
        except RuntimeError as exc:               # no thread could start
            with self._connection_lock:
                self._readers.discard(reader)
                self._connections.discard(conn)
            # Nothing was read, so the answer is busy, and the client may try again.
            # Logged through the refusal count (1st, 2nd, 4th ...), not once per
            # connection: out of threads, every client would add a line.
            self._refuse(conn, f"the daemon is busy: it could not start a thread for this connection ({exc})",
                         f"connection no reader thread could start for ({exc})")

    def _read_connection(self, conn: socket.socket) -> None:
        try:
            self._connection(conn)
        finally:
            with self._connection_lock:
                self._readers.discard(threading.current_thread())

    def _ended_here(self, conn: socket.socket) -> bool:
        """C-16.7: whether this daemon shut the stream down after a broken reply:
        no reply can go out, but no client left either."""
        with self._connection_lock:
            return conn in self._shut_down

    def _end_stream(self, conn: socket.socket) -> None:
        """C-16.7: shut a connection down after a reply that failed part way. Its
        callers hold the connection's write lock, so no other reply can follow
        the broken one. Only a connection still held is recorded: a request a
        stalled pool ran after its reader had closed the socket and let it go
        must not leave an entry nothing removes (review of 3c8fe55, P2)."""
        with self._connection_lock:
            if conn in self._connections:
                self._shut_down.add(conn)
        with contextlib.suppress(OSError):
            conn.shutdown(socket.SHUT_RDWR)

    def _refuse(self, conn: socket.socket, message: str, detail: str) -> None:
        """C-16.7: answer busy (exit 69) before reading anything, and close."""
        if self.stopping.is_set():
            conn.close()                       # shutting down: not busy, just gone
            return
        self._count_connection("refused", detail)
        try:
            conn.setblocking(False)            # a fresh socket's buffer takes one line
            conn.send(busy_answer(message))
        except OSError:
            pass
        finally:
            conn.close()

    #: C-16.7: what `_count_connection` logs, at the 1st, 2nd, 4th, 8th, ... time.
    _CONNECTION_EVENTS = {"refused": "connections refused busy",
                          "idle_closed": "idle connections closed",
                          "abandoned": "requests dropped because their client hung up",
                          "unscheduled": "requests whose pool could not start a thread"}

    def _count_connection(self, kind: str, detail: str | None = None) -> int:
        with self._connection_lock:
            self._connection_counts[kind] += 1
            count = self._connection_counts[kind]
            held = len(self._connections)
        if kind in self._CONNECTION_EVENTS and count & (count - 1) == 0:   # the log stays bounded
            self.log.warning("client connections: %d %s so far%s (%d of %d held)",
                             count, self._CONNECTION_EVENTS[kind],
                             f", the last a {detail}" if detail else "", held, self.max_connections)
        return count

    def _descriptor_status(self) -> dict:
        """C-16.6, C-16.7: the open-file limit, what is open, and what the connection cap has done."""
        soft, hard = descriptors.open_file_limits()
        turns = self._live_turns()
        with self._connection_lock:
            held = len(self._connections)
            counts = dict(self._connection_counts)
        return {"soft_limit": descriptors.limit_for_display(soft),
                "hard_limit": descriptors.limit_for_display(hard),
                "open": descriptors.open_descriptors(), "connections": held,
                "max_connections": self.max_connections, "idle_s": self.connection_idle_s,
                "live_turns": turns,
                "reserve": descriptors.DESCRIPTOR_RESERVE + descriptors.TURN_DESCRIPTORS * turns,
                **counts}

    def serve_forever(self) -> None:
        sock_path = self.root / "daemon.sock"
        sock_path.unlink(missing_ok=True)  # Protected by the lifetime flock.
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        # C-16.7: socket.SOMAXCONN, 128, which is also macOS's default
        # kern.ipc.somaxconn cap: connections wait there while this loop pauses
        # on a shortage. A burst past a full backlog is refused at connect,
        # which the CLI reports as no daemon at all.
        self._socket.listen(max(64, socket.SOMAXCONN))
        self._socket.settimeout(.2)
        self._control_thread = threading.Thread(target=self._control, name="subfleet-control", daemon=True)
        self._control_thread.start()
        self.lock_watch.start()
        failures, failing_since = 0, 0.0
        try:
            while not self.stopping.is_set():
                try:
                    conn, _ = self._socket.accept()
                except socket.timeout:
                    continue
                except OSError as exc:
                    if self.stopping.is_set():
                        break
                    if exc.errno not in ACCEPT_TRANSIENT:
                        raise
                    # C-16.6: out of descriptors or buffers is a condition to
                    # wait out, not a reason to exit. On 2026-09-24 one EMFILE
                    # here ended the daemon while its clients waited on replies.
                    # The connection `accept` failed on is lost: macOS drops it,
                    # and its client reads the end of the stream with no answer.
                    now = time.monotonic()
                    failures += 1
                    if failures == 1:
                        failing_since = now
                    self._count_connection("accept_failures")
                    if now - failing_since >= ACCEPT_GIVE_UP_S:
                        self.log.error("accept has failed without a break for %d s (%s, %d in a row); "
                                       "exiting so launchd starts a fresh daemon", ACCEPT_GIVE_UP_S,
                                       errno.errorcode.get(exc.errno, exc.errno), failures)
                        raise
                    delay = min(ACCEPT_RETRY_CEILING_S, ACCEPT_RETRY_BASE_S * 2 ** min(failures - 1, 16))
                    if failures & (failures - 1) == 0:     # 1, 2, 4, 8, ...: the log stays bounded
                        self.log.error("accept failed: %s (%d in a row, %d connections open, %s descriptors, next try in %g s)",
                                       errno.errorcode.get(exc.errno, exc.errno), failures,
                                       len(self._connections), descriptors.open_descriptors() or "unknown", delay)
                    # A sleep, not `self.stopping.wait`: this is the main thread,
                    # where the SIGTERM/SIGINT handler runs and calls
                    # `stopping.set()`. `Event.wait` holds the event's
                    # non-reentrant lock except while it blocks, so a signal in
                    # that window would leave `set()` waiting on its own thread
                    # for ever. The loop sees `stopping` on its next pass, at
                    # most one pause (2 s) late. (Release line's hotfix review, F5.)
                    time.sleep(delay)
                    continue
                if failures:
                    self.log.info("accept recovered after %d failures", failures)
                    failures = 0
                self._hold_connection(conn)
        finally:
            self.close()

    def _write_lock(self, *, stack_dumps: bool) -> None:
        """Write this daemon's identity to `daemon.lock` (C-5.3), and whether
        SIGUSR1 dumps its stacks now (C-3.6). Only `daemon stacks` reads the flag."""
        record = {**self._ident, **({"stack_dumps": True} if stack_dumps else {})}
        os.ftruncate(self._lock_fd, 0)
        os.pwrite(self._lock_fd, json_bytes(record), 0)
        os.fsync(self._lock_fd)

    def _enable_stack_dumps(self) -> bool:
        """C-3.6: SIGUSR1 writes every thread's Python stack to daemon.log.

        `faulthandler` writes from the signal handler itself, so the dump
        arrives even when every Python thread is stuck behind a lock, the GIL
        or a pool (`subfleet daemon stacks` sends the signal). Registered as
        soon as the log is open, and before `daemon.lock` says so: SIGUSR1's
        default action is to end the process. False when the signal could not
        be made safe: the lock must then not say `stack_dumps`.
        """
        self._dumps = _DumpsState(self.root / "daemon.lock")
        self._dumps_token = next(_DUMPS_TOKENS)
        stream = self._log_handler.stream
        stream.flush()
        with _DUMPS_LOCK:
            return self._take_sigusr1(stream)

    def _take_sigusr1(self, stream) -> bool:
        """`_enable_stack_dumps`, under `_DUMPS_LOCK`: take SIGUSR1 and join
        `_ADVERTISED`, so no daemon handing the signal on in between finds this
        one missing and lets it go (review of 1efa0ef, P3)."""
        global _STACK_DUMPS
        if threading.current_thread() is not threading.main_thread() and callable(
                signal.getsignal(signal.SIGUSR1)):
            # The host has a Python SIGUSR1 handler of its own. Python's exit resets
            # it to the default action before faulthandler lets the signal go, and
            # off the main thread nothing can put SIG_IGN in Python's table in its
            # place: a `daemon stacks` during that exit would end the host (review
            # of 78bfbf0, P2). So this daemon does not dump stacks.
            self.log.warning("SIGUSR1 has a Python handler of the host's own, and this daemon was built "
                             "off the main thread: it does not dump stacks on SIGUSR1")
            return False
        if not _ADVERTISED:
            # Taking the signal fresh. A daemon built earlier in this process
            # (tests build several) may hold the registration still, and
            # `faulthandler.register` over a live one only changes the file: it
            # would not put its handler back over the SIG_IGN below, and the
            # signal would be ignored while the lock says `stack_dumps` (review
            # of 78a8476). Let it go first (a no-op if none).
            faulthandler.unregister(signal.SIGUSR1)
            _HELD_STREAMS.clear()
            # What faulthandler saves now is what it puts back whenever it lets
            # the signal go: at the last close, on either thread, and as the
            # interpreter exits. Ignored, so a SIGUSR1 that races any of those
            # ends nothing (review of 2300b43, P2: the default action, saved
            # beneath a daemon built off the main thread, ended the host). A
            # child started while the handler is in place gets the default
            # action back at exec, as with any handler; one started after the
            # last close inherits SIGUSR1 ignored.
            try:
                _ignore_sigusr1()
            except (OSError, AttributeError) as exc:
                # Registered over the default action, faulthandler would put that
                # back at the last close or at exit (review of 78bfbf0, P2).
                self.log.warning("could not set SIGUSR1 to be ignored beneath its handler (%s): "
                                 "this daemon does not dump stacks on SIGUSR1", exc)
                return False
        # Joined, then settled: SIGUSR1 goes to this daemon's stream, replaced in
        # place while another daemon's lock says `stack_dumps`, never let go first
        # (review r3, P1). A take that raises (a KeyboardInterrupt anywhere here)
        # leaves and settles again (reviews of 67b9adc and 6506619, P2).
        try:
            _ADVERTISED[self._dumps_token] = (weakref.ref(self), stream, self._dumps)
            _settle_sigusr1()
        except BaseException:
            _ADVERTISED.pop(self._dumps_token, None)
            _settle_sigusr1()
            raise
        return True

    def _disable_stack_dumps(self) -> None:
        """C-3.6: `daemon.lock` stops saying `stack_dumps`, then this daemon leaves
        `_ADVERTISED` and SIGUSR1 is settled (`_settle_sigusr1`), all before its log
        may close (`_may_close_log`), so no dump is written to a closed or reused
        descriptor. If the lock may still say so (read back after the rewrite,
        whatever cut it short: EIO, ENOSPC, or an exception landing in it), this
        daemon stays in `_ADVERTISED`, its stream kept open there, until its lock
        read back by path stops saying so, and SIGUSR1 is left as it is: `daemon stacks` against this root still reads a
        living pid and the flag (reviews of 02c6320, eac0706, 4fc5b49 and 9a514a7).
        Holder or not, the same rule. A daemon that never came to write the flag,
        or whose lock reads back without it, leaves (reviews of 3cac1e8 and
        6506619). Marked as leaving first, so a settle by any later take or leave
        drops it once its lock reads back without the flag, should an exception
        cut this close short before it leaves (review of 674b99b)."""
        token = self.__dict__.get("_dumps_token")
        if (state := self.__dict__.get("_dumps")) is not None:
            state.leaving = True
        try:
            self._write_lock(stack_dumps=False)
        except BaseException as exc:
            if self._lock_may_still_advertise():
                if not isinstance(exc, OSError):
                    raise                                  # it stays; the interrupted close keeps the stream
                self.log.warning("daemon.lock could not drop stack_dumps (%s): SIGUSR1 may still dump "
                                 "into this log", exc)
                return                                     # it stays in `_ADVERTISED`, its stream kept
            self._leave_advertised(token)
            if not isinstance(exc, OSError):
                raise
            return
        self._leave_advertised(token)

    def _lock_may_still_advertise(self) -> bool:
        """C-3.6: whether `daemon.lock` may say `stack_dumps` now (`_lock_may_say_stack_dumps`)."""
        return _lock_may_say_stack_dumps(self.__dict__.get("_dumps"))

    def _leave_advertised(self, token) -> None:
        """C-3.6: leave `_ADVERTISED` and settle SIGUSR1, one step under
        `_DUMPS_LOCK`. The settle runs whoever holds the signal, and repairs what
        an earlier exception left (reviews of 9a514a7 and 674b99b)."""
        with _DUMPS_LOCK:
            try:
                _ADVERTISED.pop(token, None)
            finally:
                _settle_sigusr1()

    def _may_close_log(self) -> bool:
        """C-3.6: whether this daemon's log stream may close: no hand-off can pick it
        (it is not in `_ADVERTISED`) and faulthandler cannot hold it (it is not in
        `_HELD_STREAMS`, after a settle that moves the signal off it if it is). A
        stream that may not close stays open while either may still hold it,
        which costs a descriptor, never a dump into a closed or reused one
        (reviews of 674b99b and 65dcb6d)."""
        stream = self._log_handler.stream
        with _DUMPS_LOCK:
            if self.__dict__.get("_dumps_token", object()) in _ADVERTISED:
                return False
            if stream in _HELD_STREAMS:
                _settle_sigusr1()
            return stream not in _HELD_STREAMS

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.on_stop:
            try:
                self.on_stop()        # C-5.8a, before anything can wait
            except Exception as exc:  # noqa: BLE001 - the drain below must still run
                self.log.error("stop bound not armed: %s", type(exc).__name__)
        self.stopping.set()
        self.timers.cancel.set()
        self._notify()
        # C-15.5: every waiter wakes now, sees the daemon stopping and answers
        # `{"timeout": true}` while its connection is still open; the answers get
        # up to 2 s to be sent before the connections are shut down.
        self.wait_hub.stop()
        with self._waits_answered:
            self._waits_answered.wait_for(lambda: self._waits_answering == 0, timeout=2)
        if self._socket:
            self._socket.close()
        if self._control_thread and threading.current_thread() != self._control_thread:
            self._control_thread.join(timeout=2)
        with self._connection_lock:
            for conn in self._connections:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.timers.stop()
        # C-16.7: a reader returns as soon as its socket is shut down. The wait
        # is bounded all the same; the C-5.8a bound covers the rest of the stop.
        with self._connection_lock:
            readers = list(self._readers)
        joined_by = time.monotonic() + READER_JOIN_S
        for reader in readers:
            reader.join(max(0.0, joined_by - time.monotonic()))
        self.conversations.close()
        for pool in (self.requests, self.lookups, self.waiters, self.workers):
            pool.shutdown(wait=True, cancel_futures=True)
        self.lock_watch.stop()
        with self._connection_lock:
            # A reader that has not returned by now closes nothing, so its
            # connection is closed here (C-16.7); an embedded daemon would keep it.
            for conn in self._connections:
                conn.close()
            self._connections.clear()
        self.store.close()
        (self.root / "daemon.sock").unlink(missing_ok=True)
        # While the lock is still this daemon's: the flag goes, then the handler.
        self._disable_stack_dumps()
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        self._lock_finalizer()
        self.log.removeHandler(self._log_handler)
        if self._may_close_log():
            self._log_handler.stream.close()   # else faulthandler may hold it, or a hand-off pick it


@dataclasses.dataclass
class _DumpsState:
    """C-3.6: what a settle needs to know of a member of `_ADVERTISED`, kept in its
    entry and shared with the daemon, so a member whose daemon object has been
    collected can still be judged (review of 8ebe6b6, P3)."""
    lock_path: Path
    leaving: bool = False                  # it began to rewrite its lock without the flag
    may_advertise: bool = False            # it came to write the flag (set just before that write)


def _lock_may_say_stack_dumps(state: _DumpsState | None) -> bool:
    """C-3.6: whether a member's `daemon.lock` may say `stack_dumps` now: it came to
    write the flag (`may_advertise`), and reading it back does not show it without,
    read by its path as `daemon stacks` reads it (a record that does not parse says
    nothing; a later daemon's record there is what `daemon stacks` would act on). A
    read that fails cannot tell, so it may. By path, not by descriptor: a settle
    asks of members whose close has ended, whose descriptor number another file may
    have reused."""
    if state is None or not state.may_advertise:
        return False
    try:
        raw = state.lock_path.read_bytes()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        record = json.loads(raw)
    except ValueError:
        return False
    return isinstance(record, dict) and record.get("stack_dumps") is True


#: C-3.6: the daemon whose log SIGUSR1 dumps into (one per process in service;
#: a test process may build many).
_STACK_DUMPS: weakref.ref | None = None
#: C-3.6: every daemon in this process whose `daemon.lock` says `stack_dumps`, in
#: the order they took the signal: token -> (a weak reference to it, the log stream a dump
#: for it goes to, its `_DumpsState`). One leaves only once its lock has stopped
#: saying so; one whose lock cannot be rewritten stays, holding its stream open,
#: until its lock, read back by path, stops saying so. While any stands,
#: SIGUSR1 keeps a handler: `daemon stacks` against that root reads a living pid
#: and the flag and sends the signal, which with no handler would be ignored and
#: dump nothing (and, before review of 2300b43 put SIG_IGN beneath every fresh
#: registration, would end a host built off the main thread). A dump goes to the
#: stream of the one registered last; for the others it lands in that log.
_ADVERTISED: dict[int, tuple[weakref.ref, Any]] = {}
_DUMPS_TOKENS = itertools.count()
#: Taking SIGUSR1 with joining `_ADVERTISED`, and leaving it with handing the
#: signal on, are each one step under this lock (review of 1efa0ef, P3). The
#: signal handler itself never takes it: faulthandler runs in C.
_DUMPS_LOCK = threading.RLock()
#: C-3.6: every log stream faulthandler may hold for SIGUSR1. A stream joins before
#: it is registered and the others leave only once that registration has
#: returned, and all leave only once the handler has gone, so wherever an
#: exception lands this holds at least the stream faulthandler holds. No stream
#: in it closes (`Daemon._may_close_log`). Changed only under `_DUMPS_LOCK`
#: (review of 674b99b: ordering alone left one window or another open).
_HELD_STREAMS: set[Any] = set()


def _settle_sigusr1() -> None:
    """C-3.6, under `_DUMPS_LOCK`: make SIGUSR1's registration match `_ADVERTISED`.

    Members that began to leave, or whose daemon object has been collected, and
    whose lock reads back without the flag go first (review of 8ebe6b6, P3: a
    collected member could never be judged). Then the signal dumps into the
    stream of the member that took it last, or, when none is left, its handler
    goes, back to the SIG_IGN beneath it. Idempotent, and run by every take and every leave, whoever holds the
    signal: whatever an exception cut short in an earlier one, the next repairs
    (reviews of eac0706, 4fc5b49, 9a514a7 and 674b99b)."""
    global _STACK_DUMPS
    for token, (ref, _, state) in list(_ADVERTISED.items()):
        if (state.leaving or ref() is None) and not _lock_may_say_stack_dumps(state):
            _ADVERTISED.pop(token, None)
    if _ADVERTISED:
        holder, stream, _ = next(reversed(_ADVERTISED.values()))
        _STACK_DUMPS = holder
        _HELD_STREAMS.add(stream)
        faulthandler.register(signal.SIGUSR1, file=stream, all_threads=True, chain=False)
        _HELD_STREAMS.intersection_update((stream,))       # faulthandler let the others go
    else:
        _STACK_DUMPS = None
        faulthandler.unregister(signal.SIGUSR1)
        _HELD_STREAMS.clear()


def _ignore_sigusr1() -> None:
    """C-3.6: set SIGUSR1 to be ignored, from any thread. `signal.signal` works
    only on the main thread; off it, libc's `signal()` sets the same
    process-wide action (Python's own table then still says what it said)."""
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGUSR1, signal.SIG_IGN)
        return
    libc = ctypes.CDLL(None, use_errno=True)              # this process's own symbols: no ldconfig
    libc.signal.restype = ctypes.c_void_p
    libc.signal.argtypes = (ctypes.c_int, ctypes.c_void_p)
    if libc.signal(signal.SIGUSR1, 1) == ctypes.c_void_p(-1).value:   # SIG_IGN, SIG_ERR
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def log_open_file_limit(log: logging.Logger, before: int, after: int, hard: int) -> None:
    """C-16.6: say what raising the open-file limit at start achieved."""
    if after != before:
        log.info("open-file limit raised from %s to %s at start", before, after)
    elif after != resource.RLIM_INFINITY and after < descriptors.OPEN_FILES_WANTED:
        log.warning("open-file limit left at %s: the hard limit (%s) or the kernel allows no more",
                    after, descriptors.limit_for_display(hard) or "unlimited")


def _keychain_read(argv) -> bool:
    """Whether `argv` is one of the two reads `credentials.keychain_command` builds:
    `<agent-secret> get <reference>` or `security find-generic-password -s <reference> -w`."""
    argv = list(argv)
    return len(argv) >= 2 and ((argv[1] == "get" and len(argv) == 3) or argv[1] == "find-generic-password")


def enrollment_runner(original: Callable[..., Any], turn: Callable[..., Any]) -> Callable[..., Any]:
    """C-10.2: the adapter's runner during a re-enrollment. The keychain reads that
    resolve a keychain-token credential and its plan stay with `original`; every other
    call, the login turn above all, runs through the enrollment fence
    (`Daemon._enrollment_turn`). Routing every call through the fence made each
    keychain-token re-enrollment raise before its turn ("_enrollment_turn() missing 2
    required keyword-only arguments: 'cwd' and 'env'", 2026-09-27), so a lapsed
    account could never be brought back. The fence is the default, so a call this does
    not recognise is contained (or refused by the fence's signature), never run bare."""
    def runner(argv, **kwargs):
        if _keychain_read(argv):
            return original(argv, **kwargs)
        return turn(argv, **kwargs)
    return runner


def watch_stop(stopping: threading.Event, grace_s: float, log_path: Path) -> Callable[[], bool]:
    """C-5.8a: prepare the daemon's process-local shutdown bound at startup.

    `close()` keeps `daemon.lock` until its pools drain. If a worker cannot
    return, ending the process releases the flock without allowing two store
    writers. No guardian is signalled; interval timers are not inherited by
    forked children, so a guardian launched after arming is unaffected too.

    Call this on the main thread, before installing the stop handlers. It
    reserves SIGALRM with its default (terminate) action and opens the log
    descriptor before a stop can exhaust descriptors. The returned `arm`
    installs ITIMER_REAL for grace plus STOP_DUMP_MARGIN_S, then asks
    faulthandler to dump at the grace and exit 1. The kernel backstop needs
    neither the GIL nor a thread, and survives a failed or cancelled dump
    timer. A working dump normally wins; a slow or blocked dump may be cut
    short by SIGALRM. Faulthandler includes at most 100 threads, newest first, without
    lock-ownership metadata. Even the stopping line can block or be absent.

    The one-time claim never waits. An interrupted arming call retains the
    claim, and a nested signal handler returns without entering Event.set
    until that call finishes. No overlapping invocation replaces either
    timer. A watcher also arms when something else sets `stopping`; the bound
    itself does not depend on that event or watcher making progress.

    `close()`, the end of serving, and handled SIGTERM/SIGINT call `arm` before
    setting `stopping`. A Python signal handler cannot begin while another
    thread keeps the GIL indefinitely. Only stops initiated by launchd or
    `subfleet daemon stop` have those callers' external SIGKILL backstops;
    direct signals do not. Clean process exit discards both timers.
    """
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
    claimed = threading.Lock()
    complete = False

    def say(text: str) -> None:
        with contextlib.suppress(OSError):
            os.write(fd, f"{utcnow()} {text} (C-5.8a)\n".encode())

    def arm() -> bool:
        """Claim once and install the kernel bound before any dump/log work.

        False means an interrupted/concurrent caller is still arming: a
        signal handler must return without setting the event so the owner
        can resume. True means arming completed. This call never waits on
        the claim or starts a Python thread. Kernel timer failure exits
        immediately rather than leave a claimed but unbounded stop.
        """
        nonlocal complete
        if not claimed.acquire(blocking=False):
            return complete
        try:
            signal.setitimer(signal.ITIMER_REAL, grace_s + STOP_DUMP_MARGIN_S)
        except Exception:  # noqa: BLE001 - cannot continue without a process bound
            os._exit(1)
        try:
            # Both timers precede the log write, which gives up the GIL.
            try:
                faulthandler.dump_traceback_later(grace_s, exit=True, file=fd)
                then = "the stacks of its threads follow and it exits 1"
            except Exception as exc:  # noqa: BLE001 - e.g. no thread for the watchdog
                then = (f"SIGALRM ends it in {grace_s + STOP_DUMP_MARGIN_S:g} s "
                        f"without a stack dump (faulthandler: {type(exc).__name__})")
            say(f"stopping: if this process is still running in {grace_s:g} s, {then}")
        finally:
            complete = True
        return True

    def watch() -> None:
        stopping.wait()
        arm()

    threading.Thread(target=watch, name="subfleet-stop-watch", daemon=True).start()
    return arm


def stop_request(stopping: threading.Event, arm: Callable[[], bool | None]) -> Callable[..., None]:
    """C-5.8a: arm before setting `stopping` in the SIGTERM/SIGINT handler.

    If interrupted arming is still in progress, return without taking the
    event's lock: the outer caller must resume to install the bound. Once
    arming finishes, skip Event.set when its flag is already true. A signal
    nested inside set before that flag flips can still deadlock, but by then
    the kernel timer is installed, even if faulthandler failed. The bound
    starts when this handler runs, not when an unhandled signal was sent.
    """
    def stop(*_: object) -> None:
        if arm() is False:
            return
        if not stopping.is_set():
            stopping.set()
    return stop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="subfleet supervised daemon")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--state-root", default=os.environ.get("SUBFLEET_HOME", "~/.subfleet"))
    args = parser.parse_args(argv)
    limits = descriptors.raise_open_file_limit()
    try:
        daemon = Daemon(args.state_root)
    except DaemonUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 69
    log_open_file_limit(daemon.log, *limits)
    daemon.log.info("stack dumps: `kill -USR1 %d` (or `subfleet daemon stacks`) writes every "
                    "thread's Python stack to this log", os.getpid())
    arm = watch_stop(daemon.stopping, daemon.stop_grace_s, daemon.root / "daemon.log")
    daemon.on_stop = arm
    stop = stop_request(daemon.stopping, arm)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    try:
        daemon.serve_forever()
    finally:
        # However `serve_forever` ended, the process is on its way out, so the
        # C-5.8a bound starts here if nothing started it before.
        stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
