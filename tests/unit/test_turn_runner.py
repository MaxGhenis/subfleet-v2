"""C-24.7, C-26.5, C-26.9: the turn runner's clocks come from policy.

The runner's timer step is driven directly with a controlled clock; nothing is
sent (the relay is never reached), so these check only what each step queues.
"""

from __future__ import annotations

import pytest

from subfleet.conversations.runner import Clocks, TurnRunner
from subfleet.conversations.store import ConversationStore
from subfleet.conversations.turn import TurnSpec

MID = "7f1c9a0e-1111-4222-8333-444455556666"
SID = "0b0e0f00-aaaa-4bbb-8ccc-dddddddddddd"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def make_runner(tmp_path):
    store = ConversationStore(tmp_path / "state")
    contained: list[str] = []

    def make(clocks: Clocks):
        clock = Clock()
        spec = TurnSpec(provider="claude", message_id=MID, text="hi", model_id="opus[1m]", permission="ask",
                        native_session_id=None, new_session_id=SID)
        runner = TurnRunner(store=store, attempt={"attempt_id": "job/a1", "lane_id": "claude-1"}, spec=spec,
                            conversation_id="cv-x", attempt_dir=tmp_path / "a1",
                            control_socket=str(tmp_path / "none.sock"), on_outcome=lambda r: None,
                            on_contain=contained.append, clocks=clocks, clock=clock)
        return runner, clock, contained

    yield make
    store.close()


def queued(runner) -> list[str]:
    return [frame.tag for frame in runner.outbox]


def test_the_stop_escalation_follows_the_policy_clocks(make_runner):
    """C-24.7: SIGINT, then closing stdin, then containment, each at its configured delay
    after the stop request, and nothing before it."""
    runner, clock, contained = make_runner(Clocks(sigint_after_s=1, close_after_s=2, contain_after_s=3))
    runner.stop_at = clock.now
    clock.now += 0.99
    runner._timers()
    assert queued(runner) == [] and contained == []
    clock.now += 0.02                      # 1.01 s
    runner._timers()
    assert queued(runner) == ["signal:int"]
    clock.now += 1.0                       # 2.01 s
    runner._timers()
    assert queued(runner) == ["signal:int", "close"] and contained == []
    clock.now += 1.0                       # 3.01 s
    runner._timers()
    runner._timers()
    assert contained == ["job/a1"]         # once


def test_the_default_clocks_are_the_contract_defaults(make_runner):
    """C-24.7, C-26.5, C-26.9: 10, 20 and 30 s after a stop; 135 s after the terminal event;
    3600 s for an approval."""
    assert Clocks() == Clocks(sigint_after_s=10, close_after_s=20, contain_after_s=30, after_result_s=135,
                              approval_wait_s=3600)
    runner, clock, contained = make_runner(Clocks())
    runner.stop_at = clock.now
    clock.now += 9.9
    runner._timers()
    assert queued(runner) == []
    clock.now += 0.2
    runner._timers()
    assert queued(runner) == ["signal:int"]


def test_a_process_outliving_its_terminal_event_is_stopped_after_the_configured_time(make_runner):
    """C-26.5: SIGINT `after_result_s` after the terminal event, containment
    `contain - sigint` seconds later."""
    runner, clock, contained = make_runner(Clocks(sigint_after_s=1, close_after_s=2, contain_after_s=4,
                                                  after_result_s=5))
    runner.ended_at = clock.now
    clock.now += 4.9
    runner._timers()
    assert queued(runner) == []
    clock.now += 0.2
    runner._timers()
    assert queued(runner) == ["signal:int:late"] and contained == []
    clock.now += 3.1
    runner._timers()
    assert contained == ["job/a1"]


def test_an_unanswered_approval_stops_the_turn_after_the_configured_wait(make_runner):
    """C-26.9, IR-8: the turn is stopped with reason approval-timeout; the approval is not answered."""
    runner, clock, contained = make_runner(Clocks(approval_wait_s=2))
    runner.approval_seen["perm-1"] = clock.now
    clock.now += 1.9
    runner._timers()
    assert runner.commands.empty()
    clock.now += 0.2
    runner._timers()
    assert runner.commands.get_nowait() == ("interrupt",) and runner.stop_reason == "approval-timeout"


# --- ending after its service closed (C-25.3, C-26.6) ------------------------------------


