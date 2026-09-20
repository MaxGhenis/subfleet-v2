"""Launch construction tests; Popen is recorded and no provider is executed."""
import dataclasses
import json
from pathlib import Path

import pytest

from subfleet import daemon as module
from subfleet.adapters.registry import register
from subfleet.daemon import Daemon
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter


@pytest.fixture
def launch_state(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    harness = Harness(root)
    monkeypatch.setattr(module.procs, "boot_id", lambda: "fixture-boot")
    monkeypatch.setattr(module.procs, "proc_start", lambda pid: "fixture-start")
    # These tests exercise the launch path itself; the C-11.4 pre-launch probe
    # for unmeasured lanes has its own tests (test_probe_recovery) and would
    # otherwise run `ps` through the fake Popen below.
    monkeypatch.setattr(module.scheduler, "probe_required", lambda decision, job: False)
    register("codex", FakeAdapter)
    daemon = Daemon(root)
    calls = []
    class Child:
        pid = 987654321
        def poll(self):
            return None
    def record(command, **kwargs):
        calls.append((command, kwargs))
        return Child()
    monkeypatch.setattr(module.subprocess, "Popen", record)
    # Workdir git inspection is deliberately separate from guardian Popen.
    monkeypatch.setattr(module, "git_head", lambda path, **_: None)
    try:
        yield daemon, harness, calls
    finally:
        daemon.close()


def reserve(daemon, harness):
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon._admit()
    return daemon.store.list_attempts(job_id)[0]


def test_c5_1_launch_uses_installed_guardian_and_keeps_credentials_in_environment(launch_state, monkeypatch):
    """C-5.1, C-10.5 guardian imports from its package root; credentials are never serialized."""
    daemon, harness, calls = launch_state
    a = reserve(daemon, harness)
    original = FakeAdapter.build_launch
    secret = "fixture-token-environment-only"
    def build(self, *args, **kwargs):
        launch = original(self, *args, **kwargs)
        return dataclasses.replace(launch, env_add={**launch.env_add, "CLAUDE_CODE_OAUTH_TOKEN": secret})
    monkeypatch.setattr(FakeAdapter, "build_launch", build)
    for key in ("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(key, "fixture-api-key")
    daemon._launch(a)
    command, options = calls[0]
    assert options["cwd"] == str(Path(module.__file__).resolve().parent.parent)
    assert command[command.index("--cwd") + 1] == str(harness.workdir)
    assert options["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == secret
    assert options["env"]["SUBFLEET_ATTEMPT"] == a["attempt_id"]
    assert not set(("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")) & options["env"].keys()
    assert secret not in json.dumps(command)
    for path in (daemon.root / "jobs" / a["job_id"]).rglob("*.json"):
        assert secret not in path.read_text()
    for table in ("jobs", "attempts", "events"):
        assert secret not in json.dumps(daemon.store.query(f"SELECT * FROM {table}"))
    row = daemon.store.get_attempt(a["attempt_id"])
    assert row["state"] == "starting" and row["guardian_pid"] == 987654321


def test_c4_2_absent_guardian_identity_never_releases_launch_gate(launch_state, monkeypatch):
    """C-4.2, C-5.3 an absent launch identity closes the gate and releases an unlaunched reservation."""
    daemon, harness, calls = launch_state
    a = reserve(daemon, harness)
    monkeypatch.setattr(module.procs, "proc_start", lambda pid: None)
    writes = []
    write = module.os.write
    def record(fd, data):
        writes.append(data)
        return write(fd, data)
    monkeypatch.setattr(module.os, "write", record)
    daemon._launch(a)
    assert calls
    assert b"1" not in writes
    attempt = daemon.store.get_attempt(a["attempt_id"])
    assert attempt["state"] == "failed"
    assert attempt["outcome_detail"].startswith("guardian-identity-unavailable")
    assert daemon.store.get_job(a["job_id"])["state"] == "queued"
    assert daemon.store.query("SELECT * FROM leases") == []
