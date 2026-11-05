"""Unread notices and retention (C-8.4, D-NOTICE-KEEP1).

An unread notice (`pending` or `offered`) addressed to a session keeps its job
for `retention.notice_keep_days` after the notice was written, and no longer:
a session that never runs again never reads it, and on 2026-11-05 such
notices alone kept 88 trees (25.47 GB) on disk. 0 keeps the job for ever.

Timestamps: notices store `created_at` as `YYYY-MM-DDTHH:MM:SSZ`, while turn
retention spells its cutoff with milliseconds and `+00:00`. Ages here are
compared as parsed datetimes, never as strings across the two spellings; a
`created_at` that does not parse keeps its job (the old rule).
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

#: The unread notices that can pin, in SQL: some session can still read them (IR-17).
UNREAD_TO_A_SESSION = "state IN ('pending','offered') AND session_id IS NOT NULL AND session_id <> ''"

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
