"""Turn -> run/request -> terminal event -> wake -> next provider turn.

The real service, stores, dispatch and Claude driver; scripted provider frames
and a fake submitter let this run without a kernel process-inspection entitlement.
The companion e2e tests exercise the same path with the daemon and guardian.
"""
import json
import subprocess
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from subfleet.conversations import reconcile, wakes
from subfleet.conversations.claude_turn import ClaudeTurn
from subfleet.conversations.turn import TurnSpec
from tests.frontend.daemon_harness import claude_init, claude_result
from tests.unit.test_conversation_service import conversation, svc  # noqa: F401


def fake_provider(svc, cid, mid):
    message = svc.store.message(mid)
    sid = svc.store.conversation(cid)["native_session_id"]
    driver = ClaudeTurn(TurnSpec(provider="claude", message_id=mid, text=svc.store.message_text(message),
                                native_session_id=sid, model_id="opus[1m]", permission="ask"), read_bytes=lambda p: b"")
    driver.start()
    initialized = driver.feed(json.dumps(claude_init()), 0)
    assert initialized.outcome is None and initialized.frames
    accepted = driver.feed(json.dumps({"type": "user", "uuid": mid, "isReplay": True,
                                      "message": {"role": "user", "content": driver.spec.text}}), 1)
    assert any(e.kind == "accepted" for e in accepted.events)
    svc.store.set_state(mid, "running")
    return driver


@pytest.mark.parametrize("kind", ["run", "final", "pr", "time"])
def test_fake_provider_ends_and_exactly_one_subfleet_message_starts_next_turn(svc, monkeypatch, kind):
    sid = str(uuid.uuid4())
    cid = conversation(svc, native_session_id=sid)
    svc.wakes.now = lambda: 1000
    mid = svc.op_message_submit({"conversation_id": cid, "message_id": str(uuid.uuid4()), "text": "Build and check back"}, None)["message_id"]
    svc._dispatch()
    host = svc.store.message(mid)["job_id"]
    first = fake_provider(svc, cid, mid)
    args = {"session_id": sid, "calling_job": host, "request_id": str(uuid.uuid4()), "note": "Inspect progress"}
    final = "Work is waiting."
    if kind in ("run", "final"):
        svc.daemon.store.add_job(job_id="child-run", request_id="child-run", payload_digest="fixture", kind="dispatch",
            state="queued", workdir=svc.test_workspace, prompt_path="fixture.md", sandbox="read-only",
            caller_session=sid, parent_job_id=host, out_path="/work/result.md")
        if kind == "final":
            final += '\nWAKE-ME: runs=child-run note="Inspect progress"'
        else:
            svc.op_conversation_wake({**args, "runs": ["child-run"]}, None)
    elif kind == "time":
        svc.op_conversation_wake({**args, "at": datetime.fromtimestamp(1300, UTC).isoformat()}, None)
    else:
        svc.op_conversation_wake({**args, "prs": ["owner/repo#1"]}, None)
        def gh(argv, **kwargs):
            assert argv == ["gh", "api", "graphql", "--input", "-"]
            return subprocess.CompletedProcess(argv, 0, json.dumps({"data": {"p0": {"pullRequest": {
                "state": pr_state[0], "reviews": {"nodes": []}, "commits": {"nodes": []}}}}}), "")
        pr_state = ["OPEN"]
        monkeypatch.setattr(wakes.subprocess, "run", gh)
    svc.wakes.tick()
    assert len(svc.store.messages(cid)) == 1
    result = {**claude_result(), "result": final}
    done = first.feed(json.dumps(result), 2)
    if done.outcome is None:
        done = first.eof(3)
    assert done.outcome.state == "complete"
    runner = SimpleNamespace(message_id=mid, conversation_id=cid, attempt_id=host + "/a1", driver=first,
                             attempt={"lane_id": "claude-1"}, offset=3, next_seq=2)
    svc._settle_outcome(runner, {"native_session_id": sid, "final_text": final},
                        reconcile.Settlement(done.outcome.state, None, session_known=True))
    with svc.daemon.store.transaction("fake-provider-ended") as tx:
        tx.execute("UPDATE jobs SET state='succeeded' WHERE job_id=?", (host,))
    svc.wakes.tick()
    assert len(svc.store.messages(cid)) == 1
    if kind in ("run", "final"):
        with svc.daemon.store.transaction("fake-run-ended") as tx:
            tx.execute("UPDATE jobs SET state='succeeded' WHERE job_id='child-run'")
    elif kind == "time":
        svc.wakes.now = lambda: 1300
    else:
        pr_state[0] = "MERGED"
        svc.wakes.now = lambda: 1060
    svc.wakes.tick()
    svc.wakes.tick()
    messages = svc.store.messages(cid)
    assert len(messages) == 2 and messages[-1]["origin"] == "wake"
    assert "Inspect progress" in svc.store.message_text(messages[-1])
    svc._dispatch()
    assert len(svc.daemon.submits) == 2
    fake_provider(svc, cid, messages[-1]["message_id"])
    assert svc.store.conversation(cid)["native_session_id"] == sid
