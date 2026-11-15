"""D-REVIEW-FAMILY1: independent reviewers and unchanged ordinary routing."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from hypothesis import HealthCheck, example, given, settings, strategies as st
import pytest

from subfleet import scheduler
from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError, load_policy, routing_chain
from tests.routing_strategies import NOW, policies, route_jobs, stores, view_of
from tests.unit.test_policy_tier_preferences import CHAINS
from tests.unit.test_scheduler import job, lane, view


PROPERTY = settings(max_examples=150, deadline=None, derandomize=True, database=None,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


def baseline_routing():
    """The changed functions frozen from A2, with unchanged lane judging shared."""
    path = Path(__file__).parents[1] / "fixtures/review_family_before.py.txt"
    source = path.read_bytes()
    assert hashlib.sha256(source).hexdigest() == "7ec0a2624af36014eba175e5b82a3e628b07a46630572683dd050a8dc1bac379"
    namespace = dict(vars(scheduler))
    exec(compile(source, str(path), "exec"), namespace)
    return namespace


def outcome(function, *args):
    try:
        result = function(*args)
        return asdict(result) if hasattr(result, "chosen_lane") else result
    except (PolicyError, ValueError, KeyError) as error:
        return type(error).__name__, str(error)


@PROPERTY
@example(chain=["haiku"] * 4, tier=3, family="claude")
@example(chain=["astra"] * 4, tier=3, family="codex")
@given(chain=CHAINS, tier=st.integers(0, 3), family=st.sampled_from(["claude", "codex"]))
def test_i1_every_tier_family_and_chain_chooses_the_other_provider(chain, tier, family):
    policy = load_policy(DEFAULT_POLICY_PATH)
    policy["chains"]["review"] = chain
    policy["reserve"]["models"] = []
    spec = job(task="review", tier=policy["tiers"][tier], caller_family=family)
    snapshot = view([lane(), lane("codex-1")])
    decision = scheduler.evaluate(policy, snapshot, spec)
    assert decision.chosen_model is not None
    assert policy["models"][decision.chosen_model]["provider"] != family
    eligible = list(dict.fromkeys(name for entry in chain[tier:]
                                  for name in (entry if isinstance(entry, list) else [entry])
                                  if policy["models"][name]["provider"] != family))
    expected = eligible or [{"claude": "sol61", "codex": "opus"}[family]]
    assert scheduler.prepare(policy, snapshot, spec)["chain"] == expected
    assert scheduler.demand_models(policy, spec) == frozenset(expected)
    assert scheduler.pin_provider(policy, spec) == policy["models"][expected[0]]["provider"]


@pytest.mark.parametrize("family,missing", [("claude", "codex"), ("codex", "claude")])
@pytest.mark.parametrize("tier", ["trivial", "easy", "standard", "hard"])
def test_i2_missing_other_family_waits_and_explicit_permission_allows_fallback(family, missing, tier):
    policy = load_policy(DEFAULT_POLICY_PATH)
    policy["reserve"]["models"] = []
    snapshot = view([lane(f"{family}-1")])
    spec = job(task="review", tier=tier, caller_family=family)
    waiting = scheduler.evaluate(policy, snapshot, spec)
    assert waiting.chosen_lane is None
    assert {row["provider"] for row in waiting.evaluations} == {missing}
    assert f"missing {missing} capacity" in waiting.reason
    # Put an old-chain model of the caller's family in every eligible suffix,
    # so the override demonstrates fallback at each tier for both families.
    policy["chains"]["review"][-1] = "opus" if family == "claude" else "astra"
    allowed = scheduler.evaluate(policy, snapshot, {**spec, "allow_same_family": True})
    assert allowed.chosen_lane == f"{family}-1"
    assert allowed.evaluations[0]["provider"] == missing


@PROPERTY
@given(data=st.data())
def test_i3_null_family_full_decisions_match_a2(data):
    policy = data.draw(policies())
    policy["chains"]["review"] = data.draw(CHAINS)
    store = data.draw(stores())
    spec = data.draw(route_jobs(store))
    spec.update(task="review", tier=data.draw(st.sampled_from(policy["tiers"])), caller_family=None)
    snapshot = view_of(store, NOW)
    before = baseline_routing()
    assert outcome(scheduler.evaluate, policy, snapshot, spec) == outcome(before["evaluate"], policy, snapshot, spec)
    assert outcome(scheduler.demand_models, policy, spec) == outcome(before["demand_models"], policy, spec)
    assert outcome(scheduler.pin_provider, policy, spec) == outcome(before["pin_provider"], policy, spec)


@PROPERTY
@given(data=st.data(), family=st.sampled_from([None, "claude", "codex"]), allow=st.booleans())
def test_i4_other_tasks_full_decisions_match_a2_whatever_the_family(data, family, allow):
    policy = data.draw(policies())
    store = data.draw(stores())
    spec = data.draw(route_jobs(store))
    spec.update(caller_family=family, allow_same_family=allow)
    assert spec.get("task") != "review"
    snapshot = view_of(store, NOW)
    before = baseline_routing()
    assert outcome(scheduler.evaluate, policy, snapshot, spec) == outcome(before["evaluate"], policy, snapshot, spec)
    assert outcome(scheduler.demand_models, policy, spec) == outcome(before["demand_models"], policy, spec)
    assert outcome(scheduler.pin_provider, policy, spec) == outcome(before["pin_provider"], policy, spec)


@pytest.mark.parametrize("family", ["claude", "codex"])
def test_i4_build_keeps_a2_chain_for_every_tier(family):
    policy = load_policy(DEFAULT_POLICY_PATH)
    policy["reserve"]["models"] = []
    snapshot = view([lane(), lane("codex-1")])
    before = baseline_routing()
    for tier in policy["tiers"]:
        spec = job(task="build", tier=tier, caller_family=family)
        assert outcome(scheduler.evaluate, policy, snapshot, spec) == outcome(before["evaluate"], policy, snapshot, spec)


@pytest.mark.parametrize("family,default,override", [("claude", "sol61", "terra"),
                                                     ("codex", "opus", "sonnet")])
def test_i5_absent_policy_key_uses_code_default_and_explicit_key_overrides(tmp_path, family, default, override):
    raw = json.loads(DEFAULT_POLICY_PATH.read_text())
    raw.pop("review_family_defaults")
    raw["chains"]["review"] = ["opus" if family == "claude" else "astra"] * 4
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    before_bytes = path.read_bytes()
    policy = load_policy(path)
    assert "review_family_defaults" not in policy
    assert path.read_bytes() == before_bytes
    for tier in range(4):
        assert routing_chain(policy, "review", tier, caller_family=family) == [default]
    raw["review_family_defaults"] = {family: override}
    path.write_text(json.dumps(raw))
    policy = load_policy(path)
    for tier in range(4):
        assert routing_chain(policy, "review", tier, caller_family=family) == [override]


@pytest.mark.parametrize("defaults", [None, [], {"other": "astra"}, {"claude": "absent"},
                                      {"claude": "opus"}, {"codex": "astra"}])
def test_policy_default_override_must_name_the_other_family(tmp_path, defaults):
    raw = json.loads(DEFAULT_POLICY_PATH.read_text())
    raw["review_family_defaults"] = defaults
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(PolicyError, match="review_family_defaults"):
        load_policy(path)


@pytest.mark.parametrize("family,model", [("claude", "opus"), ("codex", "astra")])
def test_old_same_family_pin_cannot_bypass_the_scheduler_guard(family, model):
    policy = load_policy(DEFAULT_POLICY_PATH)
    policy["reserve"]["models"] = []
    snapshot = view([lane(), lane("codex-1")])
    spec = job(task="review", tier="hard", caller_family=family, pinned_model=model)
    with pytest.raises(scheduler.RouteError, match="requires a .* reviewer"):
        scheduler.evaluate(policy, snapshot, spec)
    assert scheduler.demand_models(policy, spec) == frozenset()
    assert scheduler.evaluate(policy, snapshot, {**spec, "allow_same_family": True}).chosen_model == model


@pytest.mark.parametrize("family,default", [("claude", "sol61"), ("codex", "opus")])
def test_i1_repurposed_default_name_never_selects_the_callers_provider(family, default):
    policy = load_policy(DEFAULT_POLICY_PATH)
    policy.pop("review_family_defaults")
    policy["models"][default]["provider"] = family
    policy["chains"]["review"] = [default] * 4
    snapshot = view([lane(), lane("codex-1")])
    spec = job(task="review", tier="hard", caller_family=family)
    other = "codex" if family == "claude" else "claude"
    with pytest.raises(PolicyError, match=f"missing {other} reviewer"):
        scheduler.evaluate(policy, snapshot, spec)
