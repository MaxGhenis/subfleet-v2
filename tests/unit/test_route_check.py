"""C-6.3: the check a reservation makes of its early route decision, without evaluating.

`route_check.still_stands` judges again only the lanes the decision looks at that
changed since the early evaluation's snapshot: their rows, their override, or their
own clock (`capacity.lane_horizons`) (`scheduler.judge_lane`, `scheduler.rank_key`).
It is pinned against the full evaluation it stands in for, across random stores and
random commits and clocks in between: it refuses exactly when the decision now takes
lanes it never judged or that did not change (a capacity block began or ended, the
walk goes past the models the early decision judged, a pin names another lane), and
otherwise returns exactly what `scheduler.evaluate` over the rows the reservation
sees, at its clock, returns: every field, evidence and `evaluated_at` included. A
clock alone never makes it refuse (review of d04b8b3).
"""

from __future__ import annotations

import copy
from datetime import timedelta

import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies as st

from subfleet import capacity, route_check, scheduler
from tests.routing_strategies import (ACTIVE, BASE_POLICY, NOW, candidates_of, commits, event, exact, policies,
                                     route_jobs, stores, view_of, walked_no_further)

SETTINGS = settings(max_examples=1500, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large,
                                           HealthCheck.filter_too_much])


@st.composite
def cases(draw):
    store = draw(stores())
    return draw(policies()), store, draw(route_jobs(store))


# --- still_stands is exactly the full evaluation's answer ---------------------------------------

def now_rows(before: dict, after: dict) -> dict:
    """What `Daemon._route_rows` reads inside the reservation, from the store after the commits."""
    mark = max((row["reading_id"] for row in before["readings"]), default=0)
    jobs = {row["job_id"]: row for row in after["jobs"]}
    return {"lanes": [dict(row) for row in after["lanes"]],
            "readings": [row for row in after["readings"] if row["reading_id"] > mark],
            "closures": sorted((row for row in after["closures"] if row["released_at"] is None),
                               key=lambda row: row["closure_id"]),
            "attempts": [{**row, "kind": jobs[row["job_id"]]["kind"],
                          "parent_job_id": jobs[row["job_id"]]["parent_job_id"]}
                         for row in after["attempts"] if row["state"] in ACTIVE],
            "jobs": list(jobs.values()), "unavailable": dict(after["unavailable"]),
            "reserved_probes": sum(1 for holder in after["unavailable"].values() if holder.startswith("probe:")),
            "holding": set(after["overridden"])}


def from_changed_lanes(policy, job, decision, view, full, view_now) -> bool:
    """Whether the decision now can be had from the lanes whose rows changed: the
    evaluation now did not raise, names the lane its pin named, has the capacity
    blocks the early one had, and walked no model the early one did not judge."""
    if full is None:
        return False
    if job.get("pinned_lane"):
        pinned = [(scheduler.prepare(policy, one, job)["selected"] or {}).get("lane_id") for one in (view, view_now)]
        if pinned[0] != pinned[1]:
            return False
    return walked_no_further(decision, full)


def check(policy, store, job, after, seconds):
    view = view_of(store, NOW)
    try:
        decision = scheduler.evaluate(policy, view, job)
    except route_check.ROUTE_ERRORS:
        event("early: raised")
        return None
    later = NOW + timedelta(seconds=seconds)
    clocks = capacity.lane_horizons(view, reading_ttl_s=120)
    rows = now_rows(store, after)
    verdict, judged, standing = route_check.still_stands(
        policy, job, decision, view=view, candidates=candidates_of(store["readings"]),
        overridden=store["overridden"], clocks=clocks, now=later, **rows)
    try:
        full = scheduler.evaluate(policy, view_of(after, later), job)
    except route_check.ROUTE_ERRORS:
        full = None
    decidable = from_changed_lanes(policy, job, decision, view, full, view_of(after, later))
    same = bool(full) and (full.chosen_lane, full.chosen_model) == (decision.chosen_lane, decision.chosen_model)
    expired = sorted(lane_id for lane_id, horizon in clocks.items() if later >= horizon)
    event(f"{'lane' if decision.chosen_lane else 'no lane'}: verdict {verdict}; evaluate "
          f"{'same lane' if same else 'another lane'}; {judged} judged again")
    if expired:
        event("a lane's clock passed its horizon" + ("; decision kept" if verdict is None else ""))
    if verdict is None:
        # What the reservation goes on with is what a fresh evaluation decides at
        # this clock: every field, the evidence's ages and labels and
        # `evaluated_at` included.
        assert exact(standing) == exact(full)
    return verdict, decidable, standing or decision, full


