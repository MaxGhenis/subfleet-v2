"""D-REVIEW-FAMILY1 fix round 1: Sol first, checked defaults, session-id kinds.

Each test names the review finding it answers (review-a3-r1.md).
"""
from __future__ import annotations

import copy
import json
from datetime import timedelta
from pathlib import Path

import pytest

from subfleet import cli, policy as policy_module, scheduler
from subfleet.contracts import Exit
from subfleet.policy import load_policy, routing_chain

from tests.routing_strategies import BASE_POLICY, NOW, view_of
from test_route_check import JOB, check, fleet, reading, with_

DEFAULT_POLICY = Path(policy_module.__file__).with_name("default_policy.json")
TIERS = ("trivial", "easy", "standard", "hard")
#: d2937 item 3, as the hub ruled it: Sol first for a Claude caller, then the
#: review chain's other Codex models; Opus first for a Codex caller, then the
#: chain's other Claude models, each from the job's tier upward.
EXPECTED = {
    "claude": {"trivial": ["sol61", "astra"], "easy": ["sol61", "astra"],
               "standard": ["sol61", "astra"], "hard": ["sol61", "astra"]},
    "codex": {"trivial": ["opus", "haiku", "sonnet"], "easy": ["opus", "sonnet"],
              "standard": ["opus"], "hard": ["opus"]},
    None: {"trivial": ["haiku", "sonnet", "opus", "astra"], "easy": ["sonnet", "opus", "astra"],
           "standard": ["opus", "astra"], "hard": ["astra"]},
}


def lanes_up():
    """One open lane per family, so `evaluate` stops at the chain's first model."""
    def lane(lane_id, provider):
        return {"lane_id": lane_id, "provider": provider, "account_key": f"{provider}:{lane_id}@example.invalid",
                "credential_ref": f"/c/{lane_id}", "credential_kind": "keychain-token", "home": None,
                "owner": "v2", "enabled": 1, "desktop": 0, "identity_status": None, "label": None}
    return fleet(lanes=[lane("claude-1", "claude"), lane("codex-1", "codex")],
                 readings=[reading("claude-1", .1, 1), reading("codex-1", .1, 2)], jobs=[])


def review_job(family, tier, **changes):
    return {**JOB, "job_id": f"review-{family}-{tier}", "task": "review", "tier": tier,
            "pinned_model": None, "caller_family": family, **changes}


def test_p2_sol_first_routing_matrix(capsys):
    """P2 policy.py:90: the shipped policy's review chain for each caller family, and
    the model `evaluate` chooses with both families' lanes up."""
    shipped = load_policy(DEFAULT_POLICY)
    assert shipped["_review_family_warnings"] == []
    shipped["reserve"]["models"] = []       # no fable reserve on the synthetic Claude lane
    view = view_of(lanes_up(), NOW)
    rows = []
    for tier_index, tier in enumerate(TIERS):
        for family in ("claude", "codex", None):
            chain = routing_chain(shipped, "review", tier_index, caller_family=family)
            chosen = scheduler.evaluate(shipped, view, review_job(family, tier)).chosen_model
            rows.append(f"| {tier} | {family or 'unknown'} | {' -> '.join(chain)} | {chosen} |")
            assert chain == EXPECTED[family][tier], (tier, family)
            assert chosen == chain[0]
    with capsys.disabled():
        print("\n| Tier | Caller | Review chain | Chosen |\n| --- | --- | --- | --- |\n" + "\n".join(rows))


