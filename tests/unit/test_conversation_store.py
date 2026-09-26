"""C-24.1 to C-24.5, C-25.4, C-26.6, C-27.1, C-28.2: the conversation store."""

from __future__ import annotations

import os
import stat
import threading
import uuid

import pytest

from subfleet.conversations.store import (
    SCHEMA_VERSION, ConversationError, ConversationStore, message_digest, validate_settings, widens,
)

SETTINGS = {"model": "opus", "effort": "high", "fast": False, "permission": "ask"}


@pytest.fixture
def store(tmp_path):
    s = ConversationStore(tmp_path / "state")
    yield s
    s.close()


def conv(store, **kw):
    base = dict(provider="claude", workspace="/w", workspace_kind="in-place", settings=SETTINGS, origin="new")
    base.update(kw)
    return store.create_conversation(**base)[0]


def mid():
    return str(uuid.uuid4())


def test_files_are_private_and_the_job_store_is_untouched(store, tmp_path):
    """C-24.1, C-2.3: conversations.sqlite3 is 0600 and state.sqlite3 is not created."""
    assert stat.S_IMODE(os.stat(tmp_path / "state" / "conversations.sqlite3").st_mode) == 0o600
    assert not (tmp_path / "state" / "state.sqlite3").exists()


def test_a_message_is_accepted_once_and_conflicts_change_nothing(store):
    """C-24.2 same id and digest returns the stored receipt; other content is refused."""
    c = conv(store)
    m = mid()
    first, created = store.submit_message(conversation_id=c["conversation_id"], message_id=m, after_message_id=None,
                                          text="hello", attachments=[], settings=SETTINGS)
    again, created_again = store.submit_message(conversation_id=c["conversation_id"], message_id=m,
                                                after_message_id=None, text="hello", attachments=[], settings=SETTINGS)
    assert created and not created_again and first == again
    with pytest.raises(ConversationError) as err:
        store.submit_message(conversation_id=c["conversation_id"], message_id=m, after_message_id=None,
                             text="HELLO", attachments=[], settings=SETTINGS)
    assert err.value.reason == "message-id-conflict"
    assert store.message_text(store.message(m)) == "hello"


def test_messages_are_accepted_in_client_order(store):
    """C-24.2 a message whose predecessor is not committed is refused and changes nothing."""
    c = conv(store)
    m1, m2 = mid(), mid()
    with pytest.raises(ConversationError) as err:
        store.submit_message(conversation_id=c["conversation_id"], message_id=m2, after_message_id=m1,
                             text="second", attachments=[], settings=SETTINGS)
    assert err.value.reason == "out-of-order"
    store.submit_message(conversation_id=c["conversation_id"], message_id=m1, after_message_id=None,
                         text="first", attachments=[], settings=SETTINGS)
    second, _ = store.submit_message(conversation_id=c["conversation_id"], message_id=m2, after_message_id=m1,
                                     text="second", attachments=[], settings=SETTINGS)
    assert second["seq"] == 2


def test_text_is_published_before_the_row_and_is_private(store):
    """C-24.3 the text file exists (0600) once the receipt is returned."""
    c = conv(store)
    m, _ = store.submit_message(conversation_id=c["conversation_id"], message_id=mid(), after_message_id=None,
                                text="kept", attachments=[], settings=SETTINGS)
    assert stat.S_IMODE(os.stat(m["text_path"]).st_mode) == 0o600
    assert store.message_text(m) == "kept"


def test_concurrent_duplicate_submits_commit_once(store):
    """C-24.2 parallel retries of one id create one message."""
    c = conv(store)
    m = mid()
    results = []

    def go():
        results.append(store.submit_message(conversation_id=c["conversation_id"], message_id=m,
                                            after_message_id=None, text="x", attachments=[], settings=SETTINGS)[1])
    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1
    assert len(store.messages(c["conversation_id"])) == 1


