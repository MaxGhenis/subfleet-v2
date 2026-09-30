"""The runner of one live turn attempt: the I/O around a pure driver (C-26.4 to C-26.6).

One thread per live turn attempt. It tails `<attempt>/stdout` from a byte
offset, feeds each complete line to the driver, sends the driver's frames
through the guardian relay (skipping any frame whose tag the relay log shows
as written), stores approvals, and writes events in batches together with the
attempt's watermark. Operator commands (a stop, a person's answer) arrive on a
queue. After a daemon restart a new runner rebuilds the same state by replaying
stdout from the start; the store ignores events it already has.

The runner never decides a message's fate beyond reporting the driver's
outcome to its owner (`on_outcome`); reconciliation, failover and blocking
are `reconcile.py`'s decisions, which the service applies.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable

from ..policy import CONVERSATION_DEFAULTS
from ..relay import FrameTooLarge, RelayClient, RelayError, read_log
from ..state_files import open_state
from . import attachments as attachment_store
from .claude_turn import ClaudeTurn
from .codex_turn import CodexTurn
from .reconcile import SETTINGS_FRAME, USER_FRAME
from .store import ConversationError, ConversationStore
from .turn import APPROVAL_NEEDED, RUNNING, Frame, Outcome, Step, TurnSpec

FLUSH_S = 0.25
FLUSH_BYTES = 64 * 1024
POLL_S = 0.05
READ_CHUNK = 1 << 20

# Stop escalation (C-24.7, review IR-3, design D-13 revision 3), seconds after the stop request:
# the provider's interrupt at once, then SIGINT (ends a Claude turn; SIGTERM
# would leave it resumable), then closing stdin, then C-5.6 containment. The
# policy's `conversations` section sets them (C-24.7, `policy.CONVERSATION_DEFAULTS`).
SIGINT_AFTER_S = float(CONVERSATION_DEFAULTS["stop_sigint_after_s"])
CLOSE_AFTER_S = float(CONVERSATION_DEFAULTS["stop_close_after_s"])
CONTAIN_AFTER_S = float(CONVERSATION_DEFAULTS["stop_contain_after_s"])
# Background work after the terminal event (D-15): the CLI's own ceiling plus a margin.
AFTER_RESULT_S = float(CONVERSATION_DEFAULTS["after_result_s"])
# Review IR-27: an unacknowledged frame is resent at most this many times, with
# a doubling pause from RESEND_BASE_S, before the relay counts as failed.
# Codex: how long `turn/completed` may lag the thread going idle before the
# idle ends the turn (codex_turn module docstring; observed lag 0.01 s).
IDLE_GRACE_S = 2.0
RESEND_MAX = 5
RESEND_BASE_S = 0.2


@dataclass(frozen=True)
class Clocks:
    """A turn's clocks, from policy `conversations` (C-24.7, C-26.5, C-26.9)."""

    sigint_after_s: float = SIGINT_AFTER_S
    close_after_s: float = CLOSE_AFTER_S
    contain_after_s: float = CONTAIN_AFTER_S
    after_result_s: float = AFTER_RESULT_S
    approval_wait_s: float = float(CONVERSATION_DEFAULTS["approval_wait_s"])

    @classmethod
    def from_policy(cls, policy: dict) -> "Clocks":
        """The loader (`policy.load_policy`) has validated and filled the section;
        a policy map that did not pass through it gets the same defaults."""
        section = {**CONVERSATION_DEFAULTS, **(policy.get("conversations") or {})}
        return cls(sigint_after_s=float(section["stop_sigint_after_s"]),
                   close_after_s=float(section["stop_close_after_s"]),
                   contain_after_s=float(section["stop_contain_after_s"]),
                   after_result_s=float(section["after_result_s"]),
                   approval_wait_s=float(section["approval_wait_s"]))


def make_driver(spec: TurnSpec, read_bytes: Callable[[str], bytes], *,
                frame_recorded: Callable[[str], bool] = lambda tag: False,
                image_path: Callable[[str], str] = lambda path: path):
    return (ClaudeTurn(spec, read_bytes=read_bytes, frame_recorded=frame_recorded) if spec.provider == "claude"
            else CodexTurn(spec, frame_recorded=frame_recorded, image_path=image_path))