@SETTINGS
@given(cases(), st.data())
def test_c6_3_still_stands_exactly_when_evaluate_now_chooses_the_same(case, data):
    """Accepts ⇔ an evaluation over the rows now, at the clock now, picks the same lane
    and model and reads the same probe need there. Never accepts what that evaluation
    would not choose; never refuses what it would, except as the caller's own checks do."""
    policy, store, job = case
    after, seconds = data.draw(commits(store))
    result = check(policy, store, job, after, seconds)
    if result is None:
        return
    verdict, decidable, decision, full = result
    # Refused exactly when the decision now takes lanes that were never judged or
    # did not change; otherwise it is the evaluation's own (asserted in `check`).
    assert (verdict is None) == decidable, (verdict, decision.chosen_lane, full and full.chosen_lane)
    if verdict is None:            # `decision` is the decision now
        assert scheduler.verdict_signature(decision) == scheduler.verdict_signature(full)
        if decision.chosen_lane:
            assert scheduler.probe_required(decision, job) == scheduler.probe_required(full, job)


@SETTINGS
@given(cases(), st.data())
def test_c6_3_still_stands_exactly_when_commits_land_on_the_lanes_it_walked(case, data):
    """The same, with the commits aimed at the lanes the decision walked: the chosen
    lane, its rivals, and the lanes of the models before it."""
    policy, store, job = case
    try:
        decision = scheduler.evaluate(policy, view_of(store, NOW), job)
    except route_check.ROUTE_ERRORS:
        decision = None
    assume(decision is not None)
    walked = tuple(sorted({row["lane_id"] for evaluation in decision.evaluations
                           for row in evaluation["rejections"]} | {lane for evaluation in decision.evaluations
                                                                  for lane in evaluation["candidates"]}))
    after, seconds = data.draw(commits(store, walked))
    result = check(policy, store, job, after, seconds)
    if result is None:
        return
    verdict, decidable, decision, full = result
    # Refused exactly when the decision now takes lanes that were never judged or
    # did not change; otherwise it is the evaluation's own (asserted in `check`).
    assert (verdict is None) == decidable, (verdict, decision.chosen_lane, full and full.chosen_lane)
    if verdict is None:            # `decision` is the decision now
        assert scheduler.verdict_signature(decision) == scheduler.verdict_signature(full)
        if decision.chosen_lane:
            assert scheduler.probe_required(decision, job) == scheduler.probe_required(full, job)


@SETTINGS
@given(cases())
def test_c6_3_with_nothing_committed_the_early_decision_stands(case):
    policy, store, job = case
    result = check(policy, store, job, store, 0)
    if result is not None:
        assert result[0] is None


# --- the cases by name ---------------------------------------------------------------------------

LANES = [{"lane_id": lane_id, "provider": "codex", "account_key": f"codex:{lane_id}@example.invalid",
          "credential_ref": f"/c/{lane_id}", "credential_kind": "keychain-token", "home": None, "owner": "v2",
          "enabled": 1, "desktop": 0, "identity_status": None, "label": None}
         for lane_id in ("codex-1", "codex-2", "codex-3")]


def reading(lane_id, utilization, reading_id, observed=NOW, resets_in=86400):
    return {"reading_id": reading_id, "lane_id": lane_id, "scope": "account", "window": "seven_day",
            "utilization": utilization, "resets_at": capacity._iso(NOW + timedelta(seconds=resets_in)),
            "label": "provider", "source": "probe", "observed_at": capacity._iso(observed), "attempt_id": None}