def test_settings_are_validated_and_widening_is_detected():
    """C-24.2, C-25.6: settings have a fixed shape; a wider permission needs a person."""
    with pytest.raises(ConversationError):
        validate_settings("claude", {"model": "opus", "permission": "everything"})
    base = validate_settings("claude", SETTINGS)
    assert widens(base, {**base, "permission": "bypass"}) and not widens(base, {**base, "permission": "read-only"})
    assert message_digest("c", "t", [], base) != message_digest("c", "t", [], {**base, "fast": True})


def test_one_turn_at_a_time_per_conversation(store):
    """C-24.5 only the lowest queued message of an idle, unblocked conversation dispatches."""
    a, b = conv(store), conv(store, native_session_id="s-b", origin="native")
    m1, m2, m3 = mid(), mid(), mid()
    store.submit_message(conversation_id=a["conversation_id"], message_id=m1, after_message_id=None, text="1",
                         attachments=[], settings=SETTINGS)
    store.submit_message(conversation_id=a["conversation_id"], message_id=m2, after_message_id=m1, text="2",
                         attachments=[], settings=SETTINGS)
    store.submit_message(conversation_id=b["conversation_id"], message_id=m3, after_message_id=None, text="3",
                         attachments=[], settings=SETTINGS)
    assert {m["message_id"] for m in store.next_dispatchable()} == {m1, m3}
    store.set_state(m1, "running")
    assert {m["message_id"] for m in store.next_dispatchable()} == {m3}
    store.update_conversation(b["conversation_id"], blocked_by="unfinished-turn")
    assert store.next_dispatchable() == []
    store.set_state(m1, "complete")
    assert [m["message_id"] for m in store.next_dispatchable()] == [m2]


def test_guarded_state_moves(store):
    """C-24.7 a guarded move changes a message only from the expected states."""
    c = conv(store)
    m = mid()
    store.submit_message(conversation_id=c["conversation_id"], message_id=m, after_message_id=None, text="x",
                         attachments=[], settings=SETTINGS)
    assert store.set_state(m, "cancelled", expect=("queued", "waiting"))
    assert not store.set_state(m, "running", expect=("queued", "waiting"))
    assert store.message(m)["state"] == "cancelled"


def test_event_batches_are_idempotent_and_watermarked(store):
    """C-26.6, C-25.4: replaying a batch stores nothing twice; the watermark only rises."""
    c = conv(store)
    batch = [("stdout", "100", 1, "text", {"text": "a"}), ("stdout", "100", 2, "tool.started", {"id": "t"}),
             ("command", "approval:r1", 1, "approval.resolved", {"decision": "allow"})]
    kw = dict(conversation_id=c["conversation_id"], message_id="m", attempt_id="j/a1")
    assert store.append_events(events=batch, stdout_offset=200, stdin_seq=3, **kw) == 3
    assert store.append_events(events=batch, stdout_offset=150, stdin_seq=2, **kw) == 0
    mark = store.mark("j/a1")
    assert (mark["stdout_offset"], mark["stdin_seq"]) == (200, 3)
    page = store.events_after(c["conversation_id"], 0)
    assert [e["kind"] for e in page["events"]] == ["text", "tool.started", "approval.resolved"]


