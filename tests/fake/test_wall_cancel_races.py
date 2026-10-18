"""Round 6: executed review inputs and cancel/exit/signal interleavings."""
import json
from datetime import timedelta

import pytest
from hypothesis import HealthCheck, Phase, given, settings, strategies as st

from subfleet import daemon as daemon_module, procs, protocol
from subfleet.offline import Offline
from tests.fake.test_review_r223_wall_probes import clocked, cancel_events, mark_starting  # noqa: F401
from tests.fake.test_state_contract import reserve, state_daemon  # noqa: F401

CLIENT = {"by": "client", "session_id": "operator-session", "pid": 424242, "reason": "stop it"}


def setup_running(clocked, monkeypatch):
    service, harness, clock = clocked
    jid, attempt, adir = reserve(service, harness, caller_session="caller")
    aid = attempt["attempt_id"]
    mark_starting(service, aid)
    service.store.update_attempt(aid, state="running", guardian_pid=424242, pgid=424242,
                                 boot_id="unit-test-boot", proc_start="fixture-start")
    service.store.update_job(jid, max_wall_s=21600)
    clock.t += timedelta(seconds=21601, milliseconds=123)
    monkeypatch.setattr(procs, "liveness", lambda *args: "alive")
    monkeypatch.setattr(procs, "same_process", lambda *args: True)
    monkeypatch.setattr(procs, "signal_group", lambda *args, **kw: True)
    return service, harness, clock, jid, aid, adir


def publish(adir, finished, rc=0):
    for name, content in (("stdout", "verified result\n"), ("stderr", ""), ("lane.log", ""),
                          ("stream.jsonl", '{"type":"turn.completed"}\n' if rc == 0 else "")):
        (adir / name).write_text(content)
    (adir / "exit.json").write_text(json.dumps({"rc": rc, "signal": None, "child_pid": 424243,
                                               "finished_at": finished}))


def ask_client(service, jid):
    answer = service.kill(protocol.KillArgs(job_id=jid, session_id=CLIENT["session_id"],
                                           reason=CLIENT["reason"]), pid=CLIENT["pid"])
    assert answer["status"] == "cancel requested"


def wall_request(service, aid, jid):
    assert service._enforce_wall(service.store.get_attempt(aid), service.store.get_job(jid))


def children(service, harness, clock, parent, caller_session="caller"):
    running = service.dispatch("submit", harness.submit_args(parent_job_id=parent, caller_session=caller_session))["job_id"]
    queued = service.dispatch("submit", harness.submit_args(parent_job_id=parent, caller_session=caller_session))["job_id"]
    aid = running + "/a1"
    service.store.add_attempt(attempt_id=aid, job_id=running, seq=1, lane_id="codex-1",
                              model_requested="astra", state="running", started_at=clock.t.isoformat(),
                              guardian_pid=424244, pgid=424244, boot_id="unit-test-boot", proc_start="child-start")
    service.store.update_job(running, state="running")
    (service.root / "jobs" / running / "a1").mkdir()
    return running, queued, aid


@pytest.mark.parametrize("stage", ["before-kill", "after-sigterm"])
@pytest.mark.parametrize("rc", [0, 1])
def test_acknowledged_client_overrides_pending_wall(clocked, monkeypatch, stage, rc):
    service, _, clock, jid, aid, adir = setup_running(clocked, monkeypatch)
    wall_request(service, aid, jid)
    def signal(*args, **kwargs):
        if stage == "after-sigterm":
            ask_client(service, jid)
        clock.t += timedelta(seconds=2)
        publish(adir, clock.t.isoformat(), rc)
        return True
    monkeypatch.setattr(procs, "signal_group", signal)
    if stage == "before-kill":
        ask_client(service, jid)
        publish(adir, clock.t.isoformat(), rc)
    service._kill_attempt(service.store.get_attempt(aid))
    service._process_attempt(aid)
    job = service.store.get_job(jid)
    assert (job["state"], job["rc"], job["accepted_attempt_id"]) == ("cancelled", 130, None)
    assert cancel_events(service, jid)[-1] == CLIENT
    for shown in (service.dispatch("show", {"job_id": jid}), Offline(service.root).show_job(jid)):
        assert all(shown["cancel"][k] == v for k, v in CLIENT.items())
    assert any("operator-session" in n["text"] and "424242" in n["text"] and "stop it" in n["text"]
               for n in service.store.list_notices("caller"))


