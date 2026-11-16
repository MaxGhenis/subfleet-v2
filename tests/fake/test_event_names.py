"""C-3.8: an admission pass's event is named for what it did, and a repeated wait records nothing.

Incident (D-RESERVED-NOISE1, 2026-11-11): the newest 20,000 `attempt.reserved`
events in the live store spanned 02:05:33Z-05:00:34Z, every one with a NULL
`attempt_id` and data `{}`, across 111 jobs, while no job ran (230 waiting,
0 running): about 6,900 events an hour into a 9.84 GB store. Every pass that
left a job waiting recorded one, because admission's transaction opened as
`attempt.reserved` and its wait's UPDATE counts in `total_changes` even when it
writes the values the row already holds.
"""

import json

import pytest

from subfleet.adapters.registry import register
from subfleet.contracts import ClockSource, Closure, ClosureReason, Reading, ReadingLabel
from subfleet.daemon import ADMISSION_EVENT, RESERVATION_EVENT, Daemon, after, utcnow
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter
from tests.fake.test_capacity_wait_backoff import fleet  # noqa: F401  (fixture)
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)
from tests.fake.test_state_contract import state_daemon  # noqa: F401  (fixture)


@pytest.fixture
def service(tmp_path, monkeypatch, process_inspection_available):
    """A real daemon core on a temp state root with one measured Codex lane; passes are driven by hand."""
    root = tmp_path / "state"
    root.mkdir()
    harness = Harness(root)
    register("codex", FakeAdapter)
    daemon = Daemon(harness.root, desktop_prober=lambda: None)
    monkeypatch.setattr(daemon, "_launch", lambda *args: pytest.fail("nothing here may launch a guardian"))
    daemon.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                     ReadingLabel.PROVIDER, "fixture", utcnow()))
    try:
        yield daemon, harness
    finally:
        daemon.close()


def events(daemon, job_id):
    return [(row["kind"], row["attempt_id"], row["lane_id"]) for row in daemon.store.list_events(job_id)]


def due(daemon):
    with daemon.store.transaction("test.due") as tx:
        tx.execute("UPDATE jobs SET next_check_at=? WHERE state='waiting'", (after(-1),))


def close_lane(daemon):
    daemon.store.put_closure(Closure("codex-1", "account", after(3600), ClosureReason.PROVIDER_LIMIT,
                                     ClockSource.REPORTED, "fixture"))


def test_c3_8_repro_passes_with_no_lane_record_no_empty_reservation(service):
    """C-3.8 repro (D-RESERVED-NOISE1): several looks at a job no lane will take record no
    `attempt.reserved` event without an attempt id."""
    daemon, harness = service
    close_lane(daemon)
    job = daemon.dispatch("submit", harness.submit_args())["job_id"]
    for _ in range(5):
        daemon._admit()
        due(daemon)
    assert daemon.store.list_attempts(job) == []
    assert daemon.store.get_job(job)["state"] == "waiting"
    empty = [row for row in events(daemon, job) if row[0] == "attempt.reserved" and row[1] is None]
    assert empty == [], f"{len(empty)} empty reservations over 5 looks"


def kinds(daemon, job_id):
    return [kind for kind, _aid, _lane in events(daemon, job_id)]


def assert_reservations_are_real(daemon):
    """Invariant 1: every `attempt.reserved` names an attempt of its job, on that attempt's lane."""
    for row in daemon.store.query("SELECT * FROM events WHERE kind=?", (RESERVATION_EVENT,)):
        assert row["attempt_id"] is not None, dict(row)
        attempt = daemon.store.one("SELECT job_id, lane_id FROM attempts WHERE attempt_id=?", (row["attempt_id"],))
        assert attempt is not None and attempt["job_id"] == row["job_id"], dict(row)
        assert attempt["lane_id"] == row["lane_id"], dict(row)


def test_c3_8_the_admission_name_is_the_contract_s():
    """C-3.8 the constant tests and tools key on names what the contract names."""
    assert (ADMISSION_EVENT, RESERVATION_EVENT) == ("job.capacity_waiting", "attempt.reserved")


