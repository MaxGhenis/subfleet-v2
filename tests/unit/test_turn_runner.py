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

    def make(*, text="hi", server_class=RelayServer, clocks=Clocks(), serve=True, before_runner=None):
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
        spec = TurnSpec(provider="claude", message_id=MID, text=text, model_id="opus[1m]", permission="ask",
                        native_session_id=None, new_session_id=SID)
        runner = TurnRunner(store=store, attempt={"attempt_id": "job/a1", "lane_id": "claude-1"}, spec=spec,
                            conversation_id="cv-x", attempt_dir=adir, control_socket=str(short / "r.sock"),
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