def test_p2_astra_is_reached_only_when_sol_cannot_take_the_job():
    """P2 policy.py:90: with Sol's lane closed to it, a Claude caller's review walks on to Astra."""
    shipped = load_policy(DEFAULT_POLICY)
    store = lanes_up()
    store["closures"] = [{"closure_id": 1, "lane_id": "codex-1", "scope": shipped["models"]["sol61"]["id"],
                          "until_at": (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                          "reason": "provider-limit", "clock_source": "reported", "source_event": "x",
                          "created_at": NOW.isoformat().replace("+00:00", "Z"), "released_at": None}]
    decision = scheduler.evaluate(shipped, view_of(store, NOW), review_job("claude", "trivial"))
    assert decision.chain == ("sol61", "astra") and decision.chosen_model == "astra"


def policy_without_codex_reviewer():
    """An operator policy with no `review_family_defaults` key, no `sol61`, and a
    review chain with no Codex model: Claude callers have no reviewer at all."""
    loaded = copy.deepcopy(BASE_POLICY)
    loaded.pop("review_family_defaults", None)
    del loaded["models"]["sol61"]
    loaded["chains"]["review"] = ["haiku", "sonnet", "opus", "opus"]
    return loaded


def test_p2_code_defaults_are_checked_at_load_and_a_policy_without_a_reviewer_loads(tmp_path):
    """P2 policy.py:77/:560: the code default `sol61` gets the explicit key's checks;
    a policy that cannot serve it still loads, with a warning naming the family."""
    raw = json.loads(DEFAULT_POLICY.read_text())
    raw.pop("review_family_defaults")
    del raw["models"]["sol61"]
    raw["chains"]["review"] = ["haiku", "sonnet", "opus", "opus"]
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    loaded = load_policy(path)
    [warning] = loaded["_review_family_warnings"]
    assert "review_family_defaults.claude" in warning and "'sol61'" in warning
    assert "no codex reviewer" in warning and "wait" in warning
    assert policy_module.review_family_defaults(loaded) == {"claude": None, "codex": "opus"}
    assert all(routing_chain(loaded, "review", index, caller_family="claude") == [] for index in range(4))
    # Codex callers still get their reviewer; nothing else changes.
    assert routing_chain(loaded, "review", 2, caller_family="codex") == ["opus"]
    assert path.read_bytes() == json.dumps(raw).encode()


def test_p2_a_policy_without_sol_but_with_astra_routes_claude_callers_to_astra():
    loaded = copy.deepcopy(BASE_POLICY)
    loaded.pop("review_family_defaults", None)
    del loaded["models"]["sol61"]
    assert policy_module.review_family_warnings(loaded) == []
    assert [routing_chain(loaded, "review", index, caller_family="claude") for index in range(4)] == [["astra"]] * 4


def test_p2_explicit_same_family_or_unknown_default_still_fails_load(tmp_path):
    for value, message in (("opus", "other model family"), ("missing-model", "unknown model")):
        raw = json.loads(DEFAULT_POLICY.read_text())
        raw["review_family_defaults"] = {"claude": value}
        path = tmp_path / f"{value}.json"
        path.write_text(json.dumps(raw))
        with pytest.raises(policy_module.PolicyError, match=message):
            load_policy(path)


@pytest.mark.parametrize("tier", TIERS)
def test_p2_a_review_with_no_other_family_reviewer_waits_naming_the_family(tier):
    """P2: never `pinned_model: unknown model 'sol61'`, never a same-family fallback."""
    loaded = policy_without_codex_reviewer()
    decision = scheduler.evaluate(loaded, view_of(lanes_up(), NOW), review_job("claude", tier))
    assert decision.chosen_lane is None and decision.chain == ()
    assert "waiting for other-family reviewer: policy names no codex reviewer" in decision.reason
    assert "sol61" not in decision.reason
    assert scheduler.demand_models(loaded, review_job("claude", tier)) == frozenset()


@pytest.mark.parametrize("allow", [False, True])
@pytest.mark.parametrize("family,lane", [("claude", "claude-1"), ("codex", "codex-1")])
def test_p3_a_same_family_lane_pin_is_a_review_same_family_refusal(family, lane, allow):
    """P3 scheduler.py:822: a lane pin of the caller's family names `review-same-family`;
    `--allow-same-family` does not lift a bare lane pin."""
    shipped = load_policy(DEFAULT_POLICY)
    job = review_job(family, "standard", pinned_lane=lane, allow_same_family=allow)
    with pytest.raises(scheduler.RouteError, match="^review-same-family: .*needs -m naming"):
        scheduler.evaluate(shipped, view_of(lanes_up(), NOW), job)


def test_p3_same_family_lane_pin_with_a_same_family_model_and_consent_runs():
    shipped = load_policy(DEFAULT_POLICY)
    shipped["reserve"]["models"] = []
    job = review_job("claude", "standard", pinned_lane="claude-1", pinned_model="opus", allow_same_family=True)
    decision = scheduler.evaluate(shipped, view_of(lanes_up(), NOW), job)
    assert (decision.chosen_lane, decision.chosen_model) == ("claude-1", "opus")


def test_p3_reservation_recheck_keeps_the_family_naming_wait_reason():
    """P3 route_check.py:312: when the recheck leaves an attributed review no lane,
    its reason still names the missing family."""
    loaded = copy.deepcopy(BASE_POLICY)
    loaded["reserve"] = {**loaded.get("reserve", {}), "models": []}
    loaded["caps"]["max_active_attempts"] = 1
    job = review_job("claude", "standard")
    store = fleet()
    after = with_(store, attempts=[{"attempt_id": "busy/a1", "job_id": "busy", "lane_id": "codex-3",
                                    "state": "reserved"}])
    verdict, _, standing, full = check(loaded, store, job, after, 1)
    assert standing.chosen_lane is None
    assert "waiting for other-family reviewer: missing codex capacity" in standing.reason
    assert standing.reason == full.reason


@pytest.mark.parametrize("env,family,kind", [
    ({"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "synthetic-claude", "CLAUDE_PID": "4242"}, None, "claude"),
    ({"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "synthetic-claude", "CLAUDE_PID": "4242"}, "codex", "claude"),
    ({"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "synthetic-claude", "CLAUDE_PID": "4242",
      "SUBFLEET_CALLER_FAMILY": "codex"}, None, "claude"),
    ({"CODEX_THREAD_ID": "synthetic-thread"}, None, "codex"),
    ({"CODEX_THREAD_ID": "synthetic-thread"}, "claude", "codex"),
    ({"SUBFLEET_SESSION_ID": "synthetic-subfleet"}, "codex", None),
])
def test_p2_session_kind_follows_the_marker_never_the_declared_family(env, family, kind):
    """P2 daemon.py:3989: the kind is the harness that supplied the id."""
    assert cli.session_kind(env, family=family) == kind
    if kind == "claude":
        # A Claude session id keeps its process marker, so the writer check can identify it.
        assert cli.caller_pid(env, family=family) == 4242


def test_p2_mixed_markers_give_a_kind_only_when_ancestry_agrees(monkeypatch):
    both = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "synthetic-claude",
            "CODEX_THREAD_ID": "synthetic-thread"}
    monkeypatch.setattr(cli, "_ancestor_caller_family", lambda: None)
    assert cli.session_source(both, family="codex") == ("synthetic-thread", None)
    monkeypatch.setattr(cli, "_ancestor_caller_family", lambda: "codex")
    assert cli.session_source(both, family="codex") == ("synthetic-thread", "codex")
    monkeypatch.setattr(cli, "_ancestor_caller_family", lambda: "claude")
    assert cli.session_source(both, family="codex") == ("synthetic-thread", None)


@pytest.mark.parametrize("marker", ["SUBFLEET_JOB", "SUBFLEET_ATTEMPT", "SUBFLEET_TURN_JOB"])
def test_p2_cli_refuses_same_family_consent_inside_a_job(daemon, root, workdir, monkeypatch, capsys, marker):
    """P2 cli.py:3660: consent is refused from inside a Subfleet job, before any submit."""
    server = daemon({"submit": lambda request: {"job_id": "unused", "created": True},
                     "capabilities": lambda request: {"capabilities": ["review.subject.v1"]}})
    monkeypatch.setenv(marker, "20261117-000000-synthetic-parent")
    assert cli.main(["run", "--task", "review", "--tier", "standard", "--caller-family", "codex",
                     "-m", "astra", "--allow-same-family", "-d", "-C", str(workdir),
                     "synthetic review"]) == Exit.REFUSED
    assert "submit" not in server.ops()
    err = capsys.readouterr().err
    assert "--allow-same-family is refused inside a Subfleet job" in err and marker in err
