"""D-TMP1-P3 at daemon level: review r2's daemon inputs for P3-1, P3-2 and P3-3,
and the daemon's part in finishing what a crash left (C-12.10)."""

import errno
import json
import logging
import os
from pathlib import Path

import pytest

from subfleet import attempt_tmp, daemon as daemon_module, procs, retention
from tests.fake.test_attempt_tmpdir_daemon import terminal_leftover
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401

DAY = 86_400


class Crash(BaseException):
    """The daemon dies here. Only `finally` blocks above this point run, and
    `_remove`'s does not put back a rename it has not yet recorded."""


def crash_after_tombstone_rename(monkeypatch):
    real = os.rename

    def rename(src, dst, *args, **kwargs):
        real(src, dst, *args, **kwargs)
        if os.path.basename(os.fspath(dst)).startswith(attempt_tmp.TOMBSTONE):
            raise Crash()

    monkeypatch.setattr(os, "rename", rename)


def leftovers(service):
    return sorted(entry.name for entry in service._attempt_temp.root.iterdir()
                  if entry.name.startswith(attempt_tmp.PREFIX))


def tmpdir_of(service, attempt):
    return json.loads(service.store.get_attempt(attempt["attempt_id"])["evidence_json"]).get("tmpdir")


def events(service, kind):
    return [json.loads(row["data_json"]) for row in service.store.query(
        "SELECT data_json FROM events WHERE kind=? AND data_json<>'{}' ORDER BY event_id", (kind,))]


def restart(service, monkeypatch, census):
    """A new daemon on the same root, whose census says `census()`."""
    root = service.root
    service.close()
    monkeypatch.setattr(daemon_module.Daemon, "_contain", lambda self, a: census())
    return daemon_module.Daemon(root)


@pytest.mark.parametrize("marker", [False, True])
def test_p3_1_a_tombstone_a_crash_left_is_finished_after_restart(routing_state, monkeypatch, marker):
    """Review r2 P3-1 (r2h5): a terminal leftover with 64 KiB, its marker removed and
    the folder renamed to a tombstone. Four days of `_sweep_attempt_tmpdirs` left
    `outcome: absent`, the 64 KiB tombstone in place, and no sighting.

    `marker`: with it kept, a first sweep whose census cannot be verified keeps
    the tombstone, and never sights it as an unknown folder: the store names it."""
    service, _ = routing_state
    attempt, folder = terminal_leftover(routing_state)
    (folder / "payload").write_bytes(b"x" * 65536)
    if not marker:
        (folder / attempt_tmp.MARKER).unlink()
    monkeypatch.setattr(service, "_contain", lambda a: procs.Containment())
    with monkeypatch.context() as patch:
        crash_after_tombstone_rename(patch)
        with pytest.raises(Crash):
            service._sweep_attempt_tmpdirs()
    [tomb] = leftovers(service)
    assert tomb.startswith(attempt_tmp.TOMBSTONE) and not folder.exists()
    assert "removed_at" not in tmpdir_of(service, attempt), "it died before recording anything"
    verified = [not marker]
    restarted = restart(service, monkeypatch, lambda: procs.Containment(unverifiable=not verified[0]))
    clock = [1_000_000.0]
    monkeypatch.setattr(daemon_module.attempt_tmp.time, "time", lambda: clock[0])
    try:
        counted = dict(retention.job_tmpdirs(restarted.store, restarted.root, job_id=attempt["job_id"]))
        if marker:
            restarted._sweep_attempt_tmpdirs()
            assert leftovers(restarted) == [tomb] and "removed_at" not in tmpdir_of(restarted, attempt)
            assert events(restarted, "tmpdir.unknown_seen") == [], "the store names it: never an unknown folder"
            verified[0] = True
        for _ in range(4):
            restarted._sweep_attempt_tmpdirs()
            clock[0] += DAY
        record = tmpdir_of(restarted, attempt)
        assert record.get("outcome") == "removed" and record["removed_at"], record
        assert record["bytes_at_cleanup"] >= 65536
        assert leftovers(restarted) == [] and events(restarted, "tmpdir.unknown_seen") == []
        found = counted.get(attempt["job_id"])
        assert found and Path(found["path"]).name == tomb, "while there, its bytes counted at the tombstone"
    finally:
        restarted.close()


