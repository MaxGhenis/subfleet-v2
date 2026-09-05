"""Pure policy evaluation and queue admission rules (C-6.4, C-11.2–C-11.6).

The daemon supplies a capacity snapshot and reserves the chosen slot in its
admission transaction. Evaluation never probes, writes the store, or runs ps.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .capacity import fresh_provider, identity_blocked
from .contracts import DEFAULT_CAPS, HEADROOM_FLOOR, Decision, Exit
from .policy import PolicyError, resolve_model

ACTIVE_ATTEMPTS = frozenset({"reserved", "starting", "running", "finalizing"})


def _row(value: Any) -> dict[str, Any]:
    return asdict(value) if is_dataclass(value) else dict(value)


def _time(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _identities(lane: Mapping[str, Any]) -> set[str]:
    """Every name `-a` and `-x` may use for one lane (C-11.2, C-17.2).

    `label` is here because C-1.4 makes a verified Claude lane's account key a
    pair of uuids: without it an operator's `-a max@example.org` would stop
    resolving the moment a lane is re-enrolled with its identity bound.
    """
    account = str(lane.get("account_key") or "")
    return {str(value) for value in (lane.get("lane_id"), account,
            account.partition(":")[2], lane.get("label"), lane.get("email"),
            lane.get("home")) if value}


def resolve_lane(lanes: Iterable[Any], pin: str) -> dict[str, Any] | None:
    """C-11.2: resolve one identity pin without silently choosing another lane."""
    roster = [_row(lane) for lane in lanes]
    exact = next((lane for lane in roster if lane.get("lane_id") == pin), None)
    matches = [exact] if exact else [lane for lane in roster if pin in _identities(lane)]
    if len(matches) > 1:
        raise ValueError(f"pinned_lane: ambiguous lane {pin!r}; use a lane id")
    return matches[0] if matches else None


def ordered_jobs(policy: Mapping[str, Any], jobs: Iterable[Any]) -> list[dict[str, Any]]:
    """C-4.1, C-6.4; plan amendment 11: FIFO within each policy tier.

    Tiers follow the policy's declared order. Stable sorting preserves the
    store's submission order when second-precision timestamps are tied.
    """
    tiers = policy["tiers"]
    default = "standard" if "standard" in tiers else tiers[0]
    rank = {tier: index for index, tier in enumerate(tiers)}
    return sorted((_row(job) for job in jobs), key=lambda job: (
        rank.get(job.get("tier") or default, len(tiers)), job.get("created_at") or ""))


def _parent_blocks(policy: Mapping[str, Any], view: Mapping[str, Any], job: dict[str, Any]) -> list[str]:
    """All descendants of every ancestor share that ancestor's concurrency cap."""
    limit = policy.get("caps", {}).get("max_active_attempts_per_parent", 1)
    jobs = {_row(item)["job_id"]: _row(item) for item in view.get("jobs", ())}
    if job.get("job_id"):
        jobs[job["job_id"]] = job

    def ancestors(item: Mapping[str, Any]) -> set[str]:
        seen: set[str] = set()
        parent = item.get("parent_job_id")
        while parent and parent not in seen:
            seen.add(parent)
            parent = jobs.get(parent, {}).get("parent_job_id")
        return seen

    parents = ancestors(job)
    if not parents:
        return []
    counts = dict.fromkeys(parents, 0)
    for item in view.get("attempts", ()):
        attempt = _row(item)
        if attempt.get("state") in ACTIVE_ATTEMPTS:
            for parent in ancestors(jobs.get(attempt.get("job_id"), {})) & parents:
                counts[parent] += 1
    return [f"parent:{parent}" for parent in sorted(parents) if counts[parent] >= limit]


def _future_closure(closure: Mapping[str, Any], now: datetime) -> bool:
    return not closure.get("released_at") and _time(closure["until_at"]) > now


def _earliest_reset(evaluations: Iterable[Mapping[str, Any]], now: datetime) -> str | None:
    clocks: list[datetime] = []
    for evaluation in evaluations:
        for key, clock in (("closures", "until_at"), ("readings", "resets_at")):
            for evidence in evaluation.get(key, ()):
                if not evidence.get(clock):
                    continue  # Admission observations have no quota/reset clock.
                try:
                    instant = _time(evidence[clock])
                    if instant > now:
                        clocks.append(instant)
                except (KeyError, TypeError, ValueError):
                    continue
    return _iso(min(clocks)) if clocks else None


