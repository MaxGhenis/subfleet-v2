"""Review r1b of PR 181 (suggested conversations), its probes kept as regressions.

The reviewer's local reproductions (`review-r1b-evidence/test_review181_adversarial.py`,
`test_review181_claim_poison_followup.py`, `test_review181_bounds_visibility.py` and
`local_unresolved_probes.py`), each asserting the fixed behaviour. The three P1s:

- an agent's message became a suggestion-started conversation's first message
  after a crash between the claim and the brief (and an agent that accepted the
  brief itself, under the derived id, then added work reached `started` with two
  messages): now nothing but the start's own brief is accepted while it is
  `starting` (`suggestions.admit_message`), and the brief's id is the claim's own;
- an agent reserved `suggestion:<id>` and Start adopted its conversation: now
  Start only ever uses the conversation its claim made, and the namespace is
  refused to every create and handoff;
- a failing checkout hook's output reached the list, the feed and the origin's
  events unscrubbed: now every start error is scrubbed whole, then bounded.

Plus the races, rollbacks, bounds and visibility checks the review ran and found
holding, so they keep holding.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from subfleet.conversations import suggestions
from subfleet.conversations.peers import Verdict
from subfleet.conversations.store import ConversationError
from tests.unit.test_conversation_service import SETTINGS, conversation, git, repo, svc  # noqa: F401
from tests.unit.test_suggestions import AGENT, PERSON, BRIEF, people, refused, suggest, turn_of  # noqa: F401

#: The id an earlier build derived for a start's first message (uuid5 of the
#: suggestion id): one any caller could compute, and take first.
OLD_NAMESPACE = uuid.UUID("5c2d3f0e-8a4b-4c1e-9f6a-2b7d0e1c4a91")
FIXTURE_SECRET = "sk-ant-api03-" + "A" * 90


def interrupted_claim(svc, monkeypatch, **extra) -> dict:
    """A start whose claim committed and whose process stopped before the brief."""
    row = suggest(svc, **extra)["suggestion"]
    finish = svc._finish_start
    monkeypatch.setattr(svc, "_finish_start", lambda row: (_ for _ in ()).throw(OSError("crash after claim")))
    with pytest.raises(OSError):
        svc.handle("suggestion.start", {"suggestion_id": row["suggestion_id"]}, PERSON)
    monkeypatch.setattr(svc, "_finish_start", finish)
    claimed = svc.suggestions.get(row["suggestion_id"])
    assert claimed["state"] == "starting" and svc.store.messages(claimed["started_conversation_id"]) == []
    return claimed


def turn_jobs(svc) -> list[dict]:
    return svc.daemon.store.query("SELECT * FROM jobs WHERE kind='turn' AND request_id LIKE 'turn:%:0' "
                                  "AND job_id LIKE 'job-%'")


# --- P1: nothing but the brief comes first (service.py:1086 at a2460a01) ------------


def test_r1b_an_agents_message_cannot_poison_a_claimed_start(people, monkeypatch):
    """The review's exact sequence: crash after the claim, an agent's submit, the
    person's retry. Before: the agent's message was accepted and the retry refused
    `out-of-order`. Now the agent is refused and the retry finishes the start."""
    svc = people
    row = interrupted_claim(svc, monkeypatch)
    cid, sid = row["started_conversation_id"], row["suggestion_id"]
    tries = [
        {"message_id": str(uuid.uuid4()), "text": "Unapproved agent first message"},
        {"message_id": str(uuid.uuid5(OLD_NAMESPACE, sid)), "text": row["brief"]},     # the old derived id
        {"message_id": str(uuid.uuid4()), "text": row["brief"]},                        # the brief's own words
        {"message_id": row["start_message_id"], "text": row["brief"]},                  # the private id, leaked
        {"message_id": str(uuid.uuid4()), "text": "after", "after_message_id": str(uuid.uuid4())},
    ]
    for args in tries:
        error = refused("conversation-starting", 7, svc.handle, "message.submit", {"conversation_id": cid, **args},
                        AGENT)
        assert error.fix
    assert svc.store.messages(cid) == []
    started = svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)
    assert started["created"] is True and started["suggestion"]["state"] == "started"
    assert [svc.store.message_text(m) for m in svc.store.messages(cid)] == [row["brief"]]
    assert started["message"]["message_id"] == row["start_message_id"]


def test_r1b_an_agent_that_takes_the_brief_first_never_reaches_started_with_two_messages(people, monkeypatch):
    """The review's I5 variant: the brief pre-submitted under the derived id, then
    an extra message, before the person's retry. Neither is accepted now."""
    svc = people
    row = interrupted_claim(svc, monkeypatch)
    cid, sid = row["started_conversation_id"], row["suggestion_id"]
    derived = str(uuid.uuid5(OLD_NAMESPACE, sid))
    refused("conversation-starting", 7, svc.handle, "message.submit",
            {"conversation_id": cid, "message_id": derived, "text": row["brief"]}, AGENT)
    refused("conversation-starting", 7, svc.handle, "message.submit",
            {"conversation_id": cid, "message_id": str(uuid.uuid4()), "after_message_id": derived,
             "text": "Unapproved second agent message"}, AGENT)
    started = svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)
    messages = svc.store.messages(cid)
    assert started["suggestion"]["state"] == "started" and len(messages) == 1
    assert messages[0]["message_id"] == started["suggestion"]["started_message_id"] and messages[0]["seq"] == 1
    # Once started it is an ordinary conversation: later messages come after the brief.
    later = svc.handle("message.submit", {"conversation_id": cid, "message_id": str(uuid.uuid4()),
                                          "after_message_id": messages[0]["message_id"], "text": "and then"}, AGENT)
    assert later["created"] is True and [m["seq"] for m in svc.store.messages(cid)] == [1, 2]


