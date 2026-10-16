"""D-WALL1: a wall action requires inspected liveness, and loss ends at observed liveness."""
import json
from datetime import timedelta

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import daemon as daemon_module, procs
from tests.fake.test_review_r223_wall_probes import clocked, mark_starting  # noqa: F401
from tests.fake.test_state_contract import reserve, state_daemon  # noqa: F401


def success_receipt(service, jid, aid, finished_at):
    """Publish the guardian's evidence without advancing the daemon's row."""
    adir = service.root / "jobs" / jid / aid.rsplit("/", 1)[1]
    for name, contents in (("stdout", "verified result\n"), ("stderr", ""),
                           ("lane.log", ""), ("stream.jsonl", '{"type":"turn.completed"}\n')):
        (adir / name).write_text(contents)
    (adir / "exit.json").write_text(json.dumps({"rc": 0, "signal": None, "child_pid": 424243,
                                               "finished_at": finished_at}))


def receipt_interleaving(clocked, monkeypatch, stage, shared, exit_s, inspect_s, cap):
    service, clock, jid, aid = running(clocked, monkeypatch)
    service.store.update_job(jid, max_wall_s=cap)
    origin = clock.t
    published = False

    def publish():
        nonlocal published
        if not published:
            success_receipt(service, jid, aid, (origin + timedelta(seconds=exit_s)).isoformat())
            published = True

    live = procs.ProcessTable({424242: (1, 424242, "S", "fixture-start")}, "unit-test-boot")
    empty = procs.ProcessTable({}, "unit-test-boot")

    def inspect(due):
        clock.t = origin + timedelta(seconds=inspect_s)
        if stage == "inspection" and shared:
            publish()
        return (live if shared else empty), due + 1

    def snapshot():
        if stage == "inspection":
            publish()
        return live

    monkeypatch.setattr(service, "_process_table", inspect)
    monkeypatch.setattr(procs, "liveness", lambda *args: "alive")
    monkeypatch.setattr(procs, "snapshot", snapshot)
    monkeypatch.setattr(procs, "same_process", lambda *args: True)
    signals = []
    monkeypatch.setattr(procs, "signal_group", lambda *args, **kwargs: signals.append(args))
    monkeypatch.setattr(procs, "signal_process", lambda *args, **kwargs: signals.append(args))
    request_cancel = service._request_cancel

    def cancel(*args, **kwargs):
        result = request_cancel(*args, **kwargs)
        if stage == "cancel":
            publish()  # Between the last wall check and entry to the kill protocol.
        return result

    monkeypatch.setattr(service, "_request_cancel", cancel)
    contain = service._contain
    censuses = 0

    def census(a):
        nonlocal censuses
        censuses += 1
        if stage == "settle" and censuses == 3:
            publish()  # The receipt becomes visible only when termination settles.
        return contain(a)

    monkeypatch.setattr(service, "_contain", census)
    clock.t = origin + timedelta(seconds=exit_s)
    if stage == "initial":
        publish()
    service._process_attempt(aid)
    service._process_attempt(aid)
    job, attempt = service.store.get_job(jid), service.store.get_attempt(aid)
    assert published
    assert (job["state"], job["accepted_attempt_id"], job["rc"]) == ("succeeded", aid, 0)
    assert job["cancel_requested_at"] is None
    assert attempt["killed_by"] is None and attempt["signal"] is None and attempt["rc"] == 0
    assert service._cancel_info(jid) is None
    assert service._running_s(jid) == exit_s
    if stage in ("initial", "inspection"):
        assert signals == []
        assert not service.store.query("SELECT 1 FROM events WHERE job_id=? AND kind='job.cancel_requested'", (jid,))


@pytest.mark.parametrize("shared", [True, False], ids=["shared", "fresh"])
def test_round5_receipt_during_inspection_preserves_21599_second_success(clocked, monkeypatch, shared):
    receipt_interleaving(clocked, monkeypatch, "inspection", shared, 21599, 21601, 21600)


@pytest.mark.parametrize("stage", ["cancel", "settle"])
def test_round5_receipt_between_wall_check_and_kill_settlement_wins(clocked, monkeypatch, stage):
    receipt_interleaving(clocked, monkeypatch, stage, True, 21599, 21601, 21600)


