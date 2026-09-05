"""Validated routing policy and compatibility helpers (C-11.1, C-6.4)."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import (
    DEFAULT_CAPS, HEADROOM_FLOOR, PROVIDERS, READING_TTL_S,
    Closure, Decision, Exit, Lane, Reading,
)

DEFAULT_POLICY_PATH = Path(__file__).with_name("default_policy.json")

#: `sessions.handoff_caps` (C-23.36): a character cap per brief section, carried
#: forward from v1 `handoff.py`'s module constants so a ported brief is the same
#: size it always was. `recent_records` is a count of main-chain entries, not
#: characters; it bounds the scan, and the character caps bound the result.
HANDOFF_CAPS: dict[str, int] = {
    "recent_records": 40,
    "recent": 48_000,
    "tool_result": 5_000,
    "tool_results_total": 16_000,
    "tool_input": 4_000,
    "tool_inputs_total": 12_000,
    "original_task": 24_000,
    "progress": 32_000,
    "repository": 16_000,
}

#: `sessions.*` (C-6.4): the sessions kit's caps, all of them policy data rather
#: than constants, because a restart storm or a slow host is a tuning problem.
#: `auto_revive_desktop_owned` is plan decision 7 and defaults to OFF: a lease in
#: subfleet's store binds only launches subfleet makes, so it cannot make a
#: headless revive exclusive against a desktop restart that lands between the
#: census and the launch (the 2026-09-04 twin). Handoff is the default recovery.
SESSION_DEFAULTS: dict[str, Any] = {
    "nudge_max_age_h": 8,            # C-23.33's age cap on the interruption
    "nudge_cooldown_min": 1.5,       # C-23.33's per-session cooldown (v1: 90 s)
    "nudge_delay_s": 8,              # the inbox binds a moment after SessionStart
    "nudge_sample_s": 3,             # C-23.34's liveness sample before delivery
    "sweep_quiet_s": 120,            # C-23.34: a hand-started sweep waits longer
    "muster_max_age_h": 2,           # the roll-call window
    "muster_quiet_s": 120,
    "revive_min_age_s": 120,         # below this the app may still restart it
    "revive_max_batch": 8,
    "auto_revive_desktop_owned": False,
    "mirror_interval_s": 60,         # C-23.28, plan decision 8
    "mirror_stall_min": 10,
    "mirror_hang_min": 30,           # C-23.28's in-flight tolerance
    "mirror_ultracode_default": True,
}


def policy_hash(path: str | Path) -> str:
    """C-11.1: hash the policy file's exact bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PolicyError(ValueError):
    """Invalid routing data or model input, reported as exit 2 (C-11.1)."""

    code = Exit.INVALID_INPUT

    def __init__(self, path: str | Path, key: str, message: str):
        self.path, self.key = str(path), key
        super().__init__(f"{self.path}: {key}: {message}")


def _name(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _fraction(value: Any, maximum: float = 1) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= maximum)