class TurnRunner:
    def __init__(self, *, store: ConversationStore, attempt: dict, spec: TurnSpec, conversation_id: str,
                 attempt_dir: Path, control_socket: str,
                 on_outcome: Callable[["TurnRunner"], None],
                 on_contain: Callable[[str], None],
                 clocks: Clocks = Clocks(), clock: Callable[[], float] = time.monotonic,
                 log=None, on_catalog: Callable[[str, str | None, list], None] | None = None,
                 handover: threading.Lock | None = None, ended: bool = False):
        self.store = store
        # A replay of an attempt the job store has ended (`ConversationService._replay_unsettled`):
        # its provider is gone whether or not it left an exit receipt.
        self.ended = ended
        self.attempt = attempt
        self.attempt_id = attempt["attempt_id"]
        self.spec = spec
        self.conversation_id = conversation_id
        self.message_id = spec.message_id
        self.adir = Path(attempt_dir)
        self.control_socket = control_socket
        self.on_outcome = on_outcome
        self.on_contain = on_contain
        self.on_catalog = on_catalog
        self.catalog_reported = False
        self.clocks = clocks
        self.clock = clock
        self.log = log
        self.commands: "queue.Queue[tuple]" = queue.Queue()
        self.driver = make_driver(spec, self._read_attachment,
                                  frame_recorded=lambda tag: tag in self.sent,
                                  image_path=self._attachment_path)
        self.relay = RelayClient(control_socket, timeout_s=30)
        # What the relay applied, from its log; confirmed by the status handshake
        # before anything is sent (`_handshake`, review IR-27). Until then a
        # `pending` record may be a write still in flight, not a failure.
        self._load_log()
        # C-26.8: after a message an earlier runner sent (or began to send), this one
        # does not ask `get_settings`; that includes attempts from before it existed.
        self.replayed_message = USER_FRAME in self.sent
        self.handshake_done_once = False
        self.relay_failed = False
        self.handshaken = False
        self.relay_version: int | None = None
        self.resends = 0                       # consecutive unacknowledged sends of the head frame
        self.resend_at = 0.0
        self.frame_refused: str | None = None  # the tag of a frame over the relay's cap (never sent)
        self.outbox: list = []                 # frames the relay has not acknowledged yet
        self.offset = 0                        # bytes of stdout consumed
        self.partial = b""
        self.batch: list[tuple[str, str, int, str, dict]] = []
        self.batch_bytes = 0
        self.last_flush = clock()
        self.served: dict[str, Any] = {}
        self.stop_at: float | None = None
        self.ended_at: float | None = None
        self.escalated: set[str] = set()
        self.approval_seen: dict[str, float] = {}
        self.outcome_reported = False
        self.stop_reason: str | None = None
        self.late_stop_at: float | None = None
        self.final_text: str | None = None
        self.contained = False
        self.idle_since: float | None = None
        self.finished = threading.Event()
        self.withheld = False
        # C-24.7: the message frame is handed to the relay under this lock, which a
        # person's stop is recorded under too (`ConversationService._handover`), so
        # a stop recorded first is always seen first (`_handover_verdict`).
        self.handover = handover or threading.Lock()
        self.handover_tried = False            # a send of the message frame whose answer was lost
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._stop_on_catch_up = False         # a replayed stop, sent once the replay has caught up (`_run`)
        # The outcome an earlier runner for this attempt recorded (`turn.json`), if it
        # reached one: a replay re-derives the outcome from stdout without what that
        # runner knew, so the record is kept (review of the branch, 2026-09-27).
        recorded = _read_json(self.adir / "turn.json")
        self.recorded = recorded if isinstance(recorded, dict) and recorded.get("state") else None

    # --- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Start the runner's thread; one that cannot start (`RuntimeError` at a
        thread limit) raises, and the runner stays as if never started."""
        thread = threading.Thread(target=self._run, name=f"turn:{self.attempt_id}", daemon=True)
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        """End the loop after the iteration under way (the service's close())."""
        self._stopping.set()

    def join(self, timeout: float) -> bool:
        """Wait up to `timeout` s for the runner's thread; whether it has ended (or never started)."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(max(0.0, timeout))
        return not thread.is_alive()

    def interrupt(self, reason: str = "stopped") -> None:
        self.stop_reason = self.stop_reason or reason
        if not self.handshaken:
            self._stop_on_catch_up = True
        self.commands.put(("interrupt",))

    def withhold(self, reason: str) -> None:
        """Stop the turn without ever writing its message, if it was not written.

        Called before `start`. A message the relay log does not show handed to
        the relay is never written: the driver is stopped as soon as it starts,
        before any stdout is replayed, so replaying a provider's answer to
        `initialize` cannot send it (C-30.4). One the log shows handed over is
        stopped like any other, after the replay (D-13).
        """
        if "user-message" in self.sent:
            self.stop_reason = self.stop_reason or reason
            self._stop_on_catch_up = True
            return
        self.stop_reason = self.stop_reason or reason
        self.withheld = True

    def respond(self, request_id: str, decision: str, message: str | None = None, answers: dict | None = None) -> None:
        self.commands.put(("respond", request_id, decision, message, answers))

    # --- the loop --------------------------------------------------------------

    def _run(self) -> None:
        try:
            message = self.store.message(self.message_id)
            if self.sent.get("interrupt") == "written":
                # Replay (C-26.6): an interrupt an earlier runner wrote reached the
                # provider, so what followed it is read as that runner's driver read it.
                self.driver.interrupted_earlier()
            if message.get("stop_requested_at"):
                self.stop_at = self.clock()
                # A person's stop that came before the message was handed over
                # stands: the message is never written (C-24.7, IR-2).
                self.withheld = self.withheld or "user-message" not in self.sent
                if not self.withheld:
                    # One that came after is this turn's stop too (D-13): a runner
                    # adopted for it had sent no interrupt and settled the stopped
                    # turn as failed. Its interrupt is sent once the replay has read
                    # the stdout there is (below): a driver still behind the provider
                    # would end a delivered turn as stopped before sending. A no-op
                    # when one was written.
                    self.stop_reason = self.stop_reason or "stopped"
                    self._stop_on_catch_up = True
            if self.recorded is not None and USER_FRAME not in self.sent:
                # A durable no-send outcome is also an execution boundary. The
                # first runner may have died after writing it but before closing
                # stdin, and its hold may since have lifted. Starting the driver
                # again would let an old initialization answer send the message.
                self.driver.outcome = Outcome(**{field.name: self.recorded[field.name]
                                                 for field in fields(Outcome) if field.name in self.recorded})
                self.driver.phase = "ended"
                self.stop_reason = self.recorded.get("stop_reason") or self.stop_reason
                for name in ("accepted", "answered", "limited", "served_model"):
                    setattr(self.driver, name, getattr(self.driver.outcome, name))
                self._apply(Step(frames=[Frame("close", "close")], outcome=self.driver.outcome))
            else:
                self._apply(self.driver.start())
            withhold_pending = self.withheld
            while not self._stopping.is_set():
                if not (self.handshaken or self.relay_failed or self._process_gone()):
                    if self.stop_reason is not None:
                        # Commands wait for reconstruction, but escalation must
                        # start now rather than after all status retries expire.
                        if self.stop_at is None:
                            self.stop_at = self.clock()
                        self._stop_on_catch_up = True
                    # The initial log may precede an older runner's in-flight
                    # handover. Until status confirms it, replay must not rebuild
                    # that frame (or fail because its image has since disappeared).
                    # A gone provider has no live relay to confirm; its final log
                    # and stdout are sufficient for reconciliation.
                    self._send_outbox()
                    if not (self.handshaken or self.relay_failed or self._process_gone()):
                        self._timers()
                        if self._flush_due():
                            self._flush()
                        time.sleep(POLL_S)
                        continue
                if withhold_pending:
                    withhold_pending = False
                    if USER_FRAME in self.sent and self.recorded is None:
                        # The first log may have missed an in-flight message. A
                        # confirmed handover makes this a delivered turn's stop,
                        # applied after reconstruction, never stopped-before-send.
                        self.withheld = False
                        self.stop_reason = self.stop_reason or "stopped"
                        self._stop_on_catch_up = True
                    else:
                        self.stop_at = self.stop_at or self.clock()
                        self._apply(self.driver.interrupt())
                progressed = self._read_stdout()
                if self._stop_on_catch_up and not progressed:
                    self._stop_on_catch_up = False
                    self.commands.put(("interrupt",))
                progressed |= self._drain_commands()
                self._send_outbox()
                self._timers()
                if self._flush_due():
                    self._flush()
                if self._process_gone():
                    while self._read_stdout():      # all of it: a replay can be far behind (C-26.6)
                        pass
                    if self.driver.outcome is None:
                        self._apply(self.driver.eof(self.offset))
                    self._flush()
                    self._report()
                    break
                if not progressed:
                    time.sleep(POLL_S)
        except Exception as exc:          # a runner defect must not take the daemon down
            self._failed(exc)
        finally:
            try:
                self._flush()
            except Exception as exc:
                self._failed(exc)
            try:
                self.relay.close()
            finally:
                self.finished.set()

    def _failed(self, exc: Exception) -> None:
        if not self.log:
            return
        if isinstance(exc, ConversationError) and exc.reason == "store-closed":
            # Its service closed while it was still going: what it did not record, a
            # later daemon's runner replays from stdout (C-26.6), whether it adopts the
            # attempt live or replays it ended (`ConversationService._replay_unsettled`).
            self.log.info("turn runner %s stopped: its service closed", self.attempt_id)
        else:
            self.log.error("turn runner %s failed: %s: %s", self.attempt_id, type(exc).__name__, exc)

    def _process_gone(self) -> bool:
        return self.ended or (self.adir / "exit.json").exists()

    def _read_stdout(self) -> bool:
        path = self.adir / "stdout"
        try:
            with open_state(path) as stream:      # a FIFO here fails the runner, never holds it
                stream.seek(self.offset + len(self.partial))
                chunk = stream.read(READ_CHUNK)
        except FileNotFoundError:
            return False
        if not chunk:
            return False
        data = self.partial + chunk
        start = 0
        while True:
            end = data.find(b"\n", start)
            if end < 0:
                break
            line = data[start:end]
            line_offset = self.offset
            self.offset += len(line) + 1
            if line.strip():
                self._apply(self.driver.feed(line, line_offset))
                self._sync_answers()
            start = end + 1
        self.partial = data[start:]
        return True

    def _drain_commands(self) -> bool:
        worked = False
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return worked
            worked = True
            if command[0] == "interrupt":
                if self.stop_at is None:
                    self.stop_at = self.clock()
                if self._stop_on_catch_up:
                    # Dropped: `_run` queues a fresh interrupt once the replay
                    # has caught up; the stop's clock has already started.
                    continue
                self._apply(self.driver.interrupt())
            elif command[0] == "respond":
                _, request_id, decision, message, answers = command
                try:
                    if isinstance(self.driver, ClaudeTurn):
                        step = self.driver.respond(request_id, decision, message, answers=answers)
                    else:
                        step = self.driver.respond(request_id, decision, message)
                except ValueError as exc:
                    if self.log:
                        self.log.warning("approval %s on %s refused by the driver: %s", request_id, self.attempt_id, exc)
                    continue
                self._apply(step)

    def _sync_answers(self) -> None:
        """Replay: an approval a person already answered is answered again in the
        driver; its frame is already in the relay log and is not sent twice."""
        for request_id in list(getattr(self.driver, "pending", {})):
            row = self.store.one("SELECT decision_json FROM approvals WHERE attempt_id=? AND provider_request_id=? "
                                 "AND state='answered'", (self.attempt_id, request_id))
            if row and row["decision_json"]:
                decision = json.loads(row["decision_json"])
                self.commands.put(("respond", request_id, decision.get("decision"), decision.get("message"),
                                   decision.get("answers")))

    # --- applying a driver step ------------------------------------------------

    def _apply(self, step: Step) -> None:
        for event in step.events:
            source = "command" if event.source.startswith("cmd:") else "stdout"
            self.batch.append((source, event.source, 0, event.kind, event.data))
            self.batch_bytes += len(json.dumps(event.data, default=str))
            if event.kind == "served":
                self.served.update({k: v for k, v in event.data.items() if v is not None})
            if event.kind == "text" and event.data.get("text"):
                self.final_text = event.data["text"]
            if event.kind == "accepted":
                self._flush()
                self.store.set_state(self.message_id, RUNNING, expect=("starting", "waiting"),
                                     turn_ref=event.data.get("turn_id") or self.message_id)
        for frame in step.frames:
            self.outbox.append(frame)
        for approval in step.approvals:
            self._flush()
            self.store.add_approval(message_id=self.message_id, conversation_id=self.conversation_id,
                                    attempt_id=self.attempt_id, provider_request_id=approval.provider_request_id,
                                    kind=approval.kind, request=approval.request, display=approval.summary,
                                    options=approval.options)
            if approval.kind != "question":
                # C-26.9: a question (AskUserQuestion) waits for the person with no
                # limit; only a tool approval stops its turn after approval_wait_s.
                self.approval_seen.setdefault(approval.provider_request_id, self.clock())
            self.store.set_state(self.message_id, APPROVAL_NEEDED, expect=("running", "starting"))
        if step.resolved:
            self.store.withdraw_approvals(attempt_id=self.attempt_id, provider_request_ids=list(step.resolved))
            for rid in step.resolved:
                self.approval_seen.pop(rid, None)
            if not self.store.approvals(message_id=self.message_id) and self.driver.outcome is None:
                self.store.set_state(self.message_id, RUNNING, expect=("approval-needed",))
        if step.outcome is not None:
            self._flush()
            self.ended_at = self.clock()
            self._write_outcome()
        catalog = getattr(self.driver, "catalog", None)
        if catalog is not None and not self.catalog_reported and self.on_catalog is not None:
            self.catalog_reported = True
            try:
                self.on_catalog(self.spec.provider, self.attempt.get("lane_id"), catalog)
            except Exception as exc:         # a catalog record never stops a turn
                if self.log:
                    self.log.warning("model catalog from %s not recorded: %s", self.attempt_id, exc)
        self._send_outbox()

    def _load_log(self) -> bool:
        """Read `stdin.jsonl`; True when it shows a stdin frame that was not fully
        written (a signal that found no child is not a relay failure; C-26.4)."""
        logged = read_log(self.adir / "stdin.jsonl")
        self.logged = len(logged)
        self.sent: dict[str, str] = {r["tag"]: r["status"] for r in logged if r.get("tag")}
        self.next_seq = len(logged) + 1
        return any(r["status"] != "written" and r.get("op") != "signal" for r in logged)

    def _handshake(self) -> bool:
        """Review IR-27: before this runner sends or replays anything, ask the relay
        what it applied. The answer comes after any frame the previous daemon left
        in flight has been written and logged, so the log read afterwards is final
        and a frame shown `pending` a moment earlier is not mistaken for a failure.
        It also carries the relay's frame cap. A relay older than version 2 has
        no status; its log is read as before."""
        if self.handshaken:
            return True
        try:
            status = self.relay.status()
        except RelayError as exc:
            self._unacknowledged(f"relay status: {exc}")
            return False                    # asked again later; nothing is sent meanwhile
        self.resends = 0
        unwritten = self._load_log()
        # C-26.8 (review of b0b3f153): the first handshake's log is final, so a message
        # the previous daemon had in flight counts as sent by an earlier runner. A later
        # handshake (after a lost answer) finds this runner's own message, not one.
        if not self.handshake_done_once:
            self.replayed_message = self.replayed_message or USER_FRAME in self.sent
        self.handshake_done_once = True
        self.handshaken = True
        if status is not None:
            self.relay_version = status.get("version")
            if status["applied"] != self.logged:
                # The log and the relay disagree: nothing is sent on a guess.
                self._relay_lost(f"the relay applied {status['applied']} frames, its log shows {self.logged}")
                return True
        if unwritten:
            # A frame the log shows unwritten ended relaying for good (C-26.4); a
            # runner rebuilt after a restart stops the turn as its predecessor did.
            self._relay_lost("the relay log shows a frame that was not written")
        return True

    def _unacknowledged(self, why: str) -> None:
        """IR-27: the relay did not answer. The same number is sent again (a
        duplicate is recognised by its hash) after a doubling pause, at most
        RESEND_MAX times; then the relay counts as failed."""
        self.resends += 1
        if self.resends > RESEND_MAX:
            self._relay_lost(f"{why}; no answer after {RESEND_MAX} retries")
        else:
            self.resend_at = self.clock() + RESEND_BASE_S * 2 ** (self.resends - 1)

    def _send_outbox(self) -> None:
        while self.outbox:
            if self.relay_failed:
                self.outbox.clear()
                return
            if self.resends and self.clock() < self.resend_at:
                return                          # IR-27: the next try waits its turn
            if not self._handshake():
                return
            frame = self.outbox[0] if self.outbox else None
            if frame is None or self.relay_failed:
                self.outbox.clear()
                return
            if self.sent.get(frame.tag) == "written":
                self.outbox.pop(0)            # replayed: already delivered to the provider
                continue
            if frame.tag == SETTINGS_FRAME and self.replayed_message:
                self.outbox.pop(0)            # C-26.8: an earlier runner sent the message and did
                continue                      # not ask (or its ask is lost); not worth a late write
            if self.recorded is not None and (frame.tag == USER_FRAME or frame.tag.startswith("approval:")):
                # Reconstruct a delivered turn's earlier state, but a saved
                # terminal outcome cannot authorize new provider work: the
                # message, or a person's answer whose write the crash interrupted.
                # What stops work still goes: the driver's interrupt (a model
                # mismatch's, C-26.8), the close and the escalation's signals.
                self.outbox.pop(0)
                continue
            if frame.tag == USER_FRAME:
                with self.handover:
                    verdict = self._handover_verdict()
                    going = verdict == "send" and self._transmit(frame)
                if verdict == "withdraw":
                    self._withdraw()
                elif verdict == "wait" or not going:
                    return
                continue                        # `again`: the relay's log now says what it holds
            if not self._transmit(frame):
                return

    def _transmit(self, frame: Frame) -> bool:
        """Send the head frame and act on the answer; whether the outbox may go on."""
        try:
            if frame.op == "signal":
                ack = self.relay.send(self.next_seq, "signal", tag=frame.tag, sig=frame.line)
            else:
                ack = self.relay.send(self.next_seq, frame.op, line=frame.line, tag=frame.tag)
        except FrameTooLarge as exc:
            self._refuse_frame(frame, exc)
            return True
        except RelayError as exc:
            if frame.tag == USER_FRAME:
                self.handover_tried = True    # the relay may have logged it: `_handover_verdict` asks
            self._unacknowledged(f"frame {frame.tag}: {exc}")
            return False
        self.resends = 0
        if ack.ok:
            self.sent[frame.tag] = "written"
            self.next_seq += 1
            self.outbox.pop(0)
            return True
        if (ack.error == "closed" and frame.op == "close") or (ack.error == "no-child" and frame.op == "signal"):
            # Stdin already closed, or the child already exited: nothing to do.
            if ack.error == "no-child":
                self.next_seq += 1
            self.outbox.pop(0)
            return True
        # conflict, failed, closed, gap, peer-refused: nothing more is written.
        self._relay_lost(f"relay refused frame {frame.tag}: {ack.error}")
        return False

    def _handover_verdict(self) -> str:
        """Whether the message frame at the head of the outbox may be handed over
        (C-24.7): `send`; `withdraw` (a stop came first: it is never written);
        `wait` (the relay cannot say yet; nothing is sent); or `again` (the relay's
        log, read afresh, holds the frame or ended relaying, which the outbox loop
        acts on as for any frame). Called under `handover`.

        A stop is a person's recorded in the store (read here, not from a copy:
        `turn.interrupt` and `message.cancel` record theirs under the same lock) or
        one this runner was asked for (`interrupt`: the daemon's, a timeout's). A
        send whose answer was lost may have reached the relay, so a stop after it
        withdraws the frame only when the relay's log, read after a fresh status
        answer (which comes after any frame in flight is applied), does not hold
        it; when it does, the message was handed over first and D-13 stops it."""
        stopped = self.stop_reason is not None
        if not stopped:
            row = self.store.one("SELECT stop_requested_at FROM messages WHERE message_id=?", (self.message_id,))
            stopped = bool(row and row["stop_requested_at"])
        if not stopped:
            return "send"
        if self.handover_tried:
            self.handshaken = False
            if not self._handshake():
                return "wait"
            if self.relay_failed or USER_FRAME in self.sent:
                return "again"                # handed over (or lost): not this runner's to withdraw
        return "withdraw"

    def _withdraw(self) -> None:
        """A stop came before the message frame was handed over: it and every frame
        queued behind it are dropped, never sent, and the driver ends the turn as
        stopped before sending (C-24.7, IR-2)."""
        self.outbox.clear()
        self.withheld = True
        self.stop_reason = self.stop_reason or "stopped"
        self.stop_at = self.stop_at or self.clock()
        self._apply(self.driver.withdraw())

    def _refuse_frame(self, frame: Frame, exc: FrameTooLarge) -> None:
        """A frame over the relay's cap is never sent (IR-27). Nothing after it can
        be sent in order either, so stdin is closed: the provider ends at EOF and
        the turn is reconciled from what the relay log shows (C-24.6)."""
        if self.log:
            self.log.warning("turn %s frame %s not sent: %s", self.attempt_id, frame.tag, exc)
        self.frame_refused = self.frame_refused or frame.tag
        self.outbox[:] = [] if frame.op == "close" else [Frame("close", "close")]

    def _relay_lost(self, why: str) -> None:
        """Nothing more can be written (C-26.4). A turn with no terminal event is
        then stopped as a stop request would stop it; the steps that need the
        relay are skipped, so containment ends it (D-13, design §6)."""
        self.relay_failed = True
        self.outbox.clear()
        if self.log:
            self.log.warning("turn %s relay failed: %s", self.attempt_id, why)
        if self.driver.outcome is None and self.stop_at is None:
            self.stop_reason = self.stop_reason or "relay-failed"
            self.stop_at = self.clock()

    # --- time ------------------------------------------------------------------

    def _timers(self) -> None:
        now = self.clock()
        if getattr(self.driver, "idle_pending", False) and self.driver.outcome is None:
            if self.idle_since is None:
                self.idle_since = now
            elif now - self.idle_since >= IDLE_GRACE_S:
                self._apply(self.driver.settle_idle())
        else:
            self.idle_since = None
        clocks = self.clocks
        if self.stop_at is not None and self.driver.outcome is None:
            waited = now - self.stop_at
            if waited >= clocks.sigint_after_s and "sigint" not in self.escalated:
                self.escalated.add("sigint")
                self.outbox.append(Frame("signal:int", "signal", "INT"))
            if waited >= clocks.close_after_s and "close" not in self.escalated:
                self.escalated.add("close")
                self.outbox.append(Frame("close", "close"))
            if waited >= clocks.contain_after_s and "contain" not in self.escalated:
                self.escalated.add("contain")
                self.on_contain(self.attempt_id)
        if self.ended_at is not None and now - self.ended_at >= clocks.after_result_s and "late" not in self.escalated:
            # D-15: background work outlived the ceiling; stop it the same way.
            self.escalated.add("late")
            self.outbox.append(Frame("signal:int:late", "signal", "INT"))
            self.late_stop_at = now
        late = self.late_stop_at
        if (late is not None and now - late >= clocks.contain_after_s - clocks.sigint_after_s
                and "late-contain" not in self.escalated):
            self.escalated.add("late-contain")
            self.on_contain(self.attempt_id)
        for request_id, since in list(self.approval_seen.items()):
            if now - since >= clocks.approval_wait_s and self.driver.outcome is None:
                # IR-8: the person did not answer in time. Subfleet does not answer
                # the approval (C-27.2); it stops the turn.
                self.approval_seen.pop(request_id, None)
                self.stop_reason = self.stop_reason or "approval-timeout"
                self.commands.put(("interrupt",))

    # --- persistence -----------------------------------------------------------

    def _flush_due(self) -> bool:
        return bool(self.batch) and (self.batch_bytes >= FLUSH_BYTES or self.clock() - self.last_flush >= FLUSH_S)

    def _flush(self) -> None:
        if not self.batch:
            self.last_flush = self.clock()
            return
        batch, self.batch, self.batch_bytes = self.batch, [], 0
        self.store.append_events(conversation_id=self.conversation_id, message_id=self.message_id,
                                 attempt_id=self.attempt_id, events=batch, stdout_offset=self.offset,
                                 stdin_seq=self.next_seq - 1)
        self.last_flush = self.clock()

    def _write_outcome(self) -> None:
        recorded = self.recorded
        if recorded is not None:
            # A runner before this one reached the turn's outcome, with what only it
            # knew (a withhold, a stop's reason, an idle settle): that record stands.
            # Only what stdout said after the outcome (C-24.8) is added.
            data = {**recorded, "terminal_after_end": bool(recorded.get("terminal_after_end"))
                    or bool(getattr(self.driver, "terminal_after_end", False))}
            from ..guardian import atomic_publish
            with self.store.writing():
                atomic_publish(self.adir / "turn.json", (json.dumps(data, sort_keys=True) + "\n").encode())
            return
        outcome = self.driver.outcome
        data = {**asdict(outcome), "served": self.served, "turn_id": getattr(self.driver, "turn_id", None),
                "stop_reason": self.stop_reason, "final_text": self.final_text,
                "native_session_id": getattr(self.driver, "thread_id", None) or self.spec.native_session_id
                or self.spec.new_session_id, "relay_failed": self.relay_failed,
                "user_frame_written": self.sent.get("user-message") == "written",
                "frame_refused": self.frame_refused, "relay_version": self.relay_version,
                "terminal_after_end": bool(getattr(self.driver, "terminal_after_end", False))}
        from ..guardian import atomic_publish
        with self.store.writing():          # never after its service closed (C-25.3)
            atomic_publish(self.adir / "turn.json", (json.dumps(data, sort_keys=True) + "\n").encode())

    def _report(self) -> None:
        if not self.outcome_reported:
            self.outcome_reported = True
            if self.driver.outcome is not None:
                # What stdout said after the outcome (a terminal event after the
                # driver's own stop) is final only now (C-24.8, `reconcile.settle`).
                self._write_outcome()
            self.on_outcome(self)

    def _read_attachment(self, path: str) -> bytes:
        image = next(image for image in self.spec.images if image.path == path)
        # The digest is the identity; a persisted manifest's absolute path may
        # predate a move of the state root. Verify and use bytes from one descriptor.
        return attachment_store.read_verified(self.store, image.sha256)[0]

    def _attachment_path(self, path: str) -> str:
        image = next(image for image in self.spec.images if image.path == path)
        data, ext = attachment_store.read_verified(self.store, image.sha256)
        with self.store.writing():
            # Publish directly under the existing attempt directory. Accepting
            # an `images` subdirectory could follow a replaced directory symlink.
            target = self.adir / f"image-{image.sha256}.{ext}"
            attachment_store._copy(data, target, mode=0o400)
            attachment_store._sync_directory(self.adir)
        return str(target)


def _read_json(path: Path) -> dict | None:
    try:
        with open_state(path) as source:
            return json.load(source)
    except (OSError, ValueError):
        return None
