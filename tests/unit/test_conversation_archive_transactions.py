"""C-25.2: archive, submit and handoff recovery serialize their state changes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from itertools import permutations
import threading
import uuid

import pytest

from subfleet.conversations.store import ConversationError, ConversationStore
from subfleet.conversations.turn import TERMINAL_STATES
from tests.unit.test_conversation_archive import archive, row
from tests.unit.test_conversation_handoff import source, turn_job, world  # noqa: F401
from tests.unit.test_conversation_service import SETTINGS, conversation, submit, svc  # noqa: F401
from tests.unit.test_suggestions import people  # noqa: F401


@pytest.mark.parametrize("first,second", [("archive", "submit"), ("submit", "archive"),
                                        ("archive", "recover"), ("recover", "archive")])
def test_archive_serializes_with_submit_and_handoff_recovery(people, monkeypatch, first, second):
    """Two connections contend for the same row, beyond the in-process store lock.

    Pause the winner after it reads its guard, before its write. The other
    connection attempts BEGIN IMMEDIATE while that transaction is still open.
    Only one operation can succeed, whichever takes the write transaction first.
    """
    primary = people.store
    cid = conversation(people)
    mid = str(uuid.uuid4())
    before = None
    if "recover" in (first, second):
        primary.submit_message(conversation_id=cid, message_id=mid, after_message_id=None,
                               text="cancelled by an interrupted handoff", attachments=[], settings=SETTINGS)
        primary.set_state(mid, "cancelled", reason="cancelled: handoff", job_id="handoff-job")
        before = primary.message(mid)
    contender = ConversationStore(people.root)
    guard_read, second_begin, release = (threading.Event() for _ in range(3))
    real_transaction = primary.transaction
    guard_sql = {"archive": "SELECT state FROM messages", "submit": "SELECT archived_at FROM conversations",
                 "recover": "SELECT blocked_by, archived_at FROM conversations"}[first]

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

    def accept(store):
        return store.submit_message(conversation_id=cid, message_id=mid, after_message_id=None,
                                    text="a competing message", attachments=[], settings=SETTINGS)

    def hide(store):
        return store.archive_conversation(cid, archived=True)

    def recover(store):
        return store.restore_after_handoff(cid, "handoff:h-1", [
            {"message_id": mid, "job_id": "handoff-job", "turn_seq": before["turn_seq"]}])

    operations = {"archive": hide, "submit": accept, "recover": recover}
    winner, loser = operations[first], operations[second]
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
            assert row(people, cid)["archived_at"] is not None
            if second == "recover":
                assert messages == [before]
                assert primary.message_text(messages[0]) == "cancelled by an interrupted handoff"
            else:
                assert messages == []
        else:
            assert row(people, cid)["archived_at"] is None
            assert [(m["message_id"], m["state"]) for m in messages] == [(mid, "queued")]
    finally:
        release.set()
        contender.close()


# All 40 total orders in which the handoff starts before its failure and
# recovery, including recovery before the cancelled job is settled. Run each
# for current fenced handoffs and older unfenced ones: 80 cases, 400 steps.
HANDOFF_ORDERS = [order for order in permutations(("archive", "start", "failure", "recover", "submit"))
                  if order.index("start") < order.index("failure")
                  and order.index("start") < order.index("recover")]


@pytest.mark.parametrize("fenced", [False, True], ids=["legacy-unfenced", "fenced"])
@pytest.mark.parametrize("order", HANDOFF_ORDERS, ids=lambda order: "-".join(order))
def test_archive_handoff_and_submit_all_valid_orders_preserve_messages(world, fenced, order):
    """Real audited job cancellation and recovery; no archived pending work or lost text."""
    assert len(HANDOFF_ORDERS) == 40
    cid, (mid,) = source(world, "the handoff's original message")
    job_id = turn_job(world, mid, cid, state="waiting")
    world.store.set_state(mid, "waiting", reason="admission: not yet placed", job_id=job_id)
    world.store.append_events(conversation_id=cid, message_id=mid, attempt_id=f"{mid}/a1",
                              stdout_offset=1, stdin_seq=0,
                              events=[("stdout", "0", 1, "text", {"text": "retained history"})])
    history = world.store.events_after(cid, 0)
    submitted = str(uuid.uuid4())
    expected = {mid: "the handoff's original message"}
    archive_at = None
    for operation in order:
        if operation == "start":
            if fenced:
                assert world.store.fence(cid, "handoff:h-1")
            assert world.service._cancel_job_without_attempt(
                job_id, by={"by": "conversation.handoff", "request_id": "h-1"})
        elif operation == "failure":
            world.service._settle_unstarted()
        elif operation == "recover":
            before = world.store.message(mid)
            try:
                restored = world.service._lift_fence(cid, "handoff:h-1")
            except ConversationError as error:
                assert (error.reason, error.code) == ("archived", 7)
                assert archive_at and before["state"] == "cancelled"
                assert world.store.message(mid) == before and error.fix
            else:
                assert not archive_at and restored == [mid]
        elif operation == "archive":
            try:
                archived = world.store.archive_conversation(cid, archived=True)
            except ConversationError as error:
                assert error.reason in ("conversation-active", "handoff-running") and error.code == 7
            else:
                archive_at = archived["archived_at"]
                assert archive_at
        elif operation == "submit":
            try:
                world.store.submit_message(conversation_id=cid, message_id=submitted, after_message_id=mid,
                                           text="a concurrent submission", attachments=[], settings=world.store.conversation(cid)["settings"])
            except ConversationError as error:
                assert (error.reason, error.code) == ("archived", 7) and archive_at
                assert world.store.find_message(submitted) is None
            else:
                expected[submitted] = "a concurrent submission"

        messages = world.store.messages(cid)
        assert {m["message_id"]: world.store.message_text(m) for m in messages} == expected, (order, operation)
        assert world.store.events_after(cid, 0) == history
        assert world.store.conversation(cid)["archived_at"] == archive_at
        assert not archive_at or all(m["state"] in TERMINAL_STATES for m in messages), (order, operation)


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
