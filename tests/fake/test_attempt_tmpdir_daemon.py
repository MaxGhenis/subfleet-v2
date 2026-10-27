"""D-TMP1: every daemon provider seam owns private attempt scratch."""

import json
import os
from pathlib import Path
import shutil
import stat

import pytest

from subfleet import daemon as daemon_module, procs, retention
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
    # Review r1 P3-7: as the platform spells its own, so "${TMPDIR}x" is inside.
    assert env["TMPDIR"] == str(folder) + "/"
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


def test_daemon_start_sweeps_a_clean_terminal_leftover_once_serving(routing_state, monkeypatch):
    """Review r1 P3-6: the startup sweep runs on a worker once recovery is done,
    never in the constructor."""
    service, _ = routing_state
    attempt, folder = terminal_leftover(routing_state)
    root = service.root
    service.close()
    censuses = []
    monkeypatch.setattr(daemon_module.Daemon, "_contain", lambda self, a: censuses.append(a) or procs.Containment())
    restarted = daemon_module.Daemon(root)
    try:
        assert folder.exists() and censuses == [], "constructing the daemon sweeps nothing"
        scheduled = []
        monkeypatch.setattr(restarted.timers, "start", lambda: None)
        monkeypatch.setattr(restarted, "_schedule", lambda key, fn, *args, **kwargs: scheduled.append(key) or fn(*args))
        restarted._recover_then_start_timers()
        assert restarted._recovery_complete.is_set() and "tmpdir-sweep" in scheduled
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


@pytest.mark.parametrize("verified", [False, True])
def test_cleanup_preserves_evidence_committed_during_its_census(routing_state, monkeypatch, verified):
    service, _ = routing_state
    attempt, folder = terminal_leftover(routing_state)
    value = {"12345": {"pid": 12345, "boot_id": "dummy", "proc_start": "dummy"}}

    def census(a):
        current = service.store.get_attempt(a["attempt_id"])
        evidence = json.loads(current["evidence_json"])
        evidence["seen_identities"] = value
        service.store.update_attempt(a["attempt_id"], evidence_json=json.dumps(evidence))
        return procs.Containment(unverifiable=not verified)

    monkeypatch.setattr(service, "_contain", census)
    service._cleanup_attempt_tmpdir(attempt)
    evidence = json.loads(service.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["seen_identities"] == value
    assert folder.exists() != verified
    assert ("removed_at" in evidence["tmpdir"]) == verified
    if verified:
        assert evidence["tmpdir"]["outcome"] == "removed" and evidence["tmpdir"]["cleared_at"]


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


@pytest.mark.parametrize("probe", [False, True])
def test_sweep_reclaims_known_allocation_after_provider_removes_marker(routing_state, monkeypatch, probe):
    service, _ = routing_state
    attempt, folder = terminal_leftover(routing_state)
    if probe:
        record = reserved_probe(service, attempt["job_id"], state="completed")
        record["tmpdir"] = service._attempt_temp.create(record["holder"])
        service._save_probe(record)
        folder = Path(record["tmpdir"]["path"])
    (folder / ".subfleet-attempt.json").unlink()
    monkeypatch.setattr(service, "_contain", lambda a: procs.Containment())
    service._sweep_attempt_tmpdirs()
    assert not folder.exists()
    evidence = (service._probe_record(record["holder"]) if probe else
                json.loads(service.store.get_attempt(attempt["attempt_id"])["evidence_json"]))
    assert evidence["tmpdir"]["removed_at"]


def test_launch_tries_of_one_attempt_share_one_allocation(routing_state, monkeypatch):
    """Review r1 P3-5: three launch tries made three folders and evidence named
    only the last. Every try of one attempt now uses one folder."""
    service, harness = routing_state
    job_id = service.dispatch("submit", harness.submit_args())["job_id"]
    service._admit()
    attempt = service.store.list_attempts(job_id)[0]
    given = []
    monkeypatch.setattr(service, "_spec", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("dummy later step")))
    original = service._prepare_attempt_tmpdir
    monkeypatch.setattr(service, "_prepare_attempt_tmpdir", lambda a: given.append(original(a)) or given[-1])
    for _ in range(3):
        with pytest.raises(OSError):
            service._launch(service.store.get_attempt(attempt["attempt_id"]))
    allocated = [entry for entry in service._attempt_temp.root.iterdir() if entry.name.startswith("subfleet-attempt-")]
    assert len(set(given)) == 1 and len(allocated) == 1
    evidence = json.loads(service.store.get_attempt(attempt["attempt_id"])["evidence_json"])
    assert evidence["tmpdir"]["path"] == given[0] == str(allocated[0])


