"""C-11.2: preference order, upward-only routing, and release/217 differential."""

from dataclasses import asdict
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import scheduler
from subfleet.cli import _format_decision
from subfleet.policy import DEFAULT_POLICY_PATH, flatten_chain, load_policy
from tests.routing_strategies import NOW as ROUTE_NOW, commits, policies, route_jobs, stores, view_of
from tests.unit.test_policy_tier_preferences import CHAINS, MODELS
from tests.unit.test_route_check import check as check_reservation
from tests.unit.test_scheduler import closure, job, lane, reading, view

PROPERTY = settings(max_examples=300, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


@pytest.fixture(scope="module")
def release217():
    # For evaluate, prepare and the model-scope walk are the changed dependencies;
    # freeze them and evaluate from the requested baseline. Lane judging is shared
    # unchanged code. No git executable or old commit is needed to run the test.
    namespace = dict(vars(scheduler))
    fixture = Path(__file__).parents[1] / "fixtures/tier_preferences/release_217_chain_paths.py.txt"
    exec(compile(fixture.read_text(), str(fixture), "exec"), namespace)
    return namespace


@st.composite
def legacy_cases(draw):
    policy = draw(policies())
    policy["tiers"] = list(draw(st.permutations(policy["tiers"])))
    if draw(st.booleans()):
        for model in policy["models"].values():
            model.pop("priority", None)
    # Vary the policies' task chains too, including repeated model names.
    for task in policy["chains"]:
        policy["chains"][task] = draw(st.lists(st.sampled_from(MODELS), min_size=4, max_size=4))
    store = draw(stores())
    return policy, view_of(store, ROUTE_NOW), draw(route_jobs(store))


def outcome(fn, *args):
    try:
        result = fn(*args)
        return asdict(result) if hasattr(result, "chosen_lane") else result["chain"]
    except Exception as error:
        return type(error).__name__, str(error)


@PROPERTY
@given(legacy_cases())
def test_string_chains_match_release217_candidates_and_routed_choice(release217, case):
    policy, snapshot, task = case
    for short in policy["models"]:
        assert scheduler._higher_model_scopes(policy, short) == release217["_higher_model_scopes"](policy, short)
    assert outcome(scheduler.prepare, policy, snapshot, task) == outcome(release217["prepare"], policy, snapshot, task)
    # Compare the entire decision, so model, lane, refusals, promotion and order
    # are checked, including jobs with pins, exclusions, caps and turns.
    assert outcome(scheduler.evaluate, policy, snapshot, task) == outcome(release217["evaluate"], policy, snapshot, task)


@PROPERTY
@given(CHAINS, st.integers(0, 3))
def test_flattening_is_upward_unique_and_preserves_first_preference(chain, tier):
    eligible = [name for entry in chain[tier:] for name in ([entry] if isinstance(entry, str) else entry)]
    expected = []
    for name in eligible:
        if name not in expected:
            expected.append(name)
    flattened = flatten_chain(chain, tier)
    assert flattened == expected
    assert len(flattened) == len(set(flattened))
    lower_only = {name for entry in chain[:tier] for name in ([entry] if isinstance(entry, str) else entry)} - set(eligible)
    assert not lower_only.intersection(flattened)
    policy = load_policy(DEFAULT_POLICY_PATH)
    policy["chains"]["review"] = chain
    task = job(task="review", tier=policy["tiers"][tier])
    assert scheduler.prepare(policy, view([]), task)["chain"] == expected
    assert scheduler.demand_models(policy, task) == frozenset(expected)
    assert scheduler.evaluate(policy, view([]), task).chain == tuple(expected)


def preference_policy():
    policy = load_policy(DEFAULT_POLICY_PATH)
    policy["reserve"]["models"] = []
    policy["chains"]["review"] = ["terra", ["haiku", "sonnet"], ["opus", "astra"], ["astra", "opus"]]
    return policy


@PROPERTY
@given(st.lists(st.sampled_from(MODELS), min_size=3, max_size=3, unique=True), st.integers(1, 3))
def test_closed_first_model_uses_next_in_same_tier_before_higher_tier(names, lane_count):
    first, second, higher = names
    policy = preference_policy()
    policy["chains"]["review"] = [higher, [first, second], higher, higher]
    providers = {policy["models"][name]["provider"] for name in names}
    lanes = [lane(f"{provider}-{n}") for provider in sorted(providers) for n in range(1, lane_count + 1)]
    closed = [closure(row["lane_id"], scope=policy["models"][first]["id"]) for row in lanes]
    snapshot = view(lanes, [reading(row["lane_id"]) for row in lanes], closed)
    decision = scheduler.evaluate(policy, snapshot, job(task="review", tier="easy"))
    assert decision.chosen_model == second
    assert decision.chain == (first, second)
    assert decision.chosen_lane in {row["lane_id"] for row in lanes if row["provider"] == policy["models"][second]["provider"]}


def test_preference_examples_and_pins():
    policy = preference_policy()
    snapshot = view([lane(), lane("codex-1")], [reading("claude-1"), reading("codex-1")])
    assert flatten_chain(policy["chains"]["review"], 1) == ["haiku", "sonnet", "opus", "astra"]
    assert flatten_chain(["haiku", ["haiku", "sonnet"], ["sonnet", "opus"]], 1) == ["haiku", "sonnet", "opus"]
    assert scheduler.evaluate(policy, snapshot, job(task="review", tier="easy")).chosen_model == "haiku"
    assert scheduler.evaluate(policy, snapshot, job(task="review", tier="standard")).chosen_model == "opus"
    assert scheduler.evaluate(policy, snapshot, job(task="review", tier="hard")).chosen_model == "astra"
    assert scheduler.evaluate(policy, snapshot, job(task="review", tier="easy", pinned_model="astra")).chain == ("astra",)
    assert scheduler.evaluate(policy, snapshot, job(task="review", tier="easy", pinned_lane="claude-1")).chain == ("haiku",)
    assert scheduler.pin_provider(policy, job(task="review", tier="easy")) == "claude"


def test_standing_refusals_check_every_preferred_model():
    policy = preference_policy()
    task = job(task="review", tier="standard")
    # Missing Claude capacity must not hide an available Codex fallback.
    snapshot = view([lane("codex-1")])
    assert scheduler.unadmittable(policy, snapshot, task, memo={}) is None
    assert scheduler.refused_for_good(policy, scheduler.evaluate(policy, snapshot, task), task, snapshot["lanes"]) is None
    snapshot = view([lane(enabled=False), lane("codex-1", enabled=False)])
    assert scheduler.unadmittable(policy, snapshot, task, memo={}) == ["disabled"]
    assert scheduler.refused_for_good(policy, scheduler.evaluate(policy, snapshot, task), task, snapshot["lanes"]) == ["disabled"]


def test_mcp_filter_and_model_scope_readers_handle_preferences():
    policy = preference_policy()
    policy["chains"]["review"][2] = ["astra", "opus"]
    task = job(task="review", tier="standard", mcp_servers=["docs"])
    assert scheduler.prepare(policy, view([]), task)["chain"] == ["opus"]
    assert scheduler.pin_provider(policy, task) == "claude"
    for model in policy["models"].values():
        model.pop("priority", None)
    assert policy["models"]["sonnet"]["id"] in scheduler._higher_model_scopes(policy, "haiku")
    assert policy["models"]["opus"]["id"] in scheduler._higher_model_scopes(policy, "haiku")


def test_legacy_stranding_keeps_a_model_repeated_after_the_current_model():
    policy = preference_policy()
    policy["chains"] = {"review": ["opus", "haiku", "opus", "sonnet"]}
    for model in policy["models"].values():
        model.pop("priority", None)
    assert scheduler._higher_model_scopes(policy, "haiku") == {
        policy["models"]["opus"]["id"], policy["models"]["sonnet"]["id"]}


def test_why_renders_flattened_walk():
    policy = preference_policy()
    decision = scheduler.evaluate(policy, view([]), job(task="review", tier="easy"))
    rendered = _format_decision(asdict(decision))
    assert rendered.startswith("chain: haiku → sonnet → opus → astra\n")
    assert "terra" not in rendered


@settings(max_examples=150, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(st.data())
def test_reservation_recheck_matches_fresh_routing_for_mixed_chains(data):
    policy = data.draw(policies())
    for task in policy["chains"]:
        policy["chains"][task] = data.draw(CHAINS)
    store = data.draw(stores())
    task = data.draw(route_jobs(store))
    after, seconds = data.draw(commits(store))
    result = check_reservation(policy, store, task, after, seconds)
    if result is not None:
        verdict, decidable, _, _ = result
        assert (verdict is None) == decidable
