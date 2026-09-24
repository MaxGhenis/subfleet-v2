"""C-29.6, D-26 (review IR-18): the conversation summary `status.json` carries.

`conversations.store.status_summary` is read on the timer thread that publishes
`status.json`, through a read-only connection of its own.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid

import pytest

from subfleet.conversations.store import STATUS_TITLE_CHARS, ConversationStore, status_summary

SETTINGS = {"model": "opus", "effort": None, "fast": False, "permission": "ask"}


@pytest.fixture
def store(tmp_path):
    s = ConversationStore(tmp_path)
    yield s
    s.close()


def conv(store, title="t", provider="claude", **kw):
    return store.create_conversation(provider=provider, workspace="/w", workspace_kind="in-place",
                                     settings=SETTINGS, origin="new", title=title, **kw)[0]["conversation_id"]


def send(store, cid, state=None, after=None):
    message_id = str(uuid.uuid4())
    store.submit_message(conversation_id=cid, message_id=message_id, after_message_id=after, text="hi",
                         attachments=[], settings=SETTINGS)
    if state:
        assert store.set_state(message_id, state)
    return message_id


def approval(store, cid, message_id, request_id="r1"):
    store.add_approval(message_id=message_id, conversation_id=cid, attempt_id=f"job/{message_id}/a1",
                       provider_request_id=request_id, kind="tool", request={"tool": "Bash"},
                       display={"tool": "Bash"}, options=("allow", "deny"))


def test_c29_6_no_store_file_is_no_conversations_and_creates_nothing(tmp_path):
    """C-29.6, C-3.4 a daemon with no conversations yet publishes zeros it observed; the read writes nothing."""
    assert status_summary(tmp_path) == {"available": True, "items": [], "truncated": False,
                                        "counts": {"active": 0, "needs_approval": 0, "blocked": 0}}
    assert list(tmp_path.iterdir()) == []


def test_c29_6_counts_and_list_by_attention(store, tmp_path):
    """C-29.6, D-26 active, needing approval and blocked conversations are counted and listed:
    approvals first, then blocked, then the rest newest first; idle and archived ones are not."""
    idle = conv(store, "idle")
    send(store, idle, "complete")
    queued = conv(store, "queued")
    send(store, queued)
    running = conv(store, "running")
    send(store, running, "running")
    blocked = conv(store, "blocked")
    store.update_conversation(blocked, blocked_by="unfinished-turn")
    asking = conv(store, "asking", provider="codex")
    approval(store, asking, send(store, asking, "approval-needed"))
    archived = conv(store, "archived")
    approval(store, archived, send(store, archived, "approval-needed"))
    store.update_conversation(archived, blocked_by="delivery-unknown", archived_at="2026-09-24T00:00:00Z")

    summary = status_summary(tmp_path)
    assert summary["available"] is True and summary["truncated"] is False
    assert summary["counts"] == {"active": 3, "needs_approval": 1, "blocked": 1}
    assert [(i["title"], i["state"], i["pending_approvals"], i["blocked_by"]) for i in summary["items"]] == [
        ("asking", "approval-needed", 1, None), ("blocked", "blocked", 0, "unfinished-turn"),
        ("running", "running", 0, None), ("queued", "queued", 0, None)]
    assert summary["items"][0]["provider"] == "codex"
    assert set(summary["items"][0]) == {"conversation_id", "provider", "title", "state", "blocked_by",
                                        "pending_approvals", "updated_at"}


def test_c29_6_the_list_is_bounded_and_says_so(store, tmp_path):
    """C-29.6 the list is bounded; the counts still cover every conversation; titles are bounded."""
    for n in range(5):
        send(store, conv(store, f"c{n}" + "x" * (STATUS_TITLE_CHARS * 2)), "waiting")
    summary = status_summary(tmp_path, limit=3)
    assert summary["counts"]["active"] == 5
    assert len(summary["items"]) == 3 and summary["truncated"] is True
    assert all(len(item["title"]) == STATUS_TITLE_CHARS for item in summary["items"])
    assert status_summary(tmp_path, limit=5)["truncated"] is False


def test_c29_6_the_reader_neither_takes_the_stores_lock_nor_waits_for_its_writer(store, tmp_path):
    """C-29.6, C-25.3 while the daemon's connection holds its lock and an open write transaction, the
    summary still returns at once, from another thread, with the last committed state only."""
    cid = conv(store)
    send(store, cid, "running")
    results = {}
    with store.transaction() as tx:                       # holds the store's RLock and BEGIN IMMEDIATE
        tx.execute("UPDATE conversations SET blocked_by='unfinished-turn' WHERE conversation_id=?", (cid,))
        started = time.monotonic()
        reader = threading.Thread(target=lambda: results.update(summary=status_summary(tmp_path)))
        reader.start()
        reader.join(timeout=5)
        elapsed = time.monotonic() - started
    assert not reader.is_alive() and elapsed < 2
    assert results["summary"]["counts"] == {"active": 1, "needs_approval": 0, "blocked": 0}
    assert status_summary(tmp_path)["counts"]["blocked"] == 1           # committed now


def test_c29_6_the_reader_cannot_write(store, tmp_path, monkeypatch):
    """C-29.6, C-3.4 the connection is read-only: a write through it fails."""
    opened = []
    real = sqlite3.connect

    def spy(*args, **kwargs):
        connection = real(*args, **kwargs)
        opened.append((args, kwargs, connection))
        return connection
    monkeypatch.setattr(sqlite3, "connect", spy)
    status_summary(tmp_path)
    (target, *_), kwargs, _ = opened[-1]
    assert kwargs.get("uri") is True and str(target).endswith("conversations.sqlite3?mode=ro")
    with real(str(target), uri=True) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE conversations SET title='x'")


def test_c29_6_an_unreadable_or_newer_store_is_unavailable_never_zero(tmp_path):
    """C-29.6 a store this build cannot read says so with the error's type, and no counts."""
    newer = tmp_path / "newer"
    ConversationStore(newer).close()
    with sqlite3.connect(newer / "conversations.sqlite3") as db:
        db.execute("INSERT INTO schema_version VALUES (99, 'later')")
    assert status_summary(newer) == {"available": False, "error": "schema"}
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "conversations.sqlite3").write_bytes(b"not a database, just bytes" * 100)
    result = status_summary(broken)
    assert result["available"] is False and result["error"] == "DatabaseError"
