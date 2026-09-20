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
    monkeypatch.setattr(module, "git_head", lambda path: None)
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


# --- C-14.2: the guard preflight leaves a diagnosable record -----------------

class GuardedFake(FakeAdapter):
    """The fake adapter with a Codex binary, so `_guard_override` runs the preflight."""
    codex_bin = "fixture-codex"


def _refused(kind):
    from subfleet.guard import preflight as guard
    return guard.PreflightResult(
        False, 7, "Guard preflight timed out: Codex app-server did not answer hooks/list before the "
        "60s deadline (probe pid 4242, 60.01s elapsed, /fixture/codex); guard trust is unverified, "
        "not mismatched", guard._TIMEOUT_FIX, version="codex-cli 0.153.3", kind=kind,
        timeout_s=60.0, elapsed_s=60.01, probe_pid=4242, executable="/fixture/codex",
        stderr_tail="fixture stderr\n", transcript=("> initialize", "< init reply"))


def _verified(cached=False):
    from subfleet.guard import preflight as guard
    return guard.PreflightResult(
        True, 0, "Codex never-rules guard trust verified", version="codex-cli 0.153.3",
        hooks_hash="sha256:fixture", override="hooks={fixture}", kind=guard.CACHED if cached else guard.VERIFIED,
        cached=cached, cache_key="k" * 64, timeout_s=60.0, elapsed_s=0.31, probe_pid=None if cached else 4243,
        executable="/fixture/codex")


@pytest.fixture
def guarded_launch(launch_state, monkeypatch):
    daemon, harness, calls = launch_state
    register("codex", GuardedFake)
    from subfleet.guard import preflight as guard
    seen = []

    def fake_preflight(binary, *, home, workdir, **kwargs):
        seen.append({"binary": binary, "home": home, "workdir": workdir})
        return fake_preflight.result

    fake_preflight.result = _verified()
    monkeypatch.setattr(guard, "preflight", fake_preflight)
    try:
        yield daemon, harness, calls, fake_preflight, seen
    finally:
        register("codex", FakeAdapter)


def test_c14_2_refused_preflight_is_recorded_in_the_attempt_directory_and_daemon_log(guarded_launch):
    """2026-09-20: a timeout refusal leaves guard-preflight.json beside exit.json and one log line."""
    daemon, harness, calls, fake_preflight, seen = guarded_launch
    fake_preflight.result = _refused("timeout")
    a = reserve(daemon, harness)
    daemon._launch(a)
    assert calls == [], "a refused launch starts no guardian"
    assert seen == [{"binary": "fixture-codex", "home": str(harness.root / "home"), "workdir": str(harness.workdir)}]
    adir = daemon.root / "jobs" / a["job_id"] / "a1"
    record = json.loads((adir / "guard-preflight.json").read_text())
    assert record["ok"] is False and record["kind"] == "timeout" and record["code"] == 7
    assert record["probe_pid"] == 4242 and record["elapsed_s"] == 60.01 and record["timeout_s"] == 60.0
    assert record["stderr_tail"] == "fixture stderr\n" and record["transcript"] == ["> initialize", "< init reply"]
    assert record["lane_id"] == "codex-1" and record["attempt_dir"] == str(adir)
    assert record["workdir"] == str(harness.workdir) and record["recorded_at"]
    assert record["executable"] == "/fixture/codex" and record["version"] == "codex-cli 0.153.3"
    exit_receipt = json.loads((adir / "exit.json").read_text())
    assert exit_receipt["rc"] == 7 and exit_receipt["spawn_error"].startswith("Guard preflight timed out")
    assert "unverified, not mismatched" in exit_receipt["spawn_error"]
    assert "Restore" not in exit_receipt["spawn_error"]
    log = (daemon.root / "daemon.log").read_text()
    line, = [line for line in log.splitlines() if line.startswith("guard preflight ")]
    for fragment in ("lane=codex-1", f"attempt={a['job_id']}/a1", "kind=timeout", "ok=False",
                     "cached=False", "elapsed=60.01s", "pid=4242", "deadline=60.0s",
                     "version='codex-cli 0.153.3'", "executable=/fixture/codex"):
        assert fragment in line, (fragment, line)
    assert "fixture stderr" not in log, "stderr belongs to the attempt directory, not the daemon log"


def test_c14_2_verified_preflight_is_recorded_and_the_launch_proceeds(guarded_launch):
    daemon, harness, calls, fake_preflight, seen = guarded_launch
    a = reserve(daemon, harness)
    daemon._launch(a)
    assert len(calls) == 1, "the guardian launched"
    adir = daemon.root / "jobs" / a["job_id"] / "a1"
    record = json.loads((adir / "guard-preflight.json").read_text())
    assert record["ok"] is True and record["kind"] == "verified" and record["cached"] is False
    assert record["probe_pid"] == 4243 and record["cache_key"] == "k" * 64
    assert (adir / "launch.json").is_file(), "the launch record follows the verified verdict"
    log = (daemon.root / "daemon.log").read_text()
    assert any("guard preflight " in line and "kind=verified ok=True cached=False" in line
               for line in log.splitlines())


def test_c23_5_cached_verdict_is_recorded_as_cached(guarded_launch):
    daemon, harness, calls, fake_preflight, seen = guarded_launch
    fake_preflight.result = _verified(cached=True)
    a = reserve(daemon, harness)
    daemon._launch(a)
    assert len(calls) == 1
    record = json.loads((daemon.root / "jobs" / a["job_id"] / "a1" / "guard-preflight.json").read_text())
    assert record["cached"] is True and record["kind"] == "cached" and record["probe_pid"] is None
    log = (daemon.root / "daemon.log").read_text()
    assert any("kind=cached ok=True cached=True" in line for line in log.splitlines())
