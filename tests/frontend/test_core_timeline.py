"""Folding a conversation's events, receipts, approvals and history into its timeline
(design §5, §12; C-25.4, C-27.1, C-29.8).

Events are the real drivers' (`claude_turn`, `codex_turn`) fed scripted
provider output, stored by the real store and read back with the real
`conversation.events` handler, page by page, as the app polls them.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import uuid

import pytest

from subfleet.guard.preflight import HOOK_KEY
from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import (
    ServiceHarness, claude_assistant, claude_init, claude_result, claude_stream,
)

pytestmark = needs_swift


@pytest.fixture
def harness():
    harness = ServiceHarness(Path(tempfile.mkdtemp(prefix="sf-tl-", dir="/tmp")))
    yield harness
    harness.close()


def fold(core_probe, tmp_path, cid: str, steps: list[dict]) -> dict:
    return run_probe(core_probe, "fold", write_json(tmp_path / f"fold-{uuid.uuid4().hex}.json",
                                                    {"conversation_id": cid, "steps": steps}))


def page(harness, cid: str, after: int = 0, limit: int | None = None) -> dict:
    return harness.call("conversation.events", conversation_id=cid, after=after, **({"limit": limit} if limit else {}))


def replay(mid: str, text: str = "hi") -> dict:
    return {"type": "user", "uuid": mid, "isReplay": True, "message": {"role": "user", "content": text}}


def items_of(result: dict, message_id: str, kind: str | None = None) -> list[dict]:
    return [i for i in result["items"] if i["message_id"] == message_id and (kind is None or i["type"] == kind)]


def test_design_5_a_delta_streams_and_the_full_text_replaces_it(core_probe, tmp_path, harness):
    cid = harness.create()["conversation_id"]
    mid = harness.submit(cid, "hello")["message_id"]
    turn = harness.attempt(cid, mid)
    turn.feed(claude_init(), replay(mid),
              *claude_stream("msg_t", ["I am ", "thinking\nabout it"], kind="thinking", index=0),
              *claude_stream("msg_a", ["Hel", "lo\nwor", "ld"]))
    streaming = page(harness, cid)
    turn.feed(claude_assistant("msg_t", [{"type": "thinking", "thinking": "I am thinking\nabout it"}]),
              claude_assistant("msg_a", [{"type": "text", "text": "Hello\nworld"}]), claude_result())
    rest = page(harness, cid, after=streaming["next"])
    result = fold(core_probe, tmp_path, cid, [
        {"page": streaming, "snapshot": True}, {"page": rest, "snapshot": True},
    ])
    during, done = result["snapshots"]
    texts = items_of(during, mid, "text")
    assert [(t["text"], t["final"]) for t in texts] == [("Hello\nworld", False)]
    assert items_of(during, mid, "thinking")[0]["final"] is False
    assert during["turns"][mid]["streaming"] is True
    final = items_of(done, mid, "text")
    assert [(t["id"], t["text"], t["final"]) for t in final] == [(texts[0]["id"], "Hello\nworld", True)]
    assert [(t["text"], t["final"]) for t in items_of(done, mid, "thinking")] == [("I am thinking\nabout it", True)]
    turn_state = done["turns"][mid]
    assert turn_state["phases"] == ["starting-provider", "sent", "accepted"]
    assert turn_state["accepted"] is True and turn_state["outcome"]["state"] == "complete"
    assert turn_state["served"]["account"] == "fixture@example.invalid"
    assert turn_state["streaming"] is False and done["cursor"] == rest["next"]
    # A late delta for a finished block changes nothing (hand-made: the drivers never send one).
    late = {"events": [{"seq": rest["next"] + 1, "message_id": mid, "kind": "text.delta", "ts": None,
                        "data": {"block": "msg_a:0", "text": " and more"}}], "next": rest["next"] + 1, "reset": False}
    again = fold(core_probe, tmp_path, cid, [{"page": streaming}, {"page": rest}, {"page": late}])
    assert [t["text"] for t in items_of(again, mid, "text")] == ["Hello\nworld"]


def test_design_5_tool_calls_are_activity_rows(core_probe, tmp_path, harness):
    cid = harness.create()["conversation_id"]
    mid = harness.submit(cid, "look")["message_id"]
    turn = harness.attempt(cid, mid)
    turn.feed(claude_init(), replay(mid),
              claude_assistant("msg_1", [{"type": "tool_use", "id": "toolu_a", "name": "Read",
                                          "input": {"file_path": "/work/README.md"}}]))
    running = page(harness, cid)
    turn.feed({"type": "user", "parent_tool_use_id": None, "message": {"role": "user", "content": [
                  {"type": "tool_result", "tool_use_id": "toolu_a", "content": "# Title", "is_error": False}]}},
              claude_assistant("msg_2", [{"type": "tool_use", "id": "toolu_b", "name": "Bash",
                                          "input": {"command": "false"}}]),
              {"type": "user", "parent_tool_use_id": None, "message": {"role": "user", "content": [
                  {"type": "tool_result", "tool_use_id": "toolu_b", "content": "exit 1", "is_error": True}]}},
              claude_assistant("msg_3", [{"type": "text", "text": "Read it."}]), claude_result())
    result = fold(core_probe, tmp_path, cid, [{"page": running, "snapshot": True},
                                              {"page": page(harness, cid, after=running["next"])}])
    first = items_of(result["snapshots"][0], mid, "tool")
    assert [(t["name"], t["state"], t["summary"]) for t in first] == [("Read", "running", "file_path: /work/README.md")]
    tools = items_of(result, mid, "tool")
    assert [(t["name"], t["state"], t["preview"]) for t in tools] == [("Read", "succeeded", "# Title"),
                                                                        ("Bash", "failed", "exit 1")]
    kinds = [i["type"] for i in items_of(result, mid)]
    assert kinds == ["person", "tool", "tool", "text"]


def claude_approval(turn, mid: str, request_id: str, tool: str = "Bash", tool_input: dict | None = None) -> None:
    tool_input = tool_input or {"command": "echo approved-by-person", "description": "Say hello"}
    turn.feed(claude_assistant(f"msg_{request_id}", [{"type": "tool_use", "id": f"toolu_{request_id}", "name": tool,
                                                      "input": tool_input}]),
              {"type": "control_request", "request_id": request_id, "request": {
                  "subtype": "can_use_tool", "tool_name": tool, "tool_use_id": f"toolu_{request_id}",
                  "input": tool_input, "decision_reason": "fake: asks every time"}})


def test_c27_1_an_approval_card_appears_joins_its_id_and_resolves(core_probe, tmp_path, harness):
    cid = harness.create()["conversation_id"]
    mid = harness.submit(cid, "run it")["message_id"]
    turn = harness.attempt(cid, mid)
    turn.feed(claude_init(), replay(mid))
    claude_approval(turn, mid, "perm-1")
    asked = page(harness, cid)
    approvals = harness.call("approval.list", conversation_id=cid)["approvals"]
    receipts = harness.call("message.status", message_ids=[mid])["messages"]
    turn.respond("perm-1", "allow")
    turn.feed({"type": "user", "parent_tool_use_id": None, "message": {"role": "user", "content": [
                  {"type": "tool_result", "tool_use_id": "toolu_perm-1", "content": "approved-by-person"}]}},
              claude_assistant("msg_z", [{"type": "text", "text": "The command ran."}]), claude_result())
    result = fold(core_probe, tmp_path, cid, [
        {"page": asked, "snapshot": True}, {"receipts": receipts}, {"approvals": approvals, "snapshot": True},
        {"page": page(harness, cid, after=asked["next"])},
    ])
    before_join, joined = result["snapshots"]
    card = items_of(before_join, mid, "approval")[0]["card"]
    assert card["state"] == "pending" and card["request_id"] == "perm-1" and card["approval_id"] is None
    assert card["options"] == ["allow", "deny", "cancel-turn"] and card["kind"] == "tool"
    assert ["tool", "Bash"] in card["shown"] and ["input", "echo approved-by-person\ndescription: Say hello"] in card["shown"]
    assert ["reason", "fake: asks every time"] in card["shown"]
    assert before_join["turns"][mid]["state"] == "approval-needed" and before_join["pending_cards"]
    joined_card = items_of(joined, mid, "approval")
    assert len(joined_card) == 1 and joined_card[0]["card"]["approval_id"] == approvals[0]["approval_id"]
    done = items_of(result, mid, "approval")[0]["card"]
    assert done["state"] == "answered:allow" and result["pending_cards"] == []
    assert [t["state"] for t in items_of(result, mid, "tool")] == ["succeeded"]


def test_c27_1_an_approval_known_before_its_event_is_one_card(core_probe, tmp_path, harness):
    """conversation.open's pending approvals can arrive before the events do."""
    cid = harness.create()["conversation_id"]
    mid = harness.submit(cid, "ask")["message_id"]
    turn = harness.attempt(cid, mid)
    turn.feed(claude_init(), replay(mid))
    claude_approval(turn, mid, "perm-q", tool="AskUserQuestion", tool_input={"questions": [
        {"question": "Which color?", "header": "Color", "multiSelect": False,
         "options": [{"label": "Blue", "description": "calm"}, {"label": "Red", "description": "bold"}]}]})
    opened = harness.call("conversation.open", conversation_id=cid)
    result = fold(core_probe, tmp_path, cid, [
        {"receipts": opened["messages"]}, {"approvals": opened["pending_approvals"], "snapshot": True},
        {"page": page(harness, cid)},
    ])
    early = items_of(result["snapshots"][0], mid, "approval")
    assert len(early) == 1 and early[0]["card"]["approval_id"] and early[0]["card"]["request_id"] is None
    cards = items_of(result, mid, "approval")
    assert len(cards) == 1
    card = cards[0]["card"]
    assert card["request_id"] == "perm-q" and card["kind"] == "question"
    assert card["options"] == ["answer", "deny", "cancel-turn"] and card["questions"] == ["Which color?"]


