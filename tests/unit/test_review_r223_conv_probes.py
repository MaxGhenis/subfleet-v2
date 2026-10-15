"""Review r1 regressions: socket conversation stops retain their requester."""
import json
import struct

import pytest

from subfleet.conversations.peers import AuditToken
from tests.unit.test_conversation_service import FakeRunner, conversation, submit, svc  # noqa: F401


def cancel_events(svc, job_id):
    return [json.loads(row["data_json"]) for row in svc.daemon.store.query(
        "SELECT data_json FROM events WHERE job_id=? AND kind='job.cancel_requested' ORDER BY event_id",
        (job_id,))]


@pytest.fixture(params=[None, 424242])
def peer(request):
    return (AuditToken(struct.pack("=8I", 0, 0, 0, 0, 0, request.param, 0, 7))
            if request.param else None)


@pytest.mark.parametrize("op", ["message.cancel", "turn.interrupt"])
def test_waiting_conversation_cancel_records_socket_client(svc, peer, op):
    cid = conversation(svc)
    mid = submit(svc, cid)
    svc._dispatch()
    job_id = svc.store.message(mid)["job_id"]
    assert job_id
    assert svc.handle(op, {"message_id": mid, "session_id": "stated-session",
                           "pid": -123, "by": "daemon"}, peer)["state"] == "cancelled"
    assert cancel_events(svc, job_id) == [{"by": "client", "session_id": "stated-session",
                                         "pid": peer.pid if peer else None,
                                         "reason": "message-cancelled"}]


def test_running_turn_interrupt_keeps_client_through_containment(svc, peer):
    cid = conversation(svc)
    mid = submit(svc, cid)
    svc._dispatch()
    job_id = svc.store.message(mid)["job_id"]
    aid = job_id + "/a1"
    svc.daemon.store.add_attempt(attempt_id=aid, job_id=job_id, seq=1,
                                 lane_id="claude-1", model_requested="opus", state="running")
    svc.store.set_state(mid, "running", expect=("waiting",))
    runner = FakeRunner()
    runner.message_id, runner.attempt_id, runner.stop_reason = mid, aid, None
    runner.end_title = lambda reason: None

    def interrupt(reason):
        runner.stop_reason = reason
        # Escalation can happen immediately, before the socket handler returns.
        svc._on_contain(aid)
    runner.interrupt = interrupt
    svc.runners[aid] = runner
    svc.handle("turn.interrupt", {"message_id": mid, "session_id": "stated-session", "pid": -123}, peer)
    assert cancel_events(svc, job_id) == [{"by": "client", "session_id": "stated-session",
                                         "pid": peer.pid if peer else None, "reason": "stopped"}]
    assert svc.daemon.store.get_attempt(aid)["killed_by"] == "stopped"


def test_internal_conversation_cancel_remains_daemon_attributed(svc):
    cid = conversation(svc)
    mid = submit(svc, cid)
    svc._dispatch()
    job_id = svc.store.message(mid)["job_id"]
    svc._cancel_job_without_attempt(job_id, by={"by": "message-settled"})
    assert cancel_events(svc, job_id) == [{"by": "daemon", "reason": "message-settled", "running_s": 0.0}]


@pytest.mark.parametrize("state", ["waiting", "starting", "running"])
def test_interrupt_before_runner_adoption_retains_the_client(svc, peer, state):
    cid = conversation(svc)
    mid = submit(svc, cid)
    svc._dispatch()
    job_id = svc.store.message(mid)["job_id"]
    aid = job_id + "/a1"
    svc.daemon.store.add_attempt(attempt_id=aid, job_id=job_id, seq=1,
                                 lane_id="claude-1", model_requested="opus", state="starting")
    svc.store.set_state(mid, state)
    svc.handle("turn.interrupt", {"message_id": mid, "session_id": "stated-session"}, peer)
    svc._on_contain(aid)  # A recovered runner can arrive after the handler returned.
    assert cancel_events(svc, job_id) == [{"by": "client", "session_id": "stated-session",
                                         "pid": peer.pid if peer else None, "reason": "stopped"}]
