"""Independent #269 review: whole-second clocks, queued jobs, and durable recovery."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import daemon as daemon_module, doctor, procs
from subfleet.contracts import Outcome, OutcomeClass
from subfleet.daemon import Daemon
from subfleet.policy import PolicyError, load_policy
from tests.fake.test_probe_deadline import run_probe, stamp
from tests.fake.test_probe_turn import fleet
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401

#: D-PROBE-DEADLINE1 r2: the loader's refusal of any deadline but whole seconds 1 to 900.
REFUSED = "admission.probe_deadline_s: must be a whole number of seconds from 1 to 900"


class SimulatedCrash(RuntimeError):
    pass


def reserve_running_probe(service, harness, monkeypatch, seconds):
    """Reserve through admission, then interrupt before any provider is started."""
    service.policy["admission"]["probe_deadline_s"] = seconds
    job_id = service.dispatch("submit", harness.submit_args(tier="hard"))["job_id"]
    records = []

    def crash(job, lane, model, holder):
        record = service._probe_record(holder)
        record.update(state="starting", guardian_pid=900001, pgid=900001,
                      boot_id="fixture-boot", proc_start="fixture-start")
        service._save_probe(record)
        records.append(record)
        raise SimulatedCrash

    monkeypatch.setattr(service, "_execute_probe", crash)
    with pytest.raises(SimulatedCrash):
        service._admit()
    record, = records
    assert record["job_id"] == job_id
    return record


def supervise_with_fake_time(service, monkeypatch, clock, *, tick=1):
    # Match production utcnow's whole-second format, including at fractional times.
    monkeypatch.setattr(daemon_module, "utcnow",
                        lambda: stamp(clock["now"].replace(microsecond=0)))
    monkeypatch.setattr(daemon_module, "after",
                        lambda seconds: stamp((clock["now"] + timedelta(seconds=seconds)).replace(microsecond=0)))
    monkeypatch.setattr(service.stopping, "wait",
                        lambda seconds: clock.update(now=clock["now"] + timedelta(seconds=tick)))
    monkeypatch.setattr(procs, "same_process", lambda *args: True)
    monkeypatch.setattr(procs, "liveness", lambda *args: "alive")
    monkeypatch.setattr(service, "_new_group_identities", lambda *args: {})
    monkeypatch.setattr(service, "_probe_census", lambda record: procs.Containment())


@pytest.mark.parametrize("seconds", [180, 900])
def test_restart_with_shorter_policy_keeps_reserved_probe_deadline(routing_state, monkeypatch, seconds):
    """C-11.4: a new daemon at t=75 retains a reservation made under 180/900."""
    service, harness = routing_state
    start = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    clock = {"now": start}
    supervise_with_fake_time(service, monkeypatch, clock)
    original = reserve_running_probe(service, harness, monkeypatch, seconds)
    service.close()
    policy_path = harness.root / "policy.json"
    policy = json.loads(policy_path.read_text())
    policy["admission"]["probe_deadline_s"] = 60
    policy_path.write_text(json.dumps(policy))
    clock["now"] = start + timedelta(seconds=75)

    successor = Daemon(harness.root)
    try:
        supervise_with_fake_time(successor, monkeypatch, clock)
        assert successor.policy["admission"]["probe_deadline_s"] == 60
        successor._recover_probes()
        recovered = successor._probe_record(original["holder"])
        assert recovered["deadline_at"] == original["deadline_at"] == stamp(start + timedelta(seconds=seconds))
        assert clock["now"] == start + timedelta(seconds=seconds)
        assert recovered["state"] == "completed"
        assert recovered["outcome"]["evidence"]["stopped"] == "deadline"
        assert not successor.store.list_leases(original["holder"])
        assert not Path(original["directory"]).exists()
    finally:
        successor.close()


@pytest.mark.parametrize("deadline_s", [180, 900])
def test_job_behind_150_second_probe_waits_then_carries_its_own_probe(routing_state, monkeypatch, deadline_s):
    """C-6.9: the follower reaches its own probe only after the first one's 150 s."""
    service, harness = routing_state
    dispatch, execute, prepare = service.dispatch, service._execute_probe, service._prepare_route
    jobs, looked_at, snapshots, probes = [], [], [], []

    def submit_pair(verb, args):
        result = dispatch(verb, args)
        if verb == "submit" and not jobs:
            jobs.append(result["job_id"])
            jobs.append(dispatch(verb, harness.submit_args(tier="hard"))["job_id"])
        return result

    def preparing(job, *args):
        looked_at.append((job["job_id"], daemon_module.utcnow()))
        return prepare(job, *args)

    def watching(job, lane, model, holder):
        probes.append((job["job_id"], daemon_module.utcnow()))
        if job["job_id"] == jobs[1]:
            # Admission observations are not usage measurements: the hard
            # follower needs its own probe. Script this second answer as instant.
            return Outcome(OutcomeClass.OK, "instant fake follower probe", {"rc": 0, "signal": None})
        created = datetime.fromisoformat(service._probe_record(holder)["created_at"])
        wait = service.stopping.wait

        def advance(seconds):
            wait(seconds)
            elapsed = (datetime.fromisoformat(daemon_module.utcnow()) - created).total_seconds()
            if elapsed in (60, 120):
                assert not service.store.list_attempts(jobs[1])
                assert all(job_id == jobs[0] for job_id, _ in looked_at)
                status = dispatch("daemon.status", {})["status"]
                why = dispatch("why", {"job_id": jobs[1]})["text"]
                assert "probe=starting" in status
                assert "no admission pass has reached this job yet" in why
                assert "no-slot" in why
                snapshots.append(elapsed)

        monkeypatch.setattr(service.stopping, "wait", advance)
        return execute(job, lane, model, holder)

    monkeypatch.setattr(service, "dispatch", submit_pair)
    monkeypatch.setattr(service, "_prepare_route", preparing)
    monkeypatch.setattr(service, "_execute_probe", watching)
    first, record, now = run_probe(service, harness, monkeypatch, deadline_s=deadline_s, answer_s=150)
    assert first == jobs[0]
    assert record["outcome"]["cls"] == "ok"
    assert snapshots == [60, 120]
    follower_look, = [at for job_id, at in looked_at if job_id == jobs[1]]
    assert follower_look == stamp(now)
    assert [row["job_id"] for row in service.store.query("SELECT job_id FROM attempts ORDER BY rowid")] == jobs
    assert probes == [(jobs[0], record["created_at"]), (jobs[1], stamp(now))]
    assert [row["job_id"] for row in service.store.query(
        "SELECT job_id FROM events WHERE kind='probe.reserved' ORDER BY event_id")] == jobs


