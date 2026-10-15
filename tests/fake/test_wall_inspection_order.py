"""D-WALL1: a wall action requires inspected liveness, and loss ends at observed liveness."""
import json
from datetime import timedelta

import pytest

from subfleet import daemon as daemon_module, procs
from tests.fake.test_review_r223_wall_probes import clocked, mark_starting  # noqa: F401
from tests.fake.test_state_contract import reserve, state_daemon  # noqa: F401


def running(clocked, monkeypatch):
    service, harness, clock = clocked
    jid, a, _ = reserve(service, harness)
    mark_starting(service, a["attempt_id"])
    service.store.update_attempt(a["attempt_id"], state="running", guardian_pid=424242,
                                 pgid=424242, boot_id="unit-test-boot", proc_start="fixture-start")
    monkeypatch.setattr(service, "_inspect_running", daemon_module.Daemon._inspect_running.__get__(service))
    return service, clock, jid, a["attempt_id"]


def test_confirmed_liveness_is_durable_and_bounds_later_loss(clocked, monkeypatch):
    service, clock, jid, aid = running(clocked, monkeypatch)
    clock.t += timedelta(hours=1)
    table = procs.ProcessTable({424242: (1, 424242, "S", "fixture-start")}, "unit-test-boot")
    monkeypatch.setattr(service, "_process_table", lambda due: (table, due + 1))
    service._process_attempt(aid)
    observed = daemon_module.utcnow_ms()
    saved = json.loads(service.store.get_attempt(aid)["evidence_json"])
    assert saved["last_observed_alive_at"] == observed
    # A fresh daemon's first pass has no in-memory liveness or pacing history.
    service._inspect_next.clear()
    clock.t += timedelta(hours=6)
    monkeypatch.setattr(service, "_process_table", lambda due: (procs.ProcessTable({}, "new-boot"), due + 1))
    monkeypatch.setattr(procs, "liveness", lambda *args: "dead")
    service._process_attempt(aid)
    assert service.store.get_attempt(aid)["finished_at"] == observed
    assert service.store.get_attempt(aid)["state"] == "lost"
    assert service.store.get_job(jid)["state"] == "waiting"
    assert service._running_s(jid) == 3600


@pytest.mark.parametrize("answer", ["unknown", "dead", "alive"])
def test_only_confirmed_running_provider_can_be_wall_cancelled(clocked, monkeypatch, answer):
    service, clock, jid, aid = running(clocked, monkeypatch)
    service.store.update_job(jid, max_wall_s=3600)
    clock.t += timedelta(hours=7)
    empty = procs.ProcessTable({}, "unit-test-boot")
    live = procs.ProcessTable({424242: (1, 424242, "S", "fixture-start")}, "unit-test-boot")
    monkeypatch.setattr(service, "_process_table", lambda due: (empty, due + 1))
    monkeypatch.setattr(procs, "liveness", lambda *args: answer)
    monkeypatch.setattr(procs, "snapshot", lambda: live)
    killed = []
    monkeypatch.setattr(service, "_kill_attempt", lambda a: killed.append(a["attempt_id"]))
    service._process_attempt(aid)
    job = service.store.get_job(jid)
    assert bool(job["cancel_requested_at"]) == (answer == "alive")
    assert killed == ([aid] if answer == "alive" else [])
    if answer == "dead":
        assert service.store.get_attempt(aid)["state"] == "lost"
        assert service._running_s(jid) == 0  # Legacy row: only the launch is known alive.
        assert job["state"] == "waiting"
    elif answer == "unknown":
        assert service.store.get_attempt(aid)["state"] == "running"
