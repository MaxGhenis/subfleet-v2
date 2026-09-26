"""C-5.8a against the real `subfleetd`: a stop that cannot drain still frees the lock.

2026-09-25: pid 93697 had shut its socket and was joining pool threads parked on
a lock, holding `daemon.lock` until it was killed; clients were refused and every
replacement exited 69 (docs/reports/2026-09-25-daemon-stop-wedge.md).
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time

from subfleet.procs import same_process


GRACE_S = 2.0
#: Scheduling slack past the grace on a loaded machine.
SLACK_S = 8.0


def test_c5_8a_a_worker_that_never_returns_cannot_keep_the_lock(e2e):
    """C-5.8a, C-5.8, C-4.2, C-15.1: SIGTERM with a worker held forever.

    The process is ended at its grace, exit 1, with the held worker's stack in
    the log. The lock is free, the guardian was not signalled, and the next
    daemon adopts the attempt and accepts it once.
    """
    e2e.start(scenario="slow", delay_s=12, env={
        # Test-only sitecustomize instrumentation around the daemon's existing
        # crash_hook: the worker that commits `attempt.running` then parks
        # until `release-hook` exists, which this test never creates.
        "SUBFLEET_E2E_HOLD_AT": "running",
        "SUBFLEET_E2E_STOP_GRACE_S": str(GRACE_S),
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
    # Single writer (C-5.8): while the old daemon is still stopping, its lock
    # is still held; only the end of the process releases it.
    e2e.until(lambda: "stopping: if this process is still running in" in
              (e2e.root / "daemon.log").read_text())
    fd = os.open(e2e.root / "daemon.lock", os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held_while_stopping = True
        else:
            held_while_stopping = False
            fcntl.flock(fd, fcntl.LOCK_UN)
        alive = old.poll() is None
    finally:
        os.close(fd)
    assert alive and held_while_stopping, (alive, held_while_stopping)
    try:
        old.wait(timeout=GRACE_S + SLACK_S)
    except subprocess.TimeoutExpired:
        raise AssertionError("C-5.8a: the stopping daemon still holds daemon.lock "
                             f"{GRACE_S + SLACK_S:g} s after SIGTERM\n{e2e.log_text()}") from None
    elapsed = time.monotonic() - signalled

    # Bounded, and not early: the drain had its whole grace.
    assert GRACE_S <= elapsed <= GRACE_S + SLACK_S, elapsed
    assert old.returncode == 1
    log = (e2e.root / "daemon.log").read_text()
    assert (f"stopping: if this process is still running in {GRACE_S:g} s, "
            "every thread's stack follows and it exits 1") in log
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
    waited = e2e.cli("wait", job_id, "--timeout", "30")
    assert waited.rc == 0, waited.stderr
    job = e2e.job(job_id)
    assert job["state"] == "succeeded" and job["rc"] == 0
    [accepted] = e2e.attempts(job_id)
    assert accepted["attempt_id"] == before["attempt_id"] == job["accepted_attempt_id"]
    assert accepted["guardian_pid"] == start["guardian_pid"]
    assert accepted["state"] == "succeeded" and accepted["outcome_class"] == "ok"
    assert len(e2e.rows("SELECT * FROM notices WHERE job_id=?", (job_id,))) == 1
