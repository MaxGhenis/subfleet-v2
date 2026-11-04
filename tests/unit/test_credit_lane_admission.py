"""C-31.1, C-31.3, C-31.5, C-31.6: the credit lane in admission, policy and launches.

Invariant 1 (credit keys only), 3 (one credit lane, at admission) and 4
(fallback only when the credit is gone), with the C-11.4 exemption, the slot
cap, the `claude_api` policy section and the `-api` aliases.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import scheduler
from subfleet.adapters.claude_api import ClaudeApiAdapter
from subfleet.contracts import Credential, JobSpec, Lane, LaneOwner, Launch, Sandbox
from subfleet.daemon import Daemon
from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError, lane_slot_cap, load_policy
from subfleet.provider_env import ProviderEnvironment
from tests.unit.test_scheduler import closure, job, lane, reading, view

POLICY = load_policy(DEFAULT_POLICY_PATH)
KEY = "sk-ant-api03-" + "A1b2C3d4" * 10 + "-_Zz"
DAEMON_KEY = "sk-ant-api03-" + "D" * 80          # the daemon's own metered key, never a launch's
ORG = "00000000-0000-4000-8000-00000000000a"
TWINS = {"opus-api": "opus", "sonnet-api": "sonnet", "haiku-api": "haiku"}


def policy_with(chain, **changes):
    policy = copy.deepcopy(POLICY)
    policy["reserve"]["models"] = []
    policy["chains"]["review"] = [chain] * len(policy["tiers"])
    policy.update(changes)
    return policy


def credit_lane_row(identity="claude-api-1", **changes):
    fields = {"account_key": "claude-api:plan@example.org", "credential_ref": "claude-api-plan@example.org",
              "credential_kind": "keychain-token", "home": None, **changes}
    return lane(identity, **fields)


# --- C-31.6: aliases --------------------------------------------------------------------------

def test_the_api_aliases_are_their_claude_twins_on_the_credit_provider():
    raw = json.loads(DEFAULT_POLICY_PATH.read_text())
    for alias, twin in TWINS.items():
        model, other = raw["models"][alias], raw["models"][twin]
        assert model["provider"] == "claude-api" and other["provider"] == "claude"
        assert model.get("effort") == other.get("effort") and model.get("priority") == other.get("priority")
    assert {raw["models"][alias]["id"] for alias in TWINS} == {"claude-opus-5-5", "claude-sonnet-5-5",
                                                               "claude-haiku-5-5"}
    # No chain names an alias: putting them first is Max's call (cos add-decision).
    named = json.dumps(raw["chains"]) + json.dumps(raw.get("fallback"))
    assert not any(alias in named for alias in TWINS)


# --- C-31.4, C-31.2: the `claude_api` section ---------------------------------------------------

def test_the_shipped_claude_api_section():
    assert POLICY["claude_api"] == {"max_usd_per_attempt": 25, "max_turns_per_attempt": 400, "denied_org_ids": []}


@pytest.mark.parametrize("section,key", [
    ([], "claude_api"), ({"max_usd_per_attempt": 0}, "claude_api.max_usd_per_attempt"),
    ({"max_usd_per_attempt": True}, "claude_api.max_usd_per_attempt"),
    ({"max_usd_per_attempt": "25"}, "claude_api.max_usd_per_attempt"),
    ({"max_turns_per_attempt": 2.5}, "claude_api.max_turns_per_attempt"),
    ({"max_turns_per_attempt": 0}, "claude_api.max_turns_per_attempt"),
    ({"denied_org_ids": "org"}, "claude_api.denied_org_ids"), ({"denied_org_ids": [1]}, "claude_api.denied_org_ids"),
    ({"denied_org_ids": [""]}, "claude_api.denied_org_ids"), ({"denied_org_ids": [" a"]}, "claude_api.denied_org_ids"),
    ({"denied_org_ids": None}, "claude_api.denied_org_ids"), ({"other": 1}, "claude_api.other")])
def test_the_claude_api_section_refuses_anything_else(tmp_path, section, key):
    raw = json.loads(DEFAULT_POLICY_PATH.read_text())
    raw["claude_api"] = section
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(PolicyError) as caught:
        load_policy(path)
    assert caught.value.key == key


def test_a_policy_without_the_section_loads_and_the_adapter_keeps_its_defaults(tmp_path):
    raw = json.loads(DEFAULT_POLICY_PATH.read_text())
    raw.pop("claude_api")
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert "claude_api" not in load_policy(path)
    assert ClaudeApiAdapter()._limits() == (25.0, 400)


# --- C-31.5: slot cap and the C-11.4 exemption -------------------------------------------------

def test_a_credit_lane_takes_two_attempts_unless_the_policy_says_otherwise():
    assert lane_slot_cap(POLICY["caps"], False, "claude-api") == 2
    assert lane_slot_cap({**POLICY["caps"], "max_in_flight_per_api_lane": 5}, False, "claude-api") == 5
    assert lane_slot_cap({**POLICY["caps"], "max_in_flight_per_lane": 1}, False, "claude-api") == 1
    assert lane_slot_cap(POLICY["caps"], False, "claude") is None
    policy = policy_with(["sonnet-api"])
    setup = scheduler.prepare(policy, view([credit_lane_row()]), job(task="review"))
    reasons, _ = scheduler.judge_lane(setup, "sonnet-api", credit_lane_row(), (), (), in_flight=2, unavailable={})
    assert "no-slot" in reasons
    reasons, _ = scheduler.judge_lane(setup, "sonnet-api", credit_lane_row(), (), (), in_flight=1, unavailable={})
    assert reasons == []


@pytest.mark.parametrize("fields", [{"sandbox": "workspace-write"}, {"tier": "hard"}, {}])
def test_a_credit_lane_is_never_probed_before_launch(fields):
    """C-31.5: C-11.4 probes an unmeasured lane before writable or hard work; a
    credit lane has no window to burn, so it never is."""
    policy = policy_with(["opus-api"])
    decision = scheduler.evaluate(policy, view([credit_lane_row()]), job(task="review", **fields))
    assert decision.chosen_lane == "claude-api-1"
    evaluation = next(row for row in decision.evaluations if row["model"] == decision.chosen_model)
    assert evaluation["candidate_details"]["claude-api-1"]["measured"] is False
    assert scheduler.probe_required(decision, job(task="review", **fields)) is False
    # The same unmeasured job on a Claude lane is probed, as before.
    claude = scheduler.evaluate(policy_with(["opus"]), view([lane("claude-1")]), job(task="review", **fields))
    assert scheduler.probe_required(claude, job(task="review", **fields)) is bool(fields)


# --- C-31.3: one credit lane, at admission -----------------------------------------------------

def test_two_enabled_credit_lanes_hold_both():
    policy = policy_with(["opus-api", "opus"])
    lanes = [credit_lane_row("claude-api-1"), credit_lane_row("claude-api-2", account_key="claude-api:b@example.org"),
             lane("claude-1")]
    decision = scheduler.evaluate(policy, view(lanes), job(task="review"))
    assert decision.chosen_model == "opus" and decision.chosen_lane == "claude-1"
    api = next(row for row in decision.evaluations if row["model"] == "opus-api")
    assert {row["lane_id"]: row["reasons"] for row in api["rejections"]} == {
        "claude-api-1": ["api-lane-limit"], "claude-api-2": ["api-lane-limit"]}
    assert api["capacity_blocks"] == ["api-lane-limit"]
    assert "api-lane-limit" in scheduler.STANDING_REFUSALS
    # A disabled second lane holds nothing.
    lanes[1] = {**lanes[1], "enabled": False}
    assert scheduler.evaluate(policy, view(lanes), job(task="review")).chosen_lane == "claude-api-1"


ROSTERS = st.lists(st.tuples(st.sampled_from(["claude-api", "claude", "codex"]), st.booleans()),
                   min_size=1, max_size=6)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(roster=ROSTERS, chain=st.sampled_from([["opus-api"], ["opus-api", "opus"], ["haiku-api", "terra"]]))
def test_no_credit_lane_is_chosen_while_two_are_enabled(roster, chain):
    """Invariant 3 at admission: for any roster, while two or more `claude-api`
    lanes are enabled, no `claude-api` lane is chosen."""
    counts: dict[str, int] = {}
    lanes = []
    for provider, enabled in roster:
        counts[provider] = counts.get(provider, 0) + 1
        identity = f"{provider}-{counts[provider]}"
        row = credit_lane_row(identity) if provider == "claude-api" else lane(identity)
        lanes.append({**row, "enabled": enabled})
    decision = scheduler.evaluate(policy_with(chain), view(lanes), job(task="review"))
    enabled_api = sum(1 for row in lanes if row["provider"] == "claude-api" and row["enabled"])
    if enabled_api >= 2:
        assert not str(decision.chosen_lane or "").startswith("claude-api-")
    elif enabled_api == 1 and chain[0].endswith("-api"):
        assert decision.chosen_lane.startswith("claude-api-")


# --- invariant 4: fallback only when the credit is gone ------------------------------------------

STATES = st.fixed_dictionaries({
    "credit_closed": st.booleans(), "credit_enabled": st.booleans(), "credit_in_flight": st.integers(0, 3),
    "claude_lanes": st.integers(0, 2), "claude_closed": st.booleans(), "second_credit_lane": st.booleans()})


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(state=STATES, pair=st.sampled_from(sorted(TWINS.items())))
def test_the_chain_falls_back_only_when_the_credit_lane_is_not_admissible(state, pair):
    """Invariant 4: for a chain `[X-api, X]`, the scheduler picks X only when the
    credit lane is not admissible, and always picks X when a credit closure holds
    the credit lane and X has an admissible lane."""
    api, twin = pair
    policy = policy_with([api, twin])
    lanes = [credit_lane_row("claude-api-1", enabled=state["credit_enabled"])]
    if state["second_credit_lane"]:
        lanes.append(credit_lane_row("claude-api-2", account_key="claude-api:b@example.org"))
    lanes += [lane(f"claude-{n}") for n in range(1, state["claude_lanes"] + 1)]
    closures = []
    if state["credit_closed"]:
        closures.append({**closure("claude-api-1"), "reason": "credits"})
    if state["claude_closed"]:
        closures += [closure(f"claude-{n}") for n in range(1, state["claude_lanes"] + 1)]
    readings = [reading(f"claude-{n}") for n in range(1, state["claude_lanes"] + 1)]
    snapshot = view(lanes, readings, closures)
    snapshot["in_flight"] = {"claude-api-1": state["credit_in_flight"]}
    decision = scheduler.evaluate(policy, snapshot, job(task="review"))
    # The oracle, from the lane states alone: C-31.3 holds every credit lane while
    # two are enabled; otherwise a credit lane is admissible when it is enabled,
    # open and below its two slots (C-31.5).
    enabled_api = int(state["credit_enabled"]) + int(state["second_credit_lane"])
    open_lanes = [identity for identity, ok in (
        ("claude-api-1", state["credit_enabled"] and not state["credit_closed"] and state["credit_in_flight"] < 2),
        ("claude-api-2", state["second_credit_lane"])) if ok] if enabled_api == 1 else []
    credit_admissible = bool(open_lanes)
    twin_admissible = state["claude_lanes"] > 0 and not state["claude_closed"]
    if decision.chosen_model == twin:
        assert not credit_admissible
    if credit_admissible:
        assert decision.chosen_model == api and decision.chosen_lane in open_lanes
    elif twin_admissible:
        assert decision.chosen_model == twin
    if state["credit_closed"] and not state["second_credit_lane"] and twin_admissible:
        # The clause as the record words it: a credit closure holds the credit lane.
        assert decision.chosen_model == twin and not decision.chosen_lane.startswith("claude-api-")


# --- invariant 1: credit keys only -----------------------------------------------------------------

def _launch(provider: str, tmp_path: Path, key_in_env_add: str | None) -> tuple[Lane, Launch]:
    lane_row = Lane(f"{provider}-1", provider, f"{provider}:x", Credential(provider, "ref", "keychain-token"),
                    None, LaneOwner.V2, False, identity=ORG)
    env_add = {"SUBFLEET_API_LANE": lane_row.lane_id} if provider == "claude-api" else {}
    if key_in_env_add is not None:
        env_add["ANTHROPIC_API_KEY"] = key_in_env_add
    return lane_row, Launch(("x",), env_add, (), str(tmp_path), None, "o", "e", None, None, lane_row.lane_id)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(provider=st.sampled_from(["claude-api", "claude", "codex"]),
       daemon_has_key=st.booleans(), passed=st.booleans(), leaked=st.booleans())
def test_only_a_credit_launch_carries_anthropic_api_key_and_only_its_own(provider, daemon_has_key, passed, leaked,
                                                                         tmp_path):
    """Invariant 1, C-31.17: for any daemon environment, including one that sets
    `ANTHROPIC_API_KEY`, and a policy that passes it, a credit launch carries
    exactly its resolved key, as its launch gate's frame, and no other launch
    carries one. No launch's environment carries the variable, a credit launch's
    included, whatever its additions name (a leaked env_add)."""
    parent = {"PATH": "/usr/bin:/bin", **({"ANTHROPIC_API_KEY": DAEMON_KEY} if daemon_has_key else {})}
    policy = {"providers": {"codex": {"ca_bundle": None}},
              **({"env": {"pass": ["ANTHROPIC_API_KEY"]}} if passed else {})}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "environ", parent)
        daemon = Daemon.__new__(Daemon)
        daemon._provider_environment = ProviderEnvironment(policy)
        key = KEY if provider == "claude-api" else (DAEMON_KEY if leaked else None)
        lane_row, launch = _launch(provider, tmp_path, key)
        environment = daemon._launch_environment(lane_row, launch)
        # What resolution hands the daemon: the credit key for a credit lane, and for
        # the others whatever a leaked credential map would hold.
        credential_env = {"ANTHROPIC_API_KEY": key} if key else {}
        framed = Daemon._credit_key(lane_row, credential_env)
    assert "ANTHROPIC_API_KEY" not in environment
    assert DAEMON_KEY not in environment.values() and KEY not in environment.values()
    read, write = os.pipe()
    try:
        Daemon._open_gate(write, framed)
        os.close(write)
        write = -1
        gate = b"".join(iter(lambda: os.read(read, 65536), b""))
    finally:
        os.close(read)
        if write >= 0:
            os.close(write)
    if provider == "claude-api":
        assert framed == KEY
        assert gate == b"1" + len(KEY).to_bytes(4, "big") + KEY.encode()
    else:
        assert framed is None and gate == b"1"


def test_a_credit_launch_never_runs_the_claude_binary(tmp_path):
    """Invariant 1: the credit launch's argv starts the harness under this Python."""
    adapter = ClaudeApiAdapter(registry_reader=lambda: {"accounts": {}})
    adapter.credit_budget = {"credit_usd": 200.0, "ledger_usd": 0.0, "in_flight_usd": 0.0}
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x", encoding="utf-8")
    spec = JobSpec("r", "dispatch", str(tmp_path), str(prompt), "build", "standard", None, None, Sandbox.READ_ONLY,
                   None, None)
    lane_row = Lane("claude-api-1", "claude-api", "claude-api:plan@example.org",
                    Credential("claude-api", "claude-api-plan@example.org", "keychain-token"), None, LaneOwner.V2,
                    False, identity=ORG)
    launch = adapter.build_launch(spec, "j/a1", tmp_path / "a1", lane_row, {"ANTHROPIC_API_KEY": KEY},
                                  "claude-opus-5-5", None, prompt, None)
    assert launch.argv[1:4] == ("-I", "-m", "subfleet.api_agent")
    assert all(os.path.basename(arg) != "claude" for arg in launch.argv)


