"""Frozen routing functions from 3f50142ac1cc for D-SH1 differential tests.

Executed with the scheduler helpers in a separate namespace; these four
functions preserve the base candidate selection and explanation verbatim.
"""

def prepare(policy: Mapping[str, Any], view: Mapping[str, Any], job: Any) -> dict[str, Any]:
    """C-11.2, C-26.9: what `evaluate` settles before it looks at any lane.

    The job's chain and the lane its pin names, the clock, the caps, the job's
    exclusions, the attempts in flight in the job's pool, and the capacity
    blocks (the fleet's or a parent's cap) that refuse every lane alike. It
    reads the view's `now`, `lanes`, `in_flight` or `in_flight_turns`,
    `reserved_probes`, `attempts` and `jobs`, and no reading or closure, so
    C-6.3's check inside the reservation builds it from a few rows."""
    job = _row(job)
    authorization_reason = _unmeasured_reserve_reason(job)
    now = _time(view["now"]) if view.get("now") else datetime.now(timezone.utc)
    lanes = sorted((_row(lane) for lane in view.get("lanes", ())), key=lambda lane: lane["lane_id"])
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
        chain = flatten_chain(policy["chains"][task], policy["tiers"].index(tier or default))
    else:
        chain = []
    if chain and job_mcp_servers(job):
        # C-12.9: only a Claude launch starts the MCP servers the job named. A
        # policy edit since submit can leave none; that waits, as C-6.12 says.
        chain = mcp_chain(policy, chain, job)
        if not chain:
            raise RouteError("mcp_servers: this job names MCP servers, which only a Claude launch "
                             "starts, and its chain has no Claude model", policy_dependent=True)
    # C-11.2: a pinned job evaluates one model, the first of its chain, so a lane
    # of any other provider could never take it (`pin_provider` says the same).
    selected = (resolve_lane(lanes, pin, policy["models"][chain[0]]["provider"] if chain else None,
                             follow=not authorization_reason) if pin else None)
    if authorization_reason and selected and pin != selected["lane_id"]:
        raise RouteError("unmeasured_reserve_reason: pinned_lane must be the canonical lane id")
    if not chain and selected:
        if job_mcp_servers(job) and selected["provider"] != MCP_PROVIDER:
            raise RouteError(f"mcp_servers: this job names MCP servers, which only a Claude launch starts, "
                             f"and lane {pin!r} is a {selected['provider']} lane")
        model_name = next((name for name, model in policy["models"].items()
                           if model["provider"] == selected["provider"]), None)
        if model_name is None:
            raise PolicyError(policy.get("_policy_path", "policy.json"), "pinned_lane",
                              f"lane {pin!r} has provider {selected['provider']!r} with no models in policy")
        chain = [model_name]
    elif not chain:
        chain = (mcp_chain(policy, policy["models"], job) or [next(iter(policy["models"]))])[:1]
    if pin:
        chain = chain[:1]
        if selected and policy["models"][chain[0]]["provider"] != selected["provider"]:
            raise RouteError("pinned_lane and pinned_model/task: different providers", policy_dependent=True)
    # C-26.9: a conversation turn has its own capacity, counted apart from
    # detached jobs: `conversations.max_active_turns` across the fleet and
    # `conversations.turn_slots_per_lane` per lane, each no cap unless the policy
    # sets one. Neither kind waits for the other's slots; an attended turn never
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
    capacity_blocks = _parent_blocks(policy, view, job)
    # C-6.4: each fleet cap is none unless the policy sets one.
    fleet_cap = turn_cap(conversation_caps, "max_active_turns") if is_turn else cap(caps, "max_active_attempts")
    if fleet_cap is not None and sum(in_flight.values()) + (0 if is_turn else view.get("reserved_probes", 0)) >= fleet_cap:
        capacity_blocks.append("fleet")
    return {"policy": policy, "job": job, "authorization_reason": authorization_reason, "now": now,
            "lanes": lanes, "caps": caps, "floor": floor, "excluded": excluded, "pin": pin,
            "selected": selected, "chain": chain, "is_turn": is_turn, "conversation_caps": conversation_caps,
            "in_flight": in_flight, "capacity_blocks": capacity_blocks, "higher": {},
            "provider_live": provider_live(view) if provider_caps_on(policy) else {},
            "lane_spread": admission_settings(policy)["lane_spread"]}