def test_compaction_sets_a_floor_that_resets_stale_cursors(store):
    """C-25.4, IR-6: deltas are removed after the turn; `reset` is true exactly when the
    cursor is below the floor, the highest sequence number compaction removed."""
    c = conv(store)
    kw = dict(conversation_id=c["conversation_id"], message_id="m", attempt_id="j/a1")
    store.append_events(events=[("stdout", str(i), 1, "text.delta", {"text": str(i)}) for i in range(5)]
                        + [("stdout", "9", 1, "text", {"text": "01234"}),
                           ("stdout", "10", 1, "turn.completed", {})], stdout_offset=10, stdin_seq=1, **kw)
    seqs = [e["seq"] for e in store.events_after(c["conversation_id"], 0)["events"]]
    last_delta, text, completed = seqs[4], seqs[5], seqs[6]
    assert store.events_after(c["conversation_id"], 0)["reset"] is False       # nothing removed yet
    assert store.compact("j/a1") == 5
    assert store.floor(c["conversation_id"]) == last_delta
    for cursor in (0, seqs[0], seqs[3], last_delta - 1):
        page = store.events_after(c["conversation_id"], cursor)
        assert page["reset"] is True and page["floor"] == last_delta, cursor
    for cursor in (last_delta, text, completed):
        assert store.events_after(c["conversation_id"], cursor)["reset"] is False, cursor
    # A client that read every delta but not the final text is not sent back.
    assert [e["kind"] for e in store.events_after(c["conversation_id"], last_delta)["events"]] == ["text", "turn.completed"]
    assert [e["kind"] for e in store.events_after(c["conversation_id"], 0)["events"]] == ["text", "turn.completed"]


def test_the_floor_only_rises_and_an_attempt_without_deltas_moves_nothing(store):
    """C-25.4: a second compaction never lowers the floor; an attempt with no deltas is
    marked compacted and leaves the floor where it was."""
    c = conv(store)
    cid = c["conversation_id"]
    store.append_events(conversation_id=cid, message_id="m1", attempt_id="j1/a1", stdout_offset=1, stdin_seq=1,
                        events=[("stdout", "1", 1, "thinking.delta", {"text": "a"})])
    store.append_events(conversation_id=cid, message_id="m2", attempt_id="j2/a1", stdout_offset=1, stdin_seq=1,
                        events=[("stdout", "1", 1, "text.delta", {"text": "b"}), ("stdout", "2", 1, "text", {"text": "b"})])
    store.append_events(conversation_id=cid, message_id="m3", attempt_id="j3/a1", stdout_offset=1, stdin_seq=1,
                        events=[("stdout", "1", 1, "text", {"text": "c"})])
    assert store.compact("j2/a1") == 1
    high = store.floor(cid)
    assert store.compact("j1/a1") == 1
    assert store.floor(cid) == high
    assert store.compact("j3/a1") == 0
    assert store.floor(cid) == high and store.mark("j3/a1")["compacted"] == 1


def test_a_replay_after_compaction_does_not_bring_deltas_back(store):
    """C-26.6, C-25.4: a runner replaying a compacted attempt's stdout stores no delta
    again, so nothing appears above the floor that a client could mistake for new."""
    c = conv(store)
    kw = dict(conversation_id=c["conversation_id"], message_id="m", attempt_id="j/a1")
    batch = [("stdout", "1", 1, "text.delta", {"text": "x"}), ("stdout", "2", 1, "text", {"text": "x"})]
    store.append_events(events=batch, stdout_offset=5, stdin_seq=1, **kw)
    store.compact("j/a1")
    assert store.append_events(events=batch, stdout_offset=5, stdin_seq=1, **kw) == 0
    assert [e["kind"] for e in store.events_after(c["conversation_id"], 0)["events"]] == ["text"]


def test_compactable_lists_settled_messages_attempts_once(store):
    """IR-6: only attempts whose message is terminal, settled before the cutoff, and not
    yet compacted are candidates; the job store's word on the attempt is the caller's."""
    c = conv(store)
    cid = c["conversation_id"]
    done, live = mid(), mid()
    store.submit_message(conversation_id=cid, message_id=done, after_message_id=None, text="1", attachments=[],
                         settings=SETTINGS)
    store.submit_message(conversation_id=cid, message_id=live, after_message_id=done, text="2", attachments=[],
                         settings=SETTINGS)
    for message, attempt in ((done, "a/a1"), (live, "b/a1")):
        store.append_events(conversation_id=cid, message_id=message, attempt_id=attempt, stdout_offset=1,
                            stdin_seq=1, events=[("stdout", "1", 1, "text.delta", {"text": "x"})])
    store.set_state(done, "complete")
    store.set_state(live, "running")
    later = "9999-01-01T00:00:00.000Z"
    assert [r["attempt_id"] for r in store.compactable(settled_before=later, limit=10)] == ["a/a1"]
    assert store.compactable(settled_before="2000-01-01T00:00:00.000Z", limit=10) == []
    store.compact("a/a1")
    assert store.compactable(settled_before=later, limit=10) == []


