"""Round 8: C-9.2 orders provider exits against every D-13 stop delivery.

Real relay application, synthetic child, no process signals or runner threads.
"""
from datetime import timedelta
from dataclasses import replace
import json
import os
import signal
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, Phase, given, settings, strategies as st

from subfleet import daemon as dm
from subfleet.conversations.claude_turn import ClaudeTurn
from subfleet.conversations.codex_turn import CodexTurn
from subfleet.conversations.runner import Clocks, TurnRunner
from subfleet.conversations.turn import Frame, TurnSpec
from subfleet.relay import Ack, RelayClient, RelayError, RelayServer, frame_sha256
from tests.fake.test_review_r223_wall_probes import clocked  # noqa: F401
from tests.fake.test_state_contract import reserve, state_daemon  # noqa: F401
from tests.fake.test_wall_cancel_races import setup_running, publish


@pytest.fixture
def stopped_turn(clocked, monkeypatch):
    service, _, clock, jid, aid, adir = setup_running(clocked, monkeypatch)
    service.store.update_job(jid, kind="turn")
    monkeypatch.setattr(service, "_turn_trees", lambda *a, **kw: {})
    monkeypatch.setattr(service.conversations, "stop", type(service.conversations).stop.__get__(service.conversations))
    spec = TurnSpec(provider="codex", message_id="aaaaaaaa-1111-4222-8333-444455556666",
                    text="hi", model_id="gpt-6-astra", permission="read-only", native_session_id=None)
    elapsed = [1000.0]
    runner = TurnRunner(store=service.conversations.store, attempt=service.store.get_attempt(aid),
                        spec=spec, conversation_id="cv-synthetic", attempt_dir=adir,
                        control_socket=str(adir / "none.sock"), on_outcome=lambda r: None,
                        on_contain=lambda a: pytest.fail("must finish before containment"),
                        clocks=Clocks(), clock=lambda: elapsed[0])
    delivered = []
    server = RelayServer(adir / "none.sock", adir / "stdin.jsonl")
    reader, server._pipe = os.pipe()
    server._child = SimpleNamespace(poll=lambda: None,
                                    send_signal=lambda sig: delivered.append((sig, clock.t)))

    def send(seq, op, *, tag, line=None, sig=None, before_send=None):
        if before_send is not None:
            before_send()
        answer = server.apply({"seq": seq, "op": op, "line": line, "sig": sig, "tag": tag,
                               "sha256": frame_sha256(op, line, sig)})
        return Ack(**{k: answer[k] for k in ("seq", "ok", "error") if k in answer})

    monkeypatch.setattr(runner.relay, "send", send)
    monkeypatch.setattr(runner.relay, "status", server.status)
    service.conversations.runners[aid] = runner
    service._process_attempt(aid)
    assert service.store.get_attempt(aid)["killed_by"] == "max_wall_s"
    assert runner.stop_reason == "wall-limit"
    try:
        yield service, clock, jid, aid, adir, runner, server, reader, delivered, elapsed
    finally:
        service.conversations.runners.pop(aid, None)
        runner.relay.close()
        server._close_pipe()
        os.close(reader)


def finish(service, clock, jid, aid, adir, *, exited=None):
    publish(adir, (exited or clock.t).isoformat())
    (adir / "turn.json").write_text(json.dumps({"state": "complete", "final_text": "verified turn result",
                                              "accepted": True, "answered": True}))
    service._process_attempt(aid)
    service._process_attempt(aid)
    return service.store.get_job(jid), service.store.get_attempt(aid)


def assert_cancelled(job, attempt):
    assert (job["state"], job["rc"], attempt["state"], attempt["outcome_class"], attempt["killed_by"]) == (
        "cancelled", 130, "interrupted", "unknown", "max_wall_s")


def stop_events(service, aid):
    return [json.loads(r["data_json"]) for r in service.store.query(
        "SELECT data_json FROM events WHERE attempt_id=? AND kind='attempt.stop_delivery' ORDER BY event_id", (aid,))]


def transmit(runner, frame):
    runner.outbox.append(frame)
    assert runner._transmit(frame)


