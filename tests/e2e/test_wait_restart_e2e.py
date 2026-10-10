"""C-15.4 against the real `subfleetd`: a background `subfleet wait` outlives a restart.

Defect D-WT1, 2026-10-10: the 2.1.11.4 install (09:06Z) and a `launchctl
kickstart -k` (09:08Z) each ended all five of the Subfleet hub's waiters with "the
daemon closed the connection without a response" while their jobs ran on. Here a
real daemon is stopped as launchd stops it (SIGTERM) or dies (SIGKILL) while a
real `subfleet wait` holds a poll, and the next daemon is started on the same
state root, as launchd's KeepAlive starts it.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from subfleet.client import Client

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("how", ["terminate", "kill"])
def test_c15_4_a_background_wait_outlives_a_daemon_restart(e2e, how):
    e2e.start(scenario="slow", delay_s=6)
    submitted = e2e.cli(*e2e.run_args("astra", "-d"))
    assert submitted.rc == 0, submitted.stderr
    job_id = submitted.stdout.strip()
    waiter = subprocess.Popen(
        [sys.executable, "-m", "subfleet.cli", "wait", job_id, "--timeout", "180"],
        cwd=REPO, env=e2e.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    try:
        # The waiter holds a poll in the old daemon (C-15.5 counts it) before the stop.
        status = Client(e2e.root)
        e2e.until(lambda: status.call("daemon.status", {}, timeout=5)["wait_hub"]["waiters"] >= 1,
                  timeout=30)
        assert e2e.job(job_id)["state"] not in {"succeeded", "failed", "cancelled", "lost"}
        old = e2e.process
        if how == "terminate":
            old.terminate()                             # what `launchctl kickstart -k` sends
            old.wait(timeout=60)
        else:
            e2e.crash()
        e2e.start()
        assert e2e.process.pid != old.pid
        out, err = waiter.communicate(timeout=180)
    finally:
        if waiter.poll() is None:
            waiter.kill()
            waiter.communicate()
    assert waiter.returncode == 0, err
    assert err.count("subfleet wait: daemon restarted; still waiting") == 1, err
    assert "without a response" not in err and "restart window" not in err
    job = e2e.job(job_id)
    assert job["state"] == "succeeded" and job["rc"] == 0
