"""Regressions for the REQUEST CHANGES review of PR 127 at 7504f3b8."""
import json
import uuid

from subfleet.conversations import history
from subfleet.adapters.claude import encode_project_dir
from tests.unit.test_conversation_service import svc, submit, conversation  # noqa: F401


def test_history_before_first_catalog_pass(svc, monkeypatch):
    cid = conversation(svc, native_session_id=str(uuid.uuid4()))
    mid = submit(svc, cid)
    sid = svc.store.conversation(cid)["native_session_id"]
    projects = svc.root / "native-projects"
    path = projects / encode_project_dir(svc.test_workspace) / f"{sid}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "user", "uuid": mid, "message": {"content": "hello"}}) + "\n")
    monkeypatch.setattr(history.transcripts, "projects_dir", lambda: projects)
    monkeypatch.setattr(history.transcripts, "transcript_path", lambda *a: path)
    page = svc.op_conversation_history({"conversation_id": cid}, None)
    assert page["items"] and page["items"][0]["id"] == mid
    assert page["items"][0]["source"] == "subfleet"


from unittest.mock import Mock
import pytest
from subfleet.conversations import wakes
from tests.unit.test_conversation_wakes import bound, finish, register, rows, settle_all


def test_already_announced_run_request_is_satisfied_without_a_wake(svc):
    cid = bound(svc)
    finish(svc, cid)
    svc.wakes.tick()
    settle_all(svc, cid)
    streak = svc.store.conversation(cid)["wake_streak"]
    register(svc, cid, runs=["run-one"])
    svc.wakes.tick()
    assert len(rows(svc, cid)) == 1
    assert svc.store.conversation(cid)["wake_streak"] == streak
    assert svc.store.one("SELECT state FROM wake_requests")["state"] == "satisfied"


def test_all_of_fanout_has_one_wake_and_one_throttle_charge(svc):
    cid = bound(svc)
    ids = [f"fanout-{i}" for i in range(10)]
    finish(svc, cid, ids)
    for job in ids:
        svc.daemon.store.update_job(job, state="running")
    register(svc, cid, runs=ids)
    clock = [wakes.time.time()]
    svc.wakes.now = lambda: clock[0]
    for i, job in enumerate(ids):
        svc.daemon.store.update_job(job, state="succeeded")
        svc.wakes.tick()
        if i < 9:
            assert rows(svc, cid) == []
        clock[0] += 180
    assert len(rows(svc, cid)) == 1
    body = svc.store.message_text(svc.store.message(rows(svc, cid)[0]["message_id"]))
    assert all(f"{job} finished: succeeded" in body for job in ids)
    assert svc.store.conversation(cid)["wake_streak"] == 1


def test_notice_repair_does_no_writes_for_already_delivered_history(svc, monkeypatch):
    cid = bound(svc)
    finish(svc, cid, ["first", "second"])
    for job in ["first", "second"]:
        svc.daemon.store.add_notice(job, "done", svc.store.conversation(cid)["native_session_id"])
    svc.wakes.tick()
    assert all(n["state"] == "surfaced" for n in svc.daemon.store.list_notices())
    transaction = Mock(wraps=svc.daemon.store.transaction)
    monkeypatch.setattr(svc.daemon.store, "transaction", transaction)
    for _ in range(10):
        svc.wakes._surface_notices()
    assert transaction.call_count == 0


def test_notice_job_lookup_uses_an_index(svc):
    plan = svc.daemon.store.query("EXPLAIN QUERY PLAN SELECT 1 FROM notices WHERE job_id=? AND state='acknowledged'", ("job",))
    assert any("INDEX notices_job" in r["detail"] for r in plan), plan


@pytest.mark.parametrize("explicit", [False, True])
def test_upgrade_does_not_automatically_announce_old_completions(svc, explicit):
    cid = bound(svc)
    svc.store._db.execute("UPDATE conversations SET created_at='2026-09-20T00:00:00+00:00' WHERE conversation_id=?", (cid,))
    finish(svc, cid)
    svc.daemon.store.update_job("run-one", created_at="2026-09-28T00:00:00+00:00")
    svc.daemon.store.add_notice("run-one", "old result", svc.store.conversation(cid)["native_session_id"])
    if explicit:
        register(svc, cid, runs=["run-one"])
    svc.wakes.tick()
    assert len(rows(svc, cid)) == int(explicit)


def test_notice_repair_survives_a_cross_store_crash(svc, monkeypatch):
    cid = bound(svc)
    finish(svc, cid)
    svc.daemon.store.add_notice("run-one", "done", svc.store.conversation(cid)["native_session_id"])
    original = svc.daemon.store.transaction
    def fail_repair(kind, **kw):
        if kind == "conversation.wake-notices":
            raise RuntimeError("crash between stores")
        return original(kind, **kw)
    monkeypatch.setattr(svc.daemon.store, "transaction", fail_repair)
    with pytest.raises(RuntimeError, match="between stores"):
        svc.wakes.tick()
    assert len(rows(svc, cid)) == 1
    assert svc.store.one("SELECT * FROM wake_notice_repairs")
    monkeypatch.setattr(svc.daemon.store, "transaction", original)
    engine = wakes.WakeEngine(svc)
    try:
        engine.tick()
        assert len(rows(svc, cid)) == 1
        assert svc.daemon.store.list_notices()[0]["state"] == "surfaced"
        assert svc.store.query("SELECT * FROM wake_notice_repairs") == []
    finally:
        engine.close()


def test_notice_repair_batch_is_bounded(svc):
    with svc.store.transaction() as tx:
        tx.executemany("INSERT INTO wake_notice_repairs VALUES(?,?)", [(f"job-{i}", "2026-10-04T00:00:00Z") for i in range(501)])
    svc.wakes._surface_notices()
    assert len(svc.store.query("SELECT * FROM wake_notice_repairs")) == 1


def test_satisfied_alternatives_do_not_hold_an_unrelated_result(svc):
    cid = bound(svc)
    finish(svc, cid)
    svc.wakes.tick()
    settle_all(svc, cid)
    register(svc, cid, runs=["run-one"], prs=["o/r#1"])
    with svc.store.transaction() as tx:
        tx.execute("UPDATE wake_requests SET ready_json='[\"o/r#1\"]' WHERE kind='pr'")
    sid = svc.store.conversation(cid)["native_session_id"]
    svc.daemon.store.add_job(job_id="unrelated", request_id="unrelated", payload_digest="fixture", kind="dispatch",
                            caller_session=sid, state="succeeded", workdir=svc.test_workspace, prompt_path="fixture.md", sandbox="read-only")
    svc.wakes.tick()
    assert len(rows(svc, cid)) == 2
    assert "unrelated finished" in svc.store.message_text(svc.store.message(rows(svc, cid)[1]["message_id"]))
    assert all(r["state"] == "satisfied" for r in svc.store.query("SELECT state FROM wake_requests"))
