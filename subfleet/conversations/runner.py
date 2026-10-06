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
from contextlib import ExitStack
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
from .store import ConversationError, ConversationStore, TitleUpdate
from .turn import APPROVAL_NEEDED, COMPLETE, RUNNING, Event, Frame, Image, Outcome, Step, TurnSpec
from .titles import CLAIMED, OPEN, PIPE_FLOOR, SessionTitle, TITLE_CANCEL_FRAME, TITLE_FRAME, request_line

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
STEER_GRACE_S = 15.0
RESEND_MAX = 5
RESEND_BASE_S = 0.2
# A Claude steer's fates that come from the provider's own rows (a lifecycle, an echo,
# an interrupt's receipt): each shows the provider read the steer's line from stdin.
STEER_READ_FATES = ("delivered", "consumed", "cancelled", "refused")
# While the title holds the close (titles.py): what only the store knows that ends the
# hold. Another message of the conversation queued, waiting or steering, or a name a
# person gave it meanwhile. (A person's Stop ends the title before it is recorded.)
TITLE_WATCH_SQL = ("SELECT EXISTS (SELECT 1 FROM messages WHERE conversation_id=? AND message_id<>? "
                   "AND state IN ('queued','waiting','steering')) AS waiting, "
                   "(SELECT title_source FROM conversations WHERE conversation_id=?) AS source")


@dataclass(frozen=True)
class Clocks:
    """A turn's clocks, from policy `conversations` (C-24.7, C-26.5, C-26.9)."""

    sigint_after_s: float = SIGINT_AFTER_S
    close_after_s: float = CLOSE_AFTER_S
    contain_after_s: float = CONTAIN_AFTER_S
    after_result_s: float = AFTER_RESULT_S
    approval_wait_s: float | None = CONVERSATION_DEFAULTS["approval_wait_s"]

    @classmethod
    def from_policy(cls, policy: dict) -> "Clocks":
        """The loader (`policy.load_policy`) has validated and filled the section;
        a policy map that did not pass through it gets the same defaults."""
        section = {**CONVERSATION_DEFAULTS, **(policy.get("conversations") or {})}
        return cls(sigint_after_s=float(section["stop_sigint_after_s"]),
                   close_after_s=float(section["stop_close_after_s"]),
                   contain_after_s=float(section["stop_contain_after_s"]),
                   after_result_s=float(section["after_result_s"]),
                   approval_wait_s=(None if section["approval_wait_s"] is None
                                    else float(section["approval_wait_s"])))


def make_driver(spec: TurnSpec, read_bytes: Callable[[Image], bytes], *,
                frame_recorded: Callable[[str], bool] = lambda tag: False,
                image_path: Callable[[Image], str] = lambda image: image.path):
    return (ClaudeTurn(spec, read_bytes=read_bytes, frame_recorded=frame_recorded) if spec.provider == "claude"
            else CodexTurn(spec, frame_recorded=frame_recorded, image_path=image_path))


