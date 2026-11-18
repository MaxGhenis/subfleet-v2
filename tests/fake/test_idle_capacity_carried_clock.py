"""D-ADMIT-IDLE1 review r2: a 600 s clock never outlasts the event-only verdict that earned it.

A look brought forward by capacity news that ends in a wait setting no clock of
its own (`probe-pending` with no probe reserved, `route-moved`) keeps the clock
its job carried. That clock may be an event-only verdict's 600 s one; the wait
it ends in is not event-only, so at most 30 s of it is kept (C-6.10). Each case
has a 30 s control: the same look after a verdict that kept the 30 s ceiling.
"""

import pytest

from subfleet.contracts import ClosureReason
from subfleet.daemon import after
from tests.fake.test_capacity_wait_backoff import backoff
from tests.fake.test_idle_capacity_events import clock, held_fleet, long_wait, release  # noqa: F401
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401

CARRIED = {"long": (ClosureReason.OPERATOR_HOLD, 600), "short": (ClosureReason.PROVIDER_LIMIT, 30)}


def waiting(service, harness, closure, carried):
    """A job on the ceiling its closure's verdict earns: 600 s, or the 30 s control."""
    reason, ceiling = CARRIED[carried]
    with service.store.transaction("fixture.reason") as tx:
        tx.execute("UPDATE closures SET reason=? WHERE closure_id=?", (reason.value, closure))
    job = long_wait(service, harness)
    assert backoff(service, job) == ceiling
    return job


def clipped(service, job):
    return [row["data_json"] for row in service.store.query(
        "SELECT data_json FROM events WHERE job_id=? AND kind='job.capacity_clock_clipped'", (job,))]


def refuse_probe_slot(service, monkeypatch, job):
    """Review r2's case: the job needs a probe, and only the probe's `:slot:0` lease is refused,
    as when the slot is taken between the evaluation and the probe's reservation."""
    monkeypatch.setattr(service, "_needs_probe", lambda decision, candidate: candidate["job_id"] == job)
    acquire = service.store.acquire_lease

    def refused(key, holder, *args, **kwargs):
        if key.endswith(":slot:0") and str(holder).startswith("probe:"):
            return False
        return acquire(key, holder, *args, **kwargs)

    monkeypatch.setattr(service.store, "acquire_lease", refused)


@pytest.mark.parametrize("carried", ["long", "short"])
def test_probe_pending_look_brought_forward_keeps_at_most_30_seconds(held_fleet, monkeypatch, carried):
    service, harness, closure, advance = held_fleet
    job = waiting(service, harness, closure, carried)
    before = service.store.get_job(job)["next_check_at"]
    refuse_probe_slot(service, monkeypatch, job)
    looks = []
    prepare = service._prepare_route
    monkeypatch.setattr(service, "_prepare_route",
                        lambda candidate, *args: looks.append(candidate["job_id"]) or prepare(candidate, *args))
    release(service, closure)
    service._admit()
    assert looks == [job]                               # the release brought it forward
    wait = service._capacity_waits[job]
    assert wait["hold"]["reason"] == "probe-pending" and "behind" not in wait["hold"]
    row = service.store.get_job(job)
    assert (row["state"], row["wait_reason"]) == ("waiting", "capacity")
    assert row["next_check_at"] <= after(30)
    assert wait["hold"]["next_check_at"] == row["next_check_at"]
    if carried == "long":
        assert len(clipped(service, job)) == 1
    else:
        assert row["next_check_at"] == before and not clipped(service, job)    # the old rule's clock, unwritten
    advance(31)
    service._admit()
    assert looks == [job, job]                          # looked at again within the 30 s ceiling


@pytest.mark.parametrize("carried", ["long", "short"])
def test_route_moved_look_brought_forward_keeps_at_most_30_seconds(held_fleet, monkeypatch, carried):
    service, harness, closure, advance = held_fleet
    job = waiting(service, harness, closure, carried)
    before = service.store.get_job(job)["next_check_at"]
    checks = []
    monkeypatch.setattr(service, "_route_stands",
                        lambda basis, decision: checks.append(decision) or ("moved", 1, decision))
    release(service, closure)
    service._admit()
    assert len(checks) == 3                             # ROUTE_TRIES reservations, each overtaken
    assert service._capacity_waits[job]["hold"] == {"reason": "route-moved", "tries": 3}
    row = service.store.get_job(job)
    assert (row["state"], row["wait_reason"]) == ("waiting", "capacity")
    assert row["next_check_at"] <= after(30)
    if carried == "long":
        assert len(clipped(service, job)) == 1
    else:
        assert row["next_check_at"] == before and not clipped(service, job)
    advance(31)
    service._admit()
    assert len(checks) == 6                             # looked at again within the 30 s ceiling


def test_clipping_a_carried_clock_lets_a_task_hold_take_the_job(held_fleet, monkeypatch):
    """C-11.10 (R3-1): the clip is an admission write, so a hold committed since the door's read takes the job."""
    service, harness, closure, _ = held_fleet
    job = waiting(service, harness, closure, "long")
    taken = []
    holds = {}
    carried = service.store.get_job(job)["next_check_at"]
    with monkeypatch.context() as patch:
        patch.setattr(service, "_task_held_at_write",
                      lambda tx, candidate, holds, **kwargs: taken.append(candidate["job_id"]) or {"task": "t"})
        assert service._clip_carried_clock(service.store.get_job(job), carried, holds, "route-moved") == (True, None)
    assert taken == [job] and not clipped(service, job)
    assert service.store.get_job(job)["next_check_at"] == carried
    # A sooner clock is kept as it is, with no transaction and so no task-hold read.
    assert service._clip_carried_clock(service.store.get_job(job), after(20), holds, "route-moved") == (
        False, after(20))
    assert service._clip_carried_clock(service.store.get_job(job), carried, holds, "route-moved") == (
        False, after(30))
    assert service.store.get_job(job)["next_check_at"] == after(30) and len(clipped(service, job)) == 1