def load_policy(path: str | Path) -> dict[str, Any]:
    """C-11.1: validate routing data and retain the hash of the bytes evaluated.

    Extra policy keys are preserved for independent policy consumers. Each
    known field is validated before routing can use it; every error includes
    the file and dotted/indexed key that needs correcting.
    """
    path = Path(path)

    def fail(key: str, message: str) -> None:
        raise PolicyError(path, key, message)

    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PolicyError(path, "$", f"cannot read policy JSON: {error}") from error
    if not isinstance(value, dict):
        fail("$", "must be an object")
    required = ("tiers", "chains", "fallback", "permissions", "models", "retired",
                "desktop_login", "caps", "reset_credits")
    for key in required:
        if key not in value:
            fail(key, "required key is missing")

    tiers = value["tiers"]
    if not isinstance(tiers, list) or not tiers:
        fail("tiers", "must be a nonempty list of distinct tier names")
    for index, tier in enumerate(tiers):
        if not _name(tier):
            fail(f"tiers[{index}]", "must be a nonempty tier name")
        if tier in tiers[:index]:
            fail(f"tiers[{index}]", f"duplicate tier {tier!r}")

    models = value["models"]
    if not isinstance(models, dict) or not models:
        fail("models", "must be a nonempty map of short names to model definitions")
    for name, model in models.items():
        key = f"models.{name}"
        if not _name(name):
            fail(key, "model short name must be nonempty")
        if not isinstance(model, dict):
            fail(key, "must be a model definition object")
        if model.get("provider") not in PROVIDERS:
            fail(f"{key}.provider", f"must be one of {', '.join(PROVIDERS)}")
        if not _name(model.get("id")):
            fail(f"{key}.id", "must be a nonempty model id")
        for optional in ("effort", "scope"):
            if optional in model and not _name(model[optional]):
                fail(f"{key}.{optional}", "must be a nonempty string when provided")

    chains = value["chains"]
    if not isinstance(chains, dict) or not chains:
        fail("chains", "must map task names to one model per tier")
    for task, chain in chains.items():
        key = f"chains.{task}"
        if not _name(task):
            fail(key, "task name must be nonempty")
        if not isinstance(chain, list) or len(chain) != len(tiers):
            fail(key, "must be a list with one short model name per tier")
        for index, model in enumerate(chain):
            if not _name(model) or model not in models:
                fail(f"{key}[{index}]", f"unknown model {model!r}; expected a models key")
    if value["fallback"] != "upward-only":
        fail("fallback", 'must be "upward-only"')
    if value["desktop_login"] != "never":
        fail("desktop_login", 'must be "never"')

    permissions = value["permissions"]
    if not isinstance(permissions, dict):
        fail("permissions", "must map task names (or *) to sandboxes")
    for task, sandbox in permissions.items():
        if task != "*" and task not in chains:
            fail(f"permissions.{task}", "unknown task; expected a chains key or *")
        if sandbox not in ("read-only", "workspace-write"):
            fail(f"permissions.{task}", "must be read-only or workspace-write")
    for task in chains:
        if task not in permissions and "*" not in permissions:
            fail(f"permissions.{task}", "missing sandbox; provide this task or *")

    retired = value["retired"]
    if not isinstance(retired, dict):
        fail("retired", "must map retired aliases to model short names")
    for alias, target in retired.items():
        if not _name(alias):
            fail(f"retired.{alias}", "alias must be a nonempty string")
        if alias in models:
            fail(f"retired.{alias}", "alias must not shadow a current model short name")
        if not _name(target) or target not in models:
            fail(f"retired.{alias}", f"unknown model {target!r}; expected a models key")

    if not isinstance(value["caps"], dict):
        fail("caps", "must be a map of positive admission bounds")
    caps = {**DEFAULT_CAPS, "reading_ttl_s": READING_TTL_S,
            "max_tokens_observed": None, **value["caps"]}
    for name, cap in caps.items():
        if name == "max_tokens_observed" and cap is None:
            continue
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
            fail(f"caps.{name}", "must be a positive integer")
    value["caps"] = caps

    floor = value.get("headroom_floor", HEADROOM_FLOOR)
    if not _fraction(floor):
        fail("headroom_floor", "must be a finite fraction between 0 and 1")
    value["headroom_floor"] = floor
    reset = value["reset_credits"]
    if not isinstance(reset, dict):
        fail("reset_credits", "must be an object")
    for key in ("enabled", "headroom_floor_pct", "min_interval_min"):
        if key not in reset:
            fail(f"reset_credits.{key}", "required key is missing")
    if not isinstance(reset["enabled"], bool):
        fail("reset_credits.enabled", "must be a boolean")
    if not _fraction(reset["headroom_floor_pct"], 100):
        fail("reset_credits.headroom_floor_pct", "must be a finite percentage between 0 and 100")
    interval = reset["min_interval_min"]
    if (not isinstance(interval, (int, float)) or isinstance(interval, bool)
            or not math.isfinite(interval) or interval <= 0):
        fail("reset_credits.min_interval_min", "must be a positive number of minutes")

    for section, defaults in (("timers", {"probe_interval_s": 300, "keepalive_interval_s": 18300}),
                              ("alerts", {"realert_hours": 6, "expiring_capacity_daily": True}),
                              ("sessions", SESSION_DEFAULTS)):
        supplied = value.get(section, {})
        if not isinstance(supplied, dict):
            fail(section, "must be an object")
        settings = {**defaults, **supplied}
        for key, default in defaults.items():
            item = settings[key]
            if isinstance(default, bool):
                if not isinstance(item, bool):
                    fail(f"{section}.{key}", "must be a boolean")
            elif (not isinstance(item, (int, float)) or isinstance(item, bool)
                  or not math.isfinite(item) or item <= 0):
                fail(f"{section}.{key}", "must be a positive finite number")
        value[section] = settings

    # `sessions.handoff_caps` is the one nested section: every handoff section is
    # bounded by an explicit character cap (C-23.36), and a cap of zero or a
    # non-number would silently produce an unbounded or empty brief.
    supplied_caps = value["sessions"].get("handoff_caps", {})
    if not isinstance(supplied_caps, dict):
        fail("sessions.handoff_caps", "must be an object of per-section character caps")
    caps = {**HANDOFF_CAPS, **supplied_caps}
    for key, item in caps.items():
        if (not isinstance(item, int) or isinstance(item, bool) or item <= 0):
            fail(f"sessions.handoff_caps.{key}", "must be a positive whole number of characters")
    value["sessions"]["handoff_caps"] = caps

    # Metadata is replaced even when a caller serializes a previously loaded map.
    value["_policy_hash"] = hashlib.sha256(raw).hexdigest()
    value["_policy_path"] = str(path)
    return value


def resolve_model(policy: Mapping[str, Any], name: str, *, key: str = "pinned_model",
                  note: bool = True) -> str:
    """C-11.1: resolve a current name, exact id, or retired alias to a short name."""
    models = policy["models"]
    if _name(name):
        if name in models:
            return name
        retired = policy.get("retired", {})
        if name in retired:
            target = retired[name]
            if note:
                print(f"subfleet: retired model {name!r} resolves to {target!r}", file=sys.stderr)
            return target
        for short, model in models.items():
            if model["id"] == name:
                return short
    raise PolicyError(policy.get("_policy_path", "policy.json"), key,
                      f"unknown model {name!r}; expected a models or retired key")


