"""C-11.10 (D-TASKHOLD1): each mutant of the task hold fails its named assertion.

Every scenario passes on the daemon as built (the baseline), and each mutant in
`tests/task_hold_mutants.py` makes the scenario paired with it fail with that
scenario's own message, so the test that catches it is the one it names. The
build's seven, review r1's four (fix round 1), one per decision of fix round 1,
and one each for fix round 2's F1 and F2.
"""

from __future__ import annotations

import pytest

from subfleet import protocol, task_holds
from tests.disk_floor_model import stamp
from tests.task_hold_mutants import MUTANTS
from tests.task_hold_support import (attempts, configure, finalize_transient, finish, fresh_daemon, hold, job,
                                     release, restart, submit, turn)


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


def hold_after_the_door_stops_the_reservation():
    with fresh_daemon() as (service, harness, clock, signals):
        build = submit(service, harness, "build", "build-1")
        route = service._prepare_route

        def hold_then_route(job_row, *args, **kwargs):
            if job_row["job_id"] == build:
                epoch = service._task_hold_epoch       # only the reserving transaction can see it
                hold(service, "build", stamp(3600))
                service._task_hold_epoch = epoch
            return route(job_row, *args, **kwargs)
        service._prepare_route = hold_then_route
        service._admit()
        assert attempts(service, build) == [], "T1-recheck: a hold committed after the door look was not read again"


def a_hold_frees_no_capacity():
    with fresh_daemon() as (service, harness, clock, signals):
        hold(service, "build", stamp(3600))
        service._admit()
        seen = [key for kind in ("detached", "turn") for key, _ in service._leases_seen.get(kind, frozenset())]
        assert not any(key.startswith(task_holds.KEY) for key in seen), "C-6.10: a hold's row was counted as capacity"


def hold_survives_a_restart():
    with fresh_daemon() as (service, harness, clock, signals):
        hold(service, "build", stamp(3600))
        build = submit(service, harness, "build", "build-1")
        service = restart(service, harness)
        service._admit()
        assert attempts(service, build) == [], "storage: a restart let a held build start"


def pin_grace_survives_a_hold():
    with fresh_daemon() as (service, harness, clock, signals):
        configure(service, harness, lambda daemon: daemon.policy["admission"].update(pin_grace_s=60))
        service.store.update_lane("codex-1", enabled=0)
        build = submit(service, harness, "build", "build-1", pinned_lane="codex-1")
        service._admit()
        clock[0] = 10
        hold(service, "build", stamp(1000))
        service._admit()
        clock[0] = 500
        release(service, "build")
        service._admit()
        row = job(service, build)
        fail_at = service._pin_episodes.get(build, {}).get("fail_at")
        assert (row["state"], fail_at) == ("waiting", stamp(550)), \
            "T6-grace: the hold's time ran the job's pin grace"


def retry_backoff_survives_a_hold():
    with fresh_daemon() as (service, harness, clock, signals):
        build = submit(service, harness, "build", "build-1")
        service._admit()
        finalize_transient(service, attempts(service, build)[0])
        clock[0] = 10
        hold(service, "build", stamp(20))
        service._admit()
        clock[0] = 15
        release(service, "build")
        service._admit()
        assert len(attempts(service, build)) == 1, "T6-retry: a release started a retry before its backoff ended"


def hold_no_pass_saw_pauses_the_grace():
    """Fix round 2, F1: held at t=10 with no pass, released at 500: the commit paused the grace."""
    with fresh_daemon() as (service, harness, clock, signals):
        configure(service, harness, lambda daemon: daemon.policy["admission"].update(pin_grace_s=60))
        service.store.update_lane("codex-1", enabled=0)
        build = submit(service, harness, "build", "build-1", pinned_lane="codex-1")
        service._admit()
        clock[0] = 10
        hold(service, "build", stamp(1000))
        clock[0] = 500
        release(service, "build")
        service._admit()
        row = job(service, build)
        fail_at = service._pin_episodes.get(build, {}).get("fail_at")
        assert (row["state"], fail_at) == ("waiting", stamp(550)), \
            "T6-commit: a hold no pass saw ran the job's pin grace"


def recheck_is_no_kept_deadline():
    """Fix round 2, F2: review r2's input; the retry starts at 93, as with no hold, not at 121."""
    from tests.fake.test_task_hold_clocks import at, blocked_retry, retried_at
    with fresh_daemon(lanes=1, caps={"max_in_flight_per_lane": 1}) as (service, harness, clock, signals):
        build, blocking = blocked_retry(service, harness, clock)
        hold(service, "build", stamp(3600))
        at(service, clock, 92)
        clock[0] = 93
        release(service, "build")
        finish(service, blocking)
        service._admit()
        assert retried_at(service, build) == stamp(93), "T6-recheck: the hold kept a capacity recheck as a deadline"