def test_the_daemon_resolves_a_credit_key_for_the_lanes_own_organization_only(monkeypatch):
    """C-31.2: the daemon hands resolution the lane's identity and the live policy's
    denied organizations; a subscription lane's call is unchanged."""
    from subfleet import daemon as daemon_module
    calls = []

    def resolve(credential, **keywords):
        calls.append((credential.provider, keywords))
        return {"ANTHROPIC_API_KEY": KEY} if credential.provider == "claude-api" else {}

    monkeypatch.setattr(daemon_module, "resolve_credential", resolve)
    daemon = Daemon.__new__(Daemon)
    daemon._provider_environment = ProviderEnvironment({"providers": {"codex": {"ca_bundle": None}}})
    daemon.policy = {"claude_api": {"denied_org_ids": ["00000000-0000-4000-8000-00000000000b"]}}
    credit = Lane("claude-api-1", "claude-api", "claude-api:plan@example.org",
                  Credential("claude-api", "claude-api-plan@example.org", "keychain-token"), None, LaneOwner.V2,
                  False, identity=ORG)
    claude = Lane("claude-1", "claude", "claude:x", Credential("claude", "claude-quota-x@example.org",
                                                               "keychain-token"), None, LaneOwner.V2, False)
    assert daemon._credential_environment(credit) == {"ANTHROPIC_API_KEY": KEY}
    daemon._credential_environment(claude)
    assert calls == [("claude-api", {"identity": ORG,
                                     "denied_org_ids": frozenset({"00000000-0000-4000-8000-00000000000b"})}),
                     ("claude", {})]
