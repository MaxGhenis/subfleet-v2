"""Dormant sessions: killed mid-turn, their process gone, nothing restarting them.

A Claude desktop app relaunch or account switch kills every running Code
session's process, and the app restarts only some of them. The ones it restarts
wake through `nudge` (their SessionStart hook, or `tickle`), because a live
process has an inbox. The rest stay dead however long it has been. A scan on
2026-09-27 found 59 such sessions, killed between 38 minutes and 46 hours
earlier across six restart events. They included a sidebar-mirror chip dead for
19 hours and the owner of a PR that blocked a stacked PR. The cold scope did not
reach them. Its liveness came from the registry alone, it capped age at two
hours, it filtered neither archived nor scheduled sessions, and its one
continuation, a headless revive job, refuses a cwd that is not a committed
repository.

This module is the cold scope's detector and waker (C-23.56 to C-23.60):

* `classify` maps every transcript tail to exactly one of `completed`,
  `interrupted` or `active` (C-23.56). A tail that stops mid-turn is
  `interrupted` only when its session's process is known to be dead AND the
  transcript, with its side directory, has been quiet for the whole window.
  Mid-turn means a tool result the model never continued from, a tool call
  with no result, or an unanswered prompt. Any other mid-turn tail is `active`,
  because a process inside a long tool call writes nothing to its main
  transcript.
* `liveness` is tri-state (C-23.57). A session is `alive` when a registry row
  with its id belongs to a running process (the pid is live and, when the row
  records its start, started then), or when a running process names the
  session after `--resume`, `-r` or `--session-id`. The desktop app's
  `claude --resume=<id>`, a tmux `claude -r <id>` and a Subfleet conversation
  turn all do. It is `unknown` when the process table cannot be read, when the
  registry directory cannot be listed, or when a registry file of a running pid
  cannot be read, because an inspection failure is never evidence of death
  (C-4.2). Otherwise it is `dead`.
* The desktop record decides eligibility (C-23.58). A session is never woken
  when its record is archived in the app's loaded folder, in the mirror's
  merged flags or, just before a wake, in any other copy; when any copy names a
  scheduled task; or when its model is not the one policy wakes
  (`claude-opus-5-5`). A missing cwd is reported, and nothing here creates a
  directory. A session whose last message is an unanswered wake or revive is
  not woken again without `--force`.
* `pace` spends a five-hour window slowly (C-23.59). A wake goes out only while
  the window's usage stays at or under the elapsed share of the window plus ten
  points, and under seventy. That usage includes one point for each wake still
  running and each wake already sent in this batch. At most six go out per
  batch.
* `wake_pass` continues each chosen session, oldest interruption first
  (C-23.60). Subfleet can do it itself, as a Subfleet conversation: the
  daemon's `conversation.open` of the native session, then one
  `message.submit`, paced against the lane the turn would run on. Or it can
  plan the wakes for an agent that can call the desktop app's session API,
  paced against the window that agent reads. A conversation wake makes the
  conversation the session's one writer (C-26.13), so the kit never nudges,
  revives or wakes it again, and a wake cannot loop.

Nothing here writes Claude Code's or the desktop app's files. The reservation
that makes a wake happen once per interruption point is a daemon record (the
`sessions` op's `nudged`, kind `wake`), taken before anything is sent.
"""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import registry, transcripts
from .transcripts import WAKE_MARKER, TurnState

# --- the three verdicts (C-23.56) ---------------------------------------------

VERDICTS = ("completed", "interrupted", "active")
LIVENESS = ("alive", "dead", "unknown")

#: `turn_state` states whose last turn was left unfinished. `tickled` is here
#: because a live session's nudge that its process died before answering is an
#: unanswered prompt like any other; `admits` holds back the one kind that must
#: not be answered by another wake (an unanswered wake or revive).
MID_TURN = frozenset({"interrupted", "tickled"})


@dataclass(frozen=True)
class Verdict:
    """How one session's last turn stands. `state` is one of `VERDICTS`."""

    state: str
    reason: str
    shape: str                      # the `turn_state` state underneath
    liveness: str
    quiet_s: float | None

    def to_dict(self) -> dict[str, Any]:
        quiet = None if self.quiet_s is None or not math.isfinite(self.quiet_s) \
            else round(self.quiet_s)
        return {"state": self.state, "reason": self.reason, "shape": self.shape,
                "liveness": self.liveness, "quiet_s": quiet}


def classify(turn: TurnState, liveness: str, quiet_s: float | None, *,
             window_s: float) -> Verdict:
    """C-23.56: exactly one of `completed`, `interrupted`, `active`, for any input.

    A finished tail is `completed` whatever the process is doing. An unfinished
    one is `interrupted` only when the process is known dead and the transcript,
    with its side directory, has been quiet for `window_s`. Every other
    combination is `active`. That includes an unreadable process table, any
    liveness value but `dead`, and a quiet time that is unknown, not finite or
    short (a clock that moved back reads as negative).
    """
    shape = turn.state
    if shape not in MID_TURN:
        return Verdict("completed", turn.detail, shape, liveness, quiet_s)
    if liveness == "alive":
        return Verdict("active", "a process for this session is running", shape,
                       liveness, quiet_s)
    if liveness != "dead":
        return Verdict("active", "its process could not be judged dead, and an "
                       "inspection failure is not a death (C-4.2)", shape, liveness, quiet_s)
    finite = quiet_s is not None and math.isfinite(quiet_s)
    if not finite or not math.isfinite(window_s) or quiet_s < window_s:
        heard = f"{max(0, int(quiet_s))}s" if finite else "an unknown time"
        wait = f"{int(window_s)}s" if math.isfinite(window_s) else f"{window_s}s"
        return Verdict("active", f"last write {heard} ago; a wake waits for {wait} of quiet",
                       shape, liveness, quiet_s)
    return Verdict("interrupted", turn.detail, shape, liveness, quiet_s)


# --- liveness (C-23.57) ---------------------------------------------------------

_UUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
#: A session named on a command line: `--resume=<id>` (the desktop app),
#: `--resume <id>` (a Subfleet turn), `-r <id>` and `--session-id <id>`.
_NAMED = re.compile(r"(?:^|\s)(?:--resume|--session-id|-r)(?:=|\s+)[\"']?(" + _UUID + r")")
#: One read of every process: its start (to tell a registry row's process from
#: a later one at the same pid) and its command line, never its environment
#: (C-5.5). `lstart` is five words under `procs._read`'s `LC_ALL=C` and `TZ=UTC`.
PROCESS_ARGV = ["/bin/ps", "-axww", "-o", "pid=,stat=,lstart=,command="]


def _spaced(text: str) -> str:
    return " ".join(text.split())


@dataclass(frozen=True)
class Processes:
    """The live, non-zombie processes one `ps` read saw: pid -> start, and the
    session ids their command lines name (lower case). Commands are dropped."""

    starts: Mapping[int, str]
    named: frozenset[str]


def parse_processes(table: str) -> Processes:
    """`PROCESS_ARGV` output as `Processes`; `ValueError` for a row that is not one."""
    starts: dict[int, str] = {}
    named: set[str] = set()
    for row in table.splitlines():
        parts = row.split(None, 7)
        if not parts:
            continue
        if len(parts) < 7:
            raise ValueError(f"ps printed a row that is not a process: {row[:80]!r}")
        pid, stat = int(parts[0]), parts[1]
        if stat.startswith("Z"):
            continue
        starts[pid] = " ".join(parts[2:7])
        if len(parts) > 7:
            named.update(match.lower() for match in _NAMED.findall(" " + parts[7]))
    return Processes(starts=starts, named=frozenset(named))


def read_processes(read: Callable[[list[str]], str] | None = None, *,
                   own_pid: int | None = None) -> Processes | None:
    """One read of the process table, or None when `ps` cannot say (C-4.2).

    A table that does not list the process reading it is not a table of this
    machine's processes: `ps` that printed nothing (a sandbox, a visibility
    restriction) would otherwise read as every session dead."""
    from .. import procs
    reader = read or procs._read
    try:
        table = parse_processes(reader(PROCESS_ARGV))
    except (procs.InspectionError, OSError, ValueError):
        return None
    return table if (own_pid or os.getpid()) in table.starts else None


