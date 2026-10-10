"""C-6.17: the real admission pass, fake disk, no providers or background workers."""
from __future__ import annotations

import json
import os
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


def rulings(daemon):
    return [json.loads(row["data_json"]) for row in daemon.store.query(
        "SELECT data_json FROM events WHERE kind='admission.disk_floor' ORDER BY event_id")]


def test_timed_floor_expiry_visibility_events_and_no_store_lock(state_daemon, monkeypatch):
    from tests.disk_floor_model import Files, LOWER, RAISE, file, lowering
    daemon, harness = state_daemon
    clock = fake_clock(monkeypatch)
    enable(daemon, monkeypatch, 34)
    daemon.policy["admission"]["disk"].update(lower_path=LOWER, raise_path=RAISE)
    files = Files()

    def read(path):
        import threading
        held = daemon.store._lock.held
        assert held is None or held[0] != threading.get_ident()
        return files(path)

    daemon._disk.read_ruling = read
    waiting = submit(daemon, harness)
    daemon._admit()
    assert daemon._disk.holding and not placed(daemon)
    assert not rulings(daemon)
    files.files[LOWER] = file(lowering(until=100, why="overnight ruling"))
    clock[0] = 1
    daemon._admit()
    assert len(placed(daemon)) == 1
    status = daemon.dispatch("daemon.status", {})
    assert status["disk"]["floor_gb"] == 30 and status["disk"]["resume_margin_gb"] == 0
    assert "lowered until" in status["status"] and "overnight ruling" in status["status"]
    why = daemon.dispatch("why", {"job_id": waiting})
    assert why["disk"]["floor_gb"] == 30 and "lowered until" in why["text"]
    daemon._admit()
    assert len(rulings(daemon)) == 1
    # No queued jobs: the next pass must still restore and latch the policy.
    clock[0] = 100
    daemon._admit()
    assert daemon._disk.snapshot["floor_gb"] == 40
    assert daemon._disk.snapshot["resume_margin_gb"] == 5 and daemon._disk.holding
    waiting = submit(daemon, harness)
    daemon._admit()
    why = daemon.dispatch("why", {"job_id": waiting})
    assert "floor 40 GB" in why["text"] and "policy; lower_expired:" in why["text"]
    status = daemon.dispatch("daemon.status", {})
    assert "policy; lower_expired:" in status["status"]
    assert [event["floor_source"] for event in rulings(daemon)] == [
        f"lowered until {stamp(100)} by Max", "policy"]
    assert files.reads == [LOWER, RAISE] * 5


def test_raise_lower_precedence_and_source_transitions(state_daemon, monkeypatch):
    from tests.disk_floor_model import Files, LOWER, RAISE, file, lowering
    daemon, harness = state_daemon
    fake_clock(monkeypatch)
    enable(daemon, monkeypatch, 0)
    daemon.policy["admission"]["disk"].update(lower_path=LOWER, raise_path=RAISE,
                                             floor_gb=60, resume_margin_gb=7)
    files = Files(file(lowering()), file({"floor_gb": 35, "until": stamp(100)}))
    daemon._disk.read_ruling = files
    job = submit(daemon, harness)
    daemon._admit()
    assert daemon._disk.snapshot["floor_gb"] == 35  # Agent's pass_once beats lower, even below policy.
    assert daemon._disk.snapshot["resume_margin_gb"] == 7
    assert "raised until" in daemon.dispatch("why", {"job_id": job})["text"]
    daemon._admit()
    assert len(rulings(daemon)) == 1
    files.files[RAISE] = file({"floor_gb": 25, "until": stamp(100)})
    daemon._admit()
    assert daemon._disk.snapshot["floor_gb"] == 30 and daemon._disk.snapshot["resume_margin_gb"] == 0
    assert "lowered until" in daemon.dispatch("why", {"job_id": job})["text"]
    files.files[LOWER] = None
    daemon._admit()
    assert daemon._disk.snapshot["floor_gb"] == 60 and daemon._disk.snapshot["floor_source"] == "policy"
    assert len(rulings(daemon)) == 3
    files.files[LOWER] = OSError("cannot read")
    files.files[RAISE] = (b"broken JSON", epoch(stamp(0)))
    daemon._admit()
    why = daemon.dispatch("why", {"job_id": job})["text"]
    assert "lower_error:" in why and "override_error:" in why
    assert len(rulings(daemon)) == 3


