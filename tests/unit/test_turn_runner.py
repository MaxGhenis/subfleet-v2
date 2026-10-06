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
    no limit for an approval."""
    assert Clocks() == Clocks(sigint_after_s=10, close_after_s=20, contain_after_s=30, after_result_s=135,
                              approval_wait_s=None)
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
    finish_write = threading.Event()

    def in_flight(server, adir):
        server._lock.acquire()                       # `apply` holds it for the whole frame
        intent = logged_intent(1, "init", INIT_LINE)
        server._append(intent)
        server._records.append({**intent, "status": "pending"})

        def finish():
            finish_write.wait()
            server._append({"kind": "written", "seq": 1})
            server._records[-1]["status"] = "written"
            server._lock.release()
        threading.Thread(target=finish, daemon=True).start()

    try:
        runner, clock, server, adir = relayed(before_runner=in_flight)
        assert runner.sent == {"init": "pending"}       # read while the write was in flight
    finally:
        finish_write.set()                             # independent of store construction speed
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


@pytest.mark.parametrize("kind", ["question", "tool", "command"])
@pytest.mark.parametrize("limit", [None, 2])
def test_approvals_wait_without_limit_unless_policy_caps_tool_approvals(make_runner, monkeypatch, kind, limit):
    """C-26.9: approvals remain pending overnight by default. A configured limit
    stops tool approvals, including Codex command approvals, but never questions."""
    from subfleet.conversations.turn import Approval, Step
    runner, clock, contained = make_runner(Clocks(approval_wait_s=limit))
    monkeypatch.setattr(runner.store, "add_approval", lambda **kw: None)
    monkeypatch.setattr(runner.store, "set_state", lambda *a, **kw: None)
    runner._apply(Step(approvals=[Approval("req-1", kind, {"tool": "AskUserQuestion"}, ("answer", "deny"))]))
    assert ("req-1" in runner.approval_seen) is (kind != "question")
    clock.now += 3600 * 24
    runner._timers()
    if limit is not None and kind != "question":
        assert runner.stop_reason == "approval-timeout" and runner.commands.get_nowait() == ("interrupt",)
    else:
        assert runner.stop_reason is None and runner.commands.empty()
        assert queued(runner) == [] and contained == []


# --- steer handover and replay (C-24.9, C-26.6) ----------------------------------------

STEER_MID = "7f1c9a0e-2222-4222-8333-444455556666"
STEER_CAPS = json.dumps({"type": "system", "subtype": "init", "session_id": SID,
                        "capabilities": ["msg_lifecycle_v1", "interrupt_receipt_v1", "interrupt_cancel_queued_v1"]})


def claim_steer(runner, *, text="remember the second point", message_id=STEER_MID):
    host = runner.store.message(MID)
    runner.store.submit_message(conversation_id=runner.conversation_id, message_id=message_id,
                                after_message_id=MID, text=text, attachments=[], settings=host["settings"])
    runner.store.set_state(message_id, "steering", reason=f"steer:{MID}", expect=("queued",))
    return message_id


def ready_to_steer(runner, tmp_path):
    runner._apply(runner.driver.start())
    answer_up_to_the_message(runner, tmp_path, runner.spec.provider)
    if runner.spec.provider == "claude":
        runner._apply(runner.driver.feed(STEER_CAPS, 20))
    else:
        runner._apply(runner.driver.feed(json.dumps({"id": 5, "result": {"turn": {"id": "turn-one"}}}), 20))
    runner.replay_caught_up = True
    assert runner.steerable


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_steer_commands_write_once_with_a_durable_tag(relayed, tmp_path, provider):
    runner, _, _, adir = relayed(provider=provider, recorded=True)
    ready_to_steer(runner, tmp_path)
    claim_steer(runner)
    runner.steer(STEER_MID)
    runner.steer(STEER_MID)
    runner._drain_commands()
    assert logged(adir).count(f"steer:{STEER_MID}") == 1
    assert runner.steer_written(STEER_MID)
    assert runner.steer_facts()[STEER_MID] == {"frame": "written", "fate": "unknown", "detail": None}
    assert runner.store.message(STEER_MID)["state"] == "steering"


@pytest.mark.parametrize("stop", ["host", "cancel", "interrupt", "close", "replay"])
def test_a_steer_frame_is_withdrawn_when_stop_cancel_or_close_wins_handover(relayed, tmp_path, stop):
    runner, _, _, adir = relayed(recorded=True)
    ready_to_steer(runner, tmp_path)
    claim_steer(runner)
    runner.outbox.extend(runner.driver.steer(STEER_MID, "second").frames)
    if stop == "host":
        runner.store.update_message(MID, stop_requested_at="2026-09-28T12:00:00Z")
    elif stop == "cancel":
        runner.store.set_state(STEER_MID, "cancelled", expect=("steering",), stop_requested_at="2026-09-28T12:00:00Z")
    elif stop == "interrupt":
        runner.interrupt()
    elif stop == "close":
        from subfleet.conversations.turn import Frame
        runner.outbox.insert(0, Frame("close", "close"))
    else:
        runner.replay_caught_up = False
    runner._send_outbox()
    assert f"steer:{STEER_MID}" not in logged(adir)
    row = runner.store.message(STEER_MID)
    assert row["state"] == ("cancelled" if stop == "cancel" else "queued")
    assert runner.steer_facts()[STEER_MID]["frame"] == "unsent"
    assert not runner.relay_failed


def test_a_steer_over_the_relay_cap_is_requeued_without_closing_the_host(relayed, tmp_path):
    runner, _, server, adir = relayed(recorded=True)
    ready_to_steer(runner, tmp_path)
    claim_steer(runner, text="x" * 2000)
    runner.relay.frame_max = 600
    runner.steer(STEER_MID)
    runner._drain_commands()
    assert logged(adir) == ["init", "user-message", "settings"]
    row = runner.store.message(STEER_MID)
    assert row["state"] == "queued" and row["state_reason"] == "steer-missed: frame-too-large"
    assert runner.driver.outcome is None and not runner.relay_failed and runner.frame_refused is None
    assert runner.steer_facts()[STEER_MID]["fate"] == "refused"


def test_a_relay_loss_records_unwritten_steers_without_losing_their_queue_position(relayed, tmp_path):
    runner, _, _, adir = relayed(recorded=True)
    ready_to_steer(runner, tmp_path)
    claim_steer(runner)
    before = runner.store.message(STEER_MID)["seq"]
    runner.outbox.extend(runner.driver.steer(STEER_MID, "second").frames)
    runner._relay_lost("gone")
    assert runner.outbox == [] and f"steer:{STEER_MID}" not in logged(adir)
    row = runner.store.message(STEER_MID)
    assert row["state"] == "queued" and row["seq"] == before
    assert runner.steer_facts()[STEER_MID]["detail"] == "relay-failed"


@pytest.mark.parametrize("reached", [True, False])
def test_a_lost_steer_receipt_is_resolved_before_stop_or_retry(relayed, tmp_path, reached):
    runner, clock, _, adir = relayed(recorded=True)
    ready_to_steer(runner, tmp_path)
    claim_steer(runner)
    real_send = runner.relay.send
    first = True

    def send(seq, op, line=None, tag=None, sig=None):
        nonlocal first
        if tag == f"steer:{STEER_MID}" and first:
            first = False
            if reached:
                real_send(seq, op, line=line, tag=tag, sig=sig)
            raise relay_module.RelayError("lost steer receipt")
        return real_send(seq, op, line=line, tag=tag, sig=sig)

    runner.relay.send = send
    runner.steer(STEER_MID)
    runner._drain_commands()
    assert runner.steer_written(STEER_MID)  # A cancel cannot guess while handover is ambiguous.
    runner.interrupt()
    clock.now += 100
    runner._send_outbox()
    assert logged(adir).count(f"steer:{STEER_MID}") == int(reached)
    assert runner.store.message(STEER_MID)["state"] == ("steering" if reached else "queued")
    assert runner.steer_facts()[STEER_MID]["frame"] == ("written" if reached else "unsent")


def test_replay_restores_a_written_steer_before_reading_stdout_and_never_writes_it_again(relayed, tmp_path):
    from subfleet.conversations.claude_turn import ClaudeTurn
    runner, _, _, adir = relayed(recorded=True)
    ready_to_steer(runner, tmp_path)
    claim_steer(runner)
    runner.steer(STEER_MID)
    runner._drain_commands()
    before = (adir / "stdin.jsonl").read_bytes()
    runner.driver = ClaudeTurn(runner.spec, read_bytes=runner._read_attachment)
    runner._restore_steers()
    assert runner.driver.steers[STEER_MID]["frame"] == "written"
    ready_to_steer(runner, tmp_path)
    runner._apply(runner.driver.feed(json.dumps({"type": "command_lifecycle", "command_uuid": STEER_MID,
                                               "state": "started"}), 25))
    runner.steer(STEER_MID)
    runner._drain_commands()
    assert (adir / "stdin.jsonl").read_bytes() == before
    assert runner.steer_facts()[STEER_MID]["fate"] == "delivered"
    runner._flush()
    events = runner.store.query("SELECT kind FROM events WHERE message_id=?", (MID,))
    assert [row["kind"] for row in events].count("steer.delivered") == 1


def test_replay_defers_an_unwritten_steer_until_the_provider_is_caught_up(relayed, tmp_path):
    runner, _, _, adir = relayed(recorded=True)
    claim_steer(runner)
    runner._restore_steers()
    assert runner._replay_steers == [STEER_MID] and runner.driver.steers == {}
    ready_to_steer(runner, tmp_path)
    for mid in runner._replay_steers:
        runner.steer(mid)
    runner._replay_steers.clear()
    runner._drain_commands()
    assert logged(adir).count(f"steer:{STEER_MID}") == 1


def test_ended_replay_settles_an_unwritten_steer_as_missed(relayed, tmp_path):
    runner, _, _, adir = relayed(recorded=True)
    claim_steer(runner)
    runner._restore_steers()
    runner.ended = True
    runner._discard_steers("provider-ended")
    assert runner.store.message(STEER_MID)["state"] == "queued"
    assert runner.steer_facts()[STEER_MID] == {"frame": "unsent", "fate": "refused", "detail": "provider-ended"}
    assert f"steer:{STEER_MID}" not in logged(adir)


def test_a_silently_dropped_steer_has_a_bounded_cancel_then_unknown_outcome(relayed, tmp_path):
    from subfleet.conversations.runner import STEER_GRACE_S
    runner, clock, _, adir = relayed(recorded=True)
    ready_to_steer(runner, tmp_path)
    claim_steer(runner)
    runner.steer(STEER_MID)
    runner._drain_commands()
    runner._apply(runner.driver.feed(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                               "user_message_uuids": [MID], "queued_turn_count": 0}), 30))
    assert runner.driver.outcome is None
    runner._timers()
    clock.now += STEER_GRACE_S
    runner._timers()
    assert f"cancel-steer:{STEER_MID}" in logged(adir) and runner.driver.outcome is None
    clock.now += STEER_GRACE_S
    runner._timers()
    assert runner.driver.outcome is not None and "close" in logged(adir)
    data = json.loads((adir / "turn.json").read_text())
    assert data["steers"][STEER_MID]["frame"] == "written"
    assert data["steers"][STEER_MID]["fate"] == "unknown"


def test_recorded_steer_fate_survives_replay_that_lacks_its_original_timer_evidence(make_runner):
    runner, _, _ = make_runner(Clocks())
    runner.recorded = {"state": "complete", "steers": {STEER_MID: {
        "frame": "written", "fate": "cancelled", "detail": "cancelled-after-result"}}}
    runner.driver.restore_steer(STEER_MID)
    assert runner.steer_facts()[STEER_MID]["fate"] == "cancelled"


def test_swapped_handover_stripes_are_acquired_in_one_global_order(make_runner, monkeypatch):
    """Two conversations may hash their host/steer ids onto opposite lock stripes."""
    import threading
    import time
    from subfleet.conversations.turn import Frame
    first, second = threading.Lock(), threading.Lock()
    ready = threading.Barrier(2)
    trace = []

    class SlowLock:
        def __init__(self, lock):
            self.lock = lock
        def __enter__(self):
            assert self.lock.acquire(timeout=2), "opposite stripe order deadlocked"
            trace.append((threading.get_ident(), id(self)))
            time.sleep(0.01)
        def __exit__(self, *args):
            self.lock.release()

    a, b = SlowLock(first), SlowLock(second)
    runners = [make_runner(Clocks())[0], make_runner(Clocks())[0]]
    errors = []
    for index, runner in enumerate(runners):
        runner.handover, other = ((a, b) if index == 0 else (b, a))
        runner.handover_for = lambda mid, lock=other: lock
        runner.handshaken = True
        runner.outbox = [Frame(f"steer:{STEER_MID}", "write", "{}")]
        monkeypatch.setattr(runner, "_steer_handover_verdict", lambda mid: "send")
        monkeypatch.setattr(runner, "_transmit", lambda frame, r=runner: bool(r.outbox.pop(0)))

    def work(runner):
        try:
            ready.wait(timeout=2)
            runner._send_outbox()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(r,)) for r in runners]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(4)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert len(trace) == 4
    assert [entry[1] for entry in trace] == sorted([id(a), id(b)]) * 2


# These generated schedules use the real relay apply/log implementation directly;
# no provider, socket thread, external state or live model is involved.
from contextlib import contextmanager
from hypothesis import given, settings, strategies as st


@contextmanager
def steer_schedule(provider):
    from subfleet.relay import Ack
    with tempfile.TemporaryDirectory(prefix="steer-property-", dir="/tmp") as directory:
        root = Path(directory)
        adir = root / "a1"
        adir.mkdir()
        store = ConversationStore(root / "state")
        choice = {"model": "opus[1m]" if provider == "claude" else "gpt-6", "effort": None,
                  "fast": False, "permission": "ask" if provider == "claude" else "read-only", "auto_continue": True}
        cid = store.create_conversation(provider=provider, workspace=str(root), workspace_kind="in-place",
                                        settings=choice, origin="new")[0]["conversation_id"]
        store.submit_message(conversation_id=cid, message_id=MID, after_message_id=None,
                             text="host", attachments=[], settings=choice)
        spec = TurnSpec(provider=provider, message_id=MID, text="host", model_id=choice["model"],
                        permission=choice["permission"], native_session_id=None,
                        new_session_id=SID if provider == "claude" else None, cwd=str(root))
        server = RelayServer(root / "unused.sock", adir / "stdin.jsonl")
        server._pipe = os.open(os.devnull, os.O_WRONLY)

        class DirectRelay:
            frame_max = 64 * 1024 * 1024
            def status(self):
                return server.status()["status"]
            def send(self, seq, op, *, line=None, tag=None, sig=None):
                body = server.apply({"seq": seq, "op": op, "line": line, "tag": tag, "sig": sig,
                                     "sha256": relay_module.frame_sha256(op, line, sig)})
                return Ack(seq=seq, ok=body["ok"], dup=body.get("dup", False), error=body.get("error"))
            def close(self):
                pass

        def build():
            runner = TurnRunner(store=store, attempt={"attempt_id": "job/a1"}, spec=spec,
                                conversation_id=cid, attempt_dir=adir, control_socket=str(root / "unused.sock"),
                                on_outcome=lambda r: None, on_contain=lambda a: None)
            runner.relay = DirectRelay()
            return runner
        try:
            yield build, root, adir
        finally:
            if server._pipe is not None:
                os.close(server._pipe)
            store.close()


@settings(max_examples=60, deadline=None)
@given(provider=st.sampled_from(["claude", "codex"]),
       actions=st.lists(st.sampled_from(["send", "stop", "cancel", "close"]), min_size=1, max_size=15))
def test_property_no_steer_write_after_stop_cancel_or_close(provider, actions):
    """Invariant 3: every generated schedule respects the first handover barrier."""
    from subfleet.conversations.turn import Frame
    with steer_schedule(provider) as (build, root, adir):
        runner = build()
        ready_to_steer(runner, root)
        claim_steer(runner)
        sequence = runner.store.message(STEER_MID)["seq"]
        barrier_count = None
        for action in actions:
            if action == "send":
                runner.steer(STEER_MID)
                runner._drain_commands()
            elif action == "stop":
                runner.store.update_message(MID, stop_requested_at="2026-09-28T12:00:00Z")
            elif action == "cancel":
                if not runner.steer_written(STEER_MID):
                    runner.store.set_state(STEER_MID, "cancelled", expect=("steering",))
            else:
                runner.outbox.append(Frame("close", "close"))
                runner._send_outbox()
            count = logged(adir).count(f"steer:{STEER_MID}")
            assert count <= 1
            if barrier_count is not None:
                assert count == barrier_count
            if action in ("stop", "cancel", "close"):
                barrier_count = count
        assert runner.store.message(STEER_MID)["seq"] == sequence


@settings(max_examples=40, deadline=None)
@given(provider=st.sampled_from(["claude", "codex"]),
       restarts=st.lists(st.integers(min_value=0, max_value=4), min_size=0, max_size=8))
def test_property_replay_at_each_steer_boundary_writes_once_and_preserves_fate(provider, restarts):
    """Invariant 4: crashes before/after claim, handover, echo and result are idempotent."""
    with steer_schedule(provider) as (build, root, adir):
        runner = build()
        ready_to_steer(runner, root)
        transcript = ([INIT_OK, STEER_CAPS] if provider == "claude" else
                      codex_replies(str(root)) + [json.dumps({"id": 5, "result": {"turn": {"id": "turn-one"}}})])
        claim_steer(runner)
        for boundary in range(5):
            if boundary == 1:
                runner.steer(STEER_MID)
            elif boundary == 2:
                runner._drain_commands()
            elif boundary == 3:
                row = ({"type": "command_lifecycle", "command_uuid": STEER_MID, "state": "completed"}
                       if provider == "claude" else
                       {"method": "item/completed", "params": {"threadId": runner.driver.thread_id,
                        "turnId": "turn-one", "item": {"type": "userMessage", "id": "u1", "clientId": STEER_MID,
                                                           "content": []}}})
                transcript.append(json.dumps(row))
                runner._apply(runner.driver.feed(transcript[-1], len(transcript) * 100))
            elif boundary == 4:
                row = ({"type": "result", "subtype": "success", "is_error": False,
                        "user_message_uuids": [MID, STEER_MID], "queued_turn_count": 0}
                       if provider == "claude" else
                       {"method": "turn/completed", "params": {"threadId": runner.driver.thread_id,
                        "turn": {"id": "turn-one", "status": "completed"}}})
                transcript.append(json.dumps(row))
                runner._apply(runner.driver.feed(transcript[-1], len(transcript) * 100))
            for _ in range(restarts.count(boundary)):
                runner._flush()
                runner = build()
                runner._restore_steers()
                runner._apply(runner.driver.start())
                for index, line in enumerate(transcript):
                    runner._apply(runner.driver.feed(line, (index + 1) * 100))
                runner.replay_caught_up = True
                for mid in runner._replay_steers:
                    runner.steer(mid)
                runner._replay_steers.clear()
                runner._drain_commands()
            assert logged(adir).count(f"steer:{STEER_MID}") <= 1
        assert logged(adir).count(f"steer:{STEER_MID}") == 1
        assert runner.steer_facts()[STEER_MID]["fate"] == ("consumed" if provider == "claude" else "unanswered")
        assert runner.driver.outcome.state == "complete"


@pytest.mark.parametrize("when", ["command", "replay"])
def test_a_cancel_before_the_steer_command_is_drained_remains_in_the_attempt_audit(relayed, tmp_path, when):
    runner, _, _, adir = relayed(recorded=True)
    ready_to_steer(runner, tmp_path)
    claim_steer(runner)
    runner.store.withdraw(STEER_MID, expect=("steering",), stop_at="2026-09-28T12:00:00Z")
    if when == "command":
        runner.steer(STEER_MID)
        runner._drain_commands()
    else:
        runner._restore_steers()
    assert runner.store.message(STEER_MID)["state"] == "cancelled"
    assert runner.steer_facts()[STEER_MID] == {
        "frame": "unsent", "fate": "cancelled", "detail": "cancelled-before-handover"}
    assert f"steer:{STEER_MID}" not in logged(adir)


def test_a_steer_discovered_by_the_status_handshake_is_restored_before_stdout_replay(relayed, tmp_path):
    runner, _, server, adir = relayed(recorded=True)
    ready_to_steer(runner, tmp_path)
    claim_steer(runner)
    runner._restore_steers()
    assert runner._replay_steers == [STEER_MID]
    frame = json.dumps({"type": "user", "uuid": STEER_MID, "priority": "next", "message": {"role": "user", "content": []}})
    # Another runner's already accepted request finishes after this runner's log snapshot.
    server.apply({"seq": server.last_applied + 1, "op": "write", "line": frame, "tag": f"steer:{STEER_MID}",
                  "sha256": relay_module.line_sha256(frame)})
    runner.handshaken = False
    assert runner._handshake()
    assert runner._replay_steers == []
    runner._apply(runner.driver.feed(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                               "user_message_uuids": [MID, STEER_MID]}), 30))
    assert runner.steer_facts()[STEER_MID]["fate"] == "consumed"
    assert logged(adir).count(f"steer:{STEER_MID}") == 1


@pytest.mark.parametrize("fate", ["cancelled", "refused", "unknown"])
def test_recorded_positive_delivery_evidence_wins_negative_replayed_evidence(make_runner, fate):
    runner, _, _ = make_runner(Clocks())
    runner.recorded = {"state": "complete", "steers": {STEER_MID: {
        "frame": "written", "fate": "consumed", "detail": None}}}
    runner.driver.restore_steer(STEER_MID)
    runner.driver.steers[STEER_MID]["fate"] = fate
    assert runner.steer_facts()[STEER_MID]["fate"] == "consumed"


def test_a_steer_pending_at_construction_is_registered_before_its_written_handshake(relayed, tmp_path):
    import threading
    import time

    def pending(server, adir):
        for seq, tag in enumerate(("init", "user-message", "settings"), 1):
            server.apply({"seq": seq, "op": "write", "line": "{}", "tag": tag,
                          "sha256": relay_module.line_sha256("{}")})
        server._lock.acquire()
        intent = logged_intent(4, f"steer:{STEER_MID}", "{}")
        server._append(intent)
        server._records.append({**intent, "status": "pending"})

    runner, _, server, adir = relayed(recorded=True, before_runner=pending)
    claim_steer(runner)
    assert runner.sent[f"steer:{STEER_MID}"] == "pending"
    runner._restore_steers()
    assert runner.driver.steers[STEER_MID]["frame"] == "written"

    def finish():
        time.sleep(0.05)
        server._append({"kind": "written", "seq": 4})
        server._records[-1]["status"] = "written"
        server._lock.release()

    threading.Thread(target=finish, daemon=True).start()
    ready_to_steer(runner, tmp_path)
    runner._apply(runner.driver.feed(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                               "user_message_uuids": [MID, STEER_MID]}), 30))
    assert not runner.relay_failed
    assert logged(adir).count(f"steer:{STEER_MID}") == 1
    assert runner.steer_facts()[STEER_MID]["fate"] == "consumed"


@pytest.mark.parametrize("recorded,replayed,want", [("consumed", "delivered", "consumed"),
                                                   ("delivered", "unanswered", "delivered"),
                                                   ("unanswered", "delivered", "delivered")])
def test_replay_refines_positive_evidence_without_downgrading_a_recorded_answer(make_runner, recorded, replayed, want):
    runner, _, _ = make_runner(Clocks())
    runner.recorded = {"state": "complete", "steers": {STEER_MID: {
        "frame": "written", "fate": recorded, "detail": None}}}
    runner.driver.restore_steer(STEER_MID)
    runner.driver.steers[STEER_MID]["fate"] = replayed
    assert runner.steer_facts()[STEER_MID]["fate"] == want


def test_reclaiming_a_previously_missed_steer_requeues_promptly_instead_of_waiting_for_host_end(relayed, tmp_path):
    runner, _, _, adir = relayed(recorded=True)
    ready_to_steer(runner, tmp_path)
    claim_steer(runner, text="x" * 2000)
    runner.relay.frame_max = 600
    runner.steer(STEER_MID)
    runner._drain_commands()
    assert runner.store.message(STEER_MID)["state"] == "queued"
    runner.store.set_state(STEER_MID, "steering", reason=f"steer:{MID}", expect=("queued",))
    runner.steer(STEER_MID)
    runner._drain_commands()
    assert runner.store.message(STEER_MID)["state"] == "queued"
    assert f"steer:{STEER_MID}" not in logged(adir)


# --- a steer's images (C-24.9, C-28.1): by digest; a steer never ends its host's runner -----

PNG = b"\x89PNG\r\n\x1a\n" + b"a screenshot pasted mid-turn"


def stored_image(runner, tmp_path, data=PNG) -> str:
    from subfleet.conversations import attachments
    original = tmp_path / f"pasted-{len(data)}.png"
    original.write_bytes(data)
    return attachments.add(runner.store, str(original))["sha256"]


def claim_image_steer(runner, sha, *, message_id=STEER_MID, text="look at this"):
    host = runner.store.message(MID)
    runner.store.submit_message(conversation_id=runner.conversation_id, message_id=message_id,
                                after_message_id=MID, text=text, attachments=[sha], settings=host["settings"])
    runner.store.set_state(message_id, "steering", reason=f"steer:{MID}", expect=("queued",))
    return message_id


def steer_payload(adir, mid) -> dict:
    [record] = [r for r in read_log(adir / "stdin.jsonl") if r["tag"] == f"steer:{mid}"]
    return json.loads(record["line"])


def remove_stored_copy(runner, sha) -> None:
    Path(runner.store.attachment(sha)["path"]).unlink()


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_steer_carries_an_image_its_host_does_not_have(relayed, tmp_path, provider):
    """The blocker of the 2026-09-28 steer review: a steer's images were looked up in the
    host's own spec (StopIteration), which ended the runner thread. They resolve by digest."""
    import base64
    runner, _, _, adir = relayed(provider=provider, recorded=True)
    ready_to_steer(runner, tmp_path)
    assert runner.spec.images == ()                  # the host carries no image at all
    sha = stored_image(runner, tmp_path)
    claim_image_steer(runner, sha)
    runner.steer(STEER_MID)
    runner._drain_commands()
    assert logged(adir).count(f"steer:{STEER_MID}") == 1
    payload = steer_payload(adir, STEER_MID)
    if provider == "claude":
        image = payload["message"]["content"][1]
        assert image["source"] == {"type": "base64", "media_type": "image/png",
                                   "data": base64.b64encode(PNG).decode("ascii")}
    else:
        image = payload["params"]["input"][1]
        assert image == {"type": "localImage", "path": str(adir / f"image-{sha}.png")}
        assert (adir / f"image-{sha}.png").read_bytes() == PNG
    assert runner.store.message(STEER_MID)["state"] == "steering"
    assert runner.steer_facts()[STEER_MID] == {"frame": "written", "fate": "unknown", "detail": None}
    assert runner.steerable