def test_c3_8_a_pass_that_leaves_a_job_waiting_records_the_wait_not_a_reservation(service):
    """C-3.8, C-3.2, C-6.3 a job with no lane records `job.capacity_waiting`, never `attempt.reserved`."""
    daemon, harness = service
    close_lane(daemon)
    job = daemon.dispatch("submit", harness.submit_args())["job_id"]
    for _ in range(3):
        daemon._admit()
        due(daemon)
    assert daemon.store.list_attempts(job) == []
    assert ADMISSION_EVENT in kinds(daemon, job) and RESERVATION_EVENT not in kinds(daemon, job)
    assert daemon.store.get_job(job)["wait_reason"] == "capacity"


def test_c3_8_a_reservation_is_named_and_carries_its_attempt_and_lane(service):
    """C-3.8, C-6.3 the one event that says `attempt.reserved` names the attempt it reserved and its lane."""
    daemon, harness = service
    job = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon._admit()
    attempt = daemon.store.list_attempts(job)[-1]
    reserved = [row for row in events(daemon, job) if row[0] == RESERVATION_EVENT]
    assert reserved == [(RESERVATION_EVENT, attempt["attempt_id"], "codex-1")]
    assert ADMISSION_EVENT not in kinds(daemon, job)
    assert_reservations_are_real(daemon)


def test_c3_8_a_full_fleet_names_the_wait_it_records(service):
    """C-3.8, C-6.9 the job that finds the fleet full records a wait, and says so."""
    daemon, harness = service
    daemon.policy["caps"]["max_active_attempts"] = 1
    daemon.dispatch("submit", harness.submit_args())
    daemon._admit()
    second = daemon.dispatch("submit", harness.submit_args(tier="hard"))["job_id"]
    daemon._admit()
    assert daemon.store.get_job(second)["state"] == "waiting"
    assert ADMISSION_EVENT in kinds(daemon, second) and RESERVATION_EVENT not in kinds(daemon, second)
    assert_reservations_are_real(daemon)


def test_c3_8_a_held_lease_records_the_wait(fleet):  # noqa: F811
    """C-3.8, C-6.5 a job whose output path another job holds records `job.capacity_waiting`, never a reservation."""
    service, harness = fleet
    out = str(harness.root / "report.md")
    waiting = service.dispatch("submit", harness.submit_args(pinned_model="terra", out_path=out))["job_id"]
    with service.store.transaction("fixture.lease") as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                   (f"out:{out}", "some-other-job", utcnow()))
    service._admit()
    assert service._capacity_waits[waiting]["label"] == "lease-held"
    assert ADMISSION_EVENT in kinds(service, waiting) and RESERVATION_EVENT not in kinds(service, waiting)
    assert service.store.list_attempts(waiting) == []
    assert_reservations_are_real(service)


def test_c3_8_a_pair_that_changed_after_its_probe_records_the_wait(fleet, monkeypatch):  # noqa: F811
    """C-3.8, C-6.12 a chosen pair that is no longer the probed one records `job.capacity_waiting`."""
    from subfleet import daemon as daemon_module
    service, harness = fleet
    job_id = service.dispatch("submit", harness.submit_args(pinned_model="astra"))["job_id"]
    monkeypatch.setattr(daemon_module.scheduler, "probe_required", lambda decision, job: True)
    # The probe approved nothing, so whatever pair the reserving transaction chooses has changed since.
    monkeypatch.setattr(service, "_prepare_route",
                        lambda job, decision_job, exclusions: (set(), service._desktop_identity()))
    service._admit()
    assert service._holds[job_id]["reason"] == "probe-pending"
    assert ADMISSION_EVENT in kinds(service, job_id) and RESERVATION_EVENT not in kinds(service, job_id)
    assert service.store.list_attempts(job_id) == []
    data = [row for row in service.store.list_events(job_id) if row["kind"] == ADMISSION_EVENT]
    hold = service._holds[job_id]
    assert json.loads(data[-1]["data_json"]) == {
        "reason": "probe-pending", "next_check_at": service.store.get_job(job_id)["next_check_at"],
        **{key: hold[key] for key in ("lane", "model") if hold.get(key)}}


# --- C-3.8: a repeated wait records nothing ---------------------------------------------------

def waits(daemon, job_id):
    return [json.loads(row["data_json"]) for row in daemon.store.list_events(job_id) if row["kind"] == ADMISSION_EVENT]


