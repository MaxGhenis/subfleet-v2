"""C-5.12: what a tick costs while attempts run.

Incident, 2026-09-20: with four attempts running the daemon held a full core. About
fifteen processes a second were `ps` and `sysctl`: three to ask whether each
healthy guardian was alive on every 50 ms tick, and a census of `2 + 3N` every
0.5 s to record its group's members.
"""

import importlib.util
import math
import subprocess
import sys
from pathlib import Path

import pytest

from subfleet import procs
from subfleet.adapters.registry import register
from subfleet.contracts import Reading, ReadingLabel, attempt_dir
from subfleet.daemon import Daemon, after, utcnow
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter

REAL_POPEN = subprocess.Popen


class CountingPopen(REAL_POPEN):
    """`run`, `check_output` and `call` all reach the module's `Popen`, so this one
    seam sees every process the daemon starts, once each."""
    calls: list[tuple[str, ...]] = []

    def __init__(self, args, *rest, **kwargs):
        CountingPopen.calls.append(tuple(str(part) for part in args))
        super().__init__(args, *rest, **kwargs)


@pytest.fixture
def spawns(monkeypatch):
    CountingPopen.calls = []
    monkeypatch.setattr(subprocess, "Popen", CountingPopen)
    return CountingPopen.calls


@pytest.fixture
def service(tmp_path, monkeypatch, process_inspection_available):
    """A real daemon core on a temp state root with one measured Codex lane; ticks are driven by hand."""
    root = tmp_path / "state"
    root.mkdir()
    harness = Harness(root)
    register("codex", FakeAdapter)
    daemon = Daemon(harness.root, desktop_prober=lambda: None)
    monkeypatch.setattr(daemon, "_launch", lambda *args: pytest.fail("nothing here may launch a guardian"))
    daemon.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                     ReadingLabel.PROVIDER, "fixture", utcnow()))
    try:
        yield daemon, harness
    finally:
        daemon.close()


def test_c5_12_running_attempts_share_one_ps_per_interval(service, spawns):
    """C-5.12, C-5.3 four healthy attempts ticked twenty times cost one `ps` and one `sysctl`, not three hundred."""
    daemon, harness = service
    guardians, attempts = [], []
    try:
        for index in range(4):
            job_id = daemon.dispatch("submit", harness.submit_args(name=f"run-{index}"))["job_id"]
            child = REAL_POPEN([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            guardians.append(child)
            aid = f"{job_id}/a1"
            attempt_dir(daemon.root, job_id, 1).mkdir(parents=True, exist_ok=True)
            daemon.store.add_attempt(attempt_id=aid, job_id=job_id, seq=1, lane_id="codex-1",
                                     model_requested="gpt-6-astra", state="running",
                                     guardian_pid=child.pid, child_pid=child.pid, pgid=child.pid,
                                     boot_id=procs.boot_id(),
                                     # `ps` can miss a pid for a few milliseconds after the fork.
                                     proc_start=procs.proc_start_retry(child.pid, alive=lambda: child.poll() is None),
                                     started_at=utcnow(), evidence_json="{}")
            daemon.store.update_job(job_id, state="running", started_at=utcnow())
            attempts.append(aid)
        assert all(daemon.store.get_attempt(aid)["proc_start"] for aid in attempts)
        getattr(procs, "forget_boot_id", lambda: None)()          # absent before C-5.12; the count below is the test
        daemon.inspect_interval_s = 30           # one interval spans the whole loop below
        spawns.clear()
        for _ in range(20):
            for aid in attempts:
                daemon._process_attempt(aid)
        assert sorted(call[0].rsplit("/", 1)[-1] for call in spawns) == ["ps", "sysctl"]
        assert not any("-axEww" in call for call in spawns)       # the environment scan is for a census that decides
        for aid, child in zip(attempts, guardians):
            a = daemon.store.get_attempt(aid)
            assert a["state"] == "running" and str(child.pid) in a["evidence_json"]
        # A guardian that dies is still found, by a fresh read, on the next inspection.
        guardians[0].kill()
        guardians[0].wait()
        daemon._inspect_next.clear()
        daemon._table = (None, 0.0)
        daemon._process_attempt(attempts[0])
        assert daemon.store.get_attempt(attempts[0])["state"] != "running"
    finally:
        for child in guardians:
            if child.poll() is None:
                child.kill()
                child.wait()


def test_c5_12_the_idle_cost_measurement_runs(process_inspection_available):
    """C-5.12, C-6.10 `tools/measure_idle_cost.py` drives a real control loop; a repeated wait stores no decision row.

    Each window starts once admission has settled: its first look at a new queue
    writes a decision row per job it evaluates, and under load that look can land
    inside a window that follows a fixed pause (2026-09-25, CI run 36016193771).
    """
    spec = importlib.util.spec_from_file_location(
        "measure_idle_cost", Path(__file__).resolve().parents[2] / "tools" / "measure_idle_cost.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    result = tool.measure(window_s=.5, history=20, waiting=6, settle_s=.5, closed_settle_s=.3, deadline_s=60)
    assert result["settled"] == {"idle": True, "saturated": True, "closed": True}, result
    assert result["decisions_written"] == 0 and result["closed_decisions_written"] == 0, result
    # Every window reports the daemon's share and its children's, as finite numbers.
    for window in ("idle", "saturated", "closed"):
        assert len(result[window]) == 2 and all(math.isfinite(value) and value >= 0 for value in result[window]), result