def judge_lane(setup: Mapping[str, Any], short: str, lane: Mapping[str, Any],
               readings: Iterable[Mapping[str, Any]], closures: Iterable[Mapping[str, Any]], *,
               in_flight: int, unavailable: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """C-11.3–C-11.7, C-26.2, C-26.9: one lane for one model of the chain.

    The reasons the lane is refused (none: it is a candidate) and the detail
    its ranking reads. `readings` and `closures` are the view's rows of this
    lane (every scope and window), in the view's order; `in_flight` is the
    lane's attempts in the job's pool; `unavailable` is the view's
    `unavailable_lanes`. Nothing else about other lanes is read: the fleet and
    parent caps come in `setup` as capacity blocks."""
    policy, job, now, caps = setup["policy"], setup["job"], setup["now"], setup["caps"]
    model = policy["models"][short]
    higher_scopes = setup["higher"].get(short)
    if higher_scopes is None:
        higher_scopes = setup["higher"][short] = _higher_model_scopes(policy, short)
    readings, closures = list(readings), list(closures)
    identity = lane["lane_id"]
    reasons = []
    lane_readings = [row for row in readings if row["scope"] in ("account", model["id"])]
    measured_readings = [row for row in lane_readings
                         if fresh_provider(row, now=now, reading_ttl_s=caps["reading_ttl_s"])]
    headroom = min((1 - row["utilization"] for row in measured_readings), default=None)
    ranking = ranking_usage(lane_readings, now=now, reading_ttl_s=caps["reading_ttl_s"],
                            admission=admission_settings(policy))
    # C-11.3's uncertainty orders candidates only. C-11.4 probes and C-11.5
    # pick keep the admission freshness predicate they used before this rule.
    detail = {**ranking, "ranking_measured": ranking["measured"], "measured": bool(measured_readings),
              "headroom": headroom, "in_flight": in_flight}
    detail["status"] = "eligible" if detail["measured"] else "eligible but unmeasured"
    provider = provider_capacity(setup, short)
    if provider:
        detail["provider_cap"] = provider
    if model["provider"] == "claude":
        detail["stranded_scopes"] = sorted({row["scope"] for row in closures
            if row["scope"] in higher_scopes and _future_closure(row, now)})
    if lane.get("desktop"):
        # C-10.3, C-11.3: a desktop lane that is a candidate sorts after every other.
        detail["desktop"] = True
    if _identities(lane) & setup["excluded"]:
        reasons.append("excluded")
    if desktop_excluded(lane) and not job.get("allow_desktop"):
        # C-10.3: only while Claude Code is using the desktop login (or that is unknown).
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
    reasons.extend(f"closed:{row['scope']}:{row['until_at']}" for row in closures
                   if row["scope"] in ("account", model["id"]) and _future_closure(row, now))
    lane_measured = any(fresh_provider(row, now=now, reading_ttl_s=caps["reading_ttl_s"]) for row in readings)
    # C-6.4, C-26.9: None when the policy sets no per-lane cap for the job's pool.
    slot_cap = (turn_cap(setup["conversation_caps"], "turn_slots_per_lane") if setup["is_turn"] else
                lane_slot_cap(caps, lane_measured))
    if identity in unavailable and not (setup["is_turn"] and pilot_block(unavailable[identity])):
        # C-6.14: a pilot's block holds detached attempts only; a turn is never held for one.
        detail["slot_block"] = unavailable[identity]
    if capacity_blocks(setup, short) or (slot_cap is not None and in_flight >= slot_cap) or detail.get("slot_block"):
        reasons.append("no-slot")
    if any(row["utilization"] >= 1 - setup["floor"] for row in measured_readings):
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
        if verdict["state"] == "unmeasured" and setup["authorization_reason"]:
            # This authorizes uncertainty on the explicit pinned pair;
            # it supplies no usage evidence and releases no other guard.
            detail["reserve"]["authorization"] = {
                "reason": setup["authorization_reason"], "lane_id": identity, "model_id": model["id"]}
        elif verdict["state"] != "slack":
            reasons.append(f"reserve:{reserved}:{verdict['state']}")
        elif job.get("kind") == "turn" and verdict.get("requires_probe"):
            # C-26.9: a turn never waits on a probe; a lane that needs one
            # is not a candidate for it.
            reasons.append(f"reserve:{reserved}:probe-required")
    return reasons, detail

def model_reason(setup: Mapping[str, Any], index: int, short: str, candidates: list[str],
                 details: Mapping[str, Mapping[str, Any]]) -> str:
    """C-11.5: what one model of the chain came to, in words (`candidates` ranked)."""
    if candidates:
        return f"{short}: chose {candidates[0]}; {details[candidates[0]]['status']}"
    suffix = "; promoted" if index + 1 < len(setup["chain"]) else ""
    reason = f"{short}: no candidate lanes after exclusions{suffix}"
    if setup["pin"] and setup["selected"] is None:
        reason += f"; pinned lane {setup['pin']!r} is unknown"
    return reason

def evaluate(policy: Mapping[str, Any], view: Mapping[str, Any], job: Any) -> Decision:
    """C-11.2–C-11.6: walk upward, applying every rejection before comparison.

    `prepare` settles the chain, the pin and the capacity blocks; each lane of
    each model is judged alone (`judge_lane`) and the candidates ranked by
    `rank_key`, so a lane can be judged again without the rest (C-6.3)."""
    setup = prepare(policy, view, job)
    job, now, pin, selected = setup["job"], setup["now"], setup["pin"], setup["selected"]
    readings = [_row(item) for item in view.get("readings", ())]
    closures = [_row(item) for item in view.get("closures", ())]
    unavailable = view.get("unavailable_lanes", {})
    by_lane_readings: dict[str, list[dict[str, Any]]] = {}
    for row in readings:
        by_lane_readings.setdefault(row["lane_id"], []).append(row)
    by_lane_closures: dict[str, list[dict[str, Any]]] = {}
    for row in closures:
        by_lane_closures.setdefault(row["lane_id"], []).append(row)
    chain = setup["chain"]
    evaluations: list[dict[str, Any]] = []
    chosen_lane = chosen_model = None
    for index, short in enumerate(chain):
        model = policy["models"][short]
        higher_scopes = setup["higher"].get(short)
        if higher_scopes is None:
            higher_scopes = setup["higher"][short] = _higher_model_scopes(policy, short)
        lanes_here = model_lanes(setup, short)
        lane_ids = {lane["lane_id"] for lane in lanes_here}
        scoped_readings = [row for row in readings if row["lane_id"] in lane_ids
                           and row["scope"] in ("account", model["id"])]
        scoped_closures = [row for row in closures if row["lane_id"] in lane_ids
                          and row["scope"] in ("account", model["id"]) and _future_closure(row, now)]
        candidates, rejections, details = [], [], {}
        for lane in lanes_here:
            identity = lane["lane_id"]
            reasons, detail = judge_lane(setup, short, lane, by_lane_readings.get(identity, ()),
                                         by_lane_closures.get(identity, ()),
                                         in_flight=setup["in_flight"].get(identity, 0), unavailable=unavailable)
            if reasons:
                rejections.append({"lane_id": identity, "reason": reasons[0], "reasons": reasons, **detail})
            else:
                candidates.append(identity)
                details[identity] = detail
        candidates.sort(key=lambda identity: rank_key(setup, short, identity, details[identity]))
        if candidates:
            chosen_lane, chosen_model = candidates[0], short
        reason = model_reason(setup, index, short, candidates, details)
        evaluations.append({"model": short, "model_id": model["id"], "provider": model["provider"],
                            "candidates": candidates, "candidate_details": details,
                            "rejections": rejections, "rejected": rejections,
                            "readings": scoped_readings,
                            "capacity_readings": [row for row in readings if row["lane_id"] in lane_ids],
                            "closures": scoped_closures, "capacity_blocks": capacity_blocks(setup, short),
                            "stranding_closures": [row for row in closures if row["lane_id"] in lane_ids
                                and row["scope"] in higher_scopes and _future_closure(row, now)],
                            "reason": reason, "evaluated_at": _iso(now)})
        if candidates:
            break
    digest = job.get("policy_hash") or policy.get("_policy_hash", "")
    return Decision(tuple(row["model"] for row in evaluations), tuple(evaluations),
                    chosen_lane, chosen_model, decision_reason(evaluations, chosen_lane, now), digest)