def evaluate(policy: Mapping[str, Any], view: Mapping[str, Any], job: Any) -> Decision:
    """C-11.2–C-11.6: walk upward, applying every rejection before comparison."""
    job = _row(job)
    now = _time(view["now"]) if view.get("now") else datetime.now(timezone.utc)
    lanes = sorted((_row(lane) for lane in view.get("lanes", ())), key=lambda lane: lane["lane_id"])
    readings = [_row(item) for item in view.get("readings", ())]
    closures = [_row(item) for item in view.get("closures", ())]
    caps = {**DEFAULT_CAPS, "reading_ttl_s": 120, **policy.get("caps", {})}
    floor = policy.get("headroom_floor", HEADROOM_FLOOR)
    excluded = job.get("exclusions") or ()
    excluded = set(json.loads(excluded) if isinstance(excluded, str) else excluded)
    pin = job.get("pinned_lane")
    selected = resolve_lane(lanes, pin) if pin else None
    task, tier = job.get("task"), job.get("tier")
    if task is not None and task not in policy["chains"]:
        raise ValueError(f"task: unknown task {task!r}")
    if tier is not None and tier not in policy["tiers"]:
        raise ValueError(f"tier: unknown tier {tier!r}")
    if job.get("pinned_model"):
        chain = [resolve_model(policy, job["pinned_model"])]
    elif task:
        default = "standard" if "standard" in policy["tiers"] else policy["tiers"][0]
        chain = policy["chains"][task][policy["tiers"].index(tier or default):]
    elif selected:
        model_name = next((name for name, model in policy["models"].items()
                           if model["provider"] == selected["provider"]), None)
        if model_name is None:
            raise PolicyError(policy.get("_policy_path", "policy.json"), "pinned_lane",
                              f"lane {pin!r} has provider {selected['provider']!r} with no models in policy")
        chain = [model_name]
    else:
        chain = [next(iter(policy["models"]))]
    if pin:
        chain = chain[:1]
        if selected and policy["models"][chain[0]]["provider"] != selected["provider"]:
            raise ValueError("pinned_lane and pinned_model/task: different providers")
    # Repeated tiers on Fable and Terra do not create another admission chance.
    chain = list(dict.fromkeys(chain))
    in_flight = dict(view.get("in_flight", {}))
    if "in_flight" not in view:
        for item in view.get("attempts", ()):
            attempt = _row(item)
            if attempt.get("state") in ACTIVE_ATTEMPTS:
                identity = attempt["lane_id"]
                in_flight[identity] = in_flight.get(identity, 0) + 1
    capacity_blocks = _parent_blocks(policy, view, job)
    if sum(in_flight.values()) + view.get("reserved_probes", 0) >= caps["max_active_attempts"]:
        capacity_blocks.append("fleet")
    evaluations: list[dict[str, Any]] = []
    messages: list[str] = []
    chosen_lane = chosen_model = None
    for index, short in enumerate(chain):
        model = policy["models"][short]
        model_lanes = [lane for lane in lanes if lane["provider"] == model["provider"]
                       and (not pin or selected and lane["lane_id"] == selected["lane_id"])]
        lane_ids = {lane["lane_id"] for lane in model_lanes}
        scoped_readings = [row for row in readings if row["lane_id"] in lane_ids
                           and row["scope"] in ("account", model["id"])]
        scoped_closures = [row for row in closures if row["lane_id"] in lane_ids
                          and row["scope"] in ("account", model["id"]) and _future_closure(row, now)]
        candidates, rejections, details = [], [], {}
        for lane in model_lanes:
            identity = lane["lane_id"]
            reasons = []
            lane_readings = [row for row in scoped_readings if row["lane_id"] == identity]
            measured_readings = [row for row in lane_readings
                                 if fresh_provider(row, now=now, reading_ttl_s=caps["reading_ttl_s"])]
            measured = bool(measured_readings)
            headroom = min((1 - row["utilization"] for row in measured_readings), default=None)
            resets = [_time(row["resets_at"]) for row in measured_readings
                      if row["window"] == "seven_day" and row.get("resets_at")]
            detail = {"measured": measured, "headroom": headroom,
                      "in_flight": in_flight.get(identity, 0),
                      "seven_day_reset": _iso(min(resets)) if resets else None,
                      "status": "eligible" if measured else "eligible but unmeasured"}
            if _identities(lane) & excluded:
                reasons.append("excluded")
            if lane.get("desktop") and not job.get("allow_desktop"):
                reasons.append("desktop")
            if lane.get("owner") != "v2":
                reasons.append("owner-v1")
            if not lane.get("enabled", True):
                reasons.append("disabled")
            if identity_blocked(lane):
                # C-10.6: the profile endpoint said this credential holds another
                # account. Its usage is not this lane's, so neither is its capacity.
                reasons.append("identity-mismatch")
            reasons.extend(f"closed:{row['scope']}:{row['until_at']}" for row in scoped_closures
                           if row["lane_id"] == identity)
            lane_measured = any(row["lane_id"] == identity and fresh_provider(
                row, now=now, reading_ttl_s=caps["reading_ttl_s"]) for row in readings)
            slot_cap = (caps["max_in_flight_per_lane"] if lane_measured else
                        min(caps["max_in_flight_per_lane"], caps["max_in_flight_unmeasured"], 1))
            if identity in view.get("unavailable_lanes", {}):
                detail["slot_block"] = view["unavailable_lanes"][identity]
            if capacity_blocks or in_flight.get(identity, 0) >= slot_cap or detail.get("slot_block"):
                reasons.append("no-slot")
            if any(row["utilization"] >= 1 - floor for row in measured_readings):
                reasons.append("below-floor")
            if reasons:
                rejections.append({"lane_id": identity, "reason": reasons[0], "reasons": reasons, **detail})
            else:
                candidates.append(identity)
                details[identity] = detail

        def comparator(identity: str) -> tuple:
            row = details[identity]
            if model["provider"] == "codex":
                return (not row["measured"], row["seven_day_reset"] or "9999", identity)
            return (not row["measured"], -(row["headroom"] or 0), row["in_flight"], identity)

        candidates.sort(key=comparator)
        if candidates:
            chosen_lane, chosen_model = candidates[0], short
            reason = f"{short}: chose {chosen_lane}; {details[chosen_lane]['status']}"
        else:
            suffix = "; promoted" if index + 1 < len(chain) else ""
            reason = f"{short}: no candidate lanes after exclusions{suffix}"
            if pin and selected is None:
                reason += f"; pinned lane {pin!r} is unknown"
        evaluations.append({"model": short, "model_id": model["id"], "provider": model["provider"],
                            "candidates": candidates, "candidate_details": details,
                            "rejections": rejections, "rejected": rejections,
                            "readings": scoped_readings,
                            "capacity_readings": [row for row in readings if row["lane_id"] in lane_ids],
                            "closures": scoped_closures, "capacity_blocks": list(capacity_blocks),
                            "reason": reason, "evaluated_at": _iso(now)})
        messages.append(reason)
        if candidates:
            break
    if chosen_lane is None:
        messages.append("earliest reset: " + (_earliest_reset(evaluations, now) or "unknown"))
    digest = job.get("policy_hash") or policy.get("_policy_hash", "")
    return Decision(tuple(row["model"] for row in evaluations), tuple(evaluations),
                    chosen_lane, chosen_model, "; ".join(messages), digest)


def probe_required(decision: Decision, job: Any) -> bool:
    """C-11.4: probe the requested model before expensive unmeasured work."""
    job = _row(job)
    expensive = job.get("sandbox") == "workspace-write" or job.get("tier") == "hard"
    if not decision.chosen_lane or not expensive:
        return False
    evaluation = next(row for row in decision.evaluations if row["model"] == decision.chosen_model)
    return not evaluation["candidate_details"][decision.chosen_lane]["measured"]


def exit_code(decision: Decision) -> Exit:
    """C-17.3: an evaluation without a candidate is NO_LANE, including pins."""
    return Exit.OK if decision.chosen_lane else Exit.NO_LANE


def waiting_metadata(decision: Decision, now: str | datetime | None = None) -> dict[str, str]:
    """C-4.1; plan amendment 11: capacity waits always have a recheck clock."""
    instant = _time(now) if now is not None else datetime.now(timezone.utc)
    next_check = instant + timedelta(seconds=1)
    earliest = _earliest_reset(decision.evaluations, instant)
    if earliest:
        next_check = min(next_check, _time(earliest))
    return {"wait_reason": "capacity", "next_check_at": _iso(next_check)}
