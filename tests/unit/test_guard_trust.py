"""Guard file integrity, v1 override parity, and fake-only preflight checks."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib

import pytest

guard = importlib.import_module("subfleet.guard.preflight")

from tests.fake.guard import install_guard


@pytest.fixture(autouse=True)
def isolated_overlay(tmp_path, monkeypatch):
    """No guard test depends on the operator overlay or installed state."""
    root = tmp_path / "state"
    monkeypatch.setenv("SUBFLEET_HOME", str(root))
    monkeypatch.delenv("SUBFLEET_GUARD_TRUST", raising=False)
    return install_guard(root)


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    """C-14.2 exercise the preflight protocol without invoking installed Codex."""
    script = tmp_path / "fake-codex"
    script.write_text("#!" + sys.executable + "\n" + r'''
import json
import os
from pathlib import Path
import re
import sys
import time

if sys.argv[1:] == ["--version"]:
    if os.environ.get("GUARD_TEST_VERSION_MODE") == "hang":
        Path(os.environ["GUARD_TEST_REPORT"]).write_text(json.dumps({"version_pid": os.getpid()}))
        time.sleep(30)
        raise SystemExit(1)
    if os.environ.get("GUARD_TEST_VERSION_MODE") == "fail":
        print("fake launcher: node not found", file=sys.stderr)
        raise SystemExit(127)
    print(os.environ.get("GUARD_TEST_VERSION", "codex-cli 0.153.3"))
    raise SystemExit(0)
assert sys.argv[1] == "app-server", sys.argv
home = Path(os.environ["CODEX_HOME"])
report = {"argv": sys.argv[1:], "home": str(home), "cwd": os.getcwd(),
          "files": {p.name: p.read_text() for p in home.iterdir()},
          "api_keys": [key for key in ("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
                       if key in os.environ], "pid": os.getpid()}
Path(os.environ["GUARD_TEST_REPORT"]).write_text(json.dumps(report))
mode = os.environ.get("GUARD_TEST_MODE", "trusted")
print("fake app-server starting in mode " + mode, file=sys.stderr, flush=True)
if mode == "hang":
    time.sleep(30)
    raise SystemExit(1)
if mode == "launcher-exits":
    # The npm launcher shape: spawn the "native" child in the same session and
    # exit at once; only a process-group kill can reap the child.
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    report["child_pid"] = child.pid
    Path(os.environ["GUARD_TEST_REPORT"]).write_text(json.dumps(report))
    raise SystemExit(0)
if mode == "dies":
    print("fake app-server: dyld: library not loaded", file=sys.stderr, flush=True)
    raise SystemExit(3)
if mode == "flood":
    junk = "x" * 65536
    for _ in range(20):
        print(junk, flush=True)
    time.sleep(30)
    raise SystemExit(1)
for line in sys.stdin:
    request = json.loads(line)
    if request.get("id") == 1:
        print(json.dumps({"id": 1, "result": {"userAgent": "fake"}}), flush=True)
        if mode == "chatty":
            for index in range(100):
                print(json.dumps({"method": "notice", "params": {"n": index, "pad": "y" * 3000}}), flush=True)
    if request.get("id") != 2:
        continue
    report["request"] = request
    Path(os.environ["GUARD_TEST_REPORT"]).write_text(json.dumps(report))
    expected_hash = re.search(r'trusted_hash="([^"]+)"', sys.argv[-1]).group(1)
    entry = {"key": "/<session-flags>/config.toml:pre_tool_use:0:0", "enabled": True,
             "trustStatus": "trusted", "currentHash": expected_hash}
    if mode == "hash-mismatch":
        entry["currentHash"] = "sha256:" + "0" * 64
    if mode == "untrusted":
        entry["trustStatus"] = "untrusted"
    if mode == "disabled":
        entry["enabled"] = False
    hooks = [] if mode == "missing" else [entry]
    if mode == "duplicate":
        hooks.append(entry.copy())
    data = [{"cwd": os.getcwd(), "hooks": hooks,
             "warnings": ["fixture warning"] if mode == "warning" else [],
             "errors": ["fixture error"] if mode == "error" else []}]
    response = {"id": 2, "result": {"data": data}}
    if mode == "wrong-cwd":
        data[0]["cwd"] = str(Path(os.getcwd()) / "another-workdir")
    if mode == "rpc-error-with-result":
        response["error"] = {"message": "incomplete result"}
    if mode == "rpc-error":
        response = {"id": 2, "error": {"message": "unknown method"}}
    print(json.dumps(response), flush=True)
''')
    script.chmod(0o755)
    report = tmp_path / "report.json"
    monkeypatch.setenv("GUARD_TEST_REPORT", str(report))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("GUARD_TEST_MODE", raising=False)
    monkeypatch.delenv("GUARD_TEST_VERSION_MODE", raising=False)
    monkeypatch.delenv(guard.TIMEOUT_ENV, raising=False)
    # C-2.1: scratch homes and verified markers stay under this test's own root,
    # never the real state root of the machine running the suite.
    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(guard.CACHE_ENV, str(tmp_path / "guard-cache"))
    monkeypatch.setattr(guard, "_jq_available", lambda: True)
    return script, report


def test_fixture_hook_matches_pinned_sha256():
    """C-14.1 the fake-only overlay pins the exact fixture bytes."""
    trust = json.loads(guard.guard_paths()[1].read_text())
    assert hashlib.sha256(guard.guard_paths()[0].read_bytes()).hexdigest() == trust["hook_sha256"]
    assert os.access(guard.guard_paths()[0], os.X_OK)


def test_hook_has_valid_bash_syntax():
    """C-14.1 the byte-preserved Bash hook remains syntactically executable."""
    result = subprocess.run(["/bin/bash", "-n", str(guard.guard_paths()[0])], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_hash_matches_v1_live_verified_pin():
    """C-14.2 reproduce v1's independent 0.144.0 live-verified identity hash."""
    assert guard.hooks_trust_hash(
        "/Users/maxghenis/chief-of-staff/carpool/bin/carpool-guard-hook", timeout=30
    ) == "sha256:40f500b8eb48e50cad1c5886605713f0e91fc159163e9cb874128ec93f6fffb0"


