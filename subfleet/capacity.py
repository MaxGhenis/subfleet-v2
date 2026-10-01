"""A read-only snapshot of routing evidence (C-6.4, C-9.1, C-10).

The builder operates on rows and never probes, reads process state, or changes
the store. Desktop identity is read separately so callers can keep filesystem
access outside admission transactions (C-3.3).
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .contracts import READING_TTL_S, IdentityStatus
from .sessions.transcripts import read_regular

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


def _login_email(text: str) -> str | None:
    value = json.loads(text)
    account = value.get("oauthAccount") if isinstance(value, dict) else None
    email = account.get("emailAddress") if isinstance(account, dict) else None
    return email.strip().casefold() if isinstance(email, str) and email.strip() else None


#: The last login file read: (path, what `stat` said of it, the email it named).
_desktop_hint: tuple[str, tuple[int, ...], str | None] | None = None


def forget_desktop_account() -> None:
    """Drop the remembered login file; the next read parses it again."""
    global _desktop_hint
    _desktop_hint = None


def read_desktop_account(path: str | Path | None = None) -> str | None:
    """C-10.3: reread the current Claude desktop login without caching secrets.

    An unreadable or incomplete login file is unknown, so callers preserve the
    last recorded desktop flags instead of silently removing protection.

    The file is looked at on every call and parsed only when it has changed:
    admission asks each pass, the app's file runs to hundreds of kilobytes, and
    a rewrite of any kind changes its inode, size or one of its timestamps. What
    is kept between calls is the email alone, which the store already holds.
    """
    global _desktop_hint
    target = Path(path) if path is not None else Path.home() / ".claude.json"
    try:
        found = os.stat(target)
        seen = (found.st_dev, found.st_ino, found.st_size, found.st_mtime_ns, found.st_ctime_ns)
        kept = _desktop_hint
        if kept is not None and kept[:2] == (str(target), seen):
            return kept[2]
        # Only as a regular file, never waiting in open() (readings, pick and the
        # admission pass read it on pools Daemon.close() waits for).
        result = _login_email(read_regular(target).decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        _desktop_hint = None
        return None
    _desktop_hint = (str(target), seen, result)
    return result


def cached_desktop_identity(path: str | Path | None = None) -> dict[str, Any]:
    """C-10.3: the whole cached login, for `doctor` to compare against a profile.

    `~/.claude.json` carries the desktop app's own idea of who it is signed in
    as. It is a hint: the file is written by the app and can name an account the
    credential beside it does not hold — the 2026-09-05 incident exactly. Only
    `doctor` reads more of it than the email, and only to report a disagreement.
    """
    try:
        value = json.loads(read_regular(Path(path) if path is not None
                                        else Path.home() / ".claude.json").decode("utf-8"))
        account = value.get("oauthAccount") if isinstance(value, dict) else None
    except (OSError, UnicodeError, ValueError):
        return {}
    if not isinstance(account, dict):
        return {}
    return {"email": account.get("emailAddress"),
            "account_uuid": account.get("accountUuid"),
            "org_uuid": account.get("organizationUuid"),
            "organization": account.get("organizationName")}


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
    """The newest recorded desktop identity, or nothing (C-10.3).

    Rows are scanned newest first for one that actually carries an identity:
    every store mutation also writes an audit event of the same kind (C-3.2),
    and that companion row has no payload of its own.
    """
    rows = sorted((_row(item) for item in events),
                  key=lambda row: row.get("event_id") or 0, reverse=True)
    for row in rows:
        if row.get("kind") != DESKTOP_IDENTITY_EVENT:
            continue
        try:
            data = json.loads(row.get("data_json") or "{}")
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("identity"):
            return data
    return None


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


def fresh_until(readings: Iterable[Mapping[str, Any]], *, now: str | datetime,
                reading_ttl_s: int = READING_TTL_S) -> datetime | None:
    """C-6.3: the first instant after `now` at which a reading fresh at `now`
    may no longer be (its `observed_at` plus `reading_ttl_s`, or its
    `resets_at`, whichever comes first); None when no reading is fresh.

    Until then every lane measured at `now` is still measured. A routing
    decision also waits on other clocks: `decision_horizon`."""
    instant = _time(now)
    ends = []
    for item in readings:
        row = _row(item)
        if fresh_provider(row, now=instant, reading_ttl_s=reading_ttl_s):
            end = _time(row["observed_at"]) + timedelta(seconds=reading_ttl_s)
            if row.get("resets_at"):
                end = min(end, _time(row["resets_at"]))
            ends.append(end)
    return min(ends, default=None)


def lane_horizons(view: Mapping[str, Any], *, reading_ttl_s: int = READING_TTL_S) -> dict[str, datetime]:
    """C-6.3: per lane, the first instant after the view's `now` at which the
    clock alone may change how `scheduler.evaluate` judges that lane on the
    view's rows; a lane with no such instant is left out.

    `evaluate` reads the clock only through `fresh_provider` and a closure's
    `until_at`, and a lane's verdict and detail (`scheduler.judge_lane`) read
    only that lane's readings and closures. So before its horizon a lane is
    judged as the view judged it: until one of its readings turns fresh (a
    future `observed_at`) or stops being fresh (`fresh_until`), or one of its
    closures ends (`until_at`). Either can close a lane, not only open one (a
    reported closure on a reserved model gives its lane slack behind a probe,
    C-11.7, that turns `unmeasured` when the closure ends; review of f48df54).

    One lane's horizon says nothing about another's. C-6.3's check inside a
    reservation judges again only the lanes whose horizon has passed among
    those the job's decision looks at, so a reading ageing out on a lane the
    job could never run on (another provider's, or not the lane it is pinned
    to) never sends its route to be evaluated again (review of d04b8b3: sixty
    Claude lanes' staggered readings kept a Codex job from ever being placed).

    A reading's label (`stale-provider` past `reading_ttl_s`) and its age are
    evidence, not judgement; the check gives them again at its own clock."""
    instant = _time(view["now"])
    found: dict[str, datetime] = {}

    def note(lane_id: str, clock: datetime | None) -> None:
        if clock is not None and (lane_id not in found or clock < found[lane_id]):
            found[lane_id] = clock
    for item in view.get("readings", ()):
        row = _row(item)
        note(row["lane_id"], fresh_until([row], now=instant, reading_ttl_s=reading_ttl_s))
        observed = _time(row["observed_at"])
        if observed > instant and fresh_provider(row, now=observed, reading_ttl_s=reading_ttl_s):
            note(row["lane_id"], observed)
    for item in view.get("closures", ()):
        row = _row(item)
        if not row.get("released_at") and (until := _time(row["until_at"])) > instant:
            note(row["lane_id"], until)
    return found


def decision_horizon(view: Mapping[str, Any], *, reading_ttl_s: int = READING_TTL_S,
                     ends: Iterable[str | datetime] = ()) -> datetime | None:
    """C-6.3: the first instant after the view's `now` at which a routing decision
    taken on the view's rows may change with no row changing; None when nothing
    in it waits on the clock.

    `scheduler.evaluate` reads the clock only through `fresh_provider` and a
    closure's `until_at`, so its decision on these rows is the same at every
    instant before the earliest of: a reading turning fresh (its future
    `observed_at`) or no longer fresh (`fresh_until`), a closure ending (its
    `until_at`), and each of `ends` (a confirmed override's `weekly_reset_at`,
    which puts the readings it held out back). Any of them can close a lane,
    not only open one: an override that ends shows a reading below the floor,
    and a reported closure on a reserved model gives its lane slack behind a
    probe (C-11.7) that turns `unmeasured` when the closure ends (review of
    f48df54). It is the earliest of every lane's own (`lane_horizons`) and of
    `ends`; C-6.3's check reads the lanes' own, never this fleet-wide one."""
    clocks = [*lane_horizons(view, reading_ttl_s=reading_ttl_s).values(), *(_time(end) for end in ends)]
    return min(clocks, default=None)


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


def mark_desktop(lane: dict[str, Any], *, desktop: DesktopIdentity | None = None,
                 desktop_account: str | None = None, desktop_in_use: bool | None = None) -> dict[str, Any]:
    """C-10.3: set a lane row's `desktop` flag as a view does, and return the row.

    Only a Claude lane is the desktop app's. `desktop` (the profile endpoint's
    answer) decides when it can say anything; otherwise the recorded flag
    stands. `desktop_account` is the bare hint, for callers that have only it.
    `desktop_in_use` is whether Claude Code is using that login now
    (`sessions.registry.desktop_login_in_use`): it is put on the desktop lane as
    `desktop_in_use`, and a desktop lane without it is judged in use, as every
    desktop lane was before 2026-09-27. C-6.3's check inside a reservation marks
    the lane rows it reads with this, so it sees each lane as the view the
    decision was made on did."""
    if lane["provider"] == "claude":
        if desktop is not None:
            if desktop.decisive:
                lane["desktop"] = desktop.owns(lane)
            lane["desktop_identity"] = desktop.status
        elif desktop_account is not None:
            lane["desktop"] = _account_matches(lane, desktop_account)
        if lane.get("desktop") and desktop_in_use is not None:
            lane["desktop_in_use"] = bool(desktop_in_use)
        else:
            lane.pop("desktop_in_use", None)
    return lane


def desktop_excluded(lane: Mapping[str, Any]) -> bool:
    """C-10.3: the desktop login's lane while Claude Code uses it (or while that is unknown)."""
    return bool(lane.get("desktop")) and lane.get("desktop_in_use", True) is not False