def test_a_worktree_is_recorded_through_update_conversation(store):
    """C-24.1, D-16: the workspace and the worktree record change through the store's
    update, which writes the change feed; unknown fields are still refused."""
    c = conv(store, workspace_kind="worktree")
    start = store.changes_after(0)["next"]
    record = {"path": "/state/worktrees/conversation-x", "branch": "subfleet/x", "source": "/w", "base": "abc"}
    after = store.update_conversation(c["conversation_id"], workspace=record["path"], worktree=record)
    assert after["workspace"] == record["path"] and after["worktree"] == record
    assert store.conversation(c["conversation_id"])["worktree"] == record
    assert [ch["conversation_id"] for ch in store.changes_after(start)["changes"]] == [c["conversation_id"]]
    with pytest.raises(ValueError):
        store.update_conversation(c["conversation_id"], workspace="")
    with pytest.raises(ValueError):
        store.update_conversation(c["conversation_id"], provider="codex")


def test_an_earlier_conversation_store_gains_the_worktree_column(tmp_path):
    """C-24.1: a store written before `worktree_json` existed opens, keeps its schema (an
    added column needs no numbered step), and reads its conversations with no worktree."""
    import sqlite3
    root = tmp_path / "state"
    first = ConversationStore(root)
    c = conv(first)
    first.close()
    db = sqlite3.connect(root / "conversations.sqlite3")
    db.execute("ALTER TABLE conversations DROP COLUMN worktree_json")
    db.commit()
    db.close()
    again = ConversationStore(root)
    try:
        assert again.conversation(c["conversation_id"])["worktree"] is None
        assert again.one("SELECT MAX(version) v FROM schema_version")["v"] == SCHEMA_VERSION
    finally:
        again.close()


def test_an_unbound_move_refuses_a_message_a_job_already_carries(store):
    """IR-2: a withdrawal guarded by `unbound` loses to a dispatcher that bound a job first."""
    c = conv(store)
    m = mid()
    store.submit_message(conversation_id=c["conversation_id"], message_id=m, after_message_id=None, text="x",
                         attachments=[], settings=SETTINGS)
    store.set_state(m, "waiting", expect=("queued",), job_id="job-1")
    assert not store.set_state(m, "cancelled", expect=("queued", "waiting"), unbound=True)
    assert store.message(m)["state"] == "waiting"


def test_page_size_is_bounded(store):
    """C-25.4 a page stops at 256 KiB."""
    c = conv(store)
    big = "x" * 60_000
    store.append_events(conversation_id=c["conversation_id"], message_id="m", attempt_id="j/a1",
                        events=[("stdout", str(i), 1, "text", {"text": big}) for i in range(10)],
                        stdout_offset=1, stdin_seq=0)
    page = store.events_after(c["conversation_id"], 0)
    assert 1 <= len(page["events"]) < 10


def test_approvals_are_recorded_once_with_the_exact_request(store):
    """C-27.1 one row per provider request; the exact request is kept 0600 with its hash."""
    c = conv(store)
    m = mid()
    store.submit_message(conversation_id=c["conversation_id"], message_id=m, after_message_id=None, text="x",
                         attachments=[], settings=SETTINGS)
    kw = dict(message_id=m, conversation_id=c["conversation_id"], attempt_id="j/a1", provider_request_id="r1",
              kind="tool", request={"input": {"command": "rm -rf build"}}, display={"tool": "Bash"},
              options=("allow", "deny"))
    first, created = store.add_approval(**kw)
    again, created_again = store.add_approval(**kw)
    assert created and not created_again and first["approval_id"] == again["approval_id"]
    assert stat.S_IMODE(os.stat(first["request_path"]).st_mode) == 0o600
    assert len(first["nonce"]) == 32
    assert store.answer_approval(first["approval_id"], {"decision": "allow"})
    assert not store.answer_approval(first["approval_id"], {"decision": "deny"})
    assert store.approval(first["approval_id"])["decision"] == {"decision": "allow"}


