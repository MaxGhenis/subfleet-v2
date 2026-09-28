"""How a turn's message settles, and reconciliation when its delivery is uncertain
(C-24.4, C-24.6, C-24.8, C-26.7; design D-12 to D-14).

The runner reports the driver's outcome in `<attempt>/turn.json` once the
attempt's exit receipt exists. `settle` turns that outcome into the message's
state, a conversation block, a re-admission, or a failover continuation. It
is pure: the service applies what it returns, and `gather` is the only part
that reads files.

What ended the turn decides how much is known (`Outcome.ended_by`):

- `provider`: its own terminal event (Claude `result`; Codex `turn/completed`,
  or its error answer to `turn/start`). The provider's word is the outcome.
- `driver`: the driver's own check ended the turn. Almost always that is
  before the message frame existed (identity, catalog, Fast, guard, thread,
  a stop before sending); a Claude `model-mismatch` can come after it.
- `eof`: stdout ended with no terminal event. The message may or may not have
  reached the provider.

For the last two the delivery is reconciled from evidence (D-14):

1. delivered, when the attempt's stdout acknowledged the message (D-12: Claude
   `command_lifecycle started` or the replayed user message with our uuid;
   Codex a turn id), or the native record holds it (Claude: a `user` record
   with the message id as `uuid` in the session transcript after the
   attempt's `transcript_offset`; Codex: a rollout record whose `client_id` is
   the message id);
2. not delivered, only when the process is verified gone (the exit receipt:
   the guardian writes it after reaping the child, `guardian.py`, or the
   daemon after a containment census it verified empty, `daemon.py
   _kill_attempt`), the native record was read and does not hold the message
   (a native session with no record on disk at all counts: a new Claude
   session with no transcript, a Codex attempt with no thread id; D-14
   revision 3), and the relay log, read whole and consistent, holds no record
   of the message frame (`relay.py`: every frame is logged as an intent, with
   fsync, before its pipe write, so a frame with no intent was never written);
3. otherwise unknown: `delivery-unknown`, which blocks the conversation until
   a person's `message.resolve` (C-24.6).

A Claude turn that ends without a terminal event after delivery blocks its
conversation `unfinished-turn` (C-24.8): the next `--resume` could continue
it. A message with unknown delivery also blocks (as `delivery-unknown`), so a
written message frame always holds the conversation (review IR-5).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..relay import read_log
from ..sessions import transcripts
from ..state_files import open_state
from .store import LEGACY_OWNER
from .turn import COMPLETE, DELIVERY_UNKNOWN, FAILED, INTERRUPTED, WAITING

DELIVERED = "delivered"
NOT_DELIVERED = "not-delivered"
UNKNOWN = "delivery-unknown"

# The tag both drivers give the message frame (`claude_turn.py` `_control_response`,
# `codex_turn.py` `_thread`).
USER_FRAME = "user-message"
#: Claude's `get_settings`, asked right after the message so the turn records the
#: effort the provider applied (C-26.8).
SETTINGS_FRAME = "settings"

# Refusals before the message was sent that another admission may get past (review
# IR-23: Fast on another account; an external writer that has gone; a provider or
# guard that failed to start). The same message is carried by a new turn job, at
# most MAX_READMITS times for the chargeable ones; a wait for the session's other
# writer is re-admitted however often it happens and never counted (`ownership_wait`).
READMIT = frozenset({"external-writer", "fast-unavailable", "provider-init-failed", "guard-refused"})
MAX_READMITS = 3

# Reasons a stop was requested: a person's (`turn.interrupt`, `cancel-turn`) or the
# daemon's (IR-4, IR-8). A relay failure also stops a turn, but nobody asked for it.
STOPS = frozenset({"stopped", "wall-limit", "operator-kill", "approval-timeout"})

# Driver reasons that end a turn before its message frame is produced.
BEFORE_SEND = frozenset({"stopped-before-send", "identity", "guard-refused", "settings-unsupported",
                         "effort-unsupported", "provider-init-failed", "thread-failed", "thread-mismatch",
                         "external-writer", "fast-unavailable"})


@dataclass(frozen=True)
class Evidence:
    """What is known about one attempt's message, for D-14."""

    acknowledged: bool          # the attempt's stdout acknowledged the message (D-12)
    frame: str                  # the relay log's record of it: written | failed | pending | absent | unreadable
    process_gone: bool          # the attempt's exit receipt exists
    native: str                 # the native record: found | absent | unreadable
    native_path: str | None = None
    session_exists: bool = False    # the native session has a record on disk at all

    def as_dict(self) -> dict:
        return {"acknowledged": self.acknowledged, "frame": self.frame, "process_gone": self.process_gone,
                "native": self.native, "native_path": self.native_path, "session_exists": self.session_exists}


