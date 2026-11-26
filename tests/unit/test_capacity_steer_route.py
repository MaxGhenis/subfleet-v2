"""C-9.5, C-11.2, D-CAP1: a live capacity steer excludes the refused model.

An unpinned job takes the first admissible unsteered model in chain order, or
waits for capacity. Pins and session continuations keep their existing routing.
Properties also check admission against reservation-time evaluation (C-6.3).
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
SETTINGS = settings(max_examples=40, deadline=None, derandomize=True,
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
    assert f"astra: at capacity for this job until {UNTIL}, skipped by capacity steer (C-9.5)" in decision.reason


def test_c11_2_with_no_lane_for_the_next_model_the_job_waits(policy):
    view = fleet([closure("claude-1"), closure("claude-2")])
    decision = scheduler.evaluate(policy, view, job(capacity_steer=STEER))
    assert decision.chain == ("opus",)
    assert (decision.chosen_model, decision.chosen_lane) == (None, None)
    assert f"astra: at capacity for this job until {UNTIL}, skipped by capacity steer" in decision.reason
    assert scheduler.waiting_metadata(decision, now=NOW)["wait_reason"] == "capacity"


def test_c11_2_a_chain_of_one_steered_model_waits(policy):
    decision = scheduler.evaluate(policy, fleet(), job(task="build", capacity_steer=STEER))
    assert decision.chain == ()
    assert (decision.chosen_model, decision.chosen_lane) == (None, None)
    assert UNTIL in decision.reason and "capacity steer" in decision.reason


def test_c9_5_an_expired_steer_does_not_exclude_the_model(policy):
    view = {**fleet(), "now": UNTIL}
    assert scheduler.evaluate(policy, view, job(capacity_steer=STEER)).chosen_model == "astra"


def test_c9_5_a_capacity_wait_rechecks_at_the_steers_end(policy):
    now = "2026-10-13T07:37:00Z"
    decision = scheduler.evaluate(policy, {**fleet(), "now": now}, job(task="build", capacity_steer=STEER))
    waiting = scheduler.waiting_metadata(decision, now=now, rechecks=20, capacity_steer=STEER)
    assert waiting == {"wait_reason": "capacity", "next_check_at": UNTIL}


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


@pytest.mark.parametrize("tier,expected", [("hard", ["astra"]), ("standard", ["opus", "astra"])])
def test_c11_2_a_steer_on_a_model_outside_the_walk_changes_nothing(policy, tier, expected):
    """Below-tier Sonnet and off-chain Terra cannot affect either eligible walk."""
    other = {policy["models"]["sonnet"]["id"]: UNTIL, "gpt-5.6-terra": UNTIL}
    spec = job(tier=tier, task="build")
    assert scheduler.prepare(policy, fleet(), spec)["chain"] == expected
    assert scheduler.prepare(policy, fleet(), {**spec, "capacity_steer": other})["chain"] == expected
    assert exact(scheduler.evaluate(policy, fleet(), {**spec, "capacity_steer": other})) == \
        exact(scheduler.evaluate(policy, fleet(), spec))


def test_c11_2_a_steer_inside_the_upward_walk_changes_routing(policy):
    """A standard build includes hard-tier Astra after Opus, so steering Opus moves it."""
    spec = job(tier="standard", task="build")
    assert scheduler.prepare(policy, fleet(), spec)["chain"] == ["opus", "astra"]
    assert scheduler.evaluate(policy, fleet(), spec).chosen_model == "opus"
    steered = {**spec, "capacity_steer": {OPUS: UNTIL}}
    assert scheduler.prepare(policy, fleet(), steered)["chain"] == ["astra"]
    assert scheduler.evaluate(policy, fleet(), steered).chosen_model == "astra"


@pytest.mark.parametrize("steer", [{ASTRA: UNTIL, OPUS: UNTIL}, {OPUS: UNTIL, ASTRA: UNTIL}])
def test_c11_2_multiple_steers_keep_policy_order_regardless_of_map_order(policy, steer):
    """Every live steer excludes its model, regardless of insertion order."""
    spec = job(tier="standard", task="build", capacity_steer=steer)
    assert scheduler.prepare(policy, fleet(), spec)["chain"] == []
    assert scheduler.evaluate(policy, fleet(), spec).chosen_model is None


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
def test_c11_2_a_steer_excludes_live_models_and_nothing_else(case):
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
    assert walk == [short for short in plain if short not in steered]


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
    assert decision.chosen_model is None or policy["models"][decision.chosen_model]["id"] not in steer



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


@SETTINGS
@given(chain=st.lists(st.sampled_from(sorted(BASE_POLICY["models"])), min_size=1, unique=True),
       admissible=st.sets(st.sampled_from(("codex-1", "codex-2", "claude-1", "claude-2"))),
       steer=st.dictionaries(st.sampled_from(MODEL_IDS),
                             st.sampled_from((UNTIL, "2026-10-13T07:20:00Z"))))
def test_c9_5_any_chain_and_admissible_lanes_choose_first_unsteered_or_wait(chain, admissible, steer):
    """Independent oracle over arbitrary chains, lane availability and live/expired steers."""
    policy = copy.deepcopy(BASE_POLICY)
    policy["reserve"] = {**policy.get("reserve", {}), "models": []}
    policy["chains"]["review"][-1] = chain
    view = build_view([lane(identity, enabled=identity in admissible)
                       for identity in ("codex-1", "codex-2", "claude-1", "claude-2")],
                      (), (), (), (), now=NOW)
    live = {model_id for model_id, until in steer.items() if until > NOW}
    expected = next((short for short in chain
                     if policy["models"][short]["id"] not in live
                     and any(identity.startswith(policy["models"][short]["provider"] + "-")
                             for identity in admissible)), None)
    decision = scheduler.evaluate(policy, view, job(capacity_steer=steer))
    assert decision.chosen_model == expected
    assert decision.chosen_model is None or policy["models"][decision.chosen_model]["id"] not in live
    if expected is None:
        assert decision.chosen_lane is None
        assert scheduler.waiting_metadata(decision, now=NOW)["wait_reason"] == "capacity"