class TurnRunner:
    def __init__(self, *, store: ConversationStore, attempt: dict, spec: TurnSpec, conversation_id: str,
                 attempt_dir: Path, control_socket: str,
                 on_outcome: Callable[["TurnRunner"], None],
                 on_contain: Callable[[str], None],
                 clocks: Clocks = Clocks(), clock: Callable[[], float] = time.monotonic,
                 log=None, on_catalog: Callable[[str, str | None, list], None] | None = None,
                 handover: threading.Lock | None = None, ended: bool = False,
                 handover_for: Callable[[str], Any] | None = None):
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
        # The first turn's title (titles.py): asked after the reply, at a quiescent point.
        self.title = SessionTitle(conversation_id, spec.message_id)
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
        self.optional_ack_lost = False         # a title write went unanswered: resynchronize first
        self.wrote_bytes: dict[str, int] = {}  # tag → bytes of each stdin line this runner wrote
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
        self.handover_for = handover_for or (lambda mid: threading.Lock())
        self.handover_tried = False            # a send of the message frame whose answer was lost
        self._steer_tried: set[str] = set()
        self.replay_caught_up = False
        self._replay_steers: list[str] = []
        self._steers_restored = False
        self.steer_wait_since: float | None = None
        self._steer_watch_seen = 0             # the driver's `steer_watch` the clock above belongs to
        self._discarding_steers = False
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
        # The runner's own record of the stop, first: from it on no title is claimed
        # or written (`_title_quiescent`, `_title_may_write`), and the command frees a
        # stdin close the title holds (`_drain_commands`).
        self.stop_reason = self.stop_reason or reason
        if not self.handshaken:
            self._stop_on_catch_up = True
        self.commands.put(("interrupt",))

    def end_title(self, why: str) -> None:
        """No title write begins from now on (titles.py); one already begun goes on,
        and the stdin close it held follows. Safe on any thread and never waits on
        I/O or a lock held across it: a person's Stop calls it before the stop is
        recorded (`ConversationService._interrupt`), so no title write begins after
        a recorded stop even before the runner hears of it."""
        self.title.end(why)

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

    @property
    def steerable(self) -> bool:
        """The request handler's fast check; handover rechecks on the runner thread."""
        return (self.replay_caught_up and not self.ended and not self.finished.is_set()
                and not self.withheld and not self.relay_failed and self.stop_reason is None
                and self.stop_at is None and "close" not in self.sent
                and self.driver.outcome is None and bool(getattr(self.driver, "steerable", False)))

    def steer(self, message_id: str) -> None:
        self.commands.put(("steer", message_id))

    def steer_written(self, message_id: str) -> bool:
        """Cancel's read-only handover check, called under this message's lock.

        An intent or a send with a lost receipt may already have reached the pipe;
        only the runner's fresh handshake can clear that uncertainty.
        """
        tag = f"steer:{message_id}"
        return (message_id in self._steer_tried or tag in self.sent
                or any(row.get("tag") == tag for row in read_log(self.adir / "stdin.jsonl")))

    # --- the loop --------------------------------------------------------------

    def _run(self) -> None:
        try:
            message = self.store.message(self.message_id)
            self._restore_steers()
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
                if not progressed and not self.replay_caught_up:
                    self.replay_caught_up = True
                    for mid in self._replay_steers:
                        self.commands.put(("steer", mid))
                    self._replay_steers.clear()
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
                    # Ended-attempt replay may never reach an empty read in the main
                    # loop. Its unwritten claims must still settle as missed.
                    self._discard_steers("provider-ended")
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
                self.title.receive(line)
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
            if self.title.holding:
                # A stop, an answer or a steer wants the conversation: the stdin close
                # the title held goes next (a steer after the result is missed, queued).
                self.end_title("command")
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
            elif command[0] == "steer":
                self._steer(command[1])

    def _restore_steers(self) -> None:
        """Prime written commands before stdout replay, and defer unwritten ones.

        Tags, including incomplete relay intents, are durable delivery evidence.
        A written command is never reconstructed by calling `steer`: that could
        write it twice or reject it before replay reaches the active phase.
        """
        if self._steers_restored:
            return
        self._steers_restored = True
        for tag in self.sent:
            if tag.startswith("steer:"):
                mid = tag.removeprefix("steer:")
                self.driver.restore_steer(mid, frame="written")
                self._apply(Step(events=[Event("steer.sent", {"message_id": mid}, f"cmd:steer:{mid}")]), send=False)
        for row in self.store.query("SELECT message_id FROM messages WHERE state='steering' "
                                    "AND state_reason=? ORDER BY seq", (f"steer:{self.message_id}",)):
            mid = row["message_id"]
            if f"steer:{mid}" not in self.sent:
                self._replay_steers.append(mid)
        # Cancel removes the current binding. Its durable claim change still
        # identifies a steer withdrawn before the queued command was drained.
        for row in self.store.query(
                "SELECT DISTINCT m.message_id FROM messages m JOIN changes c USING(message_id) "
                "WHERE m.state='cancelled' AND c.state='steering' AND c.state_reason=?",
                (f"steer:{self.message_id}",)):
            if f"steer:{row['message_id']}" not in self.sent:
                self._miss_steer(row["message_id"], "cancelled-before-handover")

    def _steer(self, message_id: str) -> None:
        row = self.store.find_message(message_id)
        if row and row["state"] == "cancelled" and row["conversation_id"] == self.conversation_id:
            self._miss_steer(message_id, "cancelled-before-handover")
            return
        if not row or row["state"] != "steering" or row["state_reason"] != f"steer:{self.message_id}":
            return
        if f"steer:{message_id}" in self.sent:
            if message_id not in self.driver.steers:
                self.driver.restore_steer(message_id, frame="written")
            return
        previous = self.driver.steers.get(message_id)
        if previous and previous.get("fate") in ("refused", "cancelled"):
            # A second explicit request after a missed claim must not strand the
            # message in steering: driver commands are idempotent by message id.
            self._miss_steer(message_id, previous.get("detail") or "previously-refused")
            return
        if not self.steerable:
            self._miss_steer(message_id, "not-steerable")
            return
        try:
            images = []
            for sha in row["attachments"]:
                path, media = attachment_store.check(self.store, sha)
                images.append(Image(sha, media, path))
            step = self.driver.steer(message_id, self.store.message_text(row), tuple(images))
        except Exception as exc:
            # C-24.9: whatever fails in building or taking a steer (its text or an
            # image unreadable, a driver defect) sends that steer back to the queue.
            # It never takes its host's runner down: Stop, Esc and the queue behind
            # the host all go through this thread.
            if self.log and not isinstance(exc, (OSError, ValueError, ConversationError)):
                self.log.warning("steer %s on %s failed: %s: %s", message_id, self.attempt_id,
                                 type(exc).__name__, exc)
            self._miss_steer(message_id, f"input-unavailable: {str(exc) or type(exc).__name__}")
            return
        self._apply(step)

    def _requeue_steer(self, message_id: str, why: str) -> None:
        if self.store.set_state(message_id, "queued", reason=f"steer-missed: {why}", expect=("steering",)):
            event = Event("steer.missed", {"message_id": message_id, "why": why}, f"cmd:steer-missed:{message_id}")
            self.batch.append(("command", event.source, 0, event.kind, event.data))
            self.batch_bytes += len(json.dumps(event.data))

    def _miss_steer(self, message_id: str, why: str) -> None:
        self._apply(self.driver.drop_steer(message_id, why), send=False)
        row = self.store.find_message(message_id)
        if row and row["state"] == "cancelled" and not self.steer_written(message_id):
            self.driver.steers[message_id].update(frame="unsent", fate="cancelled", detail="cancelled-before-handover")
        self._requeue_steer(message_id, why)

    def _discard_steers(self, why: str) -> None:
        """Record every definitely unwritten command before dropping its frame."""
        if self._discarding_steers:
            return
        mids = set(self._replay_steers)
        mids.update(frame.tag.removeprefix("steer:") for frame in self.outbox if frame.tag.startswith("steer:"))
        mids.update(mid for mid, fact in getattr(self.driver, "steers", {}).items()
                    if fact.get("frame") == "unsent" and fact.get("fate") == "unknown")
        self.outbox[:] = [frame for frame in self.outbox if not frame.tag.startswith("steer:")]
        self._replay_steers.clear()
        self._discarding_steers = True
        try:
            for mid in mids:
                if self.steer_written(mid):
                    self.driver.steer_written(mid)
                else:
                    self._miss_steer(mid, why)
        finally:
            self._discarding_steers = False

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

    def _apply(self, step: Step, *, send: bool = True) -> None:
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
            if event.kind in ("steer.refused", "steer.missed") and source == "command":
                self.store.set_state(event.data["message_id"], "queued",
                                     reason=f"steer-missed: {event.data.get('why') or 'provider-refused'}",
                                     expect=("steering",))
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
                # limit; a tool approval stops its turn only if policy sets a limit.
                self.approval_seen.setdefault(approval.provider_request_id, self.clock())
            self.store.set_state(self.message_id, APPROVAL_NEEDED, expect=("running", "starting"))
        if step.resolved:
            self.store.withdraw_approvals(attempt_id=self.attempt_id, provider_request_ids=list(step.resolved))
            for rid in step.resolved:
                self.approval_seen.pop(rid, None)
            if not self.store.approvals(message_id=self.message_id) and self.driver.outcome is None:
                self.store.set_state(self.message_id, RUNNING, expect=("approval-needed",))
        if step.outcome is not None:
            # The batch that records the turn's result also claims the conversation's
            # title, when the first turn ended quiescent (titles.py): no transaction of
            # its own, so no stop, steer or message ever waits on the claim.
            self._flush(claim_title=self._title_quiescent())
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
        if send:
            self._send_outbox()

    def _load_log(self) -> bool:
        """Read `stdin.jsonl`; True when it shows a stdin frame that was not fully
        written (a signal that found no child is not a relay failure; C-26.4)."""
        logged = read_log(self.adir / "stdin.jsonl")
        self.logged = len(logged)
        self.sent: dict[str, str] = {r["tag"]: r["status"] for r in logged if r.get("tag")}
        self.next_seq = len(logged) + 1
        return any(r["status"] != "written" and r.get("op") != "signal"
                   and r.get("tag") not in (TITLE_FRAME, TITLE_CANCEL_FRAME) for r in logged)

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
        # A completed status handshake resolves sends whose receipts were lost.
        self._steer_tried.intersection_update(tag.removeprefix("steer:") for tag in self.sent
                                             if tag.startswith("steer:"))
        for tag in self.sent:
            if tag.startswith("steer:"):
                mid = tag.removeprefix("steer:")
                if mid not in getattr(self.driver, "steers", {}):
                    # The previous runner may have handed it to the guardian
                    # after construction read the log. Status waits for that
                    # write; register it before any stdout replay can consume
                    # the matching lifecycle/result evidence.
                    self.driver.restore_steer(mid, frame="written")
                    self._apply(Step(events=[Event("steer.sent", {"message_id": mid}, f"cmd:steer:{mid}")]), send=False)
                self.driver.steer_written(mid)
                self._replay_steers[:] = [pending for pending in self._replay_steers if pending != mid]
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
        self._send_frames()                     # stops at a stdin close the title holds (`_title_holds`)
        if self._send_title():                  # its one line went, or it ended: the close may be free
            self._send_frames()

    def _send_frames(self) -> None:
        while self.outbox:
            if self.outbox[0].op == "close" and self._title_holds():
                return                          # the title's answer, its budget, or anything else first
            if self.relay_failed:
                self._discard_steers("relay-failed")
                self.outbox.clear()
                return
            if self.resends and self.clock() < self.resend_at:
                return                          # IR-27: the next try waits its turn
            if self.optional_ack_lost:
                # A title write went unanswered, so its number may be taken. Stdout
                # was read meanwhile; the turn's next frame resynchronizes first.
                self.optional_ack_lost = False
                self.handshaken = False
            if not self._handshake():
                return
            frame = self.outbox[0] if self.outbox else None
            if frame is None or self.relay_failed:
                self.outbox.clear()
                return
            if self.sent.get(frame.tag) == "written":
                self.outbox.pop(0)            # replayed: already delivered to the provider
                if frame.tag.startswith("steer:"):
                    self.driver.steer_written(frame.tag.removeprefix("steer:"))
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
            if frame.tag.startswith("steer:"):
                mid = frame.tag.removeprefix("steer:")
                # Striped locks can coincide or appear in opposite host/message
                # order on two runners. Deduplicate and order them globally.
                locks = {id(lock): lock for lock in (self.handover, self.handover_for(mid))}
                with ExitStack() as held:
                    for _, lock in sorted(locks.items()):
                        held.enter_context(lock)
                    verdict = self._steer_handover_verdict(mid)
                    going = verdict == "send" and self._transmit(frame)
                if verdict == "withdraw":
                    self.outbox.pop(0)
                    self._miss_steer(mid, "stopped-or-cancelled")
                elif verdict == "wait" or not going and verdict != "again":
                    return
                continue
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
            if frame.tag.startswith("steer:"):
                self._steer_tried.add(frame.tag.removeprefix("steer:"))
            self._unacknowledged(f"frame {frame.tag}: {exc}")
            return False
        self.resends = 0
        if ack.ok:
            self.sent[frame.tag] = "written"
            self.next_seq += 1
            if frame.op == "write":
                self.wrote_bytes[frame.tag] = len((frame.line or "").encode()) + 1
            self.outbox.pop(0)
            if frame.tag.startswith("steer:"):
                self.driver.steer_written(frame.tag.removeprefix("steer:"))
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

    # --- the first turn's title (titles.py) ---------------------------------------

    def _title_quiescent(self) -> bool:
        """Whether the turn that just ended is a quiescent point for the title: this
        runner's own first Claude turn (never a replay), ended by the provider's
        successful result, with nothing of the turn left to write but its stdin close,
        no stop of any kind, no command waiting (a stop, a steer, an answer), and a
        provider that has read everything large this runner gave it (`_title_fits`).
        The store adds what only it knows, in the same transaction (`_claim_title`):
        no stop recorded, no other message of the conversation queued.

        A replay is ruled out by `replayed_message` alone: an outcome an earlier runner
        recorded, or an ended attempt, with the message in the relay's log is a replay,
        and one without it never had a successful result. So is a withheld message, or
        one this runner did not write. Every stop has its reason (`stop_reason`, which a
        failed relay sets too, `_relay_lost`) or is the store's, and a close already
        written follows a stop, a refused frame or an earlier outcome. The provider
        check guards a Codex process against a store that would grant the claim."""
        outcome = self.driver.outcome
        return (self.title.state == OPEN and self.spec.provider == "claude" and not self.replayed_message
                and outcome is not None and outcome.state == COMPLETE and outcome.ended_by == "provider"
                and self.stop_reason is None and not getattr(self.driver, "interrupt_requested", False)
                and self.frame_refused is None and self.commands.empty()
                and self._title_idle() and self._title_fits())

    def _title_idle(self) -> bool:
        """The provider is idle, reading stdin, with no turn in flight: its result is in,
        nothing of the turn but the stdin close waits to be written, and every steer
        this runner wrote shows, in the provider's own rows, that it was read."""
        steers = getattr(self.driver, "steers", {})
        return ([frame.op for frame in self.outbox] == ["close"]
                and all(steers.get(tag.removeprefix("steer:"), {}).get("fate") in STEER_READ_FATES
                        for tag in self.wrote_bytes if tag.startswith("steer:")))

    def _title_fits(self) -> bool:
        """The title's line and every byte the provider may not have read yet fit the
        smallest pipe buffer, so the relay's write of it never waits on the provider
        (review of 66d692a0, P1: a write that waits holds the relay's lock and this
        thread). Read, by the provider's own answers: its `initialize` answer, the
        message it accepted, every tool answer its successful result waited for (a
        `can_use_tool` answer has no timeout), and each steer it reported."""
        unread = sum(size for tag, size in self.wrote_bytes.items()
                     if tag not in ("init", USER_FRAME) and not tag.startswith(("approval:", "steer:")))
        return unread + len(request_line(self.spec.text)) + 1 <= PIPE_FLOOR

    def _title_may_write(self) -> bool:
        """Checked under the title's lock at the moment of the write (`begin_write`):
        what another thread can change between the claim and the write, a stop the
        runner is told of (`interrupt`) or a command it is sent (a steer, an answer).
        The rest is this thread's own state, checked at the claim and unchanged since
        (the claim and the write are one `_apply`). In-memory only, never I/O."""
        return self.stop_reason is None and self.commands.empty()

    def _send_title(self) -> bool:
        """Write the claimed title request: one line, at most TITLE_LINE_MAX bytes, to
        an idle provider, sized so its write does not wait on it (`_title_fits`). Only this thread
        writes it; the decision is ordered against every stop by the title's gate
        (`SessionTitle.begin_write`, `end_title`), and nothing of the turn is behind
        it but the stdin close it holds. A refused, oversized or unanswered write is
        never retried and never fails the relay: the close resynchronizes first
        (`_send_frames`). It uses only the relay's `send`. Whether anything changed."""
        if self.title.state != CLAIMED:
            return False
        if not self.title.begin_write(self._title_may_write):
            self.end_title("not-quiescent")
            return True
        line = request_line(self.spec.text)
        try:
            ack = self.relay.send(self.next_seq, "write", line=line, tag=TITLE_FRAME)
        except FrameTooLarge:
            self.end_title("too-large")     # nothing reached the relay
            return True
        except RelayError:
            self.optional_ack_lost = True       # the relay's log, read at the next handshake, decides
            self.end_title("unanswered")
            return True
        if ack.ok:
            self.sent[TITLE_FRAME] = "written"
            self.next_seq += 1
            self.wrote_bytes[TITLE_FRAME] = len(line) + 1
        else:
            self.optional_ack_lost = True
            self.end_title("refused")
        return True

    def _title_holds(self) -> bool:
        """Whether the turn's stdin close waits for the title: only while it is claimed
        or sent and nothing else wants the conversation (`_title_release`). The CLI's
        end-of-input teardown does not wait for the title's answer (titles.py)."""
        if not self.title.holding:
            return False
        why = self._title_release()
        if why is None:
            return True
        self.end_title(why)
        return False

    def _title_release(self) -> str | None:
        """What ends the title's hold now, from the runner's own state, or None. The
        rest ends the title itself: a person's Stop (`end_title`, before it is
        recorded), any command the runner drains (`_drain_commands`: the daemon's
        stop, an answer, a steer), and what only the store knows (`_watch_title`)."""
        if self.title.answered:
            return "answered"
        if self.title.expired():
            return "budget"
        if len(self.outbox) != 1:
            return "frame"                      # a signal (D-15) or another frame waits behind the close
        return None

    def _watch_title(self) -> None:
        """Once per loop turn while the title holds the close: what only the store
        knows ends the hold too (another message of the conversation, a name a person
        gave it). A read: it commits nothing. The close then goes."""
        if not self.title.holding:
            return
        row = self.store.one(TITLE_WATCH_SQL, (self.conversation_id, self.message_id, self.conversation_id))
        if row and row["waiting"]:
            self.end_title("message-waiting")
        elif row and row["source"] != "fallback":
            self.end_title("renamed")
        if not self._title_holds():
            self._send_outbox()

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
        self._discard_steers("host-withheld")
        self._apply(self.driver.withdraw())

    def _steer_handover_verdict(self, message_id: str) -> str:
        """Host stop and this message's cancel serialize with the actual write."""
        if message_id in self._steer_tried:
            self.handshaken = False
            if not self._handshake():
                return "wait"
            if self.relay_failed or f"steer:{message_id}" in self.sent:
                return "again"
        row = self.store.find_message(message_id)
        host = self.store.find_message(self.message_id)
        if (not self.steerable or not row or row["state"] != "steering"
                or row["state_reason"] != f"steer:{self.message_id}" or row.get("stop_requested_at")
                or bool(host and host.get("stop_requested_at"))):
            return "withdraw"
        return "send"

    def _refuse_frame(self, frame: Frame, exc: FrameTooLarge) -> None:
        """A frame over the relay's cap is never sent (IR-27). Nothing after it can
        be sent in order either, so stdin is closed: the provider ends at EOF and
        the turn is reconciled from what the relay log shows (C-24.6)."""
        if self.log:
            self.log.warning("turn %s frame %s not sent: %s", self.attempt_id, frame.tag, exc)
        if frame.tag.startswith("steer:"):
            self.outbox.pop(0)
            self._miss_steer(frame.tag.removeprefix("steer:"), "frame-too-large")
            return
        self.frame_refused = self.frame_refused or frame.tag
        self.outbox.clear()
        self._discard_steers("host-frame-refused")
        self.outbox[:] = [] if frame.op == "close" else [Frame("close", "close")]

    def _relay_lost(self, why: str) -> None:
        """Nothing more can be written (C-26.4). A turn with no terminal event is
        then stopped as a stop request would stop it; the steps that need the
        relay are skipped, so containment ends it (D-13, design §6)."""
        self.relay_failed = True
        self._discard_steers("relay-failed")
        self.outbox.clear()
        if self.log:
            self.log.warning("turn %s relay failed: %s", self.attempt_id, why)
        if self.driver.outcome is None and self.stop_at is None:
            self.stop_reason = self.stop_reason or "relay-failed"
            self.stop_at = self.clock()

    # --- time ------------------------------------------------------------------

    def _timers(self) -> None:
        now = self.clock()
        if getattr(self.driver, "steer_waiting", False) and self.driver.outcome is None:
            watch = getattr(self.driver, "steer_watch", 0)
            if self.steer_wait_since is None or watch != self._steer_watch_seen:
                # A new spell of waiting, even one that began and ended between two
                # polls (a steer's own turn and its result read in one batch): its
                # 15 s start now, not with the spell before it (C-26.5).
                self.steer_wait_since, self._steer_watch_seen = now, watch
            elif now - self.steer_wait_since >= STEER_GRACE_S:
                self.steer_wait_since = now
                self._apply(self.driver.expire_steers())
        else:
            self.steer_wait_since = None
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
            if (clocks.approval_wait_s is not None and now - since >= clocks.approval_wait_s
                    and self.driver.outcome is None):
                # IR-8: the person did not answer in time. Subfleet does not answer
                # the approval (C-27.2); it stops the turn.
                self.approval_seen.pop(request_id, None)
                self.stop_reason = self.stop_reason or "approval-timeout"
                self.commands.put(("interrupt",))
        self._watch_title()                     # last: a frame queued above ends the title's hold

    # --- persistence -----------------------------------------------------------

    def _flush_due(self) -> bool:
        return (bool(self.batch) and (self.batch_bytes >= FLUSH_BYTES or self.clock() - self.last_flush >= FLUSH_S)
                or self.title.pending)

    def _flush(self, *, claim_title: bool = False) -> None:
        """One events batch and the watermark (C-25.4), with the title's work riding in
        the same transaction (titles.py): its claim, only in the batch that records the
        first turn's result, and a generated title the provider answered."""
        answer = self.title.take()
        if not self.batch and answer is None and not claim_title:
            self.last_flush = self.clock()
            return
        title = (TitleUpdate(claim_at=self.title.clock() if claim_title else None, answer=answer)
                 if claim_title or answer is not None else None)
        batch, self.batch, self.batch_bytes = self.batch, [], 0
        self.store.append_events(conversation_id=self.conversation_id, message_id=self.message_id,
                                 attempt_id=self.attempt_id, events=batch, stdout_offset=self.offset,
                                 stdin_seq=self.next_seq - 1, title=title)
        self.last_flush = self.clock()
        if title is None:
            return
        if title.error and self.log:
            self.log.debug("optional session title for %s unavailable: %s", self.conversation_id, title.error)
        if claim_title and not (title.claimed and self.title.claim(title.claim_at)):
            self.end_title("refused")           # the store refused, or a stop ended it meanwhile

    def _write_outcome(self) -> None:
        recorded = self.recorded
        if recorded is not None:
            # A runner before this one reached the turn's outcome, with what only it
            # knew (a withhold, a stop's reason, an idle settle): that record stands.
            # Only what stdout said after the outcome (C-24.8) is added.
            data = {**recorded, "terminal_after_end": bool(recorded.get("terminal_after_end"))
                    or bool(getattr(self.driver, "terminal_after_end", False)),
                    "steers": {**recorded.get("steers", {}), **self.steer_facts()}}
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
                "steers": self.steer_facts(),
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

    def steer_facts(self) -> dict[str, dict]:
        """Driver evidence with the relay's authoritative handover facts."""
        facts = {mid: dict(fact) for mid, fact in (self.recorded or {}).get("steers", {}).items()}
        delivered_rank = {"unanswered": 1, "delivered": 2, "consumed": 3}
        for mid, fact in getattr(self.driver, "steers", {}).items():
            previous = facts.get(mid, {}).get("fate")
            if ((fact.get("fate") == "unknown" and previous not in (None, "unknown"))
                    or delivered_rank.get(previous, 0) > delivered_rank.get(fact.get("fate"), 0)):
                continue
            facts[mid] = dict(fact)
        for mid, fact in facts.items():
            if f"steer:{mid}" in self.sent or mid in self._steer_tried:
                fact["frame"] = "written"
        return facts

    def _read_attachment(self, image: Image) -> bytes:
        # The digest is the identity, for the host's images and a steer's alike (a
        # steered message's images are not in the host's spec), and a persisted
        # manifest's absolute path may predate a move of the state root. Verify
        # and use bytes from one descriptor.
        return attachment_store.read_verified(self.store, image.sha256)[0]

    def _attachment_path(self, image: Image) -> str:
        """A private copy of the image, by its digest, under the attempt directory."""
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