def test_c27_3_a_turn_that_ends_withdraws_its_pending_card(core_probe, tmp_path, harness):
    """The driver withdraws pending requests at the end without an approval.resolved event."""
    cid = harness.create()["conversation_id"]
    mid = harness.submit(cid, "ask")["message_id"]
    turn = harness.attempt(cid, mid)
    turn.feed(claude_init(), replay(mid))
    claude_approval(turn, mid, "perm-2")
    turn.feed(claude_result(ok=False, subtype="error_during_execution"))
    events = page(harness, cid)
    assert "approval.resolved" not in [e["kind"] for e in events["events"]]
    result = fold(core_probe, tmp_path, cid, [{"page": events}])
    assert items_of(result, mid, "approval")[0]["card"]["state"] == "withdrawn"
    assert result["turns"][mid]["outcome"]["state"] == "failed"


def test_c25_4_a_reset_reads_the_log_again_from_zero(core_probe, tmp_path, harness):
    """Compacted deltas behind the cursor: `reset: true`, then the page from 0 has the final text."""
    cid = harness.create()["conversation_id"]
    mid = harness.submit(cid, "hello")["message_id"]
    turn = harness.attempt(cid, mid)
    turn.feed(claude_init(), replay(mid), *claude_stream("msg_a", ["one\n", "two\n", "three"]))
    partial = page(harness, cid, limit=5)                   # the app has read part of the stream
    turn.feed(claude_assistant("msg_a", [{"type": "text", "text": "one\ntwo\nthree"}]), claude_result())
    assert turn.compact() > 0                               # the daemon drops the deltas (design §3)
    stale = page(harness, cid, after=partial["next"])
    assert stale["reset"] is True
    fresh = page(harness, cid, after=0)
    assert "text.delta" not in [e["kind"] for e in fresh["events"]]
    result = fold(core_probe, tmp_path, cid, [
        {"page": partial, "snapshot": True}, {"page": stale, "snapshot": True}, {"page": fresh},
    ])
    assert result["results"] == ["applied:5", "reset", f"applied:{len(fresh['events'])}"]
    before, after_reset = result["snapshots"]
    assert items_of(before, mid, "text")[0]["final"] is False
    assert after_reset["cursor"] == 0 and items_of(after_reset, mid, "text") == []
    assert result["resets"] == 1
    assert [(t["text"], t["final"]) for t in items_of(result, mid, "text")] == [("one\ntwo\nthree", True)]
    assert result["turns"][mid]["outcome"]["state"] == "complete"