def build_view(lanes: Iterable[Any], readings: Iterable[Any] = (), closures: Iterable[Any] = (),
               attempts: Iterable[Any] = (), jobs: Iterable[Any] = (), *,
               now: str | datetime | None = None, reading_ttl_s: int = READING_TTL_S,
               desktop_account: str | None = None,
               desktop: DesktopIdentity | None = None,
               desktop_in_use: bool | None = None) -> dict[str, Any]:
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
    # C-26.9: conversation turns have their own capacity, so their attempts are
    # counted apart from detached jobs' (`in_flight` stays the detached count).
    turn_jobs = {row["job_id"] for row in job_rows if row.get("kind") == "turn"}
    active = [row for row in attempt_rows if row["state"] in ACTIVE_ATTEMPT_STATES]
    counts = Counter(row["lane_id"] for row in active if row.get("job_id") not in turn_jobs)
    turn_counts = Counter(row["lane_id"] for row in active if row.get("job_id") in turn_jobs)
    roster = []
    for item in lanes:
        lane = mark_desktop(_row(item), desktop=desktop, desktop_account=desktop_account,
                            desktop_in_use=desktop_in_use)
        identity = lane["lane_id"]
        lane["readings"] = [row for row in evidence if row["lane_id"] == identity]
        lane["closures"] = [row for row in active_closures if row["lane_id"] == identity]
        lane["in_flight"] = counts[identity]
        lane["in_flight_turns"] = turn_counts[identity]
        lane["measured"] = any(fresh_provider(row, now=instant, reading_ttl_s=reading_ttl_s)
                               for row in lane["readings"])
        roster.append(lane)
    roster.sort(key=lambda lane: _display_order(lane, now=instant, reading_ttl_s=reading_ttl_s))
    return {"lanes": roster, "readings": evidence, "closures": active_closures,
            "attempts": attempt_rows, "jobs": job_rows,
            "in_flight": {lane["lane_id"]: counts[lane["lane_id"]] for lane in roster},
            "in_flight_turns": {lane["lane_id"]: turn_counts[lane["lane_id"]] for lane in roster},
            "now": timestamp, "reading_ttl_s": reading_ttl_s}