def test_receipt_winning_parent_does_not_stop_either_dependent(clocked, monkeypatch):
    service, harness, clock, jid, aid, adir = setup_running(clocked, monkeypatch)
    live, queued, child_aid = children(service, harness, clock, jid)
    wall_request(service, aid, jid)
    assert all(service.store.get_job(j)["cancel_requested_at"] is None for j in (live, queued))
    publish(adir, (clock.t - timedelta(milliseconds=1)).isoformat())
    service._kill_attempt(service.store.get_attempt(aid))
    service._process_attempt(aid)
    signals = []
    monkeypatch.setattr(procs, "signal_group", lambda *a, **kw: signals.append(a))
    service._process_attempt(child_aid)
    assert service.store.get_job(jid)["state"] == "succeeded"
    assert service.store.get_job(queued)["state"] == "queued"
    assert service.store.get_job(live)["cancel_requested_at"] is None
    assert signals == []


@pytest.mark.parametrize("rc", [0, 1])
def test_exit_between_last_receipt_read_and_signal_keeps_own_outcome(clocked, monkeypatch, rc):
    service, _, clock, jid, aid, adir = setup_running(clocked, monkeypatch)
    wall_request(service, aid, jid)
    read = service._read_json
    reads = 0
    def last_read(path):
        nonlocal reads
        answer = read(path)
        if path == adir / "exit.json":
            reads += 1
            if reads == 3:  # Census, preflight, then the final pre-signal read.
                publish(adir, clock.t.isoformat(), rc)
                clock.t += timedelta(milliseconds=2)
        return answer
    monkeypatch.setattr(service, "_read_json", last_read)
    service._kill_attempt(service.store.get_attempt(aid))
    service._process_attempt(aid)
    job, attempt = service.store.get_job(jid), service.store.get_attempt(aid)
    assert job["state"] == ("succeeded" if rc == 0 else "failed")
    assert job["rc"] == rc and job["cancel_requested_at"] is None
    assert attempt["outcome_class"] == ("ok" if rc == 0 else "unknown")
    assert attempt["killed_by"] is None
    [event] = service.store.query("SELECT ts,data_json FROM events WHERE attempt_id=? AND kind='attempt.kill_started'", (aid,))
    assert daemon_module._seconds_between(json.loads((adir / "exit.json").read_text())["finished_at"], event["ts"]) > 0
    assert json.loads(event["data_json"])["signal_at"] == event["ts"]


@pytest.mark.parametrize("precision", ["milliseconds", "seconds"])
def test_alive_at_signal_cannot_claim_ok_even_with_completion(clocked, monkeypatch, precision):
    service, _, clock, jid, aid, adir = setup_running(clocked, monkeypatch)
    wall_request(service, aid, jid)
    def signal(*args, **kwargs):
        clock.t += timedelta(seconds=2 if precision == "milliseconds" else 0)
        publish(adir, clock.t.isoformat(timespec=precision))
        return True
    monkeypatch.setattr(procs, "signal_group", signal)
    service._kill_attempt(service.store.get_attempt(aid))
    service._process_attempt(aid)
    attempt = service.store.get_attempt(aid)
    assert (service.store.get_job(jid)["state"], attempt["state"], attempt["outcome_class"], attempt["killed_by"]) == (
        "cancelled", "interrupted", "unknown", "max_wall_s")


