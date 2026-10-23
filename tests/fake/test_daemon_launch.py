"""Launch construction tests; Popen is recorded and no provider is executed."""
import dataclasses
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import daemon as module
from subfleet import procs
from subfleet.adapters.registry import register
from subfleet.contracts import attempt_dir
from subfleet.daemon import Daemon
from subfleet.guardian import atomic_publish
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



def test_c25_7_a_guardian_that_cannot_start_is_named_by_its_errno_not_its_words(launch_state, monkeypatch):
    """C-4.2 with C-25.7: Popen's OS error names a path. The attempt's detail (which a
    turn's `state_reason` and a job's notice read) names its class and errno; its words
    are in daemon.log only, scrubbed whole."""
    daemon, harness, calls = launch_state
    a = reserve(daemon, harness)
    said = "password='SECRET7-left\n右秘密-TAIL8'"

    def refuse(command, **kwargs):
        raise PermissionError(13, "Permission denied", str(harness.root / said))
    monkeypatch.setattr(module.subprocess, "Popen", refuse)
    daemon._launch(a)
    attempt = daemon.store.get_attempt(a["attempt_id"])
    assert attempt["outcome_detail"] == "guardian-identity-unavailable: PermissionError: [Errno 13] Permission denied"
    recorded = json.dumps([daemon.store.query(f"SELECT * FROM {table}") for table in ("attempts", "events", "notices")])
    assert "SECRET7" not in recorded and "TAIL8" not in recorded
    daemon._log_handler.flush()
    log = (daemon.root / "daemon.log").read_text()
    assert f"attempt {a['attempt_id']} guardian identity unavailable: PermissionError: [Errno 13]" in log
    assert "SECRET7" not in log and "TAIL8" not in log


def test_c5_12_a_new_attempt_is_first_inspected_with_a_table_read_after_its_guardian_started(launch_state,
                                                                                               monkeypatch):
    """C-5.12 an attempt this daemon launches falls due one interval after its guardian started, so its first
    inspection is given a table read after that, which shows the guardian: nothing is asked about it singly and
    it reads no table of its own.

    Reviews of 2026-09-26: it fell due as it first asked, just after `start.json`, and was usually given the table
    another running attempt had read before its guardian existed, so it paid a fresh `liveness` and a `ps -axo` of
    its own, outside the one read per interval."""
    daemon, harness, calls = launch_state
    daemon.inspect_interval_s = 30                           # longer than this launch takes, even under load
    a = reserve(daemon, harness)
    aid, guardian = a["attempt_id"], 987654321
    before = time.monotonic()
    # Another running attempt's table, read just before this launch: it cannot show the new guardian.
    daemon._table = (procs.ProcessTable({1: (0, 1, "Ss", "fixture-start")}, "fixture-boot"),
                     before + daemon.inspect_interval_s - .001)
    daemon._launch(a)
    assert daemon.store.get_attempt(aid)["state"] == "starting"
    atomic_publish(attempt_dir(daemon.root, a["job_id"], a["seq"]) / "start.json", json.dumps(
        {"guardian_pid": guardian, "pgid": guardian, "boot_id": "fixture-boot", "proc_start": "fixture-start",
         "started_at": module.utcnow()}).encode())
    asked, tables = [], []
    monkeypatch.setattr(module.procs, "liveness", lambda *args: asked.append(args) or "alive")
    monkeypatch.setattr(module.procs, "snapshot", lambda: tables.append(1) or procs.ProcessTable(
        {guardian: (1, guardian, "Ss", "fixture-start")}, "fixture-boot"))
    daemon._process_attempt(aid)                             # `start.json`: running, and not yet due
    assert daemon.store.get_attempt(aid)["state"] == "running"
    assert asked == [] and tables == []
    due = daemon._inspect_next[aid]
    assert due > daemon._table[1]                            # no table read before the launch can serve it
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: due))
    daemon._process_attempt(aid)                             # due: it reads the table, which shows its guardian
    assert asked == [] and tables == [1]
    assert str(guardian) in json.loads(daemon.store.get_attempt(aid)["evidence_json"])["owned_identities"]

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
        seen.append({"binary": binary, "home": home, "workdir": workdir, **kwargs})
        return fake_preflight.result

    fake_preflight.result = _verified()
    monkeypatch.setattr(guard, "preflight", fake_preflight)
    try:
        yield daemon, harness, calls, fake_preflight, seen
    finally:
        register("codex", FakeAdapter)