def fleet(**changes):
    store = {"lanes": [dict(row) for row in LANES],
             "readings": [reading("codex-1", .2, 1), reading("codex-2", .5, 2), reading("codex-3", .6, 3)],
             "closures": [], "jobs": [{"job_id": "busy", "kind": "dispatch", "parent_job_id": None}],
             "attempts": [], "unavailable": {}, "overridden": set()}
    store.update(changes)
    return store


@pytest.fixture
def policy():
    loaded = copy.deepcopy(BASE_POLICY)
    loaded["reserve"] = {**loaded.get("reserve", {}), "models": []}
    return loaded


JOB = {"job_id": "route-job", "kind": "dispatch", "sandbox": "read-only", "task": None, "tier": None,
       "pinned_model": "astra", "pinned_lane": None, "exclusions": (), "allow_desktop": 0,
       "parent_job_id": None, "policy_hash": "fixture", "unmeasured_reserve_reason": None}


def stands(policy, store, after, job=JOB, seconds=1):
    """The check's verdict and the lane of the decision now (the early one's, if refused)."""
    result = check(policy, store, job, after, seconds)
    assert result is not None
    verdict, decidable, decision, _ = result
    assert (verdict is None) == decidable
    return verdict, decision.chosen_lane


def with_(store, **changes):
    after = copy.deepcopy(store)
    after.update(changes)
    return after


def test_c6_3_a_commit_that_touches_no_lane_leaves_the_decision_standing(policy):
    store = fleet()
    assert stands(policy, store, with_(store)) == (None, "codex-1")


def test_c6_3_a_fresher_reading_elsewhere_that_ranks_below_changes_nothing(policy):
    store = fleet()
    after = with_(store, readings=store["readings"] + [reading("codex-3", .4, 4)])
    assert stands(policy, store, after) == (None, "codex-1")


def test_c6_3_a_reading_that_puts_another_lane_first_chooses_that_lane(policy):
    """Codex ranks measured lanes by their weekly reset, soonest first: codex-2's new
    reading puts it first, and the check chooses it from that lane's rows alone."""
    store = fleet()
    after = with_(store, readings=store["readings"] + [reading("codex-2", .5, 4, resets_in=600)])
    assert stands(policy, store, after) == (None, "codex-2")


def test_c6_3_a_closure_on_the_chosen_lane_chooses_the_next(policy):
    store = fleet()
    closure = {"closure_id": 1, "lane_id": "codex-1", "scope": "account",
               "until_at": capacity._iso(NOW + timedelta(hours=1)), "reason": "provider-limit",
               "clock_source": "reported", "source_event": "x", "created_at": capacity._iso(NOW), "released_at": None}
    assert stands(policy, store, with_(store, closures=[closure])) == (None, "codex-2")


def test_c6_3_a_concurrent_reservation_that_fills_the_fleet_is_full(policy):
    policy["caps"]["max_active_attempts"] = 1
    store = fleet()
    after = with_(store, attempts=[{"attempt_id": "busy/a1", "job_id": "busy", "lane_id": "codex-3", "state": "reserved"}])
    assert stands(policy, store, after) == ("full", "codex-1")


def test_c6_3_a_second_attempt_on_an_unmeasured_lane_is_never_reserved(policy):
    """C-6.4's `max_in_flight_unmeasured`: codex-1 unmeasured takes one attempt at most."""
    store = fleet(readings=[reading("codex-2", .5, 2), reading("codex-3", .6, 3)],
                  lanes=[dict(LANES[0])])
    after = with_(store, attempts=[{"attempt_id": "busy/a1", "job_id": "busy", "lane_id": "codex-1", "state": "running"}])
    assert stands(policy, store, after) == (None, None)                 # the one lane has no slot now


def test_c6_3_an_attempt_ending_on_a_better_lane_chooses_it(policy):
    """codex-1 was full; the one attempt holding it ends, and it ranks first again."""
    policy["caps"]["max_in_flight_per_lane"] = 1
    running = [{"attempt_id": "busy/a1", "job_id": "busy", "lane_id": "codex-1", "state": "running"}]
    store = fleet(readings=[reading("codex-1", .2, 1, resets_in=600), reading("codex-2", .5, 2)], attempts=running)
    ended = [{**running[0], "state": "succeeded"}]
    assert stands(policy, store, with_(store, attempts=ended)) == (None, "codex-1")


