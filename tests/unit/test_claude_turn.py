"""C-24.4, C-26.5, C-26.6, C-26.8, C-27.1 to C-27.4: the Claude turn driver."""

from __future__ import annotations

import json

import pytest

from subfleet.conversations.claude_turn import (
    INIT_REQUEST_ID, INTERRUPT_REQUEST_ID, ClaudeTurn, argv,
)
from subfleet.conversations.turn import Image, TurnSpec

MID = "7f1c9a0e-1111-4222-8333-444455556666"
SID = "0b0e0f00-aaaa-4bbb-8ccc-dddddddddddd"


def spec(**kw):
    base = dict(provider="claude", message_id=MID, text="fix the bug", model_id="claude-opus-5-5",
                permission="ask", native_session_id=SID, effort="high", lane_identity="max@example.org")
    base.update(kw)
    return TurnSpec(**base)


def line(**row):
    return json.dumps(row)


INIT_OK = line(type="control_response", response={"subtype": "success", "request_id": INIT_REQUEST_ID, "response": {
    "account": {"email": "max@example.org"}, "fast_mode_state": "off",
    "models": [{"value": "default", "resolvedModel": "claude-opus-5-5[1m]", "supportsEffort": True,
                "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]}]}})


def started(turn):
    first = turn.start()
    assert [f.tag for f in first.frames] == ["init"]
    step = turn.feed(INIT_OK, 0)
    return step


def test_argv_carries_resume_model_effort_and_the_permission_policy():
    """C-26.8, design D-7: ask mode routes permission prompts to the host; bypass does not."""
    assert argv(spec()) == ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                            "--verbose", "--include-partial-messages", "--replay-user-messages",
                            "--model", "claude-opus-5-5", "--effort", "high", "--resume", SID,
                            "--permission-mode", "default", "--permission-prompt-tool", "stdio"]
    bypass = argv(spec(permission="bypass", native_session_id=None, new_session_id=SID, fast=True, effort=None))
    assert "--session-id" in bypass and "--permission-prompt-tool" not in bypass
    assert bypass[-2:] == ["--settings", '{"fastMode":true}']
    with pytest.raises(ValueError):
        argv(spec(permission="everything"))


def test_the_message_is_sent_only_after_initialize_and_carries_its_uuid():
    """C-24.4 the user message goes out after the provider answers initialize, with the
    message id as its uuid; `accepted` needs the provider's replay of that uuid."""
    turn = ClaudeTurn(spec(images=(Image("ab" * 32, "image/png", "/x.png"),)), read_bytes=lambda p: b"\x89PNG")
    step = started(turn)
    frame = step.frames[0]
    assert frame.tag == "user-message"
    body = json.loads(frame.line)
    assert body["uuid"] == MID and body["message"]["content"][0] == {"type": "text", "text": "fix the bug"}
    assert body["message"]["content"][1]["source"] == {"type": "base64", "media_type": "image/png", "data": "iVBORw=="}
    assert not turn.accepted
    ack = turn.feed(line(type="user", uuid=MID, message={"role": "user", "content": "fix the bug"}), 100)
    assert turn.accepted and ack.events[0].kind == "accepted"


def test_identity_mismatch_ends_the_turn_before_anything_is_sent():
    """C-10.6, C-26.8: the credential answered for another account; nothing is sent."""
    turn = ClaudeTurn(spec(lane_identity="other@example.org"), read_bytes=lambda p: b"")
    step = started(turn)
    assert [f.tag for f in step.frames] == ["close"]
    assert step.outcome.state == "failed" and step.outcome.reason == "identity" and not step.outcome.accepted


def test_an_effort_the_model_does_not_offer_is_refused_before_sending():
    """C-26.8 a requested setting the provider does not offer never silently applies."""
    turn = ClaudeTurn(spec(effort="ultra"), read_bytes=lambda p: b"")
    step = started(turn)
    assert step.outcome.reason == "effort-unsupported"
    assert all(f.tag != "user-message" for f in step.frames)


def test_streamed_text_thinking_tools_and_completion():
    """C-25.5, C-26.5: deltas stream, full blocks replace them, tools are summarised,
    `result` ends the turn and closes stdin."""
    turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(turn)
    turn.feed(line(type="user", uuid=MID, message={"role": "user", "content": "x"}), 10)
    turn.feed(line(type="stream_event", event={"type": "message_start", "message": {"id": "msg_1"}}), 20)
    d = turn.feed(line(type="stream_event", event={"type": "content_block_delta", "index": 0,
                                                   "delta": {"type": "text_delta", "text": "Looking\n"}}), 30)
    assert d.events[0].kind == "text.delta" and d.events[0].data == {"block": "msg_1:0", "text": "Looking\n"}
    t = turn.feed(line(type="stream_event", event={"type": "content_block_delta", "index": 1,
                                                   "delta": {"type": "thinking_delta", "thinking": "plan it"}}), 40)
    assert t.events == []                     # held until a line ends or the block stops
    stop = turn.feed(line(type="stream_event", event={"type": "content_block_stop", "index": 1}), 45)
    assert stop.events[0].kind == "thinking.delta" and stop.events[0].data["text"] == "plan it"
    full = turn.feed(line(type="assistant", message={"id": "msg_1", "model": "claude-opus-5-5", "content": [
        {"type": "text", "text": "Looking"}, {"type": "thinking", "thinking": "plan it", "signature": "sig"},
        {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "pytest -q"}}]}), 50)
    kinds = [e.kind for e in full.events]
    assert kinds == ["text", "thinking", "tool.started"]
    assert "sig" not in json.dumps([e.data for e in full.events])
    done = turn.feed(line(type="user", message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu1", "content": "3 passed", "is_error": False}]}), 60)
    assert done.events[0].kind == "tool.completed" and done.events[0].data["preview"] == "3 passed"
    end = turn.feed(line(type="result", subtype="success", is_error=False, result="Fixed.", num_turns=2), 70)
    assert end.outcome.state == "complete" and end.outcome.accepted and end.outcome.answered
    assert [f.tag for f in end.frames] == ["close"] and end.events[-1].kind == "turn.completed"


def test_can_use_tool_becomes_an_approval_and_the_reply_is_scoped():
    """C-27.1, C-27.2: a permission request waits for a person; allow returns the original
    input and nothing more; the frame is tagged with the request id."""
    turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(turn)
    request = line(type="control_request", request_id="req-9", request={
        "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "rm -rf build"},
        "tool_use_id": "tu9", "permission_suggestions": [{"type": "addRules"}]})
    step = turn.feed(request, 80)
    assert step.approvals[0].provider_request_id == "req-9"
    assert step.approvals[0].options == ("allow", "deny", "cancel-turn")
    assert step.events[0].kind == "approval.requested" and "rm -rf build" in step.events[0].data["input"]
    again = turn.feed(request, 80)
    assert again.approvals == []              # a re-announced request is not a second approval
    reply = turn.respond("req-9", "allow")
    frame = json.loads(reply.frames[0].line)
    assert reply.frames[0].tag == "approval:req-9"
    assert frame["response"] == {"subtype": "success", "request_id": "req-9", "response": {
        "behavior": "allow", "updatedInput": {"command": "rm -rf build"}, "toolUseID": "tu9"}}
    assert "updatedPermissions" not in json.dumps(frame)
    assert turn.respond("req-9", "allow").frames == []   # answered once


def test_requires_user_interaction_offers_no_one_tap_allow():
    """C-27.2 the provider's own rule: no one-tap allow where it requires interaction."""
    turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(turn)
    step = turn.feed(line(type="control_request", request_id="r", request={
        "subtype": "can_use_tool", "tool_name": "AskUserQuestion", "input": {}, "requires_user_interaction": True}), 5)
    assert step.approvals[0].options == ("deny", "cancel-turn")


def test_unsupported_control_requests_are_refused_visibly():
    """C-27.4 an authentication refresh or any other host request gets an error response."""
    turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(turn)
    step = turn.feed(line(type="control_request", request_id="auth-1", request={"subtype": "oauth_token_refresh"}), 5)
    body = json.loads(step.frames[0].line)
    assert body["response"]["subtype"] == "error" and step.events[0].kind == "error"
    assert "token" not in json.dumps(body).replace("oauth_token_refresh", "")


def test_model_mismatch_interrupts_and_fails():
    """C-26.8 a served model other than the requested one stops the turn."""
    turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(turn)
    step = turn.feed(line(type="system", subtype="init", model="claude-haiku-4-5", session_id=SID), 5)
    tags = [f.tag for f in step.frames]
    assert tags == ["interrupt", "close"] and step.outcome.reason == "model-mismatch"


def test_interrupt_before_send_never_sends_the_message():
    """C-24.7 stopping before the message was written ends the turn with nothing delivered."""
    turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    turn.start()
    step = turn.interrupt()
    assert step.outcome.state == "interrupted" and step.outcome.reason == "stopped-before-send"
    assert [f.tag for f in step.frames] == ["close"]
    assert turn.feed(INIT_OK, 0).frames == []


def test_interrupt_during_the_turn_uses_the_control_protocol():
    """C-24.7 a running turn gets the provider's interrupt, then its result is `interrupted`."""
    turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(turn)
    step = turn.interrupt()
    assert json.loads(step.frames[0].line)["request"] == {"subtype": "interrupt"}
    assert json.loads(step.frames[0].line)["request_id"] == INTERRUPT_REQUEST_ID
    end = turn.feed(line(type="result", subtype="error_during_execution", is_error=True), 9)
    assert end.outcome.state == "interrupted"


def test_eof_without_result_is_left_to_reconciliation():
    """C-24.6 a process that ends without `result` gives no completion claim."""
    turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(turn)
    turn.feed(line(type="user", uuid=MID, message={}), 1)
    step = turn.eof(2)
    assert step.outcome.state == "failed" and step.outcome.reason == "ended-without-result"
    assert step.outcome.accepted


def test_rate_limit_rejection_marks_the_turn_limited():
    """C-26.7 a rejected rate-limit event makes the failure a limit."""
    turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(turn)
    turn.feed(line(type="rate_limit_event", rate_limit_info={"status": "rejected", "rateLimitType": "five_hour",
                                                             "resetsAt": 1789000000}), 3)
    end = turn.feed(line(type="result", subtype="success", is_error=True, result="You've hit your limit"), 4)
    assert end.outcome.state == "failed" and end.outcome.reason == "limited" and end.outcome.limited


def test_replay_produces_the_same_events_and_frames():
    """C-26.6 a driver rebuilt from the same stdout emits identical event sources and frame tags."""
    rows = [INIT_OK, line(type="user", uuid=MID, message={}),
            line(type="assistant", message={"id": "m", "model": "claude-opus-5-5",
                                            "content": [{"type": "text", "text": "done"}]}),
            line(type="result", subtype="success", is_error=False, result="done")]

    def run():
        turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
        out = [turn.start()]
        offset = 0
        for row in rows:
            out.append(turn.feed(row, offset))
            offset += len(row) + 1
        return [(e.kind, e.source) for s in out for e in s.events], [f.tag for s in out for f in s.frames]

    assert run() == run()
    events, frames = run()
    assert frames == ["init", "user-message", "close"]
    assert len({source for _, source in events}) == len(events)
