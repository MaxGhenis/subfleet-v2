"""C-9.5, C-11.2, D-CAP1: a model the job's own attempt found at capacity is walked last.

2026-10-13: three `hard` jobs lost hours to gpt-6.1-sol answering "Selected model is at
capacity. Please try a different model." after 4 to 26 minutes of work. Each retry asked the
same model again, on the same lane 60 s later (C-4.5, C-9.5) and then on another of its lanes,
where the refusal was the same. The daemon now hands the route of such a job (no model or lane
pin, no turn, resume or revive: `scheduler.capacity_steerable`) a `capacity_steer`, the model
ids its attempts found at capacity whose steer has not ended, and `scheduler.prepare` walks
those models after the rest of the chain. So the chain's next model that has a candidate takes
the job, and the model at capacity does only when no other can: a steer never leaves a job
waiting. These pin the walk; `tests/fake/test_capacity_steer_daemon.py` and
`tests/e2e/test_capacity_steer_e2e.py` pin how a refusal comes to steer a job.

Invariants, over random stores, policies and jobs (`tests/routing_strategies.py`):
- the steered walk is the unsteered walk reordered: the same models, those not steered first
  in their order, then the steered ones in theirs; no model below the job's tier, none added;
- a job the steer does not apply to (pinned, a turn, a resume, a revive) is routed exactly as
  without it, and so is any job whose steer names no model of its walk;
- differential: the steered decision chooses the first model of its walk that has a candidate,
  and that model's lane, exactly as an evaluation of that model alone (pinned) chooses them,
  with the same candidates and rejections for every model it walked;
- C-6.3: the reservation's check of a steered decision is exactly a fresh evaluation's answer.
"""

from __future__ import annotations

import copy
import dataclasses

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import route_check, scheduler
from subfleet.capacity import build_view
from tests.routing_strategies import BASE_POLICY, commits, exact, policies, route_jobs, stores, view_of
from tests.routing_strategies import NOW as STRATEGY_NOW
from tests.unit.test_route_check import check

NOW = "2026-10-13T07:22:09Z"
UNTIL = "2026-10-13T07:37:09Z"          # the refusal's finish plus MODEL_CAPACITY_STEER_S
ASTRA, OPUS = "gpt-6-astra", "claude-opus-5-5"
SETTINGS = settings(max_examples=400, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large,
                                           HealthCheck.filter_too_much])


@pytest.fixture
def policy():
    """The shipped policy shaped as the live chains were on 2026-10-13: a `hard` review prefers
    the Codex model, then Opus (live: `["sol61", "opus"]`); a `hard` build has the Codex model
    alone (live: `"sol61"`). The reserve rule (C-11.7) is off, as in the other routing cases."""
    loaded = copy.deepcopy(BASE_POLICY)
    loaded["reserve"] = {**loaded.get("reserve", {}), "models": []}
    loaded["chains"]["review"] = ["haiku", "sonnet", ["astra", "opus"], ["astra", "opus"]]
    loaded["chains"]["build"] = ["haiku", "sonnet", "opus", "astra"]
    return loaded


def lane(identity, **changes):
    provider = identity.split("-")[0]
    return {"lane_id": identity, "provider": provider, "account_key": f"{provider}:{identity}@example.com",
            "owner": "v2", "enabled": True, "desktop": False, "home": f"/lanes/{identity}", **changes}


def closure(identity, scope="account"):
    return {"lane_id": identity, "scope": scope, "until_at": "2026-10-13T09:00:00Z",
            "reason": "provider-limit", "clock_source": "reported", "source_event": "fixture"}


def fleet(closures=()):
    return build_view([lane("codex-1"), lane("codex-2"), lane("claude-1"), lane("claude-2")],
                      (), list(closures), (), (), now=NOW)


def job(**changes):
    return {"job_id": "pr198-review-r6", "kind": "dispatch", "task": "review", "tier": "hard",
            "sandbox": "workspace-write", "pinned_model": None, "pinned_lane": None, **changes}