def test_attachments_must_exist_and_submit_touches_last_use(store):
    """C-28.2 a message names known attachments only, and submitting refreshes their last use."""
    c = conv(store)
    sha = "a" * 64
    with pytest.raises(ConversationError) as err:
        store.submit_message(conversation_id=c["conversation_id"], message_id=mid(), after_message_id=None,
                             text="x", attachments=[sha], settings=SETTINGS)
    assert err.value.reason == "unknown-attachment"
    store.add_attachment(sha, "image/png", 10, "/p")
    before = store.attachment(sha)["last_used_at"]
    store.submit_message(conversation_id=c["conversation_id"], message_id=mid(), after_message_id=None,
                         text="x", attachments=[sha], settings=SETTINGS)
    assert store.attachment(sha)["last_used_at"] >= before


def test_change_feed_reports_state_and_pending_approvals(store):
    """C-29.9 the watch feed carries each state change and the pending-approval count."""
    c = conv(store)
    start = store.changes_after(0)["next"]
    m = mid()
    store.submit_message(conversation_id=c["conversation_id"], message_id=m, after_message_id=None, text="x",
                         attachments=[], settings=SETTINGS)
    store.set_state(m, "running")
    store.add_approval(message_id=m, conversation_id=c["conversation_id"], attempt_id="a", provider_request_id="r",
                       kind="tool", request={}, display={}, options=("allow",))
    changes = store.changes_after(start)["changes"]
    assert [ch["state"] for ch in changes] == ["queued", "running", None]
    assert changes[-1]["pending_approvals"] == 1


def test_native_sessions_map_to_one_conversation(store):
    """C-24.1 a provider's native session has at most one conversation."""
    a, created = store.create_conversation(provider="claude", workspace="/w", workspace_kind="in-place",
                                           settings=SETTINGS, origin="native", native_session_id="sid")
    b, created_b = store.create_conversation(provider="claude", workspace="/w", workspace_kind="in-place",
                                             settings=SETTINGS, origin="native", native_session_id="sid")
    assert created and not created_b and a["conversation_id"] == b["conversation_id"]


def test_a_withdrawal_tombstone_is_born_cancelled_and_never_dispatchable(store):
    """C-24.7, IR-7: the tombstone for an id the daemon never received is cancelled in the
    transaction that creates it, so no dispatch pass can see it queued; the change feed
    reports it cancelled. Only queued and terminal states may be written at acceptance."""
    c = conv(store)
    ghost = mid()
    message, created = store.submit_message(conversation_id=c["conversation_id"], message_id=ghost,
                                            after_message_id=None, text="(withdrawn)", attachments=[],
                                            settings=SETTINGS, origin="tombstone", state="cancelled",
                                            state_reason="withdrawn-before-receipt")
    assert created and (message["state"], message["state_reason"]) == ("cancelled", "withdrawn-before-receipt")
    assert store.next_dispatchable() == []
    assert [(r["message_id"], r["state"]) for r in store.changes_after(0)["changes"] if r["message_id"]] == [
        (ghost, "cancelled")]
    with pytest.raises(ValueError):
        store.submit_message(conversation_id=c["conversation_id"], message_id=mid(), after_message_id=None,
                             text="x", attachments=[], settings=SETTINGS, state="running")


