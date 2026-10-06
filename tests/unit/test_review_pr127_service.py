"""Control-loop latency and launch/notice correctness regressions from PR 127."""
import concurrent.futures
import json
import threading
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from subfleet import cli
from subfleet.conversations import catalog, wakes
from subfleet.conversations.launch import codex_launch
from tests.unit.test_conversation_service import SETTINGS, svc  # noqa: F401
from tests.unit.test_conversation_wakes import bound, register


def test_pr_poll_does_not_hold_person_dispatch(svc, monkeypatch):
    cid = bound(svc)
    register(svc, cid, prs=["o/r#1"])
    entered, release, dispatched = threading.Event(), threading.Event(), threading.Event()
    def query(_):
        entered.set()
        assert release.wait(5)
        return {"o/r#1": {"state": "OPEN", "checks": [], "reviews": []}}
    monkeypatch.setattr(wakes, "query_prs", query)
    for name in ("_lift_stale_fences", "_catalog_tick", "_moot_blocks", "_adopt_runners", "_replay_unsettled", "_settle_unstarted", "_reap_runners", "_compact"):
        monkeypatch.setattr(svc, name, lambda: None)
    monkeypatch.setattr(svc, "_dispatch", dispatched.set)
    pool = concurrent.futures.ThreadPoolExecutor(1)
    try:
        turn = pool.submit(svc.tick)
        assert entered.wait(2)
        assert dispatched.wait(1), "a slow PR query held person dispatch"
    finally:
        release.set()
        pool.shutdown(wait=True)


def test_open_is_not_queued_behind_worktree_and_diff(svc):
    cid = bound(svc)
    release = threading.Event()
    started = threading.Barrier(3)
    def slow_file_op():
        started.wait(timeout=3)
        release.wait(5)
    blockers = [svc.files.submit(slow_file_op) for _ in range(2)]
    try:
        started.wait(timeout=3)
        future = svc.pool_for("conversation.open").submit(svc.op_conversation_open, {"conversation_id": cid}, None)
        assert future.result(timeout=1)["conversation"]["conversation_id"] == cid
    finally:
        release.set()
        for f in blockers:
            f.result(timeout=3)


def test_moot_block_writer_and_catalog_checks_are_paced(svc, monkeypatch):
    cid = bound(svc)
    svc.store.update_conversation(cid, blocked_by="unfinished-turn")
    c = svc.store.conversation(cid)
    stamp = datetime.fromisoformat(c["blocked_at"]).timestamp()
    record = Mock(return_value={"mtime": stamp + 10})
    writers = Mock(return_value=[123])
    monkeypatch.setattr(catalog, "transcript_record", record)
    monkeypatch.setattr(catalog, "external_writers", writers)
    for _ in range(100):
        svc._moot_blocks()
    assert writers.call_count == record.call_count == 1
    svc.clock.now += 5
    svc._moot_blocks()
    assert writers.call_count == record.call_count == 2
    assert svc.store.conversation(cid)["blocked_by"] == "unfinished-turn"


def test_resumed_codex_launch_preserves_its_own_session_marker(tmp_path):
    lane = SimpleNamespace(home=str(tmp_path / "codex"), credential=SimpleNamespace(ref=None),
                           lane_id="codex-test", identity="codex:test", label="test")
    launch = codex_launch({"provider": "codex", "conversation_id": "cv", "message_id": str(uuid.uuid4()),
                          "text": "continue", "cwd": str(tmp_path), "settings": {**SETTINGS, "model": "gpt-6-sol"},
                          "native_session_id": "thread-id"},
                         attempt_id="turn-job/a1", attempt_dir=tmp_path / "attempt", lane=lane,
                         credential_env={}, model_id="gpt-6-sol", executable="codex", override="hooks={}")
    env = {"SUBFLEET_SESSION_ID": "unrelated-parent", **launch.env_add}
    for key in launch.env_remove:
        env.pop(key, None)
    assert env["SUBFLEET_SESSION_ID"] == "thread-id"
    assert env["SUBFLEET_TURN_JOB"] == "turn-job"


@pytest.mark.parametrize("state,exit_code", [("succeeded", 0), ("failed", 1)])
def test_wait_preserves_notices_for_finished_results_it_returns(monkeypatch, state, exit_code):
    monkeypatch.setattr(cli, "session_id", lambda: "session")
    job = {"job_id": "job", "state": state, "rc": exit_code,
           "notices": [{"notice_id": 7, "session_id": "session", "state": "pending"},
                       {"notice_id": 8, "session_id": "other", "state": "pending"}]}
    def call(op, args, **kw):
        if op == "wait":
            return {"jobs": [job], "timeout": False}
        raise AssertionError(f"wait must not acknowledge notices: {op}")
    client = SimpleNamespace(call=Mock(side_effect=call))
    monkeypatch.setattr(cli, "_client", lambda *a, **kw: client)
    args = cli.build_parser().parse_args(["wait", "job", "--json"])
    assert args.handler(args) == exit_code
    assert [c.args[0] for c in client.call.call_args_list] == ["wait"]


def test_wait_receipt_preserves_the_conversation_wake(svc, monkeypatch):
    from subfleet.daemon import Daemon
    from tests.unit.test_conversation_wakes import finish, rows
    cid = bound(svc)
    finish(svc, cid)
    sid = svc.store.conversation(cid)["native_session_id"]
    svc.daemon.store.add_notice("run-one", "finished", sid)
    backend = SimpleNamespace(store=svc.daemon.store, _job=svc.daemon.store.get_job)
    def call(op, args, **kw):
        if op == "wait":
            return Daemon._wait_answer(backend, args["job_ids"])
        raise AssertionError(f"wait must not acknowledge notices: {op}")
    monkeypatch.setattr(cli, "_client", lambda *a, **kw: SimpleNamespace(call=call))
    monkeypatch.setattr(cli, "session_id", lambda: sid)
    args = cli.build_parser().parse_args(["wait", "run-one", "--json"])
    assert args.handler(args) == 0
    assert svc.daemon.store.list_notices()[0]["state"] == "pending"
    svc.wakes.tick()
    assert len(rows(svc, cid)) == 1


def test_control_loop_paces_completion_scans(svc, monkeypatch):
    svc.wakes.now = svc.clock
    completions = Mock(return_value={})
    monkeypatch.setattr(svc.wakes, "_completions", completions)
    for name in ("_lift_stale_fences", "_catalog_tick", "_moot_blocks", "_dispatch", "_adopt_runners", "_replay_unsettled", "_settle_unstarted", "_reap_runners", "_compact"):
        monkeypatch.setattr(svc, name, lambda: None)
    for _ in range(100):
        svc.tick()
    assert completions.call_count == 1
    svc.clock.now += 1
    svc.tick()
    assert completions.call_count == 2
