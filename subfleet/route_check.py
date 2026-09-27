"""C-6.3: a route decision made before the reservation, as it stands inside it.

Admission evaluates a job's route off the store lock, on one read snapshot
(`Daemon._pick`), and reserves inside a transaction that holds the lock. It
used to evaluate the route again inside whenever any transaction had committed
in between, which under load was nearly always: a whole capacity view built in
Python with the store lock held, 7.7 to 12.6 s at a time on 2026-09-26.

This module works from a few rows the transaction reads by index instead
(`Daemon._route_rows`): every lane row, the attempts in flight, the probe
leases, the readings added since the snapshot and the open closures. A lane
whose facts (`scheduler.LANE_FACTS`, its readings, closures, attempts in flight
in the job's pool and slot block) are what they were is judged as it was,
since the caller has checked that the clock has not reached the decision's
horizon (`capacity.decision_horizon`). A lane whose facts changed is judged
again, alone (`scheduler.judge_lane`), and ranked against the candidates that
did not change (`scheduler.rank_key`). When those lanes decide the question
alone, the answer is the decision `scheduler.evaluate` over the rows the
transaction sees, at this clock, would return (`as_now`), the same lane or
another; when they do not (a capacity block began or ended, or the walk would
go past the models the early decision judged), it says so, and the caller
evaluates again off the lock. Nothing here builds a capacity view or calls
`evaluate`.

The caller refuses on its own what this module cannot see: a policy that was
replaced, a reset-credit override context that changed, and readings the
snapshot held that were deleted since.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from . import capacity, scheduler
from .contracts import Decision
from .policy import PolicyError

#: C-6.12: what an evaluation raises for a job whose route cannot be evaluated.
ROUTE_ERRORS = (ValueError, KeyError, TypeError, AttributeError, IndexError, PolicyError)
ACTIVE = frozenset({"reserved", "starting", "running", "finalizing"})


def lane_facts(lane: Mapping[str, Any]) -> tuple:
    """The values of a lane row that `scheduler.evaluate` reads."""
    return tuple(lane.get(key) for key in scheduler.LANE_FACTS)


def closure_facts(rows: Iterable[Mapping[str, Any]]) -> tuple:
    """A lane's open closures, every column, in the order a view lists them."""
    return tuple(tuple(sorted(dict(row).items())) for row in rows)


def active_closures(rows: Iterable[Mapping[str, Any]], instant: datetime) -> dict[str, list[dict[str, Any]]]:
    """Per lane, the closures a view built at `instant` holds, as `capacity.build_view` keeps them."""
    kept: dict[str, list[dict[str, Any]]] = {}
    for item in rows:
        row = dict(item)
        if not row.get("released_at") and capacity._time(row["until_at"]) > instant:
            kept.setdefault(row["lane_id"], []).append(row)
    return kept


def in_flight(attempts: Iterable[Mapping[str, Any]], lanes: Iterable[str]) -> tuple[dict[str, int], dict[str, int]]:
    """Detached and turn attempts in flight per lane of the roster, as a view counts them (C-26.9)."""
    roster = set(lanes)
    detached = dict.fromkeys(roster, 0)
    turns = dict.fromkeys(roster, 0)
    for row in attempts:
        if row.get("state") in ACTIVE and row["lane_id"] in roster:
            counts = turns if row.get("kind") == "turn" else detached
            counts[row["lane_id"]] += 1
    return detached, turns


