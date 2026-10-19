"""D-ENV1: provider metadata, healing and local helpers share the allowlist."""

import io
import json
import os
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings
import pytest

from subfleet import claude_cards, compat, descriptors, doctor, retention_git, salvage
from subfleet.adapters import isolation
from subfleet.guard import preflight
from subfleet.gate import merge
from subfleet.conversations import catalog
from subfleet.sessions import nudge
from subfleet.provider_env import ProviderEnvironment
from tests.unit.test_launch_env_allowlist import ALLOWED, PARENTS, POLLUTION, assert_environment


HELPERS = ("heal", "claude-version", "doctor-version", "codex-inspection", "guard-version",
           "guard-hooks", "salvage", "retention", "gate", "catalog", "nudge", "descriptors", "compat")


def helper_environments(path, parent, monkeypatch, tmp_path, passed=()):
    """Capture actual subprocess kwargs; never read a credential or run a provider."""
    seen = []
    monkeypatch.setattr(os, "environ", parent)
    def runner(argv, **kwargs):
        seen.append(kwargs.get("env", dict(os.environ)))
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")
    def popen(argv, **kwargs):
        seen.append(kwargs.get("env", dict(os.environ)))
        return SimpleNamespace(pid=123, returncode=0, stdin=io.BytesIO(),
            stdout=SimpleNamespace(fileno=lambda: 123), communicate=lambda **_: (b"", b""), wait=lambda **_: 0)
    base = {k: v for k, v in parent.items() if k in ALLOWED or k in passed}
    expected = dict(base)
    environment = ProviderEnvironment({"env": {"pass": list(passed)}})
    if path == "heal":
        monkeypatch.setattr(claude_cards, "_kill_group", lambda *args, **kwargs: None)
        claude_cards.heal_turn(tmp_path, model="fake", prompt="OK", popen=popen, environment=environment)
        expected["CLAUDE_CONFIG_DIR"] = str(tmp_path)
    elif path == "claude-version":
        claude_cards.cli_version("fake-claude", runner=runner, environment=environment)
    elif path == "doctor-version":
        monkeypatch.setattr(doctor.subprocess, "run", runner)
        doctor._run_full(["fake-provider", "--version"], **({"env": environment.build("claude")} if passed else {}))
    elif path == "guard-version":
        monkeypatch.setattr(preflight.subprocess, "Popen", popen)
        preflight._version("fake-codex", env=parent, timeout_s=1, pass_names=passed)
    elif path == "guard-hooks":
        def refused(argv, **kwargs):
            seen.append(kwargs.get("env", dict(os.environ)))
            raise OSError("fake spawn stopped")
        monkeypatch.setattr(preflight.subprocess, "Popen", refused)
        with pytest.raises(OSError):
            preflight._hooks_list("fake-codex", home=tmp_path, workdir=tmp_path,
                             override="hooks={}", env=parent, timeout_s=1, pass_names=passed)
        expected["CODEX_HOME"] = str(tmp_path)
    elif path == "codex-inspection":
        monkeypatch.setattr(isolation.subprocess, "Popen", popen)
        monkeypatch.setattr(isolation.subprocess, "run", runner)
        monkeypatch.setattr(preflight, "_stop_probe", lambda *args: None)
        class Selector:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def register(self, *args): pass
            def select(self, *args): return [True]
        monkeypatch.setattr(isolation.selectors, "DefaultSelector", Selector)
        responses = [{"id": 2, "result": {"requirements": None}},
                     {"id": 3, "result": {"layers": [{"name": {"type": "user"}, "config": {}}]}}]
        monkeypatch.setattr(isolation.os, "read", lambda *args: ("\n".join(map(json.dumps, responses)) + "\n").encode())
        isolation.inspect_codex("fake-codex", home=tmp_path, workdir=tmp_path, env=parent, pass_names=passed)
        expected["CODEX_HOME"] = str(tmp_path.resolve())
    elif path == "salvage":
        seen.append(salvage._git_env(None))
        expected.update(dict.fromkeys(salvage.GIT_TRACE2_TARGETS, "0"), LC_ALL="C")
    elif path == "retention":
        seen.append(retention_git.environment())
        expected.update(dict.fromkeys(salvage.GIT_TRACE2_TARGETS, "0"), LC_ALL="C")
        expected.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0",
            GIT_NO_REPLACE_OBJECTS="1", GIT_ASKPASS="/usr/bin/false", SSH_ASKPASS="/usr/bin/false")
    elif path == "gate":
        merge.run_command(["fake-gh"], cwd=tmp_path, runner=runner)
    elif path == "catalog":
        monkeypatch.setattr(catalog, "refresh_running", lambda root: False)
        monkeypatch.setattr(catalog.subprocess, "Popen", popen)
        catalog.spawn_refresh(tmp_path)
        expected["PYTHONPATH"] = str(catalog.Path(catalog.__file__).resolve().parents[2])
    elif path == "nudge":
        nudge.spawn("fake-session", source="startup", transcript=None, delay_s=0, root=tmp_path, popen=popen)
        expected.update(SUBFLEET_HOME=str(tmp_path), PYTHONPATH=str(nudge.Path(nudge.__file__).resolve().parents[2]))
    elif path == "descriptors":
        monkeypatch.setattr(descriptors.sys, "platform", "darwin")
        monkeypatch.setattr(descriptors.subprocess, "run", runner)
        descriptors.kernel_open_files_ceiling()
    elif path == "compat":
        monkeypatch.setattr(compat, "v1_binary", lambda: "fake-legacy")
        compat.delegate(["fake-command"], runner=runner)
    assert seen, "helper did not reach its subprocess boundary"
    return seen, expected


@pytest.mark.parametrize("path", HELPERS)
def test_helper_spawn_drops_polluted_parent(path, tmp_path, monkeypatch):
    parent = {"PATH": "/usr/bin:/bin", **POLLUTION}
    seen, expected = helper_environments(path, parent, monkeypatch, tmp_path)
    for env in seen:
        assert_environment(env, expected)


@settings(max_examples=20, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(parent=PARENTS)
def test_bounded_property_all_helper_spawns(parent, tmp_path, monkeypatch):
    parent = {**dict.fromkeys(ALLOWED, "dummy"), **parent, **POLLUTION}
    for path in HELPERS:
        with monkeypatch.context() as patch:
            seen, expected = helper_environments(path, parent, patch, tmp_path)
            for env in seen:
                assert_environment(env, expected)


@pytest.mark.parametrize("path", HELPERS[:6])
def test_provider_helper_pass_through_is_deliberate(path, tmp_path, monkeypatch):
    parent = {"PATH": "/usr/bin:/bin", **POLLUTION}
    seen, expected = helper_environments(path, parent, monkeypatch, tmp_path, passed=("FOO_API_KEY",))
    for env in seen:
        assert_environment(env, expected)
