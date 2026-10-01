"""C-5.8a against the real `subfleetd`: a stop that cannot drain still frees the lock.

2026-09-25: pid 93697 had shut its socket and was joining pool threads parked on
a lock, holding `daemon.lock` until it was killed; clients were refused and every
replacement exited 69 (docs/reports/2026-09-25-daemon-stop-wedge.md).
"""

from __future__ import annotations

import fcntl
import json
import os
import selectors
import signal
import subprocess
import sys
import time

import pytest

from subfleet.procs import same_process

# `serial`: these assert how a real daemon ends against a grace clock. Beside
# pytest-xdist's busy workers one failed in 8 of 28 CI runs (2026-10-01), mostly
# with the daemon ended by signal 14 where the test expects exit 1; serial CI
# saw 8 to 10%.
pytestmark = pytest.mark.serial


#: Give the held worker time to drain before faulthandler ends the process.
GRACE_S = 5.0
#: Scheduling slack past the grace on a loaded machine.
SLACK_S = 10.0
#: A process's descriptors, and so its flock, close as it exits, before
#: `waitpid` can reap it: a free lock is an early unlock only if the process is
#: still alive this long after the lock was seen free. An early-unlock daemon
#: stays alive until its grace ends (GRACE_S), well past this.
EXIT_SETTLE_S = 2.0


def _wait_for_exit_with_lock(process, path, deadline, timeout_message):
    """Observe lock then liveness until exit, with no serving-time sample."""
    fd = os.open(path, os.O_RDWR)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held_while_stopping = True
            else:
                held_while_stopping = False
                fcntl.flock(fd, fcntl.LOCK_UN)
            # Check the lock before polling: the process may exit between
            # these observations, which is valid. A free lock followed by a
            # live process proves it relinquished single-writer ownership early.
            alive = process.poll() is None
            if not held_while_stopping and alive:
                # The lock is released as the process exits, a moment before it
                # can be reaped (seen on CI, Python 3.12: a clean exit read as an
                # early unlock). Only a process that outlives the settle released
                # the lock while it went on running.
                try:
                    process.wait(timeout=EXIT_SETTLE_S)
                except subprocess.TimeoutExpired:
                    pass
                alive = process.poll() is None
            assert held_while_stopping or not alive, "live stopping daemon released daemon.lock"
            if not alive:
                return
            remaining = deadline - time.monotonic()
            assert remaining > 0, timeout_message()
            try:
                process.wait(timeout=min(.02, remaining))
            except subprocess.TimeoutExpired:
                pass
    finally:
        os.close(fd)