class Restartable:
    """The service fixture's daemon, which a test may stop and start again on the same root."""

    def __init__(self, harness, monkeypatch):
        self.harness, self.monkeypatch, self.daemon = harness, monkeypatch, None
        self.start()

    def start(self):
        self.daemon = Daemon(self.harness.root, desktop_prober=lambda: None)
        self.monkeypatch.setattr(self.daemon, "_launch", lambda *args: pytest.fail("nothing here may launch a guardian"))
        return self.daemon

    def restart(self):
        self.daemon.close()
        return self.start()


@pytest.fixture
def restartable(tmp_path, monkeypatch, process_inspection_available):
    root = tmp_path / "state"
    root.mkdir()
    harness = Harness(root)
    register("codex", FakeAdapter)
    holder = Restartable(harness, monkeypatch)
    holder.daemon.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                            ReadingLabel.PROVIDER, "fixture", utcnow()))
    try:
        yield holder
    finally:
        holder.daemon.close()


def test_c3_8_a_hundred_looks_at_one_verdict_record_one_wait(service):
    """C-3.8, C-6.10 a hundred passes that reach one verdict record one `job.capacity_waiting`.

    Its data is the hold's reason and the next look, never the decision (its row holds
    that). Before C-3.8 each look recorded an `attempt.reserved` with no attempt id."""
    daemon, harness = service
    close_lane(daemon)
    job = daemon.dispatch("submit", harness.submit_args())["job_id"]
    for _ in range(100):
        daemon._admit()
        due(daemon)
    assert daemon.store.get_job(job)["state"] == "waiting" and daemon.store.list_attempts(job) == []
    recorded = waits(daemon, job)
    assert len(recorded) == 1, recorded
    assert set(recorded[0]) == {"reason", "next_check_at"}, recorded
    assert RESERVATION_EVENT not in kinds(daemon, job)
    assert len(daemon.store.list_decisions(job)) == 1        # C-6.10: one decision row, as one event


def test_c3_8_a_new_verdict_records_a_new_wait_and_a_reservation_ends_them(restartable):
    """C-3.8, C-6.10, C-6.5 a closed lane, then an output another job holds, then a free lane:
    two waits (one per verdict, each recorded once), then one real reservation."""
    daemon, harness = restartable.daemon, restartable.harness
    close_lane(daemon)
    out = str(harness.root / "report.md")
    job = daemon.dispatch("submit", harness.submit_args(out_path=out))["job_id"]
    for _ in range(3):
        daemon._admit()
        due(daemon)
    with daemon.store.transaction("fixture.reopen") as tx:
        tx.execute("DELETE FROM closures")
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                   (f"out:{out}", "some-other-job", utcnow()))
    for _ in range(3):
        daemon._admit()
        due(daemon)
    assert [data["reason"] for data in waits(daemon, job)][1:] == ["lease-held"]
    assert len(waits(daemon, job)) == 2
    with daemon.store.transaction("fixture.free") as tx:
        tx.execute("DELETE FROM leases WHERE holder='some-other-job'")
    daemon._admit()
    attempt = daemon.store.list_attempts(job)[-1]
    assert [row for row in events(daemon, job) if row[0] == RESERVATION_EVENT] == [
        (RESERVATION_EVENT, attempt["attempt_id"], "codex-1")]
    assert len(waits(daemon, job)) == 2
    assert_reservations_are_real(daemon)


def test_c3_8_a_restart_forgets_the_verdict_so_each_waiting_job_records_one_more(restartable):
    """C-3.8, C-6.10 the record of the last verdict is memory: after a restart the first look
    records the wait again, once, and the looks after it record nothing."""
    close_lane(restartable.daemon)
    job = restartable.daemon.dispatch("submit", restartable.harness.submit_args())["job_id"]
    for _ in range(4):
        restartable.daemon._admit()
        due(restartable.daemon)
    assert len(waits(restartable.daemon, job)) == 1
    daemon = restartable.restart()
    for _ in range(4):
        daemon._admit()
        due(daemon)
    assert len(waits(daemon, job)) == 2
    assert RESERVATION_EVENT not in kinds(daemon, job)


