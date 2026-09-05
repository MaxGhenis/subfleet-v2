"""A read-only snapshot of routing evidence (C-6.4, C-9.1, C-10).

The builder operates on rows and never probes, reads process state, or changes
the store. Desktop identity is read separately so callers can keep filesystem
access outside admission transactions (C-3.3).
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import READING_TTL_S

ACTIVE_ATTEMPT_STATES = frozenset({"reserved", "starting", "running", "finalizing"})


def _row(value: Any) -> dict[str, Any]:
    return asdict(value) if is_dataclass(value) else dict(value)


def _time(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def read_desktop_account(path: str | Path | None = None) -> str | None:
    """C-10.3: reread the current Claude desktop login without caching secrets.

    An unreadable or incomplete login file is unknown, so callers preserve the
    last recorded desktop flags instead of silently removing protection.
    """
    try:
        value = json.loads((Path(path) if path is not None else Path.home() / ".claude.json").read_text())
        account = value.get("oauthAccount") if isinstance(value, dict) else None
        email = account.get("emailAddress") if isinstance(account, dict) else None
        return email.strip().casefold() if isinstance(email, str) and email.strip() else None
    except (OSError, UnicodeError, ValueError):
        return None


def _account_matches(lane: Mapping[str, Any], account: str) -> bool:
    identity = str(lane.get("account_key", "")).casefold()
    account = account.casefold()
    return identity == account or identity == f"claude:{account}"


def latest_readings(readings: Iterable[Any], *, now: str | datetime,
                    reading_ttl_s: int = READING_TTL_S) -> list[dict[str, Any]]:
    """C-9.1: retain the newest evidence for each lane, scope, and window."""
    instant = _time(now)
    latest: dict[tuple[str, str, str], tuple[tuple[datetime, int], dict[str, Any]]] = {}
    for item in readings:
        reading = _row(item)
        observed = _time(reading["observed_at"])
        key = (reading["lane_id"], reading["scope"], reading["window"])
        order = (observed, int(reading.get("reading_id") or 0))
        if key not in latest or order > latest[key][0]:
            age = (instant - observed).total_seconds()
            reading["age_s"] = age
            if reading.get("label") == "provider" and age > reading_ttl_s:
                reading["label"] = "stale-provider"
            latest[key] = (order, reading)
    return [latest[key][1] for key in sorted(latest)]


def fresh_provider(reading: Mapping[str, Any], *, now: str | datetime,
                   reading_ttl_s: int = READING_TTL_S) -> bool:
    """C-6.4: only current numeric provider evidence grants measured slots."""
    if reading.get("label") != "provider":
        return False
    utilization = reading.get("utilization")
    if (not isinstance(utilization, (int, float)) or isinstance(utilization, bool)
            or not math.isfinite(utilization) or not 0 <= utilization <= 1):
        return False
    instant = _time(now)
    age = (instant - _time(reading["observed_at"])).total_seconds()
    return (0 <= age <= reading_ttl_s
            and (not reading.get("resets_at") or _time(reading["resets_at"]) > instant))


def _display_order(lane: Mapping[str, Any], *, now: datetime, reading_ttl_s: int) -> tuple:
    """C-11.3: mirror provider ordering for the account-wide status view."""
    measured = [reading for reading in lane["readings"]
                if fresh_provider(reading, now=now, reading_ttl_s=reading_ttl_s)]
    if lane["provider"] == "codex":
        weekly = [_time(reading["resets_at"]) for reading in measured
                  if reading["window"] == "seven_day" and reading.get("resets_at")]
        reset = min(weekly, default=datetime.max.replace(tzinfo=timezone.utc))
        return (0, not measured, reset, lane["lane_id"])
    headroom = min((1 - reading["utilization"] for reading in measured), default=0)
    return (1, not measured, -headroom, lane["in_flight"], lane["lane_id"])


def build_view(lanes: Iterable[Any], readings: Iterable[Any] = (), closures: Iterable[Any] = (),
               attempts: Iterable[Any] = (), jobs: Iterable[Any] = (), *,
               now: str | datetime | None = None, reading_ttl_s: int = READING_TTL_S,
               desktop_account: str | None = None) -> dict[str, Any]:
    """C-6.4, C-9.1, C-9.6, C-10.3–4: assemble an immutable-input snapshot.

    V1-owned and desktop lanes remain visible for status and rejection evidence.
    Unknown desktop identity preserves recorded flags. No lease or process-list
    estimate can create an occupied lane slot: only active attempt rows do so.
    """
    instant = _time(now) if now is not None else datetime.now(timezone.utc)
    timestamp = _iso(instant)
    evidence = latest_readings(readings, now=instant, reading_ttl_s=reading_ttl_s)
    active_closures = [row for item in closures
                       if not (row := _row(item)).get("released_at") and _time(row["until_at"]) > instant]
    attempt_rows, job_rows = [_row(item) for item in attempts], [_row(item) for item in jobs]
    counts = Counter(row["lane_id"] for row in attempt_rows if row["state"] in ACTIVE_ATTEMPT_STATES)
    roster = []
    for item in lanes:
        lane = _row(item)
        identity = lane["lane_id"]
        if desktop_account is not None and lane["provider"] == "claude":
            lane["desktop"] = _account_matches(lane, desktop_account)
        lane["readings"] = [row for row in evidence if row["lane_id"] == identity]
        lane["closures"] = [row for row in active_closures if row["lane_id"] == identity]
        lane["in_flight"] = counts[identity]
        lane["measured"] = any(fresh_provider(row, now=instant, reading_ttl_s=reading_ttl_s)
                               for row in lane["readings"])
        roster.append(lane)
    roster.sort(key=lambda lane: _display_order(lane, now=instant, reading_ttl_s=reading_ttl_s))
    return {"lanes": roster, "readings": evidence, "closures": active_closures,
            "attempts": attempt_rows, "jobs": job_rows,
            "in_flight": {lane["lane_id"]: counts[lane["lane_id"]] for lane in roster},
            "now": timestamp, "reading_ttl_s": reading_ttl_s}


def owned_lanes(view: Mapping[str, Any], owner: str = "v2") -> list[dict[str, Any]]:
    """C-10.4: select owner-bound rows without mutating the visible roster."""
    return [lane for lane in view["lanes"] if lane.get("owner") == owner]


def from_store(store: Any, *, now: str | datetime | None = None,
               reading_ttl_s: int = READING_TTL_S, desktop_account: str | None = None) -> dict[str, Any]:
    """Read store rows; supply desktop identity read before any transaction."""
    return build_view(store.lane_rows(), store.list_readings(), store.list_closures(),
                      store.list_attempts(), store.list_jobs(), now=now,
                      reading_ttl_s=reading_ttl_s, desktop_account=desktop_account)