def test_a_runner_ends_and_closes_its_relay_even_when_its_last_writes_are_refused(make_runner, caplog):
    """C-25.3, C-26.6: a runner whose service closed while it was still going finds the
    store refusing its last events (`store-closed`). It still closes its relay and sets
    `finished`, which close() and retention read, and logs at info that its service
    closed: a runner a later daemon adopts for the attempt replays those events from
    stdout. The refused flush had raised out of the thread, skipping both."""
    import logging
    runner, clock, contained = make_runner(Clocks())
    runner.log = logging.getLogger("test-runner")
    closed: list[bool] = []
    real_close = runner.relay.close
    runner.relay.close = lambda: (closed.append(True), real_close())[1]
    runner.batch.append(("command", "cmd:start", 0, "status", {"phase": "starting-provider"}))
    runner.store.close()
    runner.stop()
    with caplog.at_level(logging.INFO, logger="test-runner"):
        runner._run()
    assert runner.finished.is_set() and closed == [True]
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.INFO, "turn runner job/a1 stopped: its service closed")] * 2   # the message read, the flush
    assert runner.join(0)                   # never started: nothing to wait for


def test_a_runner_whose_thread_cannot_start_is_left_as_never_started(make_runner, monkeypatch):
    """C-25.3 (review of 4d3d3ea, F2): `start()` kept its thread before starting it, so
    a thread that could not start (`RuntimeError` at a thread limit) left a runner
    whose `join()` raised "cannot join thread before it is started", and with it the
    service's close(), before the store and the rest of `Daemon.close()` were closed."""
    import threading
    runner, clock, contained = make_runner(Clocks())
    real_start = threading.Thread.start

    def failing(self):
        if self.name.startswith("turn:"):
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", failing)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        runner.start()
    assert runner.join(0)                   # never started: nothing to wait for, and no error
    assert not runner.finished.is_set()


def test_a_runner_writes_no_outcome_after_its_service_closed(make_runner, tmp_path):
    """C-25.3, C-26.6: `turn.json` is written only while the store is open, so a runner
    still going after close() adds nothing to an attempt directory its owner may be
    removing; a runner a later daemon adopts for the attempt writes it again."""
    from subfleet.conversations.store import ConversationError
    from subfleet.conversations.turn import Outcome
    runner, clock, contained = make_runner(Clocks())
    (tmp_path / "a1").mkdir()
    runner.driver.outcome = Outcome("complete", ended_by="provider")
    runner._write_outcome()
    assert (tmp_path / "a1" / "turn.json").exists()
    (tmp_path / "a1" / "turn.json").unlink()
    runner.store.close()
    with pytest.raises(ConversationError) as err:
        runner._write_outcome()
    assert err.value.reason == "store-closed"
    assert list((tmp_path / "a1").iterdir()) == []


# --- the relay handshake (review IR-27) ------------------------------------------------

import json
import os
import shutil
import tempfile
from pathlib import Path

from subfleet import relay as relay_module
from subfleet.conversations.claude_turn import INIT_REQUEST_ID
from subfleet.relay import RelayServer, read_log

INIT_OK = json.dumps({"type": "control_response", "response": {
    "subtype": "success", "request_id": INIT_REQUEST_ID, "response": {
        "account": {"email": "max@example.org"}, "fast_mode_state": "on",
        "models": [{"value": "opus[1m]", "resolvedModel": "claude-opus-5-5[1m]", "supportsEffort": True,
                    "supportedEffortLevels": ["high"]}]}}})


class LegacyRelay(RelayServer):
    """A version-1 relay: its `apply` refused anything without a frame number as
    `bad-frame` (relay.py before IR-27), which is how it answers `status`."""

    def apply(self, frame):
        if isinstance(frame, dict) and "seq" not in frame:
            return {"ok": False, "error": "bad-frame"}
        return super().apply(frame)