def test_c6_3_a_turn_keeps_its_lane_whatever_other_lanes_report(policy):
    """C-26.2: affinity outranks every other key, so another lane's news cannot move a turn."""
    turn = {**JOB, "kind": "turn", "affinity_lane": "codex-3"}
    store = fleet()
    after = with_(store, readings=store["readings"] + [reading("codex-1", .0, 4), reading("codex-2", .0, 5)])
    assert stands(policy, store, after, job=turn) == (None, "codex-3")


def test_c6_3_a_probe_taking_the_chosen_lane_chooses_the_next(policy):
    store = fleet()
    assert stands(policy, store, with_(store, unavailable={"codex-1": "probe:timer:1"})) == (None, "codex-2")


def test_c6_3_a_disabled_chosen_lane_chooses_the_next(policy):
    store = fleet()
    after = with_(store, lanes=[{**LANES[0], "enabled": 0}, *LANES[1:]])
    assert stands(policy, store, after) == (None, "codex-2")


def test_c6_3_a_chosen_model_that_loses_every_lane_is_evaluated_again(policy):
    """`sweep` at `standard` walks Terra, then Astra. Terra closes on every lane before the
    reservation: an evaluation now goes on to Astra, whose lanes this decision never
    judged, so the check refuses and admission evaluates again, off the lock."""
    sweep = {**JOB, "pinned_model": None, "task": "sweep", "tier": "standard"}
    store = fleet()
    closed = [{"closure_id": n, "lane_id": lane_id, "scope": "gpt-5.6-terra",
               "until_at": capacity._iso(NOW + timedelta(hours=1)), "reason": "provider-limit",
               "clock_source": "reported", "source_event": "x", "created_at": capacity._iso(NOW), "released_at": None}
              for n, lane_id in enumerate(("codex-1", "codex-2", "codex-3"), 1)]
    assert stands(policy, store, with_(store, closures=closed), job=sweep) == ("moved", "codex-1")


# --- a lane's own clock (review of d04b8b3) -------------------------------------------------------

CLAUDE = [{"lane_id": f"claude-{n}", "provider": "claude", "account_key": f"claude:c{n}@example.invalid",
           "credential_ref": f"/c/claude-{n}", "credential_kind": "keychain-token", "home": None, "owner": "v2",
           "enabled": 1, "desktop": 0, "identity_status": None, "label": None} for n in range(1, 7)]


def judged_again(policy, store, after, job=JOB, seconds=1):
    """How many lanes the check judged again, and its verdict."""
    view = view_of(store, NOW)
    decision = scheduler.evaluate(policy, view, job)
    verdict, judged, _ = route_check.still_stands(
        policy, job, decision, view=view, candidates=candidates_of(store["readings"]),
        overridden=store["overridden"], clocks=capacity.lane_horizons(view, reading_ttl_s=120),
        now=NOW + timedelta(seconds=seconds), **now_rows(store, after))
    return verdict, judged


def test_c6_3_readings_ageing_out_on_another_providers_lanes_are_never_looked_at(policy):
    """The review's livelock, in one check: six Claude lanes' readings, fresh at the early
    evaluation, age out before a Codex job's reservation. The fleet's first horizon has
    passed, but no Claude lane is one the decision looks at: nothing is judged again and
    the decision stands. The check used to refuse it (`old`), every try, for as long as
    the Claude lanes' readings kept being refreshed."""
    store = fleet(lanes=[*[dict(row) for row in LANES], *[dict(row) for row in CLAUDE]])
    store["readings"] += [reading(row["lane_id"], .3, 10 + n, observed=NOW - timedelta(seconds=118))
                          for n, row in enumerate(CLAUDE)]
    view = view_of(store, NOW)
    assert capacity.decision_horizon(view, reading_ttl_s=120) < NOW + timedelta(seconds=5)
    assert stands(policy, store, store, seconds=5) == (None, "codex-1")
    assert judged_again(policy, store, store, seconds=5) == (None, 0)


