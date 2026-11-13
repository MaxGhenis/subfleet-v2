"""D-ADMIT-IDLE1 r1: queue guards retain a waiter's capacity news."""

from subfleet.contracts import Reading, ReadingLabel
from subfleet.daemon import after, utcnow
from tests.fake.test_capacity_wait_backoff import backoff, submit
from tests.fake.test_idle_capacity_events import clock, held_fleet, long_wait, release  # noqa: F401
from tests.fake.test_routing_end_to_end import claude_lane, routing_state  # noqa: F401


def measured(service, lane, utilization):
    service.store.add_reading(Reading(lane, "account", "seven_day", utilization, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))


def test_release_news_survives_pool_saturation(held_fleet):
    service, harness, closure, advance = held_fleet
    service.store.put_lane(claude_lane("claude-queue"))
    measured(service, "claude-queue", .2)
    running = submit(service, harness, pinned_model="sonnet")
    service._admit()
    assert [row["state"] for row in service.store.list_attempts(running)] == ["reserved"]
    job = long_wait(service, harness)
    assert backoff(service, job) == 600
    clock_before = service.store.get_job(job)["next_check_at"]
    # This priority job reaches the real reservation transaction first and
    # establishes the pass's saturation flag from the running attempt count.
    service.policy["admission"]["priority_callers"] = ["queue-news-front"]
    submit(service, harness, pinned_model="sonnet", caller_session="queue-news-front")
    service.policy["caps"]["max_active_attempts"] = 1
    release(service, closure)
    service._admit()
    assert service._holds[job]["reason"] == "fleet-full"
    assert service.store.get_job(job)["next_check_at"] == clock_before
    assert service._capacity_news == {job: True}
    # Synthetic cap input clears this guard without freeing a lease or changing
    # the policy digest: the saved release, rather than fresh news, must suffice.
    service.policy["caps"]["max_active_attempts"] = None
    advance(31)
    service._admit()
    assert [row["state"] for row in service.store.list_attempts(job)] == ["reserved"]
    assert job not in service._capacity_news


def test_release_news_survives_older_waiter(held_fleet):
    service, harness, closure, advance = held_fleet
    older = submit(service, harness, pinned_model="terra")
    job = long_wait(service, harness)
    assert backoff(service, job) == 600
    clock_before = service.store.get_job(job)["next_check_at"]
    service.policy["caps"]["max_active_attempts"] = 4
    advance(1)
    measured(service, "codex-1", .99)
    release(service, closure)
    service._admit()
    assert service._holds[job]["reason"] == "behind-older-job"
    assert service._holds[job]["behind"] == older
    assert service.store.get_job(job)["next_check_at"] == clock_before
    assert service._capacity_news == {job: True}
    # The older waiter has no attempt or lease. Its cancellation and a reading
    # improvement create no new capacity census news for the surviving waiter.
    service.store.update_job(older, state="cancelled")
    advance(31)
    measured(service, "codex-1", .2)
    service._admit()
    assert [row["state"] for row in service.store.list_attempts(job)] == ["reserved"]
    assert job not in service._capacity_news
