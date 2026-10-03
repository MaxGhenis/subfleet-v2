"""Read-only lane recommendations for the permanent PATH-shim contract.

Unlike supervised submission, a caller that receives a home/email cannot hold
a durable slot or perform an admission probe. Only idle lanes with fresh usage
are returned. Recommendations never launch providers, redeem credits, or write.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .capacity import fresh_provider
from .policy import resolve_model
from .scheduler import _earliest_reset, evaluate


def _email(lane: dict) -> str | None:
    for value in (lane.get("label"), lane.get("email"),
                  str(lane.get("account_key") or "").partition(":")[2]):
        if isinstance(value, str) and "@" in value and not any(c.isspace() for c in value):
            return value
    return None


def rank(policy: dict, view: dict, *, family: str = "codex", model: str | None = None,
         exclusions: list[str] | None = None, min_headroom: float | None = None) -> dict[str, Any]:
    """C-10/C-11: preserve every admission guard without making a reservation.

    An exact model scopes known closures normally. Without a model the caller's
    eventual CLI model is unknown, so every configured model in that family must
    qualify; no model-specific limit or reserve is silently bypassed.
    """
    if family not in ("codex", "claude"):
        raise ValueError("pick: family must be codex or claude")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("pick: model must be a nonblank policy model")
    if exclusions is not None and (not isinstance(exclusions, list)
                                  or any(not isinstance(value, str) for value in exclusions)):
        raise ValueError("pick: exclusions must be a list of strings")
    if min_headroom is not None and (isinstance(min_headroom, bool)
            or not isinstance(min_headroom, (int, float))
            or not math.isfinite(min_headroom) or not 0 <= min_headroom <= 100):
        raise ValueError("pick: min_headroom must be a finite percentage from 0 to 100")
    selected = resolve_model(policy, model) if model is not None else None
    if selected and policy["models"][selected]["provider"] != family:
        raise ValueError("pick: model belongs to a different provider")
    models = [selected] if selected else [name for name, item in policy["models"].items()
                                         if item["provider"] == family]
    # Legacy --min-headroom can demand more capacity, never waive policy floors.
    rules = {**policy, "headroom_floor": max(policy.get("headroom_floor", .05),
                                            (min_headroom or 0) / 100)}
    evaluations = [evaluate(rules, view, {"pinned_model": name,
                    "sandbox": "read-only", "exclusions": exclusions or []}).evaluations[0]
                   for name in models]
    timestamp = view.get("now") or datetime.now(timezone.utc).isoformat()
    now = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    ttl = policy["caps"]["reading_ttl_s"]
    roster = {lane["lane_id"]: lane for lane in view.get("lanes", [])
              if lane["provider"] == family}
    leased = {row["lease_key"].split(":")[1] for row in view.get("lane_leases", [])
              if row.get("lease_key", "").startswith("lane:")}
    in_flight = view.get("in_flight", {})
    reasons: dict[str, list[str]] = {identity: [] for identity in roster}
    details: dict[str, list[dict]] = {identity: [] for identity in roster}
    for evaluation in evaluations:
        rejected = {row["lane_id"]: row for row in evaluation["rejections"]}
        for identity in roster:
            if identity in rejected:
                reasons[identity].extend(rejected[identity]["reasons"])
            else:
                detail = evaluation["candidate_details"].get(identity)
                if detail is None:
                    reasons[identity].append("not-eligible")
                else:
                    details[identity].append(detail)
                    if not detail["measured"]:
                        reasons[identity].append("fresh-usage-required")
                    if (detail.get("reserve") or {}).get("requires_probe"):
                        reasons[identity].append("admission-probe-required")
    emails = Counter(email.casefold() for lane in roster.values() if (email := _email(lane)))
    for identity, lane in roster.items():
        if not models:
            reasons[identity].append("no-policy-model")
        if identity in leased or in_flight.get(identity, lane.get("in_flight", 0)):
            reasons[identity].append("busy")
        if not selected:
            # Unknown/retired model scopes can still close the caller's model.
            for closure in view.get("closures", []):
                if closure["lane_id"] == identity and not closure.get("released_at"):
                    until = datetime.fromisoformat(closure["until_at"].replace("Z", "+00:00"))
                    if until > now:
                        reasons[identity].append("closed:" + closure["scope"])
        if family == "codex":
            home = lane.get("home")
            if not isinstance(home, str) or not Path(home).is_absolute() or "\n" in home or "\r" in home:
                reasons[identity].append("missing-home")
        elif not _email(lane):
            reasons[identity].append("missing-email")
        elif emails[_email(lane).casefold()] > 1:
            reasons[identity].append("ambiguous-email")

    order = {identity: index for index, identity in enumerate(
        evaluations[0]["candidates"] if evaluations else [])}
    ranked, excluded, accounts = [], [], set()
    for identity in sorted(roster, key=lambda key: (order.get(key, len(roster)), key)):
        lane = roster[identity]
        account = lane.get("account_key")
        if not reasons[identity] and (not account or account in accounts):
            reasons[identity].append("duplicate-account" if account else "missing-account")
        row = {"lane_id": identity, "home": lane.get("home"), "email": _email(lane),
               "account_id": str(account or "").partition(":")[2] or None}
        if reasons[identity]:
            why = list(dict.fromkeys(reasons[identity]))
            excluded.append({**row, "verdict": why[0], "reasons": why})
            continue
        accounts.add(account)
        readings = [reading for reading in view.get("readings", [])
                    if reading["lane_id"] == identity
                    and reading["scope"] in ("account", *[policy["models"][name]["id"] for name in models])
                    and fresh_provider(reading, now=now, reading_ttl_s=ttl)]
        def utilization(window):
            values = [reading["utilization"] * 100 for reading in readings if reading["window"] == window]
            return max(values) if values else None
        # With no exact model the recommendation must qualify for all models.
        # Summarize the least remaining weekly window, keeping its own reset;
        # retain each model's detail so the scoped explanation stays inspectable.
        binding = min(details[identity], key=lambda detail: (
            detail["weekly_headroom"] if detail["weekly_headroom"] is not None else float("inf"),
            detail["seven_day_reset"] or "9999", detail["weekly_scope"] or ""))
        weekly_low = any(detail["weekly_reserve"] for detail in details[identity])
        five_low = any(detail["five_hour_reserve"] for detail in details[identity])
        five_heads = [detail["five_hour_headroom"] for detail in details[identity]
                      if detail["five_hour_headroom"] is not None]
        row.update(five_hour_used_percent=utilization("five_hour"),
                   weekly_used_percent=utilization("seven_day"),
                   weekly_reset_at=binding["seven_day_reset"],
                   weekly_headroom=binding["weekly_headroom"], weekly_scope=binding["weekly_scope"],
                   five_hour_headroom=min(five_heads, default=None),
                   weekly_reserve=weekly_low, five_hour_reserve=five_low,
                   reserve_class=("weekly+five-hour" if weekly_low and five_low else
                                  "weekly" if weekly_low else "five-hour" if five_low else "clear"),
                   reading_age_s=max((detail["reading_age_s"] for detail in details[identity]
                                      if detail["reading_age_s"] is not None), default=None),
                   model_details=dict(zip(models, details[identity])), stale=False,
                   in_flight=0, protected=False, as_of=timestamp)
        ranked.append(row)
    return {"generated_at": timestamp, "best": (ranked[0]["home" if family == "codex" else "email"]
            if ranked else None), "ranked": ranked, "excluded": excluded,
            "family": family, "model": policy["models"][selected]["id"] if selected else None,
            "enrolled": len(roster), "earliest_reset": _earliest_reset(evaluations, now),
            "advisory": True, "scope": "exact-model" if selected else "unknown-model"}