def test_a_schema_1_store_is_migrated_to_2_in_place(tmp_path):
    """C-26.14, C-3.1: a store written by schema 1 gains `turn_trees` on open, records
    the step, keeps its rows, and opening it again changes nothing."""
    import sqlite3
    from subfleet.conversations import store as store_module
    root = tmp_path / "state"
    s = ConversationStore(root)
    c = conv(s)
    s.close()
    db = sqlite3.connect(root / "conversations.sqlite3")
    # What schema 1 wrote: no `turn_trees`, and one version row.
    db.executescript("DROP TABLE turn_trees; DELETE FROM schema_version; "
                     "INSERT INTO schema_version VALUES (1, '2026-09-24T00:00:00Z');")
    db.close()
    s = ConversationStore(root)
    assert [r["version"] for r in s.query("SELECT version FROM schema_version ORDER BY version")] == [1, 2]
    assert s.conversation(c["conversation_id"])["workspace"] == "/w"
    assert {r["name"] for r in s.query("PRAGMA table_info(turn_trees)")} >= {"start_tree", "end_tree", "head_after"}
    s.close()
    s = ConversationStore(root)
    assert [r["version"] for r in s.query("SELECT version FROM schema_version ORDER BY version")] == [1, 2]
    s.close()
    assert store_module.SCHEMA_VERSION == 2


def test_a_newer_store_is_refused(tmp_path):
    """C-26.14: a build never opens a store written by a newer schema."""
    import sqlite3
    root = tmp_path / "state"
    ConversationStore(root).close()
    db = sqlite3.connect(root / "conversations.sqlite3")
    db.execute("INSERT INTO schema_version VALUES (3, 'later')")
    db.commit()
    db.close()
    with pytest.raises(ConversationError) as err:
        ConversationStore(root)
    assert err.value.reason == "schema"


def test_turn_trees_are_recorded_once_per_attempt_and_the_latest_attempt_is_the_turn(store):
    """C-26.14: the start is written once and the end fills in; a replay changes
    nothing; the end is one change-feed row; a re-admitted message's latest attempt is
    its turn; the conversation's base is its first writable turn's start."""
    c = conv(store)
    m = mid()
    store.submit_message(conversation_id=c["conversation_id"], message_id=m, after_message_id=None, text="x",
                         attachments=[], settings=SETTINGS)
    base = dict(message_id=m, conversation_id=c["conversation_id"], workspace="/w", writable=True)
    store.record_trees(attempt_id="j1/a1", started_at="2026-09-24T10:00:00Z", head_before="h0", start_tree="t0",
                       **base)
    store.record_trees(attempt_id="j1/a1", started_at="2026-09-24T10:00:00Z", head_before="other",
                       start_tree="other", **base)
    assert store.turn_trees(m)["start_tree"] == "t0" and store.turn_trees(m)["ended_at"] is None
    feed = store.changes_after(0)["next"]
    for _ in range(2):
        store.record_trees(attempt_id="j1/a1", started_at="2026-09-24T10:00:00Z", head_before="h0",
                           start_tree="t0", head_after="h1", end_tree="t1", ended=True, **base)
    row = store.turn_trees(m)
    assert (row["head_before"], row["start_tree"], row["head_after"], row["end_tree"]) == ("h0", "t0", "h1", "t1")
    assert row["writable"] is True and row["ended_at"]
    assert [(ch["message_id"], ch["state"]) for ch in store.changes_after(feed)["changes"]] == [(m, None)]
    # A later attempt of the same message (a re-admission) is the turn.
    store.record_trees(attempt_id="j2/a1", started_at="2026-09-24T10:00:05Z", head_before="h1", start_tree="t2",
                       **base)
    assert store.turn_trees(m)["attempt_id"] == "j2/a1"
    assert store.first_trees(c["conversation_id"])["attempt_id"] == "j1/a1"
    read_only = conv(store, workspace="/r")
    r = mid()
    store.submit_message(conversation_id=read_only["conversation_id"], message_id=r, after_message_id=None,
                         text="x", attachments=[], settings=SETTINGS)
    store.record_trees(attempt_id="j3/a1", message_id=r, conversation_id=read_only["conversation_id"],
                       workspace="/r", writable=False, started_at="2026-09-24T10:00:00Z", head_before="h")
    assert store.turn_trees(r)["writable"] is False
    assert store.first_trees(read_only["conversation_id"]) is None


