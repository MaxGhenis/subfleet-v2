"""Archiving a conversation, the daemon's half (C-25.2, C-25.6, C-24.5, C-30.1).

The real conversation service and stores over the fake daemon of
`test_conversation_service`: `conversation.archive` sets and clears
`archived_at`, is a person's, and is refused while a message of the
conversation is open; `conversation.list` leaves archived conversations out
unless `include_archived`; a message is never accepted queued into an archived
conversation, checked in the same transaction either way round; and the catalog
never lists an archived conversation's session as a session of its own.

Invariants (stated in the PR and checked by the property at the end):
  - archiving and unarchiving never change a message or an event;
  - archive then unarchive gives back the conversation row exactly as it was;
  - an archived conversation never holds a message that is not terminal;
  - `conversation.list` shows a conversation iff it is not archived, or
    `include_archived` is true.
"""

from __future__ import annotations

import json
import uuid

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import descriptors, protocol
from subfleet.conversations.service import ARCHIVE_CAPABILITY, CAPABILITIES, FILE_OPS, PERSON_ONLY
from subfleet.conversations.store import ConversationError
from subfleet.conversations.turn import LIVE_STATES, TERMINAL_STATES
from tests.unit.test_conversation_service import SETTINGS, conversation, submit, svc, write_catalog  # noqa: F401
from tests.unit.test_suggestions import AGENT, PERSON, people, refused, suggest  # noqa: F401


def archive(svc, cid: str, archived: bool = True, *, peer=PERSON) -> dict:
    return svc.handle("conversation.archive", {"conversation_id": cid, "archived": archived}, peer)["conversation"]


def listed(svc, **args) -> list[str]:
    return [c["conversation_id"] for c in svc.handle("conversation.list", {"include_catalog": False, **args},
                                                     None)["conversations"]]


def row(svc, cid: str) -> dict:
    return svc.store.one("SELECT * FROM conversations WHERE conversation_id=?", (cid,))


def messages_and_events(svc, cid: str) -> tuple[list[dict], list[dict]]:
    return (svc.store.query("SELECT * FROM messages WHERE conversation_id=? ORDER BY seq", (cid,)),
            svc.store.query("SELECT * FROM events WHERE conversation_id=? ORDER BY seq", (cid,)))


def feed_rows(svc, after: int, cid: str) -> list[dict]:
    return [c for c in svc.store.changes_after(after, limit=1000)["changes"] if c["conversation_id"] == cid]


def settle(svc, mid: str, state: str = "complete") -> None:
    svc.store.set_state(mid, state)


# --- the op is part of the protocol (C-25.1, C-25.2, C-25.6, C-16.7) ---------------


def test_c25_2_the_archive_op_is_advertised_classified_and_a_persons():
    assert "conversation.archive" in protocol.CONVERSATION_OPS and "conversation.archive" in protocol.OPS
    assert ARCHIVE_CAPABILITY == "archive.v1" and ARCHIVE_CAPABILITY in CAPABILITIES
    assert "conversation.archive" in PERSON_ONLY
    # A write a caller relies on, answered on the request pool (no git, no files).
    assert not descriptors.read_only("conversation.archive", {"conversation_id": "c1"})
    assert "conversation.archive" not in FILE_OPS
    request = protocol.decode_request(json.dumps({"v": 1, "id": "r", "op": "conversation.archive",
                                                  "args": {"conversation_id": "c1"}}))
    assert request.op == "conversation.archive"


def test_c25_1_capabilities_name_archive_without_a_new_schema(svc):
    caps = svc.handle("capabilities", {}, None)
    assert "archive.v1" in caps["capabilities"] and caps["conversation_schema"] == 1


# --- archive and unarchive ----------------------------------------------------------


def test_archive_hides_a_conversation_and_unarchive_shows_it_again(people):
    svc = people
    kept, gone = conversation(svc), conversation(svc)
    settle(svc, submit(svc, gone))
    before = row(svc, gone)
    start = svc.store.changes_after(0)["next"]
    view = archive(svc, gone)
    assert view["conversation_id"] == gone and view["archived_at"] is not None
    assert row(svc, gone)["updated_at"] == before["updated_at"]          # archiving is not activity
    assert listed(svc) == [kept]
    assert set(listed(svc, include_archived=True)) == {kept, gone}
    shown = {c["conversation_id"]: c for c in svc.handle("conversation.list", {"include_catalog": False,
                                                                               "include_archived": True}, None)
             ["conversations"]}
    assert shown[gone]["archived_at"] == view["archived_at"] and shown[kept]["archived_at"] is None
    # An archived conversation still opens, and says it is archived.
    assert svc.handle("conversation.open", {"conversation_id": gone}, None)["conversation"]["archived_at"]
    # One feed row, so a watching client learns of it (C-29.9).
    assert [(c["message_id"], c["state"]) for c in feed_rows(svc, start, gone)] == [(None, None)]

    back = archive(svc, gone, False)
    assert back["archived_at"] is None and row(svc, gone) == before
    assert set(listed(svc)) == {kept, gone}
    assert len(feed_rows(svc, start, gone)) == 2


