"""C-11.10 (D-TASKHOLD1): each mutant of the task hold fails its named assertion.

Every scenario passes on the daemon as built (the baseline), and each mutant in
`tests/task_hold_mutants.py` makes the scenario paired with it fail with that
scenario's own message, so the test that catches it is the one it names.
"""

from __future__ import annotations

import pytest

from subfleet import protocol
from tests.disk_floor_model import stamp
from tests.task_hold_mutants import MUTANTS
from tests.task_hold_support import attempts, fresh_daemon, hold, job, release, submit, turn


def held_build_never_starts():
    with fresh_daemon() as (service, harness, clock, signals):
        hold(service, "build", stamp(3600))
        build = submit(service, harness, "build", "build-1")
        for _ in range(2):
            service._admit()
            clock[0] += 61
        assert attempts(service, build) == [], "T1: a held build started"


def review_on_the_lane_is_placed():
    with fresh_daemon(lanes=1) as (service, harness, clock, signals):
        hold(service, "build", stamp(3600))
        review = submit(service, harness, "review", "review-1")
        service._admit()
        assert [row["lane_id"] for row in attempts(service, review)] == ["codex-1"], \
            "T2: a review was held by a build hold"


def turn_of_a_held_task_is_placed():
    with fresh_daemon() as (service, harness, clock, signals):
        hold(service, "build", stamp(3600))
        turn_job = turn(service, harness, 1, task="build")
        service._admit()
        assert len(attempts(service, turn_job)) == 1, "T5: a turn was held"


def hold_ends_by_the_clock():
    with fresh_daemon() as (service, harness, clock, signals):
        hold(service, "build", stamp(600))
        build = submit(service, harness, "build", "build-1")
        service._admit()
        clock[0] = 600
        service._admit()
        assert len(attempts(service, build)) == 1, "T3-expiry: the hold outlived its end"


def release_wakes_at_once():
    with fresh_daemon() as (service, harness, clock, signals):
        hold(service, "build", stamp(86400))
        build = submit(service, harness, "build", "build-1")
        service._admit()
        release(service, "build")
        row = job(service, build)
        assert (row["state"], row["wait_reason"], row["next_check_at"]) == ("queued", None, None), \
            "T3-release: a release left its jobs waiting for the old end"


def running_attempt_is_untouched():
    with fresh_daemon() as (service, harness, clock, signals):
        build = submit(service, harness, "build", "build-1")
        service._admit()
        with service.store.transaction("test.running") as tx:
            tx.execute("UPDATE attempts SET state='running' WHERE job_id=?", (build,))
        before = [dict(row) for row in attempts(service, build)]
        hold(service, "build", stamp(3600))
        service._admit()
        assert [dict(row) for row in attempts(service, build)] == before and signals == [], \
            "T4: a hold touched a running attempt"


def unknown_task_is_refused():
    with fresh_daemon() as (service, harness, clock, signals):
        try:
            hold(service, "builds", stamp(60))
        except protocol.ProtocolError as exc:
            assert exc.reason == "unknown-task", "refusal: the wrong refusal"
            assert service.store.query("SELECT * FROM leases") == [], "refusal: a refused hold wrote a row"
            return
        raise AssertionError("refusal: an unknown task name was accepted")


SCENARIOS = {
    "drop-admission-check": (held_build_never_starts, "T1"),
    "hold-every-task": (review_on_the_lane_is_placed, "T2"),
    "hold-turns": (turn_of_a_held_task_is_placed, "T5"),
    "ignore-until": (hold_ends_by_the_clock, "T3-expiry"),
    "release-without-notify": (release_wakes_at_once, "T3-release"),
    "replace-running-attempts": (running_attempt_is_untouched, "T4"),
    "accept-unknown-task": (unknown_task_is_refused, "refusal"),
}


def test_every_mutant_has_a_scenario():
    assert set(SCENARIOS) == set(MUTANTS)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_baseline_passes_and_the_mutant_fails_the_named_assertion(name, monkeypatch):
    scenario, tag = SCENARIOS[name]
    scenario()                                          # the daemon as built
    with monkeypatch.context() as patch:
        MUTANTS[name](patch)
        with pytest.raises(AssertionError, match=f"^{tag}:"):
            scenario()
    scenario()                                          # and as built again, once the patch is gone
