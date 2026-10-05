"""First-message titles through the real daemon and an isolated fake Claude CLI."""

from __future__ import annotations

import json
import sys
import time

import pytest

from subfleet.conversations import titles
from tests.e2e.test_conversations import conv

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERPID is macOS")


def conversation(conv, cid):
    return conv.call("conversation.open", conversation_id=cid)["conversation"]


def title_requests(conv):
    return [row for row in conv.stdin_rows()
            if row.get("type") == "control_request"
            and row.get("request", {}).get("subtype") == "generate_session_title"]


def test_claude_names_the_conversation_after_its_first_reply_in_the_turn_process(conv):
    """The title is asked of the first turn's own process once its reply is in (titles.py):
    the provider's result comes first on its stdout, the request after it on its stdin,
    and the turn settles once the answer frees the held close."""
    cid = conv.create()
    text = "Fix the importer"
    first = conv.submit(cid, text)
    named = conv.e2e.until(lambda: (c if (c := conversation(conv, cid))["title_source"] == "generated" else None),
                          timeout=20)
    assert named["title"] == "Fixture session title"
    assert conv.until_state(first, "complete", "failed")["state"] == "complete"
    assert any(e["kind"] == "turn.completed" for e in conv.events(cid)), "the reply came first"
    [request] = title_requests(conv)
    assert request["request"] == {"subtype": "generate_session_title", "description": text, "persist": False}
    rows = [json.loads(row["stdin"]) for row in conv.turn_log() if "stdin" in row]
    assert [row.get("type") for row in rows].index("user") < rows.index(request)
    log = conv.turn_log()
    [launch] = [row for row in log if "argv" in row]
    title_pid = next(row["pid"] for row in log if "stdin" in row
                     and json.loads(row["stdin"]) == request)
    user_pid = next(row["pid"] for row in log if "stdin" in row
                    and json.loads(row["stdin"]).get("type") == "user")
    assert title_pid == user_pid == launch["pid"], "the existing turn process performs naming"


def test_a_followup_sent_while_the_title_is_asked_goes_at_once(conv):
    """A titler that never answers holds the first turn's stdin close for at most its budget,
    and nothing waits on it: the conversation's next message frees the close at once, and
    runs and completes well inside the budget."""
    cid = conv.create()
    first = conv.submit(cid, "Fix the importer [fake:title-timeout]")
    conv.e2e.until(lambda: title_requests(conv), timeout=20)
    second = conv.submit(cid, "And the exporter", after_message_id=first)
    # Held for the budget, the first turn would settle only after it: the message freed it.
    assert conv.until_state(first, "complete", "failed", timeout=titles.TITLE_BUDGET_S - 2)["state"] == "complete"
    assert conv.until_state(second, "complete", "failed", timeout=30)["state"] == "complete"
    assert conversation(conv, cid)["title_source"] == "fallback"
    assert len(title_requests(conv)) == 1


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
    mid = conv.submit(cid, "Fix the importer [fake:title-delayed]")
    conv.e2e.until(lambda: title_requests(conv), timeout=20)
    renamed = conv.call("conversation.rename", conversation_id=cid, title="My importer plan")
    assert renamed["conversation"]["title_source"] == "person"
    # The rename also frees the close the title held: the turn settles without its answer.
    assert conv.until_state(mid, "complete", "failed", timeout=20)["state"] == "complete"
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
def test_unavailable_claude_title_preserves_fallback_and_holds_the_turn_at_most_its_budget(conv, directive):
    """An error or a null title frees the held close at once; a titler that never answers
    holds it for the budget and no longer (titles.py)."""
    cid = conv.create()
    started = time.monotonic()
    mid = conv.submit(cid, f"Please fix the importer; more detail [fake:{directive}]")
    assert conv.until_state(mid, "complete", "failed", timeout=30)["state"] == "complete"
    held = titles.TITLE_BUDGET_S if directive == "title-timeout" else 0
    assert time.monotonic() - started < held + 8, "the title held the turn past its budget"
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