def test_r1b_the_poisoned_first_message_never_dispatches(people, monkeypatch):
    """Before: the agent's message was dispatched into a turn job. Now there is
    nothing to dispatch until the brief, which is the first turn's prompt."""
    svc = people
    row = interrupted_claim(svc, monkeypatch, workspace=people.test_workspace)
    cid = row["started_conversation_id"]
    refused("conversation-starting", 7, svc.handle, "message.submit",
            {"conversation_id": cid, "message_id": str(uuid.uuid4()), "text": "Unapproved agent first message"},
            AGENT)
    svc._dispatch()
    assert svc.daemon.submits == []
    svc.handle("suggestion.start", {"suggestion_id": row["suggestion_id"]}, PERSON)
    svc._dispatch()
    assert [s.request_id for s in svc.daemon.submits] == [f"turn:{row['start_message_id']}:0"]
    assert Path(svc.daemon.submits[0].prompt_path).read_text() == row["brief"]


def test_r1b_no_writer_puts_a_message_before_the_brief(people, monkeypatch):
    """Every path that writes a message checks in its own transaction: a wake's, a
    failover continuation's, an unblock note's, a tombstone's, legacy history."""
    svc = people
    row = interrupted_claim(svc, monkeypatch)
    cid = row["started_conversation_id"]
    for origin in ("person", "wake", "failover", "unblock-note"):
        refused("conversation-starting", 7, svc.store.submit_message, conversation_id=cid,
                message_id=str(uuid.uuid4()), after_message_id=None, text="not first", attachments=[],
                settings=SETTINGS, origin=origin)
    refused("conversation-starting", 7, svc._tombstone, str(uuid.uuid4()), cid)
    refused("conversation-starting", 7, svc.store.insert_legacy_history, conversation_id=cid,
            message_id=str(uuid.uuid4()), text="history", state="complete", state_reason=None, settings=SETTINGS,
            created_at="2026-10-11T00:00:00.000Z", updated_at="2026-10-11T00:00:00.000Z")
    # Another suggestion's start cannot name this conversation either.
    other = suggest(svc)["suggestion"]["suggestion_id"]
    refused("not-starting", 1, svc.store.submit_message, conversation_id=cid, message_id=str(uuid.uuid4()),
            after_message_id=None, text=row["brief"], attachments=[], settings=SETTINGS, starts_suggestion=other)
    assert svc.store.messages(cid) == []


