"""D-ST1 r7: wrapper containment, immutable bare-name refusal, and trap coverage.

Every executable probe has a recording stand-in on PATH. Deliberately spelling
the native reader inside a shell string is outside the accidental-reach contract.
Every filesystem probe uses a synthetic live root, never the person's store.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import credentials
from subfleet.contracts import Credential
from tests import conftest
from tests.keychain_standin import STANDIN_BLOB, StandIn, standin, treat_as_keychain_binary
from tests.live_store_trap import untouchable


@pytest.mark.parametrize("wrapper", ["shell", "shell-true", "env", "credential-shell"])
def test_session_path_contains_bare_name_wrapper_launches(tmp_path, monkeypatch, wrapper):
    """The outer recorder contains the old baseline; the session must prepend its own."""
    outer = StandIn(tmp_path / "outer")
    monkeypatch.setenv("PATH", outer.search_path())
    config = SimpleNamespace()
    try:
        conftest.pytest_configure(config)
        if wrapper in ("shell", "shell-true"):
            command = ["/bin/sh", "-c", "security fixture-ref"] if wrapper == "shell" else "security fixture-ref"
            result = subprocess.run(command, shell=wrapper == "shell-true",
                                    capture_output=True, text=True, check=True)
            assert result.stdout == STANDIN_BLOB
        elif wrapper == "env":
            result = subprocess.run(["/usr/bin/env", "security", "fixture-ref"],
                                    capture_output=True, text=True, check=True)
            assert result.stdout == STANDIN_BLOB
        else:
            def runner(argv, **kwargs):
                return subprocess.run(["/bin/sh", "-c", 'security "$@"', "fixture",
                                       *argv[1:]], **kwargs)
            result = credentials.resolve_credential(
                Credential("claude", "fixture-ref", "keychain-token"), runner=runner)
            assert result == {"CLAUDE_CODE_OAUTH_TOKEN": STANDIN_BLOB}
        assert outer.count() == 0
        assert config._subfleet_keychain_standin.count() == 1
    finally:
        conftest.pytest_unconfigure(config)


def test_session_home_and_path_survive_fixture_undo_and_restore(tmp_path, monkeypatch):
    original_home = str(tmp_path / "original-home")
    original_path = os.environ["PATH"]
    monkeypatch.setenv("HOME", original_home)
    config = SimpleNamespace()
    try:
        conftest.pytest_configure(config)
        baseline_home, baseline_path = os.environ["HOME"], os.environ["PATH"]
        assert baseline_home == str(config._subfleet_safe_home_base / "home")
        assert Path(baseline_path.split(os.pathsep)[0]) == config._subfleet_keychain_standin.path.parent
        inner = pytest.MonkeyPatch()
        inner.setenv("HOME", str(tmp_path / "other"))
        inner.setenv("PATH", "/nonexistent-sf-r7")
        inner.undo()
        assert (os.environ["HOME"], os.environ["PATH"]) == (baseline_home, baseline_path)
    finally:
        conftest.pytest_unconfigure(config)
    assert (os.environ["HOME"], os.environ["PATH"]) == (original_home, original_path)


@pytest.mark.parametrize("event", ["subprocess.Popen", "os.posix_spawn", "os.exec", "os.spawn"])
@pytest.mark.parametrize("search", ["standin", "missing"])
@pytest.mark.parametrize("name", ["security", "Security", "SECURITY", "ſecurity"])
def test_bare_security_is_refused_independently_of_path(standin, event, search, name):
    env = {"PATH": str(standin.path.parent) if search == "standin" else "/nonexistent-sf-r7"}
    details = {
        "subprocess.Popen": (name, [name], None, env),
        "os.posix_spawn": (name, [name], env),
        "os.exec": (name, [name], env),
        "os.spawn": (os.P_WAIT, name, [name], env),
    }
    with pytest.raises(credentials.KeychainExecRefused, match="refusing personal keychain"):
        sys.audit(event, *details[event])
    assert standin.count() == 0


@pytest.mark.parametrize("name", ["security", "Security", "SECURITY", "ſecurity"])
def test_a_later_audit_hook_cannot_change_path_after_bare_name_approval(standin, tmp_path, monkeypatch, name):
    guarded = StandIn(tmp_path / "guarded")
    treat_as_keychain_binary(monkeypatch, guarded)
    empty = tmp_path / "empty"
    empty.mkdir()
    env = {**os.environ, "PATH": str(empty)}
    active = [True]

    def change_path(event, details):
        if active[0] and event == "subprocess.Popen" and details[3] is env:
            env["PATH"] = str(guarded.path.parent)

    sys.addaudithook(change_path)
    try:
        with pytest.raises(credentials.KeychainExecRefused, match="refusing personal keychain"):
            subprocess.run([name, "fixture-ref"], env=env, capture_output=True, check=True)
    finally:
        active[0] = False
    assert guarded.count() == standin.count() == 0


def test_an_intentional_standin_uses_an_explicit_path(standin):
    result = subprocess.run([str(standin.path), "fixture-ref"], capture_output=True,
                            text=True, check=True)
    assert result.stdout == STANDIN_BLOB and standin.count() == 1


@pytest.mark.parametrize("operation", ["read_text", "rename-destination", "symlink-alias", "dir-fd"])
def test_trap_refuses_each_review_input_before_the_file_api(tmp_path, monkeypatch, operation):
    root = tmp_path / "live"
    root.mkdir()
    (root / "data").write_text("private")
    outside = tmp_path / "outside"
    outside.write_text("outside")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    fd = os.open(root, os.O_RDONLY)
    seen = []
    # The witness wraps the underlying API before the trap wraps it.
    import io
    name = "open" if operation == "read_text" else "rename" if operation == "rename-destination" else "stat"
    module = io if operation == "read_text" else os
    original = getattr(module, name)

    def witness(*args, **kwargs):
        seen.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, witness)
    try:
        with untouchable(monkeypatch, root) as touched:
            with pytest.raises(pytest.fail.Exception, match="touched"):
                if operation == "read_text":
                    (root / "data").read_text()
                elif operation == "rename-destination":
                    os.rename(outside, root / "new")
                elif operation == "symlink-alias":
                    os.stat(alias / "data")
                else:
                    os.stat("data", dir_fd=fd)
        assert len(touched) == 1
        assert seen == []
    finally:
        os.close(fd)


@pytest.mark.parametrize("operation", ["stat", "read_text", "rename-destination", "dir-fd"])
def test_trap_refuses_before_first_stat_of_a_symlink_target(tmp_path, monkeypatch, operation):
    root = tmp_path / "live"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    outside = tmp_path / "outside"
    outside.touch()
    fd = os.open(tmp_path, os.O_RDONLY)
    first = []
    for name in ("stat", "lstat", "readlink"):
        original = getattr(os, name)
        def witness(path, *args, _original=original, **kwargs):
            if not isinstance(path, int) and os.fsdecode(path).startswith(str(root)):
                first.append(path)
            return _original(path, *args, **kwargs)
        monkeypatch.setattr(os, name, witness)
    try:
        with untouchable(monkeypatch, root):
            with pytest.raises(pytest.fail.Exception, match="touched"):
                if operation == "stat":
                    os.stat(alias / "missing")
                elif operation == "read_text":
                    (alias / "missing").read_text()
                elif operation == "rename-destination":
                    os.rename(outside, alias / "missing")
                else:
                    os.stat("alias/missing", dir_fd=fd)
        assert first == []
    finally:
        os.close(fd)