def test_a_message_s_latest_attempt_is_the_one_recorded_last(store):
    """C-26.14, C-24.5: attempts are ordered as they were recorded, not by `started_at`
    (whole seconds) or the attempt id: a re-admitted message's second job reserved in
    the same second has the id `<stamp>-<slug>-1`, which sorts below the first's once
    `/a1` follows, and a clock stepped back gives the later attempt the earlier time."""
    c = conv(store)
    m = mid()
    store.submit_message(conversation_id=c["conversation_id"], message_id=m, after_message_id=None, text="x",
                         attachments=[], settings=SETTINGS)
    base = dict(message_id=m, conversation_id=c["conversation_id"], workspace="/w", writable=True)
    first, second = "20260924-100000-turn-10fa90399504/a1", "20260924-100000-turn-10fa90399504-1/a1"
    assert sorted([first, second])[-1] == first           # the tie-break the ids would give is the wrong one
    store.record_trees(attempt_id=first, started_at="2026-09-24T10:00:00Z", start_tree="t1", **base)
    store.record_trees(attempt_id=first, started_at="2026-09-24T10:00:00Z", start_tree="t1", end_tree="e1",
                       ended=True, **base)
    store.record_trees(attempt_id=second, started_at="2026-09-24T10:00:00Z", start_tree="t2", **base)
    assert store.turn_trees(m)["attempt_id"] == second
    assert store.first_trees(c["conversation_id"])["attempt_id"] == first
    # The end of the earlier attempt filling in again (a replayed finalization) moves nothing.
    store.record_trees(attempt_id=first, started_at="2026-09-24T10:00:00Z", start_tree="t1", end_tree="e1",
                       ended=True, **base)
    assert store.turn_trees(m)["attempt_id"] == second
    third = "20260924-095959-turn-10fa90399504/a1"
    store.record_trees(attempt_id=third, started_at="2026-09-24T09:59:59Z", start_tree="t3", **base)
    assert store.turn_trees(m)["attempt_id"] == third
    assert store.first_trees(c["conversation_id"])["attempt_id"] == first
# --- legacy history (C-30.4) ---------------------------------------------------

LEGACY_SETTINGS = {"model": None, "effort": None, "fast": None, "permission": None, "auto_continue": None,
                   "service_tier": None}


def history(store, c, message_id, text="from the cockpit", state="complete", **kw):
    base = dict(conversation_id=c["conversation_id"], message_id=message_id, text=text, state=state,
                state_reason="legacy-finished", settings=LEGACY_SETTINGS, created_at="2026-08-31T19:34:55.437Z",
                updated_at="2026-08-31T19:37:55.484Z", turn_ref=message_id)
    base.update(kw)
    return store.insert_legacy_history(**base)


def test_legacy_history_is_terminal_and_never_dispatched(store):
    """C-30.4, C-24.5: a history row is terminal, has no job or predecessor, and
    no dispatch path selects it; a state that is not terminal is refused."""
    c = conv(store, origin="legacy", native_session_id="5e551011-0000-4000-8000-00000000000a")
    first, second = mid(), mid()
    row, created = history(store, c, first)
    history(store, c, second, state="failed", state_reason="legacy-error: provider-failed")
    assert created and row["origin"] == "legacy" and row["state"] == "complete" and row["seq"] == 1
    assert row["job_id"] is None and row["after_message_id"] is None and row["turn_seq"] == 0
    assert row["turn_ref"] == first and row["attachments"] == [] and row["settings"] == LEGACY_SETTINGS
    assert row["created_at"] == "2026-08-31T19:34:55.437Z"
    assert store.message_text(row) == "from the cockpit"
    assert stat.S_IMODE(os.stat(row["text_path"]).st_mode) == 0o600
    assert store.next_dispatchable() == [] and store.live_messages() == []
    for state in ("queued", "waiting", "starting", "running", "approval-needed", "delivery-unknown"):
        with pytest.raises(ConversationError) as err:
            history(store, c, mid(), state=state)
        assert err.value.reason == "not-terminal"
    assert [m["message_id"] for m in store.messages(c["conversation_id"])] == [first, second]