def test_p3_1_an_unknown_folders_tombstone_is_finished_after_restart(routing_state, monkeypatch):
    """The sweep's own removal of an unknown folder dies after its rename. The
    sighting recorded the tombstone's name, so the next sweep finishes it."""
    service, _ = routing_state
    orphan = Path(service._attempt_temp.create("crashed-before-its-row")["path"])
    (orphan / "payload").write_bytes(b"x" * 4096)
    clock = [1_000_000.0]
    with monkeypatch.context() as patch:
        patch.setattr(daemon_module.attempt_tmp.time, "time", lambda: clock[0])
        service._sweep_attempt_tmpdirs()
        [seen] = events(service, "tmpdir.unknown_seen")
        assert seen["path"] == str(orphan) and seen["tombstone"].startswith(attempt_tmp.TOMBSTONE)
        clock[0] += DAY + 1
        crash_after_tombstone_rename(patch)
        with pytest.raises(Crash):
            service._sweep_attempt_tmpdirs()
    assert leftovers(service) == [seen["tombstone"]]
    restarted = restart(service, monkeypatch, procs.Containment)
    clock[0] += 60
    monkeypatch.setattr(daemon_module.attempt_tmp.time, "time", lambda: clock[0])
    try:
        restarted._sweep_attempt_tmpdirs()
        assert leftovers(restarted) == []
        [gone] = events(restarted, "tmpdir.unknown_removed")
        assert gone["path"] == str(orphan) and gone["bytes_at_cleanup"] >= 4096
        assert len(events(restarted, "tmpdir.unknown_seen")) == 1, "no new sighting of the tombstone"
    finally:
        restarted.close()


def test_p3_2_an_unreadable_top_folder_is_removed_by_the_sweep(routing_state, monkeypatch):
    """Review r2 P3-2: `chmod 000` on an allocation; its removal failed on every sweep."""
    service, _ = routing_state
    attempt, folder = terminal_leftover(routing_state)
    os.chmod(folder, 0o000)
    monkeypatch.setattr(service, "_contain", lambda a: procs.Containment())
    try:
        service._sweep_attempt_tmpdirs()
        record = tmpdir_of(service, attempt)
        assert record.get("outcome") == "removed" and record["cleared_at"] and record["removed_at"], record
        assert leftovers(service) == []
    finally:
        if folder.exists():
            os.chmod(folder, 0o700)


def test_p3_2_retiring_a_job_whose_scratch_will_not_go_names_the_folder(routing_state, monkeypatch, caplog):
    """Review r2 P3-2 (r2h5): removal kept failing (EPERM, as a `chflags uchg` file
    gives), the marker was gone, the record got `cleared_at` and no `removed_at`,
    and retention retired the job, record and all. The folder outlived it with no
    trace. The retirement now logs one line naming it."""
    service, _ = routing_state
    attempt, folder = terminal_leftover(routing_state)
    (folder / attempt_tmp.MARKER).unlink()
    monkeypatch.setattr(service, "_contain", lambda a: procs.Containment())
    monkeypatch.setattr(daemon_module.attempt_tmp, "_empty",
                        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError(errno.EPERM, "dummy uchg")))
    service.store.conn.execute("DELETE FROM leases WHERE holder IN (?,?)", (attempt["job_id"], attempt["attempt_id"]))
    service.store.conn.execute("UPDATE jobs SET finished_at='2026-10-22T00:00:00.000Z' WHERE job_id=?", (attempt["job_id"],))
    service._sweep_attempt_tmpdirs()
    record = tmpdir_of(service, attempt)
    assert record["cleared_at"] and "removed_at" not in record, "cleared: it pins nothing"
    monkeypatch.delenv(daemon_module.RETENTION_DORMANT_ENV, raising=False)
    service.policy["retention"] = {**(service.policy.get("retention") or {}), "jobs": 0, "bytes": 0}
    real = daemon_module.maintenance
    monkeypatch.setattr(daemon_module, "maintenance",
                        lambda *args, **kwargs: real(*args, **{**kwargs, "holders": lambda watches, **_: {}}))
    caplog.set_level(logging.WARNING)
    for _ in range(3):
        if service.store.get_job(attempt["job_id"]) is None:
            break
        service._retention()
    assert service.store.get_job(attempt["job_id"]) is None, "retired"
    assert (f"retention: retired {attempt['job_id']} with its TMPDIR cleared but not removed: {folder}"
            in caplog.text)
    assert folder.is_dir(), "what no mode change fixes is still there, and the log says where"