@pytest.mark.parametrize("restart_at", [1, 3600], ids=["still-live", "expired-offline"])
@pytest.mark.parametrize("edit_same_source", [False, True])
def test_restart_recovers_floor_source_and_rechecks_offline_expiry(state_daemon, monkeypatch, restart_at, edit_same_source):
    from tests.disk_floor_model import Files, LOWER, RAISE, file, lowering
    daemon, harness = state_daemon
    clock = fake_clock(monkeypatch)
    enable(daemon, monkeypatch, 34)
    daemon.policy["admission"]["disk"].update(lower_path=LOWER, raise_path=RAISE)
    files = Files(file(lowering(40, release_margin_gb=5) if edit_same_source else lowering()))
    daemon._disk.read_ruling = files
    daemon._admit()
    assert len(rulings(daemon)) == 1
    if edit_same_source:
        assert daemon._disk.holding
        files.files[LOWER] = file(lowering())
        daemon._admit()
        assert not daemon._disk.holding
        assert len(rulings(daemon)) == 1
    (daemon.root / "policy.json").write_text(json.dumps(daemon.policy))
    daemon.close()
    clock[0] = restart_at
    restarted = daemon_module.Daemon(harness.root)
    try:
        restarted._disk.read_ruling = files
        restarted._admit()
        if restart_at < 3600:
            assert len(rulings(restarted)) == 1
            assert restarted._disk.snapshot["floor_gb"] == 30
        else:
            assert len(rulings(restarted)) == 2
            assert restarted._disk.snapshot["floor_gb"] == 40
            assert restarted._disk.snapshot["resume_margin_gb"] == 5 and restarted._disk.holding
    finally:
        restarted.close()


@pytest.mark.parametrize("fields", [
    {"floor_gb": "Infinity"},
    {"release_margin_gb": "Infinity"},
    {"why": float("nan")},
    {"why": {"value": float("inf")}},
    {"ruling": "\ud800"},
    {"why": "\ud800"},
], ids=["infinite-floor", "infinite-margin", "nan-why", "nested-infinite-why",
        "surrogate-ruling", "surrogate-why"])
def test_review_live_ruling_status_is_valid_wire_json(state_daemon, monkeypatch, fields):
    from subfleet import protocol
    from tests.disk_floor_model import Files, LOWER, file, lowering
    daemon, harness = state_daemon
    fake_clock(monkeypatch)
    reading = enable(daemon, monkeypatch, 34)
    daemon.policy["admission"]["disk"].update(lower_path=LOWER, raise_path=None)
    daemon._disk.read_ruling = Files(file(lowering(**fields)))
    daemon._admit()
    result = daemon.dispatch("daemon.status", {})

    def forbidden(token):
        raise ValueError("wire contains non-JSON constant: " + token)

    wire = protocol.encode({"v": 1, "id": "review", "ok": True, "result": result})
    json.loads(wire, parse_constant=forbidden)
    assert result["disk"]["holding"] == ("floor_gb" in fields)
    for key in ("floor_gb", "resume_margin_gb"):
        if result["disk"][key] == "inf":
            assert result["disk"][key + "_raw"] == "Infinity"

    job = submit(daemon, harness)
    reading["gb"] = 0
    daemon._admit()
    assert daemon._holds[job]["reason"] == "disk"
    for op, args in (("daemon.status", {}), ("why", {"job_id": job})):
        result = daemon.dispatch(op, args)
        wire = protocol.encode({"v": 1, "id": "review", "ok": True, "result": result})
        json.loads(wire, parse_constant=forbidden)
    rows = daemon.store.query("SELECT data_json FROM events WHERE kind='admission.disk_floor'")
    assert len(rows) == 1
    for row in rows:
        event = json.loads(row["data_json"], parse_constant=forbidden)
        protocol.encode(event)
    # Notices sign the same reported holds; they must also encode strictly.
    daemon.conversations.note_holds(daemon._holds)
    for signature, _ in daemon.conversations._noted.values():
        json.loads(signature, parse_constant=forbidden)


