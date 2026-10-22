"""D-TMP1: every daemon provider seam owns private attempt scratch."""

import json
import os
from pathlib import Path
import stat

import pytest

from subfleet import daemon as daemon_module, procs
from tests.fake.test_launch_env_daemon import daemon_spawn_environment
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401
from tests.fake.test_probe_recovery import reserved_probe


@pytest.mark.parametrize("mode", ["detached", "turn", "probe", "enrollment"])
def test_provider_starts_with_private_tmpdir(routing_state, monkeypatch, tmp_path, mode):
    ambient = tmp_path / "daemon-shared"
    ambient.mkdir()
    service, _ = routing_state
    monkeypatch.setattr(service, "_unlaunched", lambda *args, **kwargs: None)
    _, env = daemon_spawn_environment(routing_state, monkeypatch, mode,
                                      extra={"TMPDIR": str(ambient)})
    folder = Path(env["TMPDIR"])
    assert folder != ambient
    assert folder.is_dir() and not folder.is_symlink()
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert folder.stat().st_uid == os.getuid()
    assert service.root not in folder.parents
    if mode in {"detached", "turn"}:
        attempt = service.store.query("SELECT * FROM attempts")[0]
        assert json.loads(attempt["evidence_json"])["tmpdir"]["path"] == str(folder)
    else:
        record = service._probe_record(env["SUBFLEET_ATTEMPT"])
        assert record["tmpdir"]["path"] == str(folder)


def test_two_live_attempts_never_share_tmpdir(routing_state, monkeypatch, tmp_path):
    service, _ = routing_state
    monkeypatch.setattr(service, "_unlaunched", lambda *args, **kwargs: None)
    folders = []
    for _ in range(2):
        _, env = daemon_spawn_environment(routing_state, monkeypatch, "detached",
                                          extra={"TMPDIR": str(tmp_path)})
        folders.append(Path(env["TMPDIR"]))
    assert folders[0] != folders[1]
    assert all(folder.is_dir() for folder in folders)


@pytest.mark.parametrize("quarantined", [False, True])
def test_finalization_removes_only_clean_attempt_tmpdir(routing_state, monkeypatch, tmp_path, quarantined):
    service, _ = routing_state
    monkeypatch.setattr(service, "_unlaunched", lambda *args, **kwargs: None)
    with monkeypatch.context() as patch:
        _, env = daemon_spawn_environment(routing_state, patch, "detached",
                                          extra={"TMPDIR": str(tmp_path / "shared")})
    folder = Path(env["TMPDIR"])
    assert folder.exists(), "provider must start with its folder already created"
    (folder / "left-behind").write_text("scratch")
    attempt = service.store.query("SELECT * FROM attempts")[0]
    service.store.conn.execute("UPDATE attempts SET state='finalizing' WHERE attempt_id=?", (attempt["attempt_id"],))
    monkeypatch.setattr(service, "_contain", lambda a: procs.Containment(
        marker_pids=frozenset({12345}) if quarantined else frozenset()))
    service.exit_settle_s = 0
    service._launch_failure(attempt, "dummy failed launch")
    service._finalize(service.store.get_attempt(attempt["attempt_id"]))
    assert folder.exists() == quarantined
    evidence = json.loads(service.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert bool(evidence["tmpdir"].get("removed_at")) != quarantined
    if quarantined:
        assert service.store.get_attempt(attempt["attempt_id"])["state"] == "quarantined"
        monkeypatch.setattr(service, "_contain", lambda a: procs.Containment())
        service._sweep_attempt_tmpdirs()
        assert folder.exists(), "quarantine needs explicit resolution even after its census clears"


def test_retention_cadence_sweeps_even_when_archiving_is_dormant(routing_state, monkeypatch):
    service, _ = routing_state
    calls = []
    monkeypatch.setattr(service, "_sweep_attempt_tmpdirs", lambda: calls.append("swept"))
    monkeypatch.setenv("SUBFLEET_RETENTION_DORMANT", "1")
    service._retention()
    assert calls == ["swept"]


def terminal_leftover(routing_state):
    service, harness = routing_state
    job_id = service.dispatch("submit", harness.submit_args())["job_id"]
    service._admit()
    attempt = service.store.list_attempts(job_id)[0]
    folder = Path(service._prepare_attempt_tmpdir(attempt))
    (folder / "left-behind").write_text("dummy scratch")
    service.store.conn.execute("UPDATE attempts SET state='failed' WHERE attempt_id=?", (attempt["attempt_id"],))
    service.store.conn.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (job_id,))
    return attempt, folder


def test_daemon_start_sweeps_a_clean_terminal_leftover(routing_state, monkeypatch):
    service, _ = routing_state
    attempt, folder = terminal_leftover(routing_state)
    root = service.root
    service.close()
    monkeypatch.setattr(daemon_module.Daemon, "_contain", lambda self, a: procs.Containment())
    restarted = daemon_module.Daemon(root)
    try:
        assert not folder.exists()
        record = json.loads(restarted.store.get_attempt(attempt["attempt_id"])["evidence_json"])["tmpdir"]
        assert record["removed_at"] and record["bytes_at_cleanup"] >= len("dummy scratch")
    finally:
        restarted.close()


def test_daemon_sweep_waits_for_terminal_attempt_census_to_clear(routing_state, monkeypatch):
    service, _ = routing_state
    attempt, folder = terminal_leftover(routing_state)
    monkeypatch.setattr(service, "_contain", lambda a: procs.Containment(unverifiable=True))
    service._sweep_attempt_tmpdirs()
    assert folder.exists()
    monkeypatch.setattr(service, "_contain", lambda a: procs.Containment())
    service._sweep_attempt_tmpdirs()
    assert not folder.exists()
    assert json.loads(service.store.get_attempt(attempt["attempt_id"])["evidence_json"])["tmpdir"]["removed_at"]


@pytest.mark.parametrize("change", ["seen_identities", "tmpdir_keep"])
def test_cleanup_preserves_evidence_committed_during_its_census(routing_state, monkeypatch, change):
    service, _ = routing_state
    attempt, folder = terminal_leftover(routing_state)
    value = {"12345": {"pid": 12345, "boot_id": "dummy", "proc_start": "dummy"}} if change == "seen_identities" else True

    def census(a):
        current = service.store.get_attempt(a["attempt_id"])
        evidence = json.loads(current["evidence_json"])
        evidence[change] = value
        service.store.update_attempt(a["attempt_id"], evidence_json=json.dumps(evidence))
        return procs.Containment(unverifiable=change == "seen_identities")

    monkeypatch.setattr(service, "_contain", census)
    service._cleanup_attempt_tmpdir(attempt)
    evidence = json.loads(service.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence[change] == value
    assert folder.exists()
    assert "removed_at" not in evidence["tmpdir"]


def test_probe_scratch_is_kept_between_containment_and_finalization(routing_state, monkeypatch):
    service, harness = routing_state
    job_id = service.dispatch("submit", harness.submit_args())["job_id"]
    record = reserved_probe(service, job_id, state="contained")
    record["tmpdir"] = service._attempt_temp.create(record["holder"])
    service._save_probe(record)
    folder = Path(record["tmpdir"]["path"])
    monkeypatch.setattr(service, "_contain", lambda a: procs.Containment())
    service._sweep_attempt_tmpdirs()
    assert folder.exists(), "classification may still need scratch after containment"
    record["state"] = "completed"
    service._save_probe(record)
    service._sweep_attempt_tmpdirs()
    assert not folder.exists()
