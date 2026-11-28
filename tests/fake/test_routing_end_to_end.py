"""Milestone 3 routing through durable submission, admission and why (C-11)."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from subfleet import daemon as daemon_module
from subfleet.adapters import registry
from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, Outcome, OutcomeClass, Reading, ReadingLabel
from subfleet.daemon import Daemon, after, utcnow
from subfleet.store import Store
from tests.caps import capped
from tests.claude_code import claude_code_active
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter


def claude_lane(identity, *, account=None, desktop=False):
    return Lane(identity, "claude", "claude:" + (account or identity),
                Credential("claude", "/fake/" + identity, "home"), "/fake/" + identity,
                LaneOwner.V2, desktop)


def seed_closed_opus(store):
    store.put_lane(claude_lane("claude-1", account="max@rulesfoundation.org", desktop=True))
    for identity in ("claude-2", "claude-3"):
        store.put_lane(claude_lane(identity))
        store.add_closure(Closure(identity, "claude-opus-5-5", after(3600),
                                  ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "fixture"))
    store.add_reading(Reading("codex-1", "account", "seven_day", .4, after(86400),
                              ReadingLabel.PROVIDER, "fixture", utcnow()))


@pytest.fixture
def routing_state(tmp_path, monkeypatch):
    root = tmp_path / "routing-state"
    root.mkdir()
    harness = Harness(root)
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
    monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
    monkeypatch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
    # C-10.3: these cases were written while the desktop lane was always refused;
    # they keep Claude Code using the desktop login, which refuses it still.
    claude_code_active(monkeypatch, tmp_path / "claude")
    service = Daemon(root)
    try:
        yield service, harness
    finally:
        service.close()


def research_args(harness, **changes):
    return harness.submit_args(pinned_model=None, task="research", tier="standard",
                               exclusions=["max@rulesfoundation.org"], **changes)


def test_c11_6_submitted_research_records_astra_promotion_and_why(routing_state):
    """C-6.3, C-11.5, C-11.6: excluded desktop plus closed Opus promotes durably."""
    service, harness = routing_state
    seed_closed_opus(service.store)
    job_id = service.dispatch("submit", research_args(harness))["job_id"]
    service._admit()
    attempt = service.store.list_attempts(job_id)[0]
    record = service.store.list_decisions(job_id)[0]
    assert record["attempt_id"] == attempt["attempt_id"]
    assert attempt["model_requested"] == "gpt-6-astra"
    assert attempt["lane_id"] == "codex-1"
    assert service.store.list_leases(attempt["attempt_id"])
    response = service.dispatch("why", {"job_id": job_id})
    assert response["decision"] == json.loads(record["decision_json"])
    assert "opus: no candidate lanes after exclusions; promoted" in response["text"]
    assert "astra" in response["text"]


def test_c11_5_socket_submission_and_cli_why_print_recorded_walk(daemon):
    """C-11.5, C-11.6, C-21: a fake process launches Astra and CLI why prints it."""
    with Store(daemon.root / "state.sqlite3") as store:
        store.put_lane(Lane("codex-1", "codex", "codex:fake",
                            Credential("codex", str(daemon.root / "home"), "home"),
                            str(daemon.root / "home"), LaneOwner.V2, False))
        seed_closed_opus(store)
    daemon.start()
    job_id = daemon.call("submit", **research_args(daemon))["job_id"]
    assert daemon.finished(job_id)["state"] == "succeeded"
    assert daemon.attempts(job_id)[0]["model_requested"] == "gpt-6-astra"
    result = subprocess.run([sys.executable, "-m", "subfleet.cli", "why", job_id],
                            env={**os.environ, "SUBFLEET_HOME": str(daemon.root)},
                            cwd=Path(__file__).resolve().parents[2], capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert "opus: no candidate lanes after exclusions; promoted" in result.stdout
    assert "astra on codex-1" in result.stdout


def test_c6_4_second_unmeasured_job_waits_with_persisted_reason(routing_state):
    """C-6.4, C-4.1, C-11.5: attempts reserve one blind slot and explain waiting."""
    service, harness = routing_state
    capped(service.policy)                          # C-6.4: the caps of before 2026-09-27 (tests/caps.py)
    first = service.submit(daemon_module.protocol.SubmitArgs(**harness.submit_args()))["job_id"]
    second = service.submit(daemon_module.protocol.SubmitArgs(**harness.submit_args()))["job_id"]
    admission_started = utcnow()
    service._admit()
    assert len(service.store.list_attempts(first)) == 1
    assert not service.store.list_attempts(second)
    waiting = service.store.get_job(second)
    assert waiting["state"] == "waiting"
    assert waiting["wait_reason"] == "capacity"
    # The recheck can become due while a slow pass or its assertions finish.
    assert waiting["next_check_at"] > admission_started
    assert "no-slot" in service.dispatch("why", {"job_id": second})["text"]


def test_c11_2_email_pin_never_admits_another_lane(routing_state):
    """C-11.2, C-17.3: an email pin remains on that account even when closed."""
    service, harness = routing_state
    seed_closed_opus(service.store)
    service.store.put_lane(claude_lane("claude-4", account="max@rules.foundation"))
    service.store.add_closure(Closure("claude-4", "account", after(3600),
                                     ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "fixture"))
    args = harness.submit_args(pinned_model="opus", pinned_lane="max@rules.foundation")
    job_id = service.dispatch("submit", args)["job_id"]
    service._admit()
    assert not service.store.list_attempts(job_id)
    why = service.dispatch("why", {"job_id": job_id})
    assert why["decision"]["chosen_lane"] is None
    assert why["decision"]["chain"] == ["opus"]
    assert "earliest reset" in why["text"]


def test_c10_3_desktop_refresh_precedes_reservation_transaction(routing_state, monkeypatch):
    """C-10.3, C-3.3: desktop login refresh occurs outside the atomic reservation."""
    service, harness = routing_state
    service.store.put_lane(claude_lane("claude-2", account="current@example.com"))
    calls = []
    def desktop():
        assert not service.store.conn.in_transaction
        calls.append(True)
        return "current@example.com"
    monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", desktop)
    job_id = service.dispatch("submit", research_args(harness))["job_id"]
    service._admit()
    decision = service.dispatch("why", {"job_id": job_id})["decision"]
    assert calls
    assert decision["chosen_model"] == "astra"
    assert "desktop" in str(decision["evaluations"])


def test_c11_4_hard_probe_uses_requested_model_and_records_admission(routing_state, monkeypatch):
    """C-11.4, C-8.4, C-3.3: probe outside SQL, holding the slot, records only evidence."""
    service, harness = routing_state
    calls = []
    def probe(job, lane, model, holder):
        assert not service.store.conn.in_transaction
        assert service.store.list_leases(holder)
        calls.append((lane.lane_id, model["id"]))
        return Outcome(OutcomeClass.OK, "admitted", {"rc": 0, "signal": None})
    monkeypatch.setattr(service, "_execute_probe", probe, raising=False)
    job_id = service.dispatch("submit", harness.submit_args(tier="hard"))["job_id"]
    service._admit()
    assert calls == [("codex-1", "gpt-6-astra")]
    assert len(service.store.list_jobs()) == len(service.store.list_attempts()) == 1
    readings = service.store.list_readings()
    assert len(readings) == 1
    assert readings[0]["label"] == "admission-observed"
    assert readings[0]["scope"] == "gpt-6-astra"
    assert readings[0]["utilization"] is readings[0]["attempt_id"] is None
    assert not any(row["holder"].startswith("probe:") for row in service.store.list_leases())


def test_c11_4_limited_probe_closes_opus_and_promotes_without_work_attempt(routing_state, monkeypatch):
    """C-11.4, C-11.6: a requested Opus probe closes its scope before promoting."""
    service, harness = routing_state
    service.store.put_lane(claude_lane("claude-2"))
    service.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                    ReadingLabel.PROVIDER, "fixture", utcnow()))
    calls = []
    def probe(job, lane, model, holder):
        model_id = model["id"]
        calls.append(model_id)
        return Outcome(OutcomeClass.LIMITED, "requested model limited", {"rc": 1},
                       closure=Closure(lane.lane_id, model_id, after(3600),
                                       ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "fixture"))
    monkeypatch.setattr(service, "_execute_probe", probe, raising=False)
    # A hard tier asks Astra by default; this test policy keeps Opus at hard
    # with an extra higher tier so the expensive probe can promote upward.
    service.policy["tiers"].append("highest")
    service.policy["chains"]["research"] = ["haiku", "sonnet", "opus", "opus", "astra"]
    job_id = service.dispatch("submit", harness.submit_args(pinned_model=None, task="research", tier="hard"))["job_id"]
    service._admit()
    assert calls == ["claude-opus-5-5"]
    attempts = service.store.list_attempts(job_id)
    assert len(attempts) == 1 and attempts[0]["model_requested"] == "gpt-6-astra"
    assert service.store.list_closures()[0]["scope"] == "claude-opus-5-5"
    assert "promoted" in service.dispatch("why", {"job_id": job_id})["text"]


def test_c11_4_inconclusive_probe_waits_without_closure_or_dispatch(routing_state, monkeypatch):
    """C-11.4, C-9.5, C-4.1: inconclusive admission waits without inventing a limit."""
    service, harness = routing_state
    monkeypatch.setattr(service, "_execute_probe", lambda *args: Outcome(
        OutcomeClass.TRANSIENT, "disconnected", {"rc": 1}), raising=False)
    job_id = service.dispatch("submit", harness.submit_args(tier="hard"))["job_id"]
    service._admit()
    job = service.store.get_job(job_id)
    assert job["state"] == "waiting" and job["wait_reason"] == "capacity"
    assert job["next_check_at"] > after(50)
    assert not service.store.list_attempts() and not service.store.list_closures()


def test_c11_4_generic_adapter_probe_classifies_a_real_tiny_fake_turn(process_inspection_available, routing_state):
    """C-11.4, C-9.2: adapters without probe_outcome use a same-model read-only turn."""
    service, harness = routing_state
    job_id = service.dispatch("submit", harness.submit_args(tier="hard"))["job_id"]
    service._admit()
    event = next(row for row in service.store.list_events(job_id) if row["kind"] == "probe.completed")
    assert json.loads(event["data_json"])["evidence"]["rc"] == 0
    assert len(service.store.list_attempts(job_id)) == 1
    assert service.store.list_readings()[0]["label"] == "admission-observed"


def test_c11_5_dry_run_never_probes_or_writes_state(routing_state, monkeypatch):
    """C-11.5, C-19.1: expensive dry-run explains routing without probes or actions."""
    service, harness = routing_state
    monkeypatch.setattr(service, "_probe_candidate", lambda *args: pytest.fail("dry run probed"))
    events = service.store.list_events()
    result = service.dispatch("submit", harness.submit_args(tier="hard", dry_run=True))
    assert result["decision"]["chosen_model"] == "astra"
    assert service.store.list_events() == events
    assert not service.store.list_jobs() and not service.store.list_attempts()


def test_c11_2_promoted_transient_retry_keeps_the_requested_model(routing_state):
    """C-4.5, C-4.6, C-11.2: the same-lane transient retry also pins promoted Astra."""
    service, harness = routing_state
    seed_closed_opus(service.store)
    job_id = service.dispatch("submit", research_args(harness))["job_id"]
    service._admit()
    attempt = service.store.list_attempts(job_id)[0]
    service.store.update_attempt(attempt["attempt_id"], state="failed", outcome_class="transient")
    service.store.release_leases(attempt["attempt_id"])
    service.store.update_job(job_id, state="waiting", wait_reason="capacity", next_check_at=utcnow())
    service._admit()
    attempts = service.store.list_attempts(job_id)
    assert len(attempts) == 2
    assert {row["model_requested"] for row in attempts} == {"gpt-6-astra"}
    assert {row["lane_id"] for row in attempts} == {"codex-1"}


def test_c6_4_capacity_waiter_cannot_be_overtaken_within_its_tier(routing_state):
    """C-4.1, C-6.4; amendment 11: a recheck delay never forfeits FIFO seniority."""
    service, harness = routing_state
    capped(service.policy)                          # C-6.4: the caps of before 2026-09-27 (tests/caps.py)
    first = service.dispatch("submit", harness.submit_args())["job_id"]
    second = service.dispatch("submit", harness.submit_args())["job_id"]
    service.store.update_job(first, state="waiting", wait_reason="capacity", next_check_at=after(30))
    service._admit()
    assert not service.store.list_attempts(second)
    service.store.update_job(first, next_check_at=utcnow())
    service._admit()
    assert len(service.store.list_attempts(first)) == 1
    assert not service.store.list_attempts(second)


def test_c11_3_status_exposes_latest_windows_closures_and_attempt_counts(routing_state):
    """C-9.1, C-11.3: the daemon status payload shares the routing capacity snapshot."""
    service, harness = routing_state
    seed_closed_opus(service.store)
    service.dispatch("submit", research_args(harness))
    service._admit()
    result = service.dispatch("daemon.status", {})
    assert len(result["closures"]) == 2
    assert len(result["readings"]) == 1
    assert result["lanes"][0]["lane_id"] == "codex-1"
    assert result["lanes"][0]["in_flight"] == result["active_attempts"] == 1


def test_c11_1_lane_pin_to_unconfigured_provider_is_key_named_invalid_input(routing_state):
    """C-11.1, C-11.2, C-17.3: a valid lane pin cannot crash on absent provider models."""
    service, harness = routing_state
    service.store.put_lane(claude_lane("claude-2"))
    service.policy["models"] = {"astra": service.policy["models"]["astra"]}
    with pytest.raises(daemon_module.protocol.ProtocolError, match="pinned_lane") as error:
        service.dispatch("submit", harness.submit_args(pinned_model=None, pinned_lane="claude-2"))
    assert error.value.code == 2


def test_c11_4_cancellation_during_probe_stops_further_admission(routing_state, monkeypatch):
    """C-7.2, C-11.4: cancelling an in-progress probe cannot dispatch its work."""
    service, harness = routing_state
    def probe(job, lane, model, holder):
        service.kill(daemon_module.protocol.KillArgs(job["job_id"]))
        return Outcome(OutcomeClass.OK, "admitted", {"rc": 0})
    monkeypatch.setattr(service, "_execute_probe", probe)
    job_id = service.dispatch("submit", harness.submit_args(tier="hard"))["job_id"]
    service._admit()
    assert service.store.get_job(job_id)["state"] == "cancelled"
    assert not service.store.list_attempts(job_id)
    assert not service.store.list_leases()


def test_c10_3_desktop_switch_during_probe_is_rechecked_before_dispatch(routing_state, monkeypatch):
    """C-10.3, C-11.4: a freshly selected desktop account cannot slip past the probe."""
    service, harness = routing_state
    service.store.put_lane(claude_lane("claude-2", account="current@example.com"))
    def probe(job, lane, model, holder):
        monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: "current@example.com")
        return Outcome(OutcomeClass.OK, "admitted", {"rc": 0})
    monkeypatch.setattr(service, "_execute_probe", probe)
    job_id = service.dispatch("submit", harness.submit_args(pinned_model="opus", tier="hard"))["job_id"]
    service._admit()
    assert not service.store.list_attempts(job_id)
    assert "desktop" in service.dispatch("why", {"job_id": job_id})["text"]
