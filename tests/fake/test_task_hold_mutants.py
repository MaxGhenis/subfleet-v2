"""C-11.10 (D-TASKHOLD1): each mutant of the task hold fails its named assertion.

Every scenario passes on the daemon as built (the baseline), and each mutant in
`tests/task_hold_mutants.py` makes the scenario paired with it fail with that
scenario's own message, so the test that catches it is the one it names. The
build's seven, review r1's four (fix round 1), one per decision of fix round 1,
one each for fix round 2's F1 and F2, and one each for fix round 3's R3-1 and
R3-2; fix round 3 (R3-3) gave round 1's two reset-demand mutants back their
own definitions and scenarios that reach them with the commit's take intact.
Fix round 4 adds three direct named pairs, outside the opt-in property registry.
Fix round 5 adds both row-clock endings and three formerly unpinned rule pairs.
"""

from __future__ import annotations

import pytest

from subfleet import protocol, task_holds
from tests.disk_floor_model import stamp
from tests.task_hold_mutants import MUTANTS
from tests.task_hold_mutants import door_without_reread, handler_clock_before_writer, legacy_deadline_from_end
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


def held_retry_is_no_reset_demand():
    """Fix round 3 (review r3 of #260, R3-3): the round-1 mutant, the check alone removed, with the
    commit's take intact: a held build's retry waits on capacity before any pass takes it."""
    from tests.fake.test_task_hold_reset_demand import held_retry_is_no_reset_demand as scenario
    scenario()


def held_retry_spends_nothing():
    """Fix round 3 (R3-3): as above, for the reset action's pending transaction."""
    from tests.fake.test_task_hold_reset_demand import held_retry_spends_nothing as scenario
    scenario()


def look_under_way_takes_the_job():
    """Fix round 3, R3-1: the first look at 10 stopped as C-11.8 opens the episode, the hold
    committed, the look resumed; released at 50, the job fails at 110, not 70."""
    from tests.fake.test_task_hold_clocks import look_under_way
    with fresh_daemon() as (service, harness, clock, signals), pytest.MonkeyPatch.context() as patch:
        configure(service, harness, lambda daemon: daemon.policy["admission"].update(pin_grace_s=60))
        service.store.update_lane("codex-1", enabled=0)
        build = submit(service, harness, "build", "build-1", pinned_lane="codex-1")
        clock[0] = 10
        with look_under_way(service, patch, "_stuck_pin"):
            hold(service, "build", stamp(1000))
        assert job(service, build)["wait_reason"] == "task-held", \
            "T6-inflight: a look under way wrote its wait over the committed hold"
        clock[0] = 50
        release(service, "build")
        service._admit()
        assert service._pin_episodes.get(build, {}).get("fail_at") == stamp(110), \
            "T6-inflight: the grace ran while the job was held"


def late_finalizer_keeps_its_deadline():
    """Fix round 3, R3-2: finalized at 100 after an exit at 0, held at 110 and released at 111:
    the retry is due at 160, as unheld."""
    from tests.fake.test_task_hold_clocks import retried_at
    with fresh_daemon() as (service, harness, clock, signals):
        build = submit(service, harness, "build", "build-1")
        service._admit()
        finalize_transient(service, attempts(service, build)[0], clock=clock, finalized_at=100)
        clock[0] = 110
        hold(service, "build", stamp(500))
        clock[0] = 111
        release(service, "build")
        service._admit()
        assert retried_at(service, build) is None, "T6-finalizer: the hold dropped the finalizer's deadline"


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
    "held-job-is-demand": (held_retry_is_no_reset_demand, "T7"),
    "pending-ignores-holds": (held_retry_spends_nothing, "T7-pending"),
    "offline-counts-the-display-cap": (offline_count_is_the_total, "C-17.4"),
    "note-strips-before-checking": (edge_controls_are_refused, "note"),
    "release-hint-unquoted": (release_hint_is_one_argument, "hint"),
    "release-skips-notify": (release_notifies_the_waiters, "T3-notify"),
    "pause-only-at-the-pass": (hold_no_pass_saw_pauses_the_grace, "T6-commit"),
    "keep-any-capacity-clock": (recheck_is_no_kept_deadline, "T6-recheck"),
    "admission-writes-over-the-hold": (look_under_way_takes_the_job, "T6-inflight"),
    "retry-deadline-from-the-attempt-end": (late_finalizer_keeps_its_deadline, "T6-finalizer"),
}


@pytest.mark.parametrize("name", ["door-without-reread", "legacy-deadline-from-end", "handler-clock-before-writer"])
def test_round_four_mutant_fails_its_named_assertion(name, monkeypatch):
    from tests.fake.test_task_hold_clocks import (
        test_hold_and_release_clocks_are_sampled_after_finalizer_lock_wait,
        test_legacy_finalizer_deadline_survives_upgrade,
        test_stale_door_take_uses_current_hold_at_write,
    )

    if name == "door-without-reread":
        mutate = door_without_reread
        scenario = lambda patch: test_stale_door_take_uses_current_hold_at_write("replacement", patch)
        tag = "R4-door-replacement:"
    elif name == "legacy-deadline-from-end":
        mutate = legacy_deadline_from_end
        scenario = lambda patch: test_legacy_finalizer_deadline_survives_upgrade(100)
        tag = "R4-legacy:"
    else:
        mutate = handler_clock_before_writer
        scenario = lambda patch: test_hold_and_release_clocks_are_sampled_after_finalizer_lock_wait("release", patch)
        tag = "R4-commit-clock:"
    with monkeypatch.context() as baseline:
        scenario(baseline)
    with monkeypatch.context() as mutated:
        mutate(mutated)
        with pytest.raises(AssertionError, match=tag):
            scenario(mutated)
    with monkeypatch.context() as restored:
        scenario(restored)


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


@pytest.mark.parametrize("name", ["ending-with-ended-at", "ending-without-ended-at",
                                  "legacy-fallback-from-job-clock", "replacement-at-request",
                                  "until-at-request"])
def test_round_five_mutant_fails_its_named_assertion(name, monkeypatch):
    from tests.fake import test_task_hold_clocks as clocks
    from tests import task_hold_mutants as mutations

    pairs = {
        "ending-with-ended-at": (
            lambda p: mutations.ending_uses_pass_clock(p, explicit=True),
            lambda p: clocks.test_r5_1_door_gives_an_ended_hold_back_twice(40, p), "R5-1:"),
        "ending-without-ended-at": (
            lambda p: mutations.ending_uses_pass_clock(p, explicit=False),
            clocks.test_r5_1b_door_ends_a_later_hold_at_the_copied_clock, "R5-1b:"),
        "legacy-fallback-from-job-clock": (
            mutations.legacy_fallback_from_job_clock,
            lambda p: clocks.test_legacy_recheck_after_the_backoff_is_no_deadline("held-92-released-93"),
            "R5-legacy-F2:"),
        "replacement-at-request": (
            mutations.replacement_judged_at_request_clock,
            clocks.test_hold_replacement_requested_before_the_first_hold_expires_commits_after,
            "R5-hold-expiry-wait:"),
        "until-at-request": (
            mutations.until_validated_at_request_clock,
            clocks.test_hold_whose_end_passes_during_the_writer_wait_is_refused, "R5-until-wait:"),
    }
    mutate, scenario, tag = pairs[name]
    with monkeypatch.context() as baseline:
        scenario(baseline)
    with monkeypatch.context() as mutated:
        mutate(mutated)
        with pytest.raises(AssertionError, match=tag):
            scenario(mutated)
    with monkeypatch.context() as restored:
        scenario(restored)