def held_job_is_no_reset_demand():
    from tests.fake.test_task_hold_reset_demand import held_job_spends_nothing
    held_job_spends_nothing()


def hold_after_the_demand_read_spends_nothing():
    from tests.fake.test_task_hold_reset_demand import hold_after_demand_was_read_spends_nothing
    hold_after_demand_was_read_spends_nothing()


def offline_count_is_the_total():
    from subfleet import cli
    from subfleet.offline import Offline
    with fresh_daemon() as (service, harness, clock, signals):
        hold(service, "build", "2099-06-01T00:00:00Z")
        for n in range(51):
            submit(service, harness, "build", f"build-{n}")
        service._admit()
        status = Offline(service.root).status()
        assert len(status["running"]) == 50                 # offline lists 50 live jobs
        assert "(51 waiting)" in cli.format_status(status), "C-17.4: the offline count stopped at the 50 jobs listed"


def edge_controls_are_refused():
    with fresh_daemon() as (service, harness, clock, signals):
        for note in ("\nbooking\n", "\tbooking\t"):
            try:
                hold(service, "build", stamp(60), note=note)
            except protocol.ProtocolError as exc:
                assert exc.reason == "task-hold-note", "note: the wrong refusal"
            else:
                raise AssertionError(f"note: {note!r} was kept")
        assert service.store.query("SELECT * FROM leases") == [], "note: a refused hold wrote a row"


def release_hint_is_one_argument():
    import shlex
    from subfleet import render
    lines = render.why_queue({"job_id": "j", "kind": "dispatch", "state": "waiting", "wait_reason": "task-held",
                              "hold": {"reason": "task-held", "task": "data build", "until": stamp(60), "note": "n"}})
    command = lines[1].split("`")[1]
    assert shlex.split(command)[-1] == "data build", f"hint: {command!r} splits the task"


def release_notifies_the_waiters():
    with fresh_daemon() as (service, harness, clock, signals):
        hold(service, "build", stamp(3600))
        submit(service, harness, "build", "build-1")
        service._admit()
        notified = []
        original = service._notify
        service._notify = lambda: (notified.append(service.store.query(
            "SELECT state FROM jobs WHERE task='build'")), original())[1]
        release(service, "build")
        assert notified and notified[-1] == [{"state": "queued"}], \
            "T3-notify: a release committed without waking the daemon's waiters"


SCENARIOS = {
    "drop-admission-check": (held_build_never_starts, "T1"),
    "hold-every-task": (review_on_the_lane_is_placed, "T2"),
    "hold-turns": (turn_of_a_held_task_is_placed, "T5"),
    "ignore-until": (hold_ends_by_the_clock, "T3-expiry"),
    "release-without-notify": (release_wakes_at_once, "T3-release"),
    "replace-running-attempts": (running_attempt_is_untouched, "T4"),
    "accept-unknown-task": (unknown_task_is_refused, "refusal"),
    "drop-transaction-recheck": (hold_after_the_door_stops_the_reservation, "T1-recheck"),
    "hold-counted-as-capacity": (a_hold_frees_no_capacity, "C-6.10"),
    "strict-expiry-boundary": (hold_ends_by_the_clock, "T3-expiry"),
    "startup-clears-leases": (hold_survives_a_restart, "storage"),
    "grace-runs-while-held": (pin_grace_survives_a_hold, "T6-grace"),
    "release-drops-retry-backoff": (retry_backoff_survives_a_hold, "T6-retry"),
    "held-job-is-demand": (held_job_is_no_reset_demand, "T7"),
    "pending-ignores-holds": (hold_after_the_demand_read_spends_nothing, "T7-pending"),
    "offline-counts-the-display-cap": (offline_count_is_the_total, "C-17.4"),
    "note-strips-before-checking": (edge_controls_are_refused, "note"),
    "release-hint-unquoted": (release_hint_is_one_argument, "hint"),
    "release-skips-notify": (release_notifies_the_waiters, "T3-notify"),
    "pause-only-at-the-pass": (hold_no_pass_saw_pauses_the_grace, "T6-commit"),
    "keep-any-capacity-clock": (recheck_is_no_kept_deadline, "T6-recheck"),
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