def _row(value: Any) -> dict[str, Any]:
    return asdict(value) if is_dataclass(value) else dict(value)


def _time(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def lane_capacity(policy: Mapping[str, Any], lane_id: str,
                  readings: Iterable[Reading | Mapping[str, Any]], *, now: datetime | str | None = None) -> int:
    """C-6.4: an unmeasured or stale lane is limited to one in-flight attempt."""
    instant = _time(now) if now is not None else datetime.now(timezone.utc)
    caps = {**DEFAULT_CAPS, "reading_ttl_s": READING_TTL_S, **policy.get("caps", {})}
    measured = False
    for item in readings:
        row = _row(item)
        if row.get("lane_id") != lane_id or row.get("label") != "provider":
            continue
        try:
            age = (instant - _time(row["observed_at"])).total_seconds()
            if 0 <= age <= caps["reading_ttl_s"] and (not row.get("resets_at") or _time(row["resets_at"]) > instant):
                measured = True
                break
        except (ValueError, TypeError, KeyError):
            continue
    return caps["max_in_flight_per_lane"] if measured else min(caps["max_in_flight_per_lane"], caps["max_in_flight_unmeasured"], 1)


def pick(policy: Mapping[str, Any], lanes: Iterable[Lane | Mapping[str, Any]], *,
         pinned_model: str | None = None, pinned_lane: str | None = None,
         task: str | None = None, tier: str | None = None, exclusions: Iterable[str] = (),
         allow_desktop: bool = False, closures: Iterable[Closure | Mapping[str, Any]] = (),
         readings: Iterable[Reading | Mapping[str, Any]] = (), in_flight: Mapping[str, int] | None = None,
         policy_digest: str = "", now: datetime | str | None = None) -> Decision:
    """Pick a pinned lane or the first eligible lane for one model (C-11.2 subset)."""
    instant = _time(now) if now is not None else datetime.now(timezone.utc)
    roster = [_row(lane) for lane in lanes]
    readings, closures = list(readings), [_row(closure) for closure in closures]
    in_flight, excluded = in_flight or {}, set(exclusions)
    models = policy["models"]
    selected = next((lane for lane in roster if lane["lane_id"] == pinned_lane), None) if pinned_lane else None
    if pinned_lane and selected is None:
        raise ValueError(f"unknown pinned lane {pinned_lane!r}")
    if task is not None and task not in policy["chains"]:
        raise ValueError(f"unknown task {task!r}")
    if tier is not None and tier not in policy["tiers"]:
        raise ValueError(f"unknown tier {tier!r}")
    if pinned_model:
        short = resolve_model(policy, pinned_model)
    elif task:
        default_tier = "standard" if "standard" in policy["tiers"] else policy["tiers"][0]
        short = policy["chains"][task][policy["tiers"].index(tier or default_tier)]
    elif selected:
        short = next(name for name, model in models.items() if model["provider"] == selected["provider"])
    else:
        short = next(iter(models))
    model = models[short]
    if selected and selected["provider"] != model["provider"]:
        raise ValueError("pinned lane and model have different providers")
    caps = {**DEFAULT_CAPS, **policy.get("caps", {})}
    candidates, rejections = [], []
    fleet_full = sum(in_flight.values()) >= caps["max_active_attempts"]
    for lane in roster:
        identity = lane["lane_id"]
        reason = None
        if pinned_lane and identity != pinned_lane:
            reason = "not pinned lane"
        elif lane["provider"] != model["provider"]:
            reason = "different provider"
        elif not lane.get("enabled", True):
            reason = "disabled"
        elif lane.get("owner") != "v2":
            reason = "owner is not v2"
        elif lane.get("desktop", False) and not allow_desktop:
            reason = "desktop excluded"
        elif identity in excluded or lane.get("account_key") in excluded:
            reason = "excluded"
        else:
            for closure in closures:
                if (closure["lane_id"] == identity and closure["scope"] in ("account", model["id"])
                        and not closure.get("released_at") and _time(closure["until_at"]) > instant):
                    reason = f"closure:{closure['scope']}"
                    break
        if reason is None and (fleet_full or in_flight.get(identity, 0) >= lane_capacity(policy, identity, readings, now=instant)):
            reason = "capacity"
        if reason:
            rejections.append({"lane_id": identity, "reason": reason})
        else:
            candidates.append(identity)
    evaluation = {"model": short, "model_id": model["id"], "candidates": candidates,
                  "rejections": rejections, "readings": [_row(reading) for reading in readings], "closures": closures}
    reason = "first eligible lane" if candidates else ("capacity" if any(item["reason"] == "capacity" for item in rejections) else "no eligible lane")
    return Decision((short,), (evaluation,), candidates[0] if candidates else None,
                    short if candidates else None, reason, policy_digest)
