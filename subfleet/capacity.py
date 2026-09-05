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
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import READING_TTL_S, IdentityStatus

ACTIVE_ATTEMPT_STATES = frozenset({"reserved", "starting", "running", "finalizing"})

#: C-10.3: the store keeps the last desktop identity the profile endpoint
#: confirmed, so an unverifiable one still has something to compare against.
DESKTOP_IDENTITY_EVENT = "desktop.identity"


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


def lane_labels(lane: Mapping[str, Any]) -> set[str]:
    """Every email this lane is known by: its recorded label (C-1.4) and, for a
    lane enrolled before identities were bound, the email inside its account key.
    A verified lane's key is a pair of uuids and contributes no email at all."""
    values = set()
    label = lane.get("label")
    if isinstance(label, str) and label.strip():
        values.add(label.strip().casefold())
    tail = str(lane.get("account_key") or "").partition(":")[2]
    if "@" in tail:
        values.add(tail.strip().casefold())
    return values


@dataclass(frozen=True)
class DesktopIdentity:
    """C-10.3: who the Claude desktop app is signed in as, and how well we know it.

    `identity` and `label` come from the profile endpoint asked with the desktop
    app's own keychain credential — never from `~/.claude.json`, which is carried
    here as `cached_label`, a hint the doctor compares against and reports on.
    `last_label` is the label of the last identity that endpoint did confirm, kept
    in the store, so an outage still has something to compare against.
    """

    status: str                      # "verified" | "unverified"
    identity: str | None = None      # "<account_uuid>:<org_uuid>"
    label: str | None = None         # the profile's email, when verified
    cached_label: str | None = None  # ~/.claude.json oauthAccount, a hint only
    last_label: str | None = None    # label of the last verified desktop identity
    detail: str | None = None        # the profile status that produced this

    @property
    def verified(self) -> bool:
        return self.status == "verified" and bool(self.identity)

    @property
    def label_hints(self) -> frozenset[str]:
        """The emails that stand in for an identity comparison.

        Verified, only the desktop's own label, and only for a lane that records
        no identity of its own. Unverified, C-10.3's two fallbacks: the cached
        `oauthAccount` email and the last verified desktop identity's label.
        """
        values = {self.label} if self.verified else {self.cached_label, self.last_label}
        return frozenset(value.strip().casefold() for value in values
                         if isinstance(value, str) and value.strip())

    @property
    def decisive(self) -> bool:
        """Can this say anything at all? An identity nobody can name and no hint
        to fall back on leaves the recorded desktop flags exactly as they were."""
        return self.verified or bool(self.label_hints)

    def owns(self, lane: Mapping[str, Any]) -> bool:
        """C-10.3: is this lane the desktop app's own account?"""
        identity = lane.get("identity")
        if self.verified and identity:
            return str(identity) == self.identity
        return bool(lane_labels(lane) & self.label_hints)


def desktop_identity(profile: Any, *, cached_label: str | None = None,
                     last_label: str | None = None) -> DesktopIdentity:
    """C-10.3: build the desktop identity from one profile answer.

    `profile` is anything shaped like the Claude adapter's `ProfileResult` — it is
    duck-typed so this module stays free of provider code. A profile that did not
    answer leaves an unverified identity carrying only its fallbacks.
    """
    identity = getattr(profile, "identity", None) if profile is not None else None
    email = getattr(profile, "email", None) if profile is not None else None
    detail = getattr(profile, "status", None) if profile is not None else None
    if identity:
        return DesktopIdentity("verified", str(identity), email, cached_label,
                               last_label, detail)
    return DesktopIdentity("unverified", None, None, cached_label, last_label, detail)


def last_desktop_identity(events: Iterable[Any]) -> dict[str, Any] | None:
    """The newest recorded `desktop.identity` event payload, or nothing (C-10.3)."""
    newest = None
    for item in events:
        row = _row(item)
        if row.get("kind") != DESKTOP_IDENTITY_EVENT:
            continue
        if newest is None or (row.get("event_id") or 0) >= (newest.get("event_id") or 0):
            newest = row
    if newest is None:
        return None
    try:
        data = json.loads(newest.get("data_json") or "{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


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
            or not math.isfinite(utilization) or utilization < 0):
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
               desktop_account: str | None = None,
               desktop: DesktopIdentity | None = None) -> dict[str, Any]:
    """C-6.4, C-9.1, C-9.6, C-10.3–4: assemble an immutable-input snapshot.

    V1-owned and desktop lanes remain visible for status and rejection evidence.
    Unknown desktop identity preserves recorded flags. No lease or process-list
    estimate can create an occupied lane slot: only active attempt rows do so.

    `desktop` is C-10.3's answer: the identity the profile endpoint returned for
    the desktop app's own credential, with the cached `~/.claude.json` email kept
    beside it as a hint and never as the authority. `desktop_account` remains for
    callers that have only that hint; `desktop` wins when both are given.
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
        if lane["provider"] == "claude":
            if desktop is not None:
                if desktop.decisive:
                    lane["desktop"] = desktop.owns(lane)
                lane["desktop_identity"] = desktop.status
            elif desktop_account is not None:
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


def identity_blocked(lane: Mapping[str, Any]) -> bool:
    """C-10.6: a lane whose own credential proved to hold a different account is
    not a candidate until an operator re-enrols it. Nothing else about the lane —
    not a fresh reading, not an empty slot — releases it."""
    return str(lane.get("identity_status") or "") == IdentityStatus.MISMATCH.value


def owned_lanes(view: Mapping[str, Any], owner: str = "v2") -> list[dict[str, Any]]:
    """C-10.4: select owner-bound rows without mutating the visible roster."""
    return [lane for lane in view["lanes"] if lane.get("owner") == owner]


def from_store(store: Any, *, now: str | datetime | None = None,
               reading_ttl_s: int = READING_TTL_S, desktop_account: str | None = None,
               desktop: DesktopIdentity | None = None) -> dict[str, Any]:
    """Read store rows; supply desktop identity read before any transaction."""
    return build_view(store.lane_rows(), store.list_readings(), store.list_closures(),
                      store.list_attempts(), store.list_jobs(), now=now,
                      reading_ttl_s=reading_ttl_s, desktop_account=desktop_account,
                      desktop=desktop)