STEER = {ASTRA: UNTIL}


def test_c11_2_a_steered_hard_review_goes_to_the_chains_next_model(policy):
    """D-CAP1, pr198-review-r6: after the refusal the next attempt is Opus, not the same model."""
    assert scheduler.evaluate(policy, fleet(), job()).chosen_model == "astra"
    decision = scheduler.evaluate(policy, fleet(), job(capacity_steer=STEER))
    assert decision.chain == ("opus",)
    assert decision.chosen_model == "opus" and decision.chosen_lane in ("claude-1", "claude-2")
    assert f"astra: at capacity for this job until {UNTIL}, walked last (C-9.5)" in decision.reason


def test_c11_2_with_no_lane_for_the_next_model_the_model_at_capacity_still_takes_the_job(policy):
    """C-9.5: a steer orders the walk and never holds a job: with every Opus lane closed (the
    incident's claude-10 was five-hour limited), the job goes back to the model it left."""
    view = fleet([closure("claude-1"), closure("claude-2")])
    decision = scheduler.evaluate(policy, view, job(capacity_steer=STEER))
    assert decision.chain == ("opus", "astra")
    assert (decision.chosen_model, decision.chosen_lane) == ("astra", "codex-1")
    assert "opus: no candidate lanes after exclusions; promoted" in decision.reason


def test_c11_2_a_chain_of_one_model_is_walked_as_before(policy):
    """D-CAP1, pr181-fix8: a `hard` build's chain is the model at capacity alone; there is
    nothing to steer to, and the job is routed as it was."""
    plain = scheduler.evaluate(policy, fleet(), job(task="build"))
    steered = scheduler.evaluate(policy, fleet(), job(task="build", capacity_steer=STEER))
    assert plain.chain == steered.chain == ("astra",)
    assert (steered.chosen_model, steered.chosen_lane) == (plain.chosen_model, plain.chosen_lane)


@pytest.mark.parametrize("changes", [
    {"pinned_model": "astra"}, {"pinned_lane": "codex-1"}, {"pinned_lane": "codex-1", "pinned_model": "astra"},
    {"kind": "turn"}, {"kind": "resume"}, {"kind": "revive"},
])
def test_c9_5_a_steer_never_moves_a_job_it_does_not_apply_to(policy, changes):
    """C-9.5: a pin is the operator's choice (C-11.2), a turn's failover is its conversation's
    (C-26.7), and a resume or revive continues one native session (C-12.3)."""
    assert not scheduler.capacity_steerable(job(**changes))
    assert exact(scheduler.evaluate(policy, fleet(), job(**changes, capacity_steer=STEER))) == \
        exact(scheduler.evaluate(policy, fleet(), job(**changes)))


def test_c9_5_the_kinds_a_steer_applies_to():
    assert all(scheduler.capacity_steerable(job(kind=kind)) for kind in ("dispatch", "gate-review", "handoff"))


def test_c11_2_a_steer_on_a_model_outside_the_walk_changes_nothing(policy):
    other = {OPUS: UNTIL, "gpt-5.6-terra": UNTIL}
    for spec in (job(task="build"), job(tier="standard", task="build")):
        assert scheduler.prepare(policy, fleet(), {**spec, "capacity_steer": other})["chain"] == \
            scheduler.prepare(policy, fleet(), spec)["chain"]


# --- properties over random stores, policies and jobs ---------------------------------------------

MODEL_IDS = sorted({model["id"] for model in BASE_POLICY["models"].values()})


@st.composite
def steered_cases(draw):
    """A store, a policy and a route job, and the steer its attempts left (model ids, any)."""
    store = draw(stores())
    route = draw(route_jobs(store))
    steer = {model_id: "2026-09-26T12:15:00Z" for model_id in draw(st.lists(st.sampled_from(MODEL_IDS),
                                                                             min_size=1, max_size=3, unique=True))}
    return draw(policies()), store, route, steer


