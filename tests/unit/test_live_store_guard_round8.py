"""D-ST1 r8: folded names, reset wrapper PATHs, HOME coverage and trap scope.

Synthetic audit events never start a program. Executed probes use recorders;
filesystem probes use temporary roots only.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from subfleet import credentials
from subfleet.contracts import Credential
from tests import conftest, keychain_standin, live_store_trap
from tests.keychain_standin import STANDIN_BLOB, StandIn

FAMILY = ("sh", "bash", "zsh", "dash", "env", "xargs", "nice", "nohup", "taskpolicy")


@pytest.mark.parametrize("name", ["security", "Security", "SECURITY", "ſecurity"])
@pytest.mark.parametrize("event", ["subprocess.Popen", "os.posix_spawn", "os.exec", "os.spawn"])
def test_safety_net_refuses_folded_bare_names_before_path_race(tmp_path, name, event):
    """The independent net must recognize bare names even without a PATH hit."""
    env = {"PATH": str(tmp_path)}
    details = (name, [name], None, env) if event == "subprocess.Popen" else (name, [name], env)
    if event == "os.spawn":
        details = (os.P_WAIT, *details)
    candidates = keychain_standin._candidates(event, details)
    assert any(keychain_standin._is_real(target, None) for target in candidates)


@pytest.mark.parametrize("family", FAMILY)
@pytest.mark.parametrize("path", ["/usr/bin:/bin", None, "/bin:/usr/bin", "/usr/sbin:/sbin"])
def test_wrapper_with_reset_path_is_refused(family, path):
    """Every family member must be stopped before a system PATH can delegate."""
    env = {} if path is None else {"PATH": path}
    program = "/usr/bin/" + family
    with pytest.raises(credentials.KeychainExecRefused, match="refusing personal keychain"):
        sys.audit("subprocess.Popen", program, [program, "fixture-ref"], None, env)


@pytest.mark.parametrize("event", ["subprocess.Popen", "os.posix_spawn", "os.exec", "os.spawn"])
@pytest.mark.parametrize("reset", ["-i", "--ignore-environment", "PATH=/usr/bin:/bin", "-u PATH"])
def test_env_arguments_cannot_discard_recorder_path(event, reset):
    """env's own arguments can reset PATH even with a safe launch environment."""
    argv = ["/usr/bin/env", *reset.split(), "fixture-ref"]
    env = dict(os.environ)
    details = (argv[0], argv, None, env) if event == "subprocess.Popen" else (argv[0], argv, env)
    if event == "os.spawn":
        details = (os.P_WAIT, *details)
    with pytest.raises(credentials.KeychainExecRefused, match="refusing personal keychain"):
        sys.audit(event, *details)


def test_env_can_explicitly_restore_recorder_after_clear():
    argv = ["/usr/bin/env", "-i", "PATH=" + os.environ["PATH"], "/bin/sh", "-c", "true"]
    sys.audit("subprocess.Popen", argv[0], argv, None, {})


def test_wrapper_path_comparison_never_probes_a_system_directory(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("wrapper containment must compare strings before path lookup")
    with monkeypatch.context() as patch:
        patch.setattr(os.path, "isfile", unexpected)
        with pytest.raises(credentials.KeychainExecRefused):
            sys.audit("subprocess.Popen", "/bin/sh", ["/bin/sh", "-c", "true"], None,
                      {"PATH": "/usr/bin:/bin"})


def test_credential_shell_runner_cannot_escape_to_emulated_system_path(tmp_path):
    """Even a wrapping runner cannot return the emulated system reader's blob."""
    system = StandIn(tmp_path / "emulated-system")
    recorder = StandIn(tmp_path / "recorder")
    # Bind an isolated guard to synthetic system/recorder directories. No real
    # system binary or globally installed hook is replaced by this probe.
    guard = credentials._exec_guard([], system_dirs=(str(system.path.parent),),
                                    recorder_dirs=(str(recorder.path.parent),))
    def runner(argv, **kwargs):
        command = ["/bin/sh", "-c", 'security "$@"', "fixture", *argv[1:]]
        env = {"PATH": str(system.path.parent)}
        guard("subprocess.Popen", (command[0], command, None, env))
        return subprocess.run(command, env=env, **kwargs)
    with pytest.raises(credentials.KeychainExecRefused):
        credentials.resolve_credential(Credential("claude", "fixture-ref", "keychain-token"), runner=runner)
    assert system.count() == recorder.count() == 0


@pytest.mark.parametrize("explicit", [False, True])
def test_session_carries_original_uv_cache_before_changing_home(tmp_path, monkeypatch, explicit):
    original = tmp_path / "original-home"
    cache = tmp_path / "explicit-cache" if explicit else original / ".cache/uv"
    monkeypatch.setenv("HOME", str(original))
    if explicit:
        monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    else:
        monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    config = SimpleNamespace()
    try:
        conftest.pytest_configure(config)
        assert os.environ["UV_CACHE_DIR"] == str(cache)
        assert os.environ["HOME"] != str(original)
    finally:
        conftest.pytest_unconfigure(config)
    assert os.environ.get("UV_CACHE_DIR") == (str(cache) if explicit else None)


@pytest.mark.parametrize("operation", ["firmlink", "FileIO", "chdir"])
def test_trap_stops_round8_access_before_underlying_api(tmp_path, monkeypatch, operation):
    root = tmp_path / "live"
    root.mkdir()
    (root / "data").write_text("fixture")
    firmlink = "/System/Volumes/Data" + str(root.resolve())
    module, name = (io, "FileIO") if operation == "FileIO" else (os, "chdir" if operation == "chdir" else "stat")
    seen = []
    original = getattr(module, name)
    def witness(*args, **kwargs):
        if os.fsdecode(args[0]).startswith((str(root), firmlink)):
            seen.append(args)
            raise AssertionError("underlying API reached")
        return original(*args, **kwargs)
    monkeypatch.setattr(module, name, witness)
    with live_store_trap.untouchable(monkeypatch, root) as touched:
        with pytest.raises(pytest.fail.Exception, match="touched"):
            if operation == "firmlink": os.stat(firmlink + "/data")
            elif operation == "FileIO": io.FileIO(root / "data")
            else: os.chdir(root)
    assert len(touched) == 1 and seen == []


def test_acceptance_contract_names_d_st1_boundary_and_trap_exclusions():
    """D-ST1's acceptance text must own the harness threat model."""
    contract = (Path(__file__).parents[2] / "docs/acceptance-contract.md").read_text()
    clause = next((line for line in contract.splitlines() if line.startswith("- **C-20.6**")), "")
    for phrase in ("D-ST1", "production code", "fixtures", "subprocess", "os", "file APIs",
                   "absolute path", "shell", "DirEntry.stat", "sqlite3", "isolated HOME"):
        assert phrase in clause, phrase
    for phrase in ("DirEntry.stat", "sqlite3"):
        assert phrase in live_store_trap.__doc__
