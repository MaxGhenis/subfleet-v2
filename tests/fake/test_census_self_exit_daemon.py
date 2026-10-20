"""D-KILL3, C-5.5: hidden-marker runners outlive self-exit and turn interrupt.

Only scratch locks and test-owned process groups are used. The real daemon,
guardian and shell provider run; the runner's real stop timer supplies SIGINT
at its default 10 s using a controlled clock, before close/contain at 20/30 s.
"""

import fcntl
import json
import os
from pathlib import Path
import signal
import sys
from types import SimpleNamespace

import pytest

from subfleet import procs
from subfleet.conversations.runner import Clocks, TurnRunner
from tests.fake.test_census_escapers_daemon import (
    escaper_identity, executable, held, settled, stop,
)

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS process census")


def interrupt_at_ten_seconds(provider_pid):
    """Use the real turn escalation, delivering its INT frame to our provider."""
    runner = SimpleNamespace(
        clock=lambda: 10, driver=SimpleNamespace(outcome=None), clocks=Clocks(),
        stop_at=0, ended_at=None, late_stop_at=None, escalated=set(), outbox=[],
        approval_seen={}, _watch_title=lambda: None,
        on_contain=lambda aid: pytest.fail("kill protocol must not run at 10 s"),
    )
    TurnRunner._timers(runner)
    assert runner.escalated == {"sigint"}
    assert [(frame.op, frame.line) for frame in runner.outbox] == [("signal", "INT")]
    os.kill(provider_pid, signal.SIGINT)


@pytest.mark.parametrize("ending", ["self-exit", "stopped-turn"])
def test_d_kill3_inspection_keeps_a_hidden_lockf_after_its_shell_provider_ends(daemon, ending):
    marker = daemon.root / "runner.pid"
    daemon.start("--hide-environment-of", str(marker), "--inspection-witness", str(marker),
                 rooted=False, isolated_group=True)
    lock = os.open(str(marker) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    runner = None
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        args = daemon.submit_args()
        Path(args["prompt_path"]).write_text(json.dumps({"scenario": "lockf-self-exit", "marker": str(marker)}))
        job = daemon.call("submit", **args)["job_id"]
        runner = escaper_identity(daemon, marker)
        daemon.until(lambda: executable(runner.pid) == "/usr/bin/lockf", timeout=30)
        daemon.until(lambda: Path(str(marker) + ".inspected").exists(), timeout=30)
        # No deciding census or kill preceded this inspection.
        before = daemon.attempts(job)[-1]
        assert before["state"] == "running" and before["killed_by"] is None
        table = procs.snapshot()
        provider = table.rows[runner.pid][0]  # our shell, still parenting lockf
        assert table.rows[provider][0] == before["guardian_pid"]
        if ending == "stopped-turn":
            interrupt_at_ten_seconds(provider)
        else:
            Path(str(marker) + ".release").touch()
        attempt = settled(daemon, job)
        assert attempt["state"] == "quarantined", attempt
        evidence = json.loads(attempt["evidence_json"])
        assert "responsible_root" not in evidence
        assert evidence["counted_identities"][str(runner.pid)] == {
            "pid": runner.pid, "boot_id": runner.boot_id, "proc_start": runner.proc_start}
        assert str(runner.pid) not in evidence.get("owned_identities", {})
        census = json.loads(attempt["quarantine_reason"])
        assert census["reason"] == "writers remain after exit receipt"
        assert runner.pid in census["descendant_pids"]
        assert runner.pid not in census["marker_pids"] + census["group_pids"]
        assert census["shapes"][str(runner.pid)]["ppid"] == 1
        assert procs.liveness(runner.pid, runner.boot_id, runner.proc_start) == "alive"
        assert held(daemon, job, attempt["attempt_id"])
        daemon.call("kill", job_id=job, confirm_dead=True)
        daemon.until(lambda: daemon.rows("SELECT 1 FROM events WHERE attempt_id=? AND kind=?",
                                         (attempt["attempt_id"], "quarantine.still_live")), timeout=30)
        assert held(daemon, job, attempt["attempt_id"])
    finally:
        stop(runner)
        Path(str(marker) + ".release").touch()
        os.close(lock)


@pytest.mark.documented_limitation
def test_d_kill3_a_hidden_runner_leaving_between_inspections_is_still_unseen(daemon):
    """C-5.5: a change that closes this window must deliberately change this test.

    The provider exits before the first inspection (paced at 60 s here), leaving
    no walk, group, marker or responsibility source for its waiting lockf.
    """
    marker = daemon.root / "unseen.pid"
    daemon.start("--hide-environment-of", str(marker), "--inspect-interval-s", "60",
                 rooted=False, isolated_group=True)
    lock = os.open(str(marker) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    runner = None
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        args = daemon.submit_args()
        Path(args["prompt_path"]).write_text(json.dumps({"scenario": "lockf-self-exit", "marker": str(marker)}))
        job = daemon.call("submit", **args)["job_id"]
        runner = escaper_identity(daemon, marker)
        daemon.until(lambda: executable(runner.pid) == "/usr/bin/lockf", timeout=30)
        Path(str(marker) + ".release").touch()
        attempt = settled(daemon, job)
        assert attempt["state"] == "failed", attempt
        assert not attempt["quarantine_reason"]
        assert not json.loads(attempt["evidence_json"]).get("counted_identities")
        assert procs.liveness(runner.pid, runner.boot_id, runner.proc_start) == "alive"
    finally:
        stop(runner)
        os.close(lock)