@pytest.fixture
def relayed(tmp_path):
    """A runner whose relay is a real RelayServer on a socket, feeding a pipe."""
    short = Path(tempfile.mkdtemp(prefix="sfrr-", dir="/tmp"))
    servers, pipes, stores = [], [], []

    def make(*, text="hi", server_class=RelayServer, clocks=Clocks(), serve=True, before_runner=None,
             provider="claude", recorded=False):
        adir = tmp_path / "a1"
        adir.mkdir(exist_ok=True)
        server = server_class(short / "r.sock", adir / "stdin.jsonl")
        server.bind()
        read_end, write_end = os.pipe()
        os.set_blocking(read_end, False)
        if serve:
            server.serve(write_end)
        servers.append(server)
        pipes.append(read_end)
        if before_runner is not None:
            before_runner(server, adir)
        store = ConversationStore(tmp_path / "state")
        stores.append(store)
        clock = Clock()
        claude = provider == "claude"
        settings = {"model": "opus[1m]" if claude else "gpt-6", "effort": None, "fast": False,
                    "permission": "ask" if claude else "read-only", "auto_continue": True}
        conversation_id = "cv-x"
        if recorded:                            # the message has a row, where a person's stop is recorded
            conversation_id = store.create_conversation(provider=provider, workspace=str(tmp_path),
                                                        workspace_kind="in-place", settings=settings,
                                                        origin="new")[0]["conversation_id"]
            store.submit_message(conversation_id=conversation_id, message_id=MID, after_message_id=None, text=text,
                                 attachments=[], settings=settings)
        spec = TurnSpec(provider=provider, message_id=MID, text=text, model_id=settings["model"],
                        permission=settings["permission"], native_session_id=None,
                        new_session_id=SID if claude else None, cwd=str(tmp_path))
        runner = TurnRunner(store=store, attempt={"attempt_id": "job/a1", "lane_id": f"{provider}-1"}, spec=spec,
                            conversation_id=conversation_id, attempt_dir=adir, control_socket=str(short / "r.sock"),
                            on_outcome=lambda r: None, on_contain=lambda a: None, clocks=clocks, clock=clock)
        runner.relay.timeout_s = 5
        return runner, clock, server, adir

    yield make
    for server in servers:
        server.stop()
    for fd in pipes:
        os.close(fd)
    for store in stores:
        store.close()
    shutil.rmtree(short, ignore_errors=True)


def test_the_runner_asks_the_relay_before_sending_and_adopts_its_cap(relayed, monkeypatch):
    """C-26.4, IR-27: the first frame goes out only after `status`; a message frame over
    the advertised cap is never sent, stdin is closed instead, and the turn records it."""
    monkeypatch.setattr(relay_module, "FRAME_MAX", 600)     # the relay advertises, and enforces, 600 bytes
    runner, clock, server, adir = relayed(text="x" * 700)
    runner._apply(runner.driver.start())
    assert runner.handshaken and runner.relay_version == 2 and runner.relay.frame_max == 600
    assert [r["tag"] for r in read_log(adir / "stdin.jsonl")] == ["init"]
    runner._apply(runner.driver.feed(INIT_OK, 0))
    assert runner.frame_refused == "user-message"
    assert [(r["tag"], r["status"]) for r in read_log(adir / "stdin.jsonl")] == [("init", "written"), ("close", "written")]
    assert runner.stop_at is None and not runner.relay_failed


def test_a_version_one_relay_is_read_from_its_log(relayed):
    """IR-27, backward compatible: a relay without `status` answers `bad-frame`; the
    runner then trusts the log as before and sends."""
    runner, clock, server, adir = relayed(server_class=LegacyRelay)
    runner._apply(runner.driver.start())
    assert runner.handshaken and runner.relay_version is None
    assert [r["tag"] for r in read_log(adir / "stdin.jsonl")] == ["init"]


def test_a_relay_that_disagrees_with_its_log_is_not_written_to(relayed):
    """C-26.4, IR-27: when the relay has applied frames its log does not show, nothing is
    sent on a guess; relaying ends and the turn is stopped (containment follows)."""
    runner, clock, server, adir = relayed()
    server.apply({"seq": 1, "op": "write", "line": "x", "tag": "init", "sha256": relay_module.line_sha256("x")})
    (adir / "stdin.jsonl").unlink()
    runner._apply(runner.driver.start())
    assert runner.relay_failed and runner.stop_reason == "relay-failed" and runner.stop_at == clock.now
    assert runner.outbox == [] and server.last_applied == 1


def test_an_unanswering_relay_is_retried_a_bounded_number_of_times(relayed):
    """IR-27: status (and any frame) is retried after a doubling pause, at most
    RESEND_MAX times; then the relay counts as failed and the turn is stopped."""
    from subfleet.conversations.runner import RESEND_MAX
    runner, clock, server, adir = relayed()
    server.stop()                                             # nothing listens any more
    runner._apply(runner.driver.start())
    tries = 1
    while not runner.relay_failed:
        clock.now += 0.1
        runner._send_outbox()
        clock.now += 100
        runner._send_outbox()
        tries += 1
        assert tries <= RESEND_MAX + 2
    assert tries == RESEND_MAX + 1 and runner.stop_reason == "relay-failed"
    assert not (adir / "stdin.jsonl").exists()


