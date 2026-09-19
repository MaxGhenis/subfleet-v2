"""Probe supervision over mocked process identity and durable rows (C-5, C-11.4)."""

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import daemon as daemon_module, procs
from subfleet.contracts import Launch, Outcome, OutcomeClass
from subfleet.daemon import after, utcnow
from tests.fake.test_routing_end_to_end import routing_state
from tests.fake_adapter import FakeAdapter


def reserved_probe(service, job_id, *, state="starting"):
    directory = service.root / "lanes" / "codex-1" / "probes" / "fixture"
    directory.mkdir(parents=True)
    record = {"holder": "probe:fixture", "job_id": job_id, "lane_id": "codex-1",
              "model_id": "gpt-6-astra", "directory": str(directory), "state": state,
              "created_at": utcnow(), "deadline_at": after(60), "guardian_pid": 900001,
              "pgid": 900001, "boot_id": "boot", "proc_start": "start", "owned_identities": {}}
    service.store.acquire_lease("lane:codex-1:slot:0", record["holder"])
    service._save_probe(record)
    return record


def submitted(service, harness):
    return service.dispatch("submit", harness.submit_args(tier="hard"))["job_id"]


def exit_receipts(record):
    directory = Path(record["directory"])
    launch = Launch(("fake",), {}, (), str(directory), None, str(directory / "stdout"),
                    str(directory / "stderr"), None, None)
    value = dataclasses.asdict(launch)
    value.pop("env_add")
    (directory / "launch.json").write_text(json.dumps(value))
    (directory / "exit.json").write_text(json.dumps({"rc": 0, "signal": None, "wall_s": .1, "child_pid": 900002}))


@pytest.mark.parametrize("change", [{"owner": "v1"}, {"enabled": False}, {"desktop": True}])
def test_c11_probe_rechecks_lane_before_reserving_after_selection(routing_state, monkeypatch, change):
    """C-10.3, C-11.2: rollback or an operator change wins before the probe lease."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    original_pick = service._pick
    selected = []
    calls = []

    def pick_then_change(*args, **kwargs):
        decision = original_pick(*args, **kwargs)
        assert decision.chosen_lane == "codex-1"
        assert not service.store.conn.in_transaction
        service.store.update_lane(decision.chosen_lane, **change)
        selected.append(decision.chosen_lane)
        return decision

    def probe(*args):
        calls.append(args)
        return Outcome(OutcomeClass.UNKNOWN, "unexpected provider call")

    monkeypatch.setattr(service, "_pick", pick_then_change)
    monkeypatch.setattr(service, "_execute_probe", probe)
    service._admit()
    assert selected == ["codex-1"]
    assert calls == []
    assert service.store.list_leases() == []
    assert service.store.list_attempts(job_id) == []
    assert service.store.query("SELECT 1 FROM events WHERE kind='probe.reserved'") == []


def test_c5_probe_restart_accepts_receipt_only_after_verified_empty(routing_state, monkeypatch):
    """C-5.3–7, C-8.4, C-11.4: recovered probes record evidence and release after containment."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    record = reserved_probe(service, job_id)
    exit_receipts(record)
    monkeypatch.setattr(service, "_probe_census", lambda record: procs.Containment())
    monkeypatch.setattr(procs, "same_process", lambda *args: False)
    service._recover_probes()
    assert not service.store.list_leases(record["holder"])
    assert service._probe_record(record["holder"])["state"] == "completed"
    assert service.store.list_readings()[0]["label"] == "admission-observed"
    assert not service.store.list_attempts(job_id)
    assert not Path(record["directory"]).exists()


def test_c5_probe_quarantine_keeps_lease_and_never_signals_unowned_escape(routing_state, monkeypatch):
    """C-5.4–7, C-9.5: unknown escaped probe processes retain leases without a quota closure."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    record = reserved_probe(service, job_id, state="quarantined")
    exit_receipts(record)
    census = procs.Containment(marker_pids=frozenset({900003}),
                              identities={900003: procs.ProcessIdentity(900003, "boot", "escaped")})
    monkeypatch.setattr(service, "_probe_census", lambda record: census)
    monkeypatch.setattr(procs, "same_process", lambda *args: False)
    def forbidden(*args, **kwargs):
        raise AssertionError("an unowned process must not be signalled")
    monkeypatch.setattr(procs, "signal_group", forbidden)
    monkeypatch.setattr(procs, "signal_process", forbidden)
    service._recover_probes()
    assert service.store.list_leases(record["holder"])
    assert service.store.get_job(job_id)["wait_reason"] == "uncertain"
    assert not service.store.list_closures()
    assert Path(record["directory"]).exists()
    snapshot = service._capacity_view()
    assert snapshot["in_flight"]["codex-1"] == 0
    assert snapshot["unavailable_lanes"]["codex-1"] == record["holder"]
    assert "probe=quarantined" in service.dispatch("daemon.status", {})["status"]


def test_c5_probe_reservation_before_launch_recovers_without_dispatch(routing_state, monkeypatch):
    """C-5.1, C-8.4: a pre-gate crash cannot launch a provider or become a work attempt."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    record = reserved_probe(service, job_id, state="reserved")
    record.pop("guardian_pid")
    record.pop("pgid")
    service._save_probe(record)
    monkeypatch.setattr(service, "_probe_census", lambda record: procs.Containment())
    service._recover_probes()
    assert not service.store.list_leases(record["holder"])
    assert service._probe_record(record["holder"])["outcome"]["cls"] == "unknown"
    assert not service.store.list_attempts(job_id)
    assert not service.store.list_readings()