def test_r1b_the_first_message_id_is_private_until_started_and_cannot_be_taken_first(people, monkeypatch):
    svc = people
    origin = conversation(svc)
    job, _ = turn_of(svc, origin)
    open_row = suggest(svc, calling_job=job)["suggestion"]
    sid = open_row["suggestion_id"]
    # Before: an agent could take the derived id anywhere and block every Start.
    squat = str(uuid.uuid5(OLD_NAMESPACE, sid))
    svc.handle("message.submit", {"conversation_id": origin, "message_id": squat, "text": "squatting",
                                  "after_message_id": svc.store.messages(origin)[-1]["message_id"]}, AGENT)
    finish = svc._finish_start
    monkeypatch.setattr(svc, "_finish_start", lambda row: (_ for _ in ()).throw(OSError("crash after claim")))
    with pytest.raises(OSError):
        svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)
    monkeypatch.setattr(svc, "_finish_start", finish)
    row = svc.suggestions.get(sid)
    private = row["start_message_id"]
    assert private and private != squat
    shown = json.dumps([svc.handle("suggestion.list", {}, AGENT),
                        svc.handle("suggestion.list", {"suggestion_ids": [sid]}, AGENT),
                        svc.store.changes_after(0, limit=1000), svc.store.events_after(origin, 0)])
    assert private not in shown
    started = svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)
    assert started["suggestion"]["state"] == "started" and started["message"]["message_id"] == private


# --- P1: Start uses only the conversation its claim made (suggestions.py:326) -------


def test_r1b_an_agent_cannot_reserve_the_conversation_a_start_makes(people, tmp_path):
    """The review's redirect: an agent created `suggestion:<id>` in another folder and
    the person's Start adopted it. Now the name is refused; and a conversation an
    earlier build let take it is never adopted."""
    svc = people
    advertised, injected = tmp_path / "advertised", tmp_path / "injected"
    advertised.mkdir()
    injected.mkdir()
    row = suggest(svc, workspace=str(advertised))["suggestion"]
    sid = row["suggestion_id"]
    for name in ("suggestion:" + sid, "suggestion:", "suggestion:anything"):
        refused("reserved-request-id", 2, svc.handle, "conversation.create",
                {"request_id": name, "provider": "claude", "workspace": str(injected), "settings": SETTINGS,
                 "title": "Agent title"}, AGENT)
    source = conversation(svc)
    refused("reserved-request-id", 2, svc.handle, "conversation.handoff",
            {"request_id": "suggestion:" + sid, "from": {"conversation_id": source},
             "to": {"provider": "claude", "workspace": str(injected)}}, AGENT)
    # As an earlier build allowed: a conversation already holding the old name.
    squatter = svc.store.create_conversation(provider="claude", workspace=str(injected), workspace_kind="in-place",
                                             settings=SETTINGS, origin="new", title="Agent title",
                                             request_id="suggestion:" + sid)[0]["conversation_id"]
    started = svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)
    view = started["conversation"]
    assert view["conversation_id"] != squatter and view["workspace"] == str(advertised.resolve())
    assert view["title"] == row["title"] and started["suggestion"]["started_conversation_id"] == view["conversation_id"]
    assert svc.store.messages(squatter) == []
    # Unrelated ids still work.
    assert svc.handle("conversation.create", {"request_id": "sugg:" + sid, "provider": "claude",
                                              "workspace": str(injected), "settings": SETTINGS}, AGENT)["created"]


def test_r1b_the_scratch_folder_is_one_no_create_can_name(people):
    """A suggestion with no folder starts in a scratch folder named outside the
    namespace `conversation.create`'s scratch folders use (sha256 of a request id)."""
    import hashlib
    svc = people
    sid = suggest(svc)["suggestion"]["suggestion_id"]
    old_name = hashlib.sha256(("suggestion:" + sid).encode()).hexdigest()
    view = svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)["conversation"]
    assert Path(view["workspace"]).name == suggestions.scratch_name(sid) != old_name
    assert len(Path(view["workspace"]).name) != 64