INIT_LINE = json.dumps({"type": "control_request", "request_id": INIT_REQUEST_ID,
                        "request": {"subtype": "initialize"}}, separators=(",", ":"))


def logged_intent(seq: int, tag: str, line: str) -> dict:
    return relay_module._intent({"seq": seq, "op": "write", "line": line, "tag": tag,
                                 "sha256": relay_module.line_sha256(line)})


def test_a_relay_log_showing_a_frame_not_written_stops_the_turn_and_sends_nothing(relayed, tmp_path):
    """C-26.4, IR-27: the log shows a stdin frame whose write failed, which ended relaying
    for good. A rebuilt runner's handshake finds it, writes nothing more (not even the
    driver's frames), and stops the turn with reason relay-failed; the escalation then
    skips the steps that need the relay and reaches containment (C-24.7)."""
    adir = tmp_path / "a1"
    adir.mkdir()
    records = [logged_intent(1, "init", INIT_LINE), {"kind": "failed", "seq": 1, "errno": 32}]
    (adir / "stdin.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    before = (adir / "stdin.jsonl").read_bytes()
    runner, clock, server, adir = relayed(clocks=Clocks(sigint_after_s=1, close_after_s=2, contain_after_s=3))
    contained: list[str] = []
    runner.on_contain = contained.append
    assert runner.sent == {"init": "failed"}
    runner._apply(runner.driver.start())
    assert runner.handshaken and runner.relay_failed
    assert runner.stop_reason == "relay-failed" and runner.stop_at == clock.now
    assert runner.outbox == [] and runner.driver.outcome is None
    clock.now += 3.1
    runner._timers()
    runner._send_outbox()
    assert contained == ["job/a1"] and runner.outbox == []
    assert (adir / "stdin.jsonl").read_bytes() == before        # nothing was written or logged


def test_a_frame_in_flight_when_the_runner_starts_is_not_taken_for_a_failure(relayed):
    """C-26.4, IR-27: the previous daemon left a frame being written, so the new runner
    reads its intent as `pending`. The relay answers `status` only after that write is
    logged, and the runner reads the log again then: the frame is written, nothing
    fails, and the driver's same frame is not sent a second time."""
    import threading
    import time

    def in_flight(server, adir):
        server._lock.acquire()                       # `apply` holds it for the whole frame
        intent = logged_intent(1, "init", INIT_LINE)
        server._append(intent)
        server._records.append({**intent, "status": "pending"})

        def finish():
            time.sleep(0.3)
            server._append({"kind": "written", "seq": 1})
            server._records[-1]["status"] = "written"
            server._lock.release()
        threading.Thread(target=finish, daemon=True).start()

    runner, clock, server, adir = relayed(before_runner=in_flight)
    assert runner.sent == {"init": "pending"}           # read while the write was in flight
    runner._apply(runner.driver.start())
    assert runner.handshaken and not runner.relay_failed and runner.stop_reason is None
    assert runner.sent == {"init": "written"} and runner.outbox == [] and runner.next_seq == 2
    assert [(r["tag"], r["status"]) for r in read_log(adir / "stdin.jsonl")] == [("init", "written")]
    assert server.last_applied == 1


# --- a stop before the message is handed over (C-24.7; review of 3c1a34e, finding 2) ---------


def codex_replies(cwd: str) -> list[str]:
    """What a Codex app server answers before `turn/start`: initialize, hooks/list,
    model/list, then the thread (the probe of `import-codex-handoff-probe.py`)."""
    from subfleet.guard.preflight import HOOK_KEY
    return [json.dumps(reply) for reply in (
        {"id": 1, "result": {}},
        {"id": 2, "result": {"data": [{"cwd": cwd, "hooks": [{"key": HOOK_KEY, "enabled": True,
                                                               "trustStatus": "trusted"}]}]}},
        {"id": 3, "result": {"data": [{"id": "gpt-6", "model": "gpt-6"}]}},
        {"id": 4, "result": {"thread": {"id": "7f1c9a0e-2222-4222-8333-444455556666", "status": {"type": "idle"}},
                             "model": "gpt-6"}})]


def answer_up_to_the_message(runner, tmp_path, provider: str) -> None:
    """Feed the provider's answers, the last of which makes the driver produce the message frame."""
    lines = [INIT_OK] if provider == "claude" else codex_replies(str(tmp_path))
    for offset, line in enumerate(lines):
        runner._apply(runner.driver.feed(line, offset))


def logged(adir) -> list[str]:
    return [record["tag"] for record in read_log(adir / "stdin.jsonl")]


def before_the_message(provider: str) -> list[str]:
    return ["init"] if provider == "claude" else ["init", "initialized", "hooks", "models"]


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_stop_recorded_after_the_runner_s_first_look_still_keeps_the_message_unwritten(relayed, tmp_path,
                                                                                        provider):
    """C-24.7 (review of 3c1a34e, finding 2): a person's stop recorded after the
    runner read the message at its start, but before the relay log shows the
    message handed over (a cancel's, or `turn.interrupt`'s before the runner
    drains its command), is seen when the message frame's turn comes: the frame
    is never sent, nor anything queued behind it, stdin is closed, and the turn
    ends stopped before sending."""
    runner, clock, server, adir = relayed(provider=provider, recorded=True)
    runner._apply(runner.driver.start())
    runner.store.update_message(MID, stop_requested_at="2026-09-26T12:00:00.000Z")
    answer_up_to_the_message(runner, tmp_path, provider)
    tail = ["thread"] if provider == "codex" else []
    assert logged(adir) == before_the_message(provider) + tail + ["close"]
    assert runner.driver.outcome.reason == "stopped-before-send" and runner.withheld
    assert runner.outbox == [] and runner.stop_reason == "stopped" and runner.stop_at is not None
    assert json.loads((adir / "turn.json").read_text())["user_frame_written"] is False


@pytest.mark.parametrize("reason", ["stopped", "wall-limit", "operator-kill"])
def test_a_stop_the_runner_was_asked_for_keeps_an_unsent_message_unwritten(relayed, tmp_path, reason):
    """C-24.7 (review of 3c1a34e, finding 2): a stop queued on the runner (a
    person's `turn.interrupt`, the daemon's wall limit or kill) before the
    provider answered `initialize` wins over the answer, which the runner reads
    before its commands: the message frame is never sent."""
    runner, clock, server, adir = relayed(recorded=True)
    runner._apply(runner.driver.start())
    runner.interrupt(reason)
    answer_up_to_the_message(runner, tmp_path, "claude")
    assert logged(adir) == ["init", "close"]
    assert runner.driver.outcome.reason == "stopped-before-send" and runner.stop_reason == reason
    runner._drain_commands()                                      # the queued interrupt finds the turn ended
    assert logged(adir) == ["init", "close"]


def test_without_a_stop_the_message_is_handed_over(relayed, tmp_path):
    """C-24.7: the check costs nothing when nobody stopped the turn."""
    runner, clock, server, adir = relayed(recorded=True)
    runner._apply(runner.driver.start())
    answer_up_to_the_message(runner, tmp_path, "claude")
    # C-26.8: `get_settings` follows the message this runner sent.
    assert logged(adir) == ["init", "user-message", "settings"] and runner.driver.outcome is None


def _lose_the_answer(runner, *, reached: bool):
    """The relay's answer to the message frame is lost once: after the relay took
    it (`reached`), or before anything reached it (a refused connection)."""
    real = runner.relay.send
    lost = []

    def send(seq, op, line=None, tag=None, sig=None):
        if tag == "user-message" and not lost:
            lost.append(tag)
            if reached:
                real(seq, op, line=line, tag=tag, sig=sig)
            raise relay_module.RelayError("relay closed the connection before acknowledging")
        return real(seq, op, line=line, tag=tag, sig=sig)
    runner.relay.send = send


def test_a_stop_after_a_handover_whose_answer_was_lost_is_decided_by_the_relay_log(relayed, tmp_path):
    """C-24.7, IR-27 (review of 3c1a34e, finding 2): the relay took the message
    frame but its answer was lost, and then a person stopped the turn. The
    relay's log, read after a fresh status answer, shows the message handed
    over before the stop, so it is not withdrawn: it is not sent twice, and
    D-13 stops the turn with the provider's interrupt."""
    runner, clock, server, adir = relayed(recorded=True)
    _lose_the_answer(runner, reached=True)
    runner._apply(runner.driver.start())
    answer_up_to_the_message(runner, tmp_path, "claude")
    assert runner.handover_tried and runner.outbox[0].tag == "user-message"
    runner.store.update_message(MID, stop_requested_at="2026-09-26T12:00:00.000Z")
    runner.interrupt("stopped")
    clock.now += 100
    runner._send_outbox()
    runner._drain_commands()
    assert logged(adir) == ["init", "user-message", "settings", "interrupt"]
    assert runner.driver.outcome is None and not runner.withheld


def test_a_stop_after_a_send_that_never_reached_the_relay_keeps_the_message_unwritten(relayed, tmp_path):
    """C-24.7, IR-27 (review of 3c1a34e, finding 2): the message frame's send
    failed before the relay took it, and then a person stopped the turn. The
    relay's log, read after a fresh status answer, does not hold it, so the
    retry never sends it: stdin is closed and the turn ends stopped before
    sending. Without a stop the retry sends it once."""
    runner, clock, server, adir = relayed(recorded=True)
    _lose_the_answer(runner, reached=False)
    runner._apply(runner.driver.start())
    answer_up_to_the_message(runner, tmp_path, "claude")
    assert runner.handover_tried and logged(adir) == ["init"]
    runner.store.update_message(MID, stop_requested_at="2026-09-26T12:00:00.000Z")
    clock.now += 100
    runner._send_outbox()
    assert logged(adir) == ["init", "close"] and runner.driver.outcome.reason == "stopped-before-send"


@pytest.mark.parametrize("reached", [True, False])
def test_without_a_stop_a_message_whose_answer_was_lost_is_sent_once(relayed, tmp_path, reached):
    """IR-27: the resend of a message frame whose answer was lost writes it once."""
    runner, clock, server, adir = relayed(recorded=True)
    _lose_the_answer(runner, reached=reached)
    runner._apply(runner.driver.start())
    answer_up_to_the_message(runner, tmp_path, "claude")
    clock.now += 100
    runner._send_outbox()
    assert logged(adir) == ["init", "user-message", "settings"] and runner.outbox == []


# --- get_settings across the upgrade (C-26.8) --------------------------------------------


@pytest.mark.parametrize("earlier,sent", [(False, ["user-message", "settings"]), (True, [])])
def test_get_settings_follows_only_a_message_this_runner_sent(make_runner, monkeypatch, earlier, sent):
    """C-26.8: a replayed attempt whose message an earlier runner sent, including one
    from before `get_settings` existed, is never asked mid-turn; a new one asks right
    after its message."""
    from subfleet.conversations.reconcile import SETTINGS_FRAME, USER_FRAME
    from subfleet.conversations.turn import Frame
    runner, _, _ = make_runner(Clocks())
    assert runner.replayed_message is False                    # an empty log: nothing sent before
    runner.handshaken, runner.replayed_message = True, earlier
    runner.sent = {USER_FRAME: "written"} if earlier else {}
    written: list[str] = []
    monkeypatch.setattr(runner, "_transmit", lambda frame: written.append(frame.tag) or runner.outbox.pop(0) or True)
    monkeypatch.setattr(runner, "_handover_verdict", lambda: "send")
    runner.outbox = [Frame(USER_FRAME, "write", "{}\n"), Frame(SETTINGS_FRAME, "write", "{}\n")]
    runner._send_outbox()
    assert written == sent and runner.outbox == []


def test_a_message_the_handshake_finds_written_is_not_followed_by_get_settings(make_runner, monkeypatch):
    """C-26.8 (review of b0b3f153, P3): the log read at construction can still show the
    previous daemon's message frame in flight; the handshake's final read decides."""
    from subfleet.conversations.reconcile import USER_FRAME
    runner, _, _ = make_runner(Clocks())
    assert runner.replayed_message is False
    monkeypatch.setattr(runner.relay, "status", lambda: None)            # a relay older than version 2

    def load():
        runner.sent, runner.logged = {"init": "written", USER_FRAME: "written"}, 2
        return False
    monkeypatch.setattr(runner, "_load_log", load)
    assert runner._handshake() is True and runner.replayed_message is True


@pytest.mark.parametrize("kind,timed", [("question", False), ("tool", True)])
def test_a_question_waits_for_its_answer_with_no_limit(make_runner, monkeypatch, kind, timed):
    """C-26.9 (2026-09-28): only a tool approval starts the approval clock; an agent's
    question waits for the person however long it takes."""
    from subfleet.conversations.turn import Approval, Step
    runner, clock, _ = make_runner(Clocks(approval_wait_s=2))
    monkeypatch.setattr(runner.store, "add_approvals", lambda **kw: None)
    runner._apply(Step(approvals=[Approval("req-1", kind, {"tool": "AskUserQuestion"}, ("answer", "deny"))]))
    assert ("req-1" in runner.approval_seen) is timed
    clock.now += 3600 * 24
    runner._timers()
    if timed:
        assert runner.stop_reason == "approval-timeout" and runner.commands.get_nowait() == ("interrupt",)
    else:
        assert runner.stop_reason is None and runner.commands.empty()
