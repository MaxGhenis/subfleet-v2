"""C-10.8: the bounded automatic re-check of lanes disabled as auth-dead.

A lane an `auth-dead` verdict disabled (C-23.44) used to stay off until someone ran
`subfleet lanes enroll <credential>`. Accounts lapse and are renewed, so the daemon
now re-checks such a lane on a slow schedule and, when its credential authenticates
again as the account it was enrolled as, re-enrolls it through the one path an
operator uses (`Daemon._enroll_lane_locked`, C-10.2), which gives it a successor
lane id (C-1.3).

This module is the pure half: which disabled lanes may be re-checked, when each is
due, and which one (at most one) runs now. It reads rows and event payloads the
caller passes in and never touches the store, the network or the clock, so its
bounds are properties a test can state for every input:

- a lane is re-checked at most once per `interval_s` (never under one hour,
  `MIN_INTERVAL_S`, whatever the policy says);
- each failed re-check doubles the wait to the next, up to `max_interval_s`;
- any two re-checks, of any lanes, start at least `spacing_s` apart;
- nothing is chosen while the daemon has no free attempt slot, and, until a lane
  is overdue by `max_interval_s`, while the host is loaded.

Only a lane whose disable was recorded as auth-dead is ever eligible. A lane
turned off for any other reason (an identity mismatch, a non-canonical duplicate,
the operator's roster) is left alone, and so is one whose identity was never
recorded, since there would be nothing to compare its credential's answer with.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

AUTH_DEAD = "auth-dead"
#: The event each re-check writes, whatever its result (`Daemon._auth_recheck`).
RECHECK_EVENT = "lane.auth-recheck"
#: The results a re-check records. `started` is written before the provider is
#: asked, so a daemon that dies mid-check still counts it; every result counts as
#: a request for the cadence bound, and only `failed` adds to the backoff.
RESULTS = ("started", "restored", "failed", "interrupted")

#: `policy.json` `timers` keys and their defaults (C-10.8, C-18.1).
DEFAULTS: dict[str, float] = {
    # 6 h from the disable to the first re-check; each failure doubles the wait.
    "auth_recheck_interval_s": 21600,
    # The ceiling of that backoff: once a day, however long the account stays lapsed.
    "auth_recheck_max_interval_s": 86400,
    # At least this long between any two re-checks, so they never come in a burst.
    "auth_recheck_spacing_s": 900,
    # How often the timer looks for a due lane.
    "auth_recheck_tick_s": 300,
    # A 1-minute load average per CPU above which a re-check waits (host load).
    "auth_recheck_max_load_per_cpu": 2.0,
}
#: The shortest per-lane interval: at most one re-check of a lane an hour, even
#: under a policy that asks for more (C-10.8; the loader refuses less).
MIN_INTERVAL_S = 3600
#: Each lane's schedule is offset by up to this fraction of its interval, the
#: same every time for the same lane, so lanes disabled together drift apart.
JITTER_FRACTION = 0.1
#: The most of `lanes.json` a re-check reads; a larger roster is unreadable.
ROSTER_LIMIT = 4 << 20


def _instant(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Settings:
    interval_s: float = DEFAULTS["auth_recheck_interval_s"]
    max_interval_s: float = DEFAULTS["auth_recheck_max_interval_s"]
    spacing_s: float = DEFAULTS["auth_recheck_spacing_s"]
    tick_s: float = DEFAULTS["auth_recheck_tick_s"]
    max_load_per_cpu: float = DEFAULTS["auth_recheck_max_load_per_cpu"]

    @property
    def enabled(self) -> bool:
        """`auth_recheck_interval_s: 0` switches the re-check off."""
        return self.interval_s > 0

    @classmethod
    def from_policy(cls, policy: Mapping[str, Any]) -> "Settings":
        """The loader validates these (`policy.load_policy`); this clamps anyway,
        because a policy dict built in code never passes through the loader."""
        timers = policy.get("timers") or {}

        def number(key: str) -> float:
            value = timers.get(key, DEFAULTS[key])
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return DEFAULTS[key]
            return float(value)

        interval = number("auth_recheck_interval_s")
        if interval <= 0:
            return cls(interval_s=0)
        interval = max(interval, MIN_INTERVAL_S)
        return cls(interval_s=interval,
                   max_interval_s=max(interval, number("auth_recheck_max_interval_s")),
                   spacing_s=max(0.0, number("auth_recheck_spacing_s")),
                   tick_s=max(1.0, number("auth_recheck_tick_s")),
                   max_load_per_cpu=number("auth_recheck_max_load_per_cpu"))


def backoff_s(failures: int, settings: Settings) -> float:
    """Seconds from a re-check to the next after `failures` in a row: each failure
    doubles the wait, from `interval_s` (none yet) up to `max_interval_s`."""
    steps = max(0, int(failures))
    # 2 ** 60 already exceeds any ceiling; the exponent is bounded so it stays finite.
    return min(settings.max_interval_s, settings.interval_s * 2 ** min(steps, 60))


def jitter_s(lane_id: str, seconds: float) -> float:
    """A fixed offset in [0, JITTER_FRACTION × seconds) for this lane."""
    digest = hashlib.sha256(lane_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64 * JITTER_FRACTION * seconds


@dataclass(frozen=True)
class Standing:
    """Where one disabled lane stands for the re-check."""
    lane_id: str
    eligible: bool
    why: str                        # `auth-dead` when eligible, else why it is left alone
    disabled_at: str | None = None
    last: Mapping[str, Any] | None = None   # its latest re-check since that disable
    next_at: str | None = None      # when it is next due; only an eligible lane is
    successor: str | None = None    # the lane a re-check (or an operator) bound since

    @property
    def failures(self) -> int:
        return int((self.last or {}).get("failures") or 0)

    def as_dict(self) -> dict[str, Any]:
        last = self.last or {}
        return {"eligible": self.eligible, "why": self.why, "disabled_at": self.disabled_at,
                "last_at": last.get("at"), "last_result": last.get("result"),
                "last_detail": last.get("detail"), "failures": self.failures,
                "next_at": self.next_at, "successor": self.successor}


def disable_reason(lane_id: str, disabled: Mapping[str, Mapping[str, Any]],
                   verdicts: Mapping[str, Mapping[str, Any]]) -> str | None:
    """Why the daemon turned `lane_id` off, as recorded when it did.

    The `lane.disabled` events (`Store.disable_lane`) name it. A lane id is never
    enabled again once disabled (a re-enrolment binds a new one), so every reason
    recorded for it stands: one that is not `auth-dead` (a mismatch found by a
    probe, then an attempt's auth-dead) excludes the lane for good. A lane
    disabled before those events existed is judged by the verdict every automatic
    disable wrote with it: `auth-dead` (`Timers.record_auth_dead`, the probe
    cycle's auth-dead), `duplicate` (non-canonical) or `identity-mismatch`. A
    disabled lane is never probed again, so that verdict is still its latest. A
    lane with neither (seeded disabled from `lanes.json`, say) has no reason.
    """
    record = disabled.get(lane_id)
    if record is not None:
        reasons = [str(reason) for reason in record.get("reasons") or (record.get("reason"),) if reason]
        return next((reason for reason in reasons if reason != AUTH_DEAD), reasons[-1] if reasons else None)
    verdict = (verdicts.get(lane_id) or {}).get("verdict")
    return {"auth-dead": AUTH_DEAD, "duplicate": "non-canonical",
            "identity-mismatch": "identity-mismatch"}.get(verdict)


def _identity_comparable(row: Mapping[str, Any]) -> str | None:
    """None when a re-check could tell this lane's account from another's, else why not.

    C-10.6: a Claude lane either recorded the identity its profile returned (a
    re-check must return the same one, verified) or is a setup-token lane whose
    token the profile endpoint refuses (`enrolled`; a re-check must be refused
    the same way, and the account is the one its keychain item names). A Codex
    lane's account comes from its home's own token claims (C-1.4)."""
    status = row.get("identity_status")
    if status == "mismatch":
        return "identity-mismatch"
    if row.get("provider") == "claude" and not row.get("identity") and status != "enrolled":
        return "identity-unverified"
    return None


def standings(lanes: Iterable[Mapping[str, Any]], *, disabled: Mapping[str, Mapping[str, Any]],
              verdicts: Mapping[str, Mapping[str, Any]], rechecks: Mapping[str, Mapping[str, Any]],
              roster_off: set[str] | None, settings: Settings) -> dict[str, Standing]:
    """Every disabled lane's standing. `lanes` in binding order (`created_at`, rowid);
    `disabled` and `rechecks` the latest event payload per lane; `verdicts` the
    timers' latest verdict per lane; `roster_off` the lane ids `lanes.json` marks
    `enabled: false`, or None when the roster could not be read (then nothing is
    eligible: the operator's choice is unknown)."""
    rows = [dict(row) for row in lanes]
    by_credential: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_credential.setdefault(row["credential_ref"], []).append(row)
    enabled_accounts = {row["account_key"]: row["lane_id"] for row in rows if row["enabled"]}
    result: dict[str, Standing] = {}
    for row in rows:
        if row["enabled"]:
            continue
        lane_id = row["lane_id"]
        record = disabled.get(lane_id) or {}
        disabled_at = record.get("at") or row.get("updated_at")
        last = rechecks.get(lane_id)
        if last and disabled_at and _instant(last["at"]) < _instant(disabled_at):
            last = None                 # a re-check from before this disable
        bindings = by_credential[row["credential_ref"]]
        later = bindings[bindings.index(row) + 1:]
        bound = next((other["lane_id"] for other in bindings if other["enabled"]), None)
        successor = (last or {}).get("successor") or bound or (later[-1]["lane_id"] if later else None)

        def left(why: str) -> Standing:
            return Standing(lane_id, False, why, disabled_at, last, None, successor)

        reason = disable_reason(lane_id, disabled, verdicts)
        if later or bound:
            result[lane_id] = left("superseded")
        elif row.get("owner") != "v2":
            result[lane_id] = left("owner-v1")
        elif row.get("desktop"):
            result[lane_id] = left("desktop")
        elif roster_off is None:
            result[lane_id] = left("roster-unreadable")
        elif lane_id in roster_off:
            result[lane_id] = left("operator")
        elif reason != AUTH_DEAD:
            result[lane_id] = left(reason or "unrecorded")
        elif (unfit := _identity_comparable(row)) is not None:
            result[lane_id] = left(unfit)
        elif row["account_key"] in enabled_accounts:
            result[lane_id] = left("account-enabled")
        elif not settings.enabled:
            result[lane_id] = Standing(lane_id, True, AUTH_DEAD, disabled_at, last, None, None)
        else:
            if last is None:
                base, wait = _instant(disabled_at), settings.interval_s
            else:
                base, wait = _instant(last["at"]), backoff_s(int(last.get("failures") or 0), settings)
            due = base + timedelta(seconds=wait + jitter_s(lane_id, wait))
            result[lane_id] = Standing(lane_id, True, AUTH_DEAD, disabled_at, last, iso(due), None)
    return result


def last_started(rechecks: Mapping[str, Mapping[str, Any]]) -> datetime | None:
    """When the latest re-check of any lane began."""
    starts = [_instant(data["at"]) for data in rechecks.values() if data.get("at")]
    return max(starts, default=None)


def choose(current: Mapping[str, Standing], *, now: datetime, settings: Settings,
           started: datetime | None, busy: str | None = None,
           load_per_cpu: float | None = None) -> tuple[str | None, str | None]:
    """At most one lane to re-check now: `(lane_id, None)`, or `(None, why)` when
    one is due but must wait, or `(None, None)` when none is due.

    `started` is when the latest re-check of any lane began; `busy` names why the
    daemon has no attempt slot to spare; `load_per_cpu` is the host's 1-minute
    load average per CPU, or None when unknown.
    """
    if not settings.enabled:
        return None, "off"
    now = _instant(now)
    due = sorted((s for s in current.values() if s.eligible and s.next_at and _instant(s.next_at) <= now),
                 key=lambda s: (s.next_at, s.lane_id))
    if not due:
        return None, None
    if started is not None and now < _instant(started) + timedelta(seconds=settings.spacing_s):
        return None, "spacing"
    if busy:
        return None, busy
    first = due[0]
    overdue = (now - _instant(first.next_at)).total_seconds()
    if (load_per_cpu is not None and load_per_cpu > settings.max_load_per_cpu
            and overdue < settings.max_interval_s):
        return None, f"host load {load_per_cpu:.1f} per CPU"
    return first.lane_id, None


def host_load_per_cpu() -> float | None:
    try:
        return os.getloadavg()[0] / (os.cpu_count() or 1)
    except (OSError, AttributeError):
        return None


def roster_disabled(root: Path) -> set[str] | None:
    """Lane ids the operator's `lanes.json` marks `"enabled": false`; None when the
    file cannot be read or parsed. Opened only as a regular file, never waiting in
    `open()` (the re-check runs on a timer worker `Timers.stop()` waits for)."""
    from .sessions.transcripts import read_regular
    path = Path(root) / "lanes.json"
    try:
        roster = json.loads(read_regular(path, ROSTER_LIMIT) or b"[]")
    except FileNotFoundError:
        return set()
    except (OSError, ValueError):
        return None
    rows = roster.get("lanes", []) if isinstance(roster, dict) else roster
    if not isinstance(rows, list):
        return None
    return {str(row["lane_id"]) for row in rows
            if isinstance(row, dict) and row.get("lane_id") and row.get("enabled") is False}


def disables_by_lane(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """`latest_by_lane` over `lane.disabled` rows, each latest payload carrying
    `reasons`: every reason recorded for that lane, in order (`disable_reason`)."""
    reasons: dict[str, list[str]] = {}
    rows = list(rows)
    for lane_id, data in ((row.get("lane_id"), _payload(row)) for row in rows):
        if lane_id and data and data.get("reason"):
            reasons.setdefault(lane_id, []).append(str(data["reason"]))
    latest = latest_by_lane(rows)
    for lane_id, data in latest.items():
        data["reasons"] = tuple(reasons.get(lane_id, ()))
    return latest


def _payload(row: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        data = json.loads(row["data_json"])
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and data else None


def latest_by_lane(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """The latest payload per lane from `events` rows (`lane_id`, `ts`, `data_json`)
    in event order; a payload without `at` takes the event's own timestamp.

    An empty payload is skipped: `Store.add_event` writes its event inside a
    transaction whose own audit event, of the same kind, follows it empty (as
    `Timers._latest` skips it too)."""
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not row.get("lane_id"):
            continue
        data = _payload(row)
        if data:
            data.setdefault("at", row.get("ts"))
            latest[row["lane_id"]] = data
    return latest


@dataclass
class Deferral:
    """The last reason a due re-check waited, kept in memory for `lanes list`."""
    why: str | None = None
    at: str | None = None
    lanes: tuple[str, ...] = field(default_factory=tuple)