def test_override_matches_v1_pinned_string():
    """C-14.2, C-12.3 preserve v1's exact complete hooks= override argument."""
    trust = json.loads(guard.guard_paths()[1].read_text())
    assert guard.override_string(trust["reference_hook_path"]) == trust["override"]
    assert guard.hooks_trust_hash(trust["reference_hook_path"]) == trust["hooks_trust_hash"]
    parsed = tomllib.loads(trust["override"])
    assert parsed["hooks"]["PreToolUse"] == [{"matcher": "Bash|apply_patch", "hooks": [{
        "type": "command", "command": trust["reference_hook_path"], "timeout": 60,
        "statusMessage": "never-rules guard"}]}]
    assert parsed["hooks"]["state"][guard.HOOK_KEY] == {
        "trusted_hash": trust["hooks_trust_hash"], "enabled": True}


@pytest.mark.parametrize(("path", "command"), [
    ("/plain/path", "/plain/path"),
    ("/a space/hook", "'/a space/hook'"),
    ("/it's/hook", "'/it'\\''s/hook'"),
    ('/we"ird\\hook', "'/we\"ird\\hook'"),
    ("/héllo/hook", "'/héllo/hook'"),
])
def test_override_quotes_shell_and_toml_like_v1(path, command):
    """C-14.2 shell quoting and TOML escaping preserve v1's hook identity."""
    parsed = tomllib.loads(guard.override_string(path))
    assert parsed["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == command
    assert parsed["hooks"]["state"][guard.HOOK_KEY]["trusted_hash"] == guard.hooks_trust_hash(path)


@pytest.mark.parametrize("path", ["relative/hook", "/tmp/line\nbreak"])
def test_override_refuses_unrepresentable_paths(path):
    """C-14.2 reject paths that cannot safely identify the armed hook."""
    with pytest.raises(ValueError):
        guard.override_string(path)


def test_preflight_preserves_home_and_strips_api_keys(fake_codex, tmp_path, monkeypatch):
    """C-14.2, C-10.5 probe a scratch home with config only and no API keys."""
    fake, report_path = fake_codex
    home = tmp_path / "lane"
    home.mkdir()
    original = {"config.toml": '[features]\nhooks=true\n', "hooks.json": '{}\n',
                "auth.json": '{"access_token":"never-copy-this"}\n'}
    for name, content in original.items():
        (home / name).write_text(content)
    for name in ("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "must-not-reach-probe")
    result = guard.preflight(fake, home=home, workdir=tmp_path)
    assert result.ok and result.code == 0, result
    assert result.override == guard.override_string(guard.guard_paths()[0].resolve())
    report = json.loads(report_path.read_text())
    assert report["files"] == {name: original[name] for name in ("config.toml", "hooks.json")}
    assert report["api_keys"] == []
    assert report["cwd"] == str(tmp_path.resolve())
    assert report["request"]["params"]["cwds"] == [str(tmp_path.resolve())]
    assert "features.plugins=false" in report["argv"]
    assert result.override in report["argv"]
    assert not Path(report["home"]).exists()
    assert {p.name: p.read_text() for p in home.iterdir()} == original


def test_preflight_refuses_changed_hook_before_codex(fake_codex, tmp_path):
    """C-14.1, C-14.2 a mismatched hook SHA-256 refuses with exit 7 and fix."""
    fake, report = fake_codex
    hook = tmp_path / "hook.sh"
    hook.write_bytes(guard.guard_paths()[0].read_bytes() + b"\n# altered\n")
    hook.chmod(0o755)
    result = guard.preflight(fake, hook_path=hook)
    assert not result.ok and result.code == 7 and result.fix
    assert "SHA-256" in result.message
    assert result.override is None
    assert not report.exists()


@pytest.mark.parametrize("field", ["hooks_trust_hash", "override"])
def test_preflight_refuses_changed_trust_pin(fake_codex, tmp_path, field):
    """C-14.2 drift in the pinned hooks hash or override refuses before launch."""
    fake, report = fake_codex
    trust = json.loads(guard.guard_paths()[1].read_text())
    trust[field] += "changed"
    pin = tmp_path / "TRUST"
    pin.write_text(json.dumps(trust))
    result = guard.preflight(fake, trust_path=pin)
    assert not result.ok and result.code == 7 and result.fix
    assert "TRUST" in result.message
    assert not report.exists()


def test_preflight_uses_environment_trust_path(fake_codex, tmp_path, monkeypatch):
    """C-14.2: daemon and doctor can check a selected TRUST file without altering installation."""
    fake, report = fake_codex
    trust = json.loads(guard.guard_paths()[1].read_text())
    trust["hook_sha256"] = "0" * 64
    pin = tmp_path / "TRUST"
    pin.write_text(json.dumps(trust))
    monkeypatch.setenv("SUBFLEET_GUARD_TRUST", str(pin))
    result = guard.preflight(fake)
    assert not result.ok and result.code == 7
    assert "SHA-256" in result.message
    assert "subfleet doctor" in result.fix
    assert not report.exists()
    assert guard.preflight(fake, trust_path=Path(os.environ["SUBFLEET_HOME"]) / "guard/TRUST").ok


def test_preflight_refuses_new_cli_version(fake_codex, monkeypatch):
    """C-14.2 Codex upgrades require review before trusting the guard scheme."""
    fake, report = fake_codex
    monkeypatch.setenv("GUARD_TEST_VERSION", "codex-cli 0.154.0")
    result = guard.preflight(fake)
    assert not result.ok and result.code == 7 and result.fix
    assert "version" in result.message
    assert not report.exists()


@pytest.mark.parametrize("mode", ["hash-mismatch", "untrusted", "disabled", "missing",
                                  "duplicate", "error", "rpc-error", "dies",
                                  "wrong-cwd", "rpc-error-with-result"])
def test_preflight_refuses_unverified_runtime_trust(fake_codex, monkeypatch, mode):
    """C-14.2 runtime hooks/list must confirm exactly one enabled trusted hash."""
    fake, _ = fake_codex
    monkeypatch.setenv("GUARD_TEST_MODE", mode)
    result = guard.preflight(fake)
    assert not result.ok and result.code == 7 and result.fix
    assert result.override is None


@pytest.mark.parametrize("timeout_s", [0, -1, float("nan"), float("inf")])
def test_preflight_refuses_unbounded_timeout(fake_codex, timeout_s):
    """C-14.2, C-20.2 refuse invalid deadlines before spawning a trust probe."""
    fake, report = fake_codex
    result = guard.preflight(fake, timeout_s=timeout_s)
    assert not result.ok and result.code == 7 and result.fix
    assert "finite and positive" in result.message
    assert not report.exists()


def test_preflight_bounds_and_reaps_hung_probe(fake_codex, monkeypatch):
    """C-14.2, C-20.2 a wedged trust probe is bounded and reaped on refusal."""
    fake, report_path = fake_codex
    monkeypatch.setenv("GUARD_TEST_MODE", "hang")
    started = time.monotonic()
    result = guard.preflight(fake, timeout_s=0.5)
    assert time.monotonic() - started < 3
    assert not result.ok and result.code == 7 and "deadline" in result.message
    assert result.override is None
    assert "daemon scheduling" in result.fix and "trust remains unverified" in result.fix
    assert "Restore" not in result.fix
    report = json.loads(report_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(report["pid"], 0)
    assert not Path(report["home"]).exists()


def test_version_timeout_does_not_claim_version_drift(fake_codex, monkeypatch):
    fake, report_path = fake_codex
    monkeypatch.setenv("GUARD_TEST_VERSION_MODE", "hang")
    result = guard.preflight(fake, timeout_s=0.5)
    assert not result.ok and result.code == 7 and result.override is None
    assert result.version is None and "daemon scheduling" in result.fix
    assert "Restore" not in result.fix
    report = json.loads(report_path.read_text())
    assert "argv" not in report, "a version timeout must not start app-server"
    with pytest.raises(ProcessLookupError):
        os.kill(report["version_pid"], 0)


def test_preflight_reports_warnings(fake_codex, monkeypatch):
    """C-14.2 preserve hooks/list warnings beside a verified trust decision."""
    fake, _ = fake_codex
    monkeypatch.setenv("GUARD_TEST_MODE", "warning")
    result = guard.preflight(fake)
    assert result.ok and result.warnings == ("fixture warning",)


def test_preflight_refuses_missing_jq(fake_codex, monkeypatch):
    """C-14.2 refuse the hook's missing dependency before it can fail open."""
    fake, report = fake_codex
    monkeypatch.setattr(guard, "_jq_available", lambda: False)
    result = guard.preflight(fake)
    assert not result.ok and result.code == 7 and "jq" in result.fix
    assert not report.exists()


def test_preflight_refuses_missing_binary(tmp_path, monkeypatch):
    """C-14.2 return a refusal and fix when the Codex binary is unavailable."""
    monkeypatch.setattr(guard, "_jq_available", lambda: True)
    result = guard.preflight(tmp_path / "no-codex")
    assert not result.ok and result.code == 7 and result.fix


def test_preflight_does_not_cache_lane_config(fake_codex, tmp_path, monkeypatch):
    """C-14.2 recheck changed lane hooks configuration on every preflight."""
    fake, report_path = fake_codex
    home = tmp_path / "lane"
    home.mkdir()
    (home / "config.toml").write_text("[features]\nhooks=true\n")
    assert guard.preflight(fake, home=home).ok
    (home / "config.toml").write_text("[features]\nhooks=false\n")
    monkeypatch.setenv("GUARD_TEST_MODE", "missing")
    assert not guard.preflight(fake, home=home).ok
    report = json.loads(report_path.read_text())
    assert report["files"]["config.toml"] == "[features]\nhooks=false\n"


# --- deadline, diagnostics, and the v1-keyed verified-verdict cache -----------

def test_preflight_default_deadline_is_sixty_seconds(monkeypatch):
    """C-23.5, 2026-09-20: the hooks/list deadline defaults to 60 s, as in v1."""
    monkeypatch.delenv(guard.TIMEOUT_ENV, raising=False)
    assert guard.resolve_timeout() == 60.0
    assert guard.DEFAULT_TIMEOUT_S == 60.0


def test_preflight_deadline_comes_from_v1_environment_variable(fake_codex, monkeypatch):
    """CODEX_GUARD_PREFLIGHT_TIMEOUT (v1's name) bounds the daemon's probe."""
    fake, report_path = fake_codex
    monkeypatch.setenv(guard.TIMEOUT_ENV, "0.5")
    monkeypatch.setenv("GUARD_TEST_MODE", "hang")
    started = time.monotonic()
    result = guard.preflight(fake)
    assert time.monotonic() - started < 4
    assert not result.ok and result.code == 7 and result.kind == guard.TIMEOUT
    assert result.timeout_s == 0.5 and "0.5s deadline" in result.message
    assert guard.TIMEOUT_ENV in result.fix
    report = json.loads(report_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(report["pid"], 0)


@pytest.mark.parametrize("value", ["", "  ", None])
def test_preflight_blank_environment_deadline_means_default(fake_codex, monkeypatch, value):
    fake, _ = fake_codex
    if value is None:
        monkeypatch.delenv(guard.TIMEOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(guard.TIMEOUT_ENV, value)
    result = guard.preflight(fake)
    assert result.ok and result.timeout_s == 60.0


@pytest.mark.parametrize("value", ["ten", "0", "-5", "nan", "inf"])
def test_preflight_refuses_unusable_environment_deadline_before_any_probe(fake_codex, monkeypatch, value):
    """A misconfigured deadline is a configuration refusal that names the variable."""
    fake, report = fake_codex
    monkeypatch.setenv(guard.TIMEOUT_ENV, value)
    result = guard.preflight(fake)
    assert not result.ok and result.code == 7 and result.kind == guard.CONFIG
    assert guard.TIMEOUT_ENV in result.message and guard.TIMEOUT_ENV in result.fix
    assert "Restore" not in result.fix
    assert not report.exists()


def test_explicit_timeout_argument_beats_environment(fake_codex, monkeypatch):
    fake, _ = fake_codex
    monkeypatch.setenv(guard.TIMEOUT_ENV, "ten")
    result = guard.preflight(fake, timeout_s=7)
    assert result.ok and result.timeout_s == 7.0


def test_timeout_refusal_is_a_timeout_not_a_trust_mismatch(fake_codex, monkeypatch):
    """2026-09-20: a probe that never answers is reported as unverified, with the
    probe pid, elapsed time, deadline, transcript and stderr tail kept for diagnosis."""
    fake, report_path = fake_codex
    monkeypatch.setenv("GUARD_TEST_MODE", "hang")
    result = guard.preflight(fake, timeout_s=0.5)
    assert result.kind == guard.TIMEOUT and result.override is None
    assert result.message.startswith("Guard preflight timed out")
    assert "unverified, not mismatched" in result.message
    assert "TRUST" not in result.message and "Restore" not in result.fix
    report = json.loads(report_path.read_text())
    assert result.probe_pid == report["pid"]
    assert f"probe pid {report['pid']}" in result.message
    assert result.elapsed_s is not None and 0.5 <= result.elapsed_s < 4
    assert result.timeout_s == 0.5
    assert result.executable == str(fake.resolve())
    assert "fake app-server starting in mode hang" in result.stderr_tail
    sent = [line for line in result.transcript if line.startswith("> ")]
    assert len(sent) == 3 and '"method": "hooks/list"' in sent[2]
    record = result.record()
    assert record["kind"] == "timeout" and record["probe_pid"] == report["pid"]
    assert json.dumps(record)  # JSON-safe for the attempt directory


def test_version_timeout_is_reported_as_a_timeout(fake_codex, monkeypatch):
    fake, report_path = fake_codex
    monkeypatch.setenv("GUARD_TEST_VERSION_MODE", "hang")
    result = guard.preflight(fake, timeout_s=0.5)
    assert result.kind == guard.TIMEOUT and result.message.startswith("Guard preflight timed out")
    assert "--version" in result.message and "unverified, not mismatched" in result.message
    assert "argv" not in json.loads(report_path.read_text())


def test_failed_version_command_is_an_environment_refusal_with_its_stderr(fake_codex, monkeypatch):
    """A launcher that cannot run is not version drift; its stderr is the diagnosis."""
    fake, report_path = fake_codex
    monkeypatch.setenv("GUARD_TEST_VERSION_MODE", "fail")
    result = guard.preflight(fake)
    assert not result.ok and result.kind == guard.ENVIRONMENT and result.version is None
    assert "exited 127" in result.message and "node not found" in result.message
    assert "does not match TRUST" not in result.message and "Restore" not in result.fix
    assert not report_path.exists()


def test_verified_result_carries_diagnostics_and_kind(fake_codex, tmp_path):
    fake, report_path = fake_codex
    result = guard.preflight(fake, workdir=tmp_path)
    assert result.ok and result.kind == guard.VERIFIED and not result.cached
    report = json.loads(report_path.read_text())
    assert result.probe_pid == report["pid"]
    assert result.elapsed_s is not None and result.elapsed_s >= 0
    assert result.timeout_s == 60.0
    assert result.cache_key is None, "no home, nothing to cache"
    assert any(line.startswith("< ") and '"id": 2' in line for line in result.transcript)
    assert "fake app-server starting" in result.stderr_tail


@pytest.mark.parametrize("mode", ["hash-mismatch", "untrusted", "disabled", "missing", "rpc-error",
                                  "wrong-cwd", "duplicate", "error"])
def test_trust_refusals_keep_the_trust_kind_and_fix(fake_codex, monkeypatch, mode):
    fake, _ = fake_codex
    monkeypatch.setenv("GUARD_TEST_MODE", mode)
    result = guard.preflight(fake)
    assert not result.ok and result.kind == guard.TRUST
    assert result.message.startswith("Guard preflight refused") and result.fix == guard._FIX


def test_app_server_death_is_a_probe_refusal_with_stderr_and_exit_status(fake_codex, monkeypatch):
    """A crashed app-server says nothing about trust; the record carries its stderr and status."""
    fake, report_path = fake_codex
    monkeypatch.setenv("GUARD_TEST_MODE", "dies")
    result = guard.preflight(fake)
    assert not result.ok and result.code == 7 and result.kind == guard.PROBE
    assert "exited without a hooks/list response" in result.message
    assert "unverified, not mismatched" in result.message and "Restore" not in result.fix
    assert "stderr_tail" in result.fix
    assert result.exit_status == 3 and "dyld: library not loaded" in result.stderr_tail
    assert result.probe_pid == json.loads(report_path.read_text())["pid"]
    record = result.record()
    assert record["exit_status"] == 3 and record["kind"] == "probe"


def test_broken_pipe_on_the_request_write_is_a_probe_refusal(fake_codex, monkeypatch):
    """C-14.2: an app-server gone before reading stdin is not guard-file drift."""
    fake, _ = fake_codex
    real_popen = guard.subprocess.Popen

    class Popen(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if "app-server" in args[0]:
                inner = self.stdin

                class Stdin:
                    def write(self_, data):
                        raise BrokenPipeError(32, "Broken pipe")

                    def flush(self_):
                        return inner.flush()

                    def close(self_):
                        return inner.close()

                self.stdin = Stdin()

    monkeypatch.setattr(guard.subprocess, "Popen", Popen)
    result = guard.preflight(fake)
    assert not result.ok and result.kind == guard.PROBE
    assert "exited before reading the request" in result.message
    assert result.probe_pid and result.fix == guard._PROBE_FIX
    assert any(line.startswith("> ") for line in result.transcript)


def test_guard_preflight_terminates_then_kills_its_probe_tree_at_the_deadline(fake_codex, monkeypatch):
    """Invariant 53, C-23.23: the launcher can exit before its native child; the
    probe's own session is reaped as a group, so the child does not survive."""
    fake, report_path = fake_codex
    monkeypatch.setenv("GUARD_TEST_MODE", "launcher-exits")
    result = guard.preflight(fake, timeout_s=3)
    # The child inherits the launcher's stdout, so there is no EOF: the deadline
    # passes, exactly the npm-launcher shape of the 2026-09-20 incident.
    assert not result.ok and result.kind == guard.TIMEOUT and result.exit_status == 0
    report = json.loads(report_path.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(report["child_pid"], 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(report["child_pid"], 0)


@pytest.mark.parametrize("failure", ["slow-reap", "reap-error"])
def test_reap_failure_never_hides_the_probe_diagnostics(fake_codex, monkeypatch, failure):
    """Finding 1: a launcher that outlives its SIGKILL wait, or a reap that errors,
    must not replace the timeout refusal or drop its diagnostics."""
    fake, report_path = fake_codex
    monkeypatch.setenv("GUARD_TEST_MODE", "hang")
    real_stop = guard._stop_probe

    def stop(process):
        if failure == "slow-reap":
            real_wait = process.wait
            waits = []

            def wait(timeout=None):
                waits.append(timeout)
                if len(waits) == 2:
                    raise subprocess.TimeoutExpired("fake", timeout)
                return real_wait(timeout=timeout)

            process.wait = wait
            real_stop(process)
            assert len(waits) >= 2
            return
        real_stop(process)
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(guard, "_stop_probe", stop)
    result = guard.preflight(fake, timeout_s=0.5)
    assert result.kind == guard.TIMEOUT and "deadline" in result.message
    assert "unverified, not mismatched" in result.message
    report = json.loads(report_path.read_text())
    assert result.probe_pid == report["pid"]
    assert result.elapsed_s is not None and result.stderr_tail and result.transcript
    with pytest.raises(ProcessLookupError):
        os.kill(report["pid"], 0)


def test_transcript_is_bounded_and_the_response_cap_refuses(fake_codex, monkeypatch):
    fake, _ = fake_codex
    monkeypatch.setenv("GUARD_TEST_MODE", "chatty")
    result = guard.preflight(fake)
    assert result.ok and len(result.transcript) <= guard.TRANSCRIPT_LINES
    assert [line[:2] for line in result.transcript[:3]] == ["> ", "> ", "> "], "the three requests stay"
    assert '"id": 2' in result.transcript[-1], "the last responses are kept, so the answer is visible"
    assert all(len(line) <= guard.TRANSCRIPT_LINE_CHARS + 40 for line in result.transcript)
    monkeypatch.setenv("GUARD_TEST_MODE", "flood")
    result = guard.preflight(fake, timeout_s=5)
    assert not result.ok and result.kind == guard.PROBE
    assert "exceeded the preflight limit" in result.message


def test_missing_workdir_or_home_is_an_environment_refusal(fake_codex, tmp_path):
    fake, report = fake_codex
    gone = guard.preflight(fake, workdir=tmp_path / "deleted-worktree")
    assert not gone.ok and gone.kind == guard.ENVIRONMENT and "workdir is missing" in gone.message
    assert "Restore the reviewed" not in gone.fix
    moved = guard.preflight(fake, home=tmp_path / "moved-lane", workdir=tmp_path)
    assert not moved.ok and moved.kind == guard.ENVIRONMENT and "home is missing" in moved.message
    assert not report.exists()


def test_unusable_scratch_root_is_an_environment_refusal(fake_codex, tmp_path, monkeypatch):
    fake, report = fake_codex
    root = tmp_path / "root-as-file"
    root.write_text("not a directory")
    hook, pin = guard.guard_paths()
    result = guard.preflight(fake, workdir=tmp_path, state_root=root, hook_path=hook, trust_path=pin)
    assert not result.ok and result.kind == guard.ENVIRONMENT and "scratch root" in result.message
    assert not report.exists()


def test_verified_result_exposes_first_byte_and_exit_status(fake_codex, tmp_path):
    fake, _ = fake_codex
    result = guard.preflight(fake, workdir=tmp_path)
    assert result.ok and result.first_byte_s is not None and 0 <= result.first_byte_s <= result.elapsed_s
    assert result.exit_status is not None  # reaped after the answer
    assert result.record()["first_byte_s"] == result.first_byte_s


def test_version_drift_has_the_version_kind(fake_codex, monkeypatch):
    fake, _ = fake_codex
    monkeypatch.setenv("GUARD_TEST_VERSION", "codex-cli 0.154.0")
    assert guard.preflight(fake).kind == guard.VERSION


def _lane(tmp_path, config="[features]\nhooks=true\n"):
    home = tmp_path / "lane"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(config)
    (home / "hooks.json").write_text("{}\n")
    return home


def test_guard_preflight_cache_key_covers_version_home_override_and_seed_config(tmp_path):
    """C-23.5, invariant 52: the marker key is sha256(version|home|override|seed fingerprint)."""
    home = _lane(tmp_path)
    fingerprint = guard.seed_fingerprint(home)
    base = guard.cache_key("codex-cli 0.153.3", home, "hooks=a", fingerprint)
    assert base == hashlib.sha256(f"codex-cli 0.153.3|{home}|hooks=a|{fingerprint}".encode()).hexdigest()
    assert guard.cache_key("codex-cli 0.154.0", home, "hooks=a", fingerprint) != base
    assert guard.cache_key("codex-cli 0.153.3", tmp_path / "other", "hooks=a", fingerprint) != base
    assert guard.cache_key("codex-cli 0.153.3", home, "hooks=b", fingerprint) != base
    (home / "config.toml").write_text("[features]\nhooks=false\n")
    assert guard.seed_fingerprint(home) != fingerprint
    assert guard.cache_key("codex-cli 0.153.3", home, "hooks=a", guard.seed_fingerprint(home)) != base
    # The fingerprint covers file names as well as contents, and only the seed files.
    (home / "config.toml").write_text("[features]\nhooks=true\n")
    (home / "auth.json").write_text('{"access_token":"x"}')
    assert guard.seed_fingerprint(home) == fingerprint
    (home / "hooks.json").unlink()
    assert guard.seed_fingerprint(home) != fingerprint


def test_verified_verdict_is_cached_per_key_and_skips_only_the_probe(fake_codex, tmp_path):
    """C-23.5: a second launch on an unchanged lane reuses the verdict without app-server."""
    fake, report_path = fake_codex
    home = _lane(tmp_path)
    first = guard.preflight(fake, home=home, workdir=tmp_path)
    assert first.ok and first.kind == guard.VERIFIED and not first.cached and first.cache_key
    marker = guard._marker_path(guard.cache_dir(), first.cache_key)
    assert marker.is_file() and (marker.stat().st_mode & 0o777) == 0o600
    contents = json.loads(marker.read_text())
    assert contents["version"] == "codex-cli 0.153.3" and contents["home"] == str(home.resolve())
    assert contents["hooks_trust_hash"] == first.hooks_hash and contents["verified_at"]
    report_path.unlink()
    second = guard.preflight(fake, home=home, workdir=tmp_path)
    assert second.ok and second.kind == guard.CACHED and second.cached
    assert second.cache_key == first.cache_key and second.override == first.override
    assert second.version == first.version and second.hooks_hash == first.hooks_hash
    assert not report_path.exists(), "a cached verdict must not start app-server"
    assert second.probe_pid is None


def test_cached_verdict_still_checks_hook_bytes_version_and_jq(fake_codex, tmp_path, monkeypatch):
    fake, report_path = fake_codex
    home = _lane(tmp_path)
    assert guard.preflight(fake, home=home, workdir=tmp_path).ok
    report_path.unlink()
    hook = tmp_path / "hook.sh"
    hook.write_bytes(guard.guard_paths()[0].read_bytes() + b"\n# altered\n")
    hook.chmod(0o755)
    altered = guard.preflight(fake, home=home, workdir=tmp_path, hook_path=hook)
    assert not altered.ok and altered.kind == guard.TRUST and not report_path.exists()
    monkeypatch.setenv("GUARD_TEST_VERSION", "codex-cli 0.154.0")
    assert guard.preflight(fake, home=home, workdir=tmp_path).kind == guard.VERSION
    monkeypatch.delenv("GUARD_TEST_VERSION")
    monkeypatch.setattr(guard, "_jq_available", lambda: False)
    assert guard.preflight(fake, home=home, workdir=tmp_path).kind == guard.ENVIRONMENT
    assert not report_path.exists()


def test_reviewed_overlay_replacement_reverifies_at_the_same_path(fake_codex, tmp_path):
    fake, report_path = fake_codex
    home = _lane(tmp_path)
    first = guard.preflight(fake, home=home, workdir=tmp_path)
    assert first.ok and first.kind == guard.VERIFIED
    hook, pin = guard.guard_paths()
    hook.write_bytes(hook.read_bytes() + b"\n# reviewed fixture revision\n")
    trust = json.loads(pin.read_text())
    trust["hook_sha256"] = hashlib.sha256(hook.read_bytes()).hexdigest()
    pin.write_text(json.dumps(trust))
    report_path.unlink()
    second = guard.preflight(fake, home=home, workdir=tmp_path)
    assert second.ok and second.kind == guard.VERIFIED and report_path.exists()
    assert second.cache_key != first.cache_key
    assert second.override == first.override


def test_lane_config_edit_reverifies_instead_of_riding_the_marker(fake_codex, tmp_path, monkeypatch):
    fake, report_path = fake_codex
    home = _lane(tmp_path)
    first = guard.preflight(fake, home=home, workdir=tmp_path)
    (home / "config.toml").write_text("[features]\nhooks=false\n")
    monkeypatch.setenv("GUARD_TEST_MODE", "missing")
    again = guard.preflight(fake, home=home, workdir=tmp_path)
    assert not again.ok and again.kind == guard.TRUST and again.cache_key != first.cache_key
    assert json.loads(report_path.read_text())["files"]["config.toml"] == "[features]\nhooks=false\n"
    assert not guard._marker_path(guard.cache_dir(), again.cache_key).exists(), "a refusal is never cached"
    assert guard._marker_path(guard.cache_dir(), first.cache_key).exists(), "the old verdict is untouched"


def test_marker_older_than_thirty_days_is_discarded_and_pruned(fake_codex, tmp_path):
    fake, report_path = fake_codex
    home = _lane(tmp_path)
    first = guard.preflight(fake, home=home, workdir=tmp_path)
    directory = guard.cache_dir()
    marker = guard._marker_path(directory, first.cache_key)
    stale = json.loads(marker.read_text())
    from datetime import datetime, timedelta, timezone
    stale["verified_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="seconds")
    marker.write_text(json.dumps(stale))
    orphan = guard._marker_path(directory, "0" * 64)
    orphan.write_text(json.dumps(stale))
    unreadable = guard._marker_path(directory, "1" * 64)
    unreadable.write_text("not json")
    assert guard.read_cached_verdict(directory, first.cache_key) is None
    report_path.unlink()
    again = guard.preflight(fake, home=home, workdir=tmp_path)
    assert again.ok and again.kind == guard.VERIFIED and report_path.exists()
    assert json.loads(marker.read_text())["verified_at"] != stale["verified_at"], "refreshed"
    assert not orphan.exists() and not unreadable.exists(), "stale markers are pruned on write"


def test_use_cache_false_neither_reads_nor_writes_a_marker(fake_codex, tmp_path):
    fake, report_path = fake_codex
    home = _lane(tmp_path)
    result = guard.preflight(fake, home=home, workdir=tmp_path, use_cache=False)
    assert result.ok and result.kind == guard.VERIFIED and result.cache_key
    assert not guard._marker_path(guard.cache_dir(), result.cache_key).exists()
    from datetime import datetime, timezone
    guard.write_cached_verdict(guard.cache_dir(), result.cache_key,
                               {"verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    assert guard.read_cached_verdict(guard.cache_dir(), result.cache_key) is not None
    report_path.unlink()
    assert guard.preflight(fake, home=home, workdir=tmp_path, use_cache=False).kind == guard.VERIFIED
    assert report_path.exists()


def test_marker_stamped_in_the_future_is_not_a_verdict(tmp_path):
    """Finding 7: a clock jump or a hand edit must not mint a verdict that never expires."""
    from datetime import datetime, timedelta, timezone
    directory = tmp_path / "cache"
    directory.mkdir()
    ahead = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    guard._marker_path(directory, "f" * 64).write_text(json.dumps({"verified_at": ahead}))
    assert guard.read_cached_verdict(directory, "f" * 64) is None
    assert guard.prune_cached_verdicts(directory) == 1
    assert not guard._marker_path(directory, "f" * 64).exists()
    guard.write_cached_verdict(directory, "e" * 64, {"verified_at": ahead})
    assert not guard._marker_path(directory, "e" * 64).exists(), "pruned on write"


def test_relative_cache_override_resolves_under_the_state_root(monkeypatch, tmp_path):
    """Finding 6, C-2.1: a relative SUBFLEET_CODEX_GUARD_CACHE never lands in the daemon cwd."""
    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path / "root"))
    monkeypatch.setenv(guard.CACHE_ENV, "markers")
    assert guard.cache_dir() == tmp_path / "root" / "markers"
    assert guard.cache_dir(tmp_path / "daemon-root") == tmp_path / "daemon-root" / "markers"
    monkeypatch.delenv(guard.CACHE_ENV)
    assert guard.cache_dir(tmp_path / "daemon-root") == tmp_path / "daemon-root" / "guard-cache"


def test_state_root_argument_places_scratch_home_and_markers(fake_codex, tmp_path, monkeypatch):
    """The daemon's own root wins over $SUBFLEET_HOME for scratch and markers."""
    fake, report_path = fake_codex
    monkeypatch.delenv(guard.CACHE_ENV)
    home = _lane(tmp_path)
    root = tmp_path / "daemon-root"
    install_guard(root)
    result = guard.preflight(fake, home=home, workdir=tmp_path, state_root=root)
    assert result.ok
    assert guard._marker_path(root / "guard-cache", result.cache_key).is_file()
    assert json.loads(report_path.read_text())["home"].startswith(str(root / "tmp"))
    assert not (tmp_path / "state/tmp").exists()
    assert not (tmp_path / "state/guard-cache").exists()


def test_marker_fingerprint_is_the_probed_bytes(fake_codex, tmp_path):
    """Finding 8: the key names the bytes that reached the scratch home."""
    fake, report_path = fake_codex
    home = _lane(tmp_path, config="[features]\nhooks=true\n")
    result = guard.preflight(fake, home=home, workdir=tmp_path)
    marker = json.loads(guard._marker_path(guard.cache_dir(), result.cache_key).read_text())
    probed = json.loads(report_path.read_text())["files"]
    assert marker["config_fingerprint"] == guard.seed_fingerprint(
        seeds={name: text.encode() for name, text in probed.items()})


def test_cache_directory_defaults_under_the_state_root_and_honours_v1_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path / "root"))
    monkeypatch.delenv(guard.CACHE_ENV, raising=False)
    assert guard.cache_dir() == tmp_path / "root" / "guard-cache"
    monkeypatch.setenv(guard.CACHE_ENV, str(tmp_path / "elsewhere"))
    assert guard.cache_dir() == tmp_path / "elsewhere"


def test_explicit_cache_directory_argument_wins(fake_codex, tmp_path):
    fake, _ = fake_codex
    home = _lane(tmp_path)
    result = guard.preflight(fake, home=home, workdir=tmp_path, cache_directory=tmp_path / "explicit")
    assert result.ok
    assert guard._marker_path(tmp_path / "explicit", result.cache_key).is_file()
    assert not (tmp_path / "guard-cache").exists()


def test_unwritable_cache_directory_does_not_turn_a_verdict_into_a_refusal(fake_codex, tmp_path, monkeypatch):
    fake, _ = fake_codex
    home = _lane(tmp_path)
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a directory")
    result = guard.preflight(fake, home=home, workdir=tmp_path, cache_directory=blocked)
    assert result.ok and result.kind == guard.VERIFIED


def test_answer_already_in_the_pipe_beats_an_expired_deadline(fake_codex, monkeypatch):
    """A daemon paused mid-probe (clamshell-guard SIGSTOPs subfleetd on a closed lid)
    wakes past its deadline; an answer that arrived meanwhile is still honoured."""
    fake, report_path = fake_codex
    real = time.monotonic
    state = {"calls": 0}

    def answered():
        try:
            return "request" in json.loads(report_path.read_text())
        except (OSError, ValueError):
            return False

    def paused_clock():
        state["calls"] += 1
        if state["calls"] == 1:
            return real()          # `started`
        if state["calls"] == 2:
            # The pause: wait (bounded) until the fake has seen hooks/list, then
            # a moment more for its answer to reach the pipe.
            limit = real() + 20
            while not answered() and real() < limit:
                time.sleep(0.05)
            time.sleep(0.3)
        return real() + 3600       # every later reading is past the deadline

    monkeypatch.setattr(guard.time, "monotonic", paused_clock)
    ok, diagnostics = guard._hooks_list(
        str(fake), home=_scratch(fake), workdir=fake.parent,
        override=guard.override_string(guard.guard_paths()[0].resolve()),
        env=dict(os.environ), timeout_s=5)
    assert ok.get("id") == 2 and not diagnostics["timed_out"]


def _scratch(fake):
    home = fake.parent / "scratch-home"
    home.mkdir(exist_ok=True)
    return home


@pytest.mark.parametrize("missing", ["trust", "hook"])
def test_missing_guard_file_is_a_trust_refusal(fake_codex, tmp_path, missing):
    """Round 2: a deleted or unreadable guard file keeps the restore-the-reviewed-files advice."""
    fake, report = fake_codex
    options = ({"trust_path": tmp_path / "no-TRUST"} if missing == "trust"
               else {"hook_path": tmp_path / "no-hook.sh"})
    result = guard.preflight(fake, **options)
    assert not result.ok and result.kind == guard.TRUST and result.fix == guard._FIX
    assert "guard file unreadable" in result.message
    assert not report.exists()


def test_version_timeout_keeps_the_stderr_printed_before_the_hang(fake_codex, monkeypatch):
    fake, _ = fake_codex

    class Hung:
        def __init__(self, *args, **kwargs):
            self.pid, self.returncode, self.stdin, self.stdout = 0, None, None, None

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("codex", timeout, stderr=b"launcher: waiting for native binary\n")

    stopped = []
    monkeypatch.setattr(guard.subprocess, "Popen", Hung)
    monkeypatch.setattr(guard, "_stop_probe", stopped.append)
    status, stdout, stderr = guard._version(str(fake), env={}, timeout_s=1)
    assert status is None and stdout == "" and "waiting for native binary" in stderr
    assert len(stopped) == 1


def test_relative_cache_override_may_not_leave_the_state_root(fake_codex, tmp_path, monkeypatch):
    """Round 2, C-2.1: `../shared` is refused as a configuration error before any probe."""
    fake, report = fake_codex
    monkeypatch.setenv(guard.CACHE_ENV, "../shared")
    with pytest.raises(ValueError):
        guard.cache_dir(tmp_path / "root")
    home = _lane(tmp_path)
    result = guard.preflight(fake, home=home, workdir=tmp_path)
    assert not result.ok and result.kind == guard.CONFIG
    assert guard.CACHE_ENV in result.message and guard.CACHE_ENV in result.fix
    assert not report.exists() and not (tmp_path / "shared").exists()