def test_half_second_probe_does_not_expire_at_its_reservation(routing_state, monkeypatch):
    """#269 r1, P2: a 0.5 s deadline (`...00.500000Z`) sorted before `utcnow()`'s `...00Z`
    and stopped its probe at t=0. Policy load now refuses it; the shortest deadline that
    loads, 1 s, stops its probe at t=1 on the same 0.05 s fake clock, not at its reservation."""
    service, harness = routing_state
    policy_path = harness.root / "policy.json"
    policy = json.loads(policy_path.read_text())
    policy["admission"]["probe_deadline_s"] = 0.5
    policy_path.write_text(json.dumps(policy))
    with pytest.raises(PolicyError) as refused:
        load_policy(policy_path)
    assert str(refused.value).endswith(REFUSED)
    policy["admission"]["probe_deadline_s"] = 1
    policy_path.write_text(json.dumps(policy))
    service.policy = load_policy(policy_path)
    start = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    clock = {"now": start}
    supervise_with_fake_time(service, monkeypatch, clock, tick=0.05)
    record = reserve_running_probe(service, harness, monkeypatch, 1)
    safe, receipt = service._await_probe(record)
    assert safe and receipt is None
    assert record["deadline_at"] == stamp(start + timedelta(seconds=1))
    assert record["stopped"] == "deadline"
    assert clock["now"] - start == timedelta(seconds=1)


