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
    base = dict(provider="claude", message_id=MID, text="fix the bug", model_id="opus",
                permission="ask", native_session_id=SID, effort="high", lane_email="max@example.org")
    base.update(kw)
    return TurnSpec(**base)


def line(**row):
    return json.dumps(row)


INIT_OK = line(type="control_response", response={"subtype": "success", "request_id": INIT_REQUEST_ID, "response": {
    "account": {"email": "max@example.org"}, "fast_mode_state": "off",
    "models": [{"value": "default", "resolvedModel": "claude-opus-5-5[1m]", "supportsEffort": True,
                "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
               {"value": "opus", "resolvedModel": "claude-opus-5-5", "supportsEffort": True,
                "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
               {"value": "opus[1m]", "resolvedModel": "claude-opus-5-5[1m]", "supportsEffort": True,
                "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
               {"value": "haiku", "resolvedModel": "claude-haiku-4-5-20251001"}]}})


def started(turn):
    first = turn.start()
    assert [f.tag for f in first.frames] == ["init"]
    step = turn.feed(INIT_OK, 0)
    return step


def test_argv_carries_resume_model_effort_and_the_permission_policy():
    """C-26.8, design D-7: ask mode routes permission prompts to the host; bypass does not."""
    assert argv(spec()) == ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                            "--verbose", "--include-partial-messages", "--replay-user-messages",
                            "--thinking-display", "summarized",
                            "--model", "opus", "--effort", "high", "--resume", SID,
                            "--permission-mode", "default", "--permission-prompt-tool", "stdio",
                            "--disallowedTools", "Monitor,CronCreate,ScheduleWakeup,RemoteTrigger,EnterPlanMode,ExitPlanMode",
                            "--settings", '{"disableAllHooks":false}']
    bypass = argv(spec(permission="bypass", native_session_id=None, new_session_id=SID, fast=True, effort=None))
    # C-26.11: every writable mode keeps the user's hooks on and routes interaction prompts to the person.
    assert "--session-id" in bypass and bypass[bypass.index("--permission-prompt-tool") + 1] == "stdio"
    assert bypass[-2:] == ["--settings", '{"disableAllHooks":false,"fastMode":true}']
    with pytest.raises(ValueError):
        argv(spec(permission="everything"))


def test_the_message_is_sent_only_after_initialize_and_carries_its_uuid():
    """C-24.4 the user message goes out after the provider answers initialize, with the
    message id as its uuid; `accepted` needs the provider's replay of that uuid."""
    turn = ClaudeTurn(spec(images=(Image("ab" * 32, "image/png", "/x.png"),)), read_bytes=lambda p: b"\x89PNG")
    step = started(turn)
    # C-26.8: `get_settings` goes just ahead of the message, so the served effort is the provider's.
    assert [f.tag for f in step.frames] == ["user-message", "settings"]
    assert json.loads(step.frames[1].line)["request"] == {"subtype": "get_settings"}
    frame = step.frames[0]
    body = json.loads(frame.line)
    assert body["uuid"] == MID and body["message"]["content"][0] == {"type": "text", "text": "fix the bug"}
    assert body["message"]["content"][1]["source"] == {"type": "base64", "media_type": "image/png", "data": "iVBORw=="}
    assert not turn.accepted
    ack = turn.feed(line(type="user", uuid=MID, message={"role": "user", "content": "fix the bug"}), 100)
    assert turn.accepted and ack.events[0].kind == "accepted"


def test_identity_mismatch_ends_the_turn_before_anything_is_sent():
    """C-10.6, C-26.8: the credential answered for another account; nothing is sent."""
    turn = ClaudeTurn(spec(lane_email="other@example.org"), read_bytes=lambda p: b"")
    step = started(turn)
    assert [f.tag for f in step.frames] == ["close"]
    assert step.outcome.state == "failed" and step.outcome.reason == "identity" and not step.outcome.accepted


def test_an_effort_the_model_does_not_offer_is_refused_before_sending():
    """C-26.8 a requested setting the provider does not offer never silently applies."""
    turn = ClaudeTurn(spec(effort="ultra"), read_bytes=lambda p: b"")
    step = started(turn)
    assert step.outcome.reason == "effort-unsupported"
    assert all(f.tag != "user-message" for f in step.frames)
    # An entry without effort levels accepts none (haiku); an unlisted value is refused.
    assert started(ClaudeTurn(spec(model_id="haiku", effort="low"), read_bytes=lambda p: b"")).outcome.reason == "effort-unsupported"
    assert started(ClaudeTurn(spec(model_id="gpt-9"), read_bytes=lambda p: b"")).outcome.reason == "settings-unsupported"


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
        "subtype": "can_use_tool", "tool_name": "ExitPlanMode", "input": {}, "requires_user_interaction": True}), 5)
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
    assert frames == ["init", "user-message", "settings", "close"]
    assert len({source for _, source in events}) == len(events)