@dataclass(frozen=True)
class Settlement:
    """What the service does with a message whose turn ended."""

    state: str
    reason: str | None
    block: str | None = None            # the conversation's new `blocked_by`
    readmit: bool = False               # carry the same message in a new turn job
    continue_elsewhere: bool = False    # D-6: a labelled continuation (Claude, auto_continue)
    delivery: str | None = None         # the reconciliation verdict, when one was needed
    evidence: Evidence | None = None
    session_known: bool = False         # the native session exists, so the next turn resumes it


def decide(evidence: Evidence) -> str:
    """D-14, C-24.6: delivered, not delivered, or unknown."""
    if evidence.acknowledged or evidence.native == "found":
        return DELIVERED
    if evidence.process_gone and evidence.native == "absent" and evidence.frame == "absent":
        return NOT_DELIVERED
    return UNKNOWN


def settle(turn: dict, *, provider: str, turn_seq: int, gather: Callable[[], Evidence],
           person_stopped: bool = False) -> Settlement:
    """The message's fate from its turn's outcome (`turn.json`). `person_stopped`
    says a person asked to stop the message (its `stop_requested_at`)."""
    state, reason = turn.get("state"), turn.get("reason")
    ended_by = turn.get("ended_by") or _legacy_ended_by(turn)
    stop = turn.get("stop_reason")
    if state == COMPLETE:
        # C-24.4: the provider's success stands even when a stop came too late.
        return Settlement(COMPLETE, "stop-too-late" if turn.get("stop_too_late") else None, session_known=True)
    if ended_by == "provider":
        if state == INTERRUPTED:
            return Settlement(INTERRUPTED, stop if stop in STOPS else reason, session_known=True)
        if reason == "limited" or turn.get("limited"):
            # C-26.7: the closure is recorded at finalization; a continuation, never a resend.
            return Settlement(FAILED, "limited", continue_elsewhere=True, session_known=True)
        return Settlement(FAILED, reason or "failed", session_known=True)

    evidence = gather()
    delivery = decide(evidence)
    known = evidence.acknowledged or evidence.native == "found" or evidence.session_exists

    def result(state: str, reason: str | None, block: str | None = None, **extra: Any) -> Settlement:
        return Settlement(state, reason, block=block, delivery=delivery, evidence=evidence, session_known=known,
                          **extra)

    if delivery == UNKNOWN:
        detail = f"{reason or 'ended'}: frame {evidence.frame}, native record {evidence.native}"
        return result(DELIVERY_UNKNOWN, detail, "delivery-unknown")

    if ended_by == "driver":
        if delivery == NOT_DELIVERED:
            # Another writer is a wait, however long it lasts (C-26.3): it never
            # uses up the re-admissions a failing provider gets.
            if reason in READMIT and (turn_seq < MAX_READMITS or reason == "external-writer"):
                return result(WAITING, f"readmit:{reason}", readmit=True)
            # C-30.4: a turn the legacy hold stopped before its message was written
            # (`TurnRunner.withhold`) waits for the hold to lift and is admitted
            # again; like another writer, that wait uses up no re-admission. A
            # person's stop stands: the message is not carried again.
            if reason == "stopped-before-send" and stop == LEGACY_OWNER and not person_stopped:
                return result(WAITING, f"readmit:{LEGACY_OWNER}", readmit=True)
            # C-26.8 names a model mismatch as such, sent or not.
            return result(FAILED, reason if reason == "model-mismatch" else f"not-delivered: {reason}")
        # Delivered, then ended by the driver (a Claude model mismatch after
        # sending): it stopped the turn with the provider's own interrupt. A
        # terminal event after that means the turn finished (C-24.8).
        unfinished = provider == "claude" and not turn.get("terminal_after_end")
        return result(state or FAILED, reason, "unfinished-turn" if unfinished else None)

    # ended_by == "eof": no terminal event.
    if delivery == NOT_DELIVERED:
        why = "frame-too-large" if turn.get("frame_refused") == USER_FRAME else (
            stop if stop else "ended-before-send")
        return result(FAILED, f"not-delivered: {why}")
    stopped = stop in STOPS or (reason == "stopped" and stop is None)
    if stopped:
        state, reason = INTERRUPTED, stop or "stopped"
    else:
        state, reason = FAILED, stop or reason or "ended-without-result"
    # C-24.8: the next --resume could continue a Claude turn left mid-way.
    return result(state, reason, "unfinished-turn" if provider == "claude" else None)