@pytest.mark.parametrize("initially_valid", [True, False])
def test_ca_selection_is_shared_by_adapter_preflight_and_guardian(guarded_launch, monkeypatch,
                                                               initially_valid):
    """C-14.2: changing the bundle mid-launch cannot change selected CA values."""
    from subfleet.provider_env import CA_SOURCES, ProviderEnvironment
    from subfleet.guard import preflight as guard
    daemon, harness, calls, fake_preflight, seen = guarded_launch
    for key in CA_SOURCES:
        monkeypatch.delenv(key, raising=False)
    certificate = (Path(__file__).parents[1] / "fixtures/codex-ca.pem").read_bytes()
    bundle = daemon.root / "ca.pem"
    bundle.write_bytes(certificate if initially_valid else b"")
    daemon._provider_environment = ProviderEnvironment({"providers": {"codex": {"ca_bundle": str(bundle)}}})
    a = reserve(daemon, harness)
    selections = []
    original_select = daemon._provider_environment.additions
    def select(*args, **kwargs):
        result = original_select(*args, **kwargs)
        selections.append(result)
        if len(selections) == 1:
            bundle.write_bytes(b"" if initially_valid else certificate)
        return result
    monkeypatch.setattr(daemon._provider_environment, "additions", select)
    def preflight(*args, **kwargs):
        result = fake_preflight(*args, **kwargs)
        bundle.write_bytes(certificate if initially_valid else b"")
        return result
    monkeypatch.setattr(guard, "preflight", preflight)
    adapter_envs = []
    original_build = FakeAdapter.build_launch
    def build(self, job, aid, adir, lane, credential_env, *args, **kwargs):
        adapter_envs.append({**module.os.environ, **credential_env})
        return original_build(self, job, aid, adir, lane, credential_env, *args, **kwargs)
    monkeypatch.setattr(FakeAdapter, "build_launch", build)
    daemon._launch(a)
    def ca_values(env):
        return {key: env[key] for key in CA_SOURCES if key in env}
    expected = {"SSL_CERT_FILE": str(bundle)} if initially_valid else {}
    assert len(calls) == len(seen) == len(adapter_envs) == 1
    assert ca_values(adapter_envs[0]) == ca_values(seen[0]["env"]) == ca_values(calls[0][1]["env"]) == expected
    assert len(selections) == 1
    # Recovery after a restart deliberately selects against the current file.
    bundle.write_bytes(b"" if initially_valid else certificate)
    recovery = daemon._relaunch_env(a["lane_id"])
    assert ca_values(recovery) == ({} if initially_valid else {"SSL_CERT_FILE": str(bundle)})
    assert len(selections) == 2


def test_c14_2_refused_preflight_is_recorded_in_the_attempt_directory_and_daemon_log(guarded_launch):
    """2026-09-20: a timeout refusal leaves guard-preflight.json beside exit.json and one log line."""
    daemon, harness, calls, fake_preflight, seen = guarded_launch
    fake_preflight.result = _refused("timeout")
    a = reserve(daemon, harness)
    daemon._launch(a)
    assert calls == [], "a refused launch starts no guardian"
    assert [{key: value for key, value in call.items() if key not in {"env", "env_pass"}} for call in seen] == [
        {"binary": "fixture-codex", "home": str(harness.root / "home"),
         "workdir": str(harness.workdir), "state_root": daemon.root}]
    # C-12.10: the attempt's own TMPDIR reaches guard preflight too (review r1 P2-1).
    assert set(seen[0]["env_pass"]) == {"CODEX_HOME", "TMPDIR"} | {
        name for name in ("SSL_CERT_FILE", "SSL_CERT_DIR") if name in seen[0]["env"]}
    assert seen[0]["env"]["CODEX_HOME"] == str(harness.root / "home")
    tmpdir = json.loads(daemon.store.get_attempt(a["attempt_id"])["evidence_json"])["tmpdir"]["path"]
    assert seen[0]["env"]["TMPDIR"] == seen[0]["env_pass"]["TMPDIR"] == tmpdir + "/"
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
