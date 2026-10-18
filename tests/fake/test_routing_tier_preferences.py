"""C-11.2: list tiers through submission, admission, probe reach, why and status."""

import json

import pytest

from subfleet import scheduler
from subfleet.cli import format_status
from subfleet.contracts import ClockSource, Closure, ClosureReason, Outcome, OutcomeClass, Reading, ReadingLabel
from subfleet.daemon import after, utcnow
from subfleet.policy import load_policy
from subfleet.protocol import ProtocolError
from subfleet.status_json import build_status
from tests.fake.test_routing_end_to_end import claude_lane, routing_state  # noqa: F401


def preference_policy(service):
    path = service.root / "policy.json"
    data = json.loads(path.read_text())
    data["models"].update(luna={"provider": "codex", "id": "gpt-6-luna"},
                          sol61={"provider": "codex", "id": "gpt-6.1-sol"})
    data["chains"]["review"] = ["luna", ["haiku", "sonnet"], ["sol61", "opus"], ["sol61", "opus"]]
    path.write_text(json.dumps(data))
    service.policy = load_policy(path)
    service.store.put_lane(claude_lane("claude-1"))


@pytest.mark.parametrize("tier,first,expected,identity", [
    ("easy", "haiku", "sonnet", "claude-1"),
    ("standard", "sol61", "opus", "claude-1"),
    ("hard", "sol61", "opus", "claude-1"),
])
def test_admission_why_and_status_use_same_tier_fallback(routing_state, tier, first, expected, identity):
    service, harness = routing_state
    preference_policy(service)
    for lane_id in ("claude-1", "codex-1"):
        service.store.add_reading(Reading(lane_id, "account", "seven_day", .2, after(86400),
                                         ReadingLabel.PROVIDER, "fixture", utcnow()))
        service.store.add_closure(Closure(lane_id, service.policy["models"][first]["id"], after(3600),
                                         ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "fixture"))
    task_id = service.dispatch("submit", harness.submit_args(pinned_model=None, task="review", tier=tier))["job_id"]
    task = service.store.get_job(task_id)
    assert scheduler.demand_models(service.policy, task) == frozenset(
        ["haiku", "sonnet", "sol61", "opus"] if tier == "easy" else ["sol61", "opus"])
    service._admit()
    attempt = service.store.list_attempts(task_id)[0]
    assert (attempt["lane_id"], attempt["model_requested"]) == (identity, service.policy["models"][expected]["id"])
    response = service.dispatch("why", {"job_id": task_id})
    assert response["decision"]["chain"] == [first, expected]
    assert response["decision"]["chosen_model"] == expected
    assert f"{first}:" in response["text"] and expected in response["text"]
    snapshot = service.dispatch("daemon.status", {})
    assert identity in format_status(snapshot)
    status = build_status(snapshot)
    row = next(row for row in status["jobs"]["live"] if row["job_id"] == task_id)
    assert (row["lane_id"], row["model"]) == (identity, service.policy["models"][expected]["id"])


def test_pins_and_probe_reach_use_first_preferred_provider(routing_state):
    service, harness = routing_state
    preference_policy(service)
    task_id = service.dispatch("submit", harness.submit_args(pinned_model=None, pinned_lane="claude-1",
                                                              task="review", tier="easy"))["job_id"]
    task = service.store.get_job(task_id)
    assert scheduler.pin_provider(service.policy, task) == "claude"
    assert scheduler.evaluate(service.policy, service._capacity_view(), task).chain == ("haiku",)
    roster = service._pin_roster()
    assert service._probe_reach(roster, task, "haiku", scheduler.demand_lanes(roster, task, service.policy), ()) == {"claude-1"}
    with pytest.raises(ProtocolError, match="providers"):
        service.dispatch("submit", harness.submit_args(pinned_model=None, pinned_lane="claude-1",
                                                       task="review", tier="standard"))


def test_admission_probe_uses_same_tier_fallback_model(routing_state, monkeypatch):
    """A closed Codex preference sends the hard review's probe to Opus."""
    service, harness = routing_state
    preference_policy(service)
    service.store.add_closure(Closure("codex-1", service.policy["models"]["sol61"]["id"], after(3600),
                                     ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "fixture"))
    probed = []

    def probe(task, lane, model, holder):
        probed.append((lane.lane_id, model["id"]))
        return Outcome(OutcomeClass.OK, "fixture probe admitted")

    monkeypatch.setattr(service, "_execute_probe", probe)
    task_id = service.dispatch("submit", harness.submit_args(pinned_model=None, task="review", tier="hard"))["job_id"]
    service._admit()
    assert probed == [("claude-1", service.policy["models"]["opus"]["id"])]
    assert service.store.list_attempts(task_id)[0]["model_requested"] == service.policy["models"]["opus"]["id"]
    assert service.dispatch("why", {"job_id": task_id})["decision"]["chain"] == ["sol61", "opus"]
