"""C-6.25: rolling start limits applied by the daemon's admission passes."""

import dataclasses
import json

import pytest

from subfleet import cli, scheduler
from subfleet.policy import PolicyError, load_policy
from tests.fake.test_admission_latency import add_codex_lanes, submit_turn
from tests.fake.test_admission_priority import reservation_order, submit
from tests.fake.test_admission_ramp_retention import clock_at, stamp
from tests.fake.test_state_contract import state_daemon  # noqa: F401 (fixture)


@pytest.fixture
def fleet(state_daemon, monkeypatch):
    service, harness = state_daemon
    monkeypatch.setenv("SUBFLEET_HOME", str(service.root))
    return service, harness, clock_at(monkeypatch, 0)


def limits(service, *, lane=None, window=30, global_=None):
    service.policy["admission"].update(lane_starts_per_window=lane,
                                       lane_start_window_s=window, global_starts_per_min=global_)


def ended_start(service, harness, state, *, lane_id="codex-1", when=0):
    job_id = submit(service, harness, f"earlier-{state}-{lane_id}", pinned_lane=lane_id)
    service.store.update_job(job_id, state=state, finished_at=stamp(when + 1))
    service.store.add_attempt(attempt_id=f"{job_id}/a1", job_id=job_id, seq=1, lane_id=lane_id,
                              model_requested="gpt-6-astra", state=state,
                              reserved_at=stamp(when), finished_at=stamp(when + 1))
    return job_id


def assert_budget_wait(service, job_id):
    job = service.store.get_job(job_id)
    assert (job["state"], job["wait_reason"]) == ("waiting", "start-budget")
    assert job["next_check_at"]
    return job["next_check_at"]


def test_one_pass_stops_at_lane_limit_in_queue_order(fleet):
    service, harness, _ = fleet
    limits(service, lane=2)
    jobs = [submit(service, harness, f"one-lane-{i}") for i in range(5)]

    service._admit()

    assert reservation_order(service) == jobs[:2]
    for job_id in jobs[2:]:
        assert_budget_wait(service, job_id)


def test_consecutive_passes_wait_until_the_oldest_start_expires(fleet):
    service, harness, clock = fleet
    limits(service, lane=2)
    first = submit(service, harness, "first")
    service._admit()
    clock[0] = 5
    second = submit(service, harness, "second")
    service._admit()
    waiting = submit(service, harness, "waiting")

    for instant in (5, 15, 29):
        clock[0] = instant
        service._admit()
        assert reservation_order(service) == [first, second]
        assert assert_budget_wait(service, waiting) <= stamp(30)
    clock[0] = 30
    service._admit()
    assert reservation_order(service) == [first, second, waiting]


def test_one_pass_stops_at_global_limit_across_lanes_in_queue_order(fleet):
    service, harness, _ = fleet
    add_codex_lanes(service, "codex-2", "codex-3")
    limits(service, lane=2, global_=2)
    jobs = [submit(service, harness, f"across-{i}", pinned_lane=f"codex-{i}") for i in (1, 2, 3)]

    service._admit()

    assert reservation_order(service) == jobs[:2]
    assert assert_budget_wait(service, jobs[2]) <= stamp(60)


@pytest.mark.parametrize("exempt", ["turn", "priority"])
def test_exempt_job_starts_with_both_budgets_full_and_uses_no_budget(fleet, exempt):
    service, harness, clock = fleet
    limits(service, lane=1, window=60, global_=1)
    if exempt == "priority":
        service.policy["admission"]["priority_callers"] = ["CHOSEN"]
        ancestor = submit(service, harness, "chosen", caller_session="chosen")
        service.store.update_job(ancestor, state="succeeded")
    first = submit(service, harness, "ordinary-first")
    service._admit()
    assert reservation_order(service) == [first]

    clock[0] = 10
    waiting = submit(service, harness, "ordinary-waiting")
    if exempt == "turn":
        exempt_job = submit_turn(service, harness, 1)
    else:
        exempt_job = submit(service, harness, "priority-descendant", parent_job_id=ancestor)
    service._admit()
    assert reservation_order(service) == [first, exempt_job]
    assert_budget_wait(service, waiting)

    clock[0] = 60
    service._admit()
    assert reservation_order(service) == [first, exempt_job, waiting]


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
def test_ended_attempts_still_use_budget(fleet, state):
    service, harness, clock = fleet
    limits(service, lane=1)
    ended = ended_start(service, harness, state, when=0.5)
    waiting = submit(service, harness, "waiting")
    clock[0] = 30

    service._admit()

    assert reservation_order(service) == [ended]
    assert assert_budget_wait(service, waiting) <= stamp(30.5)
    clock[0] = 31
    service._admit()
    assert reservation_order(service) == [ended, waiting]


