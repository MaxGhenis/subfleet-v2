"""C-25.2: archive and submit guards stay inside their SQLite transactions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
import uuid

import pytest

from subfleet.conversations.store import ConversationError, ConversationStore
from tests.unit.test_conversation_archive import archive, row
from tests.unit.test_conversation_service import SETTINGS, conversation, submit, svc  # noqa: F401
from tests.unit.test_suggestions import people  # noqa: F401


@pytest.mark.parametrize("first", ["archive", "submit"])
def test_archive_and_submit_serialize_after_their_guard_reads(people, monkeypatch, first):
    """Two connections contend for the same row, beyond the in-process store lock.

    Pause the winner after it reads its guard, before its write. The other
    connection attempts BEGIN IMMEDIATE while that transaction is still open.
    Only one operation can succeed, whichever takes the write transaction first.
    """
    primary = people.store
    cid = conversation(people)
    contender = ConversationStore(people.root)
    guard_read, second_begin, release = (threading.Event() for _ in range(3))
    real_transaction = primary.transaction
    guard_sql = ("SELECT state FROM messages" if first == "archive"
                 else "SELECT archived_at FROM conversations")

    class GuardCursor:
        def __init__(self, cursor, db):
            self.cursor, self.db = cursor, db

        def fetchone(self):
            value = self.cursor.fetchone()
            assert self.db.in_transaction, "the guard must be read in the write transaction"
            guard_read.set()
            assert release.wait(30), "the competing operation never reached its transaction"
            assert self.db.in_transaction, "the transaction must cover the guard through the write"
            return value

    class PausedTransaction:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, parameters=()):
            cursor = self.db.execute(sql, parameters)
            return GuardCursor(cursor, self.db) if sql.startswith(guard_sql) else cursor

    @contextmanager
    def paused_transaction():
        with real_transaction() as db:
            yield PausedTransaction(db)

    monkeypatch.setattr(primary, "transaction", paused_transaction)
    contender._db.set_trace_callback(lambda sql: second_begin.set() if sql == "BEGIN IMMEDIATE" else None)
    mid = str(uuid.uuid4())

    def accept(store):
        return store.submit_message(conversation_id=cid, message_id=mid, after_message_id=None,
                                    text="a competing message", attachments=[], settings=SETTINGS)

    def hide(store):
        return store.archive_conversation(cid, archived=True)

    winner, loser = (hide, accept) if first == "archive" else (accept, hide)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            leading = pool.submit(winner, primary)
            try:
                assert guard_read.wait(30), "the first operation never read its transactional guard"
                trailing = pool.submit(loser, contender)
                assert second_begin.wait(30), "the competing connection never attempted its transaction"
                assert not trailing.done(), "the second writer passed the first writer's transaction"
            finally:
                release.set()
            leading.result(timeout=30)
            with pytest.raises(ConversationError) as error:
                trailing.result(timeout=30)
        assert (error.value.reason, error.value.code) == (
            "archived" if first == "archive" else "conversation-active", 7)
        messages = primary.messages(cid)
        if first == "archive":
            assert row(people, cid)["archived_at"] is not None and messages == []
        else:
            assert row(people, cid)["archived_at"] is None
            assert [(m["message_id"], m["state"]) for m in messages] == [(mid, "queued")]
    finally:
        release.set()
        contender.close()


@pytest.mark.parametrize("state", ["queued", "waiting"])
def test_archived_rows_are_excluded_from_dispatch_and_stale_scans(people, state):
    """Exercise a legacy archived row with pending work, including readmission.

    The archive op now refuses such a row; a direct fixture write represents
    older state and lets us check both the scan and the dispatcher's recheck.
    """
    cid = conversation(people)
    mid = submit(people, cid)
    if state == "waiting":
        people.store.set_state(mid, state, reason="readmit:capacity")
    stale = people.store.message(mid)
    people.store.update_conversation(cid, archived_at="2026-10-11T12:00:00.000Z")
    assert people.store.next_dispatchable() == []
    people._dispatch_one(stale)
    assert people.daemon.submits == []
    assert people.store.message(mid)["state"] == state
    archive(people, cid, False)
