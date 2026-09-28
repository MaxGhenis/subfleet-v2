"""First-message titles through the real daemon and an isolated fake Claude CLI."""

from __future__ import annotations

import json
import sys
import time

import pytest

from tests.e2e.test_conversations import conv

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERPID is macOS")


def conversation(conv, cid):
    return conv.call("conversation.open", conversation_id=cid)["conversation"]


def title_requests(conv):
    return [row for row in conv.stdin_rows()
            if row.get("type") == "control_request"
            and row.get("request", {}).get("subtype") == "generate_session_title"]


def interrupt(conv, mid):
    conv.call("turn.interrupt", message_id=mid)
    assert conv.until_state(mid, "interrupted", "failed", "complete", timeout=20)["state"] == "interrupted"


def test_claude_names_before_first_reply_in_the_turn_process(conv):
    cid = conv.create()
    text = "Fix the importer [fake:quiet-slow]"
    first = conv.submit(cid, text)
    named = conv.e2e.until(lambda: (c if (c := conversation(conv, cid))["title_source"] == "generated" else None),
                          timeout=10)
    assert named["title"] == "Fixture session title"
    assert conv.message(first)["state"] == "running"
    assert not any(e["kind"] == "text" for e in conv.events(cid)), "title arrives before the first reply"
    [request] = title_requests(conv)
    assert request["request"] == {"subtype": "generate_session_title", "description": text, "persist": False}
    log = conv.turn_log()
    [launch] = [row for row in log if "argv" in row]
    title_pid = next(row["pid"] for row in log if "stdin" in row
                     and json.loads(row["stdin"]) == request)
    user_pid = next(row["pid"] for row in log if "stdin" in row
                    and json.loads(row["stdin"]).get("type") == "user")
    assert title_pid == user_pid == launch["pid"], "the existing turn process performs naming"

    interrupt(conv, first)


def test_a_followup_never_regenerates_the_title(conv):
    cid = conv.create()
    first = conv.submit(cid, "Fix the importer")
    assert conv.until_state(first, "complete", "failed")["state"] == "complete"
    conv.attempt(first)
    before = conversation(conv, cid)
    second = conv.submit(cid, "A completely different subject", after_message_id=first)
    assert conv.until_state(second, "complete", "failed")["state"] == "complete"
    after = conversation(conv, cid)
    assert (after["title"], after["title_source"]) == (before["title"], before["title_source"])
    assert len(title_requests(conv)) == 1, "only the conversation's first message requests a title"


def test_a_rename_wins_over_a_title_already_in_flight(conv):
    cid = conv.create()
    mid = conv.submit(cid, "Fix the importer [fake:quiet-slow] [fake:title-delayed]")
    conv.e2e.until(lambda: title_requests(conv), timeout=10)
    renamed = conv.call("conversation.rename", conversation_id=cid, title="My importer plan")
    assert renamed["conversation"]["title_source"] == "person"
    conv.e2e.until(lambda: any("title_response" in row for row in conv.turn_log()), timeout=10)
    interrupt(conv, mid)
    kept = conversation(conv, cid)
    assert (kept["title"], kept["title_source"]) == ("My importer plan", "person")


def test_a_persons_initial_title_is_never_generated(conv):
    cid = conv.create(title="My importer plan")
    mid = conv.submit(cid, "Fix the importer")
    assert conv.until_state(mid, "complete", "failed")["state"] == "complete"
    kept = conversation(conv, cid)
    assert (kept["title"], kept["title_source"]) == ("My importer plan", "person")
    assert title_requests(conv) == []


@pytest.mark.parametrize("directive", ["title-error", "title-null", "title-timeout"])
def test_unavailable_claude_title_preserves_fallback_without_delaying_the_turn(conv, directive):
    cid = conv.create()
    started = time.monotonic()
    mid = conv.submit(cid, f"Please fix the importer; more detail [fake:{directive}]")
    assert conv.until_state(mid, "complete", "failed", timeout=8)["state"] == "complete"
    assert time.monotonic() - started < 10, "the 10 s naming budget never holds the turn open"
    named = conversation(conv, cid)
    assert (named["title"], named["title_source"]) == ("the importer", "fallback")
    assert len(title_requests(conv)) == 1


def test_codex_uses_first_clause_fallback_without_a_claude_process(conv):
    cid = conv.create(provider="codex", model="gpt-6-astra", permission="read-only")
    mid = conv.submit(cid, "Please fix the importer; more detail about the bug")
    assert conv.until_state(mid, "complete", "failed")["state"] == "complete"
    named = conversation(conv, cid)
    assert (named["title"], named["title_source"]) == ("the importer", "fallback")
    assert title_requests(conv) == []
    assert all("--input-format" not in row["argv"] for row in conv.turn_log() if "argv" in row)
