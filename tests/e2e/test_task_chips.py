"""Task chips through real daemon sockets and CLI-launched stdio MCP pipes.

Every root, provider home and workspace belongs to the disposable e2e harness.
Person-only Start/Dismiss use the existing controlling-terminal peer helper.
"""

from __future__ import annotations

import json
import sys

import pytest

from tests.e2e.test_conversations import Conversations

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERPID is macOS")


@pytest.fixture
def chips(e2e):
    destination = e2e.root / "suggested-project"
    destination.mkdir()
    proposal = {
        "title": "Check the suggested project",
        "tldr": "The current session uncovered a follow-up. Check it in its own session.",
        "prompt": "  Inspect this project's files.\r\n\r\nKeep this exact Unicode prompt: café 🧪.\n",
        "cwd": str(destination),
    }
    e2e.env["SUBFLEET_FAKE_TURN_LOG"] = str(e2e.root / "turns.jsonl")
    e2e.env["SUBFLEET_FAKE_CHIP"] = json.dumps(proposal)
    e2e.start()
    return Conversations(e2e), proposal


def propose(conv, *, dismiss=False, **settings):
    cid = conv.create(**settings)
    mid = conv.submit(cid, "[fake:chip-dismiss]" if dismiss else "[fake:chip]")
    done = conv.until_state(mid, "complete", "failed", "delivery-unknown", timeout=45)
    assert done["state"] == "complete", (done, conv.turn_log(), conv.e2e.log_text())
    conv.attempt(mid)
    proposed = conv.call("chip.list", conversation_id=cid)["chips"]
    assert len(proposed) == 1, proposed
    return cid, mid, proposed[0]


def test_cli_launched_mcp_proposal_survives_daemon_restart(chips):
    conv, proposal = chips
    cid, mid, chip = propose(conv)
    assert {key: chip[key] for key in proposal} == proposal
    assert (chip["parent_conversation_id"], chip["message_id"], chip["state"]) == (cid, mid, "pending")
    assert chip["child_conversation_id"] is None
    assert conv.call("conversation.open", conversation_id=cid)["chips"] == [chip]
    created = [event for event in conv.events(cid) if event["kind"] == "chip.created"]
    assert len(created) == 1 and created[0]["message_id"] == mid
    assert created[0]["data"]["chip"]["chip_id"] == chip["chip_id"]
    assert "prompt" not in created[0]["data"]["chip"]
    calls = [row["mcp_method"] for row in conv.turn_log() if "mcp_method" in row]
    assert calls == ["initialize", "tools/list", "tools/call"]
    assert not any((row.get("request") or {}).get("subtype") == "mcp_message" for row in conv.stdin_rows())

    conv.e2e.crash()
    conv.e2e.start()
    assert conv.call("chip.list", conversation_id=cid)["chips"] == [chip]
    opened = conv.call("conversation.open", conversation_id=cid)
    assert opened["chips"] == [chip] and len(opened["messages"]) == 1
    assert len([event for event in conv.events(cid) if event["kind"] == "chip.created"]) == 1


def test_cli_can_withdraw_its_chip_through_mcp(chips):
    conv, _ = chips
    cid, _, chip = propose(conv, dismiss=True)
    assert chip["state"] == "dismissed" and chip["child_conversation_id"] is None
    assert chip["dismissal_reason"] == "fixed in this session"
    events = [event["kind"] for event in conv.events(cid) if event["kind"].startswith("chip.")]
    assert events == ["chip.created", "chip.dismissed"]
    calls = [row for row in conv.turn_log() if row.get("mcp_method") == "tools/call"]
    assert len(calls) == 2 and all(not row["result"].get("isError") for row in calls)


def test_headless_agent_cannot_start_or_dismiss_a_chip(chips):
    conv, _ = chips
    cid, _, chip = propose(conv)
    for operation in ("chip.start", "chip.dismiss"):
        response = conv.as_agent(operation, chip_id=chip["chip_id"])
        assert not response["ok"], response
    assert conv.call("chip.list", conversation_id=cid)["chips"][0]["state"] == "pending"


def test_person_start_creates_one_exact_child_with_current_parent_defaults(chips):
    conv, proposal = chips
    cid, mid, chip = propose(conv, effort="high", fast=True)
    # Defaults are chosen when the person starts, even if the suggestion waited.
    settings = {"model": "sonnet", "effort": "medium", "fast": False, "permission": "ask", "auto_continue": True}
    conv.call("conversation.settings", conversation_id=cid, settings=settings)
    response = conv.as_person("chip.start", chip_id=chip["chip_id"])
    assert response["ok"], response
    started = response["result"]
    child, first = started["conversation"], started["message"]
    assert started["chip"]["state"] == "started"
    assert child["parent_conversation_id"] == cid and child["source_chip_id"] == chip["chip_id"]
    assert (child["title"], child["workspace"], child["provider"]) == (
        proposal["title"], proposal["cwd"], "claude")
    assert child["settings"] == settings and first["settings"] == settings
    assert first["text"] == proposal["prompt"] and first["seq"] == 1
    assert conv.until_state(first["message_id"], "complete", "failed", timeout=45)["state"] == "complete"
    conv.attempt(first["message_id"])
    reopened = conv.call("conversation.open", conversation_id=child["conversation_id"])
    parent = conv.call("conversation.open", conversation_id=cid)
    assert len(reopened["messages"]) == 1 and len(parent["messages"]) == 1
    assert reopened["conversation"]["native_session_id"] != parent["conversation"]["native_session_id"]
    users = [row for row in conv.stdin_rows() if row.get("type") == "user"]
    assert [row["uuid"] for row in users] == [mid, first["message_id"]]
    assert users[-1]["message"]["content"] == [{"type": "text", "text": proposal["prompt"]}]

    again = conv.as_person("chip.start", chip_id=chip["chip_id"])
    assert again["ok"] and again["result"]["conversation"]["conversation_id"] == child["conversation_id"]
    assert again["result"]["message"]["message_id"] == first["message_id"]
    assert len([event for event in conv.events(cid) if event["kind"] == "chip.started"]) == 1
    withdrawn = conv.as_person("chip.dismiss", chip_id=chip["chip_id"])
    assert not withdrawn["ok"], withdrawn


def test_person_dismiss_is_terminal_and_repeatable(chips):
    conv, _ = chips
    cid, _, chip = propose(conv)
    response = conv.as_person("chip.dismiss", chip_id=chip["chip_id"])
    assert response["ok"] and response["result"]["chip"]["state"] == "dismissed"
    again = conv.as_person("chip.dismiss", chip_id=chip["chip_id"])
    assert again["ok"] and again["result"]["chip"] == response["result"]["chip"]
    started = conv.as_person("chip.start", chip_id=chip["chip_id"])
    assert not started["ok"], started
    assert len([event for event in conv.events(cid) if event["kind"] == "chip.dismissed"]) == 1
