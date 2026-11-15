"""C-6.23: pins refuse before insertion, and the exception is durable authority."""

import json

import pytest

from subfleet import protocol
from subfleet.adapters.base import AdapterError
from tests.fake.test_routing_end_to_end import claude_lane
from tests.fake.test_state_contract import state_daemon  # noqa: F401


@pytest.mark.parametrize("family,model", [("claude", "opus"), ("codex", "astra"),
                                         ("codex", "sol")])
def test_same_family_review_pin_has_its_own_refusal_and_no_writes(state_daemon, family, model):
    service, harness = state_daemon
    before = {table: service.store.query(f"SELECT * FROM {table} ORDER BY rowid")
              for table in ("jobs", "events", "attempts")}
    with pytest.raises(AdapterError) as error:
        service.dispatch("submit", harness.submit_args(task="review", caller_family=family,
                                                       pinned_model=model))
    assert error.value.code == 7 and error.value.reason == "review-same-family"
    assert protocol.refusal_text(error.value) == (
        "review: the pinned model belongs to the caller's family",
        "choose a model from the other family, or pass --allow-same-family")
    assert {table: service.store.query(f"SELECT * FROM {table} ORDER BY rowid")
            for table in before} == before


@pytest.mark.parametrize("allowed", [False, True])
def test_authorization_is_recorded_on_job_manifest_and_show(state_daemon, allowed):
    service, harness = state_daemon
    args = harness.submit_args(task="review", caller_family="codex", pinned_model="opus",
                               allow_same_family=allowed)
    reply = service.dispatch("submit", args)
    job_id = reply["job_id"]
    for job in (service.store.get_job(job_id), service.dispatch("show", {"job_id": job_id})["job"],
                service.dispatch("list", {})["jobs"][0]):
        assert job["caller_family"] == "codex"
        assert job["allow_same_family"] is allowed
    manifest = json.loads((service.root / "jobs" / job_id / "manifest.json").read_text())
    assert manifest["job"]["allow_same_family"] is allowed


def test_authorization_allows_same_family_pin_and_changes_request_digest(state_daemon):
    service, harness = state_daemon
    args = harness.submit_args(task="review", caller_family="codex", pinned_model="astra",
                               allow_same_family=True)
    reply = service.dispatch("submit", args)
    assert service.store.get_job(reply["job_id"])["allow_same_family"] is True
    # With an opposite-family pin both requests are valid, but authority must
    # still be bound to the request, rather than silently reusing weaker consent.
    other = harness.submit_args(task="review", caller_family="codex", pinned_model="opus")
    service.dispatch("submit", other)
    with pytest.raises(protocol.ProtocolError) as error:
        service.dispatch("submit", {**other, "allow_same_family": True})
    assert error.value.reason == "different-payload"


@pytest.mark.parametrize("task,family,model", [("review", None, "astra"), ("build", "codex", "astra"),
                                             ("build", "claude", "opus")])
def test_null_family_and_non_review_pins_are_unchanged(state_daemon, task, family, model):
    service, harness = state_daemon
    assert service.dispatch("submit", harness.submit_args(task=task, caller_family=family,
                                                         pinned_model=model))["created"] is True


@pytest.mark.parametrize("allowed", [None, 1, "true", []])
def test_daemon_requires_a_boolean_override(state_daemon, allowed):
    service, harness = state_daemon
    with pytest.raises(protocol.ProtocolError) as error:
        service.dispatch("submit", harness.submit_args(task="review", allow_same_family=allowed))
    assert error.value.reason == "invalid-args"
    assert service.store.list_jobs() == []


def test_why_wire_input_validates_family(state_daemon):
    service, _ = state_daemon
    with pytest.raises(protocol.ProtocolError) as error:
        service.dispatch("why", {"task": "review", "tier": "hard", "caller_family": ["codex"]})
    assert error.value.reason == "caller-family"


def test_submit_lane_pin_uses_the_same_family_chain_as_admission(state_daemon):
    service, harness = state_daemon
    service.store.put_lane(claude_lane("claude-1"))
    reply = service.dispatch("submit", harness.submit_args(task="review", tier="hard",
                            caller_family="codex", pinned_model=None, pinned_lane="claude-1"))
    assert service.store.get_job(reply["job_id"])["pinned_lane"] == "claude-1"


def test_submit_account_alias_uses_the_independent_review_provider(state_daemon):
    service, harness = state_daemon
    account = "reviewer@example.invalid"
    service.store.put_lane(claude_lane("claude-1", account=account))
    reply = service.dispatch("submit", harness.submit_args(task="review", tier="hard",
                            caller_family="codex", pinned_model=None, pinned_lane=account,
                            pinned_provider="claude"))
    assert service.store.get_job(reply["job_id"])["pinned_lane"] == "claude-1"
