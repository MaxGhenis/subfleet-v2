"""C-11.10 (D-TASKHOLD1, D-RO-ADMIT1): task and sandbox holds at admission.

A hold on task T keeps admission from starting any new attempt of a detached
job whose task is T, from `subfleet tasks hold T --until <iso> --note <text>`
until that instant or `subfleet tasks release T`. Lanes stay open: jobs of every
other task, and conversation turns, are placed as if no hold existed.

A sandbox hold uses `sandbox-hold:workspace-write` with the same row rules.
It covers detached task-bearing jobs whose stored sandbox is workspace-write
(or empty in legacy state). Task and sandbox holds compose through the later
active end. Turns and taskless jobs remain unheld.

A hold is one row of `leases`, so it needs no schema change (no table or
column of its own; release/217's migrations 8 and 9 add only job and attempt
columns): key `task-hold:<task>`, holder `operator: <note>`, `acquired_at`
when it was set (a replacement of a hold in force keeps it) and `expires_at`
its end. No attempt or job holds such a key, and every lease reaper deletes by
holder (an attempt's, a job's, a probe's, retention's) or by a key of its own,
so none removes it; the report `docs/reports/2026-10-24-task-hold.md` lists
each reader. A hold past its end is not in force, by the clock: nothing reaps
it. A second hold on the task replaces the row; a release deletes it.

This module is pure: it validates what an operator gives, spells the row, and
reads which holds are in force from rows the caller read.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from . import ids

#: The key prefix of a task hold's lease row, and the bounds of a range read
#: over it (`;` is the character after `:`), which the primary key's index serves.
KEY = "task-hold:"
KEY_END = "task-hold;"
#: A separate, disjoint range: a sandbox key can never be a task-hold key.
SANDBOX_KEY = "sandbox-hold:"
SANDBOX_KEY_END = "sandbox-hold;"
SANDBOX = "workspace-write"
SANDBOX_ROWS_PARAMS = (SANDBOX_KEY, SANDBOX_KEY_END)
HoldKey = str | tuple[str, str]
#: Its holder: the operator's own words, after this prefix. No attempt id, job
#: id, probe holder (`probe:`), retention holder (`retention:`) or resume holder
#: (`resume:`) starts so, so no reaper's holder match can name it.
HOLDER = "operator: "
#: The longest note a hold keeps, in characters.
NOTE_MAX = 160
#: The rows a reader passes to `in_force`, in key order.
ROWS_SQL = ("SELECT lease_key, holder, acquired_at, expires_at FROM leases "
            "WHERE lease_key >= ? AND lease_key < ? ORDER BY lease_key")
ROWS_PARAMS = (KEY, KEY_END)
#: One task's row.
ROW_SQL = "SELECT lease_key, holder, acquired_at, expires_at FROM leases WHERE lease_key=?"


def lease_key(task: str) -> str:
    """`task-hold:<task>`, the one row a hold on `task` keeps."""
    return KEY + task


#: What no note may hold anywhere, its edges included: the C0 controls (newline and
#: tab among them), DEL, the C1 controls, and the Unicode line and paragraph separators.
CONTROLS = frozenset([*map(chr, range(0x20)), *map(chr, range(0x7f, 0xa0)), "\u2028", "\u2029"])


def note_text(value: Any) -> str:
    """The note as a hold keeps it, or ValueError. The CLI and the daemon both ask this.

    Required: one line of printable characters, at most `NOTE_MAX` of them. A
    control character (`CONTROLS`) is refused wherever it is, before anything is
    trimmed, so `"\\nbooking\\n"` is refused, not kept as `booking`; then only
    surrounding ASCII spaces are dropped. A note shaped like a credential is
    refused too: the store never holds a secret (C-10.1), and `status` shows the note.
    """
    if not isinstance(value, str):
        raise ValueError("the note is required")
    if any(char in CONTROLS for char in value):
        raise ValueError("the note must be one line of printable characters")
    note = value.strip(" ")
    if not note:
        raise ValueError("the note is required")
    if len(note) > NOTE_MAX:
        raise ValueError(f"the note is longer than {NOTE_MAX} characters")
    if not note.isprintable():
        raise ValueError("the note must be one line of printable characters")
    if ids.holds_credential(note):
        raise ValueError("the note holds what reads as a credential")
    return note


def instant(value: str) -> datetime:
    """An ISO 8601 instant in UTC; one with no zone is read as UTC, as `lanes hold` reads it."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def stamp(value: datetime) -> str:
    """The store's spelling of an instant: whole seconds, `Z`."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def until_text(value: Any, now: str | datetime) -> str:
    """The hold's end as the row keeps it, or ValueError.

    An ISO 8601 instant later than `now`, to the whole second (a fraction is
    dropped, as `lanes hold` drops it). An end already reached would hold
    nothing, so it is refused rather than written.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("--until is required")
    try:
        end = instant(value.strip())
    except (ValueError, OverflowError):
        raise ValueError("--until must be an ISO 8601 instant") from None
    text = stamp(end)
    current = instant(now) if isinstance(now, str) else now.astimezone(timezone.utc)
    if instant(text) <= current:
        raise ValueError("--until must be later than now")
    return text


