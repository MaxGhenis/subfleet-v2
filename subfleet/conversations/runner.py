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
belong to the service.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from ..policy import CONVERSATION_DEFAULTS
from ..relay import RelayClient, RelayError, read_log
from . import attachments as attachment_store
from .claude_turn import ClaudeTurn
from .codex_turn import CodexTurn
from .store import ConversationError, ConversationStore
from .turn import APPROVAL_NEEDED, RUNNING, Frame, Step, TurnSpec

FLUSH_S = 0.25
FLUSH_BYTES = 64 * 1024
POLL_S = 0.05
READ_CHUNK = 1 << 20

# Stop escalation (design D-13, review IR-3), seconds after the stop request:
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


def make_driver(spec: TurnSpec, read_bytes: Callable[[str], bytes]):
    return ClaudeTurn(spec, read_bytes=read_bytes) if spec.provider == "claude" else CodexTurn(spec)


class TurnRunner:
    def __init__(self, *, store: ConversationStore, attempt: dict, spec: TurnSpec, conversation_id: str,
                 attempt_dir: Path, control_socket: str,
                 on_outcome: Callable[["TurnRunner"], None],
                 on_contain: Callable[[str], None],
                 clocks: Clocks = Clocks(), clock: Callable[[], float] = time.monotonic,
                 log=None, on_catalog: Callable[[str, str | None, list], None] | None = None):
        self.store = store
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
        self.driver = make_driver(spec, self._read_attachment)
        self.relay = RelayClient(control_socket, timeout_s=30)
        logged = read_log(self.adir / "stdin.jsonl")
        self.sent: dict[str, str] = {r["tag"]: r["status"] for r in logged if r.get("tag")}
        self.next_seq = len(logged) + 1
        # A signal that found no child is not a relay failure; a stdin frame that
        # was not fully written is (C-26.4).
        self.relay_failed = any(r["status"] != "written" and r.get("op") != "signal" for r in logged)
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
        self.finished = threading.Event()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    # --- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"turn:{self.attempt_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()

    def interrupt(self, reason: str = "stopped") -> None:
        self.stop_reason = self.stop_reason or reason
        self.commands.put(("interrupt",))

    def respond(self, request_id: str, decision: str, message: str | None = None, answers: dict | None = None) -> None:
        self.commands.put(("respond", request_id, decision, message, answers))

    # --- the loop --------------------------------------------------------------

    def _run(self) -> None:
        try:
            message = self.store.message(self.message_id)
            if message.get("stop_requested_at"):
                self.stop_at = self.clock()
            self._apply(self.driver.start())
            while not self._stopping.is_set():
                progressed = self._read_stdout()
                progressed |= self._drain_commands()
                self._send_outbox()
                self._timers()
                if self._flush_due():
                    self._flush()
                if self._process_gone():
                    self._read_stdout()
                    if self.driver.outcome is None:
                        self._apply(self.driver.eof(self.offset))
                    self._flush()
                    self._report()
                    break
                if not progressed:
                    time.sleep(POLL_S)
        except Exception as exc:          # a runner defect must not take the daemon down
            if self.log:
                self.log.error("turn runner %s failed: %s: %s", self.attempt_id, type(exc).__name__, exc)
        finally:
            self._flush()
            self.relay.close()
            self.finished.set()

    def _process_gone(self) -> bool:
        return (self.adir / "exit.json").exists()

    def _read_stdout(self) -> bool:
        path = self.adir / "stdout"
        try:
            with open(path, "rb") as stream:
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

    def _send_outbox(self) -> None:
        while self.outbox:
            frame = self.outbox[0]
            if self.sent.get(frame.tag) == "written":
                self.outbox.pop(0)            # replayed: already delivered to the provider
                continue
            if self.relay_failed:
                self.outbox.clear()
                return
            try:
                if frame.op == "signal":
                    ack = self.relay.send(self.next_seq, "signal", tag=frame.tag, sig=frame.line)
                else:
                    ack = self.relay.send(self.next_seq, frame.op, line=frame.line, tag=frame.tag)
            except RelayError:
                return                          # retried with the same number next pass
            if ack.ok:
                self.sent[frame.tag] = "written"
                self.next_seq += 1
                self.outbox.pop(0)
                continue
            if (ack.error == "closed" and frame.op == "close") or (ack.error == "no-child" and frame.op == "signal"):
                # Stdin already closed, or the child already exited: nothing to do.
                if ack.error == "no-child":
                    self.next_seq += 1
                self.outbox.pop(0)
                continue
            # conflict, failed, closed, gap, peer-refused: nothing more is written.
            self.relay_failed = True
            if self.log:
                self.log.warning("turn %s relay refused frame %s: %s", self.attempt_id, frame.tag, ack.error)
            self.outbox.clear()
            return

    # --- time ------------------------------------------------------------------

    def _timers(self) -> None:
        now = self.clock()
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
        outcome = self.driver.outcome
        data = {**asdict(outcome), "served": self.served, "turn_id": getattr(self.driver, "turn_id", None),
                "stop_reason": self.stop_reason, "final_text": self.final_text,
                "native_session_id": getattr(self.driver, "thread_id", None) or self.spec.native_session_id
                or self.spec.new_session_id, "relay_failed": self.relay_failed,
                "user_frame_written": self.sent.get("user-message") == "written"}
        from ..guardian import atomic_publish
        atomic_publish(self.adir / "turn.json", (json.dumps(data, sort_keys=True) + "\n").encode())

    def _report(self) -> None:
        if not self.outcome_reported:
            self.outcome_reported = True
            self.on_outcome(self)

    def _read_attachment(self, path: str) -> bytes:
        # Images are read at frame time from the daemon's own copy (C-28.1).
        return Path(path).read_bytes()


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