# --- P1: start errors are scrubbed (suggestions.py:356) -----------------------------


def test_r1b_a_failing_hooks_secret_never_reaches_the_list_feed_events_or_error(people, repo):
    """The review's leak: a post-checkout hook printed a recognised token and failed."""
    svc = people
    clean, count = suggestions.normalize("Safe title", "Safe summary", FIXTURE_SECRET)
    assert count > 0 and FIXTURE_SECRET not in clean["brief"]
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text('#!/bin/sh\nprintf "%s\\n" "' + FIXTURE_SECRET + '" >&2\nexit 1\n')
    hook.chmod(0o755)
    origin = conversation(svc)
    job, _ = turn_of(svc, origin)
    row = suggest(svc, workspace=str(repo), worktree=True, calling_job=job)["suggestion"]
    with pytest.raises(ConversationError) as caught:
        svc.handle("suggestion.start", {"suggestion_id": row["suggestion_id"]}, PERSON)
    assert caught.value.reason == "worktree-failed" and FIXTURE_SECRET not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)
    stored = svc.suggestions.get(row["suggestion_id"])
    assert stored["start_error"].startswith("worktree-failed: ") and "[REDACTED]" in stored["start_error"]
    shown = json.dumps([svc.handle("suggestion.list", {"suggestion_ids": [row["suggestion_id"]]}, AGENT),
                        svc.handle("suggestion.list", {}, AGENT), svc.store.changes_after(0, limit=1000),
                        svc.store.events_after(origin, 0),
                        svc.store.query("SELECT * FROM suggestions")])
    assert FIXTURE_SECRET not in shown and "A" * 40 not in shown


def test_r1b_a_start_error_is_one_bounded_line_with_the_secret_scrubbed_before_the_cut(people, monkeypatch):
    """Cut first, a token straddling the cut would leave a fragment the rules no
    longer know; scrubbed first, nothing of it remains."""
    svc = people
    row = suggest(svc, workspace=svc.test_workspace)["suggestion"]
    text = "x " * 240 + FIXTURE_SECRET + "\n" + "tail " * 200        # the token across character 500

    def fail(**kwargs):
        raise ConversationError("worktree-failed", text, code=1)
    monkeypatch.setattr(svc.store, "submit_message", fail)
    with pytest.raises(ConversationError) as caught:
        svc.handle("suggestion.start", {"suggestion_id": row["suggestion_id"]}, PERSON)
    stored = svc.suggestions.get(row["suggestion_id"])["start_error"]
    for said in (stored, str(caught.value)):
        assert len(said) <= suggestions.ERROR_MAX and "\n" not in said
        assert "sk-ant" not in said and "AAAAAAAA" not in said


def test_r1b_a_calling_job_shaped_like_a_secret_is_refused_and_kept_nowhere(people):
    """Found re-checking C-25.7 at d0dd86b01: the row kept a caller-stated
    `calling_job` as `creator_job` unchecked. No view, feed row or event carries it,
    but the clause says no string in a row is one the scrubber recognizes. It is
    refused as a secret-shaped session is; a real turn's job is still kept."""
    svc = people
    before = svc.store.changes_after(0)["next"]
    error = refused("bad-calling-job", 2, suggest, svc, calling_job="job " + FIXTURE_SECRET)
    assert FIXTURE_SECRET not in str(error)
    assert svc.store.query("SELECT * FROM suggestions") == [] and svc.store.changes_after(before)["changes"] == []
    origin = conversation(svc)
    job, _ = turn_of(svc, origin)
    sid = suggest(svc, calling_job=job)["suggestion"]["suggestion_id"]
    assert svc.store.one("SELECT creator_job FROM suggestions WHERE suggestion_id=?", (sid,))["creator_job"] == job