@pytest.mark.parametrize("caller_session", ["caller", None])
def test_final_wall_cascade_keeps_wall_and_parent_attribution(clocked, monkeypatch, caller_session):
    service, harness, clock, jid, aid, adir = setup_running(clocked, monkeypatch)
    live, queued, child_aid = children(service, harness, clock, jid, caller_session)
    wall_request(service, aid, jid)
    service._kill_attempt(service.store.get_attempt(aid))
    service._process_attempt(aid)
    assert service.store.get_job(jid)["state"] == "cancelled"
    assert service.store.get_job(queued)["state"] == "cancelled"
    service._process_attempt(child_aid)
    service._process_attempt(child_aid)
    child = service.store.get_attempt(child_aid)
    assert child["killed_by"] == "max_wall_s"
    assert jid in child["outcome_detail"]
    assert "max_wall_s" in child["outcome_detail"]
    assert any(jid in n["text"] and "max_wall_s" in n["text"] for n in service.store.list_notices(caller_session)
               if n["job_id"] == live)


@settings(max_examples=60, deadline=None, derandomize=True, phases=[Phase.generate],
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(exit_stage=st.sampled_from(["before-cap", "inspection", "pending", "last-read", "after-signal"]),
       client_stage=st.sampled_from([None, "before-cap", "pending", "after-signal"]), rc=st.sampled_from([0, 1]),
       receipt_write=st.sampled_from(["immediate", "settle"]))
def test_wall_interleaving_property(clocked, monkeypatch, exit_stage, client_stage, rc, receipt_write):
    with monkeypatch.context() as patch:
        service, harness, clock, jid, aid, adir = setup_running(clocked, patch)
        live, queued, _ = children(service, harness, clock, jid)
        acknowledged = []
        exited_at = None
        def provider_exit():
            nonlocal exited_at
            if exited_at is None:
                exited_at = clock.t
                if receipt_write == "immediate":
                    publish(adir, exited_at.isoformat(), rc)
                clock.t += timedelta(milliseconds=2)
        def client():
            if not acknowledged:
                ask_client(service, jid)
                acknowledged.append(True)
        if client_stage == "before-cap":
            client()
        if exit_stage == "before-cap":
            provider_exit()
        def inspect(*args):
            if exit_stage == "inspection":
                provider_exit()
            return True
        patch.setattr(service, "_inspect_running", inspect)
        request = service._request_cancel
        def cancel(*args, **kwargs):
            result = request(*args, **kwargs)
            if args[1].get("reason") == "max_wall_s":
                if client_stage == "pending":
                    client()
                if exit_stage == "pending":
                    provider_exit()
            return result
        patch.setattr(service, "_request_cancel", cancel)
        read = service._read_json
        kill_reads = 0
        killing = False
        def receipt(path):
            nonlocal kill_reads
            answer = read(path)
            if killing and path == adir / "exit.json":
                kill_reads += 1
                if kill_reads == 3 and exit_stage == "last-read":
                    provider_exit()
                    clock.t += timedelta(milliseconds=2)
            return answer
        patch.setattr(service, "_read_json", receipt)
        signal_times = []
        def signal(*args, **kwargs):
            signal_times.append(clock.t)
            if client_stage == "after-signal":
                client()
            if exit_stage == "after-signal":
                clock.t += timedelta(seconds=2)
                provider_exit()
            return True
        patch.setattr(procs, "signal_group", signal)
        kill = service._kill_attempt
        def kill_attempt(a):
            nonlocal killing
            killing = True
            return kill(a)
        patch.setattr(service, "_kill_attempt", kill_attempt)
        service._process_attempt(aid)
        if exited_at is not None and receipt_write == "settle":
            # Provider exit and durable receipt publication are independent.
            # The guardian can publish only after the daemon's signal settles.
            publish(adir, exited_at.isoformat(), rc)
        service._process_attempt(aid)
        job, attempt = service.store.get_job(jid), service.store.get_attempt(aid)
        if acknowledged:
            assert (job["state"], job["rc"]) == ("cancelled", 130)
            assert service._cancel_info(jid)["session_id"] == "operator-session"
        if job["state"] == "succeeded":
            assert all(service.store.get_job(j)["cancel_requested_at"] is None for j in (live, queued))
        if signal_times and exited_at is not None and signal_times[0] <= exited_at:
            assert attempt["outcome_class"] != "ok"
        if exited_at is not None and not acknowledged and (not signal_times or exited_at < signal_times[0]):
            assert (job["state"], job["rc"]) == ("succeeded" if rc == 0 else "failed", rc)
            assert attempt["killed_by"] is None
        # Release rows so later generated examples cannot be admitted instead.
        for child in (live, queued):
            service.store.update_job(child, state="cancelled", rc=130)


@pytest.mark.parametrize("precision,offset_ms,natural", [
    ("milliseconds", -2000, True), ("milliseconds", -1, True),
    ("milliseconds", 0, False), ("milliseconds", 2000, False),
    ("seconds", -2000, True), ("seconds", -1, False), ("seconds", 0, False),
])
def test_receipt_ordering_survives_restart_and_legacy_precision(clocked, monkeypatch, precision, offset_ms, natural):
    service, _, clock, jid, aid, adir = setup_running(clocked, monkeypatch)
    wall_request(service, aid, jid)
    # Model recovery from durable signal evidence, with no in-memory signal clock.
    with service.store.transaction("attempt.kill_started", job_id=jid, attempt_id=aid,
                                   data={"signal_at": clock.t.isoformat(timespec="milliseconds"), "signal_sent": True}) as tx:
        tx.execute("UPDATE attempts SET killed_by='max_wall_s' WHERE attempt_id=?", (aid,))
    publish(adir, (clock.t + timedelta(milliseconds=offset_ms)).isoformat(timespec=precision))
    service._process_attempt(aid)
    service._process_attempt(aid)
    attempt = service.store.get_attempt(aid)
    assert service.store.get_job(jid)["state"] == ("succeeded" if natural else "cancelled")
    assert attempt["outcome_class"] == ("ok" if natural else "unknown")
    assert attempt["killed_by"] == (None if natural else "max_wall_s")


def test_unrecorded_signal_order_is_conservative_on_recovery(clocked, monkeypatch):
    service, _, clock, jid, aid, adir = setup_running(clocked, monkeypatch)
    wall_request(service, aid, jid)
    with service.store.transaction("attempt.kill_started", job_id=jid, attempt_id=aid) as tx:
        tx.execute("UPDATE attempts SET killed_by='max_wall_s' WHERE attempt_id=?", (aid,))
    publish(adir, (clock.t - timedelta(seconds=2)).isoformat())
    service._process_attempt(aid)
    service._process_attempt(aid)
    assert service.store.get_job(jid)["state"] == "cancelled"
    assert service.store.get_attempt(aid)["outcome_class"] == "unknown"


def test_deferred_wall_cascade_rolls_back_and_replays_with_parent(clocked, monkeypatch):
    service, harness, clock, jid, aid, adir = setup_running(clocked, monkeypatch)
    live, queued, _ = children(service, harness, clock, jid)
    wall_request(service, aid, jid)
    service._kill_attempt(service.store.get_attempt(aid))
    notice = service._notice
    def fail_parent_notice(tx, job, summary, **kwargs):
        if job["job_id"] == jid:
            raise RuntimeError("stop before terminal commit")
        return notice(tx, job, summary, **kwargs)
    monkeypatch.setattr(service, "_notice", fail_parent_notice)
    with pytest.raises(RuntimeError, match="stop before terminal commit"):
        service._process_attempt(aid)
    assert service.store.get_attempt(aid)["state"] == "finalizing"
    assert service.store.get_job(jid)["state"] == "running"
    assert all(service.store.get_job(child)["cancel_requested_at"] is None for child in (live, queued))
    monkeypatch.setattr(service, "_notice", notice)
    service._process_attempt(aid)
    assert service.store.get_job(jid)["state"] == "cancelled"
    assert service.store.get_job(queued)["state"] == "cancelled"
    assert service.store.get_job(live)["cancel_requested_at"]
