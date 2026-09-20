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
print("fake app-server starting in mode " + mode, file=sys.stderr, flush=True)
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
    monkeypatch.delenv(guard.TIMEOUT_ENV, raising=False)
    # C-2.1: scratch homes and verified markers stay under this test's own root,
    # never the real state root of the machine running the suite.
    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(guard.CACHE_ENV, str(tmp_path / "guard-cache"))
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
    assert result.override is None
    assert "daemon scheduling" in result.fix and "trust remains unverified" in result.fix
    assert "Restore" not in result.fix
    report = json.loads(report_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(report["pid"], 0)
    assert not Path(report["home"]).exists()


def test_version_timeout_does_not_claim_version_drift(fake_codex, monkeypatch):
    fake, report_path = fake_codex

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(guard.subprocess, "run", timeout)
    result = guard.preflight(fake)
    assert not result.ok and result.code == 7 and result.override is None
    assert result.version is None and "daemon scheduling" in result.fix
    assert "Restore" not in result.fix
    assert not report_path.exists(), "a version timeout must not start app-server"


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

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(guard.subprocess, "run", timeout)
    result = guard.preflight(fake)
    assert result.kind == guard.TIMEOUT and result.message.startswith("Guard preflight timed out")
    assert "--version" in result.message and "unverified, not mismatched" in result.message
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


@pytest.mark.parametrize("mode", ["hash-mismatch", "untrusted", "disabled", "missing", "dies", "rpc-error"])
def test_trust_refusals_keep_the_trust_kind_and_fix(fake_codex, monkeypatch, mode):
    fake, _ = fake_codex
    monkeypatch.setenv("GUARD_TEST_MODE", mode)
    result = guard.preflight(fake)
    assert not result.ok and result.kind == guard.TRUST
    assert result.message.startswith("Guard preflight refused") and result.fix == guard._FIX


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
    hook.write_bytes(guard.HOOK_PATH.read_bytes() + b"\n# altered\n")
    hook.chmod(0o755)
    altered = guard.preflight(fake, home=home, workdir=tmp_path, hook_path=hook)
    assert not altered.ok and altered.kind == guard.TRUST and not report_path.exists()
    monkeypatch.setenv("GUARD_TEST_VERSION", "codex-cli 0.154.0")
    assert guard.preflight(fake, home=home, workdir=tmp_path).kind == guard.VERSION
    monkeypatch.delenv("GUARD_TEST_VERSION")
    monkeypatch.setattr(guard, "_jq_available", lambda: False)
    assert guard.preflight(fake, home=home, workdir=tmp_path).kind == guard.ENVIRONMENT
    assert not report_path.exists()


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
    guard.write_cached_verdict(guard.cache_dir(), result.cache_key, {"verified_at": "2099-01-01T00:00:00+00:00"})
    report_path.unlink()
    assert guard.preflight(fake, home=home, workdir=tmp_path, use_cache=False).kind == guard.VERIFIED
    assert report_path.exists()


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