def test_c3_8_a_wait_that_changes_the_job_s_exclusions_records_it(fleet):  # noqa: F811
    """C-3.8, C-4.5 a look under a repeated verdict that writes new `exclusions` records the
    wait; the next, writing the same exclusions again, records nothing."""
    service, harness = fleet
    service.store.put_closure(Closure("codex-1", "account", after(3600), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))
    job = service.dispatch("submit", harness.submit_args(pinned_model="astra", pinned_lane="codex-1"))["job_id"]
    service._admit()
    due(service)
    service._admit()
    due(service)
    assert len(waits(service, job)) == 1
    # A limited attempt on another lane: C-4.5 excludes that lane from the job from now on.
    from tests.fake.test_admission_route_isolation import claude_lane
    service.store.put_lane(claude_lane("claude-a"))
    service.store.add_attempt(attempt_id=job + "/a1", job_id=job, seq=1, lane_id="claude-a",
                              model_requested="claude-opus", state="failed", outcome_class="limited")
    before = service.store.get_job(job)["exclusions"]
    service._admit()
    due(service)
    after_look = service.store.get_job(job)
    assert after_look["exclusions"] != before and "claude-a" in json.loads(after_look["exclusions"])
    assert after_look["state"] == "waiting" and service._capacity_waits[job]["rechecks"] >= 1
    assert len(waits(service, job)) == 2
    service._admit()
    due(service)
    assert len(waits(service, job)) == 2


# --- C-3.8: arms whose nested writes name them (task-held, inside admission's transaction) ----

@pytest.fixture
def held_task(state_daemon, monkeypatch):  # noqa: F811
    from subfleet import daemon as daemon_module
    from tests.fake.test_admission_ramp import fake_clock
    monkeypatch.setattr(daemon_module, "git_head", lambda *args, **kwargs: None)
    monkeypatch.setattr(daemon_module, "git_toplevel", lambda *args, **kwargs: None)
    fake_clock(monkeypatch)
    service, harness = state_daemon
    return service, harness


def admission_task_held(service, job_id):
    """Admission's task-held arm (C-11.10, C-6.3), as its transaction runs it: the hold read
    at the transaction's clock, then `_task_held(..., tx=tx)`. The events it left for the job."""
    mark = service.store.one("SELECT COALESCE(MAX(event_id), 0) AS id FROM events")["id"]
    with service.store.transaction(ADMISSION_EVENT, job_id=job_id) as tx:
        row = service._job(job_id)
        found = service._task_hold_for(row, service._task_holds_now(lambda sql, params: tx.execute(sql, params).fetchall()))
        assert found is not None
        service._task_held(row, found, {}, tx=tx)
    return [(row["kind"], json.loads(row["data_json"])) for row in service.store.query(
        "SELECT kind, data_json FROM events WHERE job_id=? AND event_id>? ORDER BY event_id", (job_id, mark))]


def test_c3_8_a_take_inside_admission_records_only_the_take_s_task_held(held_task, monkeypatch):
    """C-3.8, C-11.10 admission's transaction that takes the job for a hold records the take's
    own `job.task_held` row and nothing of its own (neither admission name)."""
    from tests.disk_floor_model import stamp
    from tests.task_hold_support import hold, submit
    service, harness = held_task
    build = submit(service, harness, "build", "build-1")
    monkeypatch.setattr(service, "_take_task_jobs", lambda *args, **kwargs: None)    # the hold's commit took nothing
    hold(service, "build", stamp(3600))
    recorded = admission_task_held(service, build)
    assert [kind for kind, _ in recorded] == ["job.task_held"], recorded
    assert recorded[0][1]["taken"] is True
    assert service.store.get_job(build)["wait_reason"] == "task-held"


def test_c3_8_a_moved_hold_end_inside_admission_records_task_hold_moved(held_task, monkeypatch):
    """C-3.8, C-11.10 admission's transaction that only moves a held job's end records
    `job.task_hold_moved` with the task and the new end, as `_task_held_at_write` names it."""
    from tests.disk_floor_model import stamp
    from tests.task_hold_support import hold, submit
    service, harness = held_task
    build = submit(service, harness, "build", "build-1")
    hold(service, "build", stamp(3600))                         # its commit takes the job
    assert service.store.get_job(build)["next_check_at"] == stamp(3600)
    monkeypatch.setattr(service, "_take_task_jobs", lambda *args, **kwargs: None)
    hold(service, "build", stamp(7200))                         # a replacement
    service.store.update_job(build, next_check_at=stamp(3600))   # ... whose end the job has not seen yet
    recorded = admission_task_held(service, build)
    assert recorded == [("job.task_hold_moved", {"task": "build", "until": stamp(7200)})], recorded
    assert service.store.get_job(build)["next_check_at"] == stamp(7200)