@pytest.mark.parametrize("code", ["ENOSPC", "EACCES"])
def test_allocation_failure_is_a_launch_failure_with_its_reason(routing_state, monkeypatch, code):
    """Review r1 P3-5: ENOSPC from `create()` left the attempt reserved under backoff."""
    import errno
    service, harness = routing_state
    job_id = service.dispatch("submit", harness.submit_args())["job_id"]
    service._admit()
    attempt = service.store.list_attempts(job_id)[0]
    number = getattr(errno, code)
    failure = OSError(number, os.strerror(number))
    monkeypatch.setattr(service._attempt_temp, "create", lambda holder: (_ for _ in ()).throw(failure))
    failures = []
    monkeypatch.setattr(service, "_launch_failure", lambda a, detail, **kwargs: failures.append(detail))
    service._launch(attempt)
    assert failures == [f"attempt TMPDIR could not be allocated: {type(failure).__name__}: "
                        f"[Errno {number}] {os.strerror(number)}"]
    assert service.store.get_attempt(attempt["attempt_id"])["state"] == "reserved", "only `_launch_failure` ends it"


def test_allocation_failure_ends_the_attempt_not_left_reserved(routing_state, monkeypatch):
    import errno
    service, harness = routing_state
    job_id = service.dispatch("submit", harness.submit_args())["job_id"]
    service._admit()
    attempt = service.store.list_attempts(job_id)[0]
    monkeypatch.setattr(service._attempt_temp, "create",
                        lambda holder: (_ for _ in ()).throw(OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))))
    monkeypatch.setattr(service, "_contain", lambda a: procs.Containment())
    service._launch(attempt)
    assert service.store.get_attempt(attempt["attempt_id"])["state"] == "finalizing"
    receipt = json.loads((service.root / "jobs" / job_id / "a1" / "exit.json").read_text())
    assert receipt["spawn_error"].startswith("attempt TMPDIR could not be allocated: ")
    assert receipt["spawn_error"].endswith(f"[Errno {errno.ENOSPC}] {os.strerror(errno.ENOSPC)}")


def _probe(service, job_id, holder, *, state, tmpdir):
    directory = service.root / "lanes" / "codex-1" / "probes" / holder.rsplit(":", 1)[-1]
    directory.mkdir(parents=True)
    record = {"holder": holder, "job_id": job_id, "lane_id": "codex-1", "model_id": "gpt-6-astra",
              "directory": str(directory), "state": state, "created_at": "2026-10-23T00:00:00Z",
              "deadline_at": daemon_module._later("2026-10-23T00:00:00Z", service.policy["admission"]["probe_deadline_s"]),
              "owned_identities": {}, "tmpdir": tmpdir}
    service._save_probe(record)
    return record


def test_sweeps_over_held_scratch_take_no_repeated_census_and_write_nothing(routing_state, monkeypatch):
    """Review r1 P2-2: three sweeps over a held attempt and a quarantined probe
    added 6 censuses and 9 event rows and released nothing."""
    service, harness = routing_state
    quarantined, quarantined_folder = terminal_leftover(routing_state)
    service.store.conn.execute("UPDATE attempts SET state='quarantined' WHERE attempt_id=?", (quarantined["attempt_id"],))
    members, members_folder = terminal_leftover(routing_state)
    (members_folder / ".subfleet-attempt.json").unlink()
    job_id = service.dispatch("submit", harness.submit_args())["job_id"]
    present = _probe(service, job_id, "probe:held", state="quarantined",
                     tmpdir=service._attempt_temp.create("probe:held"))
    gone = _probe(service, job_id, "probe:gone", state="quarantined",
                  tmpdir=service._attempt_temp.create("probe:gone"))
    shutil.rmtree(gone["tmpdir"]["path"])
    censuses = []

    def census(subject):
        censuses.append(subject.get("attempt_id") or subject.get("holder"))
        return procs.Containment(marker_pids=frozenset({12345}))

    monkeypatch.setattr(service, "_contain", census)
    monkeypatch.setattr(service, "_probe_census", census)
    rows = []
    for _ in range(3):
        before = service.store.one("SELECT MAX(event_id) n FROM events")["n"]
        service._sweep_attempt_tmpdirs()
        rows.append(service.store.query("SELECT kind FROM events WHERE event_id>?", (before,)))
    assert censuses == [members["attempt_id"]], "one census, for the terminal attempt; none for quarantine"
    assert rows[1] == rows[2] == [], "a sweep that changes nothing writes nothing"
    assert {row["kind"] for row in rows[0]} >= {"probe.state"}
    assert quarantined_folder.is_dir() and members_folder.is_dir() and Path(present["tmpdir"]["path"]).is_dir()
    assert service._probe_record("probe:gone")["tmpdir"]["outcome"] == "absent"
    assert "removed_at" not in service._probe_record("probe:held")["tmpdir"]
    # The attempt's state changes: it is looked at again.
    service.store.conn.execute("UPDATE attempts SET state='lost' WHERE attempt_id=?", (quarantined["attempt_id"],))
    monkeypatch.setattr(service, "_contain", lambda subject: censuses.append("fresh") or procs.Containment())
    service._sweep_attempt_tmpdirs()
    assert censuses.count("fresh") == 1 and not quarantined_folder.exists()


