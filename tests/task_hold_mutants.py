"""The task hold's mutants (C-11.10, D-TASKHOLD1), each a patch on one seam of the daemon.

`tests/fake/test_task_hold_mutants.py` pairs each with the scenario that must
catch it: the unmutated daemon passes the scenario, the mutant fails its named
assertion. `TASK_HOLD_MUTANT=<name>` applies one to the property tests too
(`tests/fake/test_task_hold_properties.py`), to see which invariant notices it.
"""

from __future__ import annotations

from subfleet import task_holds
from subfleet.daemon import Daemon


def drop_admission_check(patch):
    """No job is ever held: neither the door nor the reserving transaction checks."""
    patch.setattr(Daemon, "_task_hold_for", lambda self, job, holds: None)


def hold_every_task(patch):
    """Any hold in force holds every detached job, whatever its task."""
    patch.setattr(Daemon, "_task_hold_for",
                  lambda self, job, holds: next(iter(holds.values()), None) if job["kind"] != "turn" else None)


def hold_turns(patch):
    """A turn of a held task is held like a detached job."""
    patch.setattr(Daemon, "_task_hold_for",
                  lambda self, job, holds: holds.get(job["task"]) if job["task"] else None)


def ignore_until(patch):
    """A hold's end is never read: once set, it holds until released."""
    patch.setattr(task_holds, "in_force_now", lambda hold, now: bool(hold))


def release_without_notify(patch):
    """Release deletes the row and nothing else: no job is woken, no pass is told."""
    def release(self, a):
        with self.store.transaction("task.released", data={"task": a.task}) as tx:
            tx.execute("DELETE FROM leases WHERE lease_key=?", (task_holds.lease_key(a.task),))
        return {"released": a.task, "was_held": True, "woken": 0, "task_holds": []}
    patch.setattr(Daemon, "_release_task", release)


def replace_running_attempts(patch):
    """A hold also stops the held task's live attempts and puts their jobs back in the queue."""
    original = Daemon._hold_task

    def hold(self, a):
        answer = original(self, a)
        with self.store.transaction("test.mutant_replace", data={"task": a.task}) as tx:
            rows = tx.execute("SELECT a.attempt_id, a.job_id FROM attempts a JOIN jobs j USING(job_id) "
                              "WHERE j.task=? AND j.kind!='turn' AND a.state IN "
                              "('reserved','starting','running','finalizing')", (a.task,)).fetchall()
            for attempt_id, job_id in rows:
                tx.execute("UPDATE attempts SET state='interrupted' WHERE attempt_id=?", (attempt_id,))
                tx.execute("UPDATE jobs SET state='queued' WHERE job_id=?", (job_id,))
                tx.execute("DELETE FROM leases WHERE holder=?", (attempt_id,))
        return answer
    patch.setattr(Daemon, "_hold_task", hold)


def accept_unknown_task(patch):
    """Any nonempty name is a task: a typo holds nothing and says nothing."""
    patch.setattr(Daemon, "_known_task", lambda self, task: isinstance(task, str) and bool(task))


MUTANTS = {
    "drop-admission-check": drop_admission_check,
    "hold-every-task": hold_every_task,
    "hold-turns": hold_turns,
    "ignore-until": ignore_until,
    "release-without-notify": release_without_notify,
    "replace-running-attempts": replace_running_attempts,
    "accept-unknown-task": accept_unknown_task,
}
