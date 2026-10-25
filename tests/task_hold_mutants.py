"""The task hold's mutants (C-11.10, D-TASKHOLD1), each a patch on one seam of the daemon.

`tests/fake/test_task_hold_mutants.py` pairs each with the scenario that must
catch it: the unmutated daemon passes the scenario, the mutant fails its named
assertion. `TASK_HOLD_MUTANT=<name>` applies one to the property tests too
(`tests/fake/test_task_hold_properties.py`), to see which invariant notices it.
"""

from __future__ import annotations

from subfleet import daemon as daemon_module, task_holds
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


# --- review r1's four (run against the properties in fix round 1) -----------------------------

def drop_transaction_recheck(patch):
    """The reserving transaction never reads the hold again: only the door's look holds a job."""
    original = Daemon._task_holds_now
    patch.setattr(Daemon, "_task_holds_now",
                  lambda self, read=None: {} if read is not None else original(self))


def hold_counted_as_capacity(patch):
    """Admission's lease census (C-6.10) counts a hold's row as capacity in use."""
    patch.setattr(daemon_module, "LEASE_CENSUS_SKIPS", ("", ""))


def strict_expiry_boundary(patch):
    """A hold is still in force at the instant of its end (`until >= now`)."""
    def in_force_now(hold, now):
        if not hold:
            return False
        current = task_holds.instant(now) if isinstance(now, str) else now
        return task_holds.instant(hold["until"]) >= current
    patch.setattr(task_holds, "in_force_now", in_force_now)


def startup_clears_leases(patch):
    """A daemon start clears every lease row, the holds among them."""
    original = Daemon.__init__

    def start(self, *args, **kwargs):
        original(self, *args, **kwargs)
        with self.store.transaction("test.mutant_clear_leases") as tx:
            tx.execute("DELETE FROM leases")
    patch.setattr(Daemon, "__init__", start)


# --- one per decision of fix round 1 ------------------------------------------------------------

def grace_runs_while_held(patch):
    """P2-1: a hold takes a job without pausing its pin grace (as the build shipped)."""
    original = Daemon._take_for_hold

    def take(self, tx, job, hold, *, data=None):
        episode = self._pin_episodes.pop(job["job_id"], None)
        try:
            return original(self, tx, job, hold, data=data)
        finally:
            if episode is not None:
                self._pin_episodes[job["job_id"]] = episode
    patch.setattr(Daemon, "_take_for_hold", take)


def release_drops_retry_backoff(patch):
    """P2-2: a hold keeps no clock of the job's, so its end starts a retry early."""
    original = Daemon._take_for_hold

    def take(self, tx, job, hold, *, data=None):
        paused = original(self, tx, job, hold, data=data)
        if data is not None and data.get("kept"):
            data["kept"] = None
        return paused
    patch.setattr(Daemon, "_take_for_hold", take)


def held_job_is_demand(patch):
    """P2-3: `_reset_demand` counts a job a hold keeps, until admission looks at it again."""
    patch.setattr(Daemon, "_demand_held", lambda self, job, holds: False)


def pending_ignores_holds(patch):
    """P2-3: the reset action's pending transaction does not read the hold again."""
    patch.setattr(task_holds, "job_held", lambda conn, job_id, now: None)


def offline_counts_the_display_cap(patch):
    """P3-1: status counts a hold's waiting jobs from the jobs it lists (offline: 50)."""
    original = task_holds.listing
    patch.setattr(task_holds, "listing", lambda holds, waiting=None: original(holds))


def note_strips_before_checking(patch):
    """P3-2: a note is trimmed of every space and control before it is checked."""
    original = task_holds.note_text
    patch.setattr(task_holds, "note_text",
                  lambda value: original(value.strip()) if isinstance(value, str) else original(value))


def release_hint_unquoted(patch):
    """P3-3: the release hint pastes the task into the command as it is."""
    patch.setattr(task_holds, "release_command", lambda task: f"subfleet tasks release {task}")


def release_skips_notify(patch):
    """Release does all it should but wake the daemon's waiters: the notification alone is gone."""
    original = Daemon._release_task

    def release(self, a):
        notify = self._notify
        self._notify = lambda: None
        try:
            return original(self, a)
        finally:
            self._notify = notify
    patch.setattr(Daemon, "_release_task", release)


MUTANTS = {
    "drop-admission-check": drop_admission_check,
    "hold-every-task": hold_every_task,
    "hold-turns": hold_turns,
    "ignore-until": ignore_until,
    "release-without-notify": release_without_notify,
    "replace-running-attempts": replace_running_attempts,
    "accept-unknown-task": accept_unknown_task,
    "drop-transaction-recheck": drop_transaction_recheck,
    "hold-counted-as-capacity": hold_counted_as_capacity,
    "strict-expiry-boundary": strict_expiry_boundary,
    "startup-clears-leases": startup_clears_leases,
    "grace-runs-while-held": grace_runs_while_held,
    "release-drops-retry-backoff": release_drops_retry_backoff,
    "held-job-is-demand": held_job_is_demand,
    "pending-ignores-holds": pending_ignores_holds,
    "offline-counts-the-display-cap": offline_counts_the_display_cap,
    "note-strips-before-checking": note_strips_before_checking,
    "release-hint-unquoted": release_hint_unquoted,
    "release-skips-notify": release_skips_notify,
}
