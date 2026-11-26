"""C-23.53: gate rounds require a placeable job and spend budget after admission."""

from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from subfleet import cli, protocol
from subfleet.contracts import ClockSource, Closure, ClosureReason
from subfleet.gate import cli as gate_cli
from subfleet.gate.service import dispatch
from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError, load_policy
from tests.fake.test_gate_end_to_end import FakeClient, arguments, finish, wire
from tests.unit.test_gate_admission import core, lane
from tests.unit.test_gate_service import continued


@pytest.fixture
def plan(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text("Review this plan.\n")
    return path


def offset_stamp(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def close_lane(core, seconds):
    core.store.add_closure(Closure("codex-1", "account", offset_stamp(seconds),
                                  ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "test"))


def backdate(core, job_id, seconds):
    with core.store.transaction("test.backdate", job_id=job_id):
        core.store.update_job(job_id, created_at=offset_stamp(-seconds))


def test_far_closure_refuses_the_round_before_submit(core, plan):
    close_lane(core, 7200)
    result = dispatch(core, "gate.start", wire(arguments(plan)))
    assert result["code"] == 4
    assert "astra" in result["message"]
    assert "1800 s" in result["message"]
    assert "closed:account:" in result["message"]
    assert "no round was spent" in result["message"]
    assert core.store.list_jobs() == []
    gate_id = result["gate_id"]
    state = core._gate_service._load(gate_id)
    assert state["rounds"] == []
    assert state["status"] == "blocked"
    events = [json.loads(row["data_json"]) for row in core.store.list_events()
              if row["kind"] == "gate.state"]
    assert any(event.get("gate_id") == gate_id and event.get("transition") == "round-unplaceable"
               for event in events)
    assert not (core.root / "gates" / gate_id / "rounds").exists()


def test_cli_exits_4_for_an_unplaceable_round(core, plan):
    close_lane(core, 7200)
    client = FakeClient(core)
    assert gate_cli.run(arguments(plan), root=core.root, client=client, poll_interval=0) == 4
    assert client.jobs == []


def test_near_closure_still_submits(core, plan):
    close_lane(core, 600)
    result = dispatch(core, "gate.start", wire(arguments(plan)))
    assert result["code"] is None
    jobs = core.store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == result["job_id"]
    assert jobs[0]["state"] == "queued"


def test_unknown_pinned_lane_refuses_before_submit(core, plan):
    core.store.put_lane(lane(core.root / "claude-home", "claude"))
    result = dispatch(core, "gate.start", wire(arguments(plan, "--peer-account", "claude-9", peer="fable")))
    assert result["code"] == 4
    assert "unknown" in result["message"]
    assert "claude-9" in result["message"]
    assert "no round was spent" in result["message"]
    assert core.store.list_jobs() == []
    assert core._gate_service._load(result["gate_id"])["rounds"] == []


def test_queued_round_ends_unplaced_after_deadline(core, plan):
    core.policy["caps"]["gate_queue_s"] = 60
    started = dispatch(core, "gate.start", wire(arguments(plan)))
    gate_id, job_id = started["gate_id"], started["job_id"]
    backdate(core, job_id, 120)
    result = dispatch(core, "gate.poll", {"gate_id": gate_id})
    assert result["code"] == 4
    assert "not placed within 60 s" in result["message"]
    assert "no round was spent" in result["message"]
    assert f"subfleet why {job_id}" in result["message"]
    assert f"subfleet gate continue {gate_id} --main-approve" in result["message"]
    assert result["job_id"] == job_id
    assert core.store.get_job(job_id)["state"] == "cancelled"
    assert core.store.list_attempts(job_id) == []
    state = core._gate_service._load(gate_id)
    assert state["rounds"] == []
    [unplaced] = state["unplaced_rounds"]
    assert unplaced["status"] == "unplaced"
    assert unplaced["peer_run_id"] == job_id


def test_queued_round_under_deadline_keeps_waiting(core, plan):
    core.policy["caps"]["gate_queue_s"] = 60
    started = dispatch(core, "gate.start", wire(arguments(plan)))
    gate_id, job_id = started["gate_id"], started["job_id"]
    backdate(core, job_id, 10)
    assert dispatch(core, "gate.poll", {"gate_id": gate_id})["code"] is None
    assert core.store.get_job(job_id)["state"] == "queued"
    state = core._gate_service._load(gate_id)
    assert len(state["rounds"]) == 1
    assert "unplaced_rounds" not in state


def test_round_after_an_unplaced_one_has_the_full_budget(core, plan):
    core.policy["caps"]["gate_queue_s"] = 60
    first = dispatch(core, "gate.start", wire(arguments(plan, "--max-rounds", "1")))
    gate_id = first["gate_id"]
    backdate(core, first["job_id"], 120)
    assert dispatch(core, "gate.poll", {"gate_id": gate_id})["code"] == 4
    second = dispatch(core, "gate.continue", wire(continued(gate_id, plan)))
    assert second["code"] is None
    assert second["job_id"] != first["job_id"]
    finish(core, second)
    assert dispatch(core, "gate.poll", {"gate_id": gate_id})["code"] == 0
    assert len(core.store.list_attempts(second["job_id"])) == 1
    state = core._gate_service._load(gate_id)
    [approved] = state["rounds"]
    [unplaced] = state["unplaced_rounds"]
    assert approved["number"] == unplaced["number"] == 1
    assert approved["attempt_id"] != unplaced["attempt_id"]
    assert approved["verdict"]["verdict"] == "approve"
    assert (core.root / "gates" / gate_id / "certificate.json").exists()


def test_continue_ends_an_expired_queued_round_and_starts_fresh(core, plan):
    core.policy["caps"]["gate_queue_s"] = 60
    first = dispatch(core, "gate.start", wire(arguments(plan)))
    gate_id = first["gate_id"]
    backdate(core, first["job_id"], 120)
    second = dispatch(core, "gate.continue", wire(continued(gate_id, plan)))
    assert second["code"] is None
    assert second["job_id"] != first["job_id"]
    assert core.store.get_job(first["job_id"])["state"] == "cancelled"
    state = core._gate_service._load(gate_id)
    assert len(state["unplaced_rounds"]) == len(state["rounds"]) == 1


def test_killed_queued_round_spends_no_round(core, plan):
    first = dispatch(core, "gate.start", wire(arguments(plan, "--max-rounds", "1")))
    gate_id = first["gate_id"]
    core.kill(protocol.KillArgs(first["job_id"]))
    result = dispatch(core, "gate.poll", {"gate_id": gate_id})
    assert result["code"] == 4
    assert "ended cancelled" in result["message"]
    assert "no round was spent" in result["message"]
    state = core._gate_service._load(gate_id)
    assert state["rounds"] == []
    assert len(state["unplaced_rounds"]) == 1
    second = dispatch(core, "gate.continue", wire(continued(gate_id, plan)))
    assert second["code"] is None
    assert second["job_id"] != first["job_id"]


def test_placed_round_is_never_ended_by_the_deadline(core, plan):
    core.policy["caps"]["gate_queue_s"] = 60
    started = dispatch(core, "gate.start", wire(arguments(plan)))
    gate_id, job_id = started["gate_id"], started["job_id"]
    core._admit()
    [attempt] = core.store.list_attempts(job_id)
    backdate(core, job_id, 120)
    assert dispatch(core, "gate.poll", {"gate_id": gate_id})["code"] is None
    assert core.store.get_job(job_id)["state"] != "cancelled"
    assert "unplaced_rounds" not in core._gate_service._load(gate_id)
    finish(core, started)
    [finished_attempt] = core.store.list_attempts(job_id)
    assert finished_attempt["attempt_id"] == attempt["attempt_id"]
    assert dispatch(core, "gate.poll", {"gate_id": gate_id})["code"] == 0


def test_cancel_that_finds_an_attempt_keeps_waiting(core, plan, monkeypatch):
    core.policy["caps"]["gate_queue_s"] = 60
    started = dispatch(core, "gate.start", wire(arguments(plan)))
    gate_id, job_id = started["gate_id"], started["job_id"]
    backdate(core, job_id, 120)
    calls = []

    def refuse_cancel(job_id, data, **options):
        calls.append((job_id, data, options))
        return False

    monkeypatch.setattr(core, "_request_cancel", refuse_cancel)
    assert dispatch(core, "gate.poll", {"gate_id": gate_id})["code"] is None
    assert len(core._gate_service._load(gate_id)["rounds"]) == 1
    assert core.store.get_job(job_id)["state"] == "queued"
    [(cancelled_job, data, options)] = calls
    assert cancelled_job == job_id
    assert data["by"] == "daemon"
    assert data["reason"] == "gate-queue-deadline"
    assert data["gate_id"] == gate_id
    assert data["queued_s"] >= 120
    assert options["without_attempt"] is True


def test_queued_continue_names_the_queue(core, plan):
    core.policy["caps"]["gate_queue_s"] = 1800
    started = dispatch(core, "gate.start", wire(arguments(plan)))
    gate_id = started["gate_id"]
    result = dispatch(core, "gate.continue", wire(continued(gate_id, plan)))
    assert result["code"] == 4
    assert "already queued" in result["message"]
    assert started["job_id"] in result["message"]
    assert "1800 s" in result["message"]
    assert len(core.store.list_jobs()) == 1
    assert len(core._gate_service._load(gate_id)["rounds"]) == 1


def test_fable_round_on_an_unmeasured_lane_is_submitted(core, plan):
    core.store.put_lane(lane(core.root / "claude-home", "claude"))
    result = dispatch(core, "gate.start", wire(arguments(plan, peer="fable")))
    assert result["code"] is None
    [job] = core.store.list_jobs()
    assert job["pinned_model"] == "fable"


def test_queue_deadline_default_and_refusals(tmp_path):
    shipped = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    path = tmp_path / "custom-policy.json"
    for invalid in (0, -1, True, "60", None):
        shipped["caps"]["gate_queue_s"] = invalid
        path.write_text(json.dumps(shipped), encoding="utf-8")
        with pytest.raises(PolicyError) as caught:
            load_policy(path)
        assert caught.value.key == "caps.gate_queue_s"
    assert load_policy(DEFAULT_POLICY_PATH)["caps"]["gate_queue_s"] == 1800


def test_continue_without_approval_reports_the_unplaced_round(core, plan):
    core.policy["caps"]["gate_queue_s"] = 60
    first = dispatch(core, "gate.start", wire(arguments(plan)))
    gate_id = first["gate_id"]
    backdate(core, first["job_id"], 120)
    args = cli.build_parser().parse_args([
        "gate", "continue", gate_id, "--expect-sha256", hashlib.sha256(plan.read_bytes()).hexdigest(),
    ])
    result = dispatch(core, "gate.continue", wire(args))
    assert result["code"] == 4
    assert "no round was spent" in result["message"]
    assert core.store.get_job(first["job_id"])["state"] == "cancelled"
    state = core._gate_service._load(gate_id)
    assert state["rounds"] == []
    assert len(state["unplaced_rounds"]) == 1
    assert len(core.store.list_jobs()) == 1