def test_r1b_decided_by_is_the_verdict_scrubbed_never_the_start_errors_fallback(people, monkeypatch):
    """d0dd86b01: a Start's `decided_by` is the person's verdict as `scrubbed_line`
    leaves it (scrubbed whole, one line, at most ERROR_MAX characters), never
    `scrub_error`'s "the start failed", which only a start's error falls back to.
    An empty verdict is kept empty, and the start still finishes."""
    svc = people
    long = "word " * 200
    cases = {"": "", "  \n ": "", "a terminal\n(ttys003)": "a terminal (ttys003)",
             "the Subfleet app " + FIXTURE_SECRET: "the Subfleet app [REDACTED]",
             long: " ".join(long.split())[:suggestions.ERROR_MAX - 1] + "…"}
    for said, kept in cases.items():
        monkeypatch.setattr(svc, "_person", lambda peer, what, said=said: Verdict(True, said, os.getpid()))
        sid = suggest(svc, workspace=svc.test_workspace)["suggestion"]["suggestion_id"]
        out = svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)
        assert out["suggestion"]["state"] == "started" and len(svc.store.messages(
            out["conversation"]["conversation_id"])) == 1, repr(said)
        assert out["suggestion"]["decided_by"] == kept, (said[:40], out["suggestion"]["decided_by"][:40])
        assert len(kept) <= suggestions.ERROR_MAX and "the start failed" not in kept


# --- what the review found holding: races, rollbacks, bounds, visibility ------------


@pytest.mark.parametrize("close_op", ["suggestion.dismiss", "suggestion.withdraw"])
def test_r1b_start_races_close(people, close_op):
    svc = people
    for _ in range(12):
        row = suggest(svc, session_id="creator")["suggestion"]
        barrier = threading.Barrier(2)

        def start():
            barrier.wait()
            try:
                return svc.handle("suggestion.start", {"suggestion_id": row["suggestion_id"]}, PERSON)
            except ConversationError as e:
                return e.reason

        def close():
            barrier.wait()
            try:
                return svc.handle(close_op, {"suggestion_id": row["suggestion_id"], "session_id": "creator"},
                                  PERSON if close_op.endswith("dismiss") else AGENT)
            except ConversationError as e:
                return e.reason
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda fn: fn(), [start, close]))
        current = svc.suggestions.get(row["suggestion_id"])
        assert current["state"] in ("started", "dismissed", "withdrawn"), results
        if current["state"] == "started":
            assert len(svc.store.messages(current["started_conversation_id"])) == 1
        else:
            assert current["started_conversation_id"] is None


def test_r1b_two_starts_and_the_finisher_racing_a_retry(people, monkeypatch):
    svc = people
    for resume in (False, True):
        made = {r["conversation_id"] for r in svc.store.query("SELECT conversation_id FROM conversations")}
        row = suggest(svc)["suggestion"]
        if resume:
            finish = svc._finish_start
            monkeypatch.setattr(svc, "_finish_start", lambda row: (_ for _ in ()).throw(OSError("crash")))
            with pytest.raises(OSError):
                svc.handle("suggestion.start", {"suggestion_id": row["suggestion_id"]}, PERSON)
            monkeypatch.setattr(svc, "_finish_start", finish)
        barrier = threading.Barrier(2)

        def first():
            barrier.wait()
            return svc.handle("suggestion.start", {"suggestion_id": row["suggestion_id"]}, PERSON)

        def second():
            barrier.wait()
            if resume:
                svc._resume_start(row["suggestion_id"], 1, svc.clock())
                return None
            return svc.handle("suggestion.start", {"suggestion_id": row["suggestion_id"]}, PERSON)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda fn: fn(), [first, second]))
        current = svc.suggestions.get(row["suggestion_id"])
        assert current["state"] == "started"
        now = {r["conversation_id"] for r in svc.store.query("SELECT conversation_id FROM conversations")}
        assert now - made == {current["started_conversation_id"]}
        assert len(svc.store.messages(current["started_conversation_id"])) == 1
        assert svc.store.message_text(svc.store.message(current["started_message_id"])) == row["brief"]