def break_steer(runner, tmp_path, broken: str) -> str:
    """Claim a steer whose input cannot be built, in one of three ways."""
    if broken == "missing-image":
        sha = stored_image(runner, tmp_path)
        remove_stored_copy(runner, sha)             # gone from the state root after it was attached
        return claim_image_steer(runner, sha)
    if broken == "changed-image":
        sha = stored_image(runner, tmp_path)
        Path(runner.store.attachment(sha)["path"]).write_bytes(PNG + b"damaged")   # other bytes than its digest
        return claim_image_steer(runner, sha)
    claim_steer(runner)                              # a driver defect: any exception, not only I/O

    def defect(*args, **kwargs):
        raise StopIteration
    runner.driver.steer = defect
    return STEER_MID


@pytest.mark.parametrize("broken", ["missing-image", "changed-image", "driver-defect"])
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_steer_whose_input_fails_goes_back_to_the_queue_and_the_host_runs_on(relayed, tmp_path, provider, broken):
    runner, _, _, adir = relayed(provider=provider, recorded=True)
    ready_to_steer(runner, tmp_path)
    mid = break_steer(runner, tmp_path, broken)
    seq = runner.store.message(mid)["seq"]
    runner.steer(mid)
    runner._drain_commands()                        # raised StopIteration before the fix
    row = runner.store.message(mid)
    assert row["state"] == "queued" and row["seq"] == seq
    assert row["state_reason"].startswith("steer-missed: input-unavailable: ")
    assert f"steer:{mid}" not in logged(adir)
    assert runner.steer_facts()[mid]["frame"] == "unsent" and runner.steer_facts()[mid]["fate"] == "refused"
    runner._flush()
    kinds = [r["kind"] for r in runner.store.query("SELECT kind FROM events WHERE message_id=?", (MID,))]
    assert kinds.count("steer.missed") == 1
    # The host is untouched: it still takes steers, and a stop still reaches it.
    assert runner.driver.outcome is None and runner.steerable
    if broken == "driver-defect":
        del runner.driver.steer                     # the instance attribute: the class's steer again
    later = "7f1c9a0e-3333-4222-8333-444455556666"
    runner.store.submit_message(conversation_id=runner.conversation_id, message_id=later, after_message_id=mid,
                                text="and this", attachments=[], settings=runner.store.message(MID)["settings"])
    runner.store.set_state(later, "steering", reason=f"steer:{MID}", expect=("queued",))
    runner.steer(later)
    runner._drain_commands()
    assert logged(adir).count(f"steer:{later}") == 1
    runner.interrupt()
    runner._drain_commands()
    runner._send_outbox()
    assert "interrupt" in logged(adir)


