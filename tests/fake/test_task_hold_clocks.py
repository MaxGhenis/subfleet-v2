"""C-11.10, T6 (D-TASKHOLD1 fix round 1): no clock of a job's runs while a task hold keeps it.

Review r1 of #260 executed two clocks that a hold let run:

- **P2-1, a pin's grace (C-11.8).** A 60 s grace on a disabled lane from t=0, a hold from t=10,
  then a release at t=500 or the hold's end at t=1000: the job failed at once with rc 3, for a
  pinned job and for a retry every lane refuses alike. Now the grace counts only time the job
  was not held: 50 s are left when the hold ends, whether it ends by a release, by its clock,
  by a shorter replacement, or across a restart, and a replacement that still covers the task
  keeps the pause. "The end" of an expiry is the hold's `until`, not when a pass noticed.
- **P2-2, a retry's backoff (C-4.5).** Through the daemon's own transient finalizer, a retry due
  at t=60 and a hold from t=10 until t=20: a release at t=15 started it at 15, the expiry at 20.
  Now both start it at 60, as with no hold, and a hold that outlasts the backoff (t=10 to 90)
  starts it at 90: it satisfies the backoff and never extends it.

Every case runs on the real daemon pass, in process, on a virtual clock.
"""

from __future__ import annotations

import pytest

from subfleet import daemon as daemon_module
from subfleet.contracts import TRANSIENT_RETRY_DELAY_S, Exit
from tests.disk_floor_model import stamp
from tests.task_hold_support import (attempts, configure, finalize_transient, finish, fresh_daemon, hold, job,
                                     release, restart, submit)

GRACE = 60


def at(service, clock, t):
    """The clock reads `t`, and one admission pass runs."""
    clock[0] = t
    service._admit()


def stuck(service, harness, clock, kind: str) -> str:
    """At t=0, a build whose C-11.8 grace of 60 s starts: pinned to codex-1, which is disabled
    (`pinned`), or a retry after a transient attempt with every lane disabled (`retry`)."""
    configure(service, harness, lambda daemon: daemon.policy["admission"].update(pin_grace_s=GRACE))
    if kind == "pinned":
        service.store.update_lane("codex-1", enabled=0)
        build = submit(service, harness, "build", "build-1", pinned_lane="codex-1")
    else:
        build = submit(service, harness, "build", "build-1")
        service._admit()
        [first] = attempts(service, build)
        finalize_transient(service, first)
        for lane in ("codex-1", "codex-2"):
            service.store.update_lane(lane, enabled=0)
    service._admit()
    episode = service._pin_episodes[build]
    assert (episode["open"], episode["since"], episode["fail_at"]) == (True, stamp(0), stamp(GRACE))
    assert service._holds[build]["reason"] == ("pin-unadmittable" if kind == "pinned" else "lanes-unadmittable")
    return build


def fails_exactly_at(service, clock, build: str, fail_at: int) -> None:
    """The job's grace runs out at `fail_at` and not a second earlier: it fails then, rc 3 (C-11.8)."""
    at(service, clock, fail_at - 1)
    row = job(service, build)
    assert row["state"] == "waiting" and row["wait_reason"] == "capacity", row
    assert row["next_check_at"] == stamp(fail_at)
    assert service._pin_episodes[build]["fail_at"] == stamp(fail_at)
    at(service, clock, fail_at)
    row = job(service, build)
    assert (row["state"], row["rc"]) == ("failed", int(Exit.NO_LANE)), row


ENDS = ("release", "expiry", "replacement-longer", "replacement-shorter", "restart-then-release",
        "restart-then-expiry", "restart-after-expiry")