def approval_reply(runner, provider, decision):
    """Build the provider's actual reply, with an active turn and no queued steers."""
    runner.spec = replace(runner.spec, provider=provider)
    if provider == "claude":
        runner.driver = ClaudeTurn(runner.spec, read_bytes=lambda image: b"")
        runner.driver.phase = "sent"
        runner.driver.pending["wall-approval"] = {"tool_name": "Bash", "tool_use_id": "tool-1"}
        step = runner.driver.respond("wall-approval", decision, answers={"Branch?": "dev"})
    else:
        runner.driver = CodexTurn(runner.spec)
        runner.driver.thread_id, runner.driver.turn_id, runner.driver.phase = "thread-1", "turn-1", "running"
        method = "item/permissions/requestApproval" if decision == "allow-turn" else "item/commandExecution/requestApproval"
        runner.driver.pending["wall-approval"] = (method, {"permissions": {"network": {"enabled": True}}})
        step = runner.driver.respond("wall-approval", decision)
    [frame] = step.frames
    return frame


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_approval_cancel_turn_after_wall_keeps_wall_cancel(stopped_turn, monkeypatch, provider):
    service, clock, jid, aid, adir, runner, _, reader, delivered, _ = stopped_turn
    frame = approval_reply(runner, provider, "cancel-turn")
    send = runner.relay.send
    def inspected(*args, **kwargs):
        [event] = stop_events(service, aid)
        assert event["signal_pending"] is True and event["tag"] == frame.tag
        return send(*args, **kwargs)
    monkeypatch.setattr(runner.relay, "send", inspected)
    transmit(runner, frame)
    payload = json.loads(os.read(reader, 4096))
    if provider == "claude":
        assert payload["response"]["response"]["interrupt"] is True
    else:
        assert payload["result"]["decision"] == "cancel"
    assert runner.driver.interrupt().frames == []
    assert delivered == []
    clock.t += timedelta(seconds=2)
    completion = ({"type": "result", "subtype": "success", "is_error": False, "result": "verified turn result"}
                  if provider == "claude" else
                  {"method": "turn/completed", "params": {"threadId": "thread-1",
                   "turn": {"id": "turn-1", "status": "completed"}}})
    runner.driver.feed(json.dumps(completion), 100)
    assert runner.driver.outcome.state == "complete"
    # Settle in the real publication-to-close window, before another stop can mask this one.
    runner.final_text = "verified turn result"
    runner._write_outcome()
    publish(adir, clock.t.isoformat())
    service._process_attempt(aid)
    service._process_attempt(aid)
    assert_cancelled(service.store.get_job(jid), service.store.get_attempt(aid))


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("decision", ["allow", "cancel-turn"])
def test_approval_after_wall_preserves_escalation(stopped_turn, monkeypatch, provider, decision):
    service, clock, _, aid, _, runner, _, reader, delivered, elapsed = stopped_turn
    frame = approval_reply(runner, provider, decision)
    assert frame.stops is (decision == "cancel-turn")
    transmit(runner, frame)
    os.read(reader, 4096)
    assert len(stop_events(service, aid)) == (decision == "cancel-turn")
    # Drain the queued wall interrupt: it is redundant only for cancel-turn.
    runner._stop_on_catch_up = False
    monkeypatch.setattr(runner, "_send_outbox", lambda: None)
    runner._drain_commands()
    assert runner.stop_at == elapsed[0]
    if decision == "cancel-turn":
        assert runner.outbox == []
    else:
        [interrupt] = runner.outbox
        assert interrupt.stops is True
        assert runner._transmit(interrupt)
        os.read(reader, 4096)
    contained = []
    runner.on_contain = contained.append
    for seconds in (runner.clocks.sigint_after_s, runner.clocks.close_after_s, runner.clocks.contain_after_s):
        elapsed[0] = runner.stop_at + seconds + .1
        clock.t += timedelta(seconds=seconds + .1)
        runner._timers()
        for queued in list(runner.outbox):
            assert queued.stops is True
            assert runner._transmit(queued)
    assert [sig for sig, _ in delivered] == [signal.SIGINT]
    assert [e["op"] for e in stop_events(service, aid)][-2:] == ["signal", "close"]
    assert contained == [aid]


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_exit_before_approval_cancel_transmission_keeps_receipt_wins(stopped_turn, monkeypatch, provider):
    service, clock, jid, aid, adir, runner, server, _, _, _ = stopped_turn
    frame = approval_reply(runner, provider, "cancel-turn")
    exited = clock.t + timedelta(seconds=1)
    replies = []
    def sendall(payload):
        answer = server.apply(json.loads(payload))
        replies.append((json.dumps(answer) + "\n").encode())
    def connect():
        clock.t += timedelta(seconds=2)
        runner.relay._sock = SimpleNamespace(sendall=sendall, close=lambda: None)
        runner.relay._reader = SimpleNamespace(readline=lambda cap: replies.pop(), close=lambda: None)
    monkeypatch.setattr(runner.relay, "_connect", connect)
    monkeypatch.setattr(runner.relay, "send", RelayClient.send.__get__(runner.relay))
    transmit(runner, frame)
    [event] = stop_events(service, aid)
    assert event["signal_sent"] is True
    assert event["signal_at"] == clock.t.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    job, attempt = finish(service, clock, jid, aid, adir, exited=exited)
    assert (job["state"], job["rc"], attempt["outcome_class"], attempt["killed_by"]) == ("succeeded", 0, "ok", None)


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("transmitted", [False, True])
def test_approval_cancel_lost_ack_is_conservative(stopped_turn, monkeypatch, provider, transmitted):
    service, clock, jid, aid, adir, runner, *_ = stopped_turn
    frame = approval_reply(runner, provider, "cancel-turn")
    send = runner.relay.send
    def lost(*args, **kwargs):
        if transmitted:
            send(*args, **kwargs)
        raise RelayError("approval reply lost")
    monkeypatch.setattr(runner.relay, "send", lost)
    runner.outbox.append(frame)
    assert not runner._transmit(frame)
    [event] = stop_events(service, aid)
    assert event["signal_pending"] is True
    clock.t += timedelta(seconds=2)
    assert_cancelled(*finish(service, clock, jid, aid, adir))


