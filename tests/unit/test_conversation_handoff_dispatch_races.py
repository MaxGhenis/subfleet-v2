"""C-30.3, C-24.7: dispatch snapshots cannot undo a handoff rollback."""

import pytest

from subfleet.adapters.base import AdapterError
from subfleet.contracts import Exit
from subfleet.conversations.store import ConversationError
from tests.unit.test_conversation_handoff import handoff, source, turn_job, world


def test_dispatch_rechecks_order_after_a_handoff_restores_an_earlier_message(world, monkeypatch):
    """A cached follower from a legacy unfenced cancellation waits when recovery
    restores the first message before the dispatcher claims the follower.
    """
    cid, (first, second) = source(world, "first", "second")
    job_id = turn_job(world, first, cid, state="waiting")
    world.store.set_state(first, "waiting", reason="admission: submitted; waiting for the daemon to place it", job_id=job_id)
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
        raise AdapterError("keep the restored message queued", code=int(Exit.OPERATIONAL))  # refused for now (C-26.1)

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
        raise AdapterError("keep the restored message queued", code=int(Exit.OPERATIONAL))  # refused for now (C-26.1)

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
    world.store.set_state(mid, "waiting", reason="admission: submitted; waiting for the daemon to place it", job_id=job_id)
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
    world.store.set_state(mid, "waiting", reason="admission: submitted; waiting for the daemon to place it", job_id=job_id)
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
    world.store.set_state(mid, "waiting", reason="admission: submitted; waiting for the daemon to place it", job_id=job_id)
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


def test_a_claim_a_crash_interrupted_is_released_or_settled_when_its_submit_fails(world, monkeypatch):
    """Review of 6290a51, finding 2: a claim left by a crash (`waiting`, reason
    `dispatching`, no job) whose submit then fails is never left stuck. A refusal
    for now releases it to `queued`, withdrawable; a refusal of the message as it
    is (C-26.1: the workspace is gone) fails it `not-delivered`."""
    from subfleet.conversations.service import CLAIMED
    from subfleet.daemon import Daemon as JobDaemon
    import threading
    cid, (mid, later) = source(world, "first", "later")
    world.store.set_state(mid, "waiting", reason=CLAIMED)

    def refuse(conversation, message):
        raise AdapterError("could not inspect the workdir", code=int(Exit.OPERATIONAL))

    monkeypatch.setattr(world.service, "_submit_turn", refuse)
    world.service._dispatch()
    released = world.store.message(mid)
    assert (released["state"], released["job_id"]) == ("queued", None)
    assert world.service.op_message_cancel({"message_id": mid}, None)["state"] == "cancelled"

    world.store.set_state(later, "waiting", reason=CLAIMED)
    monkeypatch.undo()
    world.workspace.rmdir()                     # the real submit refuses the message as it is
    world.daemon._submit_lock = threading.Lock()
    world.daemon._batch_label = JobDaemon._batch_label
    world.daemon.submit = lambda args, **kw: JobDaemon.submit(world.daemon, args, **kw)
    world.service._dispatch()
    settled = world.store.message(later)
    assert settled["state"] == "failed" and settled["state_reason"].startswith("not-delivered:")


def test_handoff_rollbacks_do_not_use_up_provider_readmissions(world, monkeypatch, tmp_path):
    """Review of 6290a51, finding 3: three handoffs that rolled back each cancelled
    an unattempted job and gave the message a new turn sequence; the first real
    provider failure afterwards is still re-admitted (C-24.6 counts the turn jobs a
    provider reached, not turn sequences)."""
    import json
    from types import SimpleNamespace
    cid, (mid,) = source(world, "first")

    def refuse_commit(*args, **kwargs):
        raise OSError("transient commit failure")

    monkeypatch.setattr(world.store, "commit_handoff", refuse_commit)
    for index in range(3):
        message = world.store.message(mid)
        job_id = f"rollback-job-{index}"
        world.daemon.store.add_job(job_id=job_id, request_id=f"turn:{mid}:{message['turn_seq']}",
                                   payload_digest=message["digest"], kind="turn", state="waiting",
                                   workdir=str(world.workspace), prompt_path=message["text_path"],
                                   sandbox="read-only", name=f"turn-{cid}")
        world.store.set_state(mid, "waiting", reason="admission: submitted; waiting for the daemon to place it", job_id=job_id)
        with pytest.raises(OSError, match="transient commit failure"):
            handoff(world, cid, request_id=f"h-{index}")
    assert world.daemon.store.one("SELECT COUNT(*) AS n FROM attempts")["n"] == 0
    before = world.store.message(mid)
    assert before["turn_seq"] >= 3
    world.daemon.store.add_job(job_id="first-try", request_id=f"turn:{mid}:{before['turn_seq']}",
                               payload_digest=before["digest"], kind="turn", state="failed",
                               workdir=str(world.workspace), prompt_path=before["text_path"],
                               sandbox="read-only", name=f"turn-{cid}")
    world.daemon.store.add_attempt(attempt_id="first-try/a1", job_id="first-try", seq=1,
                                   lane_id="claude-1", model_requested="claude-opus-5-5", state="failed")
    world.store.set_state(mid, "starting", job_id="first-try")
    adir = tmp_path / "first-provider-attempt"
    adir.mkdir()
    (adir / "turn.json").write_text(json.dumps({"state": "failed", "reason": "provider-init-failed",
                                                "ended_by": "driver", "accepted": False}))
    # The evidence says it was never delivered (D-14): what re-admission needs.
    from subfleet.conversations import reconcile
    monkeypatch.setattr(reconcile, "gather", lambda *a: reconcile.Evidence(
        acknowledged=False, frame="absent", process_gone=True, native="absent", native_path=None,
        session_exists=False))
    runner = SimpleNamespace(adir=adir, message_id=mid, conversation_id=cid, attempt={"lane_id": "claude-1"},
                             attempt_id="first-try/a1", offset=0, next_seq=1)
    world.service._on_outcome(runner)
    after = world.store.message(mid)
    assert (after["state"], after["state_reason"]) == ("waiting", "readmit:provider-init-failed")


def test_a_waiting_message_with_no_job_moves_and_a_claimed_one_refuses(world):
    """A readmitted or deferred message (`waiting`, no job, unclaimed) moves in a
    handoff under the store's guard, so it no longer blocks every handoff; a
    message the dispatcher has claimed refuses the handoff until its job exists."""
    from subfleet.conversations.service import CLAIMED
    cid, (mid,) = source(world, "first")
    world.store.set_state(mid, "waiting", reason="deferred: could not inspect the workdir")
    out = handoff(world, cid, request_id="h-waiting")
    assert out["withdrawn"] == [mid] and world.store.message(mid)["state"] == "cancelled"
    import uuid as uuid_module
    cid2, (claimed,) = source(world, "second", native=str(uuid_module.uuid4()))
    world.store.set_state(claimed, "waiting", reason=CLAIMED)
    with pytest.raises(ConversationError) as refused:
        handoff(world, cid2, request_id="h-claimed")
    assert refused.value.reason == "live-turn" and world.store.message(claimed)["state"] == "waiting"
