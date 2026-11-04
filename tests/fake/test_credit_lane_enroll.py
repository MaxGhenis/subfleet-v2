"""C-31.1, C-31.3, C-31.4: enrolling the API credit lane through the daemon.

The adapter is the real `ClaudeApiAdapter`, handed a stand-in registry: tests never
read `~/.config/claude-credits`, and enrollment reads no key and sends no request.
Invariant 3 at enrollment: no sequence of enrollments adds a second `claude-api`
account while one is enabled.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import protocol
from subfleet.adapters import registry
from subfleet.adapters.claude_api import ClaudeApiAdapter
from tests.fake.test_lanes_enroll import FakeClaude, core, lanes_json  # noqa: F401 (the fixture)

ORGS = {"a@example.org": "00000000-0000-4000-8000-00000000000a",
        "b@example.org": "00000000-0000-4000-8000-00000000000b",
        "c@example.org": "00000000-0000-4000-8000-00000000000c"}
REGISTRY = {"version": 1, "accounts": {
    email: {"service": f"claude-api-{email}", "kind": "console", "fingerprint": "sha256:" + f"{n:x}" * 16,
            "renews_day": 15, "org_id": org, "status": "active"}
    for n, (email, org) in enumerate(ORGS.items(), start=1)}}


@pytest.fixture
def credit(core, monkeypatch):
    monkeypatch.setitem(registry._factories, "claude-api",
                        lambda: ClaudeApiAdapter(registry_reader=lambda: REGISTRY))
    return core


def credit_lanes(daemon):
    """The credit lanes stored; the harness seeds a Codex lane of its own."""
    return [row["lane_id"] for row in daemon.store.query(
        "SELECT lane_id FROM lanes WHERE provider='claude-api' ORDER BY lane_id")]


def enroll(daemon, email, **fields):
    return daemon.dispatch("lanes", {"action": "enroll", "credential": f"claude-api-{email}", **fields})


def refusal(daemon, email, **fields):
    with pytest.raises(protocol.ProtocolError) as caught:
        enroll(daemon, email, **fields)
    return caught.value


def test_enroll_a_credit_lane_from_its_email(credit):
    daemon, root = credit
    row = enroll(daemon, "a@example.org")["enrolled"]
    assert row["lane_id"] == "claude-api-1" and row["provider"] == "claude-api"
    assert row["account_key"] == "claude-api:a@example.org" and row["credential_kind"] == "keychain-token"
    assert row["credential_ref"] == "claude-api-a@example.org" and row["home"] is None
    assert row["identity"] == ORGS["a@example.org"] and row["identity_status"] == "verified"
    assert row["plan"] == "api-credit" and row["label"] == "a@example.org"
    assert row["credit_usd"] == 200.0                     # C-31.4: Max 20x's credit unless given
    seeded = [lane for lane in lanes_json(root) if lane["lane_id"] == "claude-api-1"][0]
    assert seeded["provider"] == "claude-api" and seeded["credit_usd"] == 200.0
    assert daemon.store.query("SELECT * FROM readings WHERE lane_id='claude-api-1'") == []


def test_credit_usd_sets_the_lanes_credit(credit):
    daemon, root = credit
    assert enroll(daemon, "a@example.org", credit_usd=100)["enrolled"]["credit_usd"] == 100.0
    assert [lane["credit_usd"] for lane in lanes_json(root) if lane["lane_id"] == "claude-api-1"] == [100.0]


@pytest.mark.parametrize("value", [0, -5, float("nan"), float("inf"), True, "lots", 100_001])
def test_a_bad_credit_is_refused_before_anything_is_written(credit, value):
    daemon, _ = credit
    refused = refusal(daemon, "a@example.org", credit_usd=value)
    assert refused.reason == "enroll-credit-usd"
    assert credit_lanes(daemon) == []


def test_credit_usd_is_refused_for_a_subscription_lane(credit, tmp_path):
    daemon, _ = credit
    with pytest.raises(protocol.ProtocolError) as caught:
        daemon.dispatch("lanes", {"action": "enroll", "credential": "claude-quota-x@example.org", "credit_usd": 50})
    assert caught.value.reason == "enroll-credit-provider"
    assert credit_lanes(daemon) == []


def test_a_second_account_is_refused_with_the_lane_limit(credit):
    daemon, root = credit
    enroll(daemon, "a@example.org")
    refused = refusal(daemon, "b@example.org")
    assert refused.reason == "api-lane-limit" and refused.details == {"lane_id": "claude-api-1"}
    assert "claude-api-1" in str(refused) and ORGS["b@example.org"] not in str(refused)
    assert credit_lanes(daemon) == ["claude-api-1"]
    assert [lane["lane_id"] for lane in lanes_json(root) if lane["provider"] == "claude-api"] == ["claude-api-1"]


def test_the_same_account_enrolls_again_and_keeps_its_credit(credit):
    """C-31.3: the limit counts accounts, not enrollments. While the lane is enabled
    its reference is bound (`enroll-bound`, C-10.2); once disabled, the same
    account re-enrolls as a new lane and keeps the credit it had."""
    daemon, _ = credit
    enroll(daemon, "a@example.org", credit_usd=120)
    assert refusal(daemon, "a@example.org").reason == "enroll-bound"
    daemon.store.update_lane("claude-api-1", enabled=0)
    row = enroll(daemon, "a@example.org")["enrolled"]
    assert row["lane_id"] == "claude-api-2" and row["account_key"] == "claude-api:a@example.org"
    assert row["credit_usd"] == 120.0
    assert enroll_after_disable(daemon, "claude-api-2", "a@example.org", credit_usd=90)["credit_usd"] == 90.0


def enroll_after_disable(daemon, lane_id, email, **fields):
    daemon.store.update_lane(lane_id, enabled=0)
    return enroll(daemon, email, **fields)["enrolled"]


def test_a_disabled_lane_does_not_hold_the_limit(credit):
    daemon, _ = credit
    enroll(daemon, "a@example.org")
    daemon.store.update_lane("claude-api-1", enabled=0)
    assert enroll(daemon, "b@example.org")["enrolled"]["lane_id"] == "claude-api-2"


def test_a_denied_organization_is_refused_at_enrollment(credit, monkeypatch):
    daemon, _ = credit
    monkeypatch.setitem(daemon.policy, "claude_api", {"denied_org_ids": [ORGS["a@example.org"]]})
    refused = refusal(daemon, "a@example.org")
    assert refused.reason == "credential-refused" and ORGS["a@example.org"] not in str(refused)
    assert credit_lanes(daemon) == []


@pytest.mark.parametrize("email,reason", [("nobody@example.org", "credential-refused"),
                                          ("not-an-email", "enroll-credential")])
def test_an_email_the_registry_lacks_is_refused(credit, email, reason):
    daemon, _ = credit
    assert refusal(daemon, email).reason == reason
    assert credit_lanes(daemon) == []


STEPS = st.lists(st.one_of(st.tuples(st.just("enroll"), st.sampled_from(sorted(ORGS))),
                           st.tuples(st.just("disable"), st.integers(1, 6))), max_size=8)


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(steps=STEPS)
def test_no_sequence_of_enrollments_enables_a_second_account(credit, steps):
    """Invariant 3 at enrollment: after any sequence of enrollments and disables,
    the enabled `claude-api` lanes hold at most one account, and every refusal
    names the limit or the bound reference."""
    daemon, _ = credit
    for lane_id in credit_lanes(daemon):
        daemon.store.update_lane(lane_id, enabled=0)      # each example starts from no enabled credit lane
    for action, value in steps:
        if action == "disable":
            daemon.store.update_lane(f"claude-api-{value}", enabled=0)
            continue
        enabled = {row["account_key"] for row in daemon.store.query(
            "SELECT account_key FROM lanes WHERE provider='claude-api' AND enabled=1")}
        try:
            enroll(daemon, value)
        except protocol.ProtocolError as refused:
            expected = "enroll-bound" if f"claude-api:{value}" in enabled else "api-lane-limit"
            assert refused.reason == expected
        else:
            assert enabled <= {f"claude-api:{value}"} and f"claude-api:{value}" not in enabled
        accounts = [row["account_key"] for row in daemon.store.query(
            "SELECT account_key FROM lanes WHERE provider='claude-api' AND enabled=1")]
        assert len(accounts) <= 1
