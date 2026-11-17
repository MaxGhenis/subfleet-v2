"""C-11.10 (D-RO-ADMIT1, fix r1 of #309): what a release without its own row, and
what a hold's commit, may touch.

- "A release with no row changes nothing and records nothing." Review r1's probe p5:
  a sandbox release with no sandbox row woke a job that a stale task hold kept
  waiting. Each case here has a control in which the released target's row exists.
- Review r1's probe p4: a sandbox hold's commit takes the task-bearing
  workspace-write jobs it covers and leaves a taskless workspace-write job and a
  workspace-write turn queued; each of those gets its one attempt.
"""

from __future__ import annotations

import json

import pytest

from tests.disk_floor_model import stamp
from tests.fake.test_sandbox_hold_properties import fleet, jobs_from, sandbox_hold, sandbox_release
from tests.task_hold_support import attempts, hold, job, release, store_image


HOLD_EVENTS = ("SELECT kind, job_id, data_json FROM events WHERE kind IN "
               "('task.held','job.task_held','task.released','job.task_hold_ended','job.task_hold_moved') "
               "ORDER BY event_id")
SANDBOX_KEY = "sandbox-hold:workspace-write"


def hold_events(service, after: int = 0) -> list[tuple]:
    return [(row["kind"], row["job_id"], json.loads(row["data_json"]))
            for row in service.store.query(HOLD_EVENTS)][after:]


def stale_hold_of(service, harness, clock, kind: str) -> tuple[str, str]:
    """A workspace-write job held by a `kind` hold that ended at 50, unseen: no pass ran
    since. Returns (job id, the task of the job)."""
    task = "build" if kind == "task" else "review"
    ident = jobs_from(service, harness, [(task, "workspace-write", "dispatch")])["generated-0"]
    if kind == "task":
        hold(service, "build", stamp(50), note="task brake")
    else:
        sandbox_hold(service, 50, note="disk brake")
    held = job(service, ident)
    assert (held["state"], held["wait_reason"], held["next_check_at"]) == ("waiting", "task-held", stamp(50))
    assert attempts(service, ident) == []
    clock[0] = 60
    return ident, task


@pytest.mark.parametrize("stale,released", [("task", "sandbox"), ("sandbox", "task")],
                         ids=["p5-sandbox-release", "task-release"])
def test_a_release_with_no_row_of_its_target_changes_and_records_nothing(stale, released):
    """p5 and its mirror: the stale hold was the other kind, and the released target has
    no row. The reply says nothing was held or woken; every row is as it was, and a
    pass then gives the job back at its own hold's end (50), not at the release."""
    with fleet() as (service, harness, clock, signals):
        ident, task = stale_hold_of(service, harness, clock, stale)
        before = store_image(service)
        answer = sandbox_release(service) if released == "sandbox" else release(service, task)
        assert answer == {"released": "workspace-write" if released == "sandbox" else task,
                          "was_held": False, "woken": 0, "task_holds": []}
        assert store_image(service) == before, "a release with no row changed or recorded something"
        events = len(hold_events(service))
        service._admit()
        ended = [event for event in hold_events(service, events) if event[0] == "job.task_hold_ended"]
        assert ended == [("job.task_hold_ended", ident, {
            "by": "end", **({"task": "build"} if stale == "task" else {"sandbox": "workspace-write"}),
            "ended_at": stamp(50), "next_check_at": None})]
        assert len(attempts(service, ident)) == 1
        assert signals == []


@pytest.mark.parametrize("stale,released", [("task", "sandbox"), ("sandbox", "task")],
                         ids=["p5-sandbox-release", "task-release"])
