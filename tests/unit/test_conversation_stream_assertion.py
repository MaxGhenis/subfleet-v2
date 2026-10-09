"""D-F5: real driver traces exercise the e2e stream assertion without process inspection."""

import json

import pytest

from subfleet.conversations.claude_turn import ClaudeTurn, INIT_REQUEST_ID, SETTINGS_REQUEST_ID
from subfleet.conversations.turn import TurnSpec
from tests.e2e.test_conversations import assert_claude_reply_events

MID = "7f1c9a0e-1111-4222-8333-444455556666"
SID = "0b0e0f00-aaaa-4bbb-8ccc-dddddddddddd"
TEXT = "Fake Claude read 11 characters."


def witness(settings_at):
    turn = ClaudeTurn(TurnSpec(provider="claude", message_id=MID, text="hello there", model_id="opus[1m]",
                              permission="ask", native_session_id=None, new_session_id=SID),
                      read_bytes=lambda image: b"")
    init = {"type": "control_response", "response": {"subtype": "success", "request_id": INIT_REQUEST_ID,
            "response": {"models": [{"value": "opus[1m]", "resolvedModel": "claude-opus-5-5[1m]"}]}}}
    settings = {"type": "control_response", "response": {"subtype": "success", "request_id": SETTINGS_REQUEST_ID,
                "response": {"applied": {"effort": "high"}}}}
    rows = [init,
            {"type": "user", "uuid": MID, "message": {"role": "user", "content": "hello there"}},
            {"type": "system", "subtype": "init", "model": "claude-opus-5-5"},
            {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": TEXT}}},
            {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
            {"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5-5",
                                              "content": [{"type": "text", "text": TEXT}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": TEXT}]
    rows.insert(settings_at, settings)
    steps = [turn.start()]
    offset = 0
    for row in rows:
        raw = json.dumps(row)
        steps.append(turn.feed(raw, offset))
        offset += len(raw.encode()) + 1
    events = [{"seq": i + 1, "message_id": MID, "kind": e.kind, "data": e.data, "source": e.source}
              for i, e in enumerate(e for step in steps for e in step.events)]
    return turn, events


@pytest.mark.parametrize("settings_at", [1, 2, 7, 8],
                         ids=["before-ack", "after-ack", "before-result", "after-result"])
def test_the_e2e_assertion_accepts_settings_at_any_point_in_the_reply(settings_at):
    """C-26.5, C-26.8: independent settings evidence may arrive after result."""
    turn, events = witness(settings_at)
    assert_claude_reply_events(events, MID, TEXT)
    assert len({event["source"] for event in events}) == len(events)
    assert turn.outcome.state == "complete" and turn.outcome.accepted and turn.outcome.answered
    assert [event["data"] for event in events if "effort" in event["data"]] == [{"effort": "high"}]
    if settings_at == 8:
        assert [event["kind"] for event in events[-2:]] == ["turn.completed", "served"]


@pytest.mark.parametrize("missing", ["accepted", "text", "turn.completed"])
def test_the_e2e_assertion_still_rejects_missing_reply_events(missing):
    _, events = witness(8)
    with pytest.raises(AssertionError):
        assert_claude_reply_events([event for event in events if event["kind"] != missing], MID, TEXT)


def test_the_e2e_assertion_still_rejects_reordered_reply_events():
    _, events = witness(8)
    ack = next(i for i, event in enumerate(events) if event["kind"] == "accepted")
    text = next(i for i, event in enumerate(events) if event["kind"] == "text")
    events[ack], events[text] = events[text], events[ack]
    # Keep cursor order valid: this specifically tests the reply's semantic order.
    for seq, event in enumerate(events, 1):
        event["seq"] = seq
    with pytest.raises(AssertionError):
        assert_claude_reply_events(events, MID, TEXT)


def test_the_e2e_assertion_still_rejects_duplicate_cursor_events():
    _, events = witness(8)
    with pytest.raises(AssertionError):
        assert_claude_reply_events([*events, events[-1]], MID, TEXT)


def test_the_e2e_assertion_still_rejects_failed_completion():
    _, events = witness(8)
    next(event for event in events if event["kind"] == "turn.completed")["data"]["state"] = "failed"
    with pytest.raises(AssertionError):
        assert_claude_reply_events(events, MID, TEXT)
