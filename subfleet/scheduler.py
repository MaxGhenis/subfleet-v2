"""Pure policy evaluation and queue admission rules (C-6.4, C-11.2–C-11.6).

The daemon supplies a capacity snapshot and reserves the chosen slot in its
admission transaction. Evaluation never probes, writes the store, or runs ps.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .capacity import fresh_provider, identity_blocked
from .contracts import (CAPACITY_RECHECK_BASE_S, CAPACITY_RECHECK_CEILING_S, DEFAULT_CAPS,
                        HEADROOM_FLOOR, Decision, Exit)
from . import host_pressure
from .policy import PolicyError, resolve_model

ACTIVE_ATTEMPTS = frozenset({"reserved", "starting", "running", "finalizing"})


def _row(value: Any) -> dict[str, Any]:
    return asdict(value) if is_dataclass(value) else dict(value)


def _time(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class RouteError(ValueError):
    """C-6.12: the job's own pin cannot be routed, whatever capacity does.

    Raised for a pin that names several lanes, a pin and a model of different
    providers, and an unmeasured-reserve authorization that is not whole: what
    submit refuses, and what no wait can fix, because only the caller can say
    what was meant. Admission refuses such a job with this message instead of
    abandoning its pass. `policy_dependent` marks the cases a policy edit can
    cause (a tier's chain moved to another provider): admission refuses those
    only under the policy the job was accepted with, and otherwise waits, as
    for any error that is not the job's. It is a ValueError, so a caller that
    caught the old ValueError still does.
    """

    def __init__(self, message: str, *, policy_dependent: bool = False):
        super().__init__(message)
        self.policy_dependent = policy_dependent


def _identities(lane: Mapping[str, Any]) -> set[str]:
    """Every name `-a` and `-x` may use for one lane (C-11.2, C-17.2).

    `label` is here because C-1.4 makes a verified Claude lane's account key a
    pair of uuids: without it an operator's `-a max@example.org` would stop
    resolving the moment a lane is re-enrolled with its identity bound.
    `email` is only in the capacity view: the Codex usage probe reports it and
    `Timers.enrich_view` adds it, so a pin resolved against store rows alone
    would miss it (C-11.2, the 2026-09-22 stall).
    """
    account = str(lane.get("account_key") or "")
    return {str(value) for value in (lane.get("lane_id"), account,
            account.partition(":")[2], lane.get("label"), lane.get("email"),
            lane.get("home")) if value}


def _binding(lane: Mapping[str, Any]) -> tuple[Any, Any]:
    """The credential a lane is bound to; a re-enrolment keeps it (`Timers.enrich_view`)."""
    return lane.get("provider"), lane.get("home") or lane.get("credential_ref")


def resolve_lane(lanes: Iterable[Any], pin: str, provider: str | None = None, *,
                 follow: bool = True) -> dict[str, Any] | None:
    """C-11.2: resolve one identity pin without silently choosing another lane.

    A lane id is exact, except that a disabled lane whose credential an enabled
    lane now holds (a re-enrolment, C-10.2) resolves to that lane, the same
    account on the same credential, unless `follow` is false (an authorization
    is bound to the lane id it was granted for, C-11.7). Any other name can
    match more than one lane: a Claude login and a Codex subscription often
    share an email, and a re-enrolment leaves the old binding in the roster
    under the same label. Two narrowings drop lanes that could never take the
    job, never one that could: `provider` (the job's, from `pin_provider`)
    drops lanes of the other provider, a disabled binding is dropped when an
    enabled lane that matches is bound to the same credential, and a lane whose
    credential proved to hold another account (C-10.6) is dropped when another
    lane of the same provider also matches. A name that still matches several lanes raises
    `RouteError` naming them. None means the pin names no lane.
    """
    roster = [_row(lane) for lane in lanes]
    exact = next((lane for lane in roster if lane.get("lane_id") == pin), None)
    if exact:
        if follow and not exact.get("enabled", True):
            return next((lane for lane in roster if lane.get("enabled", True)
                         and _binding(lane) == _binding(exact)), exact)
        return exact
    matches = [lane for lane in roster if pin in _identities(lane)]
    if len(matches) > 1 and provider:
        matches = [lane for lane in matches if lane.get("provider") == provider] or matches
    if len(matches) > 1:
        live = {_binding(lane) for lane in matches if lane.get("enabled", True)}
        matches = [lane for lane in matches if lane.get("enabled", True) or _binding(lane) not in live]
    if len(matches) > 1 and len({lane.get("provider") for lane in matches}) == 1:
        # C-10.6: a credential that proved to hold another account never takes a
        # job again. Only a tie within one provider: across providers the name is
        # still ambiguous, and dropping one side would pick the provider.
        matches = [lane for lane in matches if not identity_blocked(lane)] or matches
    if len(matches) > 1:
        names = ", ".join(sorted(str(lane["lane_id"]) for lane in matches))
        raise RouteError(f"pinned_lane: {pin!r} names {len(matches)} lanes ({names}); "
                         f"pin one of them by its lane id")
    return matches[0] if matches else None


def current_lane_id(lanes: Iterable[Any], lane_id: str, *, follow: bool = True) -> str:
    """C-4.5, C-11.2: the lane an attempt's lane id names now, as a pin to it resolves.

    A re-enrolment gives the same account on the same credential a new lane
    id; attempts on the old id and on its successor are attempts on one lane
    when retries and exclusions are counted.
    """
    try:
        found = resolve_lane(lanes, lane_id, follow=follow)
    except ValueError:
        found = None
    return str(found["lane_id"]) if found else lane_id


def pin_provider(policy: Mapping[str, Any], job: Any) -> str | None:
    """C-11.2: the provider a lane-pinned job must run on, as `evaluate` decides it.

    A pinned job evaluates one model: its pinned model, else the first model of
    its task's chain from its tier. None when the job names neither, so only
    the lane can say, or when the policy cannot tell (`evaluate` reports that).
    """
    job = _row(job)
    try:
        if job.get("pinned_model"):
            return policy["models"][resolve_model(policy, job["pinned_model"], note=False)]["provider"]
        task = job.get("task")
        if task in policy["chains"]:
            tiers = policy["tiers"]
            tier = job.get("tier") or ("standard" if "standard" in tiers else tiers[0])
            return policy["models"][policy["chains"][task][tiers.index(tier)]]["provider"]
    except (PolicyError, ValueError, KeyError, IndexError):
        pass
    return None


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


def demand_models(policy: Mapping[str, Any], job: Any) -> frozenset[str] | None:
    """The models a job could run on, exactly as `evaluate` builds its chain (C-11.2).

    A pin is that one model; a task is its chain from the job's tier upward. None
    means "cannot tell" (a lane pin with no model), which admission treats as
    competing with everything.
    """
    job = _row(job)
    try:
        if job.get("pinned_model"):
            return frozenset({resolve_model(policy, job["pinned_model"], note=False)})
        task = job.get("task")
        if task in policy["chains"]:
            tiers = policy["tiers"]
            default = "standard" if "standard" in tiers else tiers[0]
            return frozenset(policy["chains"][task][tiers.index(job.get("tier") or default):])
    except (PolicyError, ValueError, KeyError):
        pass
    return None


def demand_lanes(lanes: Iterable[Any], job: Any,
                 policy: Mapping[str, Any] | None = None) -> frozenset[str] | None:
    """The lanes a job could run on: its pin, resolved to one lane id (C-11.2).

    Resolved as `evaluate` resolves it when `policy` is given (the job's
    provider narrows the name). None means any lane, or a pin that cannot be
    resolved here, which admission treats as competing with everything.
    """
    pin = _row(job).get("pinned_lane")
    if not pin:
        return None
    try:
        found = resolve_lane(lanes, pin, pin_provider(policy, job) if policy else None,
                             follow=not _row(job).get("unmeasured_reserve_reason"))
    except ValueError:
        return None
    return frozenset({found["lane_id"]}) if found else None


def competes(models: frozenset[str] | None, other: frozenset[str] | None,
             lanes: frozenset[str] | None = None, other_lanes: frozenset[str] | None = None) -> bool:
    """C-6.9: two jobs compete when some model could serve both AND some lane could
    serve both; either side unknown counts as overlap. Two jobs pinned to
    different lanes never compete: neither can take a slot the other waits for."""
    if models is not None and other is not None and not models & other:
        return False
    return lanes is None or other_lanes is None or bool(lanes & other_lanes)


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


def _higher_model_scopes(policy: Mapping[str, Any], short: str) -> set[str]:
    """C-23.37: stronger models come from policy, never provider name guesses.

    Explicit priorities compare models across separate task chains (a writing
    chain and the general-work chain; until 2026-09-27 Fable's). Older policies
    still express ordering within their upward-only chains.
    """
    model = policy["models"][short]
    higher = set()
    for chain in policy["chains"].values():
        if short in chain:
            higher.update(chain[chain.index(short) + 1:])
    priority = model.get("priority")
    if priority is not None:
        higher.update(name for name, other in policy["models"].items()
                      if other.get("priority", -1) > priority)
    return {policy["models"][name]["id"] for name in higher if name != short
            and policy["models"][name]["provider"] == model["provider"]}


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


def _unmeasured_reserve_reason(job: Mapping[str, Any]) -> str | None:
    """Validate the explicit exception independently of socket input validation."""
    reason = job.get("unmeasured_reserve_reason")
    if reason is None:
        return None
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise RouteError("unmeasured_reserve_reason: provide a nonblank reason of at most 2000 characters")
    if any(not isinstance(job.get(key), str) or not job[key].strip()
           for key in ("pinned_lane", "pinned_model")):
        raise RouteError("unmeasured_reserve_reason: explicit pinned_lane and pinned_model are required")
    return reason.strip()


def host_pressure_hold(policy: Mapping[str, Any], view: Mapping[str, Any], active: int) -> dict[str, Any] | None:
    """C-6.15: what holds a new attempt while the host's memory is under pressure, or None.

    Only a policy that switches `host_pressure.enabled` on holds anything, only
    on a reading the view carries, and only while `active` attempts are in
    flight: with none, nothing this fleet started is adding to the pressure and
    nothing it will finish can relieve it, so the next job always starts. A host
    that cannot be read holds nothing.
    """
    settings = host_pressure.settings(policy)
    reading = view.get("host_pressure")
    if not settings["enabled"] or active <= 0 or not reading:
        return None
    occupied = reading.get("compressor_bytes")
    limit = settings["compressor_max_gib"]
    if occupied is None or occupied <= limit * host_pressure.GIB:
        return None
    return {"compressor_gib": round(occupied / host_pressure.GIB, 1), "compressor_max_gib": limit,
            "active_attempts": active}


def host_pressure_evidence(decision: Decision | Mapping[str, Any] | None) -> dict[str, Any]:
    """C-6.11: the reading a `host-pressure` hold reports, from the decision that met it."""
    if decision is None:
        return {}
    for evaluation in _row(decision).get("evaluations", ()):
        if evaluation.get("host_pressure"):
            return {key: evaluation["host_pressure"][key] for key in ("compressor_gib", "compressor_max_gib")}
    return {}


def evaluate(policy: Mapping[str, Any], view: Mapping[str, Any], job: Any) -> Decision:
    """C-11.2–C-11.6: walk upward, applying every rejection before comparison."""
    job = _row(job)
    authorization_reason = _unmeasured_reserve_reason(job)
    now = _time(view["now"]) if view.get("now") else datetime.now(timezone.utc)
    lanes = sorted((_row(lane) for lane in view.get("lanes", ())), key=lambda lane: lane["lane_id"])
    readings = [_row(item) for item in view.get("readings", ())]
    closures = [_row(item) for item in view.get("closures", ())]
    caps = {**DEFAULT_CAPS, "reading_ttl_s": 120, **policy.get("caps", {})}
    floor = policy.get("headroom_floor", HEADROOM_FLOOR)
    excluded = job.get("exclusions") or ()
    excluded = set(json.loads(excluded) if isinstance(excluded, str) else excluded)
    pin = job.get("pinned_lane")
    task, tier = job.get("task"), job.get("tier")
    # An unknown task, tier, or model is the policy's to fix, not the job's: submit
    # checked each against the policy it ran under (C-6.12).
    if task is not None and task not in policy["chains"]:
        raise ValueError(f"task: unknown task {task!r}")
    if tier is not None and tier not in policy["tiers"]:
        raise ValueError(f"tier: unknown tier {tier!r}")
    if job.get("pinned_model"):
        chain = [resolve_model(policy, job["pinned_model"])]
    elif task:
        default = "standard" if "standard" in policy["tiers"] else policy["tiers"][0]
        chain = policy["chains"][task][policy["tiers"].index(tier or default):]
    else:
        chain = []
    # C-11.2: a pinned job evaluates one model, the first of its chain, so a lane
    # of any other provider could never take it (`pin_provider` says the same).
    selected = (resolve_lane(lanes, pin, policy["models"][chain[0]]["provider"] if chain else None,
                             follow=not authorization_reason) if pin else None)
    if authorization_reason and selected and pin != selected["lane_id"]:
        raise RouteError("unmeasured_reserve_reason: pinned_lane must be the canonical lane id")
    if not chain and selected:
        model_name = next((name for name, model in policy["models"].items()
                           if model["provider"] == selected["provider"]), None)
        if model_name is None:
            raise PolicyError(policy.get("_policy_path", "policy.json"), "pinned_lane",
                              f"lane {pin!r} has provider {selected['provider']!r} with no models in policy")
        chain = [model_name]
    elif not chain:
        chain = [next(iter(policy["models"]))]
    if pin:
        chain = chain[:1]
        if selected and policy["models"][chain[0]]["provider"] != selected["provider"]:
            raise RouteError("pinned_lane and pinned_model/task: different providers", policy_dependent=True)
    # Repeated tiers (the one-model writing chains, Terra) do not create another
    # admission chance.
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
    pressure = host_pressure_hold(policy, view, sum(in_flight.values()))
    if pressure:
        capacity_blocks.append("host-pressure")
    evaluations: list[dict[str, Any]] = []
    messages: list[str] = []
    chosen_lane = chosen_model = None
    for index, short in enumerate(chain):
        model = policy["models"][short]
        higher_scopes = _higher_model_scopes(policy, short)
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
            if model["provider"] == "claude":
                detail["stranded_scopes"] = sorted({row["scope"] for row in closures
                    if row["lane_id"] == identity and row["scope"] in higher_scopes
                    and _future_closure(row, now)})
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
            # C-11.7: a model that is not reserved may only spend a lane's slack above
            # what the reserved model could still use of the shared weekly window.
            for reserved in (policy.get("reserve") or {}).get("models", ()):
                r_model = policy["models"].get(reserved)
                if not r_model or r_model["provider"] != model["provider"] or r_model["id"] == model["id"]:
                    continue
                verdict = reserve_verdict(identity, r_model["id"], readings, now=now,
                                          reading_ttl_s=caps["reading_ttl_s"],
                                          reserve=policy.get("reserve") or {}, closures=closures)
                detail["reserve"] = {"model": reserved, **verdict}
                if verdict["state"] == "unmeasured" and authorization_reason:
                    # This authorizes uncertainty on the explicit pinned pair;
                    # it supplies no usage evidence and releases no other guard.
                    detail["reserve"]["authorization"] = {
                        "reason": authorization_reason, "lane_id": identity, "model_id": model["id"]}
                elif verdict["state"] != "slack":
                    reasons.append(f"reserve:{reserved}:{verdict['state']}")
            if reasons:
                rejections.append({"lane_id": identity, "reason": reasons[0], "reasons": reasons, **detail})
            else:
                candidates.append(identity)
                details[identity] = detail

        def comparator(identity: str) -> tuple:
            row = details[identity]
            if model["provider"] == "codex":
                return (not row["measured"], row["seven_day_reset"] or "9999", identity)
            reserve = row.get("reserve") or {}
            stranded = bool(row.get("stranded_scopes"))
            if reserve.get("slack") is not None:
                # C-11.7: non-reserved work lands where the reserved bucket is most spent.
                return (not stranded, not row["measured"], -reserve["slack"], row["in_flight"], identity)
            return (not stranded, not row["measured"], -(row["headroom"] or 0), row["in_flight"], identity)

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
                            **({"host_pressure": pressure} if pressure else {}),
                            "stranding_closures": [row for row in closures if row["lane_id"] in lane_ids
                                and row["scope"] in higher_scopes and _future_closure(row, now)],
                            "reason": reason, "evaluated_at": _iso(now)})
        messages.append(reason)
        if candidates:
            break
    if chosen_lane is None:
        messages.append("earliest reset: " + (_earliest_reset(evaluations, now) or "unknown"))
    digest = job.get("policy_hash") or policy.get("_policy_hash", "")
    return Decision(tuple(row["model"] for row in evaluations), tuple(evaluations),
                    chosen_lane, chosen_model, "; ".join(messages), digest)


def reserve_verdict(identity: str, reserved_id: str, readings: Iterable[Mapping[str, Any]], *,
                    now: datetime, reading_ttl_s: int, reserve: Mapping[str, Any],
                    closures: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """C-11.7: how much of a lane's shared weekly window a non-reserved model may spend.

    Reads fresh `provider` readings of window `seven_day`: a complete OAuth
    usage snapshot, or shared and scoped windows from the same stream event.
    A reported, active model-only closure can instead establish that no reserve
    is usable until reset, but requires an admission probe for the requested model.
    slack = (1 - shared utilization)
    - cap_ratio * (1 - reserved utilization). States: `unmeasured` (no fresh
    usage read of the shared window), `reserved` (slack below `min_slack`),
    `slack` (may spend `slack`; a usage read that shows no reserved window makes
    the whole remainder slack).
    """
    ratio = float(reserve.get("cap_ratio", 1.0))
    min_slack = float(reserve.get("min_slack", 0.05))
    readings = list(readings)
    fresh = [row for row in readings if row["lane_id"] == identity and row.get("window") == "seven_day"
             and fresh_provider(row, now=now, reading_ttl_s=reading_ttl_s)]
    # A stream can measure reserve slack only when it actually carries both
    # weekly buckets in the same event. Its missing scoped bucket is unknown,
    # unlike absence in a validated complete OAuth usage snapshot.
    accounts = [row for row in fresh if row["scope"] == "account" and (
        row.get("source") == "oauth-usage" or
        row.get("source") == "rate_limit_event" and any(
            scoped["scope"] == reserved_id and scoped.get("source") == "rate_limit_event"
            and scoped["observed_at"] == row["observed_at"]
            and scoped.get("attempt_id") == row.get("attempt_id") for scoped in fresh))]
    account = max(accounts, key=lambda row: _time(row["observed_at"]), default=None)
    exhausted = [row for row in closures if row["lane_id"] == identity
                 and row["scope"] == reserved_id and row.get("reason") == "provider-limit"
                 and row.get("clock_source") == "reported" and _future_closure(row, now)]
    if exhausted and account is None:
        # A provider-reported model-only exhaustion protects no usable reserve
        # before its reset. This says nothing about the other model's capacity:
        # require its own supervised admission probe before dispatch.
        return {"state": "slack", "reserved_remaining": 0.0,
                "all_remaining": None,
                "requires_probe": True, "cap_ratio": ratio, "min_slack": min_slack,
                "note": "reserved model is closed by the provider until its reset",
                "closure_until": max(row["until_at"] for row in exhausted)}
    if account is None:
        return {"state": "unmeasured", "cap_ratio": ratio, "min_slack": min_slack}
    all_remaining = round(1 - float(account["utilization"]), 4)
    scoped_evidence = [row for row in readings if row["lane_id"] == identity
                       and row.get("window") == "seven_day" and row["scope"] == reserved_id
                       and _time(row["observed_at"]) >= _time(account["observed_at"])]
    scoped = next((row for row in scoped_evidence if row in fresh
                   and row.get("source") == account.get("source")
                   and (account.get("source") != "rate_limit_event" or (
                       row["observed_at"] == account["observed_at"]
                       and row.get("attempt_id") == account.get("attempt_id")))), None)
    if scoped is None and scoped_evidence:
        # Same-snapshot or newer uncertain evidence is not absence. An older
        # scoped row, however, is superseded by a newer complete usage payload.
        # The sensor rejects incomplete snapshots before publishing readings.
        return {"state": "unmeasured", "cap_ratio": ratio, "min_slack": min_slack,
                "note": "reserved window has no fresh usage reading"}
    if scoped is None:
        return {"state": "slack", "all_remaining": all_remaining, "reserved_remaining": None,
                "slack": all_remaining, "cap_ratio": ratio, "min_slack": min_slack,
                "note": "no reserved window on this account"}
    reserved_remaining = round(1 - float(scoped["utilization"]), 4)
    slack = round(all_remaining - ratio * reserved_remaining, 4)
    return {"state": "slack" if slack >= min_slack else "reserved", "all_remaining": all_remaining,
            "reserved_remaining": reserved_remaining, "slack": slack, "cap_ratio": ratio,
            "min_slack": min_slack}


def probe_required(decision: Decision, job: Any) -> bool:
    """C-11.4: probe the requested model before expensive unmeasured work.

    C-23.20 makes a revive stricter than expensive: it admits a lane only on a
    `provider` reading taken in the same pass, so a stored reading — even a
    fresh one inside `reading_ttl_s` — never qualifies the lane on its own. The
    2026-08-25 observation is the reason: three lanes the ledger called healthy
    were out of Fable, and a revive that lands on one burns the window with no
    reader. `_prepare_route` converges after one probe because the approved pair
    short-circuits the loop.
    """
    job = _row(job)
    authorization_reason = _unmeasured_reserve_reason(job)
    if not decision.chosen_lane:
        return False
    if authorization_reason:
        evaluation = next((row for row in decision.evaluations
                           if row["model"] == decision.chosen_model), None)
        if (decision.chosen_lane != job["pinned_lane"] or evaluation is None
                or job["pinned_model"] not in (decision.chosen_model, evaluation["model_id"])):
            raise RouteError("unmeasured_reserve_reason: the probe must use the authorized lane and model",
                             policy_dependent=True)
        # An admission observation or a newly measured window cannot remove
        # the promised same-model probe. The daemon's approved pair ends it.
        return True
    if job.get("kind") == "revive":
        return True
    evaluation = next(row for row in decision.evaluations if row["model"] == decision.chosen_model)
    if (evaluation["candidate_details"][decision.chosen_lane].get("reserve") or {}).get("requires_probe"):
        return True
    if not (job.get("sandbox") == "workspace-write" or job.get("tier") == "hard"):
        return False
    return not evaluation["candidate_details"][decision.chosen_lane]["measured"]


def exit_code(decision: Decision) -> Exit:
    """C-17.3: an evaluation without a candidate is NO_LANE, including pins."""
    return Exit.OK if decision.chosen_lane else Exit.NO_LANE


def capacity_recheck_delay(rechecks: int, ceiling_s: float = CAPACITY_RECHECK_CEILING_S) -> float:
    """C-6.10: seconds until the next look at a job whose verdict has held `rechecks` times."""
    # The exponent is bounded so a job that waits for days cannot overflow a float.
    return min(ceiling_s, CAPACITY_RECHECK_BASE_S * 2 ** min(max(rechecks, 0), 16))


def waiting_metadata(decision: Decision, now: str | datetime | None = None, *,
                     rechecks: int = 0, ceiling_s: float = CAPACITY_RECHECK_CEILING_S) -> dict[str, str]:
    """C-4.1; plan amendment 11: capacity waits always have a recheck clock.

    C-6.10: the clock backs off while the verdict repeats, and a known reset
    that falls sooner than the backed-off clock is still checked on time.
    """
    instant = _time(now) if now is not None else datetime.now(timezone.utc)
    next_check = instant + timedelta(seconds=capacity_recheck_delay(rechecks, ceiling_s))
    earliest = _earliest_reset(decision.evaluations, instant)
    if earliest:
        next_check = min(next_check, _time(earliest))
    return {"wait_reason": "capacity", "next_check_at": _iso(next_check)}


def verdict_signature(decision: Decision | Mapping[str, Any]) -> str:
    """C-6.10: what a decision concluded, without the evidence it consulted.

    Two evaluations with the same signature walked the same chain, chose the
    same lane and model, and rejected the same lanes for the same reasons.
    Readings, their utilization, and `evaluated_at` change every probe cycle
    without changing the verdict, so they are left out. So is `no-slot` on a
    lane that is rejected for another reason as well: a lane that is closed,
    reserved, or excluded is no nearer to taking the job when its slot empties.
    Slots fill and empty all day, and a probe's reservation counts toward the
    fleet cap, so each probe cycle (C-18.1) marks every lane `no-slot` for the
    second it runs. A lane whose only reason is `no-slot` keeps it: that job is
    waiting for a slot, and that is its verdict.
    """
    value = _row(decision)
    walked = []
    for evaluation in value.get("evaluations", ()):
        rejections = []
        for row in evaluation.get("rejections", ()):
            reasons = list(row.get("reasons") or [row.get("reason")])
            standing = [reason for reason in reasons if reason != "no-slot"]
            rejections.append((str(row.get("lane_id")), tuple(standing or reasons)))
        rejections.sort()
        candidates = sorted(row if isinstance(row, str) else str(row.get("lane_id"))
                            for row in evaluation.get("candidates", ()))
        walked.append([evaluation.get("model"), candidates, rejections])
    material = [list(value.get("chain", ())), value.get("chosen_lane"), value.get("chosen_model"),
                value.get("policy_hash"), walked]
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


def dominant_rejection(decision: Decision | Mapping[str, Any] | None) -> str:
    """C-6.11: what keeps a job out of every lane, as one short label.

    A lane rejected only for `no-slot` would take the job if it had room, so
    room is the cause: `fleet-full` when the fleet cap made it so, `parent-cap`
    for a parent's, `host-pressure` when only the host's memory holds it
    (C-6.15), else `no-slot`. When every lane has a standing reason the
    label is the commonest of those, and the cap is beside the point: a probe's
    reservation counts toward the fleet cap, so a job that no lane admits anyway
    would otherwise read `fleet-full` for the second each probe runs.
    """
    if decision is None:
        return "not-evaluated"
    value = _row(decision)
    blocks = [block for evaluation in value.get("evaluations", ())
              for block in evaluation.get("capacity_blocks", ())]
    counts: dict[str, int] = {}
    room_only = False
    for evaluation in value.get("evaluations", ()):
        for row in evaluation.get("rejections", ()):
            reasons = [str(reason) for reason in (row.get("reasons") or [row.get("reason") or "unknown"])]
            standing = [reason for reason in reasons if reason != "no-slot"]
            if not standing:
                room_only = True
                continue
            # A closure's reason carries its own expiry; the label groups them.
            label = ":".join(standing[0].split(":")[:2]) if standing[0].startswith("closed:") else standing[0]
            counts[label] = counts.get(label, 0) + 1
    if room_only:
        if "fleet" in blocks:
            return "fleet-full"
        if any(str(block).startswith("parent:") for block in blocks):
            return "parent-cap"
        return "host-pressure" if "host-pressure" in blocks else "no-slot"
    if not counts:
        return "no-lanes"
    return max(sorted(counts), key=lambda label: counts[label])
