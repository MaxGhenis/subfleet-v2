"""`scheduler.evaluate` as it was at e053b2c, before it was split into `prepare`,
`judge_lane` and `rank_key` (C-6.3). Kept verbatim as the reference the split is
tested against (`tests/unit/test_route_check.py`); nothing else uses it."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from subfleet.capacity import fresh_provider, identity_blocked
from subfleet.contracts import DEFAULT_CAPS, HEADROOM_FLOOR, Decision
from subfleet.policy import PolicyError, resolve_model
from subfleet.scheduler import (ACTIVE_ATTEMPTS, RouteError, _earliest_reset, _future_closure,
                                _higher_model_scopes, _identities, _iso, _parent_blocks, _row, _time,
                                _unmeasured_reserve_reason, reserve_verdict, resolve_lane)


def reference_evaluate(policy: Mapping[str, Any], view: Mapping[str, Any], job: Any) -> Decision:
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
    # Repeated tiers on Fable and Terra do not create another admission chance.
    chain = list(dict.fromkeys(chain))
    # C-26.9: a conversation turn has its own capacity, counted apart from
    # detached jobs: `conversations.max_active_turns` across the fleet and
    # `conversations.turn_slots_per_lane` per lane. Neither kind waits for the
    # other's slots; an attended turn never waits behind background work.
    is_turn = _row(job).get("kind") == "turn"
    conversation_caps = policy.get("conversations") or {}
    key = "in_flight_turns" if is_turn else "in_flight"
    in_flight = dict(view.get(key, {}))
    if key not in view:
        turn_jobs = {_row(item).get("job_id") for item in view.get("jobs", ()) if _row(item).get("kind") == "turn"}
        for item in view.get("attempts", ()):
            attempt = _row(item)
            if attempt.get("state") in ACTIVE_ATTEMPTS and (attempt.get("job_id") in turn_jobs) == is_turn:
                identity = attempt["lane_id"]
                in_flight[identity] = in_flight.get(identity, 0) + 1
    capacity_blocks = _parent_blocks(policy, view, job)
    if is_turn:
        if sum(in_flight.values()) >= int(conversation_caps.get("max_active_turns", 3)):
            capacity_blocks.append("fleet")
    elif sum(in_flight.values()) + view.get("reserved_probes", 0) >= caps["max_active_attempts"]:
        capacity_blocks.append("fleet")
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
            if (job.get("kind") == "turn" and model["provider"] == "claude"
                    and lane.get("credential_kind") == "home"):
                # C-26.2: a home lane has its own config directory; the
                # conversation's transcript is not there.
                reasons.append("config-dir")
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
            slot_cap = (int(conversation_caps.get("turn_slots_per_lane", 1)) if is_turn else
                        caps["max_in_flight_per_lane"] if lane_measured else
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
                elif job.get("kind") == "turn" and verdict.get("requires_probe"):
                    # C-26.9: a turn never waits on a probe; a lane that needs one
                    # is not a candidate for it.
                    reasons.append(f"reserve:{reserved}:probe-required")
            if reasons:
                rejections.append({"lane_id": identity, "reason": reasons[0], "reasons": reasons, **detail})
            else:
                candidates.append(identity)
                details[identity] = detail

        affinity = job.get("affinity_lane") if job.get("kind") == "turn" else None

        def comparator(identity: str) -> tuple:
            row = details[identity]
            if affinity is not None:
                # C-26.2: a conversation keeps the account that served its last
                # turn while that account stays a candidate (prompt cache).
                return (identity != affinity, *base_comparator(identity, row))
            return base_comparator(identity, row)

        def base_comparator(identity: str, row: dict) -> tuple:
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