def credential_latched(lane: Mapping[str, Any]) -> bool:
    """A lane whose credential its last probe found revoked or unusable, as
    `Timers.enrich_view` merges the probe into the row: the view marks it
    `credential-latched` in `unavailable_lanes`, so it has no slot until it heals
    or is re-enrolled. C-6.3's check inside a reservation marks it the same way."""
    return lane.get("revoked_epoch") is not None or lane.get("probe_status") in (
        "revoked", "auth-revoked", "expired-token", "no-auth")


def credential_gone(lane: Mapping[str, Any]) -> bool:
    """C-11.8: a latched credential only a person brings back, by logging in again or
    re-enrolling (a new credential epoch): revoked, no login at all, or a Codex
    lane's expired token whose one heal for this epoch has run and left it expired
    (`heal_spent`, set by the timers' probe, C-23.47). Any other expired token
    latches the lane's slot too (`credential_latched`) but may still come back by
    itself: before its heal, or on a Claude home lane, whose heal the timers retry
    every 20 minutes, so it is not this."""
    return (lane.get("revoked_epoch") is not None
            or lane.get("probe_status") in ("revoked", "auth-revoked", "no-auth")
            or lane.get("provider") == "codex" and lane.get("probe_status") == "expired-token"
            and bool(lane.get("heal_spent")))


