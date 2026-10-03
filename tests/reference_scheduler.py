"""`scheduler.evaluate` as it was at e053b2c, before it was split into `prepare`,
`judge_lane` and `rank_key` (C-6.3). Kept as the reference the split is tested
against (`tests/unit/test_scheduler_split.py`); nothing else uses it.

It changes only where the contract does, and then in its own words, never by
calling the code under test: null caps (C-26.9, then C-6.4 on 2026-09-27), the
load band and the desktop lane's place (C-11.3, C-10.3), then common weekly
expiry routing and reserve preferences (2026-10-03), each written here again
so the differential test compares two implementations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from subfleet.capacity import fresh_provider, identity_blocked
from subfleet.contracts import DEFAULT_CAPS, HEADROOM_FLOOR, Decision
from subfleet.policy import PolicyError, resolve_model
from subfleet.scheduler import (ACTIVE_ATTEMPTS, RouteError, _earliest_reset, _future_closure,
                                _higher_model_scopes, _identities, _iso, _row, _time,
                                _unmeasured_reserve_reason, reserve_verdict, resolve_lane)


def reference_weekly_details(rows, now, ttl, admission):
    """C-11.3's ranking evidence, written independently of `ranking_usage`.

    Admission below still uses its original fresh readings. A renewed window
    makes the ranking uncertain; it does not fabricate a refreshed measurement
    or introduce any admission refusal.
    """
    newest = {}
    for row in rows:
        if row.get("label") in ("provider", "stale-provider") and row.get("utilization") is not None:
            pair = row["scope"], row["window"]
            if pair not in newest or _time(newest[pair]["observed_at"]) < _time(row["observed_at"]):
                newest[pair] = row
    renewed = any(_time(row["resets_at"]) <= now or _time(row["resets_at"]) <= _time(row["observed_at"])
                  for row in newest.values() if row.get("resets_at"))
    current = [row for row in newest.values()
               if not renewed and fresh_provider(row, now=now, reading_ttl_s=ttl)]
    weekly_rows = [row for row in current if row["window"] == "seven_day"]
    weekly_rows.sort(key=lambda row: (1 - row["utilization"],
                                     _iso(_time(row["resets_at"])) if row.get("resets_at") else "9999",
                                     row["scope"]))
    binding = weekly_rows[0] if weekly_rows else None
    weekly_room = 1 - binding["utilization"] if binding else None
    primary_room = min([1 - row["utilization"] for row in current if row["window"] == "five_hour"],
                       default=None)
    low_weekly = binding is not None and binding["utilization"] > 1 - admission.get("weekly_reserve", .02)
    low_primary = any(row["utilization"] > 1 - admission.get("five_hour_reserve", .10)
                      for row in current if row["window"] == "five_hour")
    classes = {(False, False): "clear", (False, True): "five-hour",
               (True, False): "weekly", (True, True): "weekly+five-hour"}
    oldest = min((_time(row["observed_at"]) for row in (current or list(newest.values()))), default=None)
    return {"measured": bool(current), "weekly_headroom": weekly_room, "five_hour_headroom": primary_room,
            "seven_day_reset": _iso(_time(binding["resets_at"])) if binding and binding.get("resets_at") else None,
            "weekly_scope": binding["scope"] if binding else None,
            "weekly_reserve": low_weekly, "five_hour_reserve": low_primary,
            "reserve_class": classes[low_weekly, low_primary] if current else "unmeasured",
            "reading_observed_at": _iso(oldest) if oldest else None, "reading_renewed": renewed}


def reference_parent_blocks(policy: Mapping[str, Any], view: Mapping[str, Any], job: dict[str, Any]) -> list[str]:
    """Every ancestor whose descendants have as many attempts in flight as the
    parent cap allows; no cap (null or absent) blocks nothing."""
    limit = (policy.get("caps") or {}).get("max_active_attempts_per_parent")
    if limit is None:
        return []
    jobs = {_row(item)["job_id"]: _row(item) for item in view.get("jobs", ())}
    if job.get("job_id"):
        jobs[job["job_id"]] = job

    def ancestors(item: Mapping[str, Any]) -> list[str]:
        found: list[str] = []
        parent = item.get("parent_job_id")
        while parent and parent not in found:
            found.append(parent)
            parent = jobs.get(parent, {}).get("parent_job_id")
        return found

    mine = ancestors(job)
    counts = {parent: 0 for parent in mine}
    for item in view.get("attempts", ()):
        attempt = _row(item)
        if attempt.get("state") not in ACTIVE_ATTEMPTS:
            continue
        for parent in ancestors(jobs.get(attempt.get("job_id"), {})):
            if parent in counts:
                counts[parent] += 1
    return [f"parent:{parent}" for parent in sorted(counts) if counts[parent] >= int(limit)]


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
    # `conversations.turn_slots_per_lane` per lane, where a missing key or null
    # is no cap. Neither kind waits for the other's slots; an attended turn never
    # waits behind background work.
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
    capacity_blocks = reference_parent_blocks(policy, view, job)
    if is_turn:
        fleet_turns = conversation_caps.get("max_active_turns")
        if fleet_turns is not None and sum(in_flight.values()) >= int(fleet_turns):
            capacity_blocks.append("fleet")
    elif (caps.get("max_active_attempts") is not None
          and sum(in_flight.values()) + view.get("reserved_probes", 0) >= int(caps["max_active_attempts"])):
        capacity_blocks.append("fleet")
    # C-11.3: the load band's width; the default is 2, and null is no bands.
    spread = (policy.get("admission") or {}).get("lane_spread", 2)
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
            headroom = min((1 - row["utilization"] for row in measured_readings), default=None)
            detail = {**reference_weekly_details(lane_readings, now, caps["reading_ttl_s"],
                                                policy.get("admission") or {}),
                      "headroom": headroom, "in_flight": in_flight.get(identity, 0)}
            detail["status"] = "eligible" if detail["measured"] else "eligible but unmeasured"
            if model["provider"] == "claude":
                detail["stranded_scopes"] = sorted({row["scope"] for row in closures
                    if row["lane_id"] == identity and row["scope"] in higher_scopes
                    and _future_closure(row, now)})
            if lane.get("desktop"):
                detail["desktop"] = True
            if _identities(lane) & excluded:
                reasons.append("excluded")
            # C-10.3: the desktop login's lane is refused while Claude Code uses it,
            # and while nobody said whether it does.
            in_use = lane.get("desktop_in_use")
            if lane.get("desktop") and in_use is not False and not job.get("allow_desktop"):
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
            lane_turns = conversation_caps.get("turn_slots_per_lane")
            per_lane, unmeasured = caps.get("max_in_flight_per_lane"), caps.get("max_in_flight_unmeasured")
            if is_turn:
                slot_cap = None if lane_turns is None else int(lane_turns)
            elif lane_measured:
                slot_cap = per_lane
            elif per_lane is None:
                slot_cap = unmeasured
            elif unmeasured is None:
                slot_cap = per_lane
            else:
                slot_cap = min(per_lane, unmeasured)
            if identity in view.get("unavailable_lanes", {}):
                detail["slot_block"] = view["unavailable_lanes"][identity]
            if (capacity_blocks or (slot_cap is not None and in_flight.get(identity, 0) >= slot_cap)
                    or detail.get("slot_block")):
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
            # C-10.3: the desktop lane last; C-26.2: then a turn's own lane first.
            last = row.get("desktop", False)
            if affinity is not None:
                # C-26.2: a conversation keeps the account that served its last
                # turn while that account stays a candidate (prompt cache).
                return (last, identity != affinity, *base_comparator(identity, row))
            return (last, *base_comparator(identity, row))

        def base_comparator(identity: str, row: dict) -> tuple:
            # C-11.3: retain load bands and Claude's stranded term, then the
            # common weekly rule. C-11.7's admission reserve is still enforced
            # above, but its slack no longer ranks candidates.
            band = 0 if spread is None else row["in_flight"] // int(spread)
            prefix = (band, not bool(row.get("stranded_scopes"))) if model["provider"] == "claude" else (band,)
            return (*prefix, not row["measured"], row["weekly_reserve"], row["five_hour_reserve"],
                    row["seven_day_reset"] or "9999", -(row["weekly_headroom"] or 0), row["in_flight"], identity)

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
