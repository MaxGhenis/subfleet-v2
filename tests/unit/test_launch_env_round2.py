"""Round 2: configuration inheritance never overrides launch isolation."""

import json
import os
from types import SimpleNamespace

import pytest

from subfleet import cli, doctor
from subfleet.launch_env import build_environment
from subfleet.provider_env import ProviderEnvironment
from tests.unit.test_launch_env_allowlist import launch_for


def test_daemon_start_preserves_harness_configuration_not_credentials(tmp_path, monkeypatch):
    parent = {"PATH": "/usr/bin:/bin", "SUBFLEET_FAKE_SCENARIO": "dummy",
              "SUBFLEET_LIVE": "0", "SUBFLEET_TEST_TOKEN": "dummy"}
    monkeypatch.setattr(os, "environ", parent)
    states = iter([False, True])
    monkeypatch.setattr(cli, "_daemon_alive", lambda client: next(states))
    monkeypatch.setattr(cli, "_daemond_argv", lambda root: ["fake-daemon"])
    monkeypatch.setattr(cli.Client, "lock_info", lambda self: {})
    seen = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs:
                        seen.append(kwargs["env"]) or SimpleNamespace(pid=123))
    assert cli.cmd_daemon_start(SimpleNamespace(state_root=str(tmp_path))) == 0
    child = ProviderEnvironment({}).build("claude", environ=seen[0])
    assert child["SUBFLEET_FAKE_SCENARIO"] == "dummy"
    assert child["SUBFLEET_LIVE"] == "0"
    assert "SUBFLEET_TEST_TOKEN" not in child


@pytest.mark.parametrize("word", ["TOKEN", "KEY", "SECRET", "PASSWORD"])
@pytest.mark.parametrize("suffix", ["", "_FILE"])
def test_configuration_prefix_refuses_credential_words(word, suffix):
    name = "SUBFLEET_TEST_" + word + suffix
    parent = {name: "dummy", "SUBFLEET_FAKE_SCENARIO": "dummy"}
    assert set(build_environment(environ=parent)) == {"SUBFLEET_FAKE_SCENARIO"}
    assert name in build_environment(environ=parent, pass_names=[name])


@pytest.mark.parametrize("mode", ["detached", "resume", "turn", "isolated"])
def test_memory_removal_beats_exact_policy(mode, tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin",
                                       "CLAUDE_MEMORY_STORES": "/dummy/private-memory"})
    lane, launch = launch_for("claude", mode, tmp_path, monkeypatch)
    from subfleet.daemon import Daemon
    daemon = Daemon.__new__(Daemon)
    daemon._provider_environment = ProviderEnvironment({"env": {"pass": ["CLAUDE_MEMORY_STORES"]}})
    assert "CLAUDE_MEMORY_STORES" in launch.env_remove
    assert "CLAUDE_MEMORY_STORES" not in daemon._launch_environment(lane, launch)


def test_mandatory_removal_also_beats_selected_additions():
    environment = ProviderEnvironment({"env": {"pass": ["CLAUDE_MEMORY_STORES"]}})
    assert environment.build("claude", {"CLAUDE_MEMORY_STORES": "dummy"},
                             ("CLAUDE_MEMORY_STORES",), environ={}) == {}


def test_doctor_invalid_timeout_names_variable_without_value(tmp_path, monkeypatch):
    marker = "DUMMY_INVALID_TIMEOUT_222"
    monkeypatch.setenv("CODEX_GUARD_PREFLIGHT_TIMEOUT", marker)
    item = doctor.check_guard_preflight(tmp_path)
    assert item["status"] == doctor.FAIL
    rendered = doctor.render([item])
    assert "CODEX_GUARD_PREFLIGHT_TIMEOUT" in rendered
    assert marker not in rendered


@pytest.mark.parametrize("path", ["gate", "salvage", "finalization", "retention"])
def test_policy_reaches_helpers_with_mandatory_git_removals(path, tmp_path, monkeypatch):
    from subfleet import launch_env, retention_git, salvage
    from subfleet.gate import merge
    monkeypatch.setattr(os, "environ", {"GH_TOKEN": "dummy", "OTHER_TOKEN": "dummy",
                                       "GIT_DIR": "/dummy/other-repo", "GIT_TRACE": "dummy"})
    policy = {"env": {"pass": ["GH_TOKEN", "GIT_DIR", "GIT_TRACE"]}}
    seen = []
    with launch_env.helper_policy(policy):
        if path == "gate":
            merge.run_command(["fake-gh"], cwd=tmp_path, runner=lambda *args, **kwargs:
                              seen.append(kwargs["env"]))
        elif path == "retention":
            seen.append(retention_git.environment())
        else:
            monkeypatch.setattr(salvage.subprocess, "run", lambda *args, **kwargs:
                                seen.append(kwargs["env"]) or SimpleNamespace(returncode=0, stdout=b"", stderr=b""))
            if path == "salvage":
                salvage._git(tmp_path, "rev-parse", "HEAD")
            else:
                from subfleet.conversations import diff
                diff._git(tmp_path, "diff", "--binary")
    assert seen
    for env in seen:
        assert env["GH_TOKEN"] == "dummy"
        assert "OTHER_TOKEN" not in env
        if path != "gate":
            assert "GIT_DIR" not in env and "GIT_TRACE" not in env
    assert "GH_TOKEN" not in build_environment()


def test_helper_policy_is_nested_and_provider_policy_stays_explicit(monkeypatch):
    from subfleet.launch_env import helper_policy
    monkeypatch.setattr(os, "environ", {"FIRST_TOKEN": "dummy", "SECOND_TOKEN": "dummy"})
    with helper_policy({"env": {"pass": ["FIRST_TOKEN"]}}):
        assert set(build_environment()) == {"FIRST_TOKEN"}
        assert set(ProviderEnvironment({"env": {"pass": ["SECOND_TOKEN"]}}).build("claude")) == {"SECOND_TOKEN"}
        with pytest.raises(RuntimeError), helper_policy({"env": {"pass": ["SECOND_TOKEN"]}}):
            assert set(build_environment()) == {"SECOND_TOKEN"}
            raise RuntimeError("dummy")
        assert set(build_environment()) == {"FIRST_TOKEN"}
    assert build_environment() == {}
