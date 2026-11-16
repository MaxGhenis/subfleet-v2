"""D-ADMIT-IDLE1: an evaluated unprepared route resets consecutive errors."""

from datetime import datetime

import pytest

from subfleet.daemon import utcnow
from tests.fake.test_capacity_wait_backoff import submit
from tests.fake.test_idle_capacity_events import clock, held_fleet, release  # noqa: F401
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401
from tests.unit.test_salvage import git


def retry_delay(service, job):
    due = datetime.fromisoformat(service.store.get_job(job)["next_check_at"].replace("Z", "+00:00"))
    now = datetime.fromisoformat(utcnow().replace("Z", "+00:00"))
    return (due - now).total_seconds()


def errors_separated_by_success(held_fleet, monkeypatch, unprepared):
    service, harness, closure, advance = held_fleet
    if unprepared:
        git(harness.workdir, "init", "-b", "task/route-reset")
        git(harness.workdir, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "--allow-empty", "-m", "baseline")
    job = submit(service, harness, pinned_model="terra",
                 **({"sandbox": "workspace-write"} if unprepared else {}))
    failing = [True]
    pick = service._pick

    def sometimes_broken(candidate, **options):
        if candidate["job_id"] == job and failing[0]:
            raise ValueError("synthetic route reading failure")
        return pick(candidate, **options)

    monkeypatch.setattr(service, "_pick", sometimes_broken)
    service._admit()
    assert service.store.get_job(job)["wait_reason"] == "route"
    assert retry_delay(service, job) == 5
    advance(5)
    failing[0] = False
    service._admit()
    assert service.store.get_job(job)["wait_reason"] == "capacity"
    assert not service.store.list_attempts(job)
    assert service._capacity_waits[job]["hold"].get("unprepared", False) is unprepared
    advance(1)
    failing[0] = True
    service._admit()
    assert service.store.get_job(job)["wait_reason"] == "route"
    return service, job, closure, advance, failing


@pytest.mark.parametrize("unprepared", [False, True])
def test_successful_route_resets_prior_error_count(held_fleet, monkeypatch, unprepared):
    """A successful capacity verdict separates two errors, each with a 5 s retry."""
    service, job, _, _, _ = errors_separated_by_success(held_fleet, monkeypatch, unprepared)
    assert retry_delay(service, job) == 5
    assert service._route_deferrals[job]["deferrals"] == 1


@pytest.mark.parametrize("unprepared", [False, True])
def test_intervening_success_should_not_delay_start_after_release(held_fleet, monkeypatch, unprepared):
    """Once the hold lifts, an isolated route error costs only its first retry."""
    service, job, closure, advance, failing = errors_separated_by_success(
        held_fleet, monkeypatch, unprepared)
    failing[0] = False
    release(service, closure)
    service._admit()
    assert not service.store.list_attempts(job)  # Capacity news does not hurry a route-error wait.
    advance(5)
    service._admit()
    assert [row["state"] for row in service.store.list_attempts(job)] == ["reserved"]