def test_archive_is_idempotent_and_keeps_the_first_time(people):
    svc = people
    cid = conversation(svc)
    first = archive(svc, cid)["archived_at"]
    start = svc.store.changes_after(0)["next"]
    assert archive(svc, cid)["archived_at"] == first
    assert feed_rows(svc, start, cid) == []                              # nothing changed, nothing fed
    archive(svc, cid, False)
    start = svc.store.changes_after(0)["next"]
    assert archive(svc, cid, False)["archived_at"] is None
    assert feed_rows(svc, start, cid) == []


def test_archived_defaults_to_true_and_must_be_a_boolean(people):
    svc = people
    cid = conversation(svc)
    assert svc.handle("conversation.archive", {"conversation_id": cid}, PERSON)["conversation"]["archived_at"]
    for bad in ("false", 0, 1, None):
        refused("bad-args", 2, svc.handle, "conversation.archive", {"conversation_id": cid, "archived": bad}, PERSON)
    refused("unknown-conversation", 2, svc.handle, "conversation.archive", {"conversation_id": "cv-nope"}, PERSON)


def test_c25_6_an_agent_may_not_archive_or_unarchive(people):
    svc = people
    cid = conversation(svc)
    refused("person-only", 7, archive, svc, cid, peer=AGENT)
    assert row(svc, cid)["archived_at"] is None
    archive(svc, cid)
    refused("person-only", 7, archive, svc, cid, False, peer=AGENT)
    assert row(svc, cid)["archived_at"] is not None


@pytest.mark.parametrize("state", ["queued", *LIVE_STATES])
def test_archive_is_refused_while_a_message_is_open(people, state):
    """Nothing dispatches in an archived conversation and no wake goes into one (C-24.5),
    so an open message there would never settle: archiving waits for it."""
    svc = people
    cid = conversation(svc)
    settle(svc, submit(svc, cid))
    mid = submit(svc, cid, after=svc.store.messages(cid)[-1]["message_id"])
    if state != "queued":
        svc.store.set_state(mid, state)
    held = messages_and_events(svc, cid)
    error = refused("conversation-active", 7, archive, svc, cid)
    assert state in str(error) and error.fix
    assert row(svc, cid)["archived_at"] is None and messages_and_events(svc, cid) == held
    settle(svc, mid, "cancelled" if state == "queued" else "complete")
    assert archive(svc, cid)["archived_at"]


def test_a_blocked_conversation_can_be_archived_and_stays_blocked(people):
    """A stopped turn's block waits on the person; archiving it is the person's choice
    and leaves the block for when it comes back."""
    svc = people
    cid = conversation(svc)
    settle(svc, submit(svc, cid), "interrupted")
    svc.store.update_conversation(cid, blocked_by="unfinished-turn")
    archive(svc, cid)
    assert archive(svc, cid, False)["blocked_by"] == "unfinished-turn"


def test_archive_is_refused_while_a_handoff_of_it_runs(people):
    svc = people
    cid = conversation(svc)
    with svc._lock:
        svc._handing_off.add(cid)
    refused("handoff-running", 7, archive, svc, cid)
    with svc._lock:
        svc._handing_off.discard(cid)
    # The fence a handoff leaves while it may still put the source's messages back.
    assert svc.store.fence(cid, "handoff:req-1")
    refused("handoff-running", 7, archive, svc, cid)
    assert row(svc, cid)["archived_at"] is None


def test_archive_is_refused_while_a_suggestion_is_starting_the_conversation(people):
    """C-25.7: the brief is still to come; archiving now would leave Start nowhere to put it."""
    svc = people
    sid = suggest(svc, workspace=svc.test_workspace)["suggestion"]["suggestion_id"]
    claimed, took = svc.suggestions.claim(sid, by="the Subfleet app",
                                          conversation=svc._start_fields(svc.suggestions.get(sid), {}))
    assert took
    refused("conversation-starting", 7, archive, svc, claimed["started_conversation_id"])


# --- no message waits for ever in an archived conversation -------------------------


def test_a_message_sent_to_an_archived_conversation_is_refused(people):
    svc = people
    cid = conversation(svc)
    archive(svc, cid)
    error = refused("archived", 7, svc.handle, "message.submit",
                    {"conversation_id": cid, "message_id": str(uuid.uuid4()), "text": "hi"}, PERSON)
    assert error.fix and svc.store.messages(cid) == []


def test_an_archive_between_the_submit_s_look_and_its_transaction_still_refuses_it(people, monkeypatch):
    """The race the store closes: `message.submit` read the conversation unarchived,
    then a person archived it before the message's transaction. Either the archive
    sees the queued message and is refused, or the submit sees the archive."""
    svc = people
    cid = conversation(svc)
    real = svc.store.submit_message

    def archived_first(**kwargs):
        archive(svc, cid)
        return real(**kwargs)
    monkeypatch.setattr(svc.store, "submit_message", archived_first)
    refused("archived", 7, svc.handle, "message.submit",
            {"conversation_id": cid, "message_id": str(uuid.uuid4()), "text": "hi"}, PERSON)
    assert svc.store.messages(cid) == []


