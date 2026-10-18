"""C-12.9: explicit MCP jobs stay on providers that honor their named servers."""

import json

import pytest

from subfleet.scheduler import RouteError, evaluate, pin_provider
from tests.unit.test_scheduler import job, lane, policy, reading, view


@pytest.mark.parametrize("record", [["selected"], '["selected"]'])
def test_c12_9_opt_in_removes_codex_fallback_from_routing(policy, record):
    lanes = view([lane("codex-1")], [reading("codex-1")])
    opted = evaluate(policy, lanes, job(sandbox="workspace-write", mcp_servers=record))
    assert opted.chain == ("opus",)
    assert opted.chosen_lane is None
    default = evaluate(policy, lanes, job(sandbox="workspace-write"))
    assert default.chain == ("opus", "astra")
    assert default.chosen_lane == "codex-1"


def test_c12_9_policy_change_cannot_silently_drop_job_opt_in(policy):
    policy["chains"]["build"] = ["terra", "terra", "astra", "astra"]
    requested = job(task="build", sandbox="workspace-write", mcp_servers=json.dumps(["selected"]))
    with pytest.raises(RouteError, match="chain has no Claude model") as error:
        evaluate(policy, view([lane("codex-1")], [reading("codex-1")]), requested)
    assert error.value.policy_dependent


def test_c12_9_pin_provider_follows_filtered_mcp_chain(policy):
    policy["chains"]["build"] = ["terra", "terra", "astra", "opus"]
    assert pin_provider(policy, job(task="build", mcp_servers=["selected"])) == "claude"
    assert pin_provider(policy, job(task="build")) == "codex"


def test_c12_9_explicit_codex_pin_refuses_mcp_jobs(policy):
    with pytest.raises(RouteError, match="chain has no Claude model"):
        evaluate(policy, view([lane("codex-1")], [reading("codex-1")]),
                 job(pinned_model="astra", mcp_servers=["selected"]))
