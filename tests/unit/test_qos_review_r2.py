"""#277 review r2: legacy status rows and the QoS receipt/abandon ordering."""

import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from subfleet import guardian
from subfleet.offline import Offline
from test_store_qos_migration import old_store


def test_legacy_offline_status_defaults_dispatch_and_turn_requests(tmp_path):
    path = old_store(tmp_path / "state.sqlite3")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE jobs SET state='running'")
    status = Offline(tmp_path).status()
    assert status["schema_version"] == 9
    assert [(row["job_id"], row["provider_qos"]) for row in status["running"]] == [
        ("old-dispatch", "utility")]
    assert [(row["job_id"], row["provider_qos"]) for row in status["turns"]] == [
        ("old-turn", "utility")]
    with sqlite3.connect(path) as conn:
        assert "provider_qos" not in {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}


@pytest.mark.parametrize("requested", ["utility", "inherit"])
@pytest.mark.parametrize("abandon_on_publish", [False, True], ids=["spawn", "abandon"])
def test_qos_start_publication_precedes_abandon_check_and_spawn(
        tmp_path, monkeypatch, requested, abandon_on_publish):
    monkeypatch.setattr(guardian.os, "setsid", lambda: None)
    monkeypatch.setattr(guardian.signal, "signal", lambda *args: None)
    monkeypatch.setattr(guardian, "boot_id", lambda: "fixture-boot")
    monkeypatch.setattr(guardian, "proc_start", lambda pid: "fixture-start")
    monkeypatch.setattr(guardian, "linger", lambda: None)
    monkeypatch.delenv(guardian.PROVIDER_QOS_ENV, raising=False)
    real_access = os.access
    monkeypatch.setattr(guardian.os, "access", lambda path, mode:
                        True if path == guardian.TASKPOLICY else real_access(path, mode))
    events, commands = [], []
    real_receipt, real_abandoned = guardian._receipt, guardian.abandoned

    def receipt(path, value):
        real_receipt(path, value)
        if path.name == "start.json":
            events.append("start")
            if abandon_on_publish:
                guardian.atomic_publish(tmp_path / guardian.ABANDON_NAME,
                                        b'{"reason":"review publication race"}')

    def abandoned(directory):
        events.append("spawn-check" if (directory / "start.json").exists() else "gate-check")
        return real_abandoned(directory)

    def spawn(command, **options):
        events.append("spawn")
        commands.append(command)
        assert json.loads((tmp_path / "start.json").read_text())["provider_qos"] == requested
        return SimpleNamespace(pid=987654321, wait=lambda: 0)

    monkeypatch.setattr(guardian, "_receipt", receipt)
    monkeypatch.setattr(guardian, "abandoned", abandoned)
    monkeypatch.setattr(guardian.subprocess, "Popen", spawn)
    previous = os.umask(0o077)
    try:
        rc = guardian.run_guardian(["fixture-provider"], attempt_dir=tmp_path, cwd=str(tmp_path),
                                   stdin_path=None, stdout_path=str(tmp_path / "stdout"),
                                   stderr_path=str(tmp_path / "stderr"), qos_request=requested)
    finally:
        os.umask(previous)
    assert events == ["gate-check", "start", "spawn-check", *([] if abandon_on_publish else ["spawn"])]
    assert json.loads((tmp_path / "start.json").read_text())["provider_qos"] == requested
    if abandon_on_publish:
        assert rc == 127 and commands == []
        assert json.loads((tmp_path / "exit.json").read_text())["abandoned"]["stage"] == "spawn"
        assert not (tmp_path / "stdout").exists()
    else:
        assert rc == 0
        assert commands == [[guardian.TASKPOLICY, "-c", "utility", "fixture-provider"]
                            if requested == "utility" else ["fixture-provider"]]
