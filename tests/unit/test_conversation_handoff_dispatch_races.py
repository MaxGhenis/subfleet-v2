"""C-30.3, C-24.7: dispatch snapshots cannot undo a handoff rollback."""

import pytest

from subfleet.adapters.base import AdapterError
from tests.unit.test_conversation_handoff import handoff, source, turn_job, world


def test_dispatch_rechecks_order_after_a_handoff_restores_an_earlier_message(world, monkeypatch):
    """A cached follower from a legacy unfenced cancellation waits when recovery
    restores the first message before the dispatcher claims the follower.
    """
    cid, (first, second) = source(world, "first", "second")
    job_id = turn_job(world, first, cid, state="waiting")
    world.store.set_state(first, "waiting", job_id=job_id)
    assert world.service._cancel_job_without_attempt(
        job_id, by={"by": "conversation.handoff", "request_id": "h-1"})
    world.service._settle_unstarted()
    scan = world.store.next_dispatchable

    def scan_then_restore(conversation_id=None):
        rows = scan(conversation_id)
        if conversation_id is None:
            assert [row["message_id"] for row in rows] == [second]
            world.service._lift_fence(cid, "handoff:h-1")
        return rows

    submitted = []

    def submit(conversation, message):
        submitted.append(message["message_id"])
        raise AdapterError("keep the restored message queued")

    monkeypatch.setattr(world.store, "next_dispatchable", scan_then_restore)
    monkeypatch.setattr(world.service, "_submit_turn", submit)
    world.service._dispatch()
    assert submitted == []
    assert [world.store.message(mid)["state"] for mid in (first, second)] == ["queued", "queued"]
    monkeypatch.setattr(world.store, "next_dispatchable", scan)
    world.service._dispatch()
    assert submitted == [first]


def test_dispatch_refreshes_a_message_restored_since_its_scan(world, monkeypatch):
    """The cached message's old turn_seq must not find the cancelled job again."""
    cid, (mid,) = source(world, "first")
    scan = world.store.next_dispatchable
    old_seq = world.store.message(mid)["turn_seq"]

    def scan_then_restore(conversation_id=None):
        rows = scan(conversation_id)
        if conversation_id is None:
            job_id = turn_job(world, mid, cid)
            assert world.store.fence(cid, "handoff:h-1")
            assert world.service._cancel_job_without_attempt(
                job_id, by={"by": "conversation.handoff", "request_id": "h-1"})
            world.service._lift_fence(cid, "handoff:h-1")
        return rows

    submitted = []

    def submit(conversation, message):
        submitted.append((message["message_id"], message["turn_seq"]))
        raise AdapterError("keep the restored message queued")

    monkeypatch.setattr(world.store, "next_dispatchable", scan_then_restore)
    monkeypatch.setattr(world.service, "_submit_turn", submit)
    world.service._dispatch()
    assert submitted == [(mid, old_seq + 1)]
    restored = world.store.message(mid)
    assert restored["state"] == "queued" and restored["job_id"] is None


def test_a_submit_returning_after_handoff_rollback_cannot_rebind_its_cancelled_job(world, monkeypatch):
    """A handoff may cancel a created job before submit returns to bind it; a
    failed commit restores the message under a new turn_seq in that interval.
    """
    cid, (mid,) = source(world, "first")
    old_seq = world.store.message(mid)["turn_seq"]

    def fail_commit(*args, **kwargs):
        raise OSError("commit failed")

    def submit(conversation, message):
        job_id = turn_job(world, mid, cid)
        with pytest.raises(OSError, match="commit failed"):
            handoff(world, cid)
        return world.daemon.store.get_job(job_id)

    monkeypatch.setattr(world.store, "commit_handoff", fail_commit)
    monkeypatch.setattr(world.service, "_submit_turn", submit)
    world.service._dispatch()
    restored = world.store.message(mid)
    assert restored["state"] == "queued" and restored["job_id"] is None
    assert restored["turn_seq"] == old_seq + 1
    assert world.store.conversation(cid)["blocked_by"] is None


def test_settlement_of_an_old_cancelled_job_cannot_settle_a_restored_turn(world, monkeypatch):
    """A settlement snapshot taken before rollback cannot cancel the new turn."""
    cid, (mid,) = source(world, "first")
    job_id = turn_job(world, mid, cid)
    world.store.set_state(mid, "waiting", job_id=job_id)
    assert world.store.fence(cid, "handoff:h-1")
    assert world.service._cancel_job_without_attempt(
        job_id, by={"by": "conversation.handoff", "request_id": "h-1"})
    set_state = world.store.set_state

    def restore_before_settle(message_id, state, **kwargs):
        world.service._lift_fence(cid, "handoff:h-1")
        set_state(mid, "waiting", reason="dispatching")
        return set_state(message_id, state, **kwargs)

    monkeypatch.setattr(world.store, "set_state", restore_before_settle)
    world.service._settle_unstarted()
    restored = world.store.message(mid)
    assert restored["state"] == "waiting" and restored["state_reason"] == "dispatching"
    assert restored["job_id"] is None and restored["turn_seq"] == 1


def test_an_interrupted_fence_lift_releases_the_active_handoff_for_recovery(world, monkeypatch):
    """Even a second interruption during cleanup must leave no in-memory owner
    preventing the control loop from recovering the durable fence.
    """
    cid, (mid,) = source(world, "first")
    job_id = turn_job(world, mid, cid)
    world.store.set_state(mid, "waiting", job_id=job_id)
    restore = world.store.restore_after_handoff

    def fail_commit(*args, **kwargs):
        raise OSError("commit failed")

    def interrupt_restore(*args, **kwargs):
        raise KeyboardInterrupt("recovery interrupted")

    monkeypatch.setattr(world.store, "commit_handoff", fail_commit)
    monkeypatch.setattr(world.store, "restore_after_handoff", interrupt_restore)
    with pytest.raises(KeyboardInterrupt, match="recovery interrupted"):
        handoff(world, cid)
    assert cid not in world.service._handing_off
    assert world.store.conversation(cid)["blocked_by"] == "handoff:h-1"
    monkeypatch.setattr(world.store, "restore_after_handoff", restore)
    monkeypatch.setattr(world.service, "_submit_turn", lambda *args: None)
    world.service.tick()
    assert world.store.conversation(cid)["blocked_by"] is None
    assert world.store.message(mid)["turn_seq"] == 1
    assert world.store.message(mid)["job_id"] is None


def test_a_notification_failure_after_commit_keeps_the_handoff_files(world, monkeypatch):
    """COMMIT is durable before notifying readers; even an exception before
    commit_handoff records success cannot authorize deletion of those texts.
    """
    cid, (mid,) = source(world, "first")
    job_id = turn_job(world, mid, cid)
    world.store.set_state(mid, "waiting", job_id=job_id)
    notify = world.store.notify

    def fail_after_commit():
        if world.store.by_request("h-1") is not None:
            raise OSError("notification failed")
        notify()

    monkeypatch.setattr(world.store, "notify", fail_after_commit)
    with pytest.raises(OSError, match="notification failed"):
        handoff(world, cid)
    monkeypatch.setattr(world.store, "notify", notify)
    out = handoff(world, cid)
    assert out["created"] is False
    assert world.store.message_text(world.store.message(out["brief"]["message_id"]))
    assert world.store.message_text(world.store.message(out["moved"][0]["message_id"])) == "first"
    assert world.store.message(mid)["state_reason"].startswith("handed-off:")
    assert world.store.conversation(cid)["blocked_by"] is None
