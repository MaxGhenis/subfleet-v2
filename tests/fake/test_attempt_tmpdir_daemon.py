"""D-TMP1: every daemon provider seam owns private attempt scratch."""

import json
import os
from pathlib import Path
import stat

import pytest

from subfleet import procs
from tests.fake.test_launch_env_daemon import daemon_spawn_environment
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401


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
    _, env = daemon_spawn_environment(routing_state, monkeypatch, "detached",
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
    if quarantined:
        assert service.store.get_attempt(attempt["attempt_id"])["state"] == "quarantined"
