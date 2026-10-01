"""C-3.8: an admission pass's event is named for what it did.

Incident, 2026-09-20: every pass that left a job waiting wrote an `attempt.reserved`
event with no attempt id, 97,554 of them beside 193 real reservations, and only
the order of the rows could tell the two apart. On 2026-09-24 the live store still
gained about 1,780 a day against 61 attempts reserved.
"""

import pytest

from subfleet.adapters.registry import register
from subfleet.contracts import ClockSource, Closure, ClosureReason, Reading, ReadingLabel
from subfleet.daemon import Daemon, after, utcnow
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


def test_c3_8_a_pass_that_leaves_a_job_waiting_records_the_wait_not_a_reservation(service):
    """C-3.8, C-3.2, C-6.3 a job with no lane records `job.capacity_waiting`, never `attempt.reserved`."""
    daemon, harness = service
    daemon.store.put_closure(Closure("codex-1", "account", after(3600), ClosureReason.PROVIDER_LIMIT,
                                     ClockSource.REPORTED, "fixture"))
    job = daemon.dispatch("submit", harness.submit_args())["job_id"]
    for _ in range(3):
        daemon._admit()
        due(daemon)
    kinds = [kind for kind, _aid, _lane in events(daemon, job)]
    assert daemon.store.list_attempts(job) == []
    assert "job.capacity_waiting" in kinds and "attempt.reserved" not in kinds
    assert daemon.store.get_job(job)["wait_reason"] == "capacity"


def test_c3_8_a_reservation_is_named_and_carries_its_attempt_and_lane(service):
    """C-3.8, C-6.3 the one event that says `attempt.reserved` names the attempt it reserved and its lane."""
    daemon, harness = service
    job = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon._admit()
    attempt = daemon.store.list_attempts(job)[-1]
    reserved = [row for row in events(daemon, job) if row[0] == "attempt.reserved"]
    assert reserved == [("attempt.reserved", attempt["attempt_id"], "codex-1")]
    assert "job.capacity_waiting" not in [kind for kind, _aid, _lane in events(daemon, job)]


def test_c3_8_a_full_fleet_names_the_wait_it_records(service):
    """C-3.8, C-6.9 the job that finds the fleet full records a wait, and says so."""
    daemon, harness = service
    daemon.policy["caps"]["max_active_attempts"] = 1
    daemon.dispatch("submit", harness.submit_args())
    daemon._admit()
    second = daemon.dispatch("submit", harness.submit_args(tier="hard"))["job_id"]
    daemon._admit()
    kinds = [kind for kind, _aid, _lane in events(daemon, second)]
    assert daemon.store.get_job(second)["state"] == "waiting"
    assert "job.capacity_waiting" in kinds and "attempt.reserved" not in kinds


def test_c3_8_a_held_lease_records_the_wait(fleet):
    """C-3.8, C-6.5 a job whose output path another job holds records `job.capacity_waiting`, never a reservation."""
    service, harness = fleet
    out = str(harness.root / "report.md")
    waiting = service.dispatch("submit", harness.submit_args(pinned_model="terra", out_path=out))["job_id"]
    with service.store.transaction("fixture.lease") as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                   (f"out:{out}", "some-other-job", utcnow()))
    service._admit()
    assert service._capacity_waits[waiting]["label"] == "lease-held"
    kinds = [kind for kind, _aid, _lane in events(service, waiting)]
    assert "job.capacity_waiting" in kinds and "attempt.reserved" not in kinds
    assert service.store.list_attempts(waiting) == []


def test_c3_8_a_pair_that_changed_after_its_probe_records_the_wait(fleet, monkeypatch):
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
    kinds = [kind for kind, _aid, _lane in events(service, job_id)]
    assert "job.capacity_waiting" in kinds and "attempt.reserved" not in kinds
    assert service.store.list_attempts(job_id) == []
