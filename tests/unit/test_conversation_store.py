"""C-24.1 to C-24.5, C-25.4, C-26.6, C-27.1, C-28.2: the conversation store."""

from __future__ import annotations

import os
import stat
import threading
import uuid

import pytest

from subfleet.conversations.store import (
    ConversationError, ConversationStore, message_digest, validate_settings, widens,
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
    """C-25.4 deltas are removed after the turn; a client behind the floor is told to reload."""
    c = conv(store)
    kw = dict(conversation_id=c["conversation_id"], message_id="m", attempt_id="j/a1")
    store.append_events(events=[("stdout", str(i), 1, "text.delta", {"text": str(i)}) for i in range(5)]
                        + [("stdout", "9", 1, "text", {"text": "01234"})], stdout_offset=10, stdin_seq=1, **kw)
    first = store.events_after(c["conversation_id"], 0)["events"]
    assert store.compact("j/a1") == 5
    assert store.events_after(c["conversation_id"], first[1]["seq"])["reset"] is True
    assert store.events_after(c["conversation_id"], 0)["reset"] is False
    assert [e["kind"] for e in store.events_after(c["conversation_id"], 0)["events"]] == ["text"]


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