def test_salvage_skipped_jobs_both_retire_whatever_their_scratch(routing_state, monkeypatch):
    """Review r1 P2-2: two identical salvage-skipped jobs, one with D-TMP1 evidence
    whose folder was gone; retention retired only the one shaped like base."""
    service, _ = routing_state
    jobs = []
    for with_tmpdir in (False, True):
        attempt, folder = terminal_leftover(routing_state)
        evidence = json.loads(service.store.get_attempt(attempt["attempt_id"])["evidence_json"])
        evidence["salvage_skipped"] = {"count": 1, "paths": ["nested"]}
        shutil.rmtree(folder)
        if not with_tmpdir:
            evidence.pop("tmpdir")             # shaped like a job from before D-TMP1
        service.store.update_attempt(attempt["attempt_id"], evidence_json=json.dumps(evidence))
        service.store.conn.execute("DELETE FROM leases WHERE holder IN (?,?)", (attempt["job_id"], attempt["attempt_id"]))
        service.store.conn.execute("UPDATE jobs SET finished_at='2026-10-22T00:00:00.000Z' WHERE job_id=?", (attempt["job_id"],))
        jobs.append(attempt["job_id"])
    monkeypatch.setattr(service, "_contain", lambda a: procs.Containment())
    service._sweep_attempt_tmpdirs()
    for _ in range(3):
        retention.maintenance(service.store, service.root, max_jobs=0, max_bytes=0, holders=lambda watches, **_: {})
    assert [service.store.get_job(job) for job in jobs] == [None, None]


def test_quarantine_released_by_confirm_dead_then_swept(routing_state, monkeypatch):
    """Quarantine resolves through `kill --confirm-dead`; the sweep then removes it."""
    service, _ = routing_state
    attempt, folder = terminal_leftover(routing_state)
    service.store.conn.execute("UPDATE attempts SET state='quarantined' WHERE attempt_id=?", (attempt["attempt_id"],))
    monkeypatch.setattr(service, "_contain", lambda a: procs.Containment())
    service._sweep_attempt_tmpdirs()
    assert folder.exists(), "quarantined: kept, whatever a census would say"
    service.store.conn.execute("UPDATE attempts SET state='lost' WHERE attempt_id=?", (attempt["attempt_id"],))
    service._sweep_attempt_tmpdirs()
    assert not folder.exists()
    tmpdir = json.loads(service.store.get_attempt(attempt["attempt_id"])["evidence_json"])["tmpdir"]
    assert tmpdir["outcome"] == "removed" and tmpdir["removed_at"]


def test_unknown_folder_is_aged_by_the_store_and_a_named_one_never_is(routing_state, monkeypatch):
    """Review r1 P3-4: known or unknown, and how old, come from the store."""
    service, _ = routing_state
    attempt, known = terminal_leftover(routing_state)
    service.store.conn.execute("UPDATE attempts SET state='running' WHERE attempt_id=?", (attempt["attempt_id"],))
    orphan = Path(service._attempt_temp.create("crashed-before-its-row")["path"])
    marker = orphan / ".subfleet-attempt.json"
    marker.write_text(json.dumps({**json.loads(marker.read_text()), "created_at": 0}))
    os.utime(orphan, (0, 0))
    (known / ".subfleet-attempt.json").write_text(marker.read_text())
    clock = [1_000_000.0]
    monkeypatch.setattr(daemon_module.attempt_tmp.time, "time", lambda: clock[0])
    service._sweep_attempt_tmpdirs()
    seen = [json.loads(row["data_json"]) for row in service.store.query(
        "SELECT data_json FROM events WHERE kind='tmpdir.unknown_seen' AND data_json<>'{}'")]
    assert [row["path"] for row in seen] == [str(orphan)] and seen[0]["first_seen_at"] == 1_000_000.0
    clock[0] += 3600
    service._sweep_attempt_tmpdirs()
    assert orphan.is_dir(), "the store saw it an hour ago, whatever its marker and birth time say"
    clock[0] += 86_400
    service._sweep_attempt_tmpdirs()
    assert not orphan.exists() and known.is_dir()
    removed = service.store.query("SELECT data_json FROM events WHERE kind='tmpdir.unknown_removed' AND data_json<>'{}'")
    assert [json.loads(row["data_json"])["path"] for row in removed] == [str(orphan)]


def test_tmpdir_cleanup_and_sweep_never_raise_into_their_callers(routing_state, monkeypatch, caplog):
    """C-12.10: removal failures never fail finalization. Found at hold h6:
    `tests/unit/test_daemon_settle.py` builds a daemon without `__init__`,
    and its finalization raised from the cleanup's per-holder lock."""
    service, _ = routing_state
    attempt, folder = terminal_leftover(routing_state)
    for name in ("_tmpdir_guard", "_tmpdir_sweeping"):
        monkeypatch.delattr(service, name)
    service._cleanup_attempt_tmpdir(attempt)
    service._cleanup_probe_tmpdir({"holder": "probe:dummy", "tmpdir": {"path": str(folder)}})
    service._sweep_attempt_tmpdirs()
    assert folder.is_dir()
    assert caplog.text.count("TMPDIR") >= 3
