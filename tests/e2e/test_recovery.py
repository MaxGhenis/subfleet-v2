"""Real CLI submissions remain daemon-owned across persisted crash boundaries."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import time

from subfleet.procs import same_process


def test_running_job_survives_daemon_sigkill_once(e2e):
    """C-4.2, C-4.3, C-5.1, C-15.1: re-adopt a running guardian and accept once."""
    e2e.start(scenario="slow", delay_s=3)
    submitted = e2e.cli(*e2e.run_args("astra", "-d"))
    assert submitted.rc == 0, submitted.stderr
    job_id = submitted.stdout.strip()

    def running():
        attempts = e2e.attempts(job_id)
        return attempts[0] if attempts and attempts[0]["state"] == "running" else None

    before = e2e.until(running)
    attempt_dir = e2e.root / "jobs" / job_id / "a1"
    start = json.loads((attempt_dir / "start.json").read_text())
    assert not (attempt_dir / "exit.json").exists()
    assert same_process(start["guardian_pid"], start["boot_id"], start["proc_start"])
    assert e2e.process is not None
    old_daemon = e2e.process
    e2e.crash()
    assert old_daemon.returncode == -signal.SIGKILL
    assert same_process(start["guardian_pid"], start["boot_id"], start["proc_start"])

    e2e.start()
    assert e2e.process.pid != old_daemon.pid
    waited = e2e.cli("wait", job_id, "--timeout", "10")
    assert waited.rc == 0, waited.stderr
    job = e2e.job(job_id)
    assert job["state"] == "succeeded" and job["rc"] == 0
    attempts = e2e.attempts(job_id)
    assert len(attempts) == 1
    accepted = attempts[0]
    assert accepted["attempt_id"] == before["attempt_id"] == job["accepted_attempt_id"]
    assert accepted["guardian_pid"] == start["guardian_pid"]
    assert accepted["state"] == "succeeded"
    assert accepted["outcome_class"] == "ok"
    assert len(e2e.rows("SELECT * FROM notices WHERE job_id=?", (job_id,))) == 1
    deliverables = e2e.rows(
        "SELECT * FROM artifacts WHERE attempt_id=? AND role='deliverable'",
        (accepted["attempt_id"],),
    )
    assert len(deliverables) == 1
    shown = e2e.cli("runs", "show", job_id, "--out")
    assert shown.rc == 0, shown.stderr
    assert shown.stdout.strip() == Path(deliverables[0]["path"]).read_text().strip()


def test_starting_without_receipt_waits_contains_and_retries(e2e):
    """C-4.2, C-5.5, C-15.1, C-20.3: empty starting recovery retries after 10 s."""
    # These environment keys activate test-only sitecustomize instrumentation
    # around the daemon's existing crash_hook and guardian_start_delay_s hooks.
    # The guardian has not spawned the provider when this boundary is held.
    e2e.start(scenario="slow", env={
        "SUBFLEET_E2E_HOLD_AT": "starting",
        "SUBFLEET_E2E_START_DELAY_S": "30",
    })
    submitted = e2e.cli(*e2e.run_args("astra", "-d"))
    assert submitted.rc == 0, submitted.stderr
    job_id = submitted.stdout.strip()
    marker_path = e2e.root / "hook-starting.json"
    e2e.until(marker_path.exists)
    marker = json.loads(marker_path.read_text())
    before = e2e.attempts(job_id)[0]
    assert marker["job_id"] == job_id
    assert marker["attempt_id"] == before["attempt_id"]
    assert before["state"] == "starting"
    attempt_dir = e2e.root / "jobs" / job_id / "a1"
    assert not (attempt_dir / "start.json").exists()
    assert same_process(before["guardian_pid"], before["boot_id"], before["proc_start"])
    os.kill(before["guardian_pid"], signal.SIGKILL)
    e2e.until(lambda: not same_process(
        before["guardian_pid"], before["boot_id"], before["proc_start"],
    ))
    e2e.crash()
    assert e2e.process.returncode == -signal.SIGKILL
    assert not (attempt_dir / "start.json").exists()
    assert not (attempt_dir / "exit.json").exists()

    restarted_at = time.monotonic()
    e2e.start()
    waited = e2e.cli("wait", job_id, "--timeout", "15")
    assert waited.rc == 0, waited.stderr
    assert time.monotonic() - restarted_at >= 10
    job = e2e.job(job_id)
    assert job["state"] == "succeeded" and job["rc"] == 0
    attempts = e2e.attempts(job_id)
    assert len(attempts) == 2
    abandoned, accepted = attempts
    assert abandoned["attempt_id"] == before["attempt_id"]
    assert abandoned["state"] == "failed"
    assert abandoned["outcome_class"] == "unknown"
    assert abandoned["outcome_detail"] == "starting-no-receipt"
    assert abandoned["rc"] is None
    assert accepted["state"] == "succeeded"
    assert accepted["attempt_id"] == job["accepted_attempt_id"]
    assert not e2e.rows("SELECT * FROM leases WHERE holder=?", (abandoned["attempt_id"],))
    assert len(e2e.rows("SELECT * FROM notices WHERE job_id=?", (job_id,))) == 1
