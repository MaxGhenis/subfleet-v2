"""Conditional policy authority is resolved before shell routing (C-11.1)."""

import pytest

from subfleet import scheduler
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from tests.unit.test_scheduler import lane


@pytest.fixture
def policy():
    value = load_policy(DEFAULT_POLICY_PATH)
    value["chains"]["review"] = ["opus"] * len(value["tiers"])
    value["reserve"]["models"] = []
    return value


CONTEXTS = [
    ({}, "read-only"),
    ({"workdir_head": "a" * 40}, "workspace-write"),
    ({"workdir_head": "a" * 40, "in_place": True}, "read-only"),
    ({"workdir_head": "a" * 40, "isolated_review": True}, "read-only"),
    ({"workdir_head": "a" * 40, "kind": "gate-review"}, "read-only"),
    ({"sandbox": "read-only", "workdir_head": "a" * 40}, "read-only"),
    ({"sandbox": "workspace-write"}, "workspace-write"),
]


@pytest.mark.parametrize("source", [None, "policy"])
@pytest.mark.parametrize("context,expected", CONTEXTS)
def test_demand_models_resolves_policy_permissions(policy, source, context, expected):
    job = {"task": "review", "needs_shell": True, "sandbox": source, **context}
    assert scheduler.demand_models(policy, job) == (
        frozenset({"opus"}) if expected == "workspace-write" else frozenset())


@pytest.mark.parametrize("source", [None, "policy"])
@pytest.mark.parametrize("context,expected", CONTEXTS)
def test_lane_rejection_resolves_policy_permissions(policy, source, context, expected):
    job = {"task": "review", "needs_shell": True, "sandbox": source, **context}
    target = lane()
    setup = scheduler.prepare(policy, {"lanes": [target]}, job)
    reasons, _ = scheduler.judge_lane(setup, "opus", target, [], [], in_flight=0, unavailable={})
    shell_reasons = [reason for reason in reasons if reason.startswith("needs a shell:")]
    assert shell_reasons == ([] if expected == "workspace-write" else [
        "needs a shell: opus runs read-only jobs without one"])


@pytest.mark.parametrize("source", [None, "policy"])
@pytest.mark.parametrize("context,expected", CONTEXTS)
def test_model_reason_resolves_policy_permissions(policy, source, context, expected):
    job = {"task": "review", "needs_shell": True, "sandbox": source, **context}
    setup = {"policy": policy, "job": job, "chain": ["opus"], "pin": None}
    assert scheduler.model_reason(setup, 0, "opus", [], {}) == (
        "opus: no candidate lanes after exclusions" if expected == "workspace-write" else
        "needs a shell: opus runs read-only jobs without one")
