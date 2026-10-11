"""Suggested conversations, the daemon's half (C-25.7, C-25.6, C-25.4, C-25.5, C-29.9).

The real conversation service and stores over a fake daemon (`test_conversation_service`'s):
an agent's `suggestion.create`, from inside a turn and from outside one; the
person-only `suggestion.start` and `suggestion.dismiss`; the creator's
`suggestion.withdraw`; `suggestion.list`; the feed and event rows each change
writes; and a start cut short, finished by a retry and by the daemon.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest

from subfleet import descriptors, protocol
from subfleet.conversations import service as service_module
from subfleet.conversations import suggestions
from subfleet.conversations.peers import Verdict
from subfleet.conversations.service import CAPABILITIES, FILE_OPS, LIMITS, PERSON_ONLY, ConversationService
from subfleet.conversations.store import ConversationError
from tests.unit.test_conversation_service import SETTINGS, conversation, git, repo, svc  # noqa: F401

PERSON, AGENT = 1, 2
BRIEF = "Port the importer to the new manifest.\n\nRead docs/plan.md first; run the importer tests."


@pytest.fixture
def people(svc):
    """The peer check (C-25.6) as the daemon makes it, by pid: PERSON is the app or a
    terminal, any other pid a process Subfleet launched."""
    def person(peer, what):
        if peer == PERSON:
            return Verdict(True, "the Subfleet app", peer)
        raise ConversationError("person-only", f"{what} is a person's decision: the caller runs under a Subfleet "
                                "guardian", code=7, fix="answer it in the Subfleet app")
    svc._person = person
    return svc


def turn_of(svc, cid: str) -> tuple[str, str]:
    """A running turn of `cid` with its job as the dispatcher names one (`turn-<conversation>`,
    request `turn:<message>:<n>`): (job id, message id)."""
    last = svc.store.one("SELECT message_id FROM messages WHERE conversation_id=? AND origin='person' "
                         "ORDER BY seq DESC LIMIT 1", (cid,))
    mid = svc.op_message_submit({"conversation_id": cid, "message_id": str(uuid.uuid4()), "text": "work on it",
                                 "after_message_id": last["message_id"] if last else None}, None)["message_id"]
    job = f"20261011-000000-turn-{mid[:8]}"
    svc.daemon.store.add_job(job_id=job, request_id=f"turn:{mid}:0", payload_digest="d", kind="turn",
                             workdir=svc.test_workspace, prompt_path="p.md", sandbox="read-only", name=f"turn-{cid}",
                             in_place=1, max_attempts=1, state="running")
    svc.store.set_state(mid, "running")
    return job, mid


def suggest(svc, *, peer=AGENT, **args) -> dict:
    body = {"request_id": str(uuid.uuid4()), "title": "Port the importer", "tldr": "Out of scope here; worth its own session",
            "brief": BRIEF, **args}
    return svc.handle("suggestion.create", body, peer)


def refused(reason: str, code: int, call, *args, **kwargs) -> ConversationError:
    with pytest.raises(ConversationError) as caught:
        call(*args, **kwargs)
    assert (caught.value.reason, caught.value.code) == (reason, code), caught.value
    return caught.value


def changes_since(svc, after: int) -> list[dict]:
    return [c for c in svc.store.changes_after(after, limit=1000)["changes"] if c.get("suggestion_id")]


# --- the ops are part of the protocol (C-25.1, C-25.2, C-25.6, C-16.7) -------------


def test_c25_7_the_ops_are_advertised_classified_and_placed():
    ops = ("suggestion.create", "suggestion.list", "suggestion.start", "suggestion.dismiss", "suggestion.withdraw")
    assert set(ops) <= set(protocol.CONVERSATION_OPS) and set(ops) <= set(protocol.OPS)
    assert suggestions.CAPABILITY in CAPABILITIES and set(suggestions.LIMITS.items()) <= set(LIMITS.items())
    # A person's: start and dismiss; an agent's: create and withdraw; anyone's: list.
    assert {"suggestion.start", "suggestion.dismiss"} <= PERSON_ONLY
    assert not {"suggestion.create", "suggestion.withdraw", "suggestion.list"} & PERSON_ONLY
    # Create and start check folders and run git on the file pool (C-25.3).
    assert {"suggestion.create", "suggestion.start"} <= FILE_OPS
    assert descriptors.read_only("suggestion.list", {})
    for op in ("suggestion.create", "suggestion.start", "suggestion.dismiss", "suggestion.withdraw"):
        assert not descriptors.read_only(op, {})


def test_c25_7_capabilities_name_the_limits(svc):
    caps = svc.handle("capabilities", {}, None)
    assert "suggestions.v1" in caps["capabilities"]
    assert caps["limits"]["suggestion_title_chars"] == 60 and caps["limits"]["suggestion_tldr_chars"] == 300
    assert caps["limits"]["suggestion_brief_bytes"] == 32 * 1024
    assert caps["limits"]["suggestions_per_origin"] == 20 and caps["limits"]["suggestions_open"] == 200
    # An added op is a capability, not a new shape version (C-25.1).
    assert caps["conversation_schema"] == 1


# --- create: who suggests, and where it shows (C-25.7, C-26.13) --------------------


def test_c25_7_a_turn_suggests_into_its_own_conversation_and_its_events(svc):
    cid = conversation(svc)
    job, mid = turn_of(svc, cid)
    before = svc.store.changes_after(0)["next"]
    out = suggest(svc, calling_job=job, session_id="sess-of-the-turn", workspace=svc.test_workspace)
    row = out["suggestion"]
    assert out["created"] is True and out["scrubbed"] == 0
    assert row["state"] == "open" and row["origin_conversation_id"] == cid and row["origin_message_id"] == mid
    assert row["brief"] == BRIEF and row["brief_truncated"] is False and row["provider"] == "claude"
    # C-29.9: one feed row, under the origin conversation, carrying the suggestion.
    fed = changes_since(svc, before)
    assert [(c["conversation_id"], c["suggestion_id"], c["suggestion"]["state"]) for c in fed] == [
        (cid, row["suggestion_id"], "open")]
    assert fed[0]["message_id"] is None and fed[0]["state"] is None
    # C-25.4: and one event in that conversation, on the turn's message, with the whole brief.
    events = [e for e in svc.store.events_after(cid, 0)["events"] if e["kind"] == "suggestion"]
    assert len(events) == 1 and events[0]["message_id"] == mid
    assert events[0]["data"]["brief"] == BRIEF and events[0]["data"]["state"] == "open"


def test_c25_7_outside_a_turn_a_session_suggests_with_no_origin(svc):
    before = svc.store.changes_after(0)["next"]
    out = suggest(svc, session_id="Desktop-Session-1")
    row = out["suggestion"]
    assert row["origin_conversation_id"] is None and row["origin_message_id"] is None
    assert row["creator_session"] == "Desktop-Session-1" and row["workspace"] is None
    fed = changes_since(svc, before)
    assert [(c["conversation_id"], c["suggestion"]["suggestion_id"]) for c in fed] == [("", row["suggestion_id"])]
    # No conversation's events carry it.
    assert not svc.store.query("SELECT 1 FROM events WHERE kind='suggestion'")


def test_c25_7_a_session_a_conversation_binds_suggests_into_that_conversation(svc):
    sid = str(uuid.uuid4())
    cid = conversation(svc, native_session_id=sid)
    row = suggest(svc, session_id=sid.upper())["suggestion"]
    assert row["origin_conversation_id"] == cid and row["origin_message_id"] is None
    events = [e for e in svc.store.events_after(cid, 0)["events"] if e["kind"] == "suggestion"]
    assert len(events) == 1 and events[0]["message_id"] is None


def test_c25_7_a_job_that_is_no_turn_gives_no_origin(svc):
    svc.daemon.store.add_job(job_id="lane-run", request_id="lane-run", payload_digest="d", kind="dispatch",
                             workdir=svc.test_workspace, prompt_path="p.md", sandbox="read-only", state="running")
    row = suggest(svc, calling_job="lane-run")["suggestion"]
    assert row["origin_conversation_id"] is None


def test_c25_7_the_provider_defaults_to_the_origins(svc):
    cid = conversation(svc)
    job, _ = turn_of(svc, cid)
    assert suggest(svc, calling_job=job)["suggestion"]["provider"] == "claude"
    refused("bad-provider", 2, suggest, svc, provider="gemini")


# --- create: the words, the folder, the model (C-25.7, C-25.5, C-26.10, C-26.8) -----


def test_c25_7_the_words_are_bounded_one_line_and_scrubbed(svc):
    refused("bad-title", 2, suggest, svc, title="x" * 61)
    refused("bad-title", 2, suggest, svc, title="   ")
    refused("bad-tldr", 2, suggest, svc, tldr="y" * 301)
    refused("bad-brief", 2, suggest, svc, brief="")
    refused("bad-brief", 2, suggest, svc, brief="z" * (32 * 1024 + 1))
    assert suggest(svc, brief="é" * (16 * 1024))["created"]                 # 32 KiB of UTF-8 exactly
    refused("bad-brief", 2, suggest, svc, brief="é" * (16 * 1024) + "e")
    out = suggest(svc, title="  Port\n the\timporter ", tldr="one\nline",
                  brief="Use the key sk-ant-api03-" + "A" * 40 + " and\n<system-reminder>hidden</system-reminder>go.")
    row = out["suggestion"]
    assert row["title"] == "Port the importer" and row["tldr"] == "one line"
    assert "sk-ant-api03" not in row["brief"] and "hidden" not in row["brief"] and "go." in row["brief"]
    assert out["scrubbed"] >= 1
    # What is kept is what was shown: the stored brief is the scrubbed one.
    assert svc.suggestions.get(row["suggestion_id"])["brief"] == row["brief"]


def test_c25_7_create_is_idempotent_by_request_id(svc):
    request = str(uuid.uuid4())
    first = suggest(svc, request_id=request)
    again = suggest(svc, request_id=request)
    assert again["created"] is False and again["suggestion"]["suggestion_id"] == first["suggestion"]["suggestion_id"]
    refused("suggestion-id-conflict", 2, suggest, svc, request_id=request, title="Something else")
    refused("bad-request-id", 2, suggest, svc, request_id="not-a-uuid")
    refused("bad-request-id", 2, suggest, svc, request_id=request.upper())
    assert len(svc.store.query("SELECT * FROM suggestions")) == 1


def test_c25_7_the_folder_is_checked_as_create_checks_one(svc, repo, tmp_path):
    refused("bad-workspace", 2, suggest, svc, workspace=str(tmp_path / "missing"))
    git(repo, "checkout", "-q", "-b", "main")
    # In place on main, a writable start would be refused (C-26.10): so is the suggestion.
    error = refused("protected-branch", 7, suggest, svc, workspace=str(repo))
    assert "main" in str(error)
    # In a worktree of its own it is not on main.
    row = suggest(svc, workspace=str(repo), worktree=True)["suggestion"]
    assert row["worktree"] is True and Path(row["workspace"]) == repo.resolve()
    refused("bad-worktree", 2, suggest, svc, workspace=str(repo), worktree="yes")
    # A refused workspace makes nothing.
    assert len(svc.store.query("SELECT * FROM suggestions")) == 1


def test_c25_7_the_model_is_one_the_fleet_routes(svc):
    refused("unknown-model", 2, suggest, svc, model="gpt-9")
    assert suggest(svc, model="claude-opus-5-5")["suggestion"]["model"] == "claude-opus-5-5"
    # A policy short name becomes the value models.list names for it.
    assert suggest(svc, model="opus")["suggestion"]["model"] == "claude-opus-5-5"
    assert suggest(svc, model="opus[1m]")["suggestion"]["model"] == "opus[1m]"
    refused("unknown-model", 2, suggest, svc, provider="codex", model="opus")
    refused("bad-model", 2, suggest, svc, model="m" * 81)


def test_c25_7_open_suggestions_are_bounded_per_origin_and_in_all(svc, monkeypatch):
    first, second = conversation(svc), conversation(svc)
    job_a, _ = turn_of(svc, first)
    job_b, _ = turn_of(svc, second)
    for _ in range(20):
        suggest(svc, calling_job=job_a)
    error = refused("too-many-suggestions", 7, suggest, svc, calling_job=job_a)
    assert "this conversation has 20" in str(error) and error.fix
    for _ in range(20):
        suggest(svc, session_id="one-session")
    refused("too-many-suggestions", 7, suggest, svc, session_id="one-session")
    for _ in range(20):
        suggest(svc)                                     # no session at all: one bucket of their own
    refused("too-many-suggestions", 7, suggest, svc)
    # Another origin still has room, until the global bound.
    monkeypatch.setattr(suggestions, "OPEN_MAX", 65)
    for _ in range(5):
        suggest(svc, calling_job=job_b)
    error = refused("too-many-suggestions", 7, suggest, svc, calling_job=job_b)
    assert "65 suggestions are open" in str(error)
    # Closing one frees its place.
    oldest = svc.store.one("SELECT suggestion_id FROM suggestions WHERE origin_conversation_id=? ORDER BY created_at",
                           (first,))["suggestion_id"]
    svc.handle("suggestion.withdraw", {"suggestion_id": oldest, "calling_job": job_a}, AGENT)
    assert suggest(svc, calling_job=job_b)["created"]


# --- start, dismiss and withdraw (C-25.6, C-25.7) -----------------------------------


def test_c25_6_only_a_person_starts_or_dismisses(people):
    svc = people
    sid = suggest(svc, workspace=svc.test_workspace)["suggestion"]["suggestion_id"]
    before = svc.store.changes_after(0)["next"]
    refused("person-only", 7, svc.handle, "suggestion.start", {"suggestion_id": sid}, AGENT)
    refused("person-only", 7, svc.handle, "suggestion.dismiss", {"suggestion_id": sid}, AGENT)
    refused("person-only", 7, svc.handle, "suggestion.start", {"suggestion_id": sid}, None)
    assert svc.suggestions.get(sid)["state"] == "open" and svc.store.changes_after(before)["changes"] == []
    assert not svc.store.query("SELECT 1 FROM conversations WHERE request_id=?", (suggestions.start_request_id(sid),))


def test_c25_7_a_start_makes_one_conversation_whose_first_message_is_the_brief(people):
    svc = people
    origin = conversation(svc)
    job, _ = turn_of(svc, origin)
    row = suggest(svc, calling_job=job, workspace=svc.test_workspace)["suggestion"]
    sid = row["suggestion_id"]
    notified = svc.daemon.notified
    out = svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)
    started, view, receipt = out["suggestion"], out["conversation"], out["message"]
    assert out["created"] is True and started["state"] == "started" and started["decided_by"] == "the Subfleet app"
    assert started["started_conversation_id"] == view["conversation_id"] != origin
    assert view["workspace"] == svc.test_workspace and view["workspace_kind"] == "in-place"
    assert view["title"] == row["title"] and view["origin"] == "new"
    assert view["settings"]["permission"] == "ask" and view["settings"]["model"] == "claude-opus-5-5"
    # message.submit semantics: a canonical UUID derived from the suggestion, the
    # text published under the conversation, a receipt, intent before any job.
    assert receipt["message_id"] == started["started_message_id"] == suggestions.start_message_id(sid)
    assert receipt["text"] == BRIEF and receipt["state"] == "queued" and receipt["origin"] == "person"
    messages = svc.store.messages(view["conversation_id"])
    assert len(messages) == 1 and svc.store.message_text(messages[0]) == BRIEF
    assert messages[0]["job_id"] is None and svc.daemon.notified > notified
    # Its first turn receives the brief.
    svc._dispatch()
    turn = svc.daemon.submits[-1]
    assert turn.request_id == f"turn:{receipt['message_id']}:0" and Path(turn.prompt_path).read_text() == BRIEF
    # A retry, a second click: the same conversation and message.
    again = svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)
    assert again["created"] is False and again["conversation"]["conversation_id"] == view["conversation_id"]
    assert len(svc.store.query("SELECT * FROM conversations WHERE request_id=?",
                               (suggestions.start_request_id(sid),))) == 1
    assert len(svc.store.messages(view["conversation_id"])) == 1


def test_c25_7_a_start_in_a_checkout_makes_a_worktree_conversation(people, repo):
    svc = people
    sid = suggest(svc, workspace=str(repo / "pkg"), worktree=True)["suggestion"]["suggestion_id"]
    view = svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)["conversation"]
    cid = view["conversation_id"]
    assert view["workspace_kind"] == "worktree" and view["worktree"]["branch"] == f"subfleet/{cid}"
    assert view["worktree"]["repository"] == str(repo.resolve())
    # The same subdirectory of the new worktree, as create does.
    assert view["workspace"] == str(Path(view["worktree"]["path"]) / "pkg")
    assert git(Path(view["worktree"]["path"]), "symbolic-ref", "--short", "HEAD") == f"subfleet/{cid}"
    assert svc.store.message_text(svc.store.messages(cid)[0]) == BRIEF


def test_c25_7_a_worktree_suggestion_outside_git_starts_in_place(people):
    svc = people
    sid = suggest(svc, workspace=svc.test_workspace, worktree=True)["suggestion"]["suggestion_id"]
    assert svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)["conversation"]["workspace_kind"] == "in-place"


def test_c25_7_no_folder_starts_in_a_private_scratch_folder(people):
    svc = people
    sid = suggest(svc)["suggestion"]["suggestion_id"]
    view = svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)["conversation"]
    assert Path(view["workspace"]).parent == svc.root / "conversations" / "workspaces"
    assert (Path(view["workspace"]).stat().st_mode & 0o777) == 0o700


def test_c25_7_the_person_may_widen_the_start_with_confirmation(people):
    svc = people
    sid = suggest(svc, workspace=svc.test_workspace)["suggestion"]["suggestion_id"]
    refused("confirm-widen", 2, svc.handle, "suggestion.start", {"suggestion_id": sid, "permission": "bypass"}, PERSON)
    assert svc.suggestions.get(sid)["state"] == "open"
    view = svc.handle("suggestion.start", {"suggestion_id": sid, "permission": "bypass", "confirm_widen": True},
                      PERSON)["conversation"]
    assert view["settings"]["permission"] == "bypass"


def test_c25_7_a_codex_suggestion_starts_read_only_while_codex_is(people):
    svc = people
    svc.daemon.policy["models"]["astra"] = {"provider": "codex", "id": "gpt-6-astra"}
    sid = suggest(svc, provider="codex", workspace=svc.test_workspace)["suggestion"]["suggestion_id"]
    refused("codex-read-only", 7, svc.handle, "suggestion.start", {"suggestion_id": sid, "permission": "ask"}, PERSON)
    view = svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)["conversation"]
    assert view["provider"] == "codex" and view["settings"]["permission"] == "read-only"
    assert view["settings"]["model"] == "gpt-6-astra"


def test_c25_7_a_dismissed_or_withdrawn_suggestion_never_starts(people):
    svc = people
    cid = conversation(svc)
    job, _ = turn_of(svc, cid)
    dismissed = suggest(svc, calling_job=job)["suggestion"]["suggestion_id"]
    withdrawn = suggest(svc, calling_job=job)["suggestion"]["suggestion_id"]
    assert svc.handle("suggestion.dismiss", {"suggestion_id": dismissed}, PERSON)["changed"] is True
    assert svc.handle("suggestion.withdraw", {"suggestion_id": withdrawn, "calling_job": job}, AGENT)["changed"]
    for sid, state in ((dismissed, "dismissed"), (withdrawn, "withdrawn")):
        error = refused("not-open", 7, svc.handle, "suggestion.start", {"suggestion_id": sid}, PERSON)
        assert state in str(error)
        assert not svc.store.query("SELECT 1 FROM conversations WHERE request_id=?",
                                   (suggestions.start_request_id(sid),))
    # Closing again changes nothing.
    assert svc.handle("suggestion.dismiss", {"suggestion_id": dismissed}, PERSON)["changed"] is False
    assert svc.handle("suggestion.dismiss", {"suggestion_id": withdrawn}, PERSON)["suggestion"]["state"] == "withdrawn"


def test_c25_7_only_its_creator_withdraws_it_and_only_while_open(people):
    svc = people
    mine, other = conversation(svc), conversation(svc)
    job, _ = turn_of(svc, mine)
    later_job, _ = turn_of(svc, mine)
    other_job, _ = turn_of(svc, other)
    from_turn = suggest(svc, calling_job=job)["suggestion"]["suggestion_id"]
    from_session = suggest(svc, session_id="sess-a")["suggestion"]["suggestion_id"]
    refused("not-creator", 7, svc.handle, "suggestion.withdraw", {"suggestion_id": from_turn, "calling_job": other_job},
            AGENT)
    refused("not-creator", 7, svc.handle, "suggestion.withdraw", {"suggestion_id": from_session, "session_id": "sess-b"},
            AGENT)
    refused("not-creator", 7, svc.handle, "suggestion.withdraw", {"suggestion_id": from_session}, PERSON)
    # A later turn of the same conversation is its creator too.
    assert svc.handle("suggestion.withdraw", {"suggestion_id": from_turn, "calling_job": later_job},
                      AGENT)["suggestion"]["decided_by"] == "creator"
    svc.handle("suggestion.start", {"suggestion_id": from_session}, PERSON)
    refused("too-late", 7, svc.handle, "suggestion.withdraw", {"suggestion_id": from_session, "session_id": "sess-a"},
            AGENT)
    refused("too-late", 7, svc.handle, "suggestion.dismiss", {"suggestion_id": from_session}, PERSON)
    refused("unknown-suggestion", 2, svc.handle, "suggestion.withdraw", {"suggestion_id": "sg-none", "session_id": "x"},
            AGENT)


# --- a start cut short (C-25.7 with C-26.10's repeatable create) ---------------------


def test_c25_7_a_start_cut_short_keeps_its_claim_and_is_finished_by_a_retry(people, monkeypatch):
    svc = people
    sid = suggest(svc, workspace=svc.test_workspace)["suggestion"]["suggestion_id"]
    real = svc.store.submit_message

    def crash(**kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(svc.store, "submit_message", crash)
    with pytest.raises(OSError):
        svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)
    row = svc.suggestions.get(sid)
    assert row["state"] == "starting" and row["start_error"].startswith("OSError")
    cid = row["started_conversation_id"]
    assert svc.store.conversation(cid) and svc.store.messages(cid) == []
    # Claimed: nothing may dismiss or withdraw it now.
    refused("too-late", 7, svc.handle, "suggestion.dismiss", {"suggestion_id": sid}, PERSON)
    monkeypatch.setattr(svc.store, "submit_message", real)
    out = svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)
    assert out["conversation"]["conversation_id"] == cid and out["suggestion"]["start_error"] is None
    assert [svc.store.message_text(m) for m in svc.store.messages(cid)] == [BRIEF]


def test_c25_7_the_daemon_finishes_a_claimed_start_after_a_restart(people, monkeypatch):
    svc = people
    sid = suggest(svc, workspace=svc.test_workspace)["suggestion"]["suggestion_id"]
    conversation_fields = svc._start_fields(svc.suggestions.get(sid), {})
    svc.suggestions.claim(sid, by="the Subfleet app", conversation=conversation_fields)   # then the daemon stopped
    svc.close()
    daemon = svc.daemon
    after = ConversationService(daemon)
    try:
        monkeypatch.setattr(service_module, "SUGGESTION_RESUME_AFTER_S", 0.0)
        after._resume_suggestions()
        deadline = time.monotonic() + 60
        while (after._resuming.get(sid, (0, None))[1] is None) and time.monotonic() < deadline:
            time.sleep(0.05)                    # the file pool's finishing of it
        row = after.suggestions.get(sid)
        assert row["state"] == "started" and row["decided_by"] == "the Subfleet app"
        assert after.store.message_text(after.store.messages(row["started_conversation_id"])[0]) == BRIEF
        # Done once: another tick finds nothing to finish.
        after._resume_suggestions()
        assert after._resuming == {}
    finally:
        after.close()


def test_c25_7_a_retry_while_the_message_was_accepted_records_started(people):
    """A start cut short after the message's transaction: no such state exists
    (`started` commits with the message), but a store written by hand to it is
    still finished, not given a second message."""
    svc = people
    sid = suggest(svc, workspace=svc.test_workspace)["suggestion"]["suggestion_id"]
    row, took = svc.suggestions.claim(sid, by="the Subfleet app", conversation=svc._start_fields(svc.suggestions.get(sid), {}))
    assert took
    svc.store.submit_message(conversation_id=row["started_conversation_id"], message_id=suggestions.start_message_id(sid),
                             after_message_id=None, text=row["brief"], attachments=[], settings=row["start_settings"])
    out = svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)
    assert out["suggestion"]["state"] == "started" and out["created"] is False
    assert len(svc.store.messages(row["started_conversation_id"])) == 1


# --- list and the feed (C-25.7, C-29.9, C-25.5) --------------------------------------


def test_c25_7_list_shows_open_newest_first_then_recently_closed(people):
    svc = people
    first = suggest(svc, session_id="s")["suggestion"]["suggestion_id"]
    second = suggest(svc, session_id="s")["suggestion"]["suggestion_id"]
    third = suggest(svc, session_id="s", brief="word " * 1000)["suggestion"]["suggestion_id"]
    svc.handle("suggestion.dismiss", {"suggestion_id": first}, PERSON)
    listed = svc.handle("suggestion.list", {}, AGENT)
    assert [r["suggestion_id"] for r in listed["suggestions"]] == [third, second, first]
    assert listed["open"] == 2 and listed["limits"] == suggestions.LIMITS
    long = listed["suggestions"][0]
    assert long["brief_truncated"] is True and len(long["brief"]) == 2000 and long["brief_chars"] == 5000
    full = svc.handle("suggestion.list", {"suggestion_ids": [third]}, AGENT)["suggestions"]
    assert full[0]["brief"] == "word " * 1000 and full[0]["brief_truncated"] is False
    assert svc.handle("suggestion.list", {"closed_limit": 0}, AGENT)["suggestions"][-1]["suggestion_id"] == second
    refused("bad-args", 2, svc.handle, "suggestion.list", {"suggestion_ids": "x"}, AGENT)


def test_c25_7_a_conversations_list_is_its_own(people):
    svc = people
    cid = conversation(svc)
    job, _ = turn_of(svc, cid)
    mine = suggest(svc, calling_job=job)["suggestion"]["suggestion_id"]
    suggest(svc, session_id="elsewhere")
    listed = svc.handle("suggestion.list", {"conversation_id": cid}, None)
    assert [r["suggestion_id"] for r in listed["suggestions"]] == [mine] and listed["open"] == 1


def test_c25_7_every_change_reaches_the_feed_and_the_origins_events(people):
    svc = people
    cid = conversation(svc)
    job, mid = turn_of(svc, cid)
    before = svc.store.changes_after(0)["next"]
    sid = suggest(svc, calling_job=job, workspace=svc.test_workspace)["suggestion"]["suggestion_id"]
    started = svc.handle("suggestion.start", {"suggestion_id": sid}, PERSON)["conversation"]["conversation_id"]
    fed = svc.store.changes_after(before, limit=1000)["changes"]
    # Three suggestion rows under the origin (open, starting, started), and the new
    # conversation's own rows (its creation, its first message).
    assert [c["conversation_id"] for c in fed if c.get("suggestion_id")] == [cid, cid, cid]
    assert {c["conversation_id"] for c in fed if not c.get("suggestion_id")} == {started}
    events = [e for e in svc.store.events_after(cid, 0)["events"] if e["kind"] == "suggestion"]
    assert [e["data"]["state"] for e in events] == ["open", "starting", "started"]
    assert all(e["message_id"] == mid for e in events)
    assert events[-1]["data"]["started_conversation_id"] == started
    # The views hold what a person may see, never the request id or digest.
    for data in [e["data"] for e in events] + [c["suggestion"] for c in fed if c.get("suggestion")]:
        assert not {"request_id", "digest", "start_settings", "creator_job"} & set(data)


def test_c25_7_an_old_store_gains_the_table_and_column_without_a_schema_step(tmp_path):
    """The table is additive, as the wake tables are: the store stays schema 2, so a
    build that predates it still opens the store."""
    import sqlite3
    from subfleet.conversations.store import ConversationStore
    root = tmp_path / "state"
    store = ConversationStore(root)
    store.close()
    db = sqlite3.connect(root / "conversations.sqlite3")
    db.execute("DROP TABLE suggestions")
    db.execute("ALTER TABLE changes DROP COLUMN suggestion_id")
    db.commit()
    db.close()
    store = ConversationStore(root)
    try:
        assert store.one("SELECT MAX(version) v FROM schema_version")["v"] == 2
        assert {r["name"] for r in store.query("PRAGMA table_info(changes)")} >= {"suggestion_id"}
        assert store.query("SELECT * FROM suggestions") == []
    finally:
        store.close()


def test_c25_7_an_event_too_large_for_its_row_carries_the_preview(people):
    svc = people
    cid = conversation(svc)
    job, _ = turn_of(svc, cid)
    brief = '"' * (32 * 1024)                       # each one escaped: over the 64 KiB row
    suggest(svc, calling_job=job, brief=brief)
    data = [e for e in svc.store.events_after(cid, 0)["events"] if e["kind"] == "suggestion"][0]["data"]
    assert data["brief_truncated"] is True and len(data["brief"]) == suggestions.PREVIEW_CHARS
    assert len(json.dumps(data, ensure_ascii=False).encode()) < 64 * 1024