def test_a_tombstone_is_still_written_into_an_archived_conversation(people):
    """A withdrawal of a message the daemon never had is born settled (IR-7): it must
    still be recorded, so the withdrawn id can never be accepted later."""
    svc = people
    cid = conversation(svc)
    archive(svc, cid)
    mid = str(uuid.uuid4())
    receipt = svc.handle("message.cancel", {"message_id": mid, "conversation_id": cid}, PERSON)
    assert receipt["state"] == "cancelled" and svc.store.message(mid)["state"] == "cancelled"


def test_an_archived_conversation_is_never_dispatched_and_unarchive_wakes_the_loop(people):
    svc = people
    cid = conversation(svc)
    archive(svc, cid)
    svc._dispatch()
    assert svc.daemon.submits == []
    notified = svc.daemon.notified
    archive(svc, cid, False)
    assert svc.daemon.notified == notified + 1


# --- the catalog (C-30.1) -----------------------------------------------------------


def test_an_archived_conversations_session_is_not_listed_as_a_session_of_its_own(people):
    """`conversation.list` left the catalog's copy of a session out only for the
    conversations it listed; an archived one's session came back as a native row,
    opening which reopened the archived conversation."""
    svc = people
    session = str(uuid.uuid4())
    cid = conversation(svc, origin="native", native_session_id=session)
    other = str(uuid.uuid4())
    write_catalog(svc.root, "2026-10-11T12:00:00.000Z", [
        {"provider": "claude", "native_session_id": session, "cwd": svc.test_workspace, "mtime": 2.0},
        {"provider": "claude", "native_session_id": other, "cwd": svc.test_workspace, "mtime": 1.0}])

    def natives(**args):
        out = svc.handle("conversation.list", args, None)
        return [c["conversation_id"] for c in out["conversations"]], [i["native_session_id"] for i in out["catalog"]["items"]]

    assert natives() == ([cid], [other])
    archive(svc, cid)
    assert natives() == ([], [other])
    assert natives(include_archived=True) == ([cid], [other])


# --- the invariants, for any interleaving -------------------------------------------


OPERATIONS = st.lists(st.sampled_from(["archive", "unarchive", "submit", "settle", "run", "list"]), min_size=1,
                      max_size=14)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(steps=OPERATIONS, others=st.integers(0, 2))
def test_property_archive_touches_no_message_or_event_and_round_trips(people, steps, others):
    svc = people
    cid = conversation(svc)
    bystanders = [conversation(svc) for _ in range(others)]
    for step in steps:
        before_row = row(svc, cid)
        before = messages_and_events(svc, cid)
        if step in ("archive", "unarchive"):
            try:
                archive(svc, cid, step == "archive")
            except ConversationError as exc:
                assert (exc.reason, exc.code) == ("conversation-active", 7) and step == "archive"
                assert row(svc, cid) == before_row
            # Never a message or an event, accepted or refused.
            assert messages_and_events(svc, cid) == before
        elif step == "submit":
            last = svc.store.one("SELECT message_id FROM messages WHERE conversation_id=? AND origin='person' "
                                 "ORDER BY seq DESC LIMIT 1", (cid,))
            try:
                svc.handle("message.submit", {"conversation_id": cid, "message_id": str(uuid.uuid4()), "text": "go",
                                              "after_message_id": last["message_id"] if last else None}, PERSON)
            except ConversationError as exc:
                assert exc.reason == "archived" and before_row["archived_at"] is not None
                assert messages_and_events(svc, cid) == before
        elif step in ("settle", "run"):
            open_ = [m for m in svc.store.messages(cid) if m["state"] not in TERMINAL_STATES]
            if open_:
                svc.store.set_state(open_[0]["message_id"], "complete" if step == "settle" else "running")
        current = row(svc, cid)
        # No archived conversation holds an open message.
        if current["archived_at"] is not None:
            assert all(m["state"] in TERMINAL_STATES for m in svc.store.messages(cid))
        # The list shows it iff it is not archived, or archived ones are asked for.
        assert (cid in listed(svc)) == (current["archived_at"] is None)
        assert cid in listed(svc, include_archived=True)
        assert set(bystanders) <= set(listed(svc))
    # Archive then unarchive gives the row back as it was, whenever archiving is allowed.
    for m in svc.store.messages(cid):
        if m["state"] not in TERMINAL_STATES:
            svc.store.set_state(m["message_id"], "cancelled")
    archive(svc, cid, False)
    restored = row(svc, cid)
    held = messages_and_events(svc, cid)
    archive(svc, cid)
    archive(svc, cid, False)
    assert row(svc, cid) == restored and messages_and_events(svc, cid) == held