@pytest.mark.parametrize("fields", [
    {"floor_gb": "Infinity"},
    {"why": {"\ud800": [float("nan"), float("-inf"), "\udfff"]}},
])
def test_raise_ruling_evidence_is_safe(state_daemon, monkeypatch, fields):
    from subfleet import protocol
    from tests.disk_floor_model import Files, RAISE, file
    daemon, harness = state_daemon
    fake_clock(monkeypatch)
    enable(daemon, monkeypatch, 34)
    daemon.policy["admission"]["disk"].update(lower_path=None, raise_path=RAISE)
    daemon._disk.read_ruling = Files(raised=file({"floor_gb": 80, "until": stamp(100), **fields}))
    job = submit(daemon, harness)
    daemon._admit()
    assert daemon._disk.holding
    for result in (daemon.dispatch("daemon.status", {}), daemon.dispatch("why", {"job_id": job}),
                   *rulings(daemon)):
        json.dumps(result, allow_nan=False, ensure_ascii=False).encode("utf-8")
        protocol.encode(result)


def test_review_restart_after_same_source_offline_edit(state_daemon, monkeypatch, tmp_path):
    from tests.disk_floor_model import BASE, lowering
    daemon, harness = state_daemon
    fake_clock(monkeypatch)
    enable(daemon, monkeypatch, 34)
    path = tmp_path / "lower.json"
    daemon.policy["admission"]["disk"].update(lower_path=str(path), raise_path=None)

    def write(value):
        path.write_text(json.dumps(value))
        os.utime(path, (BASE.timestamp(), BASE.timestamp()))

    write(lowering())
    daemon._admit()
    assert not daemon._disk.holding and len(rulings(daemon)) == 1
    write(lowering(40, release_margin_gb=5))
    daemon._admit()
    assert daemon._disk.holding and len(rulings(daemon)) == 1
    (daemon.root / "policy.json").write_text(json.dumps(daemon.policy))
    daemon.close()
    write(lowering())
    restarted = daemon_module.Daemon(harness.root)
    try:
        restarted._admit()
        assert restarted._disk.snapshot["floor_gb"] == 30
        assert restarted._disk.snapshot["resume_margin_gb"] == 0
        assert restarted._disk.holding is False, "free34 at floor30/margin0 retained stale held latch"
    finally:
        restarted.close()


@pytest.mark.parametrize("fields", [
    {"floor_gb": "Infinity"}, {"release_margin_gb": "Infinity"}, {"ruling": "\ud800"},
], ids=["infinite-floor", "infinite-margin", "surrogate-source"])
def test_restart_rechecks_raw_ruling_and_keeps_safe_source_event(state_daemon, monkeypatch, tmp_path, fields):
    from tests.disk_floor_model import BASE, lowering
    daemon, harness = state_daemon
    fake_clock(monkeypatch)
    enable(daemon, monkeypatch, 0)
    path = tmp_path / "lower.json"
    path.write_text(json.dumps(lowering(**fields)))
    os.utime(path, (BASE.timestamp(), BASE.timestamp()))
    daemon.policy["admission"]["disk"].update(lower_path=str(path), raise_path=None)
    daemon._admit()
    assert daemon._disk.holding and len(rulings(daemon)) == 1
    (daemon.root / "policy.json").write_text(json.dumps(daemon.policy))
    daemon.close()
    restarted = daemon_module.Daemon(harness.root)
    try:
        restarted._admit()
        assert restarted._disk.holding and len(rulings(restarted)) == 1
        json.dumps(restarted.dispatch("daemon.status", {}), allow_nan=False, ensure_ascii=False).encode("utf-8")
    finally:
        restarted.close()