def test_legacy_history_is_idempotent_by_id_and_never_rewritten(store):
    """C-30.4 repeated imports create nothing new; C-24.2 an id is never reused
    for other content or in another conversation."""
    c = conv(store, origin="legacy", native_session_id="s1")
    other = conv(store, origin="legacy", native_session_id="s2")
    m = mid()
    first, created = history(store, c, m)
    again, created_again = history(store, c, m)
    assert created and not created_again and first == again
    with pytest.raises(ConversationError) as err:
        history(store, c, m, text="other text")
    assert err.value.reason == "message-id-conflict"
    with pytest.raises(ConversationError) as err:
        history(store, other, m)
    assert err.value.reason == "message-id-conflict"
    person = mid()
    store.submit_message(conversation_id=other["conversation_id"], message_id=person, after_message_id=None,
                         text="mine", attachments=[], settings=SETTINGS)
    with pytest.raises(ConversationError) as err:
        store.insert_legacy_history(conversation_id=other["conversation_id"], message_id=person, text="mine",
                                    state="complete", state_reason=None, settings=LEGACY_SETTINGS,
                                    created_at="2026-08-31T00:00:00.000Z", updated_at="2026-08-31T00:00:00.000Z")
    assert err.value.reason == "message-id-conflict"
    assert store.message_text(store.message(m)) == "from the cockpit"


def test_legacy_history_goes_first_and_the_conversation_continues_after_it(store):
    """C-30.4, C-24.2, C-30.2: history never lands after a conversation's own
    messages; after it, the conversation takes a person's message as its first
    (no predecessor), and only that message is dispatchable."""
    c = conv(store, origin="legacy", native_session_id="s1")
    old = mid()
    history(store, c, old)
    new = mid()
    store.submit_message(conversation_id=c["conversation_id"], message_id=new, after_message_id=None,
                         text="continue here", attachments=[], settings=SETTINGS)
    assert [m["message_id"] for m in store.next_dispatchable()] == [new]
    with pytest.raises(ConversationError) as err:
        history(store, c, mid())
    assert err.value.reason == "history-after-messages"
    assert [(m["seq"], m["origin"]) for m in store.messages(c["conversation_id"])] == [(1, "legacy"), (2, "person")]
    assert store.message(old)["state"] == "complete"


def test_history_is_rechecked_inside_its_transaction(store, monkeypatch):
    """C-30.4, C-24.2 (review L6): the check that history goes first is made
    again inside the writing transaction. A person's message that lands after
    the first check (a concurrent writer) refuses the history row, which is
    never written after it."""
    c = conv(store, origin="legacy", native_session_id="s1")
    person, old = mid(), mid()
    real = store.one

    def interleaved(sql, params=()):
        if sql.startswith("SELECT 1 FROM messages WHERE conversation_id=? AND origin<>'legacy'"):
            found = real(sql, params)                 # the first check sees nothing yet...
            store.submit_message(conversation_id=c["conversation_id"], message_id=person, after_message_id=None,
                                 text="sent meanwhile", attachments=[], settings=SETTINGS)
            return found                              # ...and a person's message lands right after it
        return real(sql, params)
    monkeypatch.setattr(store, "one", interleaved)
    with pytest.raises(ConversationError) as err:
        history(store, c, old)
    assert err.value.reason == "history-after-messages"
    monkeypatch.setattr(store, "one", real)
    assert [(m["message_id"], m["origin"]) for m in store.messages(c["conversation_id"])] == [(person, "person")]