def test_lifecycle_started_acknowledges_before_the_echo():
    """C-24.4 `command_lifecycle started` for our uuid is the provider's acknowledgement."""
    turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(turn)
    assert turn.feed(line(type="command_lifecycle", command_uuid="other", state="started"), 1).events == []
    step = turn.feed(line(type="command_lifecycle", command_uuid=MID, state="queued"), 2)
    assert not turn.accepted
    step = turn.feed(line(type="command_lifecycle", command_uuid=MID, state="started"), 3)
    assert turn.accepted and step.events[0].data["by"] == "lifecycle"


def test_one_m_context_values_attest_against_their_resolved_model():
    """C-26.8 `opus[1m]` serves `claude-opus-5-5[1m]` in init and `claude-opus-5-5` in
    assistant frames: neither is a mismatch; `<synthetic>` is never one."""
    turn = ClaudeTurn(spec(model_id="opus[1m]"), read_bytes=lambda p: b"")
    started(turn)
    assert turn.feed(line(type="system", subtype="init", model="claude-opus-5-5[1m]"), 1).outcome is None
    assert turn.feed(line(type="assistant", message={"id": "m", "model": "claude-opus-5-5", "content": []}), 2).outcome is None
    synthetic = {"type": "assistant", "is_api_error_message": True, "error": "rate_limit", "message": {
        "model": "<synthetic>", "role": "assistant", "type": "message", "content": [{"type": "text", "text": "limit"}],
        "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}
    step = turn.feed(json.dumps(synthetic), 3)
    assert step.outcome is None and turn.limited and step.events[0].kind == "error"


def test_ask_user_question_is_answered_by_adding_answers_only():
    """C-27.2 a clarifying question becomes a `question` approval; the answer is the
    original input plus `answers`, nothing else."""
    turn = ClaudeTurn(spec(permission="bypass"), read_bytes=lambda p: b"")
    started(turn)
    questions = [{"question": "Which branch?", "options": [{"label": "main"}, {"label": "dev"}]}]
    step = turn.feed(line(type="control_request", request_id="q1", request={
        "subtype": "can_use_tool", "tool_name": "AskUserQuestion", "input": {"questions": questions},
        "requires_user_interaction": True}), 4)
    approval = step.approvals[0]
    assert approval.kind == "question" and approval.options == ("answer", "deny", "cancel-turn")
    assert approval.request["input"] == {"questions": questions}
    with pytest.raises(ValueError):
        turn.respond("q1", "answer", answers={})
    reply = json.loads(turn.respond("q1", "answer", answers={"Which branch?": "dev"}).frames[0].line)
    assert reply["response"]["response"] == {"behavior": "allow", "updatedInput": {
        "questions": questions, "answers": {"Which branch?": "dev"}}}


def test_a_success_that_races_a_stop_is_complete():
    """C-24.4 the provider's success wins over a late stop, recorded as stop_too_late."""
    turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(turn)
    turn.interrupt()
    end = turn.feed(line(type="result", subtype="success", is_error=False, result="done"), 9)
    assert end.outcome.state == "complete" and end.events[-1].data["stop_too_late"] is True


def test_one_block_rows_key_like_their_deltas_and_phases_mark_silent_blocks():
    """Design §12, observed 2.1.280: the CLI writes each finished block as its own
    `assistant` row (content index 0) while the block is still open, so the row
    takes the stream's index and the block is shown once; block starts announce
    thinking, writing and tool input, once per change and never after `result`."""
    turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(turn)
    turn.feed(line(type="user", uuid=MID, message={"role": "user", "content": "x"}), 10)

    def stream(event, offset):
        return turn.feed(line(type="stream_event", event=event), offset)

    def row(block, offset):
        return turn.feed(line(type="assistant", message={"id": "msg_1", "model": "claude-opus-5-5",
                                                         "content": [block]}), offset)
    stream({"type": "message_start", "message": {"id": "msg_1"}}, 20)
    think = stream({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}, 21)
    assert [(e.kind, e.data) for e in think.events] == [("status", {"phase": "thinking"})]
    # Thinking whose text the API omits streams empty deltas: nothing to show.
    assert stream({"type": "content_block_delta", "index": 0,
                   "delta": {"type": "thinking_delta", "thinking": "", "estimated_tokens": 50}}, 22).events == []
    assert row({"type": "thinking", "thinking": "", "signature": "sig"}, 23).events == []
    assert stream({"type": "content_block_stop", "index": 0}, 24).events == []
    write = stream({"type": "content_block_start", "index": 1, "content_block": {"type": "text"}}, 25)
    assert [(e.kind, e.data) for e in write.events] == [("status", {"phase": "writing"})]
    assert stream({"type": "content_block_delta", "index": 1,
                   "delta": {"type": "text_delta", "text": "Found it."}}, 26).events == []   # held: no line end
    full = row({"type": "text", "text": "Found it."}, 27)
    assert [(e.kind, e.data) for e in full.events] == [("text", {"block": "msg_1:1", "text": "Found it."})]
    assert stream({"type": "content_block_stop", "index": 1}, 28).events == []          # no second copy
    tool = stream({"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use"}}, 29)
    assert [e.data for e in tool.events] == [{"phase": "preparing-tool"}]
    started_tool = row({"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}}, 30)
    assert [e.kind for e in started_tool.events] == ["tool.started"]
    stream({"type": "content_block_stop", "index": 2}, 31)
    # The next message after the tool: thinking again is a change and is announced.
    stream({"type": "message_start", "message": {"id": "msg_2"}}, 40)
    again = stream({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}, 41)
    assert [e.data for e in again.events] == [{"phase": "thinking"}]
    stream({"type": "content_block_stop", "index": 0}, 42)
    same = stream({"type": "content_block_start", "index": 1, "content_block": {"type": "thinking"}}, 43)
    assert same.events == []                                      # unchanged: not repeated
    turn.feed(line(type="result", subtype="success", is_error=False, result="done"), 50)
    stream({"type": "message_start", "message": {"id": "msg_3"}}, 60)
    late = stream({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}, 61)
    assert late.events == []                                      # no phase after the turn ended


def test_status_rows_announce_requests_and_an_automatic_compaction():
    """Design §12, observed 2.1.280: `system` `status` rows say `requesting` before
    each API request and `compacting` while an automatic compaction runs (107 s
    for a 972k-token resume); the `compact_boundary` row is announced once with
    its token counts, so the person sees why nothing streamed."""
    turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(turn)

    def system(offset, **row):
        return [(e.kind, e.data) for e in turn.feed(line(type="system", **row), offset).events]
    assert system(10, subtype="status", status="requesting") == [("status", {"phase": "requesting"})]
    assert system(11, subtype="status", status="compacting") == [("status", {"phase": "compacting"})]
    assert system(12, subtype="status", status="compacting") == []                  # unchanged
    # The compaction ended; the request it held up goes next.
    assert system(13, subtype="status", status=None, compact_result="success") == [("status", {"phase": "requesting"})]
    assert system(14, subtype="compact_boundary", compact_metadata={
        "trigger": "auto", "pre_tokens": 972214, "post_tokens": 17683, "duration_ms": 106761}) == [
        ("status", {"phase": "compacted", "trigger": "auto", "pre_tokens": 972214, "post_tokens": 17683})]
    assert system(15, subtype="status", status="requesting") == [("status", {"phase": "requesting"})]
    assert system(16, subtype="status", status="something-new") == []


def test_phases_come_from_stdout_alone_so_a_replay_after_a_stop_matches():
    """C-26.6: a replay after a restart rebuilds the driver without the person's
    stop, and must store no event the first run did not. Phases therefore never
    depend on the stop (the app keeps saying Stopping), and each takes its line's
    own `phase` source, so no other event's ordinal moves."""
    rows = [line(type="stream_event", event={"type": "message_start", "message": {"id": "msg_1"}}),
            line(type="stream_event", event={"type": "content_block_start", "index": 0,
                                             "content_block": {"type": "thinking"}}),
            line(type="system", subtype="status", status="requesting"),
            line(type="system", subtype="compact_boundary", compact_metadata={"trigger": "auto"}),
            line(type="stream_event", event={"type": "content_block_start", "index": 1,
                                             "content_block": {"type": "text"}}),
            line(type="assistant", message={"id": "msg_1", "model": "claude-opus-5-5", "content": [
                {"type": "text", "text": "done"}]})]

    def run(stop_after: int | None):
        turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
        started(turn)
        out = []
        for i, row in enumerate(rows):
            out += [(e.source, e.kind, e.data) for e in turn.feed(row, 100 + i).events]
            if stop_after == i:
                turn.interrupt()
        return out
    stopped, replayed = run(stop_after=1), run(stop_after=None)
    assert stopped == replayed
    assert [(src, data) for src, kind, data in stopped if kind == "status"][:2] == [
        ("101:phase", {"phase": "thinking"}), ("102:phase", {"phase": "requesting"})]
    assert ("105:1", "text", {"block": "msg_1:0", "text": "done"}) in stopped


def test_a_row_that_ends_the_turn_still_takes_its_stream_positions():
    """A one-block row whose model does not match ends the turn before its block
    is shown; the message's later blocks, still streamed, keep their keys, so
    no text shows twice."""
    turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(turn)
    turn.feed(line(type="system", subtype="init", model="claude-opus-5-5", session_id=SID), 5)

    def stream(event, offset):
        return turn.feed(line(type="stream_event", event=event), offset).events
    stream({"type": "message_start", "message": {"id": "m1"}}, 10)
    stream({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}, 11)
    ended = turn.feed(line(type="assistant", message={"id": "m1", "model": "claude-haiku-4-5", "content": [
        {"type": "thinking", "thinking": "hm", "signature": "s"}]}), 12)
    assert ended.outcome.reason == "model-mismatch"
    stream({"type": "content_block_stop", "index": 0}, 13)
    stream({"type": "content_block_start", "index": 1, "content_block": {"type": "text"}}, 14)
    events = stream({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hello\nwor"}}, 15)
    events += turn.feed(line(type="assistant", message={"id": "m1", "model": "claude-haiku-4-5", "content": [
        {"type": "text", "text": "Hello\nworld"}]}), 16).events
    events += stream({"type": "content_block_stop", "index": 1}, 17)
    assert [(e.kind, e.data["block"]) for e in events] == [("text.delta", "m1:1"), ("text", "m1:1")]


def test_a_session_held_at_launch_sends_nothing_and_waits_again():
    """C-26.3: another Claude process took the session after dispatch looked.
    The driver ends before initialize, with a reason that re-admits the message."""
    turn = ClaudeTurn(spec(held_by=(4242,)), read_bytes=lambda p: b"")
    first = turn.start()
    assert [f.tag for f in first.frames] == ["close"]
    assert (first.outcome.state, first.outcome.reason, first.outcome.detail) == ("failed", "external-writer", "pid 4242")
    assert not first.outcome.accepted and turn.phase == "ended"


def test_output_after_result_is_kept_without_changing_the_outcome():
    """C-26.5 background output after `result` attaches to the message; a second
    `result` changes nothing."""
    turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(turn)
    turn.feed(line(type="result", subtype="success", is_error=False, result="done"), 9)
    late = turn.feed(line(type="assistant", message={"id": "m2", "model": "claude-opus-5-5",
                                                     "content": [{"type": "text", "text": "background done"}]}), 10)
    assert [e.kind for e in late.events] == ["text"] and late.frames == [] and late.outcome is None
    again = turn.feed(line(type="result", subtype="error_during_execution", is_error=True), 11)
    assert again.outcome is None and turn.outcome.state == "complete"


def test_fast_that_the_account_will_not_serve_fails_before_sending():
    """IR-23, C-26.8 Fast asked for, `initialize` says off: nothing is sent."""
    turn = ClaudeTurn(spec(fast=True), read_bytes=lambda p: b"")
    step = started(turn)
    assert step.outcome.reason == "fast-unavailable" and not step.outcome.accepted
    assert all(f.tag != "user-message" for f in step.frames)


def test_non_error_synthetic_rows_are_ignored():
    """IR-25 a local placeholder is neither text nor an error nor a model mismatch."""
    turn = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(turn)
    step = turn.feed(line(type="assistant", isApiErrorMessage=False, message={
        "model": "<synthetic>", "content": [{"type": "text", "text": "No response requested."}]}), 5)
    assert step.events == [] and step.outcome is None


def test_plan_mode_tools_are_off_in_writable_turns():
    """IR-24 a writable turn cannot enter plan mode it has no way to leave."""
    for permission in ("ask", "accept-edits", "bypass"):
        command = argv(spec(permission=permission))
        assert "EnterPlanMode" in command[command.index("--disallowedTools") + 1]


# The catalog Claude Code 2.1.280 reported on 2026-09-24 for a Max account: no
# bare `opus` or `fable` value, only the 1M-context entries and `default`.
OBSERVED = [
    {"value": "default", "resolvedModel": "claude-opus-5-5[1m]", "supportsEffort": True,
     "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"], "supportsFastMode": True},
    {"value": "opus[1m]", "resolvedModel": "claude-opus-5-5[1m]", "supportsEffort": True,
     "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"], "supportsFastMode": True},
    {"value": "claude-fable-5-1[1m]", "resolvedModel": "claude-fable-5-1[1m]", "supportsEffort": True,
     "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"], "supportsFastMode": False},
    {"value": "sonnet", "resolvedModel": "claude-sonnet-5", "supportsEffort": True,
     "supportedEffortLevels": ["low", "medium", "high"]},
    {"value": "haiku", "resolvedModel": "claude-haiku-4-5-20251001"},
]


def observed_init(fast_state="on", email="max@example.org"):
    return line(type="control_response", response={"subtype": "success", "request_id": INIT_REQUEST_ID, "response": {
        "account": {"email": email, "organization": "Example", "subscriptionType": "max"},
        "fast_mode_state": fast_state, "models": OBSERVED}})


def test_a_model_id_resolves_through_the_routed_model_when_the_catalog_has_no_such_value():
    """D-19, C-26.8: `--model claude-opus-5-5` is valid, and the catalog lists that model only
    as `opus[1m]`/`default`; the entry resolving to the routed model decides effort and Fast."""
    turn = ClaudeTurn(spec(model_id="claude-opus-5-5", model_ref="claude-opus-5-5", effort="xhigh"),
                      read_bytes=lambda p: b"")
    turn.start()
    step = turn.feed(observed_init(), 0)
    assert step.outcome is None and [f.tag for f in step.frames] == ["user-message", "settings"]
    assert turn.expected_model == "claude-opus-5-5"
    # Without the routed model, the same value is not in the catalog.
    bare = ClaudeTurn(spec(model_id="claude-opus-5-5", effort=None), read_bytes=lambda p: b"")
    bare.start()
    assert bare.feed(observed_init(), 0).outcome.reason == "settings-unsupported"


def test_fast_on_a_model_without_fast_mode_is_refused_before_sending():
    """IR-23: the account has Fast on, and this model does not offer it."""
    turn = ClaudeTurn(spec(model_id="claude-fable-5-1[1m]", effort=None, fast=True), read_bytes=lambda p: b"")
    turn.start()
    step = turn.feed(observed_init(), 0)
    assert step.outcome.reason == "fast-unavailable" and "no Fast" in step.outcome.detail
    assert all(f.tag != "user-message" for f in step.frames)


def test_the_catalog_is_kept_for_models_json():
    """D-19: what each value serves and offers, recorded from `initialize`."""
    turn = ClaudeTurn(spec(model_id="opus[1m]", effort=None), read_bytes=lambda p: b"")
    turn.start()
    turn.feed(observed_init(), 0)
    by_value = {entry["value"]: entry for entry in turn.catalog}
    assert by_value["opus[1m]"] == {"value": "opus[1m]", "model": "claude-opus-5-5", "context_1m": True,
                                    "display": None,
                                    # C-26.8: ultracode is offered wherever xhigh is.
                                    "efforts": ["low", "medium", "high", "xhigh", "max", "ultracode"],
                                    "fast": True}
    assert by_value["haiku"]["efforts"] == [] and by_value["claude-fable-5-1[1m]"]["fast"] is False


def test_the_lane_claims_its_email_label_not_its_uuid_identity():
    """C-1.4, C-10.6: `initialize` reports the account's email; a lane's identity is uuids."""
    from types import SimpleNamespace
    from subfleet.conversations.launch import lane_email
    assert lane_email(SimpleNamespace(label="max@example.org", identity="acct-1:org-1")) == "max@example.org"
    assert lane_email(SimpleNamespace(label="Work account", identity="acct-1:org-1")) is None
    assert lane_email(SimpleNamespace(label=None, identity=None)) is None


def test_each_outcome_says_what_ended_the_turn():
    """C-24.6, C-24.8: the provider's `result`, the driver's own check, or the end of
    stdout; after the driver's own stop, a later `result` is noted (the provider finished)."""
    done = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(done)
    assert done.feed(line(type="result", subtype="success", is_error=False), 9).outcome.ended_by == "provider"
    refused = ClaudeTurn(spec(lane_email="other@example.org"), read_bytes=lambda p: b"")
    assert started(refused).outcome.ended_by == "driver"
    cut = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(cut)
    assert cut.eof(3).outcome.ended_by == "eof"
    mismatch = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    started(mismatch)
    assert mismatch.feed(line(type="system", subtype="init", model="claude-haiku-4-5"), 5).outcome.ended_by == "driver"
    assert not mismatch.terminal_after_end
    mismatch.feed(line(type="result", subtype="error_during_execution", is_error=True), 9)
    assert mismatch.terminal_after_end and mismatch.outcome.reason == "model-mismatch"