def test_c6_3_a_reading_ageing_out_on_a_lane_the_pin_does_not_name_is_never_looked_at(policy):
    """A job pinned to codex-2 is judged on codex-2 alone: codex-1's and codex-3's
    readings ageing out change nothing it could run on."""
    pinned = {**JOB, "pinned_lane": "codex-2"}
    store = fleet(readings=[reading("codex-1", .2, 1, observed=NOW - timedelta(seconds=118)), reading("codex-2", .5, 2),
                            reading("codex-3", .6, 3, observed=NOW - timedelta(seconds=119))])
    assert stands(policy, store, store, job=pinned, seconds=5) == (None, "codex-2")
    assert judged_again(policy, store, store, job=pinned, seconds=5) == (None, 0)


def test_c6_3_the_chosen_lane_whose_reading_ages_out_is_judged_again(policy):
    """codex-1 ranks first on a reading that resets soonest; the reading ages out before
    the reservation, so codex-1 is unmeasured now and ranks after the measured lanes. The
    check judges codex-1 again at its clock and chooses codex-2, as an evaluation now does,
    instead of refusing."""
    store = fleet(readings=[reading("codex-1", .2, 1, observed=NOW - timedelta(seconds=118), resets_in=600),
                            reading("codex-2", .5, 2), reading("codex-3", .6, 3)])
    assert stands(policy, store, store, seconds=0) == (None, "codex-1")
    assert stands(policy, store, store, seconds=5) == (None, "codex-2")
    assert judged_again(policy, store, store, seconds=5) == (None, 1)


def test_c6_3_a_closure_that_ends_on_a_lane_it_looks_at_opens_that_lane(policy):
    """codex-1 was closed at the early evaluation; its closure ends before the
    reservation. Judged again at the check's clock, it is open and ranks first."""
    closure = {"closure_id": 1, "lane_id": "codex-1", "scope": "account",
               "until_at": capacity._iso(NOW + timedelta(seconds=2)), "reason": "provider-limit",
               "clock_source": "reported", "source_event": "x", "created_at": capacity._iso(NOW), "released_at": None}
    store = fleet(closures=[closure])
    assert stands(policy, store, store, seconds=0) == (None, "codex-2")
    assert stands(policy, store, store, seconds=5) == (None, "codex-1")
    assert judged_again(policy, store, store, seconds=5) == (None, 1)


def test_c6_3_an_override_that_ends_is_judged_on_its_lane_alone(policy):
    """A confirmed reset-credit override held codex-1's reading out, so it ranked as
    unmeasured; the override ends before the reservation, and codex-1, judged again with
    its reading back, ranks first. An override that ends on a Claude lane changes nothing."""
    store = fleet(readings=[reading("codex-1", .2, 1, resets_in=600), reading("codex-2", .5, 2),
                            reading("codex-3", .6, 3)], overridden={"codex-1"})
    assert stands(policy, store, store) == (None, "codex-2")
    assert stands(policy, store, with_(store, overridden=set())) == (None, "codex-1")
    assert judged_again(policy, store, with_(store, overridden=set())) == (None, 1)
    elsewhere = fleet(lanes=[*[dict(row) for row in LANES], dict(CLAUDE[0])], overridden={"claude-1"})
    assert judged_again(policy, elsewhere, with_(elsewhere, overridden=set())) == (None, 0)


def test_c6_3_a_reading_past_its_reset_is_labelled_as_an_evaluation_now_labels_it(policy):
    """Review of d04b8b3 (P3): a `provider` reading already past its reset measures
    nothing and gives no horizon. 119 s old at the early evaluation, it is past the
    120 s TTL at the reservation, where an evaluation now labels it `stale-provider`;
    the check kept `provider`. It now gives every reading as a view built at its clock
    does (label and age), and `check` asserts the whole decision equal."""
    store = fleet(lanes=[dict(LANES[0])],
                  readings=[reading("codex-1", .2, 1, observed=NOW - timedelta(seconds=119), resets_in=-1)])
    assert capacity.lane_horizons(view_of(store, NOW), reading_ttl_s=120) == {}
    result = check(policy, store, JOB, store, 2)
    verdict, _, standing, full = result
    assert verdict is None and standing.chosen_lane == "codex-1"
    assert [row["label"] for row in standing.evaluations[0]["readings"]] == ["stale-provider"]
    assert exact(standing) == exact(full)
