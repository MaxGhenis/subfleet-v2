"""C-26.4, D-RELAY-EXIT1: a refusal after provider exit preserves its end reason."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from subfleet.conversations.claude_turn import INIT_REQUEST_ID
from subfleet.conversations.reconcile import Evidence, settle
from subfleet.conversations.runner import TurnRunner
from subfleet.conversations.store import ConversationStore
from subfleet.conversations.turn import TurnSpec
from subfleet.relay import RelayClient, RelayError, RelayServer, read_log

MID = "7f1c9a0e-1111-4222-8333-444455556666"
SID = "0b0e0f00-aaaa-4bbb-8ccc-dddddddddddd"


@pytest.fixture
def refused_turn(tmp_path, monkeypatch):
    """Drive the child and relay at frame boundaries, without threads or sleeps.

    The real relay applies frames to a pipe; its client's real decoder reads the
    answers. The fake child acknowledges the message, then closes its stdin
    before settings. Its exit receipt is held until the runner's next stdout
    read, as the guardian holds it until the refusal's exchange finishes.
    """
    store = ConversationStore(tmp_path / "state")
    settings = {"model": "opus[1m]", "effort": None, "fast": False,
                "permission": "ask", "auto_continue": True}
    cid = store.create_conversation(provider="claude", workspace=str(tmp_path), workspace_kind="in-place",
                                    settings=settings, origin="new")[0]["conversation_id"]
    store.submit_message(conversation_id=cid, message_id=MID, after_message_id=None,
                         text="[fake:exit-after-ack]", attachments=[], settings=settings)
    adir = tmp_path / "a1"
    adir.mkdir()
    child = SimpleNamespace(returncode=None)
    child.poll = lambda: child.returncode
    server = RelayServer(adir / "unused.sock", adir / "stdin.jsonl")
    read_end, server._pipe = os.pipe()
    server._child = child
    open_reads = [read_end]
    refusals = []

    def make(*, exits, refusal, evidence="reply", status_unavailable=False):
        (adir / "stdout").write_text(json.dumps({"type": "control_response", "response": {
            "subtype": "success", "request_id": INIT_REQUEST_ID, "response": {"models": [
                {"value": "opus[1m]", "resolvedModel": "claude-opus-5-5[1m]"}]}}}) + "\n")
        client = RelayClient(adir / "unused.sock")

        def exchange(payload, *, before_send=None):
            if before_send is not None:
                before_send()
            frame = json.loads(payload)
            if frame.get("op") == "status" and refusals and status_unavailable:
                raise RelayError("fake relay stopped after its refusal")
            answer = server.apply(frame)
            if frame.get("tag") == "user-message":
                assert answer["ok"]
                with (adir / "stdout").open("a") as stream:
                    stream.write(json.dumps({"type": "command_lifecycle", "command_uuid": MID,
                                             "state": "started"}) + "\n")
                # The fake's after-ack hook: settings cannot precede this exit.
                child.returncode = 1 if exits else None
                os.close(open_reads.pop())
                if refusal == "closed":
                    server._closed = True
            if frame.get("tag") == "settings":
                assert not (adir / "exit.json").exists()
                assert answer["error"] == refusal
                refusals.append(answer)
                if evidence != "reply":
                    answer.pop("child_exited", None)  # an older guardian's answer
                if evidence == "receipt":
                    (adir / "exit.json").write_text(json.dumps({"rc": 1, "signal": None}))
            return answer

        monkeypatch.setattr(client, "_exchange", exchange)
        spec = TurnSpec(provider="claude", message_id=MID, text="[fake:exit-after-ack]", model_id="opus[1m]",
                        permission="ask", native_session_id=None, new_session_id=SID, cwd=str(tmp_path))
        reported = []
        runner = TurnRunner(store=store, attempt={"attempt_id": "job/a1", "lane_id": "claude-1"}, spec=spec,
                            conversation_id=cid, attempt_dir=adir, control_socket=str(adir / "unused.sock"),
                            on_outcome=reported.append, on_contain=lambda _: pytest.fail("unexpected containment"))
        runner.relay = client
        read_stdout = runner._read_stdout

        def read_after_refusal():
            if refusals:
                # In the control the live child exits only AFTER the refusal was
                # handled. A later exit must not erase that genuine relay failure.
                child.returncode = 1
                (adir / "exit.json").write_text(json.dumps({"rc": 1, "signal": None}))
            return read_stdout()

        monkeypatch.setattr(runner, "_read_stdout", read_after_refusal)
        runner._run()
        assert runner.finished.is_set() and reported == [runner]
        assert len(refusals) == 1 and runner.outbox == []
        assert runner.driver.accepted and runner.driver.outcome.reason == "ended-without-result"
        turn = json.loads((adir / "turn.json").read_text())
        outcome = settle(turn, provider="claude", turn_seq=1, gather=lambda: Evidence(
            acknowledged=True, native="found", frame="written", process_gone=True))
        return runner, outcome, read_log(adir / "stdin.jsonl")

    yield make
    for fd in open_reads:
        os.close(fd)
    server._close_pipe()
    store.close()


@pytest.mark.parametrize("refusal", ["failed", "closed"])
@pytest.mark.parametrize("evidence", ["reply", "receipt", "status"])
def test_a_settings_refusal_after_the_child_exits_keeps_its_end_reason(refused_turn, refusal, evidence):
    runner, outcome, logged = refused_turn(exits=True, refusal=refusal, evidence=evidence,
                                          status_unavailable=evidence != "status")
    assert outcome.reason == "ended-without-result"
    assert outcome.state == "failed" and outcome.block == "unfinished-turn"
    assert runner.stop_reason is None and not runner.relay_failed
    assert [(r["tag"], r["status"]) for r in logged] == [
        ("init", "written"), ("user-message", "written"),
        *([("settings", "failed")] if refusal == "failed" else [])]


@pytest.mark.parametrize("refusal", ["failed", "closed"])
@pytest.mark.parametrize("status_unavailable", [False, True])
def test_a_settings_refusal_while_the_child_lives_is_a_relay_failure(refused_turn, refusal, status_unavailable):
    runner, outcome, _ = refused_turn(exits=False, refusal=refusal, status_unavailable=status_unavailable)
    assert outcome.reason == "relay-failed"
    assert runner.stop_reason == "relay-failed" and runner.relay_failed