@pytest.mark.parametrize("early_unlock", [False, True], ids=["process-exit", "early-unlock"])
def test_stop_lock_observer_detects_unlock_before_process_exit(tmp_path, early_unlock):
    """Exercise the real observation loop even where ps/sysctl is unavailable."""
    child = r'''
import fcntl, os, sys
lock = open(sys.argv[1], "w")
fcntl.flock(lock, fcntl.LOCK_EX)
if sys.argv[2] == "early":
    fcntl.flock(lock, fcntl.LOCK_UN)
print("stopping:", flush=True)
sys.stdin.buffer.read(1)
os._exit(0)
'''
    path = tmp_path / "daemon.lock"
    process = subprocess.Popen([sys.executable, "-c", child, str(path),
                                "early" if early_unlock else "exit"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=10), "owned lock child did not acknowledge stopping"
            assert process.stdout.readline() == b"stopping:\n"
        if early_unlock:
            # The child stays alive until cleanup, making the early-unlock
            # interleaving deterministic even if pytest is descheduled.
            with pytest.raises(AssertionError, match="live stopping daemon released"):
                _wait_for_exit_with_lock(process, path, time.monotonic() + 10,
                                         lambda: "owned lock child did not exit")
        else:
            process.stdin.write(b"1")
            process.stdin.flush()
            _wait_for_exit_with_lock(process, path, time.monotonic() + 10,
                                     lambda: "owned lock child did not exit")
            assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.communicate(input=b"1", timeout=10)
        process.stdin.close()
        process.stdout.close()


def test_c5_8a_a_worker_that_never_returns_cannot_keep_the_lock(e2e):
    """C-5.8a, C-5.8, C-4.2, C-15.1: SIGTERM with a worker held forever.

    The process is ended at its grace, exit 1, with the held worker's stack in
    the log. The lock is free, the guardian was not signalled, and the next
    daemon adopts the attempt and accepts it once.
    """
    release_provider = e2e.root / "release-provider"
    e2e.start(env={
        # Test-only sitecustomize instrumentation around the daemon's existing
        # crash_hook: the worker that commits `attempt.running` then parks
        # until `release-hook` exists, which this test never creates.
        "SUBFLEET_E2E_HOLD_AT": "running",
        "SUBFLEET_E2E_STOP_GRACE_S": str(GRACE_S),
        "SUBFLEET_FAKE_RELEASE_PATH": str(release_provider),
    })
    submitted = e2e.cli(*e2e.run_args("astra", "-d"))
    assert submitted.rc == 0, submitted.stderr
    job_id = submitted.stdout.strip()
    marker = e2e.root / "hook-running.json"
    e2e.until(marker.exists)
    held = json.loads(marker.read_text())
    before = e2e.attempts(job_id)[0]
    assert held == {"job_id": job_id, "attempt_id": before["attempt_id"]}
    assert before["state"] == "running"
    start = json.loads((e2e.root / "jobs" / job_id / "a1" / "start.json").read_text())
    assert same_process(start["guardian_pid"], start["boot_id"], start["proc_start"])

    old = e2e.process
    lock = json.loads((e2e.root / "daemon.lock").read_text())
    assert lock["pid"] == old.pid
    signalled = time.monotonic()
    old.send_signal(signal.SIGTERM)
    # Acknowledge the stop before observing the single-writer property. An
    # ordinary serving-time lock check says nothing about close()'s lifetime.
    e2e.until(lambda: "stopping:" in (e2e.root / "daemon.log").read_text(),
              timeout=GRACE_S + SLACK_S)
    _wait_for_exit_with_lock(old, e2e.root / "daemon.lock", signalled + GRACE_S + SLACK_S,
                             lambda: "C-5.8a: the stopping daemon still holds daemon.lock "
                             f"{GRACE_S + SLACK_S:g} s after SIGTERM\n{e2e.log_text()}")
    elapsed = time.monotonic() - signalled

    # Bounded, and not early: the drain had its whole grace.
    assert GRACE_S <= elapsed <= GRACE_S + SLACK_S, elapsed
    assert old.returncode == 1
    log = (e2e.root / "daemon.log").read_text()
    assert (f"stopping: if this process is still running in {GRACE_S:g} s, "
            "the stacks of its threads follow and it exits 1") in log
    dump = log[log.index("Timeout ("):]
    # The dump names the thread that would not return: the held worker, in
    # the harness's hold, under the daemon's boundary, and the main thread
    # joining it from close(). Thread names appear in faulthandler's headers
    # from Python 3.14.
    for frame in (" in hold\n", " in _boundary\n", " in _process_attempt\n", " in close\n"):
        assert frame in dump, (frame, dump)
    if sys.version_info >= (3, 14):
        assert "[subfleet-io_" in dump, dump

    # The kernel released the flock with the process: nothing else holds it.
    fd = os.open(e2e.root / "daemon.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    # The guardian and its provider were not signalled.
    assert same_process(start["guardian_pid"], start["boot_id"], start["proc_start"])
    assert not (e2e.root / "jobs" / job_id / "a1" / "exit.json").exists()

    # A fresh daemon starts on the freed lock and adopts the running attempt.
    e2e.start()
    assert e2e.process.pid != old.pid
    release_provider.touch()
    waited = e2e.cli("wait", job_id, "--timeout", "30", timeout=45)
    assert waited.rc == 0, waited.stderr
    job = e2e.job(job_id)
    assert job["state"] == "succeeded" and job["rc"] == 0
    [accepted] = e2e.attempts(job_id)
    assert accepted["attempt_id"] == before["attempt_id"] == job["accepted_attempt_id"]
    assert accepted["guardian_pid"] == start["guardian_pid"]
    assert accepted["state"] == "succeeded" and accepted["outcome_class"] == "ok"
    assert len(e2e.rows("SELECT * FROM notices WHERE job_id=?", (job_id,))) == 1