@pytest.mark.parametrize("kind", ["pinned", "retry"])
@pytest.mark.parametrize("end", ENDS)
def test_a_held_jobs_pin_grace_is_paused_and_50_s_are_left_when_the_hold_ends(kind, end):
    """r1's P2-1 scenario: 60 s of grace from t=0, held at t=10, so 50 s remain from the end."""
    with fresh_daemon() as (service, harness, clock, signals):
        build = stuck(service, harness, clock, kind)
        clock[0] = 10
        hold(service, "build", stamp(1000))
        at(service, clock, 10)
        assert job(service, build)["wait_reason"] == "task-held"
        taken = service.store.query("SELECT data_json FROM events WHERE job_id=? AND kind='job.task_held' "
                                    "AND json_extract(data_json,'$.taken')", (build,))
        assert len(taken) == 1 and '"pin_paused_s":50.0' in taken[0]["data_json"]
        at(service, clock, 70)                     # long past the old end of the grace: still held, not failed
        assert job(service, build)["wait_reason"] == "task-held"
        if end == "release":
            at(service, clock, 500)
            release(service, "build")
            ended = 500
        elif end == "expiry":
            ended = 1000
        elif end == "replacement-longer":
            clock[0] = 300
            hold(service, "build", stamp(2000), note="moved later")     # still covers the task: still paused
            at(service, clock, 300)
            at(service, clock, 1000)
            assert job(service, build)["wait_reason"] == "task-held"
            ended = 2000
        elif end == "replacement-shorter":
            clock[0] = 300
            hold(service, "build", stamp(400), note="ends sooner")
            ended = 400
        elif end == "restart-then-release":
            clock[0] = 300
            service = restart(service, harness)
            at(service, clock, 300)
            assert job(service, build)["wait_reason"] == "task-held"
            clock[0] = 500
            release(service, "build")
            ended = 500
        elif end == "restart-then-expiry":
            clock[0] = 300
            service = restart(service, harness)
            at(service, clock, 300)
            ended = 1000
        else:                                       # the daemon was down when the hold ended
            clock[0] = 1020
            service = restart(service, harness)
            ended = 1000
        if end == "restart-after-expiry":
            at(service, clock, 1020)                 # noticed 20 s late: 30 s are left, not 50
            assert service._pin_episodes[build]["fail_at"] == stamp(ended + 50)
            fails_exactly_at(service, clock, build, ended + 50)
            return
        at(service, clock, ended)
        assert service._pin_episodes[build]["fail_at"] == stamp(ended + 50)
        assert job(service, build)["next_check_at"] == stamp(ended + 50)
        fails_exactly_at(service, clock, build, ended + 50)


QUIET_ENDS = ("release", "expiry", "restart-at-expiry")


@pytest.mark.parametrize("kind", ["pinned", "retry"])
@pytest.mark.parametrize("end", QUIET_ENDS)
def test_a_hold_no_pass_saw_pauses_the_grace_from_its_commit(kind, end):
    """Review r2 of #260, F1: the same stuck job, held at t=10 with no admission pass after the
    hold (the daemon stopped, held back by C-5.10's pacing, or still in startup recovery until the
    hold ended), then released at 500, expired at 1000, or expired at 1000 across a restart. The
    job failed rc 3 at the hold's end: only a pass took it, so all the held time ran its grace.
    Now the hold's own transaction takes it and pauses the grace: 50 s are left at the end."""
    with fresh_daemon() as (service, harness, clock, signals):
        build = stuck(service, harness, clock, kind)
        clock[0] = 10
        hold(service, "build", stamp(1000))         # no pass after it
        if end == "release":
            clock[0] = 500
            release(service, "build")
            ended = 500
        elif end == "expiry":
            ended = 1000
        else:                                       # the daemon was down from the hold until its end
            clock[0] = 1000
            service = restart(service, harness)
            ended = 1000
        at(service, clock, ended)                   # the first pass since the hold
        row = job(service, build)
        assert (row["state"], row["wait_reason"]) == ("waiting", "capacity"), \
            ("F1: the job ended at the hold's end, its grace run by the held time", dict(row))
        assert row["next_check_at"] == stamp(ended + 50)
        assert service._pin_episodes[build]["fail_at"] == stamp(ended + 50)
        taken = service.store.query("SELECT data_json FROM events WHERE job_id=? AND kind='job.task_held' "
                                    "AND json_extract(data_json,'$.taken')", (build,))
        assert len(taken) == 1 and '"pin_paused_s":50.0' in taken[0]["data_json"]
        fails_exactly_at(service, clock, build, ended + 50)