def test_control_a_release_whose_target_has_a_row_still_wakes_the_stale_job(stale, released):
    """The control: as above, but the released target's row exists too, ended at 40,
    so both holds covered the job until the later end, 50. The release deletes its row,
    records `task.released`, and gives the job its clocks back from 50, as C-11.10's
    release of a hold already past its end does."""
    with fleet() as (service, harness, clock, signals):
        clock[0] = 0
        task = "build" if stale == "task" else "review"
        ident = jobs_from(service, harness, [(task, "workspace-write", "dispatch")])["generated-0"]
        if stale == "task":
            hold(service, "build", stamp(50), note="task brake")
            sandbox_hold(service, 40, note="disk brake")
        else:
            sandbox_hold(service, 50, note="disk brake")
            hold(service, "review", stamp(40), note="task brake")
        assert job(service, ident)["next_check_at"] == stamp(50)
        clock[0] = 60
        events = len(hold_events(service))
        answer = sandbox_release(service) if released == "sandbox" else release(service, task)
        assert answer == {"released": "workspace-write" if released == "sandbox" else task,
                          "was_held": False, "woken": 1, "task_holds": []}
        released_target = {"sandbox": "workspace-write"} if released == "sandbox" else {"task": task}
        stale_target = {"task": "build"} if stale == "task" else {"sandbox": "workspace-write"}
        note = "disk brake" if released == "sandbox" else "task brake"
        assert hold_events(service, events) == [
            ("job.task_hold_ended", ident, {"by": "release", **stale_target, "ended_at": stamp(50),
                                           "next_check_at": None}),
            ("task.released", None, {**released_target, "until": stamp(40), "note": note, "woken": 1}),
        ]
        assert job(service, ident)["state"] == "queued"
        released_key = SANDBOX_KEY if released == "sandbox" else f"task-hold:{task}"
        kept_key = f"task-hold:{task}" if released == "sandbox" else SANDBOX_KEY
        assert service.store.one("SELECT 1 FROM leases WHERE lease_key=?", (released_key,)) is None
        assert service.store.one("SELECT 1 FROM leases WHERE lease_key=?", (kept_key,)) is not None
        assert signals == []


def test_a_release_with_no_row_and_no_held_job_is_a_no_op_reply():
    """Nothing held anywhere: either target's release answers and writes nothing."""
    with fleet() as (service, harness, clock, signals):
        jobs_from(service, harness, [("build", "workspace-write", "dispatch")])
        before = store_image(service)
        assert sandbox_release(service) == {"released": "workspace-write", "was_held": False,
                                            "woken": 0, "task_holds": []}
        assert release(service, "build") == {"released": "build", "was_held": False,
                                             "woken": 0, "task_holds": []}
        assert store_image(service) == before


def test_p4_the_sandbox_hold_commit_leaves_taskless_jobs_and_turns_queued():
    """p4: with jobs queued before the hold, the commit takes the covered review only;
    a taskless workspace-write job and a workspace-write turn stay queued, untouched,
    and each gets exactly one attempt at the next pass."""
    with fleet() as (service, harness, clock, signals):
        jobs = jobs_from(service, harness, [("review", "workspace-write", "dispatch"),
                                            (None, "workspace-write", "dispatch"),
                                            ("build", "workspace-write", "turn"),
                                            (None, "", "dispatch")])
        covered, taskless, turn, legacy = (jobs[f"generated-{n}"] for n in range(4))
        rows = {ident: job(service, ident) for ident in (taskless, turn, legacy)}
        assert all(row["state"] == "queued" for row in rows.values())
        events = len(hold_events(service))
        sandbox_hold(service, 100)
        assert [(kind, ident) for kind, ident, _ in hold_events(service, events)] == [
            ("job.task_held", covered), ("task.held", None)]
        assert (job(service, covered)["wait_reason"], job(service, covered)["next_check_at"]) == (
            "task-held", stamp(100))
        for ident, row in rows.items():
            assert job(service, ident) == row, "the hold's commit touched an exempt job"
        service._admit()
        assert attempts(service, covered) == []
        for ident in (taskless, turn, legacy):
            assert len(attempts(service, ident)) == 1, "an exempt job was held or placed twice"
        service._admit()
        assert [len(attempts(service, ident)) for ident in (covered, taskless, turn, legacy)] == [0, 1, 1, 1]
        assert signals == []
