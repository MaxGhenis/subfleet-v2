"""D-ADMIT-IDLE1 review r2: where capacity news comes from, case by case.

A change committed on the daemon's own store connection reaches admission through
its TEMP triggers (`Store.watch_admission`), which bump the committed revision. A
lane change committed on another connection fires none of them: only the pass's
lane census (`previous[1] != lanes`) sees it. A closure added on the daemon's
connection brings every eligible wait forward through the revision, which C-6.10
lists: an added closure can turn an event-only verdict into one that is not.
"""

import pytest

from subfleet.contracts import ClockSource, Closure, ClosureReason
from subfleet.daemon import after
from subfleet.store import Store
from tests.fake.test_capacity_wait_backoff import backoff, submit
from tests.fake.test_idle_capacity_events import clock, held_fleet, long_wait  # noqa: F401
from tests.fake.test_routing_end_to_end import claude_lane, routing_state  # noqa: F401


def backed_off(service, harness, monkeypatch, count=3):
    """`count` jobs on long capacity clocks, and a list of the jobs looked at from now on."""
    jobs = [submit(service, harness, pinned_model="terra") for _ in range(count)]
    service._admit()
    for job in jobs:
        service.store.update_job(job, next_check_at=after(8000))
    looked = []
    workspace = service._workspace
    monkeypatch.setattr(service, "_workspace", lambda job: looked.append(job["job_id"]) or workspace(job))
    return jobs, looked


@pytest.mark.parametrize("change", ["none", "enrolled", "disabled", "removed"])
def test_a_lane_change_from_another_connection_reaches_admission_only_through_the_census(
        held_fleet, monkeypatch, change):
    service, harness, _, _ = held_fleet
    if change == "removed":
        service.store.put_lane(claude_lane("unused-lane"))
    jobs, looked = backed_off(service, harness, monkeypatch)
    service._admit()                                    # both censuses taken after the setup above
    assert looked == []
    with service._admission_pace_lock:
        revision, wakes = service._admission_capacity_revision, service._admission_wake_seq
    with Store(service.store.path) as other:             # no TEMP trigger and no watch on this connection
        if change == "enrolled":
            other.put_lane(claude_lane("other-connection"))
        elif change == "disabled":
            other.update_lane("codex-1", enabled=False)
        elif change == "removed":
            with other.transaction("fixture.remove") as tx:
                tx.execute("DELETE FROM lanes WHERE lane_id='unused-lane'")
    with service._admission_pace_lock:                  # nothing reached the daemon's triggers
        assert (service._admission_capacity_revision, service._admission_wake_seq) == (revision, wakes)
    service._admit()
    assert looked == ([] if change == "none" else jobs)


@pytest.mark.parametrize("added", ["provider-limit-on-the-held-lane", "operator-hold-on-another-lane"])
def test_c6_10_a_closure_added_brings_every_eligible_wait_forward(held_fleet, monkeypatch, added):
    """Review r2's P3: the clause now lists closure additions, which the code brings forward."""
    service, harness, closure, _ = held_fleet
    job = long_wait(service, harness)
    assert backoff(service, job) == 600
    others, looked = backed_off(service, harness, monkeypatch, count=2)
    service.store.update_job(job, next_check_at=after(8000))
    service._admit()
    assert looked == []
    held = service.store.one("SELECT * FROM closures WHERE closure_id=?", (closure,))
    if added == "provider-limit-on-the-held-lane":
        # The held lane's verdict is mixed now: a provider limit is not event-only.
        service.store.add_closure(Closure("codex-1", held["scope"], held["until_at"], ClosureReason.PROVIDER_LIMIT,
                                          ClockSource.REPORTED, "fixture"))
    else:
        service.store.put_lane(claude_lane("claude-9"))
        service._admit()                                # the enrollment's own news, spent first
        looked.clear()
        for each in (job, *others):
            service.store.update_job(each, next_check_at=after(8000))
        service.store.add_closure(Closure("claude-9", "account", after(7200), ClosureReason.OPERATOR_HOLD,
                                          ClockSource.REPORTED, "fixture"))
    service._admit()
    assert sorted(looked) == sorted([job, *others])
    if added == "provider-limit-on-the-held-lane":
        # Its look reclassified the verdict: the 600 s clock did not outlast it.
        assert backoff(service, job) <= 30