def test_c29_9_a_superseded_poll_changes_nothing(core_probe, tmp_path, harness):
    cid = harness.create()["conversation_id"]
    mid = harness.submit(cid, "hello")["message_id"]
    harness.attempt(cid, mid).feed(claude_init(), replay(mid))
    events = page(harness, cid)
    superseded = {"events": [], "next": 999, "reset": False, "superseded": True}
    result = fold(core_probe, tmp_path, cid, [{"page": events}, {"page": superseded}])
    assert result["results"][-1] == "superseded" and result["cursor"] == events["next"]


def test_design_5_codex_reasoning_parts_complete_as_one_block(core_probe, tmp_path, harness):
    cid = harness.create()["conversation_id"]
    mid = harness.submit(cid, "plan")["message_id"]
    turn = harness.attempt(cid, mid, provider="codex")
    cwd = str(harness.workspace)
    thread = {"threadId": "th-1"}
    turn.feed({"id": 1, "result": {"userAgent": "codex"}},
              {"id": 2, "result": {"data": [{"cwd": cwd, "hooks": [{"key": HOOK_KEY, "enabled": True,
                                                                  "trustStatus": "trusted"}], "errors": []}]}},
              {"id": 3, "result": {"data": [{"id": "gpt-6-astra", "model": "gpt-6-astra",
                                             "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                                             "serviceTiers": [{"id": "priority"}], "inputModalities": ["text"]}]}},
              {"id": 4, "result": {"thread": {"id": "th-1", "status": {"type": "idle"}}, "model": "gpt-6-astra",
                                   "reasoningEffort": "high", "serviceTier": None, "approvalPolicy": "never"}},
              {"id": 5, "result": {"turn": {"id": "turn-1"}}},
              {"method": "item/reasoning/summaryTextDelta", "params": {**thread, "itemId": "r1", "summaryIndex": 0,
                                                                      "delta": "Plan it\n"}},
              {"method": "item/reasoning/summaryTextDelta", "params": {**thread, "itemId": "r1", "summaryIndex": 1,
                                                                      "delta": "Check it\n"}})
    streaming = page(harness, cid)
    turn.feed({"method": "item/completed", "params": {**thread, "item": {"type": "reasoning", "id": "r1",
                                                                         "summary": ["Plan it", "Check it"]}}},
              {"method": "item/agentMessage/delta", "params": {**thread, "itemId": "a1", "delta": "Done.\n"}},
              {"method": "item/completed", "params": {**thread, "item": {"type": "agentMessage", "id": "a1",
                                                                         "text": "Done."}}},
              {"method": "turn/completed", "params": {**thread, "turn": {"id": "turn-1", "status": "completed"}}})
    result = fold(core_probe, tmp_path, cid, [{"page": streaming, "snapshot": True},
                                              {"page": page(harness, cid, after=streaming["next"])}])
    parts = items_of(result["snapshots"][0], mid, "thinking")
    assert [(p["text"], p["final"]) for p in parts] == [("Plan it\n", False), ("Check it\n", False)]
    thinking = items_of(result, mid, "thinking")
    assert [(t["id"], t["text"], t["final"]) for t in thinking] == [(f"thinking:{mid}:r1", "Plan it\nCheck it", True)]
    assert [(t["text"], t["final"]) for t in items_of(result, mid, "text")] == [("Done.", True)]
    served = result["turns"][mid]["served"]
    assert served["model"] == "gpt-6-astra" and served["effort"] == "high" and served["native_session_id"] == "th-1"
    assert result["turns"][mid]["phases"] == ["starting-provider", "opening-thread", "sent", "accepted"]


def test_c29_8_history_shows_only_what_came_before_subfleet(core_probe, tmp_path, harness, monkeypatch):
    """The native transcript through the real `conversation.history`: rows of the
    conversation's Subfleet turns stay out, and their user rows give the person's text."""
    claude = tmp_path / "claude"
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(claude))
    session = str(uuid.uuid4())
    cid = harness.create()["conversation_id"]
    harness.store.update_conversation(cid, native_session_id=session)
    mid = harness.submit(cid, "and now this")["message_id"]
    turn = harness.attempt(cid, mid)
    turn.feed(claude_init(), replay(mid, "and now this"),
              claude_assistant("msg_n", [{"type": "text", "text": "Doing it."}]), claude_result())
    events = page(harness, cid)
    first_ts = events["events"][0]["ts"]
    rows = [
        {"type": "user", "uuid": str(uuid.uuid4()), "timestamp": "2026-09-01T10:00:00.000Z", "cwd": "/w",
         "message": {"role": "user", "content": "native question"}},
        {"type": "assistant", "uuid": str(uuid.uuid4()), "timestamp": "2026-09-01T10:00:05.000Z", "cwd": "/w",
         "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [
             {"type": "text", "text": "native answer"},
             {"type": "tool_use", "id": "toolu_x", "name": "Read", "input": {"file_path": "/w/a"}}]}},
        {"type": "user", "uuid": mid, "timestamp": first_ts, "cwd": "/w",
         "message": {"role": "user", "content": "and now this"}},
        {"type": "assistant", "uuid": str(uuid.uuid4()), "timestamp": first_ts, "cwd": "/w",
         "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "Doing it."}]}},
    ]
    project = claude / "projects" / "-w"
    project.mkdir(parents=True)
    (project / f"{session}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    history = harness.call("conversation.history", conversation_id=cid)
    assert [i["text"] for i in history["items"]][0] == "Doing it."        # newest first, all rows
    result = fold(core_probe, tmp_path, cid, [
        {"receipts": harness.call("message.status", message_ids=[mid])["messages"]},
        {"page": events}, {"history": history},
    ])
    shown = [(i["type"], i.get("role"), i["text"]) for i in result["items"]]
    assert shown == [("history", "user", "native question"), ("history", "assistant", "native answer"),
                     ("history", "assistant", "file_path: /w/a"), ("person", None, "and now this"),
                     ("text", None, "Doing it.")]
    assert result["items"][2]["tool"] == "Read" and result["history_complete"] is True


def test_design_12_messages_order_by_sequence_with_labels(core_probe, tmp_path):
    """Receipts order the timeline; a failover continuation and an unblock note read as
    notices; a tombstone is not shown; a local message shows before its receipt."""
    cid = "cv-1"
    ids = [str(uuid.uuid4()) for _ in range(5)]
    receipt = lambda i, seq, origin, state="complete", **extra: {  # noqa: E731
        "message_id": ids[i], "conversation_id": cid, "seq": seq, "origin": origin, "state": state, **extra}
    result = fold(core_probe, tmp_path, cid, [
        {"local": {"message_id": ids[4], "text": "typed just now"}},
        {"receipts": [receipt(3, 4, "person", "queued"), receipt(0, 1, "person", "failed", state_reason="limited"),
                      receipt(1, 2, "failover", served={"lane_id": "claude-2", "account": "b@example.invalid"}),
                      receipt(2, 3, "tombstone", "cancelled", state_reason="withdrawn-before-receipt")]},
    ])
    assert result["order"] == [ids[0], ids[1], ids[2], ids[3], ids[4]]
    shown = [(i["type"], i.get("text"), i.get("state")) for i in result["items"]]
    assert shown == [("person", None, "failed"), ("notice", "Continued after a usage limit on b@example.invalid", None),
                     ("person", None, "queued"), ("person", "typed just now", "sending")]
    assert result["turns"][ids[3]]["status_text"] == "Queued behind the current turn"
    assert result["turns"][ids[0]]["status_text"] == "Failed: limited"
    assert result["turns"][ids[4]]["status_text"] == "Sending"
