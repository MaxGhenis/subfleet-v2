"""Suggested conversations through the real daemon, guardian, relay and fake Claude (C-25.7, C-29.14).

An agent's turn runs `subfleet suggest` as its Bash tool would (in the turn's
process tree, with the C-26.13 markers and `SUBFLEET_TURN_JOB`); the app-facing
ops see the suggestion; an agent cannot start it; a person's `suggestion.start`
makes a worktree conversation whose first turn receives the brief, and the
origin conversation's events end in the started card. Person-only requests need
a terminal (`Conversations.as_person`), so that half skips inside a Subfleet lane;
`tests/fake/test_suggestions_fake.py` covers it with the real service there.
"""

from __future__ import annotations

import json
import os
import shlex
import sys

import pytest

from tests.e2e.test_conversations import Conversations

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERPID is macOS")

BRIEF = "Port the importer to the new manifest.\n\nRun the importer tests and report what changed."


def act(e2e, *argv: str) -> None:
    """What the fake Claude's next Bash tool call runs: `subfleet <argv>`."""
    (e2e.root / "action.sh").write_text(shlex.join([sys.executable, "-m", "subfleet.cli", *argv]) + "\n")


def suggesting(e2e) -> Conversations:
    brief = e2e.root / "brief.md"
    brief.write_text(BRIEF)
    act(e2e, "suggest", "--title", "Port the importer", "--tldr", "Out of scope for this turn; worth its own session",
        "--brief-file", str(brief), "--worktree", "--json")
    e2e.env["SUBFLEET_FAKE_TURN_LOG"] = str(e2e.root / "turns.jsonl")
    e2e.env["SUBFLEET_FAKE_BASH_COMMAND"] = shlex.join(["/bin/sh", str(e2e.root / "action.sh")])
    e2e.start()
    return Conversations(e2e)


def test_c25_7_an_agent_turn_suggests_and_a_person_starts_a_worktree_conversation(e2e):
    conv = suggesting(e2e)
    cid = conv.create()
    mid = conv.submit(cid, "fix the exporter [fake:bash]")
    assert conv.until_state(mid, "complete", "failed", "delivery-unknown", timeout=60)["state"] == "complete"
    ran = [row for row in conv.turn_log() if "bash" in row]
    assert len(ran) == 1 and ran[0]["rc"] == 0, ran
    made = json.loads(ran[0]["stdout"])
    row = made["suggestion"]
    sid = row["suggestion_id"]
    # Inside the turn: the origin is its conversation and message, the folder the turn's.
    assert made["created"] is True and row["state"] == "open"
    assert (row["origin_conversation_id"], row["origin_message_id"]) == (cid, mid)
    assert row["worktree"] is True and os.path.realpath(row["workspace"]) == os.path.realpath(e2e.workdir)
    assert row["brief"] == BRIEF

    # What the app reads: the list, the watch feed, the origin's events.
    listed = conv.call("suggestion.list")
    assert listed["open"] == 1 and [s["suggestion_id"] for s in listed["suggestions"]] == [sid]
    fed = [c for c in conv.call("conversation.watch", after=0)["changes"] if c.get("suggestion_id") == sid]
    assert fed and fed[-1]["conversation_id"] == cid and fed[-1]["suggestion"]["state"] == "open"
    cards = [e for e in conv.events(cid) if e["kind"] == "suggestion"]
    assert [(e["message_id"], e["data"]["state"]) for e in cards] == [(mid, "open")]

    # No agent may start it (C-25.6): a headless caller is refused, nothing changes.
    refused = conv.as_agent("suggestion.start", suggestion_id=sid)
    assert refused["ok"] is False and refused["error"]["code"] == 7
    assert conv.call("suggestion.list", suggestion_ids=[sid])["suggestions"][0]["state"] == "open"

    # A person's Start (skips inside a Subfleet lane: the peer check reads every ancestor).
    started = conv.as_person("suggestion.start", suggestion_id=sid)
    assert started["ok"], started
    result = started["result"]
    new = result["conversation"]
    assert new["workspace_kind"] == "worktree" and new["worktree"]["branch"] == f"subfleet/{new['conversation_id']}"
    first = result["message"]["message_id"]
    assert result["message"]["text"] == BRIEF
    assert conv.until_state(first, "complete", "failed", "delivery-unknown", timeout=60)["state"] == "complete"
    sent = [r for r in conv.stdin_rows() if r.get("type") == "user" and r.get("uuid") == first]
    assert len(sent) == 1
    content = sent[0]["message"]["content"]
    text = content if isinstance(content, str) else "".join(part.get("text", "") for part in content)
    assert text == BRIEF
    launched = [r for r in conv.turn_log() if "argv" in r and "--input-format" in r["argv"]]
    assert os.path.realpath(launched[-1]["cwd"]) == os.path.realpath(new["workspace"])
    assert os.path.realpath(new["workspace"]).startswith(os.path.realpath(new["worktree"]["path"]))
    # Retried, the same conversation; the origin's card ends started.
    again = conv.as_person("suggestion.start", suggestion_id=sid)
    assert again["result"]["conversation"]["conversation_id"] == new["conversation_id"]
    assert again["result"]["created"] is False
    cards = [e for e in conv.events(cid) if e["kind"] == "suggestion"]
    assert [e["data"]["state"] for e in cards] == ["open", "starting", "started"]
    assert cards[-1]["data"]["started_conversation_id"] == new["conversation_id"]


def test_c25_7_the_creator_withdraws_from_a_later_turn(e2e):
    """The same conversation's next turn may withdraw what an earlier one suggested."""
    conv = suggesting(e2e)
    cid = conv.create()
    first = conv.submit(cid, "suggest a follow-up [fake:bash]")
    assert conv.until_state(first, "complete", "failed", "delivery-unknown", timeout=60)["state"] == "complete"
    sid = json.loads([r for r in conv.turn_log() if "bash" in r][0]["stdout"])["suggestion"]["suggestion_id"]
    act(e2e, "suggest", "--withdraw", sid, "--json")
    second = conv.submit(cid, "that follow-up went stale [fake:bash]", after_message_id=first)
    assert conv.until_state(second, "complete", "failed", "delivery-unknown", timeout=60)["state"] == "complete"
    withdrew = json.loads([r for r in conv.turn_log() if "bash" in r][-1]["stdout"])
    assert withdrew["changed"] is True and withdrew["suggestion"]["state"] == "withdrawn"
    assert conv.call("suggestion.list")["open"] == 0
