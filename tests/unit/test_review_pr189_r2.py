"""Round-2 evidence: timeout cleanup must cover a child that changes groups."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from tests.frontend import swift


def test_compiler_timeout_does_not_leave_a_child_in_its_own_group(tmp_path, monkeypatch):
    ready = tmp_path / "escaped.json"
    child_code = (
        "import json, os, pathlib, time; os.setpgid(0, 0); "
        f"pathlib.Path({str(ready)!r}).write_text(json.dumps(dict("
        "pid=os.getpid(), ppid=os.getppid(), pgid=os.getpgrp()))); time.sleep(60)"
    )
    wrapper_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); time.sleep(60)"
    )
    real_popen = subprocess.Popen
    owned = []

    def start_owned(command, **kwargs):
        process = real_popen(command, **kwargs)
        owned.append(process)
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "the task-owned child never started"
        return process

    try:
        monkeypatch.setattr(subprocess, "Popen", start_owned)
        with pytest.raises(subprocess.TimeoutExpired):
            swift.run_swift([sys.executable, "-c", wrapper_code], timeout=0.05)
        escaped = json.loads(ready.read_text())
        assert escaped["ppid"] == owned[0].pid
        assert escaped["pid"] == escaped["pgid"] != owned[0].pid
        assert owned[0].returncode == -signal.SIGKILL
        try:
            os.kill(escaped["pid"], 0)
        except ProcessLookupError:
            surviving = False
        else:
            surviving = True
        assert not surviving, f"run_swift left its task-owned child alive: {escaped}"
    finally:
        # Clean only PIDs created by this probe, including the escaped child.
        if ready.exists():
            pid = json.loads(ready.read_text())["pid"]
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in owned:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)


def _gone_within(pid, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.01)
    return False


def test_a_hung_scan_cannot_hang_cleanup(monkeypatch):
    """Review r3 delta: a `ps` that never finishes is killed and skipped, within bounds."""
    started = []

    def slow_ps(argv, **kwargs):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(swift, "_POPEN", slow_ps)
    monkeypatch.setattr(swift, "SCAN_TIMEOUT_S", 0.5)
    began = time.monotonic()
    try:
        assert swift.marked_pids("0" * 32) == []
        assert time.monotonic() - began < 5
        assert started and started[0].poll() is not None      # the scan was killed and reaped
    finally:
        for process in started:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


def test_a_normal_exit_still_kills_the_build_group(tmp_path):
    """Review r3 delta: a child that dropped the marker but stayed in the build's
    group does not outlive a compiler that exited by itself."""
    ready = tmp_path / "child.pid"
    child = f"import os, pathlib, time; pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(30)"
    parent = ("import subprocess, sys, time, pathlib; "
              f"subprocess.Popen([sys.executable, '-c', {child!r}], env={{}}, "
              "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
              f"p = pathlib.Path({str(ready)!r})\n"
              "deadline = time.monotonic() + 20\n"
              "while not p.exists() and time.monotonic() < deadline: time.sleep(0.01)")
    pid = None
    try:
        result = swift.run_swift([sys.executable, "-c", parent], timeout=30)
        assert result.returncode == 0
        pid = int(ready.read_text())
        assert _gone_within(pid, 5), f"run_swift left its group's child {pid} alive"
    finally:
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_undecodable_output_cannot_replace_the_timeout():
    """Review r4 delta: a child that writes b'\\xff' and hangs still ends in TimeoutExpired."""
    code = "import sys, time; sys.stdout.buffer.write(b'\\xff'); sys.stdout.flush(); time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired):
        swift.run_swift([sys.executable, "-c", code], timeout=1)


def test_a_failing_group_kill_cannot_replace_the_timeout(monkeypatch):
    """Review r4 delta: an OSError from killpg must not skip the scan and drain or
    replace the timeout; the marker sweep still ends the build."""
    import errno

    def broken_killpg(group, sig):
        raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(swift.os, "killpg", broken_killpg)
    began = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        swift.run_swift([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    assert time.monotonic() - began < 20


def test_an_interrupt_survives_a_failing_drain(monkeypatch):
    """Review r4 delta: a KeyboardInterrupt during the build is what propagates,
    even when the cleanup's drain itself fails."""
    real_popen = subprocess.Popen

    class Interrupted(real_popen):
        calls = 0

        def communicate(self, *args, **kwargs):
            Interrupted.calls += 1
            if Interrupted.calls == 1:
                raise KeyboardInterrupt
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "injected")

    monkeypatch.setattr(subprocess, "Popen", Interrupted)
    with pytest.raises(KeyboardInterrupt):
        swift.run_swift([sys.executable, "-c", "import time; time.sleep(30)"], timeout=30)
