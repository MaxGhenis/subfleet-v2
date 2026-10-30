"""The task hold's mutants (C-11.10, D-TASKHOLD1), each a patch on one seam of the daemon.

`tests/fake/test_task_hold_mutants.py` pairs each with the scenario that must
catch it: the unmutated daemon passes the scenario, the mutant fails its named
assertion. `TASK_HOLD_MUTANT=<name>` applies one to the property tests too
(`tests/fake/test_task_hold_properties.py`), to see which invariant notices it.
"""

from __future__ import annotations

import inspect
import textwrap

from subfleet import daemon as daemon_module, task_holds
from subfleet.contracts import TRANSIENT_RETRY_DELAY_S
from subfleet.daemon import Daemon


def replace_method(patch, name, old, new):
    source = textwrap.dedent(inspect.getsource(getattr(Daemon, name)))
    assert old in source
    namespace = {}
    exec(source.replace(old, new), vars(daemon_module), namespace)
    patch.setattr(Daemon, name, namespace[name])


def door_without_reread(patch):
    replace_method(patch, "_task_held", "current = self._task_held_at_write(inner, job, holds)",
                   "current = (self._take_for_hold(inner, job, hold), hold)[1]")


def legacy_deadline_from_end(patch):
    replace_method(patch, "_kept_retry", "deadline = _later(accepted[0], TRANSIENT_RETRY_DELAY_S)",
                   "deadline = _later(row[0], TRANSIENT_RETRY_DELAY_S)")


def handler_clock_before_writer(patch):
    for method, kind in (("_hold_task", "task.held"), ("_release_task", "task.released")):
        transaction = f'with self.store.transaction("{kind}", data=data) as tx:'
        replace_method(patch, method, transaction + "\n        now = utcnow()",
                       "now = utcnow()\n    " + transaction)


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
    """P2-1: a hold takes a job without pausing its pin grace (as the build shipped): the take
    reads no pin episode."""
    patch.setattr(Daemon, "_held_pin_episode", lambda self, tx, job_id: {})


def release_drops_retry_backoff(patch):
    """P2-2: a hold keeps no clock of the job's, so its end starts a retry early."""
    patch.setattr(Daemon, "_kept_retry", lambda self, tx, job_id, state, reason, clock, now: None)


def pause_only_at_the_pass(patch):
    """Fix round 2, F1: the hold's commit writes its row and takes no job (as fix round 1 had it):
    a job's clocks stop only when an admission pass takes it."""
    patch.setattr(Daemon, "_take_task_jobs", lambda self, tx, hold, now: None)


def held_job_is_demand(patch):
    """P2-3: `_reset_demand` counts a job a hold keeps, until admission looks at it again.

    Round 1's definition, restored in fix round 3 (review r3 of #260, R3-3): the check alone is
    removed, and the commit's take stays. Its scenario reaches a held job on a capacity wait with
    that take intact (`tests/fake/test_task_hold_reset_demand.py`)."""
    patch.setattr(Daemon, "_demand_held", lambda self, job, holds: False)


def pending_ignores_holds(patch):
    """P2-3: the reset action's pending transaction does not read the hold again.

    Round 1's definition, restored in fix round 3 (R3-3), as for `held_job_is_demand`."""
    patch.setattr(task_holds, "job_held", lambda conn, job_id, now: None)


def keep_any_capacity_clock(patch):
    """Fix round 2, F2: a hold keeps any capacity clock whose job's latest attempt ended
    `transient` (as fix round 1 had it), a recheck after the backoff included."""
    def kept(self, tx, job_id, state, reason, clock, now):
        if not (state == "waiting" and reason == "capacity" and clock):
            return None
        transient = tx.execute("SELECT 1 FROM attempts a WHERE a.job_id=? AND a.outcome_class='transient' "
                               "AND a.seq=(SELECT MAX(seq) FROM attempts WHERE job_id=a.job_id)", (job_id,)).fetchone()
        return {"wait_reason": reason, "next_check_at": clock} if transient else None
    patch.setattr(Daemon, "_kept_retry", kept)


def admission_writes_over_the_hold(patch):
    """Fix round 3, R3-1: an admission write (an episode's opening, a pin failure, a wait) reads
    no hold in its own transaction, as fix round 2 had it: a look under way when the hold commits
    writes over `task-held`."""
    patch.setattr(Daemon, "_task_held_at_write", lambda self, tx, job, holds, **kwargs: None)


def retry_deadline_from_the_attempt_end(patch):
    """Fix round 3, R3-2: a take judges a retry's deadline as the attempt's end plus 60 s (fix
    round 2's predicate) and keeps the job's clock, not the deadline the finalizer wrote."""
    def kept(self, tx, job_id, state, reason, clock, now):
        if not (state == "waiting" and reason == "capacity" and clock):
            return None
        row = tx.execute(daemon_module.RETRY_END_SQL, (job_id,)).fetchone()
        if row is None or not isinstance(row[0], str):
            return None
        if not daemon_module._earlier(now, daemon_module._later(row[0], TRANSIENT_RETRY_DELAY_S)):
            return None
        return {"wait_reason": reason, "next_check_at": clock}
    patch.setattr(Daemon, "_kept_retry", kept)


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
    "pause-only-at-the-pass": pause_only_at_the_pass,
    "keep-any-capacity-clock": keep_any_capacity_clock,
    "admission-writes-over-the-hold": admission_writes_over_the_hold,
    "retry-deadline-from-the-attempt-end": retry_deadline_from_the_attempt_end,
}


def legacy_fallback_from_job_clock(patch):
    replace_method(patch, "_kept_retry", "deadline = _later(accepted[0], TRANSIENT_RETRY_DELAY_S)",
                   "deadline = clock")


def replacement_judged_at_request_clock(patch):
    _request_clock_mutant(patch, "if not task_holds.in_force_now(replaced, now):",
                         "if not task_holds.in_force_now(replaced, asked):")


def until_validated_at_request_clock(patch):
    _request_clock_mutant(patch, "until = task_holds.until_text(a.until, now)",
                         "until = task_holds.until_text(a.until, asked)")


def _request_clock_mutant(patch, old, new):
    declaration = '    data: dict[str, Any] = {"task": task, "note": note, "replaced": None}\n'
    source = textwrap.dedent(inspect.getsource(Daemon._hold_task))
    assert declaration in source and old in source
    source = source.replace(declaration, declaration + "    asked = utcnow()\n").replace(old, new)
    namespace = {}
    exec(source, vars(daemon_module), namespace)
    patch.setattr(Daemon, "_hold_task", namespace["_hold_task"])


def ending_uses_pass_clock(patch, *, explicit):
    """R5-1: remove just one of the two row-clock ending protections."""
    source = textwrap.dedent(inspect.getsource(Daemon._task_hold_ended))
    if explicit:
        old, new = "if ended_at is not None and seen != ended_at:", "if False:"
    else:
        old, new = "end = seen if seen and not _earlier(now, seen) else now", "end = clock if clock and not _earlier(now, clock) else now"
    assert old in source
    namespace = {}
    exec(source.replace(old, new), vars(daemon_module), namespace)
    patch.setattr(Daemon, "_task_hold_ended", namespace["_task_hold_ended"])
