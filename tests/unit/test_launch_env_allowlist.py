"""D-ENV1: arbitrary parent names cannot become ambient launch authority."""

import json
import os
import plistlib
from pathlib import Path
import subprocess
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import cli, doctor, guardian
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.adapters.codex import CodexAdapter
from subfleet.contracts import Credential, JobSpec, Lane, LaneOwner, Sandbox
from subfleet.conversations.launch import claude_launch, codex_launch
from subfleet.daemon import Daemon
from subfleet.provider_env import ProviderEnvironment

# Independent oracle: additions require an explicit reason in the D-ENV1 report.
OBSERVED = frozenset("HOME LOGNAME MKL_NUM_THREADS NUMEXPR_NUM_THREADS OMP_NUM_THREADS "
    "OMP_WAIT_POLICY OPENBLAS_NUM_THREADS OSLogRateLimit PATH PYTEST_XDIST_AUTO_NUM_WORKERS "
    "SHELL SSH_AUTH_SOCK SSL_CERT_FILE SUBFLEET_HOME SUBFLEET_RETENTION_DORMANT TMPDIR USER "
    "VECLIB_MAXIMUM_THREADS XPC_FLAGS XPC_SERVICE_NAME".split())
ADDITIONAL = frozenset("LANG LC_ALL LC_COLLATE LC_CTYPE LC_MESSAGES LC_MONETARY LC_NUMERIC LC_TIME "
    "TERM TZ __CF_USER_TEXT_ENCODING SSL_CERT_DIR HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY "
    "http_proxy https_proxy all_proxy no_proxy SUBFLEET_CLAUDE_DIR SUBFLEET_CLAUDE_SETTINGS "
    "SUBFLEET_CODEX_GUARD_CACHE SUBFLEET_CODEX_HOMES SUBFLEET_CODEX_UNIFIED_EXEC "
    "SUBFLEET_DESKTOP_LOG SUBFLEET_DEV_APP_EXECUTABLE SUBFLEET_GIT_TIMEOUT_S SUBFLEET_GUARD_TRUST "
    "SUBFLEET_HOOK_COMMAND SUBFLEET_HOOK_TIMEOUT_S SUBFLEET_NOTIFY_MODE SUBFLEET_PROVIDER_QOS "
    "SUBFLEET_RESET_INHIBIT SUBFLEET_RETENTION_QOS SUBFLEET_SESSION_STORE SUBFLEET_TICKLE "
    "CODEX_GUARD_PREFLIGHT_TIMEOUT".split())
ALLOWED = OBSERVED | ADDITIONAL
POLLUTION = {"FOO_API_KEY": "dummy", "AXIOM_TEST_SIGNING_KEY": "dummy",
             "SUBFLEET_UNLISTED_SECRET": "dummy", "LC_UNLISTED_TOKEN": "dummy",
             "UNRELATED": "dummy"}
POLICY = {"providers": {"codex": {"ca_bundle": None}}}
PATHS = [(provider, mode) for provider in ("codex", "claude")
         for mode in ("detached", "resume", "turn", "probe", "isolated")]


def assert_environment(actual, expected):
    """Failures disclose names only, including Hypothesis counterexamples."""
    assert set(actual) == set(expected)
    assert all(actual[name] == expected[name] for name in expected), "child values changed"


def launch_for(provider, mode, directory, monkeypatch):
    directory.mkdir(parents=True, exist_ok=True)
    home = directory / "home"
    home.mkdir(exist_ok=True)
    prompt = directory / "prompt.md"
    prompt.write_text("Reply OK")
    credential = {"CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR": str(home)}
    lane = Lane(provider + "-1", provider, provider + ":fake", Credential(provider, str(home), "home"),
                str(home), LaneOwner.V2, False)
    adapter = CodexAdapter() if provider == "codex" else ClaudeAdapter(projects_dir=home / "projects")
    if provider == "codex":
        def inspect(*args, **kwargs):
            selected = {k: v for k, v in os.environ.items() if k in ALLOWED}
            assert_environment(kwargs["env"], {**selected, **credential})
            return {"requirements": None}, {"layers": [{"name": {"type": "user"}, "config": {}}]}, []
        adapter.isolation_inspector = inspect
    job = JobSpec(request_id="fake", kind="dispatch", workdir=str(directory), prompt_path=str(prompt),
                  task="review", tier="standard", pinned_model=None, pinned_lane=None,
                  sandbox=Sandbox.READ_ONLY, out_path=None, name="env-test",
                  isolated_review=mode == "isolated", review_root=str(directory) if mode == "isolated" else None)
    if mode == "turn":
        turn = {"provider": provider, "conversation_id": "conversation", "message_id": "message",
                "text": "Reply OK", "cwd": str(directory), "new_session_id": "fake-session",
                "settings": {"model": "fake", "permission": "read-only"}}
        if provider == "claude":
            launch = claude_launch(turn, attempt_id="fake/a1", attempt_dir=directory, lane=lane,
                                   credential_env=credential, model_id="fake", adapter=adapter)
        else:
            launch = codex_launch(turn, attempt_id="fake/a1", attempt_dir=directory, lane=lane,
                                  credential_env=credential, model_id="fake", executable="codex", override="hooks={}")
    elif mode == "resume":
        launch = adapter.resume_launch(job, "fake/a1", directory, lane, credential, "fake-session", prompt, None)
    else:
        launch = adapter.build_launch(job, "fake/a1", directory, lane, credential, "fake", None, prompt, None)
    return lane, launch