def test_r1b_claim_and_message_transactions_roll_back_whole(people, monkeypatch):
    svc = people
    row = suggest(svc)["suggestion"]
    made = {r["conversation_id"] for r in svc.store.query("SELECT conversation_id FROM conversations")}
    changed = suggestions._changed

    def fail_claim(tx, moved):
        if moved["state"] == "starting":
            raise sqlite3.OperationalError("fixture disk full at claim")
        changed(tx, moved)
    monkeypatch.setattr(suggestions, "_changed", fail_claim)
    with pytest.raises(sqlite3.OperationalError):
        svc.handle("suggestion.start", {"suggestion_id": row["suggestion_id"]}, PERSON)
    assert svc.suggestions.get(row["suggestion_id"])["state"] == "open"
    assert {r["conversation_id"] for r in svc.store.query("SELECT conversation_id FROM conversations")} == made
    monkeypatch.setattr(suggestions, "_changed", changed)
    finish = svc.suggestions.finish_in
    monkeypatch.setattr(svc.suggestions, "finish_in", lambda *a: (_ for _ in ()).throw(
        sqlite3.OperationalError("fixture disk full before commit")))
    with pytest.raises(sqlite3.OperationalError):
        svc.handle("suggestion.start", {"suggestion_id": row["suggestion_id"]}, PERSON)
    current = svc.suggestions.get(row["suggestion_id"])
    assert current["state"] == "starting" and svc.store.messages(current["started_conversation_id"]) == []
    monkeypatch.setattr(svc.suggestions, "finish_in", finish)
    started = svc.handle("suggestion.start", {"suggestion_id": row["suggestion_id"]}, PERSON)
    assert started["message"]["text"] == row["brief"]


def test_r1b_a_worktree_created_before_a_store_failure_is_adopted(people, repo, monkeypatch):
    svc = people
    row = suggest(svc, workspace=str(repo), worktree=True)["suggestion"]
    update = svc.store.update_conversation

    def fail_update(cid, **fields):
        if fields.get("worktree"):
            raise sqlite3.OperationalError("crash after git worktree add")
        return update(cid, **fields)
    monkeypatch.setattr(svc.store, "update_conversation", fail_update)
    with pytest.raises(sqlite3.OperationalError):
        svc.handle("suggestion.start", {"suggestion_id": row["suggestion_id"]}, PERSON)
    claim = svc.suggestions.get(row["suggestion_id"])
    target = svc.store.root / "worktrees" / ("conversation-" + claim["started_conversation_id"])
    assert target.is_dir()
    monkeypatch.setattr(svc.store, "update_conversation", update)
    started = svc.handle("suggestion.start", {"suggestion_id": row["suggestion_id"]}, PERSON)
    assert started["conversation"]["worktree"]["path"] == str(target)
    assert started["message"]["text"] == row["brief"]


def test_r1b_a_job_written_before_a_failed_binding_is_not_made_twice(people, monkeypatch):
    svc = people
    row = suggest(svc)["suggestion"]
    started = svc.handle("suggestion.start", {"suggestion_id": row["suggestion_id"]}, PERSON)
    mid = started["message"]["message_id"]
    set_state = svc.store.set_state

    def fail_binding(*args, **kwargs):
        if kwargs.get("job_id"):
            raise sqlite3.OperationalError("fixture write fails after job-store commit")
        return set_state(*args, **kwargs)
    monkeypatch.setattr(svc.store, "set_state", fail_binding)
    with pytest.raises(sqlite3.OperationalError):
        svc._dispatch_one(svc.store.message(mid))
    assert len(svc.daemon.store.query("SELECT * FROM jobs WHERE kind='turn'")) == 1
    monkeypatch.setattr(svc.store, "set_state", set_state)
    svc._dispatch_one(svc.store.message(mid))
    assert len(svc.daemon.store.query("SELECT * FROM jobs WHERE kind='turn'")) == 1
    assert svc.store.message(mid)["job_id"] is not None
    assert svc.suggestions.get(row["suggestion_id"])["state"] == "started"


def test_r1b_real_bounds_hold_per_origin_and_in_all(people):
    svc = people
    for origin in range(10):
        for _ in range(20):
            suggest(svc, session_id="fixture-session-" + str(origin))
        refused("too-many-suggestions", 7, suggest, svc, session_id="fixture-session-" + str(origin))
    refused("too-many-suggestions", 7, suggest, svc, session_id="new-origin")
    assert svc.handle("suggestion.list", {}, AGENT)["open"] == 200


