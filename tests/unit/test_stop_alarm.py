"""The process-only stop alarm does not travel into a newly started guardian."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from subfleet import procs


REPO = Path(__file__).resolve().parents[2]

LAUNCHER = r'''
import faulthandler, json, os, signal, subprocess, sys, threading
from pathlib import Path
from subfleet.daemon import watch_stop

root = Path(sys.argv[1])
# A daemon must reset an inherited ignored or blocked SIGALRM before arming.
signal.signal(signal.SIGALRM, signal.SIG_IGN)
signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
arm = watch_stop(threading.Event(), 2.0, root / "daemon.log")
assert signal.SIGALRM not in signal.pthread_sigmask(signal.SIG_BLOCK, set())
def refuse(*args, **kwargs):
    raise RuntimeError("unable to start watchdog thread")
faulthandler.dump_traceback_later = refuse
arm()
remaining, interval = signal.getitimer(signal.ITIMER_REAL)
assert remaining > 0 and interval == 0, (remaining, interval)
assert signal.getsignal(signal.SIGALRM) == signal.SIG_DFL
# Check fork itself as well as the real guardian's fork/exec path below.
read_fd, write_fd = os.pipe()
forked = os.fork()
if forked == 0:
    os.close(read_fd)
    os.write(write_fd, json.dumps(signal.getitimer(signal.ITIMER_REAL)).encode())
    os._exit(0)
os.close(write_fd)
fork_timer = json.loads(os.read(read_fd, 1024))
os.close(read_fd)
_, status = os.waitpid(forked, 0)
assert status == 0 and fork_timer == [0.0, 0.0], (status, fork_timer)
if sys.argv[2] == "fork":
    print(json.dumps({"fork_timer": fork_timer, "alarm_remaining": remaining}), flush=True)
    threading.Event().wait()
provider = """import pathlib, sys, time
root = pathlib.Path(sys.argv[1])
(root / 'provider-ready').touch()
while not (root / 'release-provider').exists():
    time.sleep(.01)
print('survived')
"""
guardian = subprocess.Popen([
    sys.executable, "-m", "subfleet.guardian",
    "--attempt-dir", str(root), "--cwd", str(root),
    "--stdout-path", str(root / "stdout"), "--stderr-path", str(root / "stderr"),
    "--", sys.executable, "-c", provider, str(root),
], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(json.dumps({"guardian_pid": guardian.pid, "alarm_remaining": remaining}), flush=True)
threading.Event().wait()
'''


def test_stop_alarm_resets_inherited_signal_state_and_is_not_inherited_by_fork(tmp_path):
    """The kernel alarm remains effective without faulthandler or Python threads."""
    result = subprocess.run([sys.executable, "-c", LAUNCHER, str(tmp_path), "fork"],
                            env={**os.environ, "PYTHONPATH": str(REPO), "PYTHON_GIL": "1"},
                            cwd=REPO, text=True, capture_output=True, timeout=15)
    assert result.returncode == -signal.SIGALRM, (result.returncode, result.stdout, result.stderr)
    observed = json.loads(result.stdout)
    assert observed["fork_timer"] == [0.0, 0.0]
    assert observed["alarm_remaining"] > 0


def test_guardian_started_after_stop_alarm_is_armed_survives(tmp_path):
    """N8: fork/exec after arming cannot transfer the daemon's interval timer."""
    try:
        procs.boot_id()
        if procs.identity(os.getpid()) is None:
            pytest.skip("guardian integration requires visible process identities")
    except procs.InspectionError as exc:
        pytest.skip(f"guardian integration requires ps/sysctl: {exc}")

    env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHON_GIL": "1",
           "SUBFLEET_JOB": "alarm-guardian", "SUBFLEET_ATTEMPT": "alarm-guardian/a1"}
    launcher = subprocess.Popen([sys.executable, "-c", LAUNCHER, str(tmp_path), "guardian"],
                                env=env, cwd=REPO, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    start = None
    try:
        stdout, stderr = launcher.communicate(timeout=15)
        assert launcher.returncode == -signal.SIGALRM, (launcher.returncode, stdout, stderr)
        launched = json.loads(stdout)
        deadline = time.monotonic() + 15
        while not (tmp_path / "provider-ready").exists():
            assert time.monotonic() < deadline, (tmp_path / "stderr").read_text()
            time.sleep(.01)
        start = json.loads((tmp_path / "start.json").read_text())
        assert start["guardian_pid"] == launched["guardian_pid"]
        assert procs.same_process(start["guardian_pid"], start["boot_id"], start["proc_start"])
        assert not (tmp_path / "exit.json").exists()
        (tmp_path / "release-provider").touch()
        while not (tmp_path / "exit.json").exists():
            assert time.monotonic() < deadline, (tmp_path / "stderr").read_text()
            time.sleep(.01)
        receipt = json.loads((tmp_path / "exit.json").read_text())
        assert receipt["rc"] == 0 and receipt["signal"] is None
        assert (tmp_path / "stdout").read_text() == "survived\n"
    finally:
        (tmp_path / "release-provider").touch()
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=5)
        if start and procs.same_process(start["guardian_pid"], start["boot_id"], start["proc_start"]):
            procs.signal_group(start["guardian_pid"], signal.SIGKILL,
                               boot_id=start["boot_id"], proc_start=start["proc_start"])
