"""C-6.10: capacity-news refreshes are outside the steady commit-rate bound."""

import pytest

from subfleet.daemon import utcnow
from tests.fake.test_capacity_wait_backoff import backoff
from tests.fake.test_idle_capacity_events import clock, held_fleet, long_wait  # noqa: F401
from tests.fake.test_routing_end_to_end import claude_lane, routing_state  # noqa: F401


@pytest.mark.parametrize("unprepared", [False, True])
def test_unchanged_verdict_can_commit_more_than_six_times_per_hour(held_fleet, unprepared):
    """Seven unrelated lane changes refresh one unchanged Codex wait in 70 s."""
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
    event_kind = "job.unprepared_wait" if unprepared else "attempt.reserved"
    initial_event = service.store.one("SELECT max(event_id) n FROM events")["n"]
    assert backoff(service, job) == 600

    for number in range(7):
        advance(10)
        assert service.store.get_job(job)["next_check_at"] > utcnow()
        service.store.update_lane("unrelated-claude", enabled=number % 2 == 1)
        service._admit()
        assert service._capacity_waits[job]["checked_at"] == utcnow()
        assert service._capacity_waits[job]["signature"] == signature
        assert backoff(service, job) == 600

    refreshed = service.store.one(
        "SELECT count(*) n FROM events WHERE event_id>? AND job_id=? AND kind=?",
        (initial_event, job, event_kind))["n"]
    assert refreshed == 7
    assert len(service.store.list_decisions(job)) == initial_decisions
    assert not service.store.list_attempts(job)