@settings(max_examples=30, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.too_slow])
@example(deadline_s=1, tick=0.05, reserved_at=datetime(2026, 10, 26, 12, tzinfo=timezone.utc))
@example(deadline_s=900, tick=0.05, reserved_at=datetime(2026, 10, 26, 23, 59, 59, tzinfo=timezone.utc))
@example(deadline_s=180, tick=7, reserved_at=datetime(2026, 12, 31, 23, 59, 58, tzinfo=timezone.utc))
@given(deadline_s=st.integers(1, 900), tick=st.sampled_from([0.05, 0.3, 1, 7]),
       reserved_at=st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2099, 1, 1),
                                timezones=st.just(timezone.utc)).map(lambda at: at.replace(microsecond=0)))
def test_no_probe_stops_before_its_deadline_or_later_than_one_tick_after(deadline_s, tick, reserved_at):
    """C-11.4, D-PROBE-DEADLINE1 r2: for every deadline policy load accepts (whole seconds
    1 to 900) and every whole-second reservation, no probe is stopped before `created_at +
    deadline`, and every probe is stopped by then plus one tick. A tick is one supervision
    pass of `_await_probe` on the fake clock (production waits 0.05 s between passes,
    each reading `utcnow()` at whole seconds)."""
    with fleet() as (service, harness, patch):
        policy_path = harness.root / "policy.json"
        policy = json.loads(policy_path.read_text())
        policy["admission"]["probe_deadline_s"] = deadline_s
        policy_path.write_text(json.dumps(policy))
        service.policy = load_policy(policy_path)
        clock = {"now": reserved_at}
        supervise_with_fake_time(service, patch, clock, tick=tick)
        record = reserve_running_probe(service, harness, patch, deadline_s)
        created = datetime.fromisoformat(record["created_at"])
        deadline = created + timedelta(seconds=deadline_s)
        assert created == reserved_at
        assert record["deadline_at"] == stamp(deadline)
        safe, receipt = service._await_probe(record)
        assert safe and receipt is None
        assert record["stopped"] == "deadline"
        assert deadline <= clock["now"] <= deadline + timedelta(seconds=tick)


def test_legacy_policy_loads_and_doctor_reports_fractional_and_zero_deadlines(routing_state, monkeypatch):
    """Doctor's provider row uses policy validation; it does not dump admission settings.
    D-PROBE-DEADLINE1 r2: 0.5 and 180.0 are refused with 0 (#269 r1, P2)."""
    _, harness = routing_state
    policy_path = harness.root / "policy.json"
    policy = json.loads(policy_path.read_text())
    policy["admission"].pop("probe_deadline_s", None)
    policy_path.write_text(json.dumps(policy))
    assert json.loads(json.dumps(load_policy(policy_path)))["admission"]["probe_deadline_s"] == 180
    monkeypatch.setattr(doctor.shutil, "which", lambda binary: "/fake/codex")
    monkeypatch.setattr(doctor, "_run", lambda *args, **kwargs: (0, "fake version"))
    assert doctor.check_provider("codex", root=harness.root)["status"] == doctor.PASS
    for refused in (0.5, 180.0, 0):
        policy["admission"]["probe_deadline_s"] = refused
        policy_path.write_text(json.dumps(policy))
        with pytest.raises(PolicyError) as caught:
            load_policy(policy_path)
        assert str(caught.value).endswith(REFUSED)
        row = doctor.check_provider("codex", root=harness.root)
        assert row["status"] == doctor.FAIL
        assert REFUSED in doctor.render([row])
