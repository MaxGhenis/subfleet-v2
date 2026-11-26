"""C-6.10: capacity-news refreshes are outside the steady commit-rate bound."""

import pytest

from subfleet.daemon import utcnow
from tests.fake.test_capacity_wait_backoff import backoff
from tests.fake.test_idle_capacity_events import clock, held_fleet, long_wait  # noqa: F401
from tests.fake.test_routing_end_to_end import claude_lane, routing_state  # noqa: F401


@pytest.mark.parametrize("unprepared", [False, True])
def test_unchanged_verdict_can_commit_more_than_six_times_per_hour(held_fleet, unprepared):
    """Seven unrelated lane changes refresh one unchanged Codex wait in 70 s.

    Count changes to the persisted next_check_at because C-3.8 makes repeated
    looks inside the reserving transaction silent.
    """
    service, harness, _, advance = held_fleet
    service.store.put_lane(claude_lane("unrelated-claude"))
    changes = {}
    if unprepared:
        from tests.unit.test_salvage import git

        git(harness.workdir, "init", "-b", "task/refresh-bound")
        git(harness.workdir, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "--allow-empty", "-m", "baseline")
        changes["sandbox"] = "workspace-write"
    job = long_wait(service, harness, **changes)
    signature = service._capacity_waits[job]["signature"]
    initial_decisions = len(service.store.list_decisions(job))
    initial_event = service.store.one("SELECT max(event_id) n FROM events")["n"]
    assert backoff(service, job) == 600

    committed = 0
    for number in range(7):
        advance(10)
        before = service.store.get_job(job)["next_check_at"]
        assert before > utcnow()
        service.store.update_lane("unrelated-claude", enabled=number % 2 == 1)
        service._admit()
        if service.store.get_job(job)["next_check_at"] != before:
            committed += 1
        assert service._capacity_waits[job]["checked_at"] == utcnow()
        assert service._capacity_waits[job]["signature"] == signature
        assert backoff(service, job) == 600

    assert committed == 7
    if unprepared:
        refreshed = service.store.one(
            "SELECT count(*) n FROM events WHERE event_id>? AND job_id=? AND kind='job.unprepared_wait'",
            (initial_event, job))["n"]
        assert refreshed == 7
    else:
        assert service.store.one(
            "SELECT count(*) n FROM events WHERE event_id>? AND job_id=? "
            "AND kind IN ('job.capacity_waiting', 'attempt.reserved')",
            (initial_event, job))["n"] == 0
    assert len(service.store.list_decisions(job)) == initial_decisions
    assert not service.store.list_attempts(job)
