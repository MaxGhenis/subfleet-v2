"""Suggested conversations in the app's core (C-25.7, C-29.14, C-29.9, C-25.4).

The daemon's real service and store (`daemon_harness.ServiceHarness`) make the
suggestions, start, dismiss and withdraw them; the app's Swift models decode
every answer losslessly, its store folds the list and the watch feed into the
sidebar's Suggested section, and its timeline folds the `suggestion` events into
one card per suggestion. Requests the Swift side encodes reach the daemon's own
`protocol.decode_request` and handlers.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import uuid

import pytest

from subfleet import protocol
from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import ServiceHarness
from tests.frontend.test_core_protocol import assert_lossless

pytestmark = needs_swift

BRIEF = "Port the importer to the new manifest.\n\n- Read `docs/plan.md`\n- Run the importer tests"


@pytest.fixture
def harness():
    root = Path(tempfile.mkdtemp(prefix="sf-app-sg-", dir="/tmp"))
    harness = ServiceHarness(root)
    yield harness
    harness.close()
    shutil.rmtree(root, ignore_errors=True)


def origin(harness) -> tuple[str, str]:
    """A conversation bound to a native session: what a turn of it suggests lands there."""
    cid = harness.create(title="Importer work")["conversation_id"]
    session = str(uuid.uuid4())
    harness.store.update_conversation(cid, native_session_id=session)
    return cid, session


def suggest(harness, title: str, **extra) -> dict:
    args = {"request_id": str(uuid.uuid4()), "title": title, "tldr": "Out of scope here", "brief": BRIEF,
            "workspace": str(harness.workspace), **extra}
    return harness.call("suggestion.create", **args)["suggestion"]


def suggestions(core_probe, tmp_path, steps, **extra):
    return run_probe(core_probe, "suggestions", write_json(tmp_path / f"sg-{uuid.uuid4().hex[:6]}.json",
                                                           {"steps": steps, **extra}))


def test_c29_14_every_suggestion_answer_and_feed_row_decodes_losslessly(core_probe, harness, tmp_path):
    cid, session = origin(harness)
    made = harness.call("suggestion.create", request_id=str(uuid.uuid4()), title="Port the importer",
                        tldr="Out of scope here", brief=BRIEF, workspace=str(harness.workspace), session_id=session,
                        worktree=True)
    started = harness.call("suggestion.start", suggestion_id=made["suggestion"]["suggestion_id"])
    long = suggest(harness, "A long brief", session_id=session, brief="word " * 1000)
    dismissed = harness.call("suggestion.dismiss", suggestion_id=suggest(harness, "Dismiss me")["suggestion_id"])
    withdrawn = harness.call("suggestion.withdraw", session_id=session,
                             suggestion_id=suggest(harness, "Withdraw me", session_id=session)["suggestion_id"])
    listed = harness.call("suggestion.list")
    full = harness.call("suggestion.list", suggestion_ids=[long["suggestion_id"]])
    watch = harness.call("conversation.watch", after=0)
    events = harness.call("conversation.events", conversation_id=cid, after=0)
    assert any(c.get("suggestion") for c in watch["changes"]) and any(c["conversation_id"] == "" for c in watch["changes"])
    assert [e["data"]["state"] for e in events["events"] if e["kind"] == "suggestion"][:3] == ["open", "starting", "started"]
    assert listed["suggestions"][0]["brief_truncated"] is True and full["suggestions"][0]["brief_truncated"] is False
    for op, result in (("suggestion.create", made), ("suggestion.start", started), ("suggestion.dismiss", dismissed),
                       ("suggestion.withdraw", withdrawn), ("suggestion.list", listed), ("suggestion.list", full),
                       ("conversation.watch", watch), ("conversation.events", events)):
        assert_lossless(core_probe, tmp_path, op, result)


def test_c25_2_the_apps_suggestion_requests_reach_the_daemons_handlers(core_probe, harness, tmp_path):
    sid = suggest(harness, "Start me")["suggestion_id"]
    for op, args in (("suggestion.list", {"suggestion_ids": [sid]}),
                     ("suggestion.start", {"suggestion_id": sid, "permission": "accept-edits", "confirm_widen": True}),
                     ("suggestion.dismiss", {"suggestion_id": sid})):
        line = run_probe(core_probe, "request", op, write_json(tmp_path / f"{op}.json", args), raw=True)
        request = protocol.decode_request(line)
        assert request.op == op and request.args == args
    request = protocol.decode_request(run_probe(core_probe, "request", "suggestion.start",
                                                write_json(tmp_path / "start.json", {"suggestion_id": sid,
                                                                                     "permission": "accept-edits",
                                                                                     "confirm_widen": True}), raw=True))
    out = harness.call(request.op, **request.args)
    assert out["conversation"]["settings"]["permission"] == "accept-edits"
    # Start reads its timeout floor as the other git ops do.
    assert run_probe(core_probe, "timeout", "suggestion.start", write_json(tmp_path / "t.json", {"suggestion_id": sid})) >= 120


def test_c29_14_the_suggested_section_lists_every_open_one_newest_first_with_a_count(core_probe, harness, tmp_path):
    cid, session = origin(harness)
    from_turn = suggest(harness, "From the conversation", session_id=session)
    elsewhere = suggest(harness, "From a desktop session", session_id="desktop-session")
    anonymous = suggest(harness, "From a script")
    codex = suggest(harness, "A Codex review", provider="codex")
    listed = harness.call("conversation.list", include_catalog=False)
    daemon = harness.call("suggestion.list")
    out = suggestions(core_probe, tmp_path, [{"list": listed}, {"suggestions": daemon}],
                      capabilities=harness.call("capabilities"))
    # Newest first, as the daemon lists them (creations in one millisecond fall back to the id, on both sides).
    shown = [c["id"] for c in out["open"]]
    assert shown == [s["suggestion_id"] for s in daemon["suggestions"]]
    stamps = {s["suggestion_id"]: (s["created_at"], s["suggestion_id"]) for s in daemon["suggestions"]}
    assert [stamps[i] for i in shown] == sorted(stamps.values(), reverse=True)
    assert set(shown) == {from_turn["suggestion_id"], elsewhere["suggestion_id"], anonymous["suggestion_id"],
                          codex["suggestion_id"]}
    assert out["count"] == 4
    by_id = {c["id"]: c for c in out["open"]}
    assert by_id[from_turn["suggestion_id"]]["origin"] == "Importer work"
    assert by_id[elsewhere["suggestion_id"]]["origin"] is None
    assert by_id[codex["suggestion_id"]]["start_permissions"] == ["read-only"]
    assert by_id[from_turn["suggestion_id"]]["start_permissions"] == ["ask", "accept-edits", "bypass"]
    assert by_id[from_turn["suggestion_id"]]["where"].endswith("· Claude")
    # The sidebar's filters apply to the section; the badge counts every one.
    filtered = suggestions(core_probe, tmp_path, [{"suggestions": harness.call("suggestion.list")},
                                                  {"provider_filter": "codex"}])
    assert [c["id"] for c in filtered["open"]] == [codex["suggestion_id"]] and filtered["count"] == 4
    searched = suggestions(core_probe, tmp_path, [{"suggestions": harness.call("suggestion.list")}, {"search": "script"}])
    assert [c["id"] for c in searched["open"]] == [anonymous["suggestion_id"]]


def test_c29_9_the_feed_keeps_the_section_live_and_a_row_with_no_origin_changes_no_conversation(
        core_probe, harness, tmp_path):
    cid, session = origin(harness)
    listed = harness.call("conversation.list", include_catalog=False)
    start = harness.call("conversation.watch", after=0)["next"]
    first = suggest(harness, "Open, then started", session_id=session)
    second = suggest(harness, "Open, then dismissed")
    third = suggest(harness, "Open, then withdrawn", session_id=session)
    page = harness.call("conversation.watch", after=start)
    opened = suggestions(core_probe, tmp_path, [{"list": listed}, {"watch": {**page, "changes": page["changes"]}}])
    assert {c["id"] for c in opened["open"]} == {first["suggestion_id"], second["suggestion_id"], third["suggestion_id"]}
    assert "" not in opened["pending"]                   # the origin-less row named no conversation
    assert opened["sidebar_entries"] == ["cv:" + cid]
    harness.call("suggestion.start", suggestion_id=first["suggestion_id"])
    harness.call("suggestion.dismiss", suggestion_id=second["suggestion_id"])
    harness.call("suggestion.withdraw", suggestion_id=third["suggestion_id"], session_id=session)
    later = harness.call("conversation.watch", after=page["next"])
    closed = suggestions(core_probe, tmp_path, [{"list": listed}, {"watch": page}, {"watch": later}])
    assert closed["open"] == [] and closed["count"] == 0
    assert set(closed["known"]) == {first["suggestion_id"], second["suggestion_id"], third["suggestion_id"]}
    # A list read that raced the feed never moves a card back.
    stale = harness.call("suggestion.list")
    stale["suggestions"] = [{**s, "state": "open"} for s in stale["suggestions"]]
    raced = suggestions(core_probe, tmp_path, [{"watch": page}, {"watch": later}, {"suggestions": stale}])
    assert raced["open"] == []


def test_c29_14_the_timeline_shows_one_card_per_suggestion_on_the_turn_that_made_it(core_probe, harness, tmp_path):
    cid, session = origin(harness)
    mid = harness.submit(cid, "work on it")["message_id"]
    job = "20261011-000000-turn-card"
    # The turn's job, as the job store answers the caller lookup (`_suggestion_caller`).
    harness.daemon.store.one = lambda sql, params=(): ({"name": f"turn-{cid}", "request_id": f"turn:{mid}:0"}
                                                       if params == (job,) else None)
    made = harness.call("suggestion.create", request_id=str(uuid.uuid4()), title="Port the importer",
                        tldr="Out of scope here", brief=BRIEF, workspace=str(harness.workspace), calling_job=job)
    sid = made["suggestion"]["suggestion_id"]
    assert made["suggestion"]["origin_message_id"] == mid
    first = harness.call("conversation.events", conversation_id=cid, after=0)
    open_card = suggestions(core_probe, tmp_path, [{"events": first, "conversation_id": cid}])
    cards = open_card["timelines"][cid]
    assert [(c["item"], c["message_id"], c["card"]["state"]) for c in cards] == [(f"suggestion:{sid}", mid, "open")]
    assert cards[0]["card"]["brief"] == BRIEF and cards[0]["card"]["state_words"] is None
    harness.call("suggestion.start", suggestion_id=sid)
    second = harness.call("conversation.events", conversation_id=cid, after=first["next"])
    updated = suggestions(core_probe, tmp_path, [{"events": first, "conversation_id": cid},
                                                 {"events": second, "conversation_id": cid}])
    cards = updated["timelines"][cid]
    assert len(cards) == 1 and cards[0]["card"]["state"] == "started" and cards[0]["card"]["state_words"] == "Started"
    # Read again from 0 (after a reset), the card is the same one, as it is now.
    again = harness.call("conversation.events", conversation_id=cid, after=0)
    reread = suggestions(core_probe, tmp_path, [{"events": again, "conversation_id": cid}])
    assert [c["card"]["state"] for c in reread["timelines"][cid]] == ["started"]


def test_c29_14_a_listing_preview_never_replaces_a_whole_brief(core_probe, harness, tmp_path):
    long = suggest(harness, "A long brief", brief="word " * 1000)
    full = harness.call("suggestion.list", suggestion_ids=[long["suggestion_id"]])["suggestions"][0]
    preview = harness.call("suggestion.list")["suggestions"][0]
    assert preview["brief_truncated"] and not full["brief_truncated"]
    out = suggestions(core_probe, tmp_path, [{"upsert": full}, {"upsert": preview}])
    card = out["open"][0]
    assert card["brief_truncated"] is False and card["brief_chars"] == 5000 and card["brief"] == "word " * 1000


def test_c29_14_a_start_cut_short_says_why_and_offers_start_again(core_probe, harness, tmp_path):
    made = suggest(harness, "Cut short")
    row = harness.service.suggestions.get(made["suggestion_id"])
    harness.service.suggestions.claim(made["suggestion_id"], by="frontend harness",
                                      conversation=harness.service._start_fields(row, {}))
    harness.service.suggestions.note_error(made["suggestion_id"], "worktree-failed: git worktree add failed")
    out = suggestions(core_probe, tmp_path, [{"suggestions": harness.call("suggestion.list")}])
    card = out["open"][0]
    assert card["state"] == "starting" and out["count"] == 1
    assert card["state_words"] == ("Not started yet: worktree-failed: git worktree add failed. "
                                   "Start again to finish it.")