def provider_stdout(provider: str, cwd: str) -> list[str]:
    """What the provider printed up to a running, steerable turn."""
    if provider == "claude":
        return [INIT_OK, STEER_CAPS]
    return [*codex_replies(cwd), json.dumps({"id": 5, "result": {"turn": {"id": "turn-one"}}})]


def wait_until(predicate, timeout=60.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.mark.parametrize("image", ["not-the-host-s", "missing"])
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_the_runner_thread_survives_an_image_steer_and_stop_still_reaches_the_turn(relayed, tmp_path, provider,
                                                                                    image):
    """The real loop (`TurnRunner._run`): the thread is alive after the steer, the steer
    is delivered or back in the queue, and Stop still writes the provider's interrupt."""
    runner, _, _, adir = relayed(provider=provider, recorded=True)
    (adir / "stdout").write_text("".join(line + "\n" for line in provider_stdout(provider, str(tmp_path))))
    sha = stored_image(runner, tmp_path)
    if image == "missing":
        remove_stored_copy(runner, sha)
    runner.start()
    try:
        wait_until(lambda: runner.steerable)
        claim_image_steer(runner, sha)
        runner.steer(STEER_MID)
        if image == "missing":
            wait_until(lambda: runner.store.message(STEER_MID)["state"] == "queued")
            assert f"steer:{STEER_MID}" not in logged(adir)
        else:
            wait_until(lambda: f"steer:{STEER_MID}" in logged(adir))
            assert runner.store.message(STEER_MID)["state"] == "steering"
        assert runner._thread.is_alive() and not runner.finished.is_set()
        runner.interrupt()
        wait_until(lambda: "interrupt" in logged(adir))
        assert runner._thread.is_alive() and not runner.finished.is_set()
    finally:
        runner.stop()
        assert runner.join(30)


def test_the_runner_s_watchdog_never_cancels_a_steer_queued_during_another_steer_s_own_turn(relayed, tmp_path):
    """Steer review finding 8, through the runner's clock: steer A missed the host's last
    tool boundary and runs as its own turn; B, steered during A's long tool call, waits
    for A's next boundary. However long that takes, nothing cancels B or ends the turn."""
    from subfleet.conversations.runner import STEER_GRACE_S
    runner, clock, _, adir = relayed(recorded=True)
    ready_to_steer(runner, tmp_path)
    claim_steer(runner)
    runner.steer(STEER_MID)
    runner._drain_commands()

    def feed(row, offset):
        runner._apply(runner.driver.feed(json.dumps(row), offset))

    feed({"type": "result", "subtype": "success", "is_error": False, "user_message_uuids": [MID],
          "queued_turn_count": 1}, 30)
    feed({"type": "command_lifecycle", "command_uuid": STEER_MID, "state": "started"}, 31)
    second = "7f1c9a0e-3333-4222-8333-444455556666"
    runner.store.submit_message(conversation_id=runner.conversation_id, message_id=second,
                                after_message_id=STEER_MID, text="and this", attachments=[],
                                settings=runner.store.message(MID)["settings"])
    runner.store.set_state(second, "steering", reason=f"steer:{MID}", expect=("queued",))
    runner.steer(second)
    runner._drain_commands()
    feed({"type": "command_lifecycle", "command_uuid": second, "state": "queued"}, 32)
    for _ in range(8):                                  # two minutes of A's tool call
        clock.now += STEER_GRACE_S
        runner._timers()
        runner._send_outbox()
    assert not [tag for tag in logged(adir) if tag.startswith("cancel-steer:")]
    assert runner.driver.outcome is None and runner.steerable and "close" not in logged(adir)
    feed({"type": "command_lifecycle", "command_uuid": second, "state": "started"}, 33)
    feed({"type": "result", "subtype": "success", "is_error": False, "user_message_uuids": [STEER_MID, second],
          "queued_turn_count": 0}, 40)
    facts = runner.steer_facts()
    assert runner.driver.outcome.state == "complete"
    assert (facts[STEER_MID]["fate"], facts[second]["fate"]) == ("consumed", "consumed")


def test_the_watchdog_s_15_s_start_again_with_each_spell_of_waiting_even_one_between_polls(relayed, tmp_path):
    """C-26.5: the host's result is held for S1, unseen, and the watchdog's clock starts.
    S1 then runs as its own turn and its result is held for S2, all read in one batch
    between two polls. S2 has been unseen for less than 15 s at the next poll, so it is
    not cancelled then; 15 s after that poll it is."""
    from subfleet.conversations.runner import STEER_GRACE_S
    runner, clock, _, adir = relayed(recorded=True)
    ready_to_steer(runner, tmp_path)
    claim_steer(runner)
    runner.steer(STEER_MID)
    runner._drain_commands()

    def feed(row, offset):
        runner._apply(runner.driver.feed(json.dumps(row), offset))

    feed({"type": "result", "subtype": "success", "is_error": False, "user_message_uuids": [MID],
          "queued_turn_count": 0}, 30)
    runner._timers()                                    # S1 unseen: the clock starts
    assert runner.steer_wait_since == clock.now
    second = "7f1c9a0e-3333-4222-8333-444455556666"
    runner.store.submit_message(conversation_id=runner.conversation_id, message_id=second,
                                after_message_id=STEER_MID, text="and this", attachments=[],
                                settings=runner.store.message(MID)["settings"])
    runner.store.set_state(second, "steering", reason=f"steer:{MID}", expect=("queued",))
    runner.steer(second)
    runner._drain_commands()
    clock.now += STEER_GRACE_S + 5                      # a stall: one batch holds all of this
    feed({"type": "command_lifecycle", "command_uuid": STEER_MID, "state": "started"}, 31)
    feed({"type": "result", "subtype": "success", "is_error": False, "user_message_uuids": [STEER_MID],
          "queued_turn_count": 0}, 32)
    assert runner.driver.steer_waiting                  # held again, now for S2
    runner._timers()
    runner._send_outbox()
    assert not [tag for tag in logged(adir) if tag.startswith("cancel-steer:")]
    clock.now += STEER_GRACE_S
    runner._timers()
    runner._send_outbox()
    assert [tag for tag in logged(adir) if tag.startswith("cancel-steer:")] == [f"cancel-steer:{second}"]
