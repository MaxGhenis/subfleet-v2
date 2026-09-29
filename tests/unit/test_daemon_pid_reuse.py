"""I6: normal acceptance uses the real census with recorded launch identities."""

import json
import time
from dataclasses import asdict

import pytest

from subfleet import procs, protocol
from tests.unit.test_containment_identity import BOOT, REUSED_START, START, recorded
from tests.unit.test_daemon_settle import (ATTEMPT, JOB, attempt, daemon, publish_receipt,
                                         with_launch)  # noqa: F401 (fixture)


def synthetic_processes(monkeypatch, rows):
    monkeypatch.setattr(procs, "snapshot", lambda: procs.ProcessTable(rows, BOOT))

    def markers(argv, **kwargs):
        assert argv == ["/bin/ps", "-axEww", "-o", "pid=,command="]
        return ""

    monkeypatch.setattr(procs, "_read", markers)
    monkeypatch.setattr(procs, "signal_process", lambda *a, **k: pytest.fail("unexpected process signal"))
    monkeypatch.setattr(procs, "signal_group", lambda *a, **k: pytest.fail("unexpected group signal"))


@pytest.mark.parametrize("reused_guardian", [False, True], ids=["r218-child", "guardian-with-child"])
def test_i6_only_reused_pids_finalize_normally_and_reach_salvage(daemon, monkeypatch, reused_guardian):
    """Exercise _finalize -> _contain -> containment, with fake adapter/temp store."""
    with_launch(daemon, monkeypatch)
    publish_receipt(daemon)
    identities = {str(pid): asdict(recorded(pid)) for pid in (4242, 4243)}
    daemon.store.update_attempt(ATTEMPT, state="finalizing", rc=0, boot_id=BOOT, proc_start=START,
                                evidence_json=json.dumps({"owned_identities": identities}))
    if reused_guardian:
        rows = {4242: (1, 4242, "S", REUSED_START),
                4243: (4242, 4242, "S", REUSED_START),
                4244: (4243, 4244, "S", REUSED_START)}
    else:
        rows = {4243: (76443, 76443, "S", REUSED_START)}
    synthetic_processes(monkeypatch, rows)
    salvaged = []
    daemon._salvage = lambda job, row: (salvaged.append(row["attempt_id"]) or [], None)
    # The old census quarantined immediately once the already-started settle
    # window expired. The fixed census must accept in this exact condition.
    daemon._exit_settle[ATTEMPT] = time.monotonic() - daemon.exit_settle_s - 1

    daemon._finalize(attempt(daemon))

    finished = attempt(daemon)
    assert finished["state"] == "succeeded" and finished["rc"] == 0
    assert finished["quarantine_reason"] is None
    job = daemon.store.get_job(JOB)
    assert job["state"] == "succeeded" and job["rc"] == 0
    assert job["accepted_attempt_id"] == ATTEMPT
    assert salvaged == [ATTEMPT]
    assert ATTEMPT not in daemon._exit_settle
    kinds = [row["kind"] for row in daemon.store.list_events(JOB)]
    assert "attempt.accepted" in kinds
    assert "attempt.quarantined" not in kinds


def test_probe_containment_uses_launch_row_and_direct_owned_identities(daemon, monkeypatch):
    synthetic_processes(monkeypatch, {4242: (1, 900, "S", REUSED_START),
                                      4243: (1, 900, "S", REUSED_START)})
    record = {"holder": "probe:timer:codex-1", "job_id": JOB, "lane_id": "codex-1",
              "state": "running", "guardian_pid": 4242, "child_pid": 4243, "pgid": 4242,
              "boot_id": BOOT, "proc_start": START,
              "owned_identities": {"4243": asdict(recorded(4243))}}
    assert daemon._contain_probe(record)
    assert record["state"] == "contained"
    assert record["containment"]["reused_pids"] == [4242, 4243]
    assert record["containment"]["live_pids"] == []
    assert not any(row["kind"] == "probe.quarantined" for row in daemon.store.list_events(JOB))


def test_quarantine_resolution_uses_attempt_recorded_identities(daemon, monkeypatch):
    synthetic_processes(monkeypatch, {4243: (900, 900, "S", REUSED_START)})
    daemon.store.update_attempt(ATTEMPT, state="quarantined", boot_id=BOOT, proc_start=START,
                                evidence_json=json.dumps({"owned_identities": {
                                    "4243": asdict(recorded(4243))}}))
    with daemon.store.transaction("test.lease") as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES (?,?,?)",
                   ("lane:codex-1", ATTEMPT, "2026-09-28T22:11:51Z"))
    salvaged = []
    daemon._salvage = lambda job, row: (salvaged.append(row["attempt_id"]) or [], None)

    daemon._resolve_quarantine(attempt(daemon), protocol.KillArgs(job_id=JOB))

    assert attempt(daemon)["state"] == "lost"
    assert salvaged == [ATTEMPT]
    assert not daemon.store.query("SELECT * FROM leases WHERE holder=?", (ATTEMPT,))
    event = next(row for row in daemon.store.list_events(JOB) if row["kind"] == "quarantine.confirmed_dead")
    assert json.loads(event["data_json"])["containment"]["reused_pids"] == [4243]