def test_r1b_concurrent_creates_respect_the_origin_bound(people):
    def create(_):
        try:
            return suggest(people, session_id="same-origin")["suggestion"]["suggestion_id"]
        except ConversationError as error:
            assert error.reason == "too-many-suggestions"
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create, range(28)))
    assert len([r for r in results if r]) == 20
    assert people.suggestions.listing()[1] == 20


@pytest.mark.parametrize("field,value", [("title", "a" * 61), ("tldr", "a" * 301), ("brief", "a" * 32769),
                                         ("brief", "é" * 16385)])
def test_r1b_oversized_text_is_refused(people, field, value):
    refused("bad-" + field, 2, suggest, people, **{field: value})
    assert people.suggestions.listing()[1] == 0


def test_r1b_a_symlinked_and_dotted_folder_is_canonical_and_its_worktree_is_subfleets(people, repo, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    row = suggest(people, workspace=str(alias / "pkg/.."), worktree=True)["suggestion"]
    assert row["workspace"] == str(repo.resolve()) and row["repository"] == str(repo.resolve())
    started = people.handle("suggestion.start", {"suggestion_id": row["suggestion_id"]}, PERSON)
    assert started["conversation"]["workspace_kind"] == "worktree"
    assert Path(started["conversation"]["workspace"]).is_relative_to(people.root.resolve())
    assert started["conversation"]["workspace"] != str(repo)


def test_r1b_the_stored_brief_is_scrubbed_once_and_public_views_are_limited(people, monkeypatch):
    svc = people
    cid = conversation(svc)
    job, _ = turn_of(svc, cid)
    secret = "sk-ant-api03-" + "A" * 40
    made = suggest(svc, calling_job=job, brief="A fake secret " + secret + "\r\nDone.")
    row = made["suggestion"]
    assert secret not in row["brief"] and made["scrubbed"] > 0

    def forbidden(*a, **kw):
        raise AssertionError("the brief was scrubbed again after it was made")
    monkeypatch.setattr(suggestions, "normalize", forbidden)
    started = svc.handle("suggestion.start", {"suggestion_id": row["suggestion_id"]}, PERSON)
    listed = svc.handle("suggestion.list", {"suggestion_ids": [row["suggestion_id"]]}, AGENT)
    feed = svc.store.changes_after(0)
    events = svc.handle("conversation.events", {"conversation_id": cid, "after": 0}, AGENT)
    assert started["message"]["text"] == row["brief"]
    assert secret not in json.dumps([listed, feed, events])
    for entry in listed["suggestions"]:
        assert not {"request_id", "digest", "start_settings", "creator_job", "start_message_id"} & entry.keys()


# --- the plan the card shows is what Start does (Suggestions.swift:169) -------------


def test_r1b_where_a_suggestion_starts_is_decided_when_it_is_made(people, repo, tmp_path):
    svc = people
    plain = tmp_path / "plain"
    plain.mkdir()
    in_place = suggest(svc, workspace=str(plain), worktree=True)["suggestion"]
    assert (in_place["worktree"], in_place["workspace_kind"], in_place["repository"]) == (True, "in-place", None)
    # The folder became a checkout meanwhile: Start still does what the card said.
    subprocess.run(["git", "clone", "-q", str(repo), str(plain)], check=True, capture_output=True)
    view = svc.handle("suggestion.start", {"suggestion_id": in_place["suggestion_id"]}, PERSON)["conversation"]
    assert view["workspace_kind"] == "in-place" and view["workspace"] == str(plain.resolve())
    # A worktree start whose folder stopped being a checkout is refused, not run in place.
    gone = tmp_path / "gone"
    subprocess.run(["git", "clone", "-q", str(repo), str(gone)], check=True, capture_output=True)
    tree = suggest(svc, workspace=str(gone), worktree=True)["suggestion"]
    assert tree["workspace_kind"] == "worktree" and tree["repository"] == str(gone.resolve())
    shutil.rmtree(gone / ".git")
    error = refused("not-a-repository", 7, svc.handle, "suggestion.start", {"suggestion_id": tree["suggestion_id"]},
                    PERSON)
    assert "worktree" in str(error) and error.fix == "dismiss it"
    assert svc.suggestions.get(tree["suggestion_id"])["state"] == "open"


def test_r1b_a_store_an_earlier_build_made_gains_the_new_columns(tmp_path):
    from subfleet.conversations.store import ConversationStore
    root = tmp_path / "state"
    ConversationStore(root).close()
    db = sqlite3.connect(root / "conversations.sqlite3")
    for name in ("workspace_kind", "repository", "start_message_id"):
        db.execute(f"ALTER TABLE suggestions DROP COLUMN {name}")
    db.commit()
    db.close()
    store = ConversationStore(root)
    try:
        assert {"workspace_kind", "repository", "start_message_id"} <= {
            r["name"] for r in store.query("PRAGMA table_info(suggestions)")}
        assert store.one("SELECT MAX(version) v FROM schema_version")["v"] == 2
    finally:
        store.close()


# --- rollback: the base build opens this build's store ------------------------------

BASE = "4c6a8a9c"


def test_r1b_the_base_build_opens_a_store_this_build_wrote(people, tmp_path):
    """release/217 (the PR's base) opens a store with suggestions in it: the table and
    columns are additive and the schema stays 2. Its package comes from git; a
    checkout without the base commit skips."""
    top = Path(__file__).resolve().parents[2]
    if subprocess.run(["git", "-C", str(top), "cat-file", "-e", BASE + "^{commit}"], capture_output=True).returncode:
        pytest.skip(f"the base commit {BASE} is not in this checkout")
    base = tmp_path / "base"
    base.mkdir()
    archive = subprocess.run(["git", "-C", str(top), "archive", BASE, "subfleet"], capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(base)], input=archive.stdout, check=True)
    svc = people
    row = suggest(svc)["suggestion"]
    svc.handle("suggestion.start", {"suggestion_id": row["suggestion_id"]}, PERSON)
    claimed = suggest(svc)["suggestion"]
    svc.suggestions.claim(claimed["suggestion_id"], by="fixture",
                          conversation=svc._start_fields(svc.suggestions.get(claimed["suggestion_id"]), {}))
    root = svc.root
    svc.close()
    script = ("import json,sys\nfrom subfleet.conversations.store import ConversationStore,SCHEMA_VERSION\n"
              "s=ConversationStore(sys.argv[1])\n"
              "print(json.dumps({'schema': SCHEMA_VERSION, 'conversations': len(s.query('SELECT * FROM conversations')),"
              "'messages': len(s.query('SELECT * FROM messages')), 'suggestions': len(s.query('SELECT * FROM suggestions')),"
              "'changes': len(s.changes_after(0)['changes'])}))\ns.close()\n")
    result = subprocess.run([sys.executable, "-c", script, str(root)], cwd=base, capture_output=True, text=True,
                            timeout=60, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(base)})
    assert result.returncode == 0, result.stderr
    opened = json.loads(result.stdout)
    assert opened["schema"] == 2 and opened["messages"] == 1 and opened["suggestions"] == 2
    assert opened["conversations"] == 2 and opened["changes"] > 0


# --- C-25.7 re-checked against the code ---------------------------------------------


def test_r1b_the_daemon_retries_a_claimed_start_after_2_4_8_seconds_at_most_every_5_minutes(people, monkeypatch):
    svc = people
    row = interrupted_claim(svc, monkeypatch, workspace=people.test_workspace)

    def fail(**kwargs):
        raise OSError(5, "still failing")
    monkeypatch.setattr(svc.store, "submit_message", fail)
    for tries, wait in ((1, 2.0), (2, 4.0), (3, 8.0), (8, 256.0), (9, 300.0)):
        svc._resume_start(row["suggestion_id"], tries, 1000.0)
        assert svc._resuming[row["suggestion_id"]] == (tries, 1000.0 + wait)
