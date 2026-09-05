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
if mode == "hang":
    time.sleep(30)
    raise SystemExit(1)
if mode == "dies":
    raise SystemExit(1)
for line in sys.stdin:
    request = json.loads(line)
    if request.get("id") == 1:
        print(json.dumps({"id": 1, "result": {"userAgent": "fake"}}), flush=True)
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
    monkeypatch.setattr(guard, "_jq_available", lambda: True)
    return script, report


def test_copied_hook_matches_pinned_sha256():
    """C-14.1 pin the exact bytes copied from the v1 never-rules hook."""
    trust = json.loads(guard.TRUST_PATH.read_text())
    assert hashlib.sha256(guard.HOOK_PATH.read_bytes()).hexdigest() == trust["hook_sha256"]
    assert os.access(guard.HOOK_PATH, os.X_OK)


def test_hook_has_valid_bash_syntax():
    """C-14.1 the byte-preserved Bash hook remains syntactically executable."""
    result = subprocess.run(["/bin/bash", "-n", str(guard.HOOK_PATH)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_hash_matches_v1_live_verified_pin():
    """C-14.2 reproduce v1's independent 0.144.0 live-verified identity hash."""
    assert guard.hooks_trust_hash(
        "/Users/maxghenis/chief-of-staff/carpool/bin/carpool-guard-hook", timeout=30
    ) == "sha256:40f500b8eb48e50cad1c5886605713f0e91fc159163e9cb874128ec93f6fffb0"


def test_override_matches_v1_pinned_string():
    """C-14.2, C-12.3 preserve v1's exact complete hooks= override argument."""
    trust = json.loads(guard.TRUST_PATH.read_text())
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
    assert result.override == guard.override_string(guard.HOOK_PATH.resolve())
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
    hook.write_bytes(guard.HOOK_PATH.read_bytes() + b"\n# altered\n")
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
    trust = json.loads(guard.TRUST_PATH.read_text())
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
    trust = json.loads(guard.TRUST_PATH.read_text())
    trust["hook_sha256"] = "0" * 64
    pin = tmp_path / "TRUST"
    pin.write_text(json.dumps(trust))
    monkeypatch.setenv("SUBFLEET_GUARD_TRUST", str(pin))
    result = guard.preflight(fake)
    assert not result.ok and result.code == 7
    assert "SHA-256" in result.message
    assert "subfleet doctor" in result.fix
    assert not report.exists()
    assert guard.preflight(fake, trust_path=guard.TRUST_PATH).ok


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
    report = json.loads(report_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(report["pid"], 0)
    assert not Path(report["home"]).exists()


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