def routes(policy, view, spec):
    """`evaluate`'s decision, or the type of what it raised (C-6.12)."""
    try:
        return scheduler.evaluate(policy, view, spec)
    except route_check.ROUTE_ERRORS as exc:
        return type(exc)


@SETTINGS
@given(steered_cases())
def test_c11_2_a_steer_reorders_the_walk_and_nothing_else(case):
    policy, store, route, steer = case
    view = view_of(store, STRATEGY_NOW)
    try:
        plain = scheduler.prepare(policy, view, route)["chain"]
    except route_check.ROUTE_ERRORS:
        return
    walk = scheduler.prepare(policy, view, {**route, "capacity_steer": steer})["chain"]
    if not scheduler.capacity_steerable(route):
        assert walk == plain
        return
    steered = [short for short in plain if policy["models"][short]["id"] in steer]
    assert walk == [short for short in plain if short not in steered] + steered
    assert sorted(walk) == sorted(plain)


@SETTINGS
@given(steered_cases())
def test_c11_2_the_steered_decision_is_the_first_model_of_its_walk_with_a_candidate(case):
    """Differential: the steered evaluation against each model evaluated alone (pinned)."""
    policy, store, route, steer = case
    view = view_of(store, STRATEGY_NOW)
    plain = routes(policy, view, route)
    decision = routes(policy, view, {**route, "capacity_steer": steer})
    if isinstance(plain, type) or not scheduler.capacity_steerable(route):
        if isinstance(plain, type):
            assert decision is plain
        else:
            assert exact(decision) == exact(plain)
        return
    walk = scheduler.prepare(policy, view, {**route, "capacity_steer": steer})["chain"]
    expected = (None, None)
    for index, short in enumerate(walk):
        alone = scheduler.evaluate(policy, view, {**route, "pinned_model": short})
        assert index < len(decision.evaluations), (walk, decision.chain)
        walked = decision.evaluations[index]
        assert walked["model"] == short
        assert walked["candidates"] == alone.evaluations[0]["candidates"]
        assert walked["rejections"] == alone.evaluations[0]["rejections"]
        if alone.chosen_lane:
            expected = (alone.chosen_lane, short)
            break
    assert (decision.chosen_lane, decision.chosen_model) == expected
    assert decision.chain == tuple(walk[:len(decision.evaluations)])
    # Stated directly: while a model of the walk that is not at capacity can take the job, the
    # job never goes to one that is; it does only when none can (a steer never holds a job).
    free = [short for short in walk if policy["models"][short]["id"] not in steer
            and scheduler.evaluate(policy, view, {**route, "pinned_model": short}).chosen_lane]
    if free:
        assert decision.chosen_model == free[0]
    elif plain.chosen_lane:
        assert decision.chosen_lane is not None


@SETTINGS
@given(steered_cases(), st.data())
def test_c6_3_the_reservation_check_of_a_steered_decision_is_a_fresh_evaluation(case, data):
    """C-6.3: `route_check.still_stands` over a steered job returns exactly what `evaluate` over
    the rows the reservation sees returns (asserted in `check`), and refuses only as for any job."""
    policy, store, route, steer = case
    after, seconds = data.draw(commits(store))
    result = check(policy, store, {**route, "capacity_steer": steer}, after, seconds)
    if result is None:
        return
    verdict, decidable, decision, full = result
    assert (verdict is None) == decidable
    if verdict is None:
        assert scheduler.verdict_signature(decision) == scheduler.verdict_signature(full)


def test_the_decision_shape_is_unchanged():
    """A steer adds no field to a decision record: `why`, `runs show` and the importer read
    records written before it (C-11.5)."""
    assert [field.name for field in dataclasses.fields(scheduler.Decision)] == \
        ["chain", "evaluations", "chosen_lane", "chosen_model", "reason", "policy_hash"]