def test_relay_sigint_must_not_be_superseded_as_no_signal(stopped_turn):
    service, clock, jid, aid, adir, runner, _, _, delivered, elapsed = stopped_turn
    runner.stop_at = elapsed[0]
    elapsed[0] += 10.1
    clock.t += timedelta(seconds=10.1)
    runner._timers()
    [frame] = runner.outbox
    assert runner._transmit(frame)
    assert delivered == [(signal.SIGINT, clock.t)]
    clock.t += timedelta(seconds=2)
    assert_cancelled(*finish(service, clock, jid, aid, adir))
    assert not service.store.one("SELECT 1 FROM events WHERE job_id=? AND kind='job.wall_cancel_superseded'", (jid,))


def test_provider_interrupt_alone_keeps_wall_cancel(stopped_turn):
    service, clock, jid, aid, adir, runner, _, reader, delivered, _ = stopped_turn
    runner.driver.thread_id, runner.driver.turn_id, runner.driver.phase = "thread-1", "turn-1", "running"
    [frame] = runner.driver.interrupt().frames
    transmit(runner, frame)
    assert json.loads(os.read(reader, 4096))["method"] == "turn/interrupt"
    assert delivered == []
    clock.t += timedelta(seconds=2)
    assert_cancelled(*finish(service, clock, jid, aid, adir))


def test_exit_before_first_delivery_keeps_receipt_wins(stopped_turn):
    service, clock, jid, aid, adir, runner, _, _, delivered, _ = stopped_turn
    # interrupt() only queued a command. No stop has reached the provider yet.
    assert not runner.commands.empty() and delivered == []
    exited = clock.t
    receipt = {"rc": 0, "finished_at": exited.isoformat()}
    assert service._receipt_precedes_signal(runner.attempt, receipt)
    clock.t += timedelta(seconds=2)
    transmit(runner, Frame("signal:int", "signal", "INT"))
    [event] = stop_events(service, aid)
    assert event["signal_sent"] is True and event["signal_at"]
    job, attempt = finish(service, clock, jid, aid, adir, exited=exited)
    assert (job["state"], job["rc"], attempt["outcome_class"], attempt["killed_by"]) == ("succeeded", 0, "ok", None)


def test_exit_during_relay_connection_before_transmission_keeps_receipt_wins(stopped_turn, monkeypatch):
    """Like kill-protocol identity reads, connecting precedes the delivery clock."""
    service, clock, jid, aid, adir, runner, server, _, delivered, _ = stopped_turn
    exited = clock.t + timedelta(seconds=1)
    reply = []
    def sendall(payload):
        answer = server.apply(json.loads(payload))
        reply.append((json.dumps(answer) + "\n").encode())
    def connect():
        clock.t += timedelta(seconds=2)
        runner.relay._sock = SimpleNamespace(sendall=sendall, close=lambda: None)
        runner.relay._reader = SimpleNamespace(readline=lambda cap: reply.pop(), close=lambda: None)
    monkeypatch.setattr(runner.relay, "_connect", connect)
    monkeypatch.setattr(runner.relay, "send", RelayClient.send.__get__(runner.relay))
    transmit(runner, Frame("signal:int", "signal", "INT"))
    assert delivered == [(signal.SIGINT, clock.t)]
    [event] = stop_events(service, aid)
    assert event["signal_at"] == clock.t.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    job, attempt = finish(service, clock, jid, aid, adir, exited=exited)
    assert (job["state"], attempt["outcome_class"], attempt["killed_by"]) == ("succeeded", "ok", None)


