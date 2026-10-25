"""C-11.10 (D-TASKHOLD1): an operator's hold on one task, at admission.

A hold on task T keeps admission from starting any new attempt of a detached
job whose task is T, from `subfleet tasks hold T --until <iso> --note <text>`
until that instant or `subfleet tasks release T`. Lanes stay open: jobs of every
other task, and conversation turns, are placed as if no hold existed.

A hold is one row of `leases` (schema 7, no new table): key `task-hold:<task>`,
holder `operator: <note>`, `acquired_at` when it was set and `expires_at` its
end. No attempt or job holds such a key, and every lease reaper deletes by
holder (an attempt's, a job's, a probe's, retention's) or by a key of its own,
so none removes it; the report `docs/reports/2026-10-24-task-hold.md` lists
each reader. A hold past its end is not in force, by the clock: nothing reaps
it. A second hold on the task replaces the row; a release deletes it.

This module is pure: it validates what an operator gives, spells the row, and
reads which holds are in force from rows the caller read.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from . import ids

#: The key prefix of a task hold's lease row, and the bounds of a range read
#: over it (`;` is the character after `:`), which the primary key's index serves.
KEY = "task-hold:"
KEY_END = "task-hold;"
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


def note_text(value: Any) -> str:
    """The note as a hold keeps it, or ValueError.

    Required: one line of printable characters, at most `NOTE_MAX` of them, with
    surrounding spaces dropped. A note shaped like a credential is refused too:
    the store never holds a secret (C-10.1), and the note is shown by `status`.
    """
    if not isinstance(value, str):
        raise ValueError("the note is required")
    note = value.strip()
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
    """A lease row as a hold (`task`, `until`, `note`, `since`), or None for any other row."""
    key, holder, until = row.get("lease_key"), row.get("holder"), row.get("expires_at")
    if not (isinstance(key, str) and key.startswith(KEY) and len(key) > len(KEY)):
        return None
    if not isinstance(until, str):
        return None
    note = holder[len(HOLDER):] if isinstance(holder, str) and holder.startswith(HOLDER) else str(holder or "")
    return {"task": key[len(KEY):], "until": until, "note": note, "since": row.get("acquired_at")}


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


def in_force(rows: Iterable[Mapping[str, Any]], now: str | datetime) -> dict[str, dict[str, Any]]:
    """Task -> its hold, for each hold in `rows` in force at `now`."""
    holds = {}
    for row in rows:
        hold = hold_of(row)
        if hold is not None and in_force_now(hold, now):
            holds[hold["task"]] = hold
    return holds


def listing(holds: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The holds in force as `status --json` lists them (`task_holds`): by task, never absent."""
    return [{"task": task, "until": hold["until"], "note": hold["note"], "since": hold.get("since")}
            for task, hold in sorted(holds.items())]


def wait_hold(hold: Mapping[str, Any]) -> dict[str, Any]:
    """What admission records for a job a hold keeps waiting (C-6.11's `task-held`)."""
    return {"reason": "task-held", "task": hold["task"], "until": hold["until"], "note": hold["note"],
            "next_check_at": hold["until"]}
