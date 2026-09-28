"""A pure model of one admission pass (C-6.9, C-6.11, C-6.13, C-26.9), for property tests.

`Daemon._admit_pass` places jobs one at a time, each against the view the
placements before it left, with a store, workspaces, probes, leases and two
kinds of pass around it. This model keeps only what decides placement:

- the order (`scheduler.ordered_jobs`: class, tier, oldest first);
- the machine guard at the door (`scheduler.machine_hold`), never for a turn;
- C-6.9's hold-back and kept slot, only in a pool with a count cap
  (`scheduler.pool_capped`);
- one `scheduler.evaluate` per job on the view as it stands, and a placement
  counted in its lane's pool, as the reservation counts it.

It is the daemon's pass with the side effects taken out, so a property that
holds here is a property of the decisions, and the fake-provider daemon tests
check that the daemon takes the same decisions.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from subfleet import scheduler
from subfleet.policy import cap, turn_cap

ACTIVE = "running"


@dataclass
class Outcome:
    """What the pass did with one job, in the order it looked at them."""

    job_id: str
    placed: str | None                      # the lane, when placed
    hold: str | None                        # the reason, when not
    decision: Any = None                    # the evaluation, when one was made
    klass: str = "session"


@dataclass
class Pass:
    outcomes: list[Outcome] = field(default_factory=list)
    view: dict[str, Any] = field(default_factory=dict)

    @property
    def placed(self) -> list[Outcome]:
        return [row for row in self.outcomes if row.placed]

    def by_job(self) -> dict[str, Outcome]:
        return {row.job_id: row for row in self.outcomes}


def _tier(policy: dict, job: dict) -> str:
    tier = job.get("tier") or ("standard" if "standard" in policy["tiers"] else policy["tiers"][0])
    return scheduler.waiter_class(job, tier)


def _pool_cap(policy: dict, job: dict) -> int | None:
    if job.get("kind") == "turn":
        return turn_cap(policy.get("conversations"), "max_active_turns")
    return cap(policy.get("caps"), "max_active_attempts")


def _live(view: dict, turn: bool) -> int:
    return sum((view.get("in_flight_turns" if turn else "in_flight") or {}).values())


def run_pass(policy: dict, view: dict, jobs: list[dict], *, live: scheduler.Liveness | None = None,
             machine: dict | None = None) -> Pass:
    """One pass over `jobs` against `view`, which is not changed."""
    view = copy.deepcopy(view)
    view.setdefault("in_flight", {})
    view.setdefault("in_flight_turns", {})
    view.setdefault("attempts", [])
    view.setdefault("jobs", [])
    known = {row["job_id"] for row in view["jobs"]}
    view["jobs"] = list(view["jobs"]) + [
        {"job_id": job["job_id"], "kind": job.get("kind"), "parent_job_id": job.get("parent_job_id")}
        for job in jobs if job["job_id"] not in known]
    result = Pass(view=view)
    waiters: dict[str, list[tuple[str, Any, Any]]] = {}
    saturated: dict[str, bool] = {}
    lanes = view.get("lanes", ())
    for job in scheduler.ordered_jobs(policy, jobs, live):
        klass = scheduler.priority_class(job, live)
        turn = job.get("kind") == "turn"
        pool = "turn" if turn else "detached"
        tier = _tier(policy, job)
        if not turn:
            busy = scheduler.machine_hold(policy, machine, klass)
            if busy:
                result.outcomes.append(Outcome(job["job_id"], None, "machine-busy", klass=klass))
                continue
        models = scheduler.demand_models(policy, job)
        demand = scheduler.demand_lanes(lanes, job, policy)
        capped = scheduler.pool_capped(policy, job)
        behind = next((older for older, theirs, their_lanes in waiters.get(tier, ())
                       if scheduler.competes(models, theirs, demand, their_lanes)), None) if capped else None
        if saturated.get(pool) or behind:
            result.outcomes.append(Outcome(job["job_id"], None, "fleet-full" if saturated.get(pool)
                                           else "behind-older-job", klass=klass))
            continue
        try:
            decision = scheduler.evaluate(policy, view, job)
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            result.outcomes.append(Outcome(job["job_id"], None, "route", klass=klass))
            continue
        pool_cap = _pool_cap(policy, job)
        live_now = _live(view, turn)
        saturated[pool] = pool_cap is not None and live_now >= pool_cap
        limit = None if pool_cap is None else pool_cap - 1 if waiters.get(tier) else pool_cap
        at_limit = limit is not None and live_now >= limit
        if not decision.chosen_lane or at_limit:
            waiters.setdefault(tier, []).append((job["job_id"], models, demand))
            hold = (scheduler.dominant_rejection(decision) if not decision.chosen_lane
                    else "fleet-full" if saturated[pool] else "slot-kept")
            result.outcomes.append(Outcome(job["job_id"], None, hold, decision, klass=klass))
            continue
        lane = decision.chosen_lane
        counts = view["in_flight_turns" if turn else "in_flight"]
        counts[lane] = counts.get(lane, 0) + 1
        view["attempts"].append({"attempt_id": f"{job['job_id']}/model", "job_id": job["job_id"],
                                 "lane_id": lane, "state": ACTIVE})
        for row in view["lanes"]:
            if row["lane_id"] == lane:
                row["in_flight_turns" if turn else "in_flight"] = counts[lane]
        result.outcomes.append(Outcome(job["job_id"], lane, None, decision, klass=klass))
    return result
