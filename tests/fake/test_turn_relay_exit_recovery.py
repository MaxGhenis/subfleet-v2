"""C-26.4, C-26.6: exit evidence must survive receipt loss and end relaying."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from subfleet.conversations import runner as runner_module
from subfleet.conversations.claude_turn import INIT_REQUEST_ID
from subfleet.conversations.runner import Clocks, TurnRunner
from subfleet.conversations.store import ConversationStore
from subfleet.conversations.turn import Frame, TurnSpec
from subfleet.relay import Ack, RelayClient, RelayError, RelayServer, frame_sha256

MID = "7f1c9a0e-1111-4222-8333-444455556666"
SID = "0b0e0f00-aaaa-4bbb-8ccc-dddddddddddd"
INIT = {"type": "control_response", "response": {"subtype": "success", "request_id": INIT_REQUEST_ID,
        "response": {"models": [{"value": "opus[1m]", "resolvedModel": "claude-opus-5-5[1m]"}]}}}
ACCEPT = {"type": "command_lifecycle", "command_uuid": MID, "state": "started"}


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "state")
    settings = {"model": "opus[1m]", "effort": None, "fast": False,
                "permission": "ask", "auto_continue": True}
    cid = store.create_conversation(provider="claude", workspace=str(tmp_path), workspace_kind="in-place",
                                    settings=settings, origin="new")[0]["conversation_id"]
    store.submit_message(conversation_id=cid, message_id=MID, after_message_id=None,
                         text="[fake:exit-after-ack]", attachments=[], settings=settings)
    adir = tmp_path / "a1"
    adir.mkdir()
    (adir / "stdout").write_text(json.dumps(INIT) + "\n")
    spec = TurnSpec(provider="claude", message_id=MID, text="[fake:exit-after-ack]", model_id="opus[1m]",
                    permission="ask", native_session_id=None, new_session_id=SID, cwd=str(tmp_path))
    now = [1000.0]
    reported, contained = [], []

    def contain(attempt_id):
        # Model the daemon's verified containment path publishing the synthetic
        # receipt when the guardian itself never publishes one (daemon.py:9354).
        contained.append(attempt_id)
        (adir / "exit.json").write_text('{"rc":-15,"signal":15}')

    runner = TurnRunner(store=store, attempt={"attempt_id": "job/a1", "lane_id": "claude-1"}, spec=spec,
                        conversation_id=cid, attempt_dir=adir, control_socket=str(adir / "unused.sock"),
                        on_outcome=reported.append, on_contain=contain,
                        clocks=Clocks(sigint_after_s=1, close_after_s=2, contain_after_s=3), clock=lambda: now[0])
    server = RelayServer(adir / "unused.sock", adir / "stdin.jsonl")
    read_end, server._pipe = os.pipe()
    open_reads = [read_end]
    child = SimpleNamespace(returncode=None)
    child.poll = lambda: child.returncode
    server._child = child
    client = RelayClient(adir / "unused.sock")
    runner.relay = client
    requests, refusals = [], []

    def exchange(payload, *, before_send=None):
        if before_send is not None:
            before_send()
        frame = json.loads(payload)
        requests.append(frame)
        answer = server.apply(frame)
        if frame.get("tag") == "user-message":
            with (adir / "stdout").open("a") as stream:
                stream.write(json.dumps(ACCEPT) + "\n")
            child.returncode = 1
            os.close(open_reads.pop())
        if answer.get("error") in ("failed", "closed"):
            refusals.append(answer)
        return answer

    monkeypatch.setattr(client, "_exchange", exchange)
    polls = []

    def advance(_):
        # A deterministic escape, not elapsed-time synchronization: at five idle
        # polls stop this runner so a broken implementation cannot hang pytest.
        now[0] += 100
        polls.append(now[0])
        if len(polls) == 5:
            runner.stop()

    monkeypatch.setattr(runner_module.time, "sleep", advance)
    yield SimpleNamespace(runner=runner, server=server, child=child, adir=adir, store=store, cid=cid,
                          now=now, reported=reported, contained=contained, requests=requests,
                          refusals=refusals, polls=polls, exchange=exchange)
    for fd in open_reads:
        os.close(fd)
    server._close_pipe()
    store.close()


@pytest.mark.parametrize("evidence", ["reply", "status", "replay"])
def test_proved_exit_without_a_receipt_reaches_eof(recovery, monkeypatch, evidence):
    """A reaped child cannot leave the runner waiting forever for exit.json.

    The guardian can die before publishing it. Neither stdout EOF nor a daemon
    ending the attempt will help the existing runner unless it keeps exit evidence.
    """
    r = recovery
    if evidence == "status":
        def older_reply(payload, **kwargs):
            answer = r.exchange(payload, **kwargs)
            answer.pop("child_exited", None)
            return answer
        monkeypatch.setattr(r.runner.relay, "_exchange", older_reply)
    elif evidence == "replay":
        for seq, tag, line in ((1, "init", "{}"), (2, "user-message", "{}"), (3, "settings", "{}")):
            r.exchange(json.dumps({"seq": seq, "op": "write", "line": line, "tag": tag,
                                   "sha256": frame_sha256("write", line)}).encode())
        r.runner._load_log()
        r.runner.replayed_message = True
    r.runner._run()
    assert not (r.adir / "exit.json").exists()
    assert r.reported == [r.runner], {"idle_polls": len(r.polls), "outcome": r.runner.driver.outcome,
                                    "stop_reason": r.runner.stop_reason, "contained": r.contained}
    assert r.runner.driver.outcome.reason == "ended-without-result"
    assert not r.runner.relay_failed and r.runner.stop_reason is None


@pytest.mark.parametrize("caller", ["count-mismatch", "unwritten-log", "retry-exhausted", "refusal"])
def test_every_relay_loss_caller_preserves_an_exit_receipt(recovery, monkeypatch, caller):
    r = recovery
    (r.adir / "exit.json").write_text('{"rc":1}')
    if caller == "count-mismatch":
        monkeypatch.setattr(r.runner.relay, "status", lambda: {"applied": 1, "child": "exited"})
        r.runner._handshake()
    elif caller == "unwritten-log":
        r.child.returncode = 1
        r.server._closed = True
        record = {"kind": "intent", "seq": 1, "op": "write", "tag": "settings",
                  "sha256": frame_sha256("write", "{}"), "line": "{}"}
        (r.adir / "stdin.jsonl").write_text(json.dumps(record) + '\n{"kind":"failed","seq":1}\n')
        monkeypatch.setattr(r.runner.relay, "status", lambda: {"applied": 1, "child": "exited"})
        r.runner._handshake()
    elif caller == "retry-exhausted":
        r.runner.resends = runner_module.RESEND_MAX
        r.runner._unacknowledged("fake unanswered frame")
    else:
        monkeypatch.setattr(r.runner.relay, "send", lambda *args, **kwargs: Ack(1, False, error="failed"))
        r.runner.outbox = [Frame("settings", "write", "{}", stops=False)]
        r.runner._transmit(r.runner.outbox[0])
    assert r.runner.outbox == [] and r.runner.stop_reason is None and not r.runner.relay_failed


def test_replay_exit_status_prevents_a_relay_stop_before_receipt(recovery, monkeypatch):
    r = recovery
    record = {"kind": "intent", "seq": 1, "op": "write", "tag": "settings",
              "sha256": frame_sha256("write", "{}"), "line": "{}"}
    (r.adir / "stdin.jsonl").write_text(json.dumps(record) + '\n{"kind":"failed","seq":1}\n')
    monkeypatch.setattr(r.runner.relay, "status", lambda: {"applied": 1, "child": "exited"})
    assert r.runner._handshake()
    assert not r.runner.relay_failed and r.runner.stop_reason is None


def test_exit_refusal_does_not_permit_a_later_frame(recovery, monkeypatch):
    """Replay of a failed initialize must not send a new user message from stdout."""
    r = recovery
    r.child.returncode = 1

    def failed_write(data):
        raise BrokenPipeError(32, "fake child exited")

    monkeypatch.setattr(r.server, "_write_line", failed_write)
    answer = r.server.apply({"seq": 1, "op": "write", "line": "{}", "tag": "init",
                             "sha256": frame_sha256("write", "{}")})
    assert answer["child_exited"] is True
    r.runner._load_log()
    r.runner._apply(r.runner.driver.start())
    assert [frame["op"] for frame in r.requests] == ["status"]
    before = list(r.requests)
    assert r.runner._read_stdout()  # buffered initialize response generates user-message
    assert r.requests == before


def test_unwritten_steer_is_requeued_when_the_provider_ends(recovery):
    r = recovery
    steer = "8f1c9a0e-1111-4222-8333-444455556666"
    r.store.submit_message(conversation_id=r.cid, message_id=steer, after_message_id=MID,
                           text="next message", attachments=[], settings=r.store.message(MID)["settings"])
    r.store.set_state(steer, "steering", reason=f"steer:{MID}", expect=("queued",))
    seq = r.store.message(steer)["seq"]
    r.runner._replay_steers = [steer]
    r.runner._relay_lost("settings refused", child_exited=True)
    row = r.store.message(steer)
    assert (row["state"], row["state_reason"], row["seq"]) == ("queued", "steer-missed: provider-ended", seq)
    assert r.runner.steer_facts()[steer] == {"frame": "unsent", "fate": "refused", "detail": "provider-ended"}


def test_client_accepts_an_older_refusal_and_requires_a_boolean_exit_flag(tmp_path, monkeypatch):
    client = RelayClient(tmp_path / "unused.sock")
    for flag in (None, False, 1, "true", True):
        reply = {"seq": 1, "ok": False, "error": "failed"}
        if flag is not None:
            reply["child_exited"] = flag
        monkeypatch.setattr(client, "_exchange", lambda *args, **kwargs: reply)
        assert client.send(1, "write", line="{}").child_exited is (flag is True)


def test_a_delayed_receipt_drains_every_stdout_chunk_and_preserves_the_result(recovery, monkeypatch):
    r = recovery

    def reply_then_receipt(payload, **kwargs):
        answer = r.exchange(payload, **kwargs)
        if json.loads(payload).get("tag") == "settings":
            with (r.adir / "stdout").open("a") as stream:
                stream.write(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                         "result": "finished " * 500, "session_id": SID}) + "\n")
            (r.adir / "exit.json").write_text('{"rc":0}')
        return answer

    monkeypatch.setattr(r.runner.relay, "_exchange", reply_then_receipt)
    monkeypatch.setattr(runner_module, "READ_CHUNK", 64)
    r.runner._run()
    assert r.reported == [r.runner] and r.runner.driver.outcome.state == "complete"
    assert r.runner.offset == (r.adir / "stdout").stat().st_size
    assert not r.runner.relay_failed and r.runner.stop_reason is None


def test_an_unproved_exit_recovers_a_stalled_receipt_by_containment(recovery, monkeypatch):
    r = recovery

    def no_exit_evidence(payload, **kwargs):
        if json.loads(payload).get("op") == "status" and r.refusals:
            raise RelayError("fake status unavailable after refusal")
        answer = r.exchange(payload, **kwargs)
        answer.pop("child_exited", None)
        return answer

    monkeypatch.setattr(r.runner.relay, "_exchange", no_exit_evidence)
    r.runner._run()
    assert r.contained == ["job/a1"] and r.reported == [r.runner]
    assert r.runner.stop_reason == "relay-failed" and r.runner.relay_failed
    assert len(r.polls) < 5 and (r.adir / "exit.json").exists()