@settings(max_examples=32, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(stage=st.sampled_from(["initial", "inspection", "cancel", "settle"]), shared=st.booleans(),
       cap=st.integers(2, 21600), before=st.integers(1, 10), after=st.integers(1, 10))
def test_round5_wall_receipt_interleavings(clocked, monkeypatch, stage, shared, cap, before, after):
    with monkeypatch.context() as patch:
        receipt_interleaving(clocked, patch, stage, shared, max(1, cap - before), cap + after, cap)


@pytest.mark.parametrize("state", ["starting", "running", "reserved"])
def test_round5_seven_hour_recovery_never_charges_an_absent_provider(clocked, monkeypatch, state):
    service, harness, clock = clocked
    jid, first, _ = reserve(service, harness)
    aid = first["attempt_id"]
    if state != "reserved":
        mark_starting(service, aid)
        service.store.update_attempt(aid, state=state, guardian_pid=424242, pgid=424242,
                                     boot_id="old-boot", proc_start="fixture-start")
    service.store.update_job(jid, max_wall_s=21600)
    clock.t += timedelta(hours=7)
    monkeypatch.setattr(service, "_inspect_running", daemon_module.Daemon._inspect_running.__get__(service))
    monkeypatch.setattr(service, "_process_table", lambda due: (procs.ProcessTable({}, "new-boot"), due + 1))
    monkeypatch.setattr(procs, "liveness", lambda *args: "dead")
    service._starting_deadlines[aid] = 0
    service._process_attempt(aid)
    assert service._running_s(jid) == 0
    clock.t += timedelta(seconds=61)
    service.store.update_job(jid, next_check_at=None)
    service._admit()
    retry = service.store.list_attempts(jid)[-1]
    assert retry["seq"] == 2 and retry["state"] == "reserved"
    mark_starting(service, retry["attempt_id"])
    service._pending_launches.discard(retry["attempt_id"])
    (service.root / "jobs" / jid / "a2").mkdir(exist_ok=True)
    monkeypatch.setattr(service, "_inspect_running", lambda *args: True)
    service._starting_deadlines[retry["attempt_id"]] = float("inf")
    clock.t += timedelta(seconds=1)
    service._process_attempt(retry["attempt_id"])
    assert service._running_s(jid) == 1
    assert service.store.get_job(jid)["cancel_requested_at"] is None


@settings(max_examples=20, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(down_s=st.integers(1, 7 * 3600), state=st.sampled_from(["starting", "running", "reserved"]))
def test_round5_wall_property_excludes_downtime(clocked, monkeypatch, down_s, state):
    service, harness, clock = clocked
    jid, attempt, _ = reserve(service, harness)
    aid = attempt["attempt_id"]
    if state != "reserved":
        mark_starting(service, aid)
        service.store.update_attempt(aid, state=state, guardian_pid=424242, pgid=424242,
                                     boot_id="old-boot", proc_start="fixture-start")
    clock.t += timedelta(seconds=down_s)
    monkeypatch.setattr(service, "_inspect_running", daemon_module.Daemon._inspect_running.__get__(service))
    monkeypatch.setattr(service, "_process_table", lambda due: (procs.ProcessTable({}, "new-boot"), due + 1))
    monkeypatch.setattr(procs, "liveness", lambda *args: "dead")
    service._starting_deadlines[aid] = 0
    service._process_attempt(aid)
    try:
        assert service._running_s(jid) == 0
    finally:
        service.dispatch("kill", {"job_id": jid})  # Release this example's queued retry.


def running(clocked, monkeypatch):
    service, harness, clock = clocked
    jid, a, _ = reserve(service, harness)
    mark_starting(service, a["attempt_id"])
    service.store.update_attempt(a["attempt_id"], state="running", guardian_pid=424242,
                                 pgid=424242, boot_id="unit-test-boot", proc_start="fixture-start")
    monkeypatch.setattr(service, "_inspect_running", daemon_module.Daemon._inspect_running.__get__(service))
    return service, clock, jid, a["attempt_id"]


def test_confirmed_liveness_is_durable_and_bounds_later_loss(clocked, monkeypatch):
    service, clock, jid, aid = running(clocked, monkeypatch)
    clock.t += timedelta(hours=1)
    table = procs.ProcessTable({424242: (1, 424242, "S", "fixture-start")}, "unit-test-boot")
    monkeypatch.setattr(service, "_process_table", lambda due: (table, due + 1))
    service._process_attempt(aid)
    observed = daemon_module.utcnow_ms()
    saved = json.loads(service.store.get_attempt(aid)["evidence_json"])
    assert saved["last_observed_alive_at"] == observed
    # A fresh daemon's first pass has no in-memory liveness or pacing history.
    service._inspect_next.clear()
    clock.t += timedelta(hours=6)
    monkeypatch.setattr(service, "_process_table", lambda due: (procs.ProcessTable({}, "new-boot"), due + 1))
    monkeypatch.setattr(procs, "liveness", lambda *args: "dead")
    service._process_attempt(aid)
    assert service.store.get_attempt(aid)["finished_at"] == observed
    assert service.store.get_attempt(aid)["state"] == "lost"
    assert service.store.get_job(jid)["state"] == "waiting"
    assert service._running_s(jid) == 3600


@pytest.mark.parametrize("answer", ["unknown", "dead", "alive"])
def test_only_confirmed_running_provider_can_be_wall_cancelled(clocked, monkeypatch, answer):
    service, clock, jid, aid = running(clocked, monkeypatch)
    service.store.update_job(jid, max_wall_s=3600)
    clock.t += timedelta(hours=7)
    empty = procs.ProcessTable({}, "unit-test-boot")
    live = procs.ProcessTable({424242: (1, 424242, "S", "fixture-start")}, "unit-test-boot")
    monkeypatch.setattr(service, "_process_table", lambda due: (empty, due + 1))
    monkeypatch.setattr(procs, "liveness", lambda *args: answer)
    monkeypatch.setattr(procs, "snapshot", lambda: live)
    killed = []
    monkeypatch.setattr(service, "_kill_attempt", lambda a: killed.append(a["attempt_id"]))
    service._process_attempt(aid)
    job = service.store.get_job(jid)
    assert bool(job["cancel_requested_at"]) == (answer == "alive")
    assert killed == ([aid] if answer == "alive" else [])
    if answer == "dead":
        assert service.store.get_attempt(aid)["state"] == "lost"
        assert service._running_s(jid) == 0  # Legacy row: only the launch is known alive.
        assert job["state"] == "waiting"
    elif answer == "unknown":
        assert service.store.get_attempt(aid)["state"] == "running"