def test_the_hold_takes_its_jobs_in_its_own_transaction():
    """F1: the commit itself takes every job of the task that waits or is queued (a person's wait
    excepted), each with its `job.task_held` event, beside `task.held`: no pass is needed."""
    with fresh_daemon() as (service, harness, clock, signals):
        builds = [submit(service, harness, "build", f"build-{n}") for n in range(2)]
        review = submit(service, harness, "review", "review-1")
        hold(service, "build", stamp(600))
        for build in builds:
            row = job(service, build)
            assert (row["state"], row["wait_reason"], row["next_check_at"]) == ("waiting", "task-held", stamp(600))
        assert job(service, review)["state"] == "queued"
        kinds = [(row["kind"], row["job_id"]) for row in service.store.query(
            "SELECT kind, job_id FROM events WHERE kind IN ('job.task_held','task.held') ORDER BY event_id")]
        assert kinds == [("job.task_held", builds[0]), ("job.task_held", builds[1]), ("task.held", None)]


def test_the_grace_runs_between_two_holds_no_pass_saw_apart():
    """A hold that ended unseen, and another set later: the time between is time with no hold."""
    with fresh_daemon() as (service, harness, clock, signals):
        build = stuck(service, harness, clock, "pinned")
        clock[0] = 10
        hold(service, "build", stamp(30))
        at(service, clock, 10)                      # held at 10: 50 s left
        clock[0] = 50                               # the hold ended at 30; no pass ran since
        hold(service, "build", stamp(1000), note="the next booking")
        # The commit ended the first hold where it ended (30) and took the job for this one (F1).
        assert job(service, build)["next_check_at"] == stamp(1000)
        assert service.store.one("SELECT data_json FROM events WHERE job_id=? AND kind='job.task_hold_ended' "
                                 "ORDER BY event_id DESC LIMIT 1", (build,))["data_json"] == \
            daemon_module._event_json({"by": "end", "ended_at": stamp(30), "next_check_at": stamp(80),
                                       "pin_fail_at": stamp(80), "task": "build"})
        at(service, clock, 50)                      # 30 to 50 ran the grace: 30 s left
        assert job(service, build)["wait_reason"] == "task-held"
        clock[0] = 500
        release(service, "build")
        at(service, clock, 500)
        fails_exactly_at(service, clock, build, 530)


def test_a_release_ends_an_unseen_end_where_it_ended():
    """Released after its end passed unseen: the grace ran again from the end, not the release."""
    with fresh_daemon() as (service, harness, clock, signals):
        build = stuck(service, harness, clock, "pinned")
        clock[0] = 10
        hold(service, "build", stamp(100))
        at(service, clock, 10)
        clock[0] = 120                              # the hold ended at 100; released at 120
        release(service, "build")
        at(service, clock, 120)
        fails_exactly_at(service, clock, build, 150)


def test_a_release_of_a_hold_that_also_ended_unseen_ends_it_at_its_own_end():
    """Two holds and no pass after the first took the job: 10 to 30, then 50 to 100, released
    at 120. Held 10 to 30 and 50 to 100, so the grace ran 0 to 10, 30 to 50 and 100 to 120:
    50 s of its 60, and it fails at 130."""
    with fresh_daemon() as (service, harness, clock, signals):
        build = stuck(service, harness, clock, "pinned")
        clock[0] = 10
        hold(service, "build", stamp(30))
        at(service, clock, 10)
        clock[0] = 50
        hold(service, "build", stamp(100), note="the next booking")
        clock[0] = 120
        release(service, "build")
        assert job(service, build)["next_check_at"] == stamp(130)
        at(service, clock, 120)
        fails_exactly_at(service, clock, build, 130)


