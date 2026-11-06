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
id, session, creation time and a full SHA-256 digest of its text, as a pin's `job.pin_noticed` event names its
notice (C-11.8), so `notice.pending` names the job and a session Subfleet
launched surfaces it too (C-15.2, C-26.13).

The outcome pins nothing: it is a `service_notices` row, a table no pin reads,
and the job it names has no rows left to pin. Once delivered it ends like any
service notice (C-23.26: deleted 14 days after it was written); one never
delivered is withdrawn `notice_keep_days` after it was written
(`expire_outcomes`, with a `retention.notice_outcome_expired` event), so a
session that never returns holds at most one window's outcomes.

The queue's expiry does not erase the outcome: `archived_outcomes` reconstructs
it from the job's existing archived notices and retirement event, on request
through `subfleet retention outcomes --session ID`. It writes no store records, so a
session returning after any length of time can still look up what happened
without holding another notice or recreating one. A private locked snapshot
avoids state-root files on a closed store; a busy-store fallback may create
SQLite WAL/SHM coordination files, never a daemon lock or store records.

Timestamps: notices store `created_at` as `YYYY-MM-DDTHH:MM:SSZ`, while turn
retention spells its cutoff with milliseconds and `+00:00`. Ages here are
compared as parsed datetimes, never as strings across the two spellings; a
`created_at` that does not parse keeps its job (the old rule).
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
    was written after `oldest`."""
    if oldest is None:
        return True
    written = written_at(created_at)
    return written is None or written > oldest


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
        text += f" after retention.notice_keep_days ({keep_days_text(keep_s)})"
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
                  "outcome_sha256": hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
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
    matched by id, session, creation time and its event's SHA-256 of the text, so a ping
    or an alert that reused an outcome's id is left alone. One event records
    what went. Returns the service notice ids withdrawn; none while `keep_s`
    keeps for ever.

    The candidates are read before any write: this runs once per retention
    call, every few seconds while a backlog is worked off, and most calls find
    nothing due. The delete names each row whole (id, session, creation time, text,
    still unread), so a row a hook surfaced or that took a reused id between
    the read and the delete is left as it is; events are only ever added, so a
    row named once stays named. Legacy events without a digest cannot prove
    ownership and are not expired here; delivered rows retain normal cleanup."""
    from .store import pin_notice_jobs
    oldest = cutoff(now or datetime.now(UTC), keep_s)
    if oldest is None:
        return []
    old = [row for row in store.query(
        "SELECT notice_id, session_id, created_at, text FROM service_notices WHERE state IN ('pending','offered')")
        if (written := written_at(row["created_at"])) is not None and written < oldest]
    named = pin_notice_jobs(store.query, old, kinds=(RETIRED_EVENT,), match_outcome_text=True) if old else {}
    due = [row for row in old if row["notice_id"] in named]
    if not due:
        return []
    record: dict[str, Any] = {"service_notice_ids": [], "jobs": [], "notice_keep_s": float(keep_s),
                              "why": "unread for retention.notice_keep_days"}
    with store.transaction(EXPIRED_EVENT, data=record) as tx:
        for row in due:
            if tx.execute("DELETE FROM service_notices WHERE notice_id=? AND session_id=? AND created_at=? AND text=? "
                          "AND state IN ('pending','offered')",
                          (row["notice_id"], row["session_id"], row["created_at"], row["text"])).rowcount:
                record["service_notice_ids"].append(row["notice_id"])
                record["jobs"].append(named[row["notice_id"]])
    return record["service_notice_ids"]


def archived_outcomes(root: Path, session_id: str) -> dict[str, Any]:
    """Read a session's retired unread outcomes, including after queue expiry.

    The existing archive owns the original notice text, and the retirement
    event owns the time and keep period. Neither expires with its service
    notice. Only the given state root is read; an archive path recorded before
    that root moved is resolved by its name under this root's archive folder.
    No notice is marked, recreated or pinned, and no new records are written.
    A closed checkpointed database is read from a locked private copy outside
    the state root, creating nothing there. When the database is busy, has a
    WAL, cannot be write-locked, or TMPDIR is inside the state root, SQLite's
    read-only fallback may create/update only
    state.sqlite3-wal and state.sqlite3-shm for coordination. It never takes
    daemon.lock or deletes state-root files. `read_only` means store records;
    `state_root_files` reports which filesystem promise applies to this read.
    An unavailable archive is reported explicitly rather than losing its outcome.
    """
    from . import retention_fs as rfs
    from .retention_snapshot import lookup_store

    root = Path(root).resolve()
    database = root / "state.sqlite3"
    if database.resolve().parent != root:
        raise ValueError("state.sqlite3 is outside the state root")
    report: dict[str, Any] = {"session_id": session_id, "read_only": True, "read_only_scope": "records",
                              "outcomes": [], "errors": []}
    with lookup_store(database) as (store, file_policy):
        report["state_root_files"] = file_policy
        events = store.query("SELECT event_id, ts, job_id, data_json FROM events WHERE kind=? ORDER BY event_id",
                             (RETIRED_EVENT,))
    for event in events:
        try:
            record = json.loads(event["data_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("session_id") != session_id:
            continue
        try:
            stored_path = record.get("archive")
            if not isinstance(stored_path, str) or not stored_path:
                raise ValueError("retirement event names no archive")
            name = Path(stored_path).name
            if name in ("", ".", ".."):
                raise ValueError("retirement event names no archive")
            archive = root / "archive" / name
            if archive.resolve().parent != root / "archive":
                raise ValueError("archive is outside the state root's archive folder")
            saved = json.loads(rfs.read_regular(archive / "rows.json"))
            if not isinstance(saved, dict) or saved.get("job_id") != event["job_id"]:
                raise ValueError("archived rows name a different job")
            archived_rows = saved.get("rows")
            archived_notices = archived_rows.get("notices") if isinstance(archived_rows, dict) else None
            if not isinstance(archived_notices, list) or any(not isinstance(row, dict) for row in archived_notices):
                raise ValueError("archived notices are malformed")
            notice_ids = record.get("notice_ids", [])
            notices = [row for row in archived_notices
                       if row.get("session_id") == session_id and row.get("notice_id") in notice_ids
                       and row.get("state") in ("pending", "offered")]
            if not notices:
                raise ValueError("archived rows contain none of the event's unread notices")
            first_lines = list(dict.fromkeys(str(row.get("text") or "").split("\n", 1)[0].strip()
                                            for row in notices))
            keep_s = record.get("notice_keep_s")
            retired_at = record.get("created_at") or event["ts"]
            report["outcomes"].append({
                "job_id": event["job_id"], "archive": str(archive), "retired_at": retired_at,
                "notice_keep_days": keep_s / 86400 if expires(keep_s) or keep_s == 0 else None,
                "first_lines": first_lines,
                "text": outcome_text(first_lines, [str(row.get("created_at") or "") for row in notices],
                                     retired_at, keep_s, str(archive), name),
            })
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report["errors"].append({"job_id": event["job_id"], "error": str(exc)})
    return report