@pytest.mark.parametrize("provider,mode", PATHS)
def test_each_lane_launch_drops_polluted_parent(provider, mode, tmp_path, monkeypatch):
    """C-12.3/4: named regression for every detached/resume/turn/probe/review path."""
    parent = {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", **POLLUTION}
    monkeypatch.setattr(os, "environ", parent)
    lane, launch = launch_for(provider, mode, tmp_path, monkeypatch)
    daemon = Daemon.__new__(Daemon)
    daemon._provider_environment = ProviderEnvironment(POLICY)
    actual = daemon._launch_environment(lane, launch)
    expected = {k: v for k, v in parent.items() if k in ALLOWED}
    for name in launch.env_remove:
        expected.pop(name, None)
    expected.update(launch.env_add)
    assert_environment(actual, expected)


NAMES = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,24}", fullmatch=True)
VALUES = st.text(alphabet=st.characters(exclude_categories=("Cs",), exclude_characters="\0"), max_size=30)
PARENT_NAMES = st.one_of(NAMES, st.text(alphabet=st.characters(
    exclude_categories=("Cs",), exclude_characters="\0="), min_size=1, max_size=24))


class ParentEnvironment(dict):
    def __repr__(self):
        return "<parent names: " + ", ".join(sorted(self)) + ">"


PARENTS = st.dictionaries(st.one_of(PARENT_NAMES, st.sampled_from(sorted(ALLOWED)),
                                  NAMES.map(lambda n: n + "_KEY"), NAMES.map(lambda n: n + "_TOKEN"),
                                  NAMES.map(lambda n: n + "_SECRET"), NAMES.map(lambda n: n + "_PASSWORD")),
                          VALUES, max_size=35).map(ParentEnvironment)