def liveness(session_id: str, *, reading: registry.Reading,
             processes: Processes | None) -> str:
    """C-23.57: `alive`, `dead` or `unknown` for one session id, in either case.

    A registry row is evidence of life only for the process that wrote it. When
    the row records its process's start and the live pid started at another
    time, the pid was reused and the row is stale. A row with no recorded start
    counts whenever its pid is live: a false `alive` only holds a wake back.
    """
    wanted = session_id.lower()
    for row in reading.rows:
        if row.session_id.lower() != wanted or not row.pid:
            continue
        if processes is None:
            if row.alive:
                return "alive"
            continue
        started = processes.starts.get(row.pid)
        if started is None:
            continue
        if row.proc_start and _spaced(row.proc_start) != _spaced(started):
            continue
        return "alive"
    if processes is None:
        return "unknown"
    if wanted in processes.named:
        return "alive"
    if reading.error or any(pid is None or pid in processes.starts for pid in reading.unreadable):
        return "unknown"
    return "dead"


# --- quiet (C-23.56) ------------------------------------------------------------

#: How much of a session's side directory (`<projects>/<slug>/<id>/`, which holds
#: subagent transcripts and saved tool results) one quiet reading may look at.
SIDE_ENTRY_LIMIT = 4000


def last_write(transcript: str | Path, *, after: float | None = None) -> float | None:
    """The newest mtime of the transcript and its side directory, or None.

    A workflow's agents write their own transcripts under the side directory
    while the main transcript stays still, so the main file's mtime alone would
    call a busy session quiet. Past `SIDE_ENTRY_LIMIT` entries, or when any of
    it cannot be read, the answer is None (unknown), which `classify` treats as
    not quiet. With `after`, the walk ends at the first write later than it:
    one is enough to say the session is not quiet."""
    path = Path(transcript)
    try:
        newest = path.stat().st_mtime
    except OSError:
        return None
    if after is not None and newest > after:
        return newest
    seen = 0
    stack = [path.with_suffix("")]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen > SIDE_ENTRY_LIMIT:
                        return None
                    try:
                        info = entry.stat(follow_symlinks=False)
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        return None
                    newest = max(newest, info.st_mtime)
                    if after is not None and newest > after:
                        return newest
                    if is_dir:
                        stack.append(Path(entry.path))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            return None
    return newest


# --- the desktop record (C-23.58) -------------------------------------------------

RECORD_LIMIT = 1024 * 1024
FLAGS_LIMIT = 64 * 1024 * 1024


@dataclass(frozen=True)
class DesktopRecord:
    """A desktop session as the copies of its record that were read say it is.

    `archived` and `scheduled` are true when ANY copy read, or the mirror's
    merged flags, says so: an unarchive reaches the other copies on the mirror's
    next pass, while a wrong wake cannot be taken back. `model`, `cwd` and
    `title` come from the copy active most recently (the mirror's `_rank`)."""

    local_id: str
    cli_session_id: str | None
    title: str | None = None
    model: str | None = None
    cwd: str | None = None
    archived: bool = False
    scheduled: bool = False
    last_activity: float | None = None
    copies: int = 1
    unreadable: int = 0
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"local_id": self.local_id, "cli_session_id": self.cli_session_id,
                "title": self.title, "model": self.model, "cwd": self.cwd,
                "archived": self.archived, "scheduled": self.scheduled,
                "last_activity": self.last_activity, "copies": self.copies,
                "unreadable": self.unreadable, "path": self.path}


def _rank(data: Mapping[str, Any]) -> float:
    """The mirror's canonical-copy order (`mirror._rank`), as a number."""
    from .mirror import _rank as mirror_rank
    value = mirror_rank(dict(data))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.0
    return float(value)