def test_restart_after_intent_before_first_transmission_is_conservative(stopped_turn, monkeypatch):
    service, clock, jid, aid, adir, runner, _, _, delivered, _ = stopped_turn
    class Crash(BaseException):
        pass
    def crash(*a, **kw):
        raise Crash
    monkeypatch.setattr(runner.relay, "send", crash)
    runner.outbox.append(Frame("interrupt", "write", '{"method":"turn/interrupt"}'))
    with pytest.raises(Crash):
        runner._transmit(runner.outbox[0])
    assert delivered == []
    # Reconstruct the daemon, losing every in-memory clock and pending command.
    service.conversations.runners.pop(aid)
    service.close()
    successor = dm.Daemon(service.root)
    try:
        monkeypatch.setattr(successor, "_turn_trees", lambda *a, **kw: {})
        clock.t += timedelta(seconds=2)
        assert_cancelled(*finish(successor, clock, jid, aid, adir))
    finally:
        successor.close()


@pytest.mark.parametrize("frame", [Frame("close", "close"), Frame("signal:int:late", "signal", "INT"),
                                  Frame("signal:escalation", "signal", "INT")])
def test_every_relay_stop_frame_keeps_wall_cancel(stopped_turn, frame):
    service, clock, jid, aid, adir, runner, *_ = stopped_turn
    transmit(runner, frame)
    clock.t += timedelta(seconds=2)
    assert_cancelled(*finish(service, clock, jid, aid, adir))


def test_stop_intent_is_durable_before_relay_transmission(stopped_turn, monkeypatch):
    service, clock, _, aid, _, runner, *_ = stopped_turn
    send = runner.relay.send
    def inspected(*a, **kw):
        [event] = stop_events(service, aid)
        assert event["signal_pending"] is True and event["via"] == "relay"
        return send(*a, **kw)
    monkeypatch.setattr(runner.relay, "send", inspected)
    transmit(runner, Frame("signal:int", "signal", "INT"))
    [event] = stop_events(service, aid)
    assert event["signal_sent"] is True
    assert event["signal_at"] == clock.t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@pytest.mark.parametrize("transmitted", [False, True])
def test_lost_stop_ack_is_conservative(stopped_turn, monkeypatch, transmitted):
    service, clock, jid, aid, adir, runner, *_ = stopped_turn
    send = runner.relay.send
    def lost(*a, **kw):
        if transmitted:
            send(*a, **kw)
        raise RelayError("reply lost")
    monkeypatch.setattr(runner.relay, "send", lost)
    frame = Frame("signal:int", "signal", "INT")
    runner.outbox.append(frame)
    assert not runner._transmit(frame)
    clock.t += timedelta(seconds=2)
    assert_cancelled(*finish(service, clock, jid, aid, adir))


def test_relay_no_child_proves_no_delivery(stopped_turn):
    service, clock, jid, aid, adir, runner, server, *_ = stopped_turn
    server._child = None
    transmit(runner, Frame("signal:int", "signal", "INT"))
    [event] = stop_events(service, aid)
    assert event["signal_sent"] is False
    job, attempt = finish(service, clock, jid, aid, adir)
    assert (job["state"], attempt["killed_by"]) == ("succeeded", None)


@settings(max_examples=60, deadline=None, derandomize=True, phases=[Phase.generate],
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(order=st.permutations(["group", "interrupt", "signal", "late-signal", "close"]),
       exit_ms=st.integers(min_value=-2, max_value=7), old=st.booleans())
def test_mixed_stop_delivery_ordering_property(clocked, monkeypatch, order, exit_ms, old):
    """Extend the wall interleaving property to mixed kill/relay delivery orderings."""
    service, _, clock, jid, aid, _ = setup_running(clocked, monkeypatch)
    service._enforce_wall(service.store.get_attempt(aid), service.store.get_job(jid))
    first = clock.t
    for index, delivery in enumerate(order):
        kind = "attempt.kill_started" if delivery == "group" else "attempt.stop_delivery"
        at = (first + timedelta(milliseconds=index)).isoformat(timespec="milliseconds")
        with service.store.transaction(kind, attempt_id=aid, job_id=jid,
                data={"via": "group" if delivery == "group" else "relay", "tag": delivery,
                      "signal_at": at, "signal_sent": True}) as tx:
            tx.execute("UPDATE attempts SET killed_by='max_wall_s' WHERE attempt_id=?", (aid,))
    receipt = {"rc": 0, "finished_at": (first + timedelta(milliseconds=exit_ms)).isoformat(
        timespec="seconds" if old else "milliseconds")}
    # Independent precision-aware oracle: ties in the first delivery's second
    # are ambiguous for old receipts; milliseconds resolve strictly earlier exits.
    natural = (first + timedelta(milliseconds=exit_ms) < first.replace(microsecond=0) if old else exit_ms < 0)
    assert service._receipt_precedes_signal(service.store.get_attempt(aid), receipt) is natural
