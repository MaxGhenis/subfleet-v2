"""C-6.17: the real admission pass, fake disk, no providers or background workers."""
from __future__ import annotations

import json
import pytest

from subfleet import daemon as daemon_module
from subfleet.disk import DiskAdmission, GB, epoch
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.fake.test_admission_latency import measure, submit_turn
from tests.unit.test_disk_admission import stamp


@pytest.fixture(autouse=True)
def non_git_workspace(monkeypatch):
    # Harness.workdir is an empty non-git directory. Preserve that answer
    # without spawning git for each of the 70 simulated submissions.
    monkeypatch.setattr(daemon_module, "git_head", lambda *args, **kwargs: None)
    monkeypatch.setattr(daemon_module, "git_toplevel", lambda *args, **kwargs: None)


def enable(daemon, monkeypatch, free=46):
    reading = {"gb": free, "reads": 0, "paths": []}

    def read(path):
        reading["reads"] += 1
        reading["paths"].append(path)
        return int(reading["gb"] * GB)

    daemon.policy["admission"]["disk"]["enabled"] = True
    monkeypatch.setattr("subfleet.disk.free_bytes", read)
    monkeypatch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
    return reading


def submit(daemon, harness, **changes):
    return daemon.dispatch("submit", harness.submit_args(**changes))["job_id"]


def placed(daemon):
    return daemon.store.query("SELECT * FROM attempts ORDER BY rowid")


def end(daemon, row, now):
    # Execution ending releases disk even while artifact finalization is live.
    with daemon.store.transaction("test.attempt_end") as tx:
        tx.execute("UPDATE attempts SET state='finalizing',finished_at=? WHERE attempt_id=?", (now, row["attempt_id"]))


def fake_clock(monkeypatch):
    clock = [0]
    monkeypatch.setattr(daemon_module, "utcnow", lambda: stamp(clock[0]))
    monkeypatch.setattr(daemon_module, "age", lambda timestamp:
                        epoch(stamp(clock[0])) - epoch(timestamp) if timestamp else 0)
    return clock