@settings(max_examples=30, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(parent=PARENTS, passed=st.lists(st.one_of(NAMES, st.sampled_from(
    [*sorted(POLLUTION), "OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"])), max_size=5, unique=True))
def test_bounded_property_all_lane_launches(parent, passed, tmp_path, monkeypatch):
    """C-12.3/4: exact union, all allowed names, arbitrary names and secret suffixes."""
    parent = {**dict.fromkeys(ALLOWED, "dummy"), **parent, **POLLUTION}
    for name in passed:
        parent.setdefault(name, "dummy")
    environment = ProviderEnvironment({**POLICY, "env": {"pass": passed}})
    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", parent)
        daemon = Daemon.__new__(Daemon)
        daemon._provider_environment = environment
        for provider, mode in PATHS:
            lane, launch = launch_for(provider, mode, tmp_path / (provider + mode), patch)
            actual = daemon._launch_environment(lane, launch)
            expected = {k: v for k, v in parent.items() if k in ALLOWED or k in passed}
            for name in launch.env_remove:
                if name not in passed:
                    expected.pop(name, None)
            expected.update(launch.env_add)
            assert_environment(actual, expected)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_daemon_start_environment_and_launchd_are_differentially_equal(tmp_path, monkeypatch, provider):
    """C-12.3/4: pollution cannot distinguish shell-started and launchd daemons."""
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin"})
    monkeypatch.setattr(cli, "_daemond_argv", lambda root: ["fake-daemon"])
    clean = plistlib.loads(cli._plist(tmp_path))["EnvironmentVariables"]
    monkeypatch.setattr(os, "environ", {**clean, **POLLUTION})
    started = cli.daemon_env(tmp_path)
    assert_environment(started, clean)
    environment = ProviderEnvironment(POLICY)
    credential = {"CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR": "/fake"}
    assert_environment(environment.build(provider, credential, environ=started),
                       environment.build(provider, credential, environ=clean))


def test_daemon_start_popen_gets_only_selected_names(tmp_path, monkeypatch):
    """C-17.x: exercise the actual CLI spawn seam, without starting a daemon."""
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", "SUBFLEET_HOME": str(tmp_path), **POLLUTION})
    states = iter([False, True])
    monkeypatch.setattr(cli, "_daemon_alive", lambda client: next(states))
    monkeypatch.setattr(cli, "_daemond_argv", lambda root: ["fake-daemon"])
    monkeypatch.setattr(cli.Client, "lock_info", lambda self: {})
    seen = []
    def spawn(argv, **kwargs):
        seen.append(kwargs["env"])
        return SimpleNamespace(pid=123)
    monkeypatch.setattr(cli.subprocess, "Popen", spawn)
    assert cli.cmd_daemon_start(SimpleNamespace(state_root=str(tmp_path))) == 0
    assert_environment(seen[0], {"PATH": "/usr/bin:/bin", "SUBFLEET_HOME": str(tmp_path)})


def test_claude_direct_probe_filters_its_parent(tmp_path, monkeypatch):
    """C-12.4: standalone enrollment and probes use the same allowlist."""
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", **POLLUTION})
    seen = []
    def runner(argv, **kwargs):
        seen.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    adapter = ClaudeAdapter(runner=runner, projects_dir=tmp_path)
    adapter._run_turn({"CLAUDE_CONFIG_DIR": str(tmp_path)}, "fake")
    assert_environment(seen[0], {"PATH": "/usr/bin:/bin", "CLAUDE_CONFIG_DIR": str(tmp_path)})


@pytest.mark.parametrize("relay", [False, True], ids=["stdin", "relay"])
def test_guardian_provider_spawn_filters_again(tmp_path, monkeypatch, relay):
    """C-12.3/4: both guardian exec paths reject an independently polluted parent."""
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", **POLLUTION})
    monkeypatch.setattr(guardian.os, "setsid", lambda: None)
    monkeypatch.setattr(guardian.signal, "signal", lambda *args: None)
    monkeypatch.setattr(guardian, "boot_id", lambda: "fake")
    monkeypatch.setattr(guardian, "proc_start", lambda pid: "fake")
    seen = []
    def spawn(argv, **kwargs):
        seen.append(kwargs.get("env", dict(os.environ)))
        return SimpleNamespace(pid=123, wait=lambda: 0)
    monkeypatch.setattr(guardian.subprocess, "Popen", spawn)
    if relay:
        from subfleet import relay as relay_module
        monkeypatch.setattr(relay_module, "RelayServer", lambda *args, **kwargs: SimpleNamespace(
            bind=lambda: None, serve=lambda fd, **kwargs: os.close(fd), stop=lambda: None))
    previous = os.umask(0o077)
    try:
        assert guardian.run_guardian(["fake-provider"], attempt_dir=tmp_path, cwd=str(tmp_path),
            stdin_path=None, stdout_path=str(tmp_path / "stdout"), stderr_path=str(tmp_path / "stderr"),
            control_socket=str(tmp_path / "relay.sock") if relay else None) == 0
    finally:
        os.umask(previous)
    assert_environment(seen[0], {"PATH": "/usr/bin:/bin"})


def test_doctor_names_daemon_extras_without_values(tmp_path, monkeypatch):
    """C-17.x: inspect the daemon's report, never the doctor's caller environment."""
    monkeypatch.setattr(doctor.Client, "call", lambda *args, **kwargs: {
        "environment": {"extra_names": ["AXIOM_TEST_SIGNING_KEY", "FOO_API_KEY"]}})
    check = getattr(doctor, "check_daemon_environment", None)
    assert callable(check), "doctor must report daemon environment names"
    item = check(tmp_path)
    assert item["status"] == doctor.WARN
    rendered = doctor.render([item])
    assert all(name in rendered for name in ("FOO_API_KEY", "AXIOM_TEST_SIGNING_KEY"))
    assert "dummy" not in rendered


def test_environment_diagnostic_uses_names_only(monkeypatch):
    from subfleet import launch_env
    monkeypatch.setattr(os, "environ", {"PATH": "dummy", **POLLUTION})
    report = launch_env.environment_report()
    assert report == {"extra_names": sorted(POLLUTION)}
    assert "dummy" not in json.dumps(report)


@pytest.mark.parametrize("status", [None, {}, {"environment": {"extra_names": "invalid"}},
                                  {"environment": {"extra_names": [123]}}])
def test_doctor_handles_old_or_malformed_diagnostics_without_echoing_reply(tmp_path, monkeypatch, status):
    monkeypatch.setattr(doctor.Client, "call", lambda *args, **kwargs: status)
    item = doctor.check_daemon_environment(tmp_path)
    assert item["status"] == doctor.UNKNOWN
    assert "invalid" not in item["detail"] and "123" not in item["detail"]


def test_doctor_reports_unusual_os_names_without_line_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.Client, "call", lambda *args, **kwargs: {
        "environment": {"extra_names": ["FOO.API_KEY", "ODD\nNAME"]}})
    item = doctor.check_daemon_environment(tmp_path)
    assert item["status"] == doctor.WARN
    assert "FOO.API_KEY" in item["detail"] and "ODD\\nNAME" in item["detail"]
    assert "\n" not in item["detail"]


@pytest.mark.parametrize("source", ["default", "policy", "credential"])
def test_claude_transcript_location_uses_selected_launch_environment(tmp_path, monkeypatch, source):
    home, other, lane_home = (tmp_path / name for name in ("home", "other", "lane"))
    monkeypatch.setattr(os, "environ", {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(other)})
    adapter = ClaudeAdapter()
    credential = {}
    expected = home / ".claude"
    if source == "policy":
        adapter.provider_environment = ProviderEnvironment({"env": {"pass": ["CLAUDE_CONFIG_DIR"]}})
        expected = other
    elif source == "credential":
        credential["CLAUDE_CONFIG_DIR"] = str(lane_home)
        expected = lane_home
    path = adapter.expected_transcript_path(tmp_path, "fake-session", credential)
    assert path.is_relative_to(expected / "projects")
