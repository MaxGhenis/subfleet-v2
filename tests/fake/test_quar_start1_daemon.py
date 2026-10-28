"""D-QUAR-START1 (a) through a real daemon, guardian and provider (`tests/fake_adapter.py`).

2026-10-26, `20261026-104634-ce1-review-r3/a1`: under load 60 to 100 the guardian had
not written `start.json` when the 10 s start grace ran out. release/217 quarantined
it, the job ended `lost` and its caller ran it again, and 21 s after the job ended the
guardian launched its provider, which ran 13 minutes for output nobody could accept.

The harness's hidden `--start-delay` makes the guardian sleep, alive and running,
before its `start.json`, as Python start-up and its identity reads did under load.

1. A slow start within `START_GRACE_MAX_S` is not lost: the job succeeds on its one
   attempt, with no retry and nothing left running.
2. A start past the bound is KILLed at the bound, not at the grace, and ends as an
   unlaunched attempt (decision 1), its abandon written.

Every process a test starts is stopped by its recorded identity, on failure too.
"""

from __future__ import annotations

import json
from datetime import datetime
import sys
import time

import pytest

from subfleet import procs
from tests.fake.test_kill4_terminal_daemon import alive, start

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="C-5.5's census reads macOS process state")


def seconds(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def guardian_of(row: dict) -> procs.ProcessIdentity:
    return procs.ProcessIdentity(row["guardian_pid"], row["boot_id"], row["proc_start"])


def end_all(daemon, job: str) -> None:
    for row in daemon.attempts(job):
        if row["guardian_pid"] and alive(guardian_of(row)):
            procs.signal_process(guardian_of(row), 9)


def test_a_guardian_slower_than_start_grace_is_given_time_and_its_job_succeeds_on_one_attempt(daemon):
    """D-QUAR-START1's shape: 20 s before `start.json`, a 10 s start grace."""
    start(daemon, "--start-delay", "20", "--start-grace-s", "10", isolated_group=True)
    job = daemon.submit("ok", max_attempts=2)
    try:
        first = daemon.until(lambda: next((a for a in daemon.attempts(job) if a["guardian_pid"]), None), timeout=60)
        guardian = guardian_of(first)
        final = daemon.until(lambda: daemon.job(job) if daemon.job(job)["state"] in
                             ("succeeded", "failed", "lost", "cancelled") else None, timeout=180)
        attempts = daemon.attempts(job)
        assert final["state"] == "succeeded" and final["accepted_attempt_id"] == first["attempt_id"], (final, attempts)
        assert len(attempts) == 1, attempts                                  # no retry
        a1 = attempts[0]
        adir = daemon.root / "jobs" / job / "a1"
        assert not (adir / "abandon.json").exists()
        start_receipt = json.loads((adir / "start.json").read_text())
        assert seconds(start_receipt["started_at"]) - seconds(a1["started_at"]) >= 10, (start_receipt, a1)
        events = [row["kind"] for row in daemon.rows("SELECT kind FROM events WHERE attempt_id=?",
                                                      (a1["attempt_id"],))]
        assert "attempt.start_extended" in events and "attempt.kill_started" not in events, events
        daemon.until(lambda: not alive(guardian), timeout=30)                # nothing left running
    finally:
        end_all(daemon, job)


def test_a_guardian_slower_than_the_bound_is_killed_at_the_bound_and_ends_unlaunched(daemon):
    """Past `START_GRACE_MAX_S` (8 s here, with a 2 s grace), the existing branch runs
    unchanged: the group KILL, then decision 1's end."""
    start(daemon, "--start-delay", "60", "--start-grace-s", "2", "--start-grace-max-s", "8", isolated_group=True)
    job = daemon.submit("ok", max_attempts=1)
    try:
        first = daemon.until(lambda: next((a for a in daemon.attempts(job) if a["guardian_pid"]), None), timeout=60)
        guardian = guardian_of(first)
        began = seconds(first["started_at"])
        time.sleep(max(0.0, began + 5 - time.time()))
        assert alive(guardian), "killed at the grace, before the bound"
        assert daemon.attempts(job)[0]["state"] == "starting"
        a1 = daemon.until(lambda: daemon.attempts(job)[0] if daemon.attempts(job)[0]["state"]
                          not in ("reserved", "starting", "running") else None, timeout=60)
        assert (a1["state"], a1["outcome_detail"]) == ("failed", "starting-no-receipt"), a1
        assert daemon.job(job)["state"] == "failed"
        assert not alive(guardian)
        signal_at = daemon.rows("SELECT ts FROM events WHERE attempt_id=? AND kind='attempt.kill_started'",
                                (a1["attempt_id"],))[0]["ts"]
        assert seconds(signal_at) - began >= 8 - .5, (signal_at, first["started_at"])
        adir = daemon.root / "jobs" / job / "a1"
        assert json.loads((adir / "abandon.json").read_text())["reason"] == "starting-no-receipt"
        assert not (adir / "start.json").exists()
        last = json.loads(a1["evidence_json"])["terminal_containment"][-1]
        assert last["contained"] is True and last["group_signals"] == [9], last
    finally:
        end_all(daemon, job)
