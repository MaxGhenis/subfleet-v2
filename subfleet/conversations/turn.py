"""Shapes shared by the provider turn drivers (C-26.5, C-26.6).

A driver is pure: it is built from a `TurnSpec`, fed stdout lines with their
byte offsets and operator commands, and answers with `Step`s: events for the
conversation log, frames for the guardian relay, and at most one `Outcome`.
The runner does all I/O. After a daemon restart the runner rebuilds a driver
by feeding it the attempt's stdout again from the start; frames carry unique
tags, so a frame the relay log shows as applied is not sent again, and events
carry unique source keys, so the log gains no duplicate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Message states (C-24.4, design D-9).
QUEUED = "queued"
WAITING = "waiting"
STARTING = "starting"
RUNNING = "running"
APPROVAL_NEEDED = "approval-needed"
STEERING = "steering"
STEERED = "steered"
COMPLETE = "complete"
FAILED = "failed"
INTERRUPTED = "interrupted"
CANCELLED = "cancelled"
DELIVERY_UNKNOWN = "delivery-unknown"

LIVE_STATES = (WAITING, STARTING, RUNNING, APPROVAL_NEEDED, DELIVERY_UNKNOWN, STEERING)
TERMINAL_STATES = (COMPLETE, FAILED, INTERRUPTED, CANCELLED, STEERED)
MESSAGE_STATES = (QUEUED, *LIVE_STATES, *TERMINAL_STATES)

PERMISSIONS = ("ask", "accept-edits", "bypass", "read-only")
DECISIONS = ("allow", "allow-session", "allow-turn", "answer", "deny", "cancel-turn")


@dataclass(frozen=True)
class Image:
    sha256: str
    media_type: str
    path: str


@dataclass(frozen=True)
class TurnSpec:
    """Everything a driver needs, all of it in the turn job's manifest."""

    provider: str
    message_id: str
    text: str
    model_id: str
    permission: str
    native_session_id: str | None      # None: a new native session
    new_session_id: str | None = None  # Claude: the uuid minted for a new session
    effort: str | None = None
    effort_default: bool = False       # the effort is the policy's default, not the person's (C-26.8)
    fast: bool = False
    images: tuple[Image, ...] = ()
    cwd: str = ""
    lane_email: str | None = None      # the email the lane's label claims (C-1.4, C-10.6)
    model_ref: str | None = None       # the policy model id admission routed the turn to (D-19)
    guard_hash: str | None = None      # Codex: the hook hash `hooks/list` must report
    unified_exec_off: bool = False     # Codex: C-23.6's switch, as exec launches carry it
    held_by: tuple[int, ...] = ()      # Claude: outside pids holding the session at launch (C-26.3)
    network: bool = False              # Codex: a writable turn's shell reaches the network (d260)


@dataclass(frozen=True)
class Frame:
    tag: str                  # unique within the attempt: init, user-message, approval:<id>, interrupt, close, ...
    op: str                   # write | close
    line: str | None = None


@dataclass(frozen=True)
class Event:
    kind: str
    data: dict[str, Any]
    source: str               # "<stdout offset>:<ordinal>" or "cmd:<tag>"


@dataclass(frozen=True)
class Approval:
    provider_request_id: str
    kind: str                 # tool | question | command | file-change | permissions
    summary: dict[str, Any]   # display fields for the event (scrubbed, bounded)
    options: tuple[str, ...]
    request: dict[str, Any] = field(default_factory=dict)   # the provider's exact request (kept 0600)


@dataclass(frozen=True)
class Outcome:
    """How the turn ended, from the provider's own terminal evidence."""

    state: str                          # complete | failed | interrupted
    reason: str | None = None           # failed/interrupted: a short machine reason
    detail: str | None = None
    accepted: bool = False              # the provider acknowledged our message
    answered: bool = False              # the model produced any output for it
    limited: bool = False               # the provider refused for quota or credits
    served_model: str | None = None
    # What ended the turn (C-24.6): "provider" (its terminal event: Claude
    # `result`, Codex `turn/completed` or its answer to `turn/start`), "driver"
    # (the driver's own check ended it, before or after sending), or "eof"
    # (stdout ended with neither, so its delivery is for reconciliation).
    ended_by: str = "driver"
    # Shared with the driver until stdout drains: lifecycle/steer responses may
    # follow the provider's terminal frame, before settlement runs.
    steers: dict[str, dict[str, Any]] = field(default_factory=dict)
    usage: dict[str, Any] | None = None   # C-12.10: stream-reported counters for this attempt


@dataclass
class Step:
    events: list[Event] = field(default_factory=list)
    frames: list[Frame] = field(default_factory=list)
    approvals: list[Approval] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)   # provider request ids no longer pending
    outcome: Outcome | None = None

    def extend(self, other: "Step") -> "Step":
        self.events += other.events
        self.frames += other.frames
        self.approvals += other.approvals
        self.resolved += other.resolved
        self.outcome = self.outcome or other.outcome
        return self


class SteerTracking:
    """Delivery evidence shared by both pure drivers and the relay runner.

    A result or echo that proves consumption outranks a later cancellation:
    Claude also calls already-consumed folds cancelled when their turn fails.
    Requeueing those would deliver the person's message twice.
    """

    def _init_steers(self) -> None:
        self.steers: dict[str, dict[str, Any]] = {}
        self._steer_pending: set[str] = set()
        self._steer_announced: set[str] = set()

    def restore_steer(self, message_id: str, frame: str = "written") -> None:
        self.steers.setdefault(message_id, {"frame": frame, "fate": "unknown", "detail": None})
        self._steer_pending.add(message_id)

    def steer_written(self, message_id: str) -> None:
        if message_id in self.steers:
            self.steers[message_id]["frame"] = "written"

    def drop_steer(self, message_id: str, detail: str) -> Step:
        self.restore_steer(message_id, "unsent")
        self._steer_pending.discard(message_id)
        entry = self.steers[message_id]
        if entry["fate"] not in ("consumed", "delivered", "unanswered"):
            entry.update(frame="unsent", fate="refused", detail=detail)
        return Step(events=[Event("steer.missed", {"message_id": message_id, "why": detail},
                                  f"cmd:steer-missed:{message_id}")])

    def _steer_delivered(self, message_id: str, source: str, *, fate: str = "delivered") -> Step:
        if message_id not in self.steers:
            return Step()
        entry = self.steers[message_id]
        entry.update(fate="consumed" if entry["fate"] == "consumed" else fate, detail=None)
        if message_id in self._steer_announced:
            return Step()
        self._steer_announced.add(message_id)
        return Step(events=[Event("steer.delivered", {"message_id": message_id}, source)])

    def _steer_refused(self, message_id: str, detail: str, source: str, *, fate: str = "refused") -> Step:
        if message_id not in self.steers:
            return Step()
        self._steer_pending.discard(message_id)
        entry = self.steers[message_id]
        if entry["fate"] in ("consumed", "delivered", "unanswered"):
            return Step()
        entry.update(fate=fate, detail=detail)
        return Step(events=[Event("steer.missed", {"message_id": message_id, "why": detail}, source)])
