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

Fix round 3 (review r3 of #260): a look under way when the hold commits, stopped with barriers on
its own thread, writes nothing over `task-held` (R3-1); and a finalizer that runs long after the
provider's exit sets the deadline a hold keeps (R3-2).

Every case runs on the real daemon pass, in process, on a virtual clock.
"""

from __future__ import annotations

import contextlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from subfleet import daemon as daemon_module, protocol
from subfleet.contracts import TRANSIENT_RETRY_DELAY_S, Exit
from tests.disk_floor_model import stamp
from tests.task_hold_support import (attempts, configure, finalize_transient, finish, fresh_daemon, hold, job,
                                     legacy_retry, release, restart, submit)

GRACE = 60


@pytest.mark.parametrize("finalized_at", [0, 100])
def test_legacy_finalizer_deadline_survives_upgrade(finalized_at):
    with fresh_daemon() as (service, harness, clock, signals):
        build = legacy_retry(service, harness, clock, finalized_at)
        service = restart(service, harness)
        clock[0] = finalized_at + 10
        hold(service, "build", stamp(500))
        clock[0] = finalized_at + 11
        release(service, "build")
        service._admit()
        assert len(attempts(service, build)) == 1, "R4-legacy: retry started before its finalizer deadline"
        due = finalized_at + TRANSIENT_RETRY_DELAY_S
        assert job(service, build)["next_check_at"] in (stamp(due), stamp(due + 1))
        at(service, clock, due - 1)
        assert len(attempts(service, build)) == 1
        at(service, clock, due)
        if len(attempts(service, build)) == 1:
            at(service, clock, due + 1)
        assert len(attempts(service, build)) == 2


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


# --- fix round 3 (review r3 of #260): a look under way when the hold commits, a late finalizer ---


@contextlib.contextmanager
def look_under_way(service, patch, method: str):
    """One admission pass on its own thread, stopped as it enters `method` (once) until the block
    ends: the look under way when the hold commits (review r3 of #260, R3-1, run as r3 ran it). The
    pass holds no store lock while stopped, so a hold or release commits beside it."""
    entered = threading.Barrier(2, timeout=15)
    resume = threading.Barrier(2, timeout=15)
    original = getattr(service, method)
    stopped = [False]

    def stopping(*args, **kwargs):
        if not stopped[0]:
            stopped[0] = True
            entered.wait()
            resume.wait()
        return original(*args, **kwargs)
    patch.setattr(service, method, stopping)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service._admit)
        entered.wait()
        try:
            yield
        finally:
            resume.wait()
            future.result(timeout=20)


def test_a_pin_check_under_way_when_the_hold_commits_keeps_the_last_second(monkeypatch):
    """R3-1, executed: a 60 s grace from t=0, the pass that looks at 59 stopped in C-11.8's check,
    the hold committed at 59 and the pass resumed at 60. The look failed the held job rc 3 on its
    stale episode; the commit had paused the grace with 1 s left. Now the transaction that would
    fail it reads the hold, and the job keeps its second until the hold ends."""
    with fresh_daemon() as (service, harness, clock, signals):
        build = stuck(service, harness, clock, "pinned")
        clock[0] = 59
        with look_under_way(service, monkeypatch, "_stuck_pin"):
            hold(service, "build", stamp(1000))
            clock[0] = 60
        row = job(service, build)
        assert (row["state"], row["wait_reason"], row["next_check_at"]) == ("waiting", "task-held", stamp(1000)), \
            ("R3-1: a pin check under way failed a held job", dict(row))
        assert service._pin_episodes[build]["paused_s"] == 1
        clock[0] = 500
        assert release(service, "build")["woken"] == 1
        fails_exactly_at(service, clock, build, 501)


@pytest.mark.parametrize("ending,fail_at", [("retake", 660), ("release", 110), ("late-expiry", 1060)])
def test_an_episode_opened_under_way_when_the_hold_commits_is_paused_from_the_commit(ending, fail_at, monkeypatch):
    """R3-1, executed: the build's first look, at t=10, stopped as C-11.8 opens its episode; the
    hold committed (taking the queued job); the look then wrote its capacity wait, due at 70, over
    `task-held`. A release at 50 woke nothing and the job failed at 70; an expiry at 1000 seen at
    1030 failed it at once. Only a pass that took it again (the control, at 500) kept its 60 s. Now
    the opening's transaction reads the hold: the episode opens at 10 with its grace paused from
    the commit, as the commit's take pauses one, and the job stays `task-held`."""
    with fresh_daemon() as (service, harness, clock, signals):
        configure(service, harness, lambda daemon: daemon.policy["admission"].update(pin_grace_s=GRACE))
        service.store.update_lane("codex-1", enabled=0)
        build = submit(service, harness, "build", "build-1", pinned_lane="codex-1")
        clock[0] = 10
        with look_under_way(service, monkeypatch, "_stuck_pin"):
            hold(service, "build", stamp(1000))
        row = job(service, build)
        assert (row["wait_reason"], row["next_check_at"]) == ("task-held", stamp(1000)), \
            ("R3-1: the episode's opening wrote its capacity wait over task-held", dict(row))
        episode = service._pin_episodes[build]
        assert (episode["open"], episode["since"], episode["paused_s"]) == (True, stamp(10), 60)
        if ending == "retake":
            at(service, clock, 500)
            assert job(service, build)["wait_reason"] == "task-held"
            assert service._pin_episodes[build]["paused_s"] == 60
            clock[0] = 600
            assert release(service, "build")["woken"] == 1
        elif ending == "release":
            clock[0] = 50
            assert release(service, "build")["woken"] == 1
        else:
            at(service, clock, 1030)
            row = job(service, build)
            assert (row["state"], row["wait_reason"], row["next_check_at"]) == ("waiting", "capacity", stamp(1060))
        fails_exactly_at(service, clock, build, fail_at)


@pytest.mark.parametrize("held", [False, True])
def test_a_late_finalizers_deadline_is_the_one_a_hold_keeps(held):
    """R3-2, executed: the provider exits `transient` at t=0 and the finalizer runs at 100, so the
    retry is due at 160 while the attempt's end stays 0. The unheld control starts at 160. Held at
    110 and released at 111, it started at 111: the take judged the deadline as the end plus 60,
    passed at 60, and kept nothing. Now the take keeps the deadline the finalizer wrote."""
    with fresh_daemon() as (service, harness, clock, signals):
        build = submit(service, harness, "build", "build-1")
        service._admit()
        [first] = attempts(service, build)
        row = finalize_transient(service, first, clock=clock, finalized_at=100)
        assert (row["wait_reason"], row["next_check_at"]) == ("capacity", stamp(160))
        assert attempts(service, build)[0]["finished_at"] == stamp(0)
        if held:
            clock[0] = 110
            hold(service, "build", stamp(500))
            clock[0] = 111
            assert release(service, "build")["woken"] == 1
            row = job(service, build)
            assert (row["state"], row["wait_reason"], row["next_check_at"]) == ("waiting", "capacity", stamp(160)), \
                ("R3-2: the hold dropped the finalizer's deadline", dict(row))
        for t in (111, 159):
            at(service, clock, t)
            assert retried_at(service, build) is None, ("R3-2: the retry started before its deadline", t)
        at(service, clock, 160)
        assert retried_at(service, build) == stamp(160)


@pytest.mark.parametrize("site", ["route-deferral", "workspace-deferral", "admittable-again", "end-seen-late"])
def test_every_admission_write_under_way_when_the_hold_commits_takes_the_job(site, monkeypatch):
    """R3-1's fix, beyond r3's four inputs: every admission transaction that writes a job's wait
    reads the task's hold in that transaction (`Daemon._task_held_at_write`). Each look below is
    stopped as it reaches its write, the hold commits, and the look resumes; the job stays
    `task-held` until the hold's end, and this pass reports it so:

    - `route-deferral`: C-6.12's route wait (`_unroutable`);
    - `workspace-deferral`: C-6.8's backoff (`_workspace_failed`);
    - `admittable-again`: C-11.8's episode ending as its lane comes back (`_unstuck_pin`), which
      set the job due at once: the episode stays open, its grace paused, for the end to give back;
    - `end-seen-late`: a pass whose read of the holds came before a second hold committed found
      the first one ended (`_task_hold_ended`), and gave the job its grace back over the second."""
    with fresh_daemon() as (service, harness, clock, signals):
        method = {"route-deferral": "_unroutable", "workspace-deferral": "_workspace_failed",
                  "admittable-again": "_unstuck_pin", "end-seen-late": "_task_hold_ended"}[site]
        if site == "route-deferral":
            build = submit(service, harness, "build", "build-1")

            def unroutable(*args, **kwargs):
                raise daemon_module.Unroutable(RuntimeError("route probe"))
            monkeypatch.setattr(service, "_prepare_route", unroutable)
        elif site == "workspace-deferral":
            build = submit(service, harness, "build", "build-1")

            def unprepared(job_row):
                raise daemon_module.SalvageError("workspace probe", transient=True, public="workspace probe")
            monkeypatch.setattr(service, "_workspace", unprepared)
        else:
            build = stuck(service, harness, clock, "pinned")
            clock[0] = 10
            if site == "admittable-again":
                service.store.update_lane("codex-1", enabled=1)
            else:
                hold(service, "build", stamp(30))           # ends by its clock, seen by no pass
                clock[0] = 50
        with look_under_way(service, monkeypatch, method):
            hold(service, "build", stamp(1000))
        row = job(service, build)
        assert (row["state"], row["wait_reason"], row["next_check_at"]) == ("waiting", "task-held", stamp(1000)), \
            (f"R3-1: {method} under way wrote over the committed hold", dict(row))
        assert service._holds[build]["reason"] == "task-held"
        assert attempts(service, build) == []
        if site in ("admittable-again", "end-seen-late"):
            # The grace left: 50 s at the hold's start at 10; or, held 10 to 30 and not held 30 to 50,
            # 30 s at the second hold's start at 50. As the store has it (a pass reads it next look).
            episode = service._load_pin_episodes(build)[build]
            left = 50 if site == "admittable-again" else 30
            assert episode["open"] and episode["paused_s"] == left, episode
            clock[0] = 500
            assert release(service, "build")["woken"] == 1
            row = job(service, build)
            assert (row["wait_reason"], row["next_check_at"]) == ("capacity", stamp(500 + left))


@pytest.mark.parametrize("ending", ["release", "replacement"])
def test_stale_door_take_uses_current_hold_at_write(ending, monkeypatch):
    with fresh_daemon() as (service, harness, clock, signals):
        build = stuck(service, harness, clock, "pinned")
        before_read, read_on, before_take, take_on = [threading.Barrier(2, timeout=20) for _ in range(4)]
        original_read, original_take = service._task_holds_now, service._task_held
        once = [False]

        def read(*args, **kwargs):
            if not once[0]:
                once[0] = True
                before_read.wait()
                read_on.wait()
            return original_read(*args, **kwargs)

        def take(*args, **kwargs):
            before_take.wait()
            take_on.wait()
            return original_take(*args, **kwargs)

        monkeypatch.setattr(service, "_task_holds_now", read)
        monkeypatch.setattr(service, "_task_held", take)
        clock[0] = 10
        with ThreadPoolExecutor(max_workers=1) as pool:
            admission = pool.submit(service._admit_kind, "detached")
            before_read.wait()
            hold(service, "build", stamp(1000))
            read_on.wait()
            before_take.wait()
            clock[0] = 20
            if ending == "release":
                assert release(service, "build")["woken"] == 1
            else:
                hold(service, "build", stamp(2000))
            clock[0] = 30
            take_on.wait()
            admission.result(timeout=25)
        monkeypatch.setattr(service, "_task_holds_now", original_read)
        monkeypatch.setattr(service, "_task_held", original_take)
        if ending == "release":
            at(service, clock, 30)

            assert job(service, build)["next_check_at"] == stamp(70), (
                "R4-door-release: stale take extends the grace from 70 to 90", dict(job(service, build)))
            fails_exactly_at(service, clock, build, 70)
        else:
            clock[0] = 1500
            release(service, "build")
            restored = dict(job(service, build))
            service._admit()

            assert job(service, build)["state"] == "waiting", (
                "R4-door-replacement: stale until failed the held job at 1500 instead of 1550", dict(job(service, build)))
            assert restored["next_check_at"] == stamp(1550)


@pytest.mark.parametrize("operation", ["hold", "release"])
def test_hold_and_release_clocks_are_sampled_after_finalizer_lock_wait(operation, monkeypatch):
    with fresh_daemon() as (service, harness, clock, signals):
        build = stuck(service, harness, clock, "pinned")
        review = submit(service, harness, "review", "lock-owner", pinned_lane="codex-2")
        service._admit()
        [attempt] = attempts(service, review)
        clock[0] = 10
        if operation == "release":
            hold(service, "build", stamp(1000))
            clock[0] = 20
        finalizer_inside = threading.Barrier(2, timeout=20)
        finalizer_resume = threading.Barrier(2, timeout=20)
        request_waiting = threading.Barrier(2, timeout=20)
        original = service.store.transaction

        @contextlib.contextmanager
        def transaction(kind="state.changed", **kwargs):
            if kind == ("task.held" if operation == "hold" else "task.released"):
                request_waiting.wait()
            with original(kind, **kwargs) as tx:
                if kind == "attempt.accepted":
                    finalizer_inside.wait()
                    finalizer_resume.wait()
                yield tx

        monkeypatch.setattr(service.store, "transaction", transaction)
        with ThreadPoolExecutor(max_workers=2) as pool:
            finishing = pool.submit(finalize_transient, service, attempt)
            finalizer_inside.wait()
            changing = pool.submit(hold, service, "build", stamp(1000)) if operation == "hold" else pool.submit(release, service, "build")
            request_waiting.wait()
            clock[0] = 50
            finalizer_resume.wait()
            finishing.result(timeout=25)
            changing.result(timeout=25)
        monkeypatch.setattr(service.store, "transaction", original)
        if operation == "hold":
            clock[0] = 100
            release(service, "build")
            expected = 110
        else:
            expected = 100

        assert job(service, build)["next_check_at"] == stamp(expected), (
            f"R4-commit-clock: {operation} used its clock before the finalizer's lock wait", dict(job(service, build)))


# Fix round 5: the review's executed inputs, on the real pass and writer.
def events(service, job_id, kind):
    return [json.loads(r["data_json"]) for r in service.store.query(
        "SELECT data_json FROM events WHERE job_id=? AND kind=? ORDER BY event_id", (job_id, kind))]


@pytest.mark.parametrize("h1_at", [40, 55], ids=["H1-at-40-restores-135", "H1-at-55-waits-until-120"])
def test_r5_1_door_gives_an_ended_hold_back_twice(h1_at, monkeypatch):
    with fresh_daemon() as (service, harness, clock, signals):
        build = stuck(service, harness, clock, "pinned")            # grace 60 from 0
        clock[0] = 10
        hold(service, "build", stamp(25))                            # H0: 50 s left from 25
        before_read, read_on = threading.Barrier(2, timeout=20), threading.Barrier(2, timeout=20)
        original_read = service._task_holds_now
        once = [False]

        def read(*args, **kwargs):
            if not once[0]:
                once[0] = True
                before_read.wait()
                read_on.wait()
            return original_read(*args, **kwargs)
        monkeypatch.setattr(service, "_task_holds_now", read)
        clock[0] = h1_at - 2
        with ThreadPoolExecutor(max_workers=1) as pool:
            admission = pool.submit(service._admit_kind, "detached")
            before_read.wait()                                       # rows copied: task-held, clock 25
            clock[0] = h1_at
            hold(service, "build", stamp(1000))                      # H1 ends H0 and takes the job
            read_on.wait()
            admission.result(timeout=25)
        monkeypatch.setattr(service, "_task_holds_now", original_read)
        takes = events(service, build, "job.task_held")
        ended = events(service, build, "job.task_hold_ended")
        clock[0] = 100
        release(service, "build")
        released = dict(job(service, build))
        at(service, clock, 100)
        after = dict(job(service, build))
        correct = 175 - h1_at
        print("R5-1", {"h1_at": h1_at, "paused": [t.get("pin_paused_s") for t in takes],
                       "ended": [(e.get("ended_at"), e.get("pin_fail_at")) for e in ended],
                       "released_next_check": released["next_check_at"], "correct": stamp(correct),
                       "at_100": (after["state"], after["rc"], after["wait_reason"])})
        assert [t.get("pin_paused_s") for t in takes] == [50.0, 75.0 - h1_at], (
            "R5-1: the door gave H0's end back again over H1's take", takes)
        assert (after["state"], after["wait_reason"], after["next_check_at"]) == (
            "waiting", "capacity", stamp(correct)), ("R5-1: grace lost to a second give-back", after)
        fails_exactly_at(service, clock, build, correct)


def test_r5_1b_door_ends_a_later_hold_at_the_copied_clock(monkeypatch):
    with fresh_daemon() as (service, harness, clock, signals):
        build = stuck(service, harness, clock, "pinned")
        clock[0] = 10
        hold(service, "build", stamp(25))
        before_read, read_on = threading.Barrier(2, timeout=20), threading.Barrier(2, timeout=20)
        original_read = service._task_holds_now
        once = [False]

        def read(*args, **kwargs):
            if not once[0]:
                once[0] = True
                before_read.wait()
                read_on.wait()
            return original_read(*args, **kwargs)
        monkeypatch.setattr(service, "_task_holds_now", read)
        clock[0] = 38
        with ThreadPoolExecutor(max_workers=1) as pool:
            admission = pool.submit(service._admit_kind, "detached")
            before_read.wait()
            clock[0] = 40
            hold(service, "build", stamp(42))
            committed = dict(job(service, build))
            clock[0] = 46
            read_on.wait()
            admission.result(timeout=25)
        monkeypatch.setattr(service, "_task_holds_now", original_read)
        row = dict(job(service, build))
        ended = events(service, build, "job.task_hold_ended")
        print("R5-1b", {"after_commit": (committed["wait_reason"], committed["next_check_at"]),
                        "after_door": (row["wait_reason"], row["next_check_at"]),
                        "ended": [(e.get("ended_at"), e.get("pin_fail_at")) for e in ended],
                        "correct": stamp(77)})
        assert (committed["wait_reason"], committed["next_check_at"]) == ("task-held", stamp(42))
        assert (row["wait_reason"], row["next_check_at"]) == ("capacity", stamp(77)), (
            "R5-1b: the door ended H1 at the copy's clock", row, ended)
        fails_exactly_at(service, clock, build, 77)


def test_a_quarantined_probes_wait_has_no_person_answer(monkeypatch):
    """R5-4a: confirm-dead cannot resolve a probe; a person can cancel its job."""
    from tests.fake.test_probe_recovery import reserved_probe, exit_receipts
    with fresh_daemon() as (service, harness, clock, signals):
        build = submit(service, harness, "build", "probe-person")
        record = reserved_probe(service, build, state="quarantined")
        exit_receipts(record)
        hold(service, "build", stamp(1000))
        from subfleet.procs import Containment, ProcessIdentity
        census = Containment(marker_pids=frozenset({900003}), identities={
            900003: ProcessIdentity(900003, "boot", "escaped")})
        monkeypatch.setattr(service, "_probe_census", lambda record: census)
        service._recover_probes()
        assert job(service, build)["wait_reason"] == "uncertain"
        answer = service.kill(protocol.KillArgs(job_id=build, confirm_dead=True, operator_note="checked"))
        print("R5-PROBE-CONFIRM-DEAD", answer)
        assert answer["status"] == "not quarantined"
        assert job(service, build)["wait_reason"] == "uncertain"
        cancelled = service.kill(protocol.KillArgs(job_id=build))
        print("R5-PROBE-CANCEL", cancelled, job(service, build)["state"])
        assert job(service, build)["state"] == "cancelled"


def legacy_finalized(service, harness, clock, patch, *, finalized_at, stall):
    """As legacy_retry, execute real finalization then remove only retry_due_at.

    Move the wall clock after its deadline read and before its attempt.accepted
    stamp. This pins the reviewed input without depending on Git history in CI.
    """
    build = submit(service, harness, "build", "legacy-stall")
    service._admit()
    [attempt] = attempts(service, build)
    fake_after = daemon_module.after
    moved = [False]

    def after(seconds):
        value = fake_after(seconds)
        if seconds == TRANSIENT_RETRY_DELAY_S and not moved[0]:
            moved[0] = True
            clock[0] += stall
        return value
    with patch.context() as inner:
        inner.setattr(daemon_module, "after", after)
        finalize_transient(service, attempt, clock=clock, finalized_at=finalized_at)
    assert moved[0]
    evidence = json.loads(attempts(service, build)[0]["evidence_json"])
    assert evidence.pop("retry_due_at") == stamp(finalized_at + TRANSIENT_RETRY_DELAY_S)
    with service.store.transaction("test.legacy_attempt", attempt_id=attempt["attempt_id"]) as tx:
        tx.execute("UPDATE attempts SET evidence_json=? WHERE attempt_id=?",
                   (json.dumps(evidence), attempt["attempt_id"]))
    accepted = service.store.one("SELECT ts FROM events WHERE kind='attempt.accepted' AND attempt_id=?",
                                 (attempt["attempt_id"],))
    return build, accepted["ts"]


@pytest.mark.parametrize("schedule", ["held-92-released-93", "held-92-expired-93"])
def test_legacy_recheck_after_the_backoff_is_no_deadline(schedule):
    """r2's F2 input on a legacy row (no `retry_due_at`): the retry's deadline passed at 60 and
    the job's clock (121) is a C-6.10 recheck. The hold keeps nothing; the build starts at 93."""
    with fresh_daemon(lanes=1, caps={"max_in_flight_per_lane": 1}) as (service, harness, clock, signals):
        build, blocking = blocked_retry(service, harness, clock)
        first = attempts(service, build)[0]
        evidence = json.loads(first["evidence_json"])
        evidence.pop("retry_due_at")
        with service.store.transaction("test.legacy_attempt", attempt_id=first["attempt_id"]) as tx:
            tx.execute("UPDATE attempts SET evidence_json=? WHERE attempt_id=?", (json.dumps(evidence), first["attempt_id"]))
        clock[0] = 92
        hold(service, "build", stamp(93))
        at(service, clock, 92)
        take = events(service, build, "job.task_held")[-1]
        assert take["kept"] is None, ("R5-legacy-F2: the hold kept a recheck as a legacy deadline", take)
        clock[0] = 93
        if schedule == "held-92-released-93":
            release(service, "build")
        finish(service, blocking)
        service._admit()
        assert retried_at(service, build) == stamp(93), (
            "R5-legacy-F2: the hold kept a recheck as a legacy deadline", dict(job(service, build)))


def behind_finalizer(service, harness, clock, patch, kind, request, *, request_at, commit_at=50):
    """A real finalizer of an unrelated review holds the store writer; `request()` (whose
    transaction is `kind`) is made at `request_at` and waits for it; the finalizer lets
    go at `commit_at`. The request's answer, or the exception it raised."""
    review = submit(service, harness, "review", "lock-owner", pinned_lane="codex-2")
    service._admit()
    [attempt] = attempts(service, review)
    inside, resume, waiting = (threading.Barrier(2, timeout=20) for _ in range(3))
    original = service.store.transaction

    @contextlib.contextmanager
    def transaction(name="state.changed", **kwargs):
        if name == kind:
            waiting.wait()
        with original(name, **kwargs) as tx:
            if name == "attempt.accepted":
                inside.wait()
                resume.wait()
            yield tx
    patch.setattr(service.store, "transaction", transaction)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            finishing = pool.submit(finalize_transient, service, attempt)
            inside.wait()
            clock[0] = request_at
            changing = pool.submit(request)
            waiting.wait()
            clock[0] = commit_at
            resume.wait()
            finishing.result(timeout=25)
            try:
                return changing.result(timeout=25)
            except Exception as exc:              # noqa: BLE001
                return exc
    finally:
        patch.setattr(service.store, "transaction", original)


def test_hold_replacement_requested_before_the_first_hold_expires_commits_after(monkeypatch):
    """Grace 60 from 0; hold to 30 at 10; a replacement to 1000 asked at 20 commits at 50,
    after the first hold ended unseen at 30. Used: 0-10 and 30-50 (30 s); 30 s are left at
    50, so a release at 100 restores 130 (the request's clock would say 150)."""
    with fresh_daemon() as (service, harness, clock, signals):
        build = stuck(service, harness, clock, "pinned")
        clock[0] = 10
        hold(service, "build", stamp(30))
        answer = behind_finalizer(service, harness, clock, monkeypatch, "task.held",
                                  lambda: hold(service, "build", stamp(1000)), request_at=20)
        assert isinstance(answer, dict), answer
        print("R5-EXPIRY-DURING-HOLD-WAIT", {"replaced": answer["replaced"], "since": answer["task_hold"]["since"]})
        assert answer["replaced"] is None and answer["task_hold"]["since"] == stamp(50), (
            "R5-hold-expiry-wait: replacement judged at the request's clock", answer)
        clock[0] = 100
        release(service, "build")
        assert job(service, build)["next_check_at"] == stamp(130), (
            "R5-hold-expiry-wait: replacement judged at the request's clock", dict(job(service, build)))
        fails_exactly_at(service, clock, build, 130)


def test_release_requested_before_the_hold_expires_commits_after(monkeypatch):
    """Hold to 30 at 10; a release asked at 20 commits at 50, after the hold ended unseen at
    30: the grace runs again from 30 with its 50 s, due 80 (the request's clock: 70)."""
    with fresh_daemon() as (service, harness, clock, signals):
        build = stuck(service, harness, clock, "pinned")
        clock[0] = 10
        hold(service, "build", stamp(30))
        answer = behind_finalizer(service, harness, clock, monkeypatch, "task.released",
                                  lambda: release(service, "build"), request_at=20)
        print("R5-EXPIRY-DURING-RELEASE-WAIT", answer, dict(job(service, build))["next_check_at"])
        assert answer["was_held"] is False and answer["woken"] == 1
        assert job(service, build)["next_check_at"] == stamp(80), dict(job(service, build))
        fails_exactly_at(service, clock, build, 80)


def test_hold_whose_end_passes_during_the_writer_wait_is_refused(monkeypatch):
    with fresh_daemon() as (service, harness, clock, signals):
        build = stuck(service, harness, clock, "pinned")
        answer = behind_finalizer(service, harness, clock, monkeypatch, "task.held",
                                  lambda: hold(service, "build", stamp(40)), request_at=20)
        print("R5-UNTIL-PASSED-DURING-WAIT", repr(answer))
        assert isinstance(answer, protocol.ProtocolError) and answer.reason == "task-hold-until", (
            "R5-until-wait: a hold already past its end was written", answer)
        assert not service.store.query("SELECT 1 FROM leases WHERE lease_key LIKE 'task-hold:%'")
        assert not service.store.query("SELECT 1 FROM events WHERE kind='task.held'")
        fails_exactly_at(service, clock, build, 60)


@pytest.mark.documented_limitation
def test_legacy_clock_step_back_is_an_intended_deadline_violation(monkeypatch):
    """R5-4b: a -5 s wall-clock step makes the legacy retry start at 155, against 160."""
    with fresh_daemon() as (service, harness, clock, signals):
        build, accepted = legacy_finalized(service, harness, clock, monkeypatch,
                                           finalized_at=100, stall=-5)
        assert accepted == stamp(95)
        assert job(service, build)["next_check_at"] == stamp(160)
        service = restart(service, harness)
        clock[0] = 96
        hold(service, "build", stamp(1000))
        clock[0] = 97
        release(service, "build")
        assert job(service, build)["next_check_at"] == stamp(155)
        at(service, clock, 154)
        assert retried_at(service, build) is None
        at(service, clock, 155)
        assert retried_at(service, build) == stamp(155) < stamp(160)