def test_stampede_70_jobs_39_to_46_gb_and_later_passes(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    clock = fake_clock(monkeypatch)
    reading = enable(daemon, monkeypatch, 39)
    jobs = [submit(daemon, harness) for _ in range(70)]
    daemon._admit()
    assert not placed(daemon)
    assert daemon._admission["reasons"] == {"disk": 70}
    reading["gb"] = 46
    daemon._admit()
    assert len(placed(daemon)) == 4
    assert daemon._disk.snapshot["reserved_gb"] == 6
    daemon._admit()
    assert len(placed(daemon)) == 4
    # Hysteresis remains latched after one release (effective 41.5).
    end(daemon, placed(daemon)[0], stamp(clock[0]))
    assert daemon.dispatch("daemon.status", {})["disk"]["reserved_gb"] == 4.5
    daemon._admit()
    assert len(placed(daemon)) == 4
    for row in placed(daemon)[1:4]:
        end(daemon, row, stamp(clock[0]))
    daemon._admit()
    assert len(placed(daemon)) == 8
    clock[0] = 599
    daemon._admit()
    assert len(placed(daemon)) == 8
    clock[0] = 600
    daemon._admit()
    assert len(placed(daemon)) == 12
    assert daemon._disk.snapshot["reserved_gb"] == 6
    assert reading["reads"] == 7
    assert len(jobs) == 70


@pytest.mark.parametrize("caller", ["chosen", "ordinary"])
def test_priority_and_background_hold_before_any_workspace(state_daemon, monkeypatch, caller):
    daemon, harness = state_daemon
    enable(daemon, monkeypatch, 39)
    daemon.policy["admission"]["priority_callers"] = ["chosen"]
    job = submit(daemon, harness, caller_session=caller)

    def forbidden(job):
        raise AssertionError("a disk-held job must not prepare a workspace")

    monkeypatch.setattr(daemon, "_workspace", forbidden)
    daemon._admit()
    assert not placed(daemon)
    hold = daemon._holds[job]
    assert (hold["reason"], hold["free_gb"], hold["reserved_gb"], hold["floor_gb"]) == ("disk", 39, 0, 40)
    why = daemon.dispatch("why", {"job_id": job})
    assert "free 39.0 GB, reserved 0.0 GB, floor 40 GB" in why["text"]
    status = daemon.dispatch("daemon.status", {})
    assert "disk: free 39.00 GB, reserved 0.00 GB, floor 40 GB; holding" in status["status"]
    from subfleet.cli import format_status
    assert format_status(status).count("disk:") == 1


def test_attended_turn_pass_neither_reads_disk_nor_reserves(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    reading = enable(daemon, monkeypatch, 0)
    measure(daemon, "codex-1")
    monkeypatch.setattr(daemon.conversations, "admission_hold", lambda job: None)
    turn = submit_turn(daemon, harness, 1)
    daemon._admit_turns()
    assert reading["reads"] == 0
    assert [row["job_id"] for row in placed(daemon)] == [turn]
    assert daemon._disk.reserved_bytes == 0
    assert "disk_reservation" not in json.loads(placed(daemon)[0]["evidence_json"])
    background = submit(daemon, harness)
    daemon._admit()
    assert daemon._holds[background]["reason"] == "disk"
    assert daemon._disk.reserved_bytes == 0


def test_restart_rebuilds_committed_reservations_and_retry_reserves_again(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    enable(daemon, monkeypatch)
    clock = fake_clock(monkeypatch)
    job = submit(daemon, harness)
    daemon._admit()
    row = placed(daemon)[0]
    assert json.loads(row["evidence_json"])["disk_reservation"] == {"bytes": 1.5 * GB, "ttl_s": 600}
    before = dict(daemon._disk.reservations)
    # Replace exactly the in-memory object a fresh daemon creates; read from
    # the durable attempt rows, with the real SQL projection used on restart.
    daemon._disk = DiskAdmission(daemon.root)
    daemon._admit()
    assert daemon._disk.reservations == before
    with daemon.store.transaction("test.retry") as tx:
        tx.execute("UPDATE attempts SET state='failed',finished_at=?,outcome_class='transient' WHERE attempt_id=?", (stamp(1), row["attempt_id"]))
        tx.execute("UPDATE jobs SET state='queued' WHERE job_id=?", (job,))
        tx.execute("DELETE FROM leases WHERE holder=?", (row["attempt_id"],))
    clock[0] = 1
    daemon._admit()
    assert len(placed(daemon)) == 2
    assert daemon._disk.reserved_bytes == 1.5 * GB
    assert row["attempt_id"] not in daemon._disk.reservations


def test_real_daemon_restart_preserves_budgets_and_hysteresis(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    reading = enable(daemon, monkeypatch)
    fake_clock(monkeypatch)
    for _ in range(2):
        submit(daemon, harness)
    daemon._admit()
    reading["gb"] = 39
    waiting = submit(daemon, harness)
    daemon._admit()
    assert daemon._disk.holding
    (daemon.root / "policy.json").write_text(json.dumps(daemon.policy))
    daemon.close()
    restarted = daemon_module.Daemon(harness.root)
    try:
        monkeypatch.setattr(restarted, "_workspace", lambda job: (job["workdir"], None, None, []))
        assert restarted._disk.reserved_bytes == 3 * GB
        assert restarted._disk.holding
        reading["gb"] = 47   # effective 44: enough for a placement, below resume
        restarted._admit()
        assert len(placed(restarted)) == 2
        assert restarted._holds[waiting]["reason"] == "disk"
        reading["gb"] = 49.5
        restarted._admit()
        assert len(placed(restarted)) == 3
    finally:
        restarted.close()


def test_optional_path_one_read_per_pass_and_unreadable_holds(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    reading = enable(daemon, monkeypatch)
    daemon.policy["admission"]["disk"]["path"] = "/fake/volume"
    for _ in range(8):
        submit(daemon, harness)
    daemon._admit()
    assert reading["paths"] == ["/fake/volume"]
    assert len(placed(daemon)) == 4

    def unreadable(path):
        raise OSError("injected unavailable volume")

    monkeypatch.setattr("subfleet.disk.free_bytes", unreadable)
    daemon._admit()
    assert len(placed(daemon)) == 4
    assert all(hold["reason"] == "disk" and "OSError" in hold["error"] for hold in daemon._holds.values())
    held = next(iter(daemon._holds))
    assert "Disk reading failed: OSError" in daemon.dispatch("why", {"job_id": held})["text"]


@pytest.mark.parametrize("enabled", [False, None])
def test_disabled_or_absent_is_the_existing_daemon_path(state_daemon, monkeypatch, enabled):
    daemon, harness = state_daemon
    if enabled is None:
        daemon.policy["admission"].pop("disk")

    def forbidden(path):
        raise AssertionError("off must not read disk")

    monkeypatch.setattr("subfleet.disk.free_bytes", forbidden)
    jobs = [submit(daemon, harness) for _ in range(7)]
    daemon._admit()
    assert [row["job_id"] for row in placed(daemon)] == jobs
    assert daemon._disk.reserved_bytes == 0
    assert all("disk_reservation" not in json.loads(row["evidence_json"]) for row in placed(daemon))