def test_a_capacity_recheck_is_no_deadline_the_hold_keeps():
    """Only a retry's backoff is kept. A job that waited on capacity with no attempt behind it is
    queued at the hold's end and looked at on the next pass, as the build had it, so a capacity
    wait (and a reset credit's demand, C-23.16) is found again at once, not at the old recheck."""
    with fresh_daemon() as (service, harness, clock, signals):
        for lane in ("codex-1", "codex-2"):
            service.dispatch("lanes", {"action": "hold", "lane_id": lane, "until": stamp(3600)})
        build = submit(service, harness, "build", "build-1")
        at(service, clock, 0)
        row = job(service, build)
        assert (row["state"], row["wait_reason"]) == ("waiting", "capacity") and row["next_check_at"] > stamp(0)
        clock[0] = 5
        hold(service, "build", stamp(600))
        at(service, clock, 5)
        taken = service.store.query("SELECT data_json FROM events WHERE job_id=? AND kind='job.task_held'", (build,))
        assert '"kept":null' in taken[-1]["data_json"]
        clock[0] = 6
        release(service, "build")
        row = job(service, build)
        assert (row["state"], row["wait_reason"], row["next_check_at"]) == ("queued", None, None)


def test_a_lane_that_opens_while_the_job_is_held_ends_the_episode_once_the_hold_ends():
    with fresh_daemon() as (service, harness, clock, signals):
        build = stuck(service, harness, clock, "pinned")
        clock[0] = 10
        hold(service, "build", stamp(1000))
        at(service, clock, 10)
        service.store.update_lane("codex-1", enabled=1)
        clock[0] = 500
        release(service, "build")
        at(service, clock, 500)
        assert [row["lane_id"] for row in attempts(service, build)] == ["codex-1"]
        assert build not in service._pin_episodes or not service._pin_episodes[build].get("open")


# --- P2-2: a retry's backoff ---------------------------------------------------------------------

def retrying(service, harness, clock) -> str:
    """At t=0, a build's first attempt ends `transient` through the real finalizer: retry due at 60."""
    build = submit(service, harness, "build", "build-1")
    service._admit()
    [first] = attempts(service, build)
    row = finalize_transient(service, first)
    assert (row["state"], row["wait_reason"], row["next_check_at"]) == \
        ("waiting", "capacity", stamp(TRANSIENT_RETRY_DELAY_S))
    return build


def retried_at(service, build) -> str | None:
    rows = attempts(service, build)
    return rows[1]["reserved_at"] if len(rows) > 1 else None


@pytest.mark.parametrize("schedule,started", [
    ("unheld", 60),                 # the control
    ("release-at-15", 60),
    ("expiry-at-20", 60),
    ("held-until-90", 90),
])
def test_a_retrys_backoff_is_kept_apart_from_the_hold(schedule, started):
    """r1's P2-2 scenario: the retry starts at max(end, its deadline), never earlier."""
    with fresh_daemon() as (service, harness, clock, signals):
        build = retrying(service, harness, clock)
        if schedule != "unheld":
            clock[0] = 10
            hold(service, "build", stamp(90 if schedule == "held-until-90" else 20))
            at(service, clock, 10)
            row = job(service, build)
            assert (row["wait_reason"], row["next_check_at"]) == ("task-held", stamp(90 if started == 90 else 20))
        if schedule == "release-at-15":
            clock[0] = 15
            release(service, "build")
            row = job(service, build)
            assert (row["state"], row["wait_reason"], row["next_check_at"]) == ("waiting", "capacity", stamp(60))
        for t in (15, 20, 30, 59):
            if t < started:
                at(service, clock, t)
                assert retried_at(service, build) is None, (schedule, t)
        if started == 90:
            at(service, clock, 60)
            assert retried_at(service, build) is None
        at(service, clock, started)
        assert retried_at(service, build) == stamp(started)


