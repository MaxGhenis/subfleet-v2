"""C-5.1 on real processes: the provider and everything it starts run at the `utility` QoS,
and the guardian keeps whatever its daemon runs at (2026-09-27).

A QoS clamp caps a thread's priority, so a clamped process runs at 20 or lower (a busy
thread decays below its base) and a process at the default QoS at 31. The fake provider's
`qos` scenario reports `ps -M`'s priorities for itself, a child it starts and its parent,
and tries to raise its own thread to user-initiated.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from subfleet import guardian, procs

REPO = Path(__file__).resolve().parents[2]
FAKEPROV = REPO / "tests" / "bin" / "fakeprov"
UTILITY = 20
BACKGROUND = 4       # darwinbg's MAXPRI_THROTTLE


@pytest.fixture(scope="module", autouse=True)
def macos_inspection():
    if sys.platform != "darwin" or not os.access(guardian.TASKPOLICY, os.X_OK):
        pytest.skip("C-5.1's clamp is taskpolicy(8), which ships with macOS")
    try:
        if procs.identity(os.getpid()) is None:
            pytest.skip("C-5.3 process identity unavailable")
    except procs.InspectionError:
        pytest.skip("host sandbox denies ps; priorities cannot be read")


def own_priority() -> int:
    rows = subprocess.run(["/bin/ps", "-o", "pri=", "-p", str(os.getpid())], capture_output=True, text=True)
    return int(rows.stdout.split()[0].rstrip("TRSUIZ"))


def run(tmp_path: Path, **env) -> tuple[dict, dict]:
    """The guardian as the daemon starts it, with the fake provider's `qos` scenario."""
    command = [sys.executable, "-m", "subfleet.guardian", "--attempt-dir", str(tmp_path),
               "--cwd", str(tmp_path), "--stdout-path", str(tmp_path / "stdout"),
               "--stderr-path", str(tmp_path / "stderr"), "--", sys.executable, str(FAKEPROV)]
    environment = {k: v for k, v in os.environ.items() if k != guardian.PROVIDER_QOS_ENV}
    environment.update(PYTHONPATH=str(REPO), SUBFLEET_FAKE_SCENARIO="qos", SUBFLEET_ATTEMPT="qos/a1",
                       SUBFLEET_JOB="qos", **env)
    done = subprocess.run(command, env=environment, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    assert done.returncode == 0, (tmp_path / "stderr").read_text()
    return json.loads((tmp_path / "stdout").read_text()), json.loads((tmp_path / "exit.json").read_text())


def test_c5_1_the_provider_and_its_children_run_at_utility_and_cannot_raise_it(tmp_path):
    """C-5.1: the clamp covers the provider, its child and its own attempt to raise itself."""
    report, receipt = run(tmp_path)
    assert report["provider"] and report["child"], report
    # An idle thread shows its base priority: the sleeping child sits exactly at utility's
    # (not background's 4, not the default's 31); a busy one only decays below its base.
    assert report["child"] == [UTILITY] * len(report["child"]), report
    assert BACKGROUND < min(report["provider"]) and max(report["provider"]) <= UTILITY, report
    assert report["raise_rc"] == 0 and max(report["after_raise"]) <= UTILITY
    if own_priority() > UTILITY:
        # The guardian keeps its daemon's scheduling: only agent work is clamped.
        assert max(report["guardian"]) > UTILITY, report


def test_c5_1_the_clamped_provider_keeps_its_pid_group_parent_and_markers(tmp_path):
    """C-5.1, C-5.2, C-5.5: taskpolicy execs in place, so the receipt's child is the provider,
    it leads nothing new, its parent is the guardian and it carries the census marker."""
    report, receipt = run(tmp_path)
    start = json.loads((tmp_path / "start.json").read_text())
    assert receipt["child_pid"] == report["pid"]
    assert report["pgid"] == start["pgid"] == start["guardian_pid"] == report["ppid"]
    assert report["attempt"] == "qos/a1"
    assert receipt["rc"] == 0 and "spawn_error" not in receipt


def test_c5_1_inherit_runs_the_provider_at_the_guardians_qos(tmp_path):
    """C-5.1: `SUBFLEET_PROVIDER_QOS=inherit` is the operator's opt-out."""
    if own_priority() <= UTILITY:
        pytest.skip("this test process is itself clamped; inherit and utility look alike")
    report, _ = run(tmp_path, SUBFLEET_PROVIDER_QOS="inherit")
    assert max(report["provider"]) > UTILITY and max(report["child"]) > UTILITY, report