def test_shipped_defaults_keep_reservation_order_and_decision_bytes(fleet, monkeypatch):
    service, harness, _ = fleet
    assert service.policy["admission"].get("lane_starts_per_window") is None
    assert service.policy["admission"].get("global_starts_per_min") is None
    jobs = [submit(service, harness, f"default-{i}") for i in range(6)]
    pick = service._pick
    expected = {}

    def compare_with_original_policy(job, *, basis=None, **kwargs):
        supplied_basis = basis if basis is not None else {}
        decision = pick(job, basis=supplied_basis, **kwargs)
        original = {**service.policy, "admission": dict(service.policy["admission"])}
        for key in ("lane_starts_per_window", "lane_start_window_s", "global_starts_per_min"):
            original["admission"].pop(key, None)
        reference = scheduler.evaluate(original, supplied_basis["view"], supplied_basis["job"])
        expected[job["job_id"]] = json.dumps(dataclasses.asdict(reference))
        return decision

    monkeypatch.setattr(service, "_pick", compare_with_original_policy)
    service._admit()

    assert reservation_order(service) == jobs
    for job_id in jobs:
        decision, = service.store.list_decisions(job_id)
        assert decision["decision_json"] == expected[job_id]


def test_unchanged_budget_wait_writes_no_event(fleet):
    service, harness, clock = fleet
    limits(service, lane=1)
    submit(service, harness, "first")
    waiting = submit(service, harness, "waiting")
    service._admit()
    check = assert_budget_wait(service, waiting)
    events = service.store.list_events(waiting)
    decisions = service.store.list_decisions(waiting)

    clock[0] = 1
    service._admit()

    assert assert_budget_wait(service, waiting) == check
    assert service.store.list_events(waiting) == events
    assert service.store.list_decisions(waiting) == decisions


def test_older_job_uses_its_other_lane_before_a_younger_job(fleet):
    service, harness, _ = fleet
    add_codex_lanes(service, "codex-2")
    limits(service, lane=1)
    ended = ended_start(service, harness, "succeeded", when=-1)
    older = submit(service, harness, "older")
    younger = submit(service, harness, "younger", pinned_lane="codex-2")
    assert service._pick(service.store.get_job(older)).chosen_lane == "codex-1"

    service._admit()

    assert reservation_order(service) == [ended, older]
    assert service.store.list_attempts(older)[0]["lane_id"] == "codex-2"
    assert_budget_wait(service, younger)


def test_lane_budget_waiter_does_not_block_a_different_lane(fleet):
    service, harness, _ = fleet
    add_codex_lanes(service, "codex-2")
    service.policy["caps"]["max_active_attempts"] = 5
    limits(service, lane=1)
    ended = ended_start(service, harness, "succeeded", when=-1)
    older = submit(service, harness, "older", pinned_lane="codex-1")
    younger = submit(service, harness, "younger", pinned_lane="codex-2")

    service._admit()

    assert reservation_order(service) == [ended, younger]
    assert_budget_wait(service, older)
    assert service.store.list_attempts(younger)[0]["lane_id"] == "codex-2"


@pytest.mark.parametrize(("key", "invalid"), [
    ("lane_starts_per_window", -1), ("lane_starts_per_window", 1.5), ("lane_starts_per_window", True),
    ("global_starts_per_min", -1), ("global_starts_per_min", 1.5), ("global_starts_per_min", True),
    ("lane_start_window_s", 0), ("lane_start_window_s", -1),
])
def test_policy_refuses_invalid_start_budget(fleet, tmp_path, key, invalid):
    service, _, _ = fleet
    path = tmp_path / "invalid-policy.json"
    policy = json.loads((service.root / "policy.json").read_text())
    policy["admission"][key] = invalid
    path.write_text(json.dumps(policy))

    with pytest.raises(PolicyError, match="must be a positive") as error:
        load_policy(path)
    assert error.value.key == f"admission.{key}"


def test_cli_status_shows_enabled_budgets_and_current_counts(fleet, monkeypatch, capsys):
    service, harness, clock = fleet

    class Client:
        call = staticmethod(service.dispatch)

    monkeypatch.setattr(cli, "_client", lambda args: Client())
    assert cli.main(["status", "--json"]) == 0
    assert "start_budget" not in json.loads(capsys.readouterr().out)
    assert cli.main(["status"]) == 0
    assert "start budget" not in capsys.readouterr().out

    limits(service, lane=2, global_=4)
    submit(service, harness, "first")
    service._admit()
    assert cli.main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["start_budget"] == {
        "lane": {"limit": 2, "window_s": 30, "used": {"codex-1": 1}},
        "global": {"limit": 4, "window_s": 60, "used": 1},
    }
    assert cli.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "lane start budget: 1/2 starts used in the last 30 s (codex-1)" in output
    assert "global start budget: 1/4 starts used in the last 60 s" in output

    clock[0] = 30
    assert cli.main(["status", "--json"]) == 0
    counts = json.loads(capsys.readouterr().out)["start_budget"]
    assert counts["lane"]["used"] == {"codex-1": 0}
    assert counts["global"]["used"] == 1
