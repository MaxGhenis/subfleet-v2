"""Policy loading and the milestone-one lane pick (C-11.1, C-6.4).

This module intentionally evaluates one requested model. Full upward chains,
provider comparators, and probes belong to the subsequent routing milestone.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import DEFAULT_CAPS, PROVIDERS, READING_TTL_S, Closure, Decision, Lane, Reading

DEFAULT_POLICY_PATH = Path(__file__).with_name("default_policy.json")


def policy_hash(path: str | Path) -> str:
    """C-11.1: hash the policy file's exact bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_policy(path: str | Path) -> dict[str, Any]:
    """Read and validate the model map, task names, tier names, and admission caps."""
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {"tiers", "chains", "fallback", "permissions", "models", "retired", "desktop_login", "caps", "reset_credits"}
    if not isinstance(value, dict) or required - value.keys():
        raise ValueError("policy is missing required keys: " + ", ".join(sorted(required - value.keys() if isinstance(value, dict) else required)))
    tiers = value["tiers"]
    if not isinstance(tiers, list) or not tiers or any(not isinstance(tier, str) for tier in tiers) or len(set(tiers)) != len(tiers):
        raise ValueError("policy tiers must be distinct nonempty names")
    models = value["models"]
    if not isinstance(models, dict) or not models:
        raise ValueError("policy models must be a nonempty map")
    for name, model in models.items():
        if not isinstance(model, dict) or model.get("provider") not in PROVIDERS or not isinstance(model.get("id"), str) or not model["id"]:
            raise ValueError(f"invalid model {name!r}")
    if not isinstance(value["chains"], dict):
        raise ValueError("policy chains must map tasks to one model per tier")
    for task, chain in value["chains"].items():
        if not isinstance(chain, list) or len(chain) != len(tiers) or any(model not in models for model in chain):
            raise ValueError(f"invalid policy chain for {task!r}")
    if value["fallback"] != "upward-only" or value["desktop_login"] != "never":
        raise ValueError("policy requires upward-only fallback and desktop_login never")
    if not isinstance(value["permissions"], dict) or any(sandbox not in ("read-only", "workspace-write") for sandbox in value["permissions"].values()):
        raise ValueError("invalid policy permissions")
    if not isinstance(value["retired"], dict) or any(name not in models for name in value["retired"].values()):
        raise ValueError("invalid retired model alias")
    if not isinstance(value["caps"], dict):
        raise ValueError("policy caps must be a map")
    caps = {**DEFAULT_CAPS, "reading_ttl_s": READING_TTL_S, "max_tokens_observed": None, **value["caps"]}
    for name, cap in caps.items():
        if name == "max_tokens_observed" and cap is None:
            continue
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
            raise ValueError(f"policy cap {name!r} must be a positive integer")
    value["caps"] = caps
    return value


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
        short = policy.get("retired", {}).get(pinned_model, pinned_model)
        if short not in models:
            short = next((name for name, model in models.items() if model["id"] == pinned_model), "")
        if short not in models:
            raise ValueError(f"unknown pinned model {pinned_model!r}")
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