def ownership_wait(turn: Any) -> bool:
    """Whether a turn ended, before its message was written, as a wait for the
    session's other writer: another process (`external-writer`, C-26.3) or the
    legacy cockpit (`stopped-before-send` by `legacy-owner`, `TurnRunner.withhold`,
    C-30.4). Such a turn is re-admitted however often it happens and uses up none
    of the re-admissions a failing provider gets (C-24.6, design D-17), so the
    service does not count it among them (`ConversationService._provider_tries`)."""
    if not isinstance(turn, dict):
        return False
    reason = turn.get("reason")
    ended_by = turn.get("ended_by") or _legacy_ended_by(turn)
    return ended_by == "driver" and (reason == "external-writer" or (
        reason == "stopped-before-send" and turn.get("stop_reason") == LEGACY_OWNER))


def _legacy_ended_by(turn: dict) -> str:
    """A `turn.json` from before `ended_by` existed."""
    reason = turn.get("reason")
    if reason == "ended-without-result":
        return "eof"
    if reason in BEFORE_SEND or reason == "model-mismatch":
        return "driver"
    return "provider"


# --- evidence ------------------------------------------------------------------


def gather(provider: str, message_id: str, attempt_dir: Path, turn: dict) -> Evidence:
    """Read what the attempt left: its relay log, its exit receipt, and the native
    record named by its launch notes."""
    adir = Path(attempt_dir)
    notes = (_read_json(adir / "launch.json") or {}).get("notes") or {}
    frame = frame_status(adir)
    gone = (adir / "exit.json").exists()
    if provider == "claude":
        native, path, exists = claude_record(message_id, session_id=turn.get("native_session_id")
                                             or notes.get("session_id"), notes=notes)
    else:
        native, path, exists = codex_record(message_id, home=notes.get("codex_home"),
                                            thread_id=turn.get("native_session_id") or notes.get("thread_id"))
    return Evidence(acknowledged=bool(turn.get("accepted")), frame=frame, process_gone=gone, native=native,
                    native_path=path, session_exists=exists)


def frame_status(attempt_dir: Path) -> str:
    """The relay log's record of the message frame. `absent` only when the whole
    log was read and is consistent (every complete line accounted for by
    `read_log`); a log it stopped reading early is `unreadable`."""
    path = Path(attempt_dir) / "stdin.jsonl"
    try:
        with open_state(path) as stream:
            data = stream.read()
    except FileNotFoundError:
        return "absent"                     # the relay never logged a frame
    except OSError:
        return "unreadable"
    records = read_log(path)
    accounted = sum(1 if record["status"] == "pending" else 2 for record in records)
    if accounted != data.count(b"\n"):
        return "unreadable"
    for record in records:
        if record.get("tag") == USER_FRAME:
            return record["status"]
    return "absent"