def hold_of(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """A task or supported sandbox lease as a hold, or None for any other row."""
    key, holder, until = row.get("lease_key"), row.get("holder"), row.get("expires_at")
    if isinstance(key, str) and key.startswith(KEY) and len(key) > len(KEY):
        target = {"task": key[len(KEY):]}
    elif key == SANDBOX_KEY + SANDBOX:
        target = {"sandbox": SANDBOX}
    else:
        return None
    if not isinstance(until, str):
        return None
    note = holder[len(HOLDER):] if isinstance(holder, str) and holder.startswith(HOLDER) else str(holder or "")
    return {**target, "until": until, "note": note, "since": row.get("acquired_at")}


def target(hold: Mapping[str, Any]) -> dict[str, str]:
    """The discriminator carried by replies, waits and the existing hold events."""
    return {"sandbox": hold["sandbox"]} if "sandbox" in hold else {"task": hold["task"]}


def index_key(hold: Mapping[str, Any]) -> HoldKey:
    # A task may have any policy name, including 'sandbox-hold:workspace-write'.
    # A typed key keeps that task separate from the sandbox index entry.
    return ("sandbox", hold["sandbox"]) if "sandbox" in hold else hold["task"]


def in_force_now(hold: Mapping[str, Any] | None, now: str | datetime) -> bool:
    """Whether `hold` is in force at `now`: from its row until its end, by the clock."""
    if not hold:
        return False
    current = instant(now) if isinstance(now, str) else now.astimezone(timezone.utc)
    try:
        return instant(hold["until"]) > current
    except (ValueError, OverflowError, TypeError):
        # An end that cannot be read is not an end that has passed: the hold
        # stays in force until it is released or replaced (never silently open).
        return True


def in_force(rows: Iterable[Mapping[str, Any]], now: str | datetime) -> dict[HoldKey, dict[str, Any]]:
    """Task or sandbox index key -> hold, for each row in force at `now`."""
    holds = {}
    for row in rows:
        hold = hold_of(row)
        if hold is not None and in_force_now(hold, now):
            holds[index_key(hold)] = hold
    return holds


def listing(holds: Mapping[HoldKey, Mapping[str, Any]],
            waiting: Mapping[HoldKey, int] | None = None) -> list[dict[str, Any]]:
    """The task and sandbox holds in force as `status --json` lists them (`task_holds`).

    With `waiting` (task -> the jobs a hold keeps waiting, `WAITING_SQL` over the
    whole store, never a page of listed rows), each hold carries its count too."""
    return [{**target(hold), "until": hold["until"], "note": hold["note"], "since": hold.get("since"),
             **({"waiting": int(waiting.get(key, 0))} if waiting is not None else {})}
            for key, hold in sorted(holds.items(), key=lambda pair: (isinstance(pair[0], tuple), pair[0]))]


#: Task -> how many jobs wait `task-held`, counted over every row (C-17.4), so a
#: listing capped for display (offline status lists 50 jobs) never caps the count.
WAITING_SQL = ("SELECT task, count(*) AS n, NULL AS sandbox FROM jobs WHERE state='waiting' AND wait_reason='task-held' "
               "AND kind!='turn' GROUP BY task "
               "UNION ALL SELECT NULL AS task, count(*) AS n, 'workspace-write' AS sandbox FROM jobs "
               "WHERE state='waiting' AND wait_reason='task-held' AND kind!='turn' AND COALESCE(task,'')!='' "
               "AND COALESCE(NULLIF(sandbox,''),'workspace-write')='workspace-write'")


def waiting_counts(rows: Iterable[Mapping[str, Any]]) -> dict[HoldKey, int]:
    """`WAITING_SQL`'s rows as task -> count."""
    counts = {}
    for row in map(dict, rows):
        if row.get("sandbox"):
            counts[("sandbox", row["sandbox"])] = int(row["n"])
        elif row.get("task"):
            counts[row["task"]] = int(row["n"])
    return counts


def release_command(task: str = "", *, sandbox: str | None = None) -> str:
    """The command that ends `task`'s hold, as a shell reads it: `--` and then the task as
    one argument, quoted when it must be, so `shlex.split` gives the task back whole."""
    return shlex.join(["subfleet", "tasks", "release", "--sandbox", sandbox] if sandbox is not None else
                      ["subfleet", "tasks", "release", "--", str(task)])


def hold_for(job: Mapping[str, Any], holds: Mapping[HoldKey, dict[str, Any]]) -> dict[str, Any] | None:
    """The covering hold with the later end; turns and taskless jobs stay unheld.

    Admission uses the resolved sandbox on the job row, never today's policy.
    A legacy empty sandbox is conservatively workspace-write.
    """
    if job["kind"] == "turn" or not job["task"]:
        return None
    candidates = [hold for hold in (holds.get(job["task"]),
                  holds.get(("sandbox", job.get("sandbox") or SANDBOX))) if hold is not None]
    def end(hold):
        try:
            return instant(hold["until"])
        except (ValueError, TypeError, OverflowError):
            return datetime.max.replace(tzinfo=timezone.utc)
    if not candidates:
        return None
    chosen = max(candidates, key=end)
    # Both active intervals cover now, hence their union has no gap. Its
    # earliest start matters to a stale pin-episode writer, even when the
    # later-ending row began after that episode's snapshot.
    starts = [hold["since"] for hold in candidates if hold.get("since")]
    return {**chosen, "since": min(starts, key=instant)} if len(candidates) > 1 and starts else chosen


def job_held(conn: Any, job_id: str, now: str | datetime) -> dict[str, Any] | None:
    """C-11.10: the covering task or sandbox hold in force at `now`, read on `conn` (the caller's
    own transaction, so it judges the rows it commits beside), or None. A conversation
    turn and a job with no task are never held."""
    row = conn.execute("SELECT kind, task, sandbox FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None or row[0] == "turn" or not row[1]:
        return None
    rows = conn.execute("SELECT lease_key,holder,acquired_at,expires_at FROM leases WHERE lease_key IN (?,?)",
                        (lease_key(row[1]), SANDBOX_KEY + (row[2] or SANDBOX))).fetchall()
    return hold_for({"kind": row[0], "task": row[1], "sandbox": row[2]},
                    in_force([dict(zip(("lease_key", "holder", "acquired_at", "expires_at"), lease))
                              for lease in rows], now))


def wait_hold(hold: Mapping[str, Any]) -> dict[str, Any]:
    """What admission records for a job a hold keeps waiting (C-6.11's `task-held`)."""
    return {"reason": "task-held", **target(hold), "until": hold["until"], "note": hold["note"],
            "next_check_at": hold["until"]}
