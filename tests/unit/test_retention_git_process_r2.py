"""A Git deadline also stops wrappers and children retaining its output pipes."""
from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from subfleet import retention_archive as rarch
from subfleet import retention_git as rgit
from subfleet import retention_qos as rqos
from subfleet.retention_delete_guard import ISOLATED_CONFIG


@pytest.fixture
def r1_shell_shim(monkeypatch, tmp_path):
    shimdir = tmp_path / "bin"
    shimdir.mkdir()
    shim = shimdir / "git"
    # Keep this literal: replacing the shell with a Python sleep hides the bug.
    shim.write_text('#!/bin/sh\nfor a in "$@"; do [ "$a" = "for-each-ref" ] && sleep 3; done\n'
                    'exec /opt/homebrew/bin/git "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(shimdir) + os.pathsep + os.environ["PATH"])


def test_literal_r1_shell_shim_respects_listing_deadline(tmp_path, r1_shell_shim):
    """The review's literal shim sleeps in a child before it execs real Git."""
    started = time.monotonic()
    ctx = rarch.Context(tmp_path, SimpleNamespace(), deadline=started + 0.5)
    with pytest.raises(rarch.Defer, match="repository discovery deadline"):
        ctx.salvage_refs(tmp_path / ".git")
    assert time.monotonic() - started < 1.5
    assert ctx.salvage_listings == {}


def test_cancel_kills_the_literal_shell_shims_child(r1_shell_shim):
    cancel = threading.Event()
    cancel.set()
    started = time.monotonic()
    with pytest.raises(rgit.Cancelled):
        rgit.run(["for-each-ref"], timeout=60, cancel=cancel)
    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize("clamped", [False, True])
def test_qos_wrapper_execs_in_the_new_session(monkeypatch, tmp_path, clamped):
    """The QoS wrapper preserves the Git PID, and Git leads its own group."""
    if clamped and not os.access(rqos.TASKPOLICY, os.X_OK):
        pytest.skip("needs taskpolicy(8)")
    monkeypatch.setenv(rqos.QOS_ENV, "utility" if clamped else "inherit")
    shimdir = tmp_path / "bin"
    shimdir.mkdir()
    shim = shimdir / "git"
    shim.write_text(f"#!{sys.executable}\nimport json, os\n"
                    "print(json.dumps([os.getpid(), os.getpgrp(), os.getsid(0)]))\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(shimdir) + os.pathsep + os.environ["PATH"])
    spawned = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process.pid)
        return process

    monkeypatch.setattr(rgit.subprocess, "Popen", popen)
    result = rgit.run(["--version"])
    assert json.loads(result.stdout) == [spawned[0]] * 3


@pytest.mark.parametrize("setting", ["gc.auto=1", "Core.HooksPath=/elsewhere", "core.fsmonitor"])
def test_per_command_config_cannot_override_git_hardening(monkeypatch, setting):
    def unexpected_popen(*args, **kwargs):
        pytest.fail("Git started with an override of a hardening setting")

    monkeypatch.setattr(rgit.subprocess, "Popen", unexpected_popen)
    with pytest.raises(ValueError):
        rgit.run(["config", "--list"], config=(setting,))


@pytest.mark.parametrize("setting", ["pack.useSparse=false", "PACK.USESPARSE=false"])
def test_a_command_guard_admits_only_the_non_sparse_pack_setting(monkeypatch, tmp_path, setting):
    """C-8.4: under a command guard with the delete path's isolated config, the
    allowlisted pair `pack.useSparse=false` (its key in any case, as Git reads
    keys) reaches Git and the guard's check sees the command. A key outside
    the allowlist is refused before Git starts."""
    git_dir = tmp_path / "repo.git"
    rgit.run(["init", "--quiet", "--bare", "--template=", str(git_dir)], cwd=tmp_path)
    launches = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        launches.append(args[0])
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(rgit.subprocess, "Popen", popen)
    checked = []
    with rgit.command_guard(lambda args, git_dir, cwd: checked.append((args, git_dir)), env=ISOLATED_CONFIG):
        result = rgit.run(["config", "--get", "pack.useSparse"], git_dir=git_dir, config=(setting,))
        assert result.stdout == b"false\n"
        assert checked == [(("config", "--get", "pack.useSparse"), git_dir)]
        assert len(launches) == 1
        with pytest.raises(ValueError, match="under a command guard"):
            rgit.run(["config", "--get", "pack.threads"], git_dir=git_dir, config=("pack.threads=1",))
    assert len(launches) == 1


def test_per_command_config_reaches_git_outside_a_guard(tmp_path):
    git_dir = tmp_path / "repo.git"
    rgit.run(["init", "--quiet", "--bare", "--template=", str(git_dir)], cwd=tmp_path)
    result = rgit.run(["config", "--get", "pack.useSparse"], git_dir=git_dir,
                      config=("pack.useSparse=false",))
    assert result.stdout == b"false\n"


@pytest.mark.parametrize("setting", ["gc.auto=1", "Core.HooksPath=/elsewhere", "core.fsmonitor"])
def test_a_command_guard_refuses_overrides_of_git_hardening(monkeypatch, setting):
    """C-8.4: under a command guard, a per-command pair naming a key GIT_CONFIG
    sets (in any case, or without a value) is refused before Git starts."""
    def unexpected_popen(*args, **kwargs):
        pytest.fail("Git started with an override of a hardening setting under a guard")

    monkeypatch.setattr(rgit.subprocess, "Popen", unexpected_popen)
    with rgit.command_guard(lambda args, git_dir, cwd: None, env=ISOLATED_CONFIG), pytest.raises(ValueError):
        rgit.run(["config", "--list"], config=(setting,))


@pytest.mark.parametrize("setting", [
    "pack.threads=1",
    "core.sshCommand=/usr/bin/false",
    "pack.useSparse",
    "pack.useSparse=true",
    "pack.useSparse=no",
], ids=["other-pack-key", "command-key", "non-sparse-key-without-value", "non-sparse-key-true",
        "non-sparse-key-other-false-spelling"])
def test_a_command_guard_refuses_config_outside_its_allowlist(monkeypatch, setting):
    """C-8.4: under a command guard, only the exact pair `pack.useSparse=false`
    is admitted. Another key, the bare key (Git reads it as true), `true`, and
    another spelling of false are each refused before Git starts."""
    def unexpected_popen(*args, **kwargs):
        pytest.fail("Git started with per-command config outside the guard's allowlist")

    monkeypatch.setattr(rgit.subprocess, "Popen", unexpected_popen)
    with rgit.command_guard(lambda args, git_dir, cwd: None, env=ISOLATED_CONFIG), \
            pytest.raises(ValueError, match="under a command guard"):
        rgit.run(["config", "--list"], config=(setting,))


def test_the_guard_allowlist_names_no_hardening_key():
    """C-8.4: no key a command guard admits is one GIT_CONFIG sets, so the
    allowlist can never override the hardening settings."""
    allowed = {key for key, _ in rgit._GUARDED_CONFIG}
    assert allowed == {"pack.usesparse"}
    assert allowed.isdisjoint(rgit._GIT_CONFIG_KEYS)


@pytest.mark.parametrize("args,config,cancelled,message", [
    (["for-each-ref"], (), False, "timed out"),
    (["for-each-ref"], ("pack.useSparse=false",), True, "^git for-each-ref cancelled$"),
    (["-c", "fetch.fsckObjects=true", "-c", "fetch.prune=false", "fetch"], (), True,
     "^git fetch cancelled$"),
], ids=["timeout", "cancel-with-git-dir-and-config", "cancel-with-inline-config"])
@pytest.mark.parametrize("drain_finishes", [False, True])
def test_timeout_or_cancel_kills_group_and_bounds_drain(monkeypatch, tmp_path, args, config, cancelled, message,
                                                      drain_finishes):
    """A pipe which never reaches EOF cannot turn cleanup into an unbounded wait."""
    class Process:
        pid = 123456

        def __init__(self):
            self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()
            self.timeouts = []

        def communicate(self, data=None, timeout=None):
            self.timeouts.append(timeout)
            if len(self.timeouts) == 1:
                raise subprocess.TimeoutExpired("git", timeout)
            assert timeout is not None and 0 < timeout <= 3
            if not drain_finishes:
                raise subprocess.TimeoutExpired("git", timeout)
            return b"", b""

        def poll(self):
            return -signal.SIGKILL

    process = Process()
    launches = []
    killed = []

    def popen(*args, **kwargs):
        launches.append(kwargs)
        return process

    monkeypatch.setattr(rgit.subprocess, "Popen", popen)
    monkeypatch.setattr(rgit.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    cancel = threading.Event()
    if cancelled:
        cancel.set()
    error = rgit.Cancelled if cancelled else rgit.GitError
    with pytest.raises(error, match=message):
        rgit.run(args, git_dir=tmp_path / ".git", timeout=0, cancel=cancel, config=config)
    assert launches[0]["start_new_session"] is True
    assert killed == [(process.pid, signal.SIGKILL)]
    assert len(process.timeouts) == 2
    assert [pipe.closed for pipe in (process.stdin, process.stdout, process.stderr)] == [not drain_finishes] * 3


def test_exited_group_preserves_timeout_error(monkeypatch):
    """A process exiting between the deadline and kill does not mask the timeout."""
    class Process:
        pid = 123456
        calls = 0

        def communicate(self, data=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("git", timeout)
            return b"", b""

    monkeypatch.setattr(rgit.subprocess, "Popen", lambda *args, **kwargs: Process())

    def already_exited(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(rgit.os, "killpg", already_exited)
    with pytest.raises(rgit.GitError, match="timed out"):
        rgit.run(["for-each-ref"], timeout=0)