def _text(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value.strip() else None


def merge_copies(local_id: str, copies: Sequence[tuple[str, Any]], *,
                 unreadable: int = 0, flags: Mapping[str, Any] | None = None
                 ) -> DesktopRecord | None:
    """One record from the copies of `local_id` that could be read (C-23.58)."""
    readable = [(path, data) for path, data in copies if isinstance(data, Mapping)]
    if not readable:
        return None
    path, newest = max(readable, key=lambda item: (_rank(item[1]), item[0]))
    merged_flag = isinstance(flags, Mapping) and flags.get("isArchived") is True
    return DesktopRecord(
        local_id=local_id, cli_session_id=_text(newest, "cliSessionId"),
        title=_text(newest, "title"), model=_text(newest, "model"),
        cwd=_text(newest, "cwd"),
        archived=merged_flag or any(data.get("isArchived") is True for _path, data in readable),
        scheduled=any(bool(data.get("scheduledTaskId")) for _path, data in readable),
        last_activity=_rank(newest) or None, copies=len(readable),
        unreadable=unreadable, path=path)


def _read_record(path: Path) -> dict[str, Any] | None:
    from ..state_files import read_state
    try:
        data = json.loads(read_state(path, limit=RECORD_LIMIT).decode("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def store_dir() -> Path:
    from .revive import session_store_dir
    return session_store_dir()


def loaded_folder(store: Path) -> Path | None:
    """The account folder the running app lists (its log's latest load), or,
    when the log cannot say, the folder written most recently."""
    try:
        from .desktop import DesktopLog
        state = DesktopLog(store=store).poll()
        if state.load is not None and not state.load.missing:
            folder = store / state.load.account / state.load.org
            if folder.is_dir():
                return folder
    except Exception:                                   # noqa: BLE001 - the log is a diagnostic
        pass
    newest: tuple[float, Path] | None = None
    try:
        for account in os.scandir(store):
            if not account.is_dir():
                continue
            for org in os.scandir(account.path):
                if org.is_dir():
                    stamp = org.stat().st_mtime
                    if newest is None or stamp > newest[0]:
                        newest = (stamp, Path(org.path))
    except OSError:
        return None
    return newest[1] if newest else None


def folder_records(folder: Path) -> dict[str, tuple[str, dict[str, Any]]]:
    """`cliSessionId` (lower case) -> (local id, record) for one account folder.

    Every record is read. A copy's mtime cannot filter them: the mirror writes
    the copies it syncs keeping their old mtime, and a session that ran under
    another account keeps an older copy here."""
    found: dict[str, tuple[str, dict[str, Any]]] = {}
    try:
        with os.scandir(folder) as entries:
            names = sorted(entry.name for entry in entries
                           if entry.name.startswith("local_") and entry.name.endswith(".json"))
    except OSError:
        return found
    for name in names:
        data = _read_record(folder / name)
        if data is None:
            continue
        cli = _text(data, "cliSessionId")
        if cli:
            previous = found.get(cli.lower())
            if previous is None or _rank(data) > _rank(previous[1]):
                found[cli.lower()] = (name[:-len(".json")], data)
    return found


def mirror_flags(state_root: Path | None) -> dict[str, Mapping[str, Any]] | None:
    """The mirror's merged flags by `cliSessionId` (lower case): `{}` when there
    is no flags file, None when it cannot be read."""
    if state_root is None:
        return {}
    from ..state_files import read_state
    from .mirror import FLAGS_NAME
    path = Path(state_root) / "sessions" / FLAGS_NAME
    try:
        data = json.loads(read_state(path, limit=FLAGS_LIMIT).decode("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {key.lower(): value for key, value in data.items()
            if isinstance(key, str) and isinstance(value, Mapping)}


def all_copies(store: Path, local_id: str, *,
               flags: Mapping[str, Any] | None = None) -> DesktopRecord | None:
    """Every account folder's copy of `local_id`, merged (C-23.58): the strict
    check made just before a wake."""
    copies: list[tuple[str, dict[str, Any]]] = []
    unreadable = 0
    try:
        accounts = [entry.path for entry in os.scandir(store) if entry.is_dir()]
    except OSError:
        return None
    for account in accounts:
        try:
            orgs = [entry.path for entry in os.scandir(account) if entry.is_dir()]
        except OSError:
            unreadable += 1
            continue
        for org in orgs:
            path = Path(org) / f"{local_id}.json"
            if not path.exists():
                continue
            data = _read_record(path)
            if data is None:
                unreadable += 1
            else:
                copies.append((str(path), data))
    return merge_copies(local_id, copies, unreadable=unreadable, flags=flags)


# --- pacing (C-23.59) -------------------------------------------------------------

@dataclass(frozen=True)
class Window:
    """One five-hour usage reading: what pacing spends against."""

    percent_used: float | None
    resets_at: datetime | None
    as_of: datetime | None = None
    label: str = "unknown"
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"percent_used": self.percent_used,
                "resets_at": _iso(self.resets_at), "as_of": _iso(self.as_of),
                "label": self.label, "source": self.source}


@dataclass(frozen=True)
class Pace:
    """How many wakes may go out now, and why."""

    allowed: int
    reason: str
    elapsed_pct: float | None = None
    percent_used: float | None = None
    pending: int = 0
    window: Window | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason,
                "elapsed_pct": None if self.elapsed_pct is None else round(self.elapsed_pct, 1),
                "percent_used": self.percent_used, "pending": self.pending,
                "window": self.window.to_dict() if self.window else None}


def _sessions_settings(policy: Mapping[str, Any]) -> Mapping[str, Any]:
    settings = policy.get("sessions", {}) if isinstance(policy, Mapping) else {}
    return settings if isinstance(settings, Mapping) else {}


@dataclass(frozen=True)
class PaceRule:
    """C-23.59's numbers, all of them policy (`sessions.wake_*`)."""

    window_s: float = 5 * 3600
    headroom: float = 10.0
    ceiling: float = 70.0
    batch: int = 6
    cost: float = 1.0
    max_reading_age_s: float = 3600.0

    @classmethod
    def from_policy(cls, policy: Mapping[str, Any]) -> "PaceRule":
        settings = _sessions_settings(policy)
        return cls(window_s=float(settings.get("wake_window_h", 5)) * 3600,
                   headroom=float(settings.get("wake_headroom_pct", 10)),
                   ceiling=float(settings.get("wake_ceiling_pct", 70)),
                   batch=int(settings.get("wake_batch", 6)),
                   cost=float(settings.get("wake_cost_pct", 1)),
                   max_reading_age_s=float(settings.get("wake_reading_max_age_s", 3600)))


#: The labels C-9.1 lets a percentage be decided on.
TRUSTED_LABELS = frozenset({"provider", "stale-provider"})
#: The most one batch ever sends, whatever policy says.
BATCH_LIMIT = 64
#: How far in the future a reading's time may be before it is not believed.
CLOCK_SKEW_S = 300.0


def elapsed_pct(resets_at: datetime, now: datetime, window_s: float) -> float:
    """The share of the window already gone, in percent, clamped to [0, 100]."""
    start = resets_at - timedelta(seconds=window_s)
    share = (now - start).total_seconds() / window_s * 100.0
    return min(100.0, max(0.0, share))


def distrust(window: Window | None, *, now: datetime, rule: PaceRule = PaceRule()) -> str | None:
    """Why a five-hour reading cannot be paced against, or None when it can.

    No reading, one that is not a percentage, one not labelled `provider` or
    `stale-provider` (C-9.1), one that does not say when it was taken, one dated
    in the future, one older than the limit, and one whose window has already
    reset: in each case what the window holds now is unknown."""
    if window is None or window.percent_used is None or window.resets_at is None:
        return "no five-hour reading to pace against"
    try:
        used = float(window.percent_used)
    except (TypeError, ValueError):
        used = math.nan
    if isinstance(window.percent_used, bool) or not math.isfinite(used) or used < 0:
        return f"the five-hour reading is not a percentage ({window.percent_used!r})"
    if window.label not in TRUSTED_LABELS:
        return f"the five-hour reading is {window.label!r}, not a provider reading (C-9.1)"
    if window.as_of is None:
        return "the five-hour reading does not say when it was taken"
    age = (now - window.as_of).total_seconds()
    if age < -CLOCK_SKEW_S:
        return f"the five-hour reading is dated {int(-age)}s in the future"
    if age > rule.max_reading_age_s:
        return f"the five-hour reading is {int(age)}s old (limit {int(rule.max_reading_age_s)}s)"
    if window.resets_at <= now:
        return ("the reading's five-hour window has already reset; waiting for a reading "
                "of the new one")
    if not math.isfinite(rule.window_s) or rule.window_s <= 0:
        return "no plan window to pace against (sessions.wake_window_h is 0)"
    return None


def pace_lanes(windows: Sequence[Window], *, now: datetime, rule: PaceRule = PaceRule(),
               pending: int = 0) -> Pace:
    """C-23.59 across every lane a conversation turn may land on.

    A Claude turn is not pinned to a lane: admission places each one, and a turn
    that cannot have the lane ranked first goes to the next. So the batch is
    paced against the tightest lane: the first (the lane a turn would run on
    now) must have a reading to pace against, and every other candidate that
    has a current reading can only lower the count. A candidate with no current
    reading cannot be measured and is named in the reason."""
    if not windows:
        return Pace(0, "no lane would take a turn now", pending=pending)
    chosen = pace(windows[0], now=now, rule=rule, pending=pending)
    if distrust(windows[0], now=now, rule=rule) is not None:
        return chosen
    tightest, unmeasured = chosen, []
    for window in windows[1:]:
        if distrust(window, now=now, rule=rule) is not None:
            unmeasured.append(window.source or "a lane")
            continue
        other = pace(window, now=now, rule=rule, pending=pending)
        if other.allowed < tightest.allowed:
            tightest = Pace(other.allowed, f"{other.reason}, on {window.source}, where a turn "
                            f"may land", elapsed_pct=other.elapsed_pct,
                            percent_used=other.percent_used, pending=pending, window=window)
    if unmeasured:
        tightest = Pace(tightest.allowed, f"{tightest.reason}; not measured: "
                        f"{', '.join(unmeasured)}", elapsed_pct=tightest.elapsed_pct,
                        percent_used=tightest.percent_used, pending=pending, window=tightest.window)
    return tightest


def pace(window: Window | None, *, now: datetime, rule: PaceRule = PaceRule(),
         pending: int = 0) -> Pace:
    """C-23.59: how many wakes may go out now.

    The answer is the largest count, up to the batch minus the `pending` wakes
    still running, such that every wake k (from 0) satisfies both
    `used + (pending + k) * cost <= elapsed + headroom` and
    `used + (pending + k) * cost < ceiling`. A running wake has not yet
    reported what it spent, because a turn's usage is read when it finishes.

    It fails closed, allowing nothing, when there is no reading, when the
    reading is not a provider one (C-9.1), when it is older than the limit, or
    when its window has already reset. In each of those cases what the window
    holds now is unknown.
    """
    pending = max(0, int(pending))
    refusal = distrust(window, now=now, rule=rule)
    if refusal is not None:
        used_value = window.percent_used if window is not None else None
        return Pace(0, refusal, pending=pending, window=window,
                    percent_used=used_value if isinstance(used_value, (int, float))
                    and not isinstance(used_value, bool) and math.isfinite(used_value) else None)
    assert window is not None and window.resets_at is not None
    used = float(window.percent_used)
    elapsed = elapsed_pct(window.resets_at, now, rule.window_s)
    room = max(0, min(int(rule.batch), BATCH_LIMIT) - pending)
    cost = max(0.0, float(rule.cost)) if math.isfinite(rule.cost) else 0.0
    allowed = 0
    while allowed < room:
        projected = used + (pending + allowed) * cost
        if not (projected <= elapsed + rule.headroom and projected < rule.ceiling):
            break
        allowed += 1
    limit = min(elapsed + rule.headroom, rule.ceiling)
    if allowed:
        reason = (f"{used:g}% used, {elapsed:.0f}% of the window elapsed, "
                  f"{pending} running: {allowed} of {room} this batch")
    elif room == 0:
        reason = f"{pending} wakes are still running (batch {rule.batch})"
    elif used + pending * cost >= rule.ceiling:
        reason = (f"{used:g}% used (+{pending * cost:g} running), at or over the "
                  f"{rule.ceiling:g}% ceiling")
    else:
        reason = (f"{used:g}% used (+{pending * cost:g} running), more than "
                  f"{elapsed:.0f}% elapsed + {rule.headroom:g} ({limit:.0f}%)")
    return Pace(allowed, reason, elapsed_pct=elapsed, percent_used=used, pending=pending,
                window=window)


def lane_windows(view: Mapping[str, Any], policy: Mapping[str, Any], *,
                 model: str, now: datetime) -> list[Window]:
    """The five-hour reading of every lane admission could give a conversation
    turn now, the lane it would pick first (C-23.59).

    A conversation wake runs as a turn job, and admission picks its lane
    (`scheduler.evaluate` with kind `turn`), falling to the next candidate when
    the first cannot take it. A turn never runs on the desktop login, so that
    is not the window it spends. `view` is the daemon's `daemon.status` answer,
    which is a capacity view. With no lane to take a turn, or no way to
    evaluate one, the one window returned has no reading, and the pace fails
    closed."""
    from .. import scheduler
    try:
        from ..conversations.service import policy_model
        short = policy_model(dict(policy), "claude", model)
    except Exception as exc:                            # noqa: BLE001 - reported, not raised
        return [Window(None, None, label="unknown", source=f"model {model!r} does not route: {exc}")]
    try:
        decision = scheduler.evaluate(policy, view, _turn_job(policy, short))
    except Exception as exc:                            # noqa: BLE001 - reported, not raised
        return [Window(None, None, label="unknown", source=f"routing could not be evaluated: {exc}")]
    lane = decision.chosen_lane
    if not lane:
        return [Window(None, None, label="unknown",
                       source=f"no lane would take a {short} turn now: {decision.reason}")]
    candidates: list[str] = []
    for evaluation in getattr(decision, "evaluations", ()) or ():
        if isinstance(evaluation, Mapping) and evaluation.get("model") == decision.chosen_model:
            candidates = [item for item in evaluation.get("candidates") or [] if isinstance(item, str)]
    return [_lane_reading(view, lane)] + [_lane_reading(view, other) for other in candidates
                                          if other != lane]


def lane_window(view: Mapping[str, Any], policy: Mapping[str, Any], *,
                model: str, now: datetime) -> Window:
    """The five-hour reading of the lane a conversation turn would run on now."""
    return lane_windows(view, policy, model=model, now=now)[0]


def _turn_job(policy: Mapping[str, Any], short: str) -> dict[str, Any]:
    return {"kind": "turn", "pinned_model": short, "sandbox": "workspace-write",
            "exclusions": (), "policy_hash": policy.get("_policy_hash", ""),
            "job_id": "wake-pacing", "workdir": str(Path.home())}


def _lane_reading(view: Mapping[str, Any], lane: str) -> Window:
    """A lane's newest five-hour account reading in `view`, as a `Window`."""
    best: Mapping[str, Any] | None = None
    for row in view.get("readings", ()) or ():
        if not isinstance(row, Mapping):
            continue
        if row.get("lane_id") == lane and row.get("scope") == "account" \
                and row.get("window") == "five_hour":
            if best is None or str(row.get("observed_at") or "") > str(best.get("observed_at") or ""):
                best = row
    if best is None:
        return Window(None, None, label="unknown", source=f"lane {lane}: no five-hour reading")
    utilization = best.get("utilization")
    percent = float(utilization) * 100 if isinstance(utilization, (int, float)) \
        and not isinstance(utilization, bool) else None
    return Window(percent, _instant(best.get("resets_at")), as_of=_instant(best.get("observed_at")),
                  label=str(best.get("label") or "unknown"),
                  source=f"lane {lane} ({best.get('source') or 'reading'})")


def window_from(percent: float | None, resets_at: Any, *, now: datetime,
                source: str) -> Window:
    """A reading an operator or agent supplied, such as the desktop app's
    `get_usage` five-hour row, taken as a provider reading as of now."""
    return Window(percent, _instant(resets_at), as_of=now, label="provider", source=source)


# --- candidates (C-23.56 to C-23.58) ---------------------------------------------

@dataclass
class Dormant:
    """One session the scan looked at, and what became of it."""

    session_id: str
    transcript: str | None = None
    turn: TurnState = field(default_factory=TurnState)
    verdict: Verdict | None = None
    record: DesktopRecord | None = None
    desktop: bool = False
    workspace: str | None = None
    eligible: bool = False
    reason: str = ""
    fix: str | None = None
    woken: bool = False
    planned: bool = False
    conversation_id: str | None = None
    message_id: str | None = None
    state: str | None = None

    @property
    def interrupted(self) -> bool:
        return bool(self.verdict and self.verdict.state == "interrupted")

    def to_dict(self) -> dict[str, Any]:
        record = self.record
        return {"session_id": self.session_id, "transcript": self.transcript,
                "desktop": self.desktop,
                "local_id": record.local_id if record else None,
                "title": record.title if record else None,
                "model": record.model if record else None,
                "cwd": record.cwd if record else None, "workspace": self.workspace,
                "verdict": self.verdict.to_dict() if self.verdict else None,
                "age_s": self.turn.age_s, "detail": self.turn.detail,
                "eligible": self.eligible, "reason": self.reason, "fix": self.fix,
                "woken": self.woken, "planned": self.planned,
                "conversation_id": self.conversation_id,
                "message_id": self.message_id, "message_state": self.state,
                "record": record.to_dict() if record else None}


@dataclass(frozen=True)
class Settings:
    """The scan's policy (`sessions.wake_*`)."""

    max_age_s: float = 48 * 3600
    quiet_s: float = 600.0
    model: str = "claude-opus-5-5"
    settle_s: float = 1800.0

    @classmethod
    def from_policy(cls, policy: Mapping[str, Any]) -> "Settings":
        settings = _sessions_settings(policy)
        return cls(max_age_s=float(settings.get("wake_max_age_h", 48)) * 3600,
                   quiet_s=float(settings.get("wake_quiet_s", 600)),
                   model=str(settings.get("wake_model", "claude-opus-5-5")),
                   settle_s=float(settings.get("wake_settle_min", 30)) * 60)


def project_names(cwd: str) -> list[str]:
    """The `~/.claude/projects` folder names Claude Code may give `cwd`: the
    mirror's rule (`mirror.slug`) and the conversation catalog's two
    (`catalog._project_names`), without repeats."""
    from .mirror import slug
    names = [slug(cwd), re.sub(r"[/._]", "-", cwd), re.sub(r"[^A-Za-z0-9]", "-", cwd)]
    return list(dict.fromkeys(names))


def record_transcript(data: Mapping[str, Any], *,
                      projects: Path | None = None) -> tuple[Path | None, float | None]:
    """The transcript a desktop record names, found where its cwd puts it
    (`projects/<slug>/<cliSessionId>.jsonl`, the mirror's `_openable` rule),
    with its mtime. The newest of the copies under `cwd` and `originCwd` is the
    live one. One stat per candidate, never a walk of every project folder
    (5,445 of them on the machine this was written on)."""
    identity = _text(data, "cliSessionId")
    if not identity:
        return None, None
    base = projects or transcripts.projects_dir()
    names = dict.fromkeys(name for key in ("cwd", "originCwd") if (cwd := _text(data, key))
                          for name in project_names(cwd))
    best: tuple[float, Path] | None = None
    for name in names:
        candidate = base / name / f"{identity}.jsonl"
        try:
            stamp = candidate.stat().st_mtime
        except OSError:
            continue
        if best is None or stamp > best[0]:
            best = (stamp, candidate)
    return (best[1], best[0]) if best else (None, None)


@dataclass
class Scan:
    """One pass over the sessions whose transcripts moved in the window."""

    rows: list[Dormant] = field(default_factory=list)
    folder: str | None = None
    process_table: bool = True
    flags_read: bool = True
    errors: list[str] = field(default_factory=list)
    #: The mirror's merged flags as the scan read them, so a pass reads them once.
    flags: Mapping[str, Mapping[str, Any]] = field(default_factory=dict, repr=False)

    @property
    def interrupted(self) -> list[Dormant]:
        return [row for row in self.rows if row.interrupted]

    @property
    def eligible(self) -> list[Dormant]:
        return [row for row in self.rows if row.eligible]

    def counts(self) -> dict[str, int]:
        counts = {state: 0 for state in VERDICTS}
        counts["fenced"] = 0
        for row in self.rows:
            if row.verdict is not None:
                counts[row.verdict.state] += 1
            elif row.reason.startswith("active"):
                counts["active"] += 1
            else:
                counts["fenced"] += 1
        counts["eligible"] = len(self.eligible)
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {"folder": self.folder, "process_table": self.process_table,
                "errors": list(self.errors), "counts": self.counts(),
                "sessions": [row.to_dict() for row in self.rows]}


def _wake_record(facts_for: Mapping[str, Any]) -> Mapping[str, Any] | None:
    last = facts_for.get("last_nudge") if isinstance(facts_for, Mapping) else None
    return last if isinstance(last, Mapping) and last.get("kind") == WAKE_KIND else None


def wake_key(turn: TurnState) -> str:
    """The dedupe key a wake is reserved under: its own namespace, so a nudge
    of a live process at the same point (which its death left unanswered) never
    holds the wake back (C-23.33's once per interruption point, per kind)."""
    return f"{WAKE_KIND}:{turn.dedupe_key}"


def scan(facts: Mapping[str, Any], policy: Mapping[str, Any], *,
         now: datetime | None = None, only: Sequence[str] = (),
         caller: str | None = None, force: bool = False,
         processes: Any = ..., reading: registry.Reading | None = None,
         store: Path | None = None, state_root: Path | None = None,
         flags: Mapping[str, Mapping[str, Any]] | None | object = ...) -> Scan:
    """Every session whose transcript moved in the window, judged (C-23.56-58).

    `facts` is the daemon's `sessions state` answer (or `facts.offline_state`):
    retirement flags, the last nudge, the lane runs the daemon launched
    (C-23.31) and its conversations' sessions (C-26.13). A daemon too old to
    list conversations raises `SessionsUnsupported`.

    The sessions are the app's loaded folder's records whose transcripts were
    written in the window (and any session named). The cheap gates run first:
    the fences, liveness (one `ps` read and one registry read) and quiet
    (stats). Only a session that is dead and quiet has its transcript tail read.
    """
    instant = now or datetime.now(timezone.utc)
    settings = Settings.from_policy(policy)
    result = Scan()
    conversations = registry.folded(registry.conversation_ids_of(facts))
    lanes = registry.folded(item for item in (facts.get("lane_sessions") or [])
                            if isinstance(item, str))
    by_id = {key.lower(): value for key, value in (facts.get("sessions") or {}).items()
             if isinstance(key, str) and isinstance(value, Mapping)}
    base = store or store_dir()
    folder = loaded_folder(base)
    result.folder = str(folder) if folder else None
    if folder is None:
        result.errors.append(f"no desktop session folder under {base}")
        return result
    records = folder_records(folder)
    if flags is ...:
        flags = mirror_flags(state_root)
    result.flags_read = flags is not None
    if flags is None:
        result.errors.append("the mirror's flags file could not be read: archive state "
                             "comes from the desktop records alone, and no wake is sent")
        flags = {}
    result.flags = flags
    wanted = [item.lower() for item in dict.fromkeys(only) if isinstance(item, str) and item]
    keys = wanted or sorted(records)
    table = read_processes() if processes is ... else processes
    registry_reading = reading if reading is not None else registry.read()
    result.process_table = table is not None
    if table is None:
        result.errors.append("the process table could not be read, so no session is "
                             "judged dead (C-4.2)")
    if registry_reading.error:
        result.errors.append(f"the session registry could not be listed ({registry_reading.error}), "
                             "so no session is judged dead (C-4.2)")
    cutoff = instant.timestamp() - settings.max_age_s
    for key in keys:
        local, data = records.get(key, (None, None))
        path, written_at = record_transcript(data) if data else (None, None)
        if path is None and wanted:
            path = transcripts.transcript_path(key)
            written_at = _mtime(path)
        if not wanted and (written_at is None or written_at < cutoff):
            continue                    # not active in the window: not the scan's business
        row = Dormant(session_id=key, transcript=str(path) if path else None, desktop=bool(local))
        result.rows.append(row)
        if caller and key == caller.lower():
            row.reason = "this session (a pass never wakes itself)"
            continue
        if key in conversations:
            row.reason, row.fix = (f"{registry.CONVERSATION_REASON} (C-26.13)",
                                   registry.CONVERSATION_FIX)
            continue
        if key in lanes:
            row.reason = "headless lane run — never woken (C-23.31)"
            continue
        retired = by_id.get(key, {}).get("retired")
        if retired:
            why = retired.get("reason") if isinstance(retired, Mapping) else None
            row.reason = f"retired by the operator ({why or 'no reason recorded'})"
            row.fix = f"subfleet sessions unretire {key}"
            continue
        if path is None:
            row.reason = "no transcript where its desktop record puts it"
            continue
        state = liveness(key, reading=registry_reading, processes=table)
        # The side directory is walked only for a dead session whose main
        # transcript is itself quiet: that walk is the scan's costly read.
        main_quiet = None if written_at is None else instant.timestamp() - written_at
        quick = state != "dead" or main_quiet is None or main_quiet < settings.quiet_s
        written = None if quick else last_write(path, after=instant.timestamp() - settings.quiet_s)
        quiet = main_quiet if quick else (None if written is None
                                          else instant.timestamp() - written)
        if state != "dead" or quiet is None or quiet < settings.quiet_s:
            # Only a dead, quiet session can be `interrupted`, so its tail is not
            # read; `classify` would say `active` if it stopped mid-turn.
            gate = classify(TurnState(state="interrupted", detail="not read"), state, quiet,
                            window_s=settings.quiet_s)
            row.reason = f"active or completed: {gate.reason}"
            continue
        row.turn = transcripts.turn_state(path, now=instant)
        row.verdict = classify(row.turn, state, quiet, window_s=settings.quiet_s)
        if not row.interrupted:
            row.reason = f"{row.verdict.state}: {row.verdict.reason}"
            continue
        if local is None:
            # Only a session the desktop store does not know can be a `claude -p`
            # run subfleet did not launch; its transcript's shape says so. A
            # desktop record says otherwise: a chip's first prompts arrive
            # through the SDK too (55 of the app's sessions on 2026-09-28).
            if registry.is_lane_run(key, transcript=path):
                row.verdict = None
                row.reason = "headless lane run — never woken (C-23.31)"
            else:
                row.reason = "no desktop record in the app's loaded folder"
            continue
        row.record = merge_copies(local, [(str(folder / f"{local}.json"), data)],
                                  flags=flags.get(key))
        row.eligible, row.reason, row.fix = admits(
            row, settings, wake=_wake_record(by_id.get(key, {})), force=force)
    return result


def _mtime(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def admits(row: Dormant, settings: Settings, *, wake: Mapping[str, Any] | None = None,
           force: bool = False) -> tuple[bool, str, str | None]:
    """C-23.58's filters, in an order that names something to act on first."""
    record = row.record
    if record is None:
        return False, "no desktop record", None
    if record.archived:
        return False, "archived in the desktop app", None
    if record.scheduled:
        return False, "a scheduled-task run", None
    if record.model != settings.model:
        return False, (f"model {record.model or 'unrecorded'}; only {settings.model} "
                       "sessions are woken"), "continue it by hand if it should go on"
    if row.turn.age_s is not None and row.turn.age_s > settings.max_age_s:
        return False, (f"interrupted {row.turn.age_s}s ago, older than the "
                       f"{int(settings.max_age_s)}s window"), None
    if not record.cwd:
        return False, "its desktop record names no cwd", None
    if not os.path.isdir(record.cwd):
        return False, f"its cwd {record.cwd} is missing (reported, not created)", \
            "restore the folder, or archive the session"
    again = f"subfleet sessions wake --force {row.session_id}"
    if row.turn.state == "tickled" and row.turn.nudge in ("wake", "revive") and not force:
        return False, f"{row.turn.detail}; it is not woken again unasked", again
    if wake is not None and wake.get("dedupe_key") == wake_key(row.turn) and not force:
        return False, (f"a {wake.get('transport') or ''} wake was sent at this interruption "
                       f"point at {wake.get('at') or 'an unrecorded time'}").replace("a  ", "a "), \
            f"if it never arrived: {again}"
    return True, f"interrupted: {row.turn.detail}", None


# --- waking (C-23.60) -------------------------------------------------------------

WAKE_KIND = "wake"
TRANSPORTS = ("conversation", "desktop")
CONVERSATIONS_CAPABILITY = "conversations.v1"


def wake_text(row: Dormant) -> str:
    """The one message a woken session receives."""
    detail = row.turn.detail or "its last turn was interrupted"
    return (
        f"{WAKE_MARKER} (a Claude app relaunch or account switch) and nothing "
        f"restarted it; its last turn was cut off — {detail}. Continue where you "
        "left off.\n"
        "Re-check live state first: `git status` and `git log --oneline -5` in "
        "your worktree, `gh pr checks` for any PR of yours, and `subfleet runs "
        "--mine`. Background waits, monitors and workflows died with the "
        "process, so re-run anything whose result never arrived.\n"
        "EXCEPTION: if your last message asked Max a question or offered him a "
        "decision, do not proceed past it. Restate it in one line and stop; the "
        "interruption was not his answer.\n"
        "(automated wake from subfleet; no reply needed)"
    )


def recorded_wakes(facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every session's latest nudge record that is a wake."""
    found = []
    for session_id, value in (facts.get("sessions") or {}).items():
        last = _wake_record(value)
        if last is not None:
            found.append({**last, "session_id": session_id})
    return found


def pending_wakes(facts: Mapping[str, Any], *, transport: str, now: datetime,
                  settle_s: float,
                  status: Callable[[list[str]], list[Mapping[str, Any]]] | None = None) -> int:
    """How many wakes are sent and not yet settled, so not yet in any reading.

    A conversation wake is pending while its message is in a live state. A
    desktop wake reports nothing back, so it counts as pending for `settle_s`
    after it was planned. When the message states cannot be read, every wake
    recorded within `settle_s` counts as pending."""
    from ..conversations.turn import TERMINAL_STATES
    wakes = [item for item in recorded_wakes(facts) if item.get("transport") == transport]
    recent = [item for item in wakes
              if (age := _age_s(item.get("at"), now)) is not None and age < settle_s]
    if transport != "conversation" or status is None:
        return len(recent)
    newest = sorted(wakes, key=lambda item: str(item.get("at") or ""), reverse=True)
    ids = [str(item["message_id"]) for item in newest if item.get("message_id")][:200]
    if not ids:
        return 0
    try:
        states = status(ids)
    except Exception:                                   # noqa: BLE001 - unknown counts as pending
        return len(recent)
    recent_ids = {str(item.get("message_id")) for item in recent}
    # A reservation whose message the store has not seen (`unknown`) is a wake
    # being sent right now, or one a pass lost after reserving: it counts while
    # recent, then stops.
    return sum(1 for item in states if isinstance(item, Mapping)
               and (item.get("state") not in TERMINAL_STATES and item.get("state") != "unknown"
                    or item.get("state") == "unknown" and str(item.get("message_id")) in recent_ids))


class ConversationTransport:
    """Wakes through the daemon's conversation service (C-25, C-26).

    `open` and `submit` are both idempotent, by the native binding and by the
    message id and digest, so a lost answer is re-sent once (C-16.3)."""

    def __init__(self, client):
        self.client = client
        self._checked = False

    def ready(self) -> str | None:
        """C-25.1: no conversation op before `capabilities` names the service."""
        if self._checked:
            return None
        try:
            answer = self.client.call("capabilities", {})
        except Exception as exc:                        # noqa: BLE001 - reported to the operator
            return f"the daemon did not answer `capabilities`: {exc}"
        names = answer.get("capabilities") if isinstance(answer, Mapping) else None
        listed = list(names) if isinstance(names, (list, tuple)) else []
        if CONVERSATIONS_CAPABILITY not in listed:
            return f"this daemon does not offer {CONVERSATIONS_CAPABILITY}"
        self._checked = True
        return None

    def open(self, session_id: str) -> Mapping[str, Any]:
        return self.client.call_settled("conversation.open",
                                        {"native": {"provider": "claude", "session_id": session_id}})

    def submit(self, conversation_id: str, message_id: str, text: str,
               settings: Mapping[str, Any], after: str | None) -> Mapping[str, Any]:
        return self.client.call_settled("message.submit", {
            "conversation_id": conversation_id, "message_id": message_id, "text": text,
            "settings": dict(settings), "after_message_id": after})

    def status(self, message_ids: list[str]) -> list[Mapping[str, Any]]:
        answer = self.client.call("message.status", {"message_ids": list(message_ids)[:200]})
        return list(answer.get("messages") or [])


def predict_open(transcript: str | Path) -> tuple[bool, str | None, Mapping[str, Any]]:
    """What `conversation.open` of this transcript's session would do, without
    binding anything: open binds a session for good (C-26.13), so it is never
    used to find out."""
    from ..conversations import catalog
    facts = catalog.claude_session(Path(transcript))
    if not facts.get("continuable"):
        return False, str(facts.get("continue_blocker") or "not continuable"), facts
    return True, None, facts


def branch_refusal(workspace: str, permission: str | None, *, timeout_s: float = 10.0) -> str | None:
    """Why a writable turn would be refused in `workspace`: a checkout of main or
    master (C-13.2). Allowing main is a person's call (C-26.10), so a wake does not."""
    if permission == "read-only":
        return None
    from ..salvage import git_branch
    try:
        branch = git_branch(workspace, timeout_s=timeout_s)
    except Exception as exc:                            # noqa: BLE001 - unread is not "no branch"
        return f"its workspace's branch could not be read ({type(exc).__name__})"
    if branch in {"main", "master"}:
        return (f"its workspace is a checkout of {branch}, where a writable "
                "conversation turn is refused (C-13.2)")
    return None


@dataclass
class WakeReport:
    """One wake pass: the scan, the pacing, and what each session got."""

    scan: Scan
    transport: str
    pace: Pace | None = None
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def woken(self) -> list[Dormant]:
        return [row for row in self.scan.rows if row.woken or row.planned]

    def to_dict(self) -> dict[str, Any]:
        scanned = self.scan.to_dict()
        return {"scope": "cold", "wake": self.transport, "dry_run": self.dry_run,
                "pace": self.pace.to_dict() if self.pace else None,
                "errors": list(self.errors) + list(self.scan.errors),
                **{key: value for key, value in scanned.items() if key != "errors"}}


@dataclass(frozen=True)
class Probes:
    """What a wake re-reads just before it is reserved. Tests replace them."""

    processes: Callable[[], Processes | None] = read_processes
    reading: Callable[[], registry.Reading] = registry.read
    copies: Callable[[Path, str, Mapping[str, Any] | None], DesktopRecord | None] = \
        lambda store, local, flags: all_copies(store, local, flags=flags)
    open_facts: Callable[[str], tuple[bool, str | None, Mapping[str, Any]]] = predict_open
    branch: Callable[[str, str | None], str | None] = \
        lambda workspace, permission: branch_refusal(workspace, permission)


def wake_pass(sessions, policy: Mapping[str, Any], *, transport: str,
              conversations: ConversationTransport | None = None,
              window: Window | None = None,
              window_reader: Callable[[], Window | Sequence[Window]] | None = None,
              only: Sequence[str] = (), caller: str | None = None,
              dry_run: bool = False, force: bool = False,
              now: datetime | None = None, processes: Any = ...,
              reading: registry.Reading | None = None,
              store: Path | None = None, state_root: Path | None = None,
              probes: Probes = Probes()) -> WakeReport:
    """Scan, pace, and wake the eligible dormant sessions, oldest first (C-23.60).

    `transport` is `conversation` (Subfleet continues the session itself, paced
    against the lane its turn would run on) or `desktop` (the wakes are planned
    for an agent that can call the desktop app's session API, paced against the
    `window` that agent read). Each wake is reserved in the daemon's store
    before anything is sent, once per interruption point. `force` overrides
    that dedupe and the unanswered-wake hold, never a fence, a filter or the
    pace.
    """
    if transport not in TRANSPORTS:
        raise ValueError(f"transport must be one of {TRANSPORTS}")
    lock = None if dry_run or state_root is None else _pass_lock(state_root)
    if lock is False:
        busy = Scan(errors=["another wake pass is running; this one waits for the next"])
        report = WakeReport(scan=busy, transport=transport, dry_run=dry_run)
        report.pace = Pace(0, "another wake pass is running")
        return report
    try:
        return _wake_pass(sessions, policy, transport=transport, conversations=conversations,
                          window=window, window_reader=window_reader, only=only, caller=caller,
                          dry_run=dry_run, force=force, now=now, processes=processes,
                          reading=reading, store=store, state_root=state_root, probes=probes)
    finally:
        if isinstance(lock, int):
            os.close(lock)


#: One pass at a time reserves wakes on this machine, so two passes (the daemon's
#: timer and a person's, say) cannot each count the same pending wakes and each
#: send a batch (C-23.59).
LOCK_NAME = "wake.lock"


def _pass_lock(state_root: Path) -> int | bool:
    """The pass lock's descriptor, or False when another pass holds it."""
    import fcntl
    directory = Path(state_root) / "sessions"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd = transcripts.lock_fd(directory / LOCK_NAME)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    return fd


def _wake_pass(sessions, policy: Mapping[str, Any], *, transport: str,
               conversations: ConversationTransport | None,
               window: Window | None,
               window_reader: Callable[[], Window | Sequence[Window]] | None,
               only: Sequence[str], caller: str | None, dry_run: bool, force: bool,
               now: datetime | None, processes: Any, reading: registry.Reading | None,
               store: Path | None, state_root: Path | None, probes: "Probes") -> WakeReport:
    instant = now or datetime.now(timezone.utc)
    settings = Settings.from_policy(policy)
    rule = PaceRule.from_policy(policy)
    facts = sessions.state(None)
    result = scan(facts, policy, now=instant, only=only, caller=caller, force=force,
                  processes=processes, reading=reading, store=store, state_root=state_root)
    report = WakeReport(scan=result, transport=transport, dry_run=dry_run)
    eligible = sorted(result.eligible, key=lambda row: (-(row.turn.age_s or 0), row.session_id))
    if not eligible:
        report.pace = Pace(0, "no eligible dormant session")
        return report
    if transport == "conversation":
        if conversations is None:
            raise ValueError("a conversation wake needs the conversation transport")
        refusal = conversations.ready()
        if refusal:
            report.errors.append(refusal)
            report.pace = Pace(0, refusal)
            for row in eligible:
                row.reason = f"held: {refusal}"
            return report
    if not result.flags_read:
        refusal = "the mirror's flags file could not be read, so archive state is unknown"
        report.pace = Pace(0, refusal)
        for row in eligible:
            row.reason = f"held: {refusal}"
        return report
    read = window if window is not None else (window_reader() if window_reader else None)
    windows = list(read) if isinstance(read, (list, tuple)) else ([read] if read is not None else [])
    reading_window = windows[0] if windows else None
    pending = pending_wakes(facts, transport=transport, now=instant, settle_s=settings.settle_s,
                            status=conversations.status if conversations else None)
    report.pace = pace_lanes(windows, now=instant, rule=rule, pending=pending)
    budget = report.pace.allowed
    base = store or store_dir()
    table = _Recheck(probes)
    for row in eligible:
        if budget <= 0:
            row.reason = f"paced: {report.pace.reason}"
            continue
        budget -= int(_wake_one(row, sessions, settings, transport=transport,
                                conversations=conversations, dry_run=dry_run, force=force,
                                window=reading_window, now=instant, store=base,
                                flags=result.flags.get(row.session_id),
                                probes=probes, table=table))
    return report


#: How long one re-read process table serves the wakes that follow it.
RECHECK_TABLE_S = 30.0


class _Recheck:
    """The process table and registry a wake re-reads before its reservation,
    read once and reused for `RECHECK_TABLE_S`: a held session spends no place
    in the batch, and a `ps` per held session could take the pass past its
    limit on a loaded machine."""

    def __init__(self, probes: "Probes", clock: Callable[[], float] | None = None):
        import time as _time
        self.probes, self.clock = probes, clock or _time.monotonic
        self.read_at: float | None = None
        self.processes: Processes | None = None
        self.reading: registry.Reading = registry.Reading()

    def current(self) -> tuple[registry.Reading, Processes | None]:
        now = self.clock()
        if self.read_at is None or now - self.read_at > RECHECK_TABLE_S:
            self.reading, self.processes = self.probes.reading(), self.probes.processes()
            self.read_at = now
        return self.reading, self.processes



def _hold(row: Dormant, reason: str, fix: str | None = None) -> bool:
    row.eligible = False
    row.reason, row.fix = reason, fix
    return False


def _wake_one(row: Dormant, sessions, settings: Settings, *, transport: str,
              conversations: ConversationTransport | None, dry_run: bool, force: bool,
              window: Window | None, now: datetime, store: Path,
              flags: Mapping[str, Any] | None, probes: Probes,
              table: "_Recheck | None" = None) -> bool:
    """Wake one eligible session. True when it spent a place in the batch."""
    # The strict record check: every account folder's copy, just before acting.
    # A copy that cannot be read may be the archived one, so it holds the wake.
    local = row.record.local_id if row.record else None
    strict = probes.copies(store, local, flags) if local else None
    if strict is None:
        return _hold(row, "its desktop record's copies could not be read, so whether it "
                     "is archived is unknown", "run the pass again")
    if strict.unreadable:
        return _hold(row, f"{strict.unreadable} copies of its desktop record could not be "
                     "read, so whether it is archived is unknown", "run the pass again")
    row.record = strict
    ok, reason, fix = admits(row, settings, force=force)
    if not ok:
        return _hold(row, reason, fix)
    # C-23.34's re-check, made again here: the process and the last turn as they
    # are now, not as the scan found them.
    current_reading, current_processes = (table or _Recheck(probes)).current()
    again = liveness(row.session_id, reading=current_reading, processes=current_processes)
    if again != "dead":
        return _hold(row, f"active: its process is {again} now")
    if row.transcript:
        now_turn = transcripts.turn_state(row.transcript, now=now)
        if now_turn.fingerprint != row.turn.fingerprint:
            return _hold(row, "active: a new turn appeared since the scan")
        # A restart that wrote only the app's resume stub, or a subagent's file,
        # leaves the fingerprint alone; the quiet window is read again.
        written = last_write(row.transcript, after=now.timestamp() - settings.quiet_s)
        if written is None or now.timestamp() - written < settings.quiet_s:
            return _hold(row, "active: its transcript or side directory was written since the scan")
    permission = None
    if transport == "conversation":
        ok, blocker, predicted = probes.open_facts(row.transcript or "")
        if not ok:
            if blocker == "a Subfleet lane run":
                # The conversation service reads the transcript's shape, and a
                # chip's first prompts arrive through the SDK like a lane's.
                return _hold(row, "the conversation service reads its transcript as a "
                             "lane run's (a chip's first prompts arrive through the SDK), "
                             "so it will not open it",
                             "wake it through the desktop app: `subfleet sessions wake --plan`")
            return _hold(row, f"a conversation cannot continue it: {blocker}",
                         "hand it off instead (`subfleet handoff <session> --to opus`)")
        row.workspace = str(predicted.get("cwd") or "") or None
        permission = predicted.get("permission")
        refused = probes.branch(row.workspace or "", permission)
        if refused:
            return _hold(row, refused,
                         "open it in the Subfleet app and allow main, or check out a branch")
    if dry_run:
        row.reason = f"would wake by {transport}: {row.reason}"
        return True
    message_id = str(uuid.uuid4())
    record = sessions.record_nudge(
        row.session_id, dedupe_key=wake_key(row.turn), cooldown_s=None,
        kind=WAKE_KIND, force=force,
        detail={"transport": transport, "message_id": message_id,
                "local_id": row.record.local_id if row.record else None,
                "turn_uuid": row.turn.last_uuid,
                "window": window.to_dict() if window else None})
    if not record.get("recorded"):
        reason = record.get("reason") or "another pass reserved this wake"
        fix = (f"if it never arrived: subfleet sessions wake --force {row.session_id}"
               if "already" in reason else None)
        return _hold(row, reason, fix)
    row.message_id = message_id
    if transport == "desktop":
        row.planned = True
        row.reason = "planned: send it with the desktop app's send_message"
        return True
    assert conversations is not None
    try:
        opened = conversations.open(row.session_id)
    except Exception as exc:                            # noqa: BLE001 - reported per session
        row.reason = f"reserved, but conversation.open failed: {exc}"
        row.fix = f"subfleet sessions wake --force {row.session_id}"
        return True
    view = opened.get("conversation") if isinstance(opened, Mapping) else None
    if not isinstance(view, Mapping) or not view.get("conversation_id"):
        row.reason = "reserved, but conversation.open answered no conversation"
        row.fix = f"subfleet sessions wake --force {row.session_id}"
        return True
    row.conversation_id = str(view["conversation_id"])
    if any(isinstance(item, Mapping) and item.get("origin") == "person"
           for item in (opened.get("messages") or [])):
        row.reason = ("already a Subfleet conversation with messages of its own; "
                      "left to it (C-26.13)")
        row.fix = registry.CONVERSATION_FIX
        return False
    current = view.get("settings") if isinstance(view.get("settings"), Mapping) else {}
    chosen = {"model": settings.model, "effort": current.get("effort"),
              "fast": bool(current.get("fast", False)),
              "permission": current.get("permission") or permission or "ask",
              "auto_continue": bool(current.get("auto_continue", True))}
    try:
        receipt = conversations.submit(row.conversation_id, message_id, wake_text(row),
                                       chosen, None)
    except Exception as exc:                            # noqa: BLE001 - reported per session
        row.reason = f"opened as {row.conversation_id}, but message.submit failed: {exc}"
        row.fix = "send the next message there from the Subfleet app"
        return True
    row.woken = True
    row.state = str(receipt.get("state") or "queued") if isinstance(receipt, Mapping) else None
    row.reason = f"woken as Subfleet conversation {row.conversation_id} ({row.state})"
    return True


def plan(report: WakeReport) -> list[dict[str, Any]]:
    """The desktop transport's hand-off: one `send_message` per planned wake."""
    return [{"local_id": row.record.local_id if row.record else None,
             "cli_session_id": row.session_id,
             "title": row.record.title if row.record else None,
             "message": wake_text(row)} for row in report.scan.rows if row.planned]


def render(report: WakeReport) -> str:
    """The operator's view: the interrupted sessions and what each one got."""
    counts = report.scan.counts()
    verb = "planned" if report.transport == "desktop" else "woken"
    header = (f"{len(report.woken)} {verb}, {counts['eligible'] - len(report.woken)} "
              f"eligible held, {counts['interrupted']} interrupted in all "
              f"({counts['active']} active, {counts['completed']} completed, "
              f"{counts['fenced']} fenced)")
    if report.dry_run:
        header += " (dry run: nothing reserved or sent)"
    lines = [header]
    if report.pace is not None:
        lines.append(f"  pace: {report.pace.reason}")
        if report.pace.window is not None and report.pace.window.source:
            lines.append(f"        against {report.pace.window.source}")
    for error in list(report.errors) + list(report.scan.errors):
        lines.append(f"  note: {error}")
    for row in report.scan.rows:
        if not row.interrupted:
            continue
        mark = "woken" if row.woken else "plan" if row.planned else \
            "held" if not row.eligible else "ready"
        title = (row.record.title if row.record else None) or "-"
        lines.append(f"  {mark:<6}{row.session_id[:8]}  {title[:48]}")
        lines.append(f"        {row.reason}")
        if row.fix:
            lines.append(f"        fix: {row.fix}")
    return "\n".join(lines)


# --- the automatic pass (C-23.60) --------------------------------------------------

#: The daemon's last automatic wake pass, as `doctor` and a person read it.
PASS_NAME = "wake-pass.json"
#: The longest one automatic pass may run before it is stopped.
PASS_TIMEOUT_S = 900.0
#: After a stop or a timeout, how long the pass's process group has to end
#: before it is killed, and how long after the kill it is waited for.
PASS_TERM_S = 4.0
PASS_KILL_S = 1.0


def pass_path(root: str | Path) -> Path:
    return Path(root) / "sessions" / PASS_NAME


def pass_command() -> list[str]:
    """The automatic pass is `sessions wake --all --json`, the same code a person
    runs, in its own process: nothing it reads or blocks on is the daemon's."""
    import sys
    return [sys.executable, "-m", "subfleet.sessions.cli", "continue", "--scope", "cold",
            "--wake", "--all", "--json"]


def automatic_pass(root: str | Path, *, cancel, timeout_s: float = PASS_TIMEOUT_S,
                   command: Sequence[str] | None = None, popen=None,
                   clock: Callable[[], float] | None = None,
                   now: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """Run one wake pass as a child process and record what it did (C-23.60).

    `cancel` is the daemon's stop event: a stop ends the child within
    `PASS_TERM_S` plus a second, so a pass can never hold a daemon stop
    (C-5.8a). A pass stopped mid-way can leave one wake reserved and unsent,
    which the next survey names with its `--force` fix. The summary is written
    to `pass_path(root)` and returned."""
    import subprocess
    import time as _time
    tick = clock or _time.monotonic
    stamp = now or (lambda: datetime.now(timezone.utc))
    launcher = popen or subprocess.Popen
    environment = dict(os.environ)
    environment["SUBFLEET_HOME"] = str(root)
    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CLAUDE_PID"):
        environment.pop(name, None)
    package = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = (package + os.pathsep + environment["PYTHONPATH"]
                                 if environment.get("PYTHONPATH") else package)
    started = stamp()
    summary: dict[str, Any] = {"started_at": _iso(started), "exit": None, "outcome": None,
                               "woken": [], "eligible": 0, "interrupted": 0, "pace": None,
                               "errors": []}
    try:
        child = launcher(list(command or pass_command()), stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         start_new_session=True, close_fds=True, env=environment)
    except OSError as exc:
        summary.update(outcome="not started", errors=[f"{type(exc).__name__}: {exc}"])
        return _publish(root, summary, stamp)
    deadline = tick() + timeout_s
    out = err = ""
    while True:
        try:
            out, err = child.communicate(timeout=1.0)
            summary["outcome"] = "finished"
            break
        except subprocess.TimeoutExpired:
            if not (cancel.is_set() or tick() > deadline):
                continue
            summary["outcome"] = "stopped" if cancel.is_set() else "timed out"
            out, err = _end(child)
            break
    summary["exit"] = child.returncode
    report = None
    for line in reversed((out or "").splitlines()):
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict):
            report = candidate
            break
    if report is not None:
        sessions = [row for row in report.get("sessions") or [] if isinstance(row, dict)]
        summary.update(
            woken=[{"session_id": row.get("session_id"), "title": row.get("title"),
                    "conversation_id": row.get("conversation_id")}
                   for row in sessions if row.get("woken")],
            eligible=sum(1 for row in sessions if row.get("eligible")),
            interrupted=sum(1 for row in sessions
                            if (row.get("verdict") or {}).get("state") == "interrupted"),
            pace=report.get("pace"), errors=list(report.get("errors") or []))
    tail = (err or "").strip().splitlines()[-3:]
    if tail and (child.returncode or report is None):
        summary["errors"] = list(summary["errors"]) + tail
    return _publish(root, summary, stamp)


def _end(child) -> tuple[str, str]:
    """End a pass's whole process group (it runs in a session of its own), each
    wait bounded: a grandchild holding the pipe cannot hold a daemon stop."""
    import signal
    import subprocess
    for signum, wait in ((signal.SIGTERM, PASS_TERM_S), (signal.SIGKILL, PASS_KILL_S)):
        try:
            os.killpg(child.pid, signum)
        except OSError:
            pass
        try:
            return child.communicate(timeout=wait)
        except subprocess.TimeoutExpired:
            continue
    for stream in (child.stdout, child.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass
    return "", ""


def _publish(root: str | Path, summary: dict[str, Any],
             stamp: Callable[[], datetime]) -> dict[str, Any]:
    summary["finished_at"] = _iso(stamp())
    path = pass_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(summary, sort_keys=True, indent=1), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        summary["errors"] = list(summary["errors"]) + [f"cannot write {path}: {exc}"]
    return summary


def last_pass(root: str | Path) -> dict[str, Any] | None:
    """The last automatic pass's summary, or None when there is none to read."""
    from ..state_files import read_state
    try:
        data = json.loads(read_state(pass_path(root), limit=RECORD_LIMIT).decode("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _age_s(stamp: Any, now: datetime) -> float | None:
    parsed = _instant(stamp)
    return None if parsed is None else (now - parsed).total_seconds()


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            return None
        seconds = value / 1000 if abs(value) > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return _instant(float(text))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = ["BATCH_LIMIT", "ConversationTransport", "DesktopRecord", "Dormant", "LIVENESS",
           "MID_TURN", "Pace", "PaceRule", "Probes", "Processes", "Scan", "Settings",
           "TRANSPORTS", "VERDICTS", "Verdict", "WAKE_KIND", "WAKE_MARKER", "WakeReport",
           "Window", "admits", "all_copies", "automatic_pass", "last_pass", "pass_path", "branch_refusal", "classify", "elapsed_pct",
           "folder_records", "lane_window", "lane_windows", "last_write", "liveness", "loaded_folder",
           "distrust", "merge_copies", "mirror_flags", "pace_lanes", "parse_processes", "pace", "pending_wakes",
           "plan", "predict_open", "read_processes", "project_names", "record_transcript", "recorded_wakes",
           "render", "scan", "wake_key", "wake_pass", "wake_text", "window_from"]