def claude_record(message_id: str, *, session_id: str | None, notes: dict) -> tuple[str, str | None, bool]:
    """Whether the session transcript holds a `user` record with the message id as
    its `uuid` (design D-14). Transcripts are `<projects>/<encoded cwd>/<session
    id>.jsonl`; the recorded one is read from the attempt's `transcript_offset`,
    any other candidate whole. Returns (found | absent | unreadable, path,
    whether the session has a transcript)."""
    projects = notes.get("projects_dir")
    if not session_id or not isinstance(projects, str) or not projects:
        return "unreadable", None, False
    name = f"{session_id}.jsonl"
    try:
        entries = os.listdir(projects)
    except OSError:
        return "unreadable", None, False
    candidates = [os.path.join(projects, name)] + [os.path.join(projects, entry, name) for entry in sorted(entries)]
    candidates = [path for path in candidates if os.path.isfile(path)]
    if not candidates:
        return "absent", None, False
    recorded = notes.get("transcript_path")
    offset = notes.get("transcript_offset") if isinstance(notes.get("transcript_offset"), int) else 0
    for path in candidates:
        start = offset if recorded and _same_file(path, recorded) else 0
        found = _scan(path, message_id, start, lambda record: record.get("type") == "user"
                      and record.get("uuid") == message_id)
        if found != "absent":
            return found, path, True
    return "absent", candidates[0], True


def codex_record(message_id: str, *, home: str | None, thread_id: str | None) -> tuple[str, str | None, bool]:
    """Whether the thread's rollout holds a record carrying the message id as its
    `client_id` (the 2026-09-24 probe found it on the rollout's user message item;
    review record F7). The driver sends `turn/start`, the message frame, only
    after a thread response names the thread (`codex_turn.py` `_thread`), so an
    attempt with no thread id has no rollout that could hold the message."""
    if not home:
        return "unreadable", None, False
    if not thread_id:
        return "absent", None, False
    sessions = Path(home) / "sessions"
    try:
        os.listdir(sessions)
    except OSError:
        return "unreadable", None, False
    matches = sorted(str(p) for p in sessions.rglob(f"rollout-*{thread_id}.jsonl"))
    if not matches:
        return "absent", None, False
    for path in matches:
        found = _scan(path, message_id, 0, lambda record: _carries(record, "client_id", message_id))
        if found != "absent":
            return found, path, True
    return "absent", matches[0], True


def _scan(path: str, message_id: str, start: int, matches: Callable[[dict], bool]) -> str:
    """found | absent | unreadable. A line that names the message but cannot be
    read as a whole record makes the answer unreadable, not absent."""
    needle = message_id.encode()
    try:
        with transcripts.open_regular(path) as stream:
            size = os.fstat(stream.fileno()).st_size
            if 0 < start <= size:
                stream.seek(start - 1)
                if stream.read(1) != b"\n":
                    stream.readline()           # the rest of a line that began before the offset
            for raw in stream:
                if needle not in raw:
                    continue
                if not raw.endswith(b"\n"):
                    return "unreadable"
                try:
                    record = json.loads(raw)
                except ValueError:
                    return "unreadable"
                if isinstance(record, dict) and matches(record):
                    return "found"
    except OSError:
        return "unreadable"
    return "absent"


def _carries(value: Any, key: str, wanted: str, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if isinstance(value, dict):
        if value.get(key) == wanted:
            return True
        return any(_carries(v, key, wanted, depth + 1) for v in value.values() if isinstance(v, (dict, list)))
    if isinstance(value, list):
        return any(_carries(v, key, wanted, depth + 1) for v in value)
    return False


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.abspath(a) == os.path.abspath(b)


def _read_json(path: Path) -> dict | None:
    try:
        with open_state(path) as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None
