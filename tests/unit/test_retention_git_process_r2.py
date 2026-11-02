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


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("drain_finishes", [False, True])
def test_timeout_or_cancel_kills_group_and_bounds_drain(monkeypatch, cancelled, drain_finishes):
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
    with pytest.raises(error, match="cancelled" if cancelled else "timed out"):
        rgit.run(["for-each-ref"], timeout=0, cancel=cancel)
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
