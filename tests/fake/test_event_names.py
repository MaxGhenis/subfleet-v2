"""C-3.8: an admission pass's event is named for what it did, and a repeated wait records nothing.

Incident (D-RESERVED-NOISE1, 2026-11-11): the newest 20,000 `attempt.reserved`
events in the live store spanned 02:05:33Z-05:00:34Z, every one with a NULL
`attempt_id` and data `{}`, across 111 jobs, while no job ran (230 waiting,
0 running): about 6,900 events an hour into a 9.84 GB store. Every pass that
left a job waiting recorded one, because admission's transaction opened as
`attempt.reserved` and its wait's UPDATE counts in `total_changes` even when it
writes the values the row already holds.
"""

import pytest

from subfleet.adapters.registry import register
from subfleet.contracts import ClockSource, Closure, ClosureReason, Reading, ReadingLabel
from subfleet.daemon import ADMISSION_EVENT, RESERVATION_EVENT, Daemon, after, utcnow
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter
from tests.fake.test_capacity_wait_backoff import fleet  # noqa: F401  (fixture)
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)


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
