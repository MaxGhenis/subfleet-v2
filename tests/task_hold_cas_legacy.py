"""Frozen a8624a4899a2 task handlers for unconditional CAS regression tests.

Copied verbatim (except class indentation) from that revision's Daemon. Keep
these independent of subsequent edits to the production handlers. Only clock
lookup delegates to the daemon module, so the virtual clock reaches both.
"""

from __future__ import annotations

from typing import Any

from subfleet import daemon as daemon_module, protocol, refusals, task_holds
from subfleet.contracts import Exit
from subfleet.daemon import _earlier, _event_json, _seconds_between


def utcnow():
    return daemon_module.utcnow()


def _hold_task(self, a: protocol.TasksArgs) -> dict:
    """C-11.10: hold task `a.task` until `a.until`, with the operator's note.

    Every argument is checked before anything is written; a refusal writes
    nothing. The hold is one `leases` row (`task_holds`); a second hold on the
    task replaces the first, in one transaction with its `task.held` event, and
    the jobs the first kept waiting wait for the new end. A replacement of a hold
    still in force keeps its `since` (`acquired_at`): the task has been held since
    then without a break, and a held job's paused clocks count from there (T6).
    The same transaction takes every job of the task that waits or is queued now
    (`_take_task_jobs`), so a hold pauses their clocks from its commit. Running
    attempts are not read, signalled or touched: a new attempt is what a hold stops."""
    if not self._known_task(a.task):
        raise self._unknown_task(a.task, "hold")
    try:
        note = task_holds.note_text(a.note)
    except ValueError:
        raise protocol.ProtocolError(refusals.HANDLER_MESSAGES["task-hold-note"][0],
                                     reason="task-hold-note") from None
    task, key = a.task, task_holds.lease_key(a.task)
    data: dict[str, Any] = {"task": task, "note": note, "replaced": None}
    with self.store.transaction("task.held", data=data) as tx:
        now = utcnow()
        try:
            until = task_holds.until_text(a.until, now)
        except ValueError:
            raise protocol.ProtocolError(refusals.HANDLER_MESSAGES["task-hold-until"][0],
                                         reason="task-hold-until") from None
        data["until"] = until
        row = tx.execute(task_holds.ROW_SQL, (key,)).fetchone()
        replaced = task_holds.hold_of(dict(row)) if row else None
        if not task_holds.in_force_now(replaced, now):
            replaced = None
        if replaced is not None:
            data["replaced"] = {"until": replaced["until"], "note": replaced["note"]}
        since = replaced["since"] if replaced is not None and replaced.get("since") else now
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at,expires_at) VALUES(?,?,?,?) "
                   "ON CONFLICT(lease_key) DO UPDATE SET holder=excluded.holder, "
                   "acquired_at=excluded.acquired_at, expires_at=excluded.expires_at",
                   (key, task_holds.HOLDER + note, since, until))
        if replaced is not None:
            # The jobs a hold in force keeps wait for this one's end: a second hold replaces the first.
            tx.execute("UPDATE jobs SET next_check_at=? WHERE state='waiting' AND wait_reason='task-held' "
                       "AND task=? AND kind!='turn'", (until, task))
        # T6 (review r2 of #260, F1): the jobs that wait now are taken in this transaction,
        # so their clocks stop at the commit, whether or not a pass looks before the hold ends.
        self._take_task_jobs(tx, {"task": task, "until": until, "since": since}, now)
    self._task_holds_changed()
    return {"held": task, "task_hold": {"task": task, "until": until, "note": note, "since": since},
            "replaced": replaced, "task_holds": task_holds.listing(self._task_holds_now())}

def _release_task(self, a: protocol.TasksArgs) -> dict:
    """C-11.10: end the hold on `a.task` at once, and wake the jobs it kept waiting.

    One transaction deletes the row and gives each job it kept waiting its clocks
    back (`_task_hold_restore`: queued again, unless a retry's backoff or a paused
    pin grace says when), each with its `job.task_hold_ended` event, beside the
    `task.released` event; the next pass looks at them, and one in flight reads
    the holds again at its next job. A task the policy no longer routes can
    still be released while its row is there. A release with no row changes
    nothing and records nothing. A release of a hold already past its end
    (`was_held` false) still deletes the row and records `task.released`, and
    gives any job no pass has woken since the end its clocks back, from that end."""
    task = a.task
    if not (isinstance(task, str) and task and (
            self._known_task(task) or self.store.one(task_holds.ROW_SQL, (task_holds.lease_key(task),)))):
        raise self._unknown_task(task, "release")
    data: dict[str, Any] = {"task": task}
    with self.store.transaction("task.released", data=data) as tx:
        now = utcnow()
        row = tx.execute(task_holds.ROW_SQL, (task_holds.lease_key(task),)).fetchone()
        ended = task_holds.hold_of(dict(row)) if row else None
        if ended is not None:
            data.update(until=ended["until"], note=ended["note"])
        tx.execute("DELETE FROM leases WHERE lease_key=?", (task_holds.lease_key(task),))
        held = tx.execute("SELECT job_id, next_check_at FROM jobs WHERE state='waiting' "
                          "AND wait_reason='task-held' AND task=? AND kind!='turn' "
                          "AND cancel_requested_at IS NULL ORDER BY created_at, rowid", (task,)).fetchall()
        woken = 0
        for job_id, clock in held:
            end, unheld = now, 0.0
            if clock and _earlier(clock, now):
                # Held for a hold that had already ended by its clock, unseen by any pass:
                # it ended there. Had the hold released here begun after that end, the time
                # between counts as time with no hold, and the job was held again until
                # that hold's own end: now, or its `until` if that passed unseen too.
                end = clock
                if ended is not None and ended.get("since") and _earlier(clock, ended["since"]):
                    end = now if task_holds.in_force_now(ended, now) else ended["until"]
                    unheld = _seconds_between(clock, ended["since"])
            record: dict[str, Any] = {"by": "release"}
            if self._task_hold_restore(tx, job_id, end, now, record=record, unheld_s=unheld) is not None:
                woken += 1
                tx.execute("INSERT INTO events(ts,kind,job_id,attempt_id,lane_id,data_json) "
                           "VALUES (?,?,?,?,?,?)", (now, "job.task_hold_ended", job_id, None, None,
                                                    _event_json(record)))
        data["woken"] = woken
    self._task_holds_changed()
    return {"released": task, "was_held": task_holds.in_force_now(ended, now), "woken": woken,
            "task_holds": task_holds.listing(self._task_holds_now())}