def test_c5_probe_exception_after_spawn_keeps_unverifiable_lease(routing_state, monkeypatch):
    """C-5.5–7: an exception after guardian start never releases unverified probe ownership."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    record = reserved_probe(service, job_id)
    service.term_grace_s = 0
    def failed(*args):
        raise OSError("receipt unavailable")
    monkeypatch.setattr(service, "_execute_probe", failed)
    monkeypatch.setattr(service, "_probe_census", lambda record: procs.Containment(unverifiable=True))
    monkeypatch.setattr(procs, "same_process", lambda *args: False)
    decision = SimpleNamespace(chosen_lane="codex-1", chosen_model="astra")
    outcome = service._probe_candidate(service.store.get_job(job_id), decision, record["holder"])
    assert outcome.evidence["probe_quarantined"]
    assert service._probe_record(record["holder"])["state"] == "quarantined"
    assert service.store.list_leases(record["holder"])


def test_c5_probe_gate_opens_after_durable_identity_and_readonly_launch(routing_state, monkeypatch):
    """C-5.1, C-10.5, C-11.4: probe guardian ownership commits before gated readonly dispatch."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    record = reserved_probe(service, job_id, state="reserved")
    job = service.store.get_job(job_id)
    lane = service.store.get_lane("codex-1")
    model = service.policy["models"]["astra"]
    writes = []
    original_close, original_write = daemon_module.os.close, daemon_module.os.write
    original_build = FakeAdapter.build_launch
    def build(adapter, spec, *args, **kwargs):
        assert spec.sandbox == "read-only"
        assert spec.kind == "probe"
        return original_build(adapter, spec, *args, **kwargs)
    monkeypatch.setattr(FakeAdapter, "build_launch", build)
    monkeypatch.setattr(daemon_module.os, "pipe", lambda: (800, 801))
    monkeypatch.setattr(daemon_module.os, "close", lambda fd: None if fd in (800, 801) else original_close(fd))
    def release(fd, value):
        if fd != 801:
            return original_write(fd, value)
        stored = service._probe_record(record["holder"])
        assert stored["state"] == "starting"
        assert stored["guardian_pid"] == 900001
        assert stored["proc_start"] == "fixture-start"
        assert not service.store.conn.in_transaction
        writes.append((fd, value))
    monkeypatch.setattr(daemon_module.os, "write", release)
    def spawn(command, **kwargs):
        assert command[1:3] == ["-m", "subfleet.guardian"]
        assert "--launch-fd" in command
        assert kwargs["env"]["SUBFLEET_ATTEMPT"] == record["holder"]
        assert all(key not in kwargs["env"] for key in ("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"))
        return SimpleNamespace(pid=900001)
    monkeypatch.setattr(daemon_module.subprocess, "Popen", spawn)
    def awaited(value, child):
        value["state"] = "contained"
        service._save_probe(value)
        return True, {"rc": 0, "signal": None, "wall_s": .1, "child_pid": 900002}
    monkeypatch.setattr(service, "_await_probe", awaited)
    outcome = service._execute_probe(job, lane, model, record["holder"])
    assert outcome.cls == OutcomeClass.OK
    assert writes == [(801, b"1")]
    launch = json.loads((Path(record["directory"]) / "launch.json").read_text())
    assert "env_add" not in launch
    assert "probe" in launch["cwd"]


def test_c6_capacity_waiter_cannot_be_bypassed_by_newer_same_tier(routing_state):
    """C-4.1, C-6.4; amendment 11: FIFO retains an older capacity waiter's place."""
    service, harness = routing_state
    first = service.dispatch("submit", harness.submit_args())["job_id"]
    second = service.dispatch("submit", harness.submit_args())["job_id"]
    service.store.update_job(first, state="waiting", wait_reason="capacity", next_check_at=after(60))
    service._admit()
    assert not service.store.list_attempts(second)
    service.store.update_job(first, next_check_at=utcnow())
    service._admit()
    assert service.store.list_attempts(first)
    assert not service.store.list_attempts(second)