def still_stands(policy: Mapping[str, Any], job: Mapping[str, Any], decision: Decision, *,
                 view: Mapping[str, Any], candidates: Iterable[Mapping[str, Any]], overridden: Iterable[str],
                 now: datetime, lanes: Iterable[Mapping[str, Any]], readings: Iterable[Mapping[str, Any]],
                 closures: Iterable[Mapping[str, Any]], attempts: Iterable[Mapping[str, Any]],
                 jobs: Iterable[Mapping[str, Any]], unavailable: Mapping[str, Any],
                 reserved_probes: int) -> tuple[str | None, int, Decision | None]:
    """Whether `decision`, evaluated for `job` on `view`, is what an evaluation now would choose.

    Returns (None, lanes judged again, the decision now) when the lanes whose
    rows changed decide it alone (`as_now`): the decision chose a lane and it
    still ranks first; another lane now ranks first at that model, or a lane
    now takes an earlier model of the chain; or it chose none, and now one of
    its lanes takes a model, or none does and the verdict is now's. The
    decision now is what `scheduler.evaluate` over the rows the reservation
    sees would return, but for `evaluated_at` and the age (`age_s`) of the
    readings of lanes that did not change, which are the snapshot's. Returns ("full" or
    "moved", n, None) when it takes lanes that were never judged or did not
    change: a fleet or parent cap began ("full") or ended, or the model the
    decision chose has no candidate now and the chain goes on past it.

    The early side: `view` is the view the decision was evaluated on (after
    `_pick` held out overridden lanes' readings), `candidates` the raw reading
    rows its readings were chosen from (`Store.latest_reading_candidates`) and
    `overridden` the lanes whose readings were held out.

    The side now, read inside the reservation: `lanes`, every lane row marked
    and merged as a view marks and merges them (`capacity.mark_desktop`,
    `Timers.merge_lane`); `readings`, every reading added since the snapshot;
    `closures`, every closure not released; `attempts`, those in flight with
    their job's `kind`; `jobs`, every job on the ancestry of the job and of
    those attempts (`job_id`, `parent_job_id`, `kind`); `unavailable`, the
    probe leases and latched credentials by lane; `reserved_probes`, the probe
    leases. `now` must be before the decision's horizon.
    """
    # `capacity.build_view` keeps closures and labels readings on the clock it is
    # given, and gives `evaluate` that clock in whole seconds: so here.
    precise = capacity._time(now)
    instant = capacity._time(capacity._iso(precise))
    ttl = view.get("reading_ttl_s", capacity.READING_TTL_S)
    then_lanes = {row["lane_id"]: row for row in view.get("lanes", ())}
    now_lanes = {row["lane_id"]: dict(row) for row in lanes}
    attempts = [dict(row) for row in attempts]
    detached, turns = in_flight(attempts, now_lanes)
    pool = "in_flight_turns" if job.get("kind") == "turn" else "in_flight"
    counts_now = turns if pool == "in_flight_turns" else detached
    counts_then = view.get(pool, {})
    open_then: dict[str, list[dict[str, Any]]] = {}
    for row in view.get("closures", ()):
        open_then.setdefault(row["lane_id"], []).append(row)
    open_now = active_closures(closures, precise)
    added: dict[str, list[dict[str, Any]]] = {}
    for row in readings:
        added.setdefault(row["lane_id"], []).append(dict(row))
    unavailable_then = view.get("unavailable_lanes", {})
    changed = set()
    for lane_id in then_lanes.keys() | now_lanes.keys():
        then, current = then_lanes.get(lane_id), now_lanes.get(lane_id)
        if (then is None or current is None or lane_facts(then) != lane_facts(current)
                or added.get(lane_id)
                or closure_facts(open_then.get(lane_id, ())) != closure_facts(open_now.get(lane_id, ()))
                or counts_then.get(lane_id, 0) != counts_now.get(lane_id, 0)
                or (lane_id in unavailable_then) != (lane_id in unavailable)
                or unavailable_then.get(lane_id) != unavailable.get(lane_id)):
            changed.add(lane_id)
    skeleton = {"now": capacity._iso(instant), "lanes": list(now_lanes.values()), "in_flight": detached,
                "in_flight_turns": turns, "reserved_probes": reserved_probes, "attempts": attempts,
                "jobs": [dict(row) for row in jobs]}
    try:
        setup = scheduler.prepare(policy, skeleton, job)
        if setup["pin"]:
            # The lane the pin named then, from the roster alone (`prepare` reads
            # attempts and jobs only for the caps, which no pin depends on).
            before = scheduler.prepare(policy, {"now": view["now"], "lanes": view.get("lanes", ()),
                                                "in_flight": {}, "in_flight_turns": {}}, job)["selected"]
            if (setup["selected"] or {}).get("lane_id") != (before or {}).get("lane_id"):
                return "moved", 0, None
    except ROUTE_ERRORS:
        return "moved", 0, None      # an evaluation now would raise; the caller evaluates again outside
    held_out = set(overridden)
    earlier: dict[str, list[dict[str, Any]]] = {}
    for row in candidates:
        earlier.setdefault(row["lane_id"], []).append(dict(row))
    now_readings = {lane_id: ([] if lane_id in held_out else capacity.latest_readings(
        earlier.get(lane_id, []) + added.get(lane_id, []), now=precise, reading_ttl_s=ttl))
        for lane_id in changed if lane_id in now_lanes}
    judged_lanes: set[str] = set()
    walked: list[dict[str, tuple[list[str], dict[str, Any]]]] = []

    def judge(index: int, short: str) -> dict[str, tuple[list[str], dict[str, Any]]]:
        """The changed lanes this model looks at now, judged at this clock."""
        here = {lane["lane_id"] for lane in scheduler.model_lanes(setup, short)}
        found = {}
        for lane_id in sorted(changed & here):
            judged_lanes.add(lane_id)
            found[lane_id] = scheduler.judge_lane(setup, short, now_lanes[lane_id], now_readings[lane_id],
                                                  open_now.get(lane_id, ()), in_flight=counts_now.get(lane_id, 0),
                                                  unavailable=unavailable)
        walked.append(found)
        return found

    # A fleet or parent cap that began or ended refuses or frees every lane alike,
    # and which lanes it frees takes every lane's rows: evaluated again outside.
    blocks = decision.evaluations[0]["capacity_blocks"] if decision.evaluations else []
    if list(setup["capacity_blocks"]) != list(blocks):
        return ("full" if setup["capacity_blocks"] else "moved"), 0, None
    walk = tuple(setup["chain"])
    if (walk[:len(decision.chain)] if decision.chosen_lane else walk) != tuple(decision.chain):
        return "moved", 0, None
    last = len(decision.chain) - 1
    for index, short in enumerate(decision.chain):
        evaluation = decision.evaluations[index]
        found = judge(index, short)
        # Every lane that did not change is judged as it was: before the model the
        # decision chose, and anywhere in a decision that chose none, refused.
        standing = {lane_id: detail for lane_id, (reasons, detail) in found.items() if not reasons}
        if decision.chosen_lane and index == last:
            standing.update({lane_id: evaluation["candidate_details"][lane_id]
                             for lane_id in evaluation["candidates"] if lane_id not in changed})
        if standing:
            best = min(standing, key=lambda lane_id: scheduler.rank_key(setup, short, lane_id, standing[lane_id]))
            return None, len(judged_lanes), as_now(decision, setup, changed, walked, now_readings, open_now,
                                                   instant, chosen=(best, short))
        if decision.chosen_lane and index == last and index + 1 < len(walk):
            # The model it chose has no candidate now, and the walk goes on to
            # models this decision never judged its lanes for.
            return "moved", len(judged_lanes), None
    return None, len(judged_lanes), as_now(decision, setup, changed, walked, now_readings, open_now,
                                           instant, chosen=None)