def test_p3_3_a_launch_whose_marker_publish_fails_leaves_no_folder(routing_state, monkeypatch):
    """Review r2 P3-3 (r2h5): `_launch` with the marker publish failing. The attempt
    went to `finalizing` with its launch failure and no `tmpdir` in its evidence,
    and the unmarked folder was still there after four days of sweeps."""
    service, harness = routing_state
    job_id = service.dispatch("submit", harness.submit_args())["job_id"]
    service._admit()
    attempt = service.store.list_attempts(job_id)[0]
    monkeypatch.setattr(service, "_contain", lambda a: procs.Containment())
    real = os.rename

    def full(src, dst, *args, **kwargs):
        if os.path.basename(os.fspath(dst)) == attempt_tmp.MARKER:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return real(src, dst, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "rename", full)
        service._launch(attempt)
    current = service.store.get_attempt(attempt["attempt_id"])
    assert current["state"] == "finalizing"
    assert "tmpdir" not in json.loads(current["evidence_json"] or "{}")
    receipt = json.loads((service.root / "jobs" / job_id / "a1" / "exit.json").read_text())
    assert receipt["spawn_error"].startswith("attempt TMPDIR could not be allocated: ")
    assert leftovers(service) == [], "the folder made for it is gone"
    clock = [1_000_000.0]
    monkeypatch.setattr(daemon_module.attempt_tmp.time, "time", lambda: clock[0])
    for _ in range(4):
        service._sweep_attempt_tmpdirs()
        clock[0] += DAY
    assert leftovers(service) == []


def test_t1_a_folder_a_crash_left_unmarked_is_the_stores_by_its_journal(routing_state, monkeypatch):
    """The daemon died between the allocation's mkdir and its marker: no marker and
    no record. The store's journal names the folder, so the sweep ages it from
    its sighting and removes it, and drops the journal entry."""
    service, _ = routing_state

    def die(*args, **kwargs):
        raise Crash()

    with monkeypatch.context() as patch:
        patch.setattr(os, "chmod", die)          # the step after the mkdir
        patch.setattr(os, "rmdir", die)          # and no cleanup runs: the process is gone
        with pytest.raises(Crash):
            service._attempt_temp.create("job-x/a1")
    [left] = leftovers(service)
    folder = service._attempt_temp.root / left
    assert not (folder / attempt_tmp.MARKER).exists()
    assert os.listdir(service.root / attempt_tmp.INTENTS) == [left]
    restarted = restart(service, monkeypatch, procs.Containment)
    clock = [1_000_000.0]
    monkeypatch.setattr(daemon_module.attempt_tmp.time, "time", lambda: clock[0])
    try:
        restarted._sweep_attempt_tmpdirs()
        assert [row["path"] for row in events(restarted, "tmpdir.unknown_seen")] == [str(folder)]
        assert folder.is_dir(), "unknown: it waits 24 hours from the store's sighting"
        clock[0] += DAY + 1
        restarted._sweep_attempt_tmpdirs()
        assert leftovers(restarted) == [] and os.listdir(restarted.root / attempt_tmp.INTENTS) == []
        assert [row["path"] for row in events(restarted, "tmpdir.unknown_removed")] == [str(folder)]
    finally:
        restarted.close()
