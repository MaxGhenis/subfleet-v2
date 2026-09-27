"""C-5.1 through a real daemon: every provider it launches runs at the `utility` QoS (2026-09-27).

The daemon here is a subprocess of the test, so it runs at the test's own scheduling; the
guardian keeps that, and the provider it starts, with everything the provider starts, is
clamped to `utility` (priority 20 or lower). The fake provider's `qos` scenario reports
`ps -M`'s priorities for itself, a child, and its guardian.
"""

import json
import os
import signal
import subprocess
import sys

import pytest

from subfleet import guardian
from subfleet.contracts import attempt_dir

UTILITY = 20
BACKGROUND = 4       # darwinbg's MAXPRI_THROTTLE

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not os.access(guardian.TASKPOLICY, os.X_OK),
                                reason="C-5.1's clamp is taskpolicy(8), which ships with macOS")


def own_priority() -> int:
    out = subprocess.run(["/bin/ps", "-o", "pri=", "-p", str(os.getpid())], capture_output=True, text=True)
    return int(out.stdout.split()[0].rstrip("TRSUIZ"))


def terminal(daemon, job_id, timeout=90):
    """A clamped provider starts slowly on a loaded machine; wait for the row, not the clock."""
    return daemon.until(lambda: (lambda job: job if job["state"] in {"succeeded", "failed", "cancelled", "lost"}
                                 else None)(daemon.job(job_id)), timeout=timeout)


def test_c5_1_a_daemon_launch_runs_its_provider_at_utility(daemon, monkeypatch):
    """C-5.1: the provider, its child and its attempt to raise itself stay at utility; the guardian does not."""
    monkeypatch.delenv(guardian.PROVIDER_QOS_ENV, raising=False)
    daemon.start()
    job_id = daemon.submit("qos")
    job = terminal(daemon, job_id)
    assert job["state"] == "succeeded", daemon.log_text()
    adir = attempt_dir(daemon.root, job_id, 1)
    report = json.loads((adir / "stdout").read_text())
    receipt = json.loads((adir / "exit.json").read_text())
    assert report["child"] == [UTILITY] * len(report["child"]), report          # idle: exactly utility's base
    assert BACKGROUND < min(report["provider"]) and max(report["provider"]) <= UTILITY, report
    assert max(report["after_raise"]) <= UTILITY, report
    assert receipt["child_pid"] == report["pid"] and report["attempt"] == f"{job_id}/a1"
    if own_priority() > UTILITY:
        assert max(report["guardian"]) > UTILITY, report


def test_c5_6_the_kill_protocol_contains_a_clamped_provider(daemon, monkeypatch):
    """C-5.5, C-5.6 under the clamp: a TERM-ignoring provider is killed through its group, and
    its leases are released, which the daemon does only on a census verified empty."""
    monkeypatch.delenv(guardian.PROVIDER_QOS_ENV, raising=False)
    daemon.start()
    job_id = daemon.submit("ignore-sigterm")
    attempt = daemon.until(lambda: (lambda rows: rows[-1] if rows and rows[-1]["state"] == "running" else None)(
        daemon.attempts(job_id)), timeout=60)
    stdout = attempt_dir(daemon.root, job_id, 1) / "stdout"
    daemon.until(lambda: stdout.exists() and "ready" in stdout.read_text(), timeout=60)
    daemon.call("kill", job_id=job_id)
    assert terminal(daemon, job_id)["state"] == "cancelled", daemon.log_text()
    killed = daemon.attempts(job_id)[0]
    assert killed["state"] == "interrupted" and killed["killed_by"]
    assert killed["signal"] == signal.SIGKILL
    assert not daemon.rows("SELECT * FROM leases WHERE holder IN (?,?)", (job_id, attempt["attempt_id"]))