def as_now(decision: Decision, setup: Mapping[str, Any], changed: set[str],
           walked: list[dict[str, tuple[list[str], dict[str, Any]]]], readings: Mapping[str, list[dict[str, Any]]],
           closures: Mapping[str, list[dict[str, Any]]], instant: datetime, *,
           chosen: tuple[str, str] | None) -> Decision:
    """The decision as an evaluation now makes it: every changed lane's verdict,
    detail and evidence (its readings and closures) put in place of the
    snapshot's, in the order `evaluate` lists them, the walk ended at the model
    `chosen` names (or run through, when it is None), and the reasons said
    again. Its verdict (`scheduler.verdict_signature`) is now's, which is what
    a wait recorded on it is clocked by (C-6.10). `evaluated_at` stays the
    early evaluation's, and so does the age of each reading of a lane that did
    not change; each changed lane's readings carry their age now."""
    policy = setup["policy"]
    evaluations = []
    for index, evaluation in enumerate(decision.evaluations[:len(walked)]):
        short, found = evaluation["model"], walked[index]
        model = policy["models"][short]
        lane_ids = {lane["lane_id"] for lane in scheduler.model_lanes(setup, short)}
        higher = setup["higher"].get(short)
        if higher is None:
            higher = setup["higher"][short] = scheduler._higher_model_scopes(policy, short)
        details = {lane_id: detail for lane_id, detail in evaluation["candidate_details"].items()
                   if lane_id not in changed}
        details.update({lane_id: detail for lane_id, (reasons, detail) in found.items() if not reasons})
        ranked = sorted(details, key=lambda lane_id: scheduler.rank_key(setup, short, lane_id, details[lane_id]))
        rejections = [row for row in evaluation["rejections"] if row["lane_id"] not in changed]
        rejections += [{"lane_id": lane_id, "reason": reasons[0], "reasons": reasons, **detail}
                       for lane_id, (reasons, detail) in found.items() if reasons]
        rejections.sort(key=lambda row: row["lane_id"])
        fresh = [row for lane_id in sorted(changed & lane_ids) for row in readings.get(lane_id, ())]
        open_ = [row for lane_id in sorted(changed & lane_ids) for row in closures.get(lane_id, ())
                 if scheduler._future_closure(row, instant)]

        def merged(rows: list[dict[str, Any]], new: list[dict[str, Any]], key) -> list[dict[str, Any]]:
            return sorted([row for row in rows if row["lane_id"] not in changed] + new, key=key)

        by_key = lambda row: (row["lane_id"], row["scope"], row["window"])      # noqa: E731 - a view's order
        by_id = lambda row: row["closure_id"]                                   # noqa: E731 - a view's order
        scoped = lambda row: row["scope"] in ("account", model["id"])          # noqa: E731
        evaluations.append({**evaluation, "candidates": ranked, "candidate_details": details,
                            "rejections": rejections, "rejected": rejections,
                            "readings": merged(evaluation["readings"], [row for row in fresh if scoped(row)], by_key),
                            "capacity_readings": merged(evaluation["capacity_readings"], fresh, by_key),
                            "closures": merged(evaluation["closures"], [row for row in open_ if scoped(row)], by_id),
                            "stranding_closures": merged(evaluation["stranding_closures"],
                                                         [row for row in open_ if row["scope"] in higher], by_id),
                            "reason": scheduler.model_reason(setup, index, short, ranked, details)})
    lane, model = chosen or (None, None)
    return Decision(tuple(row["model"] for row in evaluations), tuple(evaluations), lane, model,
                    scheduler.decision_reason(evaluations, lane, instant), decision.policy_hash)
