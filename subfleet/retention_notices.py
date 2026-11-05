"""Unread notices and retention (C-8.4, C-23.26, D-NOTICE-KEEP1).

An unread notice (`pending` or `offered`) addressed to a session keeps its job
for `retention.notice_keep_days` after the notice was written, and no longer:
a session that never runs again never reads it, and on 2026-11-05 such
notices alone kept 88 trees (25.47 GB) on disk. 0 keeps the job for ever.

When retention retires a job that still has an unread notice, the job's
rows go, its notices among them (`retention_archive.Retirement.commit`). So
the same transaction writes the notice's session a service notice, the
*outcome*: the notice's first line (C-15.1: job id, state, rc, deliverable and
`-o` paths), when retention retired the job with the notice unread, and where
the job's archive is. A `retention.notice_retired` event names the outcome by
id, session and creation time, as a pin's `job.pin_noticed` event names its
notice (C-11.8), so `notice.pending` names the job and a session Subfleet
launched surfaces it too (C-15.2, C-26.13).

The outcome pins nothing: it is a `service_notices` row, a table no pin reads,
and the job it names has no rows left to pin. Once delivered it ends like any
service notice (C-23.26: deleted 14 days after it was written); one never
delivered is withdrawn `notice_keep_days` after it was written
(`expire_outcomes`, with a `retention.notice_outcome_expired` event), so a
session that never returns holds at most one window's outcomes.

Timestamps: notices store `created_at` as `YYYY-MM-DDTHH:MM:SSZ`, while turn
retention spells its cutoff with milliseconds and `+00:00`. Ages here are
compared as parsed datetimes, never as strings across the two spellings; a
`created_at` that does not parse keeps its job (the old rule).
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any, Sequence

#: The event that names an outcome notice (its id, session and creation time) and the job.
RETIRED_EVENT = "retention.notice_retired"
#: The event that records the outcome notices withdrawn unread after `notice_keep_days`.
EXPIRED_EVENT = "retention.notice_outcome_expired"
#: The unread notices that can pin, in SQL: some session can still read them (IR-17).
UNREAD_TO_A_SESSION = "state IN ('pending','offered') AND session_id IS NOT NULL AND session_id <> ''"


def stamp(moment: datetime) -> str:
    """A UTC time in the notices' own spelling, `YYYY-MM-DDTHH:MM:SSZ`."""
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def written_at(value: Any) -> datetime | None:
    """A stored UTC time as an aware datetime: `...Z`, `...+00:00`, fractional
    seconds and a naive spelling (read as UTC) alike; None when it does not parse."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def expires(keep_s: Any) -> bool:
    """Whether a keep time lets anything expire: a finite number of seconds
    above 0. 0 means for ever."""
    return (isinstance(keep_s, (int, float)) and not isinstance(keep_s, bool) and math.isfinite(keep_s)
            and keep_s > 0)


def cutoff(now: datetime, keep_s: float) -> datetime | None:
    """The oldest a notice may have been written and still keep its job: `now`
    less `keep_s`. None when nothing expires (`expires`), or when the time
    would fall before year 1: a keep that long keeps everything."""
    if not expires(keep_s):
        return None
    try:
        return now - timedelta(seconds=keep_s)
    except OverflowError:
        return None


def fresh(created_at: Any, oldest: datetime | None) -> bool:
    """Whether a notice written at `created_at` still keeps its job: nothing
    expires (`oldest` None), its time does not parse (kept, as before), or it
    was written at or after `oldest`."""
    if oldest is None:
        return True
    written = written_at(created_at)
    return written is None or written >= oldest


def keep_days_text(keep_s: float) -> str:
    days = keep_s / 86400
    return f"{days:g} day{'' if days == 1 else 's'}"


def outcome_text(first_lines: Sequence[str], queued: Sequence[str], retired_at: str, keep_s: float,
                 archive: str | None, name: str | None) -> str:
    """The outcome notice: each unread notice's first line as written (a job
    has one notice, normally), then what retention did and where the job's
    files and rows are now."""
    lines = [line for line in dict.fromkeys(first_lines) if line] or ["(the notice had no text)"]
    when = ", ".join(dict.fromkeys(item for item in queued if item)) or "at an unknown time"
    text = f"retention retired this job at {retired_at} with its notice unread (queued {when})"
    if expires(keep_s):
        text += f": an unread notice keeps its job retention.notice_keep_days ({keep_days_text(keep_s)})"
    text += "."
    if archive:
        text += (f" Its files and rows are in {archive}; `subfleet retention restore {name} --to DIR` "
                 "puts its job directory, deliverable included, in DIR/job.")
    return "\n".join([*lines, text])


def write_outcomes(conn: Any, job_id: str, *, keep_s: float, archive: str | None, name: str | None,
                   now: datetime | None = None) -> list[dict[str, Any]]:
    """Inside the transaction that deletes the job's rows, before it does: one
    outcome notice for each session with an unread notice for the job, and the
    event that names it. Returns one record per outcome."""
    rows = conn.execute(f"SELECT notice_id, session_id, text, created_at FROM notices WHERE job_id=? AND "
                        f"{UNREAD_TO_A_SESSION} ORDER BY notice_id", (job_id,)).fetchall()
    by_session: dict[str, list[Any]] = {}
    for row in rows:
        by_session.setdefault(row["session_id"], []).append(row)
    written = stamp(now or datetime.now(UTC))
    records = []
    for session, notices in by_session.items():
        text = outcome_text([str(row["text"] or "").split("\n", 1)[0].strip() for row in notices],
                            [str(row["created_at"] or "") for row in notices], written, keep_s, archive, name)
        cursor = conn.execute("INSERT INTO service_notices(session_id,text,state,created_at) "
                              "VALUES(?,?,'pending',?)", (session, text, written))
        record = {"service_notice_id": cursor.lastrowid, "session_id": session, "created_at": written,
                  "notice_ids": [row["notice_id"] for row in notices],
                  "notices_created_at": [row["created_at"] for row in notices],
                  "notice_keep_s": keep_s if expires(keep_s) or keep_s == 0 else None, "archive": archive}
        conn.execute("INSERT INTO events(ts,kind,job_id,attempt_id,lane_id,data_json) VALUES (?,?,?,?,?,?)",
                     (written, RETIRED_EVENT, job_id, None, None,
                      json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)))
        records.append(record)
    return records


def expire_outcomes(store: Any, keep_s: float, *, now: datetime | None = None) -> list[int]:
    """Withdraw the outcome notices no one was shown (`pending` or `offered`)
    within `keep_s` of their writing: their session has not run since. Each is
    matched by id, session and creation time, as its event names it, so a ping
    or an alert that reused an outcome's id is left alone. One event records
    what went. Returns the service notice ids withdrawn; none while `keep_s`
    keeps for ever."""
    from .store import pin_notice_jobs
    oldest = cutoff(now or datetime.now(UTC), keep_s)
    if oldest is None:
        return []
    record: dict[str, Any] = {"service_notice_ids": [], "jobs": [], "notice_keep_s": float(keep_s),
                              "why": "unread for retention.notice_keep_days"}
    with store.transaction(EXPIRED_EVENT, data=record) as tx:
        old = [dict(row) for row in tx.execute(
            "SELECT notice_id, session_id, created_at FROM service_notices WHERE state IN ('pending','offered')")
            if (written := written_at(row["created_at"])) is not None and written < oldest]
        named = pin_notice_jobs(lambda sql, params: tx.execute(sql, params).fetchall(), old,
                                kinds=(RETIRED_EVENT,)) if old else {}
        for row in old:
            if row["notice_id"] in named and tx.execute(
                    "DELETE FROM service_notices WHERE notice_id=? AND session_id=? AND created_at=? "
                    "AND state IN ('pending','offered')",
                    (row["notice_id"], row["session_id"], row["created_at"])).rowcount:
                record["service_notice_ids"].append(row["notice_id"])
                record["jobs"].append(named[row["notice_id"]])
    return record["service_notice_ids"]