def identity_blocked(lane: Mapping[str, Any]) -> bool:
    """C-10.6: a lane whose own credential proved to hold a different account is
    not a candidate until an operator re-enrols it. Nothing else about the lane —
    not a fresh reading, not an empty slot — releases it."""
    return str(lane.get("identity_status") or "") == IdentityStatus.MISMATCH.value


def open_lanes(view: Mapping[str, Any], caps: Mapping[str, Any]) -> list[str]:
    """C-6.11: the lanes that could take some job now, whatever its model.

    Owned by v2, enabled, not the desktop login while Claude Code uses it
    (C-10.3), identity not mismatched, under no account-wide closure, with a slot
    free (always, while no per-lane cap is set, C-6.4) and no probe holding it. A
    lane closed for one model only is open: another model may still run there. A
    job can still be refused an open lane (a model-scoped closure, the reserve,
    its own exclusions); the count says capacity exists, not that it fits.
    """
    from .policy import lane_slot_cap

    found = []
    for lane in view["lanes"]:
        slots = lane_slot_cap(caps, bool(lane.get("measured")))
        if (lane.get("owner") == "v2" and lane.get("enabled", True) and not desktop_excluded(lane)
                and not identity_blocked(lane)
                and not any(row.get("scope") == "account" for row in lane.get("closures", ()))
                and (slots is None or lane.get("in_flight", 0) < slots)
                and lane["lane_id"] not in view.get("unavailable_lanes", {})):
            found.append(lane["lane_id"])
    return sorted(found)


def owned_lanes(view: Mapping[str, Any], owner: str = "v2") -> list[dict[str, Any]]:
    """C-10.4: select owner-bound rows without mutating the visible roster."""
    return [lane for lane in view["lanes"] if lane.get("owner") == owner]


def store_rows(store: Any) -> dict[str, list]:
    """The store rows `build_view` is made from, as its keyword arguments.

    C-3.7: read them in one `Store.snapshot()` and build the view after it, so
    a view build holds no read connection (review of 5841d8b, finding 2)."""
    # Every reading that can be a key's newest, not every reading.
    readings = getattr(store, "latest_reading_candidates", store.list_readings)()
    return {"lanes": store.lane_rows(), "readings": readings, "closures": store.list_closures(),
            "attempts": store.list_attempts(), "jobs": store.list_jobs()}


def from_store(store: Any, *, now: str | datetime | None = None,
               reading_ttl_s: int = READING_TTL_S, desktop_account: str | None = None,
               desktop: DesktopIdentity | None = None, desktop_in_use: bool | None = None) -> dict[str, Any]:
    """Read store rows; supply desktop identity read before any transaction."""
    return build_view(**store_rows(store), now=now, reading_ttl_s=reading_ttl_s,
                      desktop_account=desktop_account, desktop=desktop, desktop_in_use=desktop_in_use)