def blocked_retry(service, harness, clock) -> tuple[str, dict]:
    """Review r2 of #260, F2's input: the build's retry is due at 60 on codex-1, whose one slot a
    review holds, so from 60 the build's C-6.10 recheck backs off, to 121 by t=92. The review is a
    priority caller's (C-6.16), so the build's own wait keeps no slot from it (C-6.9). The build and
    the blocking review's attempt."""
    build = retrying(service, harness, clock)
    configure(service, harness, lambda daemon: daemon.policy["admission"].update(priority_callers=["blocker"]))
    blocker = submit(service, harness, "review", "review-1", pinned_lane="codex-1", caller_session="blocker")
    service._admit()
    [blocking] = attempts(service, blocker)
    assert blocking["lane_id"] == "codex-1"
    for t in (60, 61, 63, 67, 75, 91, 92):
        at(service, clock, t)
    row = job(service, build)
    assert (row["state"], row["wait_reason"], row["next_check_at"]) == ("waiting", "capacity", stamp(121)), dict(row)
    assert retried_at(service, build) is None
    return build, blocking


@pytest.mark.parametrize("schedule", ["unheld", "held-92-released-93", "held-92-expired-93"])
def test_a_capacity_recheck_after_the_backoff_is_no_deadline_a_hold_keeps(schedule):
    """F2: held at 92 and released (or expired) at 93, as the blocker finishes at 93. The hold kept
    the recheck clock (121) as if it were C-4.5's deadline, which passed at 60, and dropped the
    capacity wait, so the freed slot no longer brought the build forward: it started at 121, 28 s
    after the unheld control. Now the hold keeps only a retry deadline still ahead; the build is
    queued at the hold's end and starts at 93, as the control does."""
    with fresh_daemon(lanes=1, caps={"max_in_flight_per_lane": 1}) as (service, harness, clock, signals):
        build, blocking = blocked_retry(service, harness, clock)
        if schedule != "unheld":
            clock[0] = 92
            hold(service, "build", stamp(93))
            at(service, clock, 92)
            assert job(service, build)["wait_reason"] == "task-held"
            clock[0] = 93
            if schedule == "held-92-released-93":
                release(service, "build")
        clock[0] = 93
        finish(service, blocking)
        service._admit()
        assert retried_at(service, build) == stamp(93), \
            ("F2: the hold kept a capacity recheck as the retry's deadline", schedule, dict(job(service, build)))
        if schedule != "unheld":
            [ended] = service.store.query("SELECT data_json FROM events WHERE job_id=? AND kind='job.task_hold_ended'",
                                          (build,))
            assert '"next_check_at":null' in ended["data_json"]


def test_the_kept_backoff_survives_a_restart_while_held():
    with fresh_daemon() as (service, harness, clock, signals):
        build = retrying(service, harness, clock)
        clock[0] = 10
        hold(service, "build", stamp(20))
        at(service, clock, 10)
        clock[0] = 15
        service = restart(service, harness)
        at(service, clock, 20)                      # the end, after the restart
        assert job(service, build)["next_check_at"] == stamp(60)
        at(service, clock, 59)
        assert retried_at(service, build) is None
        at(service, clock, 60)
        assert retried_at(service, build) == stamp(60)


def test_a_job_held_from_the_queue_is_queued_again_with_no_clock():
    """A job with no clock of its own is woken at once, as before (T3)."""
    with fresh_daemon() as (service, harness, clock, signals):
        hold(service, "build", stamp(600))
        build = submit(service, harness, "build", "build-1")
        at(service, clock, 0)
        release(service, "build")
        row = job(service, build)
        assert (row["state"], row["wait_reason"], row["next_check_at"]) == ("queued", None, None)
        ended = service.store.query("SELECT data_json FROM events WHERE job_id=? AND kind='job.task_hold_ended'",
                                    (build,))
        assert [row["data_json"] for row in ended] == [
            daemon_module._event_json({"by": "release", "ended_at": stamp(0), "next_check_at": None,
                                       "task": "build"})]
