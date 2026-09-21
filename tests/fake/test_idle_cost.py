"""C-3.6, C-5.12, C-6.9, C-6.12: what a tick costs while jobs wait and attempts run.

Incident, 2026-09-20: with four attempts running and twelve jobs queued the daemon
held a full core. About fifteen processes a second were `ps`, `sysctl` and
`git add -A` in the worktree of a job that was only waiting, and every second each
evaluable waiting job got an `attempt.reserved` event and a 22 KB decision row
although nothing was reserved: 97,623 such rows, 2.17 GB of a 2.33 GB store.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from subfleet import procs
from subfleet.adapters.registry import register
from subfleet.contracts import (ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner,
                                Reading, ReadingLabel, attempt_dir)
from subfleet.daemon import Daemon, after, utcnow
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter
from tests.unit.test_salvage import git

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


def repository(path):
    path.mkdir(parents=True)
    git(path, "init", "-b", "task/example")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "tracked.txt").write_text("baseline\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "baseline")
    return path


def close_every_lane(daemon):
    daemon.store.put_closure(Closure("codex-1", "account", after(3600), ClosureReason.PROVIDER_LIMIT,
                                     ClockSource.REPORTED, "fixture"))


def due(daemon):
    """End every capacity wait's clock, so the next pass evaluates each waiting job again."""
    with daemon.store.transaction("test.due") as tx:
        tx.execute("UPDATE jobs SET next_check_at=? WHERE state='waiting'", (after(-1),))


def kinds(daemon, job_id=None):
    return [row["kind"] for row in daemon.store.list_events(job_id)]


def test_c6_12_a_waiting_queue_starts_no_process_per_tick(service, spawns, tmp_path):
    """C-6.12, C-6.8 jobs with no lane to go to are not prepared: no git, no worktree, however often they are looked at."""
    daemon, harness = service
    close_every_lane(daemon)
    # Three tiers, so no job is held back behind another and each is evaluated on every pass;
    # a checkout each, because two writable jobs may not share one.
    jobs = [daemon.dispatch("submit", harness.submit_args(workdir=str(repository(tmp_path / tier)), tier=tier,
                                                          caller_session=None, sandbox=sandbox,
                                                          in_place=in_place))["job_id"]
            for tier, sandbox, in_place in (("easy", "workspace-write", False), ("standard", "workspace-write", True),
                                            ("hard", "read-only", False))]
    spawns.clear()
    for _ in range(5):
        daemon._admit()
        due(daemon)
    assert spawns == []
    assert list((daemon.root / "worktrees").iterdir()) == []
    assert all(daemon.store.get_job(job)["wait_reason"] == "capacity" for job in jobs)
    assert daemon.store.list_attempts() == []
    # The moment a lane opens, the same jobs are prepared and admitted: nothing was skipped for good.
    with daemon.store.transaction("test.open") as tx:
        tx.execute("UPDATE closures SET released_at=?", (utcnow(),))
    daemon._admit()
    assert any(call[0] == "git" for call in spawns)
    assert [row["state"] for row in daemon.store.list_attempts(jobs[0])] == ["reserved"]


def test_c6_8_a_job_waiting_on_its_workspace_is_still_prepared_whatever_capacity_says(service, spawns, tmp_path):
    """C-6.8, C-6.12 the workspace wait is preparation's own to end, so it is not skipped for want of a lane."""
    daemon, harness = service
    workdir = repository(tmp_path / "repo")
    close_every_lane(daemon)
    job = daemon.dispatch("submit", harness.submit_args(workdir=str(workdir), sandbox="workspace-write",
                                                        in_place=True, caller_session=None))["job_id"]
    daemon.store.update_job(job, state="waiting", wait_reason="workspace", next_check_at=after(-1))
    spawns.clear()
    daemon._admit()
    assert any(call[0] == "git" for call in spawns)
    assert daemon.store.get_job(job)["wait_reason"] == "capacity"
    assert "job.workspace_ready" in kinds(daemon, job)


def test_c6_12_a_workspace_wait_is_ended_even_under_a_full_fleet_and_is_not_probed(service, spawns, tmp_path):
    """C-6.12, C-6.8 a full fleet ends the pass for every job but the one whose workspace is what it waits for."""
    daemon, harness = service
    daemon.policy["caps"]["max_active_attempts"] = 1
    daemon.dispatch("submit", harness.submit_args())
    daemon._admit()                                               # the fleet is now full
    job = daemon.dispatch("submit", harness.submit_args(workdir=str(repository(tmp_path / "repo")), tier="hard",
                                                        sandbox="workspace-write", in_place=True,
                                                        caller_session=None))["job_id"]
    daemon.store.update_job(job, state="waiting", wait_reason="workspace", next_check_at=after(-1))
    daemon._prepare_route = lambda *args: pytest.fail("no probe for a job the fleet cannot take")
    spawns.clear()
    daemon._admit()
    assert any(call[0] == "git" for call in spawns)
    row = daemon.store.get_job(job)
    assert (row["state"], row["wait_reason"]) == ("waiting", "capacity") and "job.workspace_ready" in kinds(daemon, job)
    spawns.clear()
    due(daemon)
    daemon._admit()                                               # a capacity wait now: the full fleet ends the pass
    assert spawns == []


def test_c3_6_a_pass_that_only_records_exclusions_says_so(service):
    """C-3.6, C-6.12 a lane that frees between the check and the transaction records no wait that did not happen."""
    daemon, harness = service
    home = harness.root / "home-2"
    home.mkdir()
    daemon.store.put_lane(Lane("codex-2", "codex", "codex:second", Credential("codex", str(home), "home"),
                               str(home), LaneOwner.V2, False))
    job = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon.store.add_attempt(attempt_id=f"{job}/a1", job_id=job, seq=1, lane_id="codex-2", model_requested="gpt-6-astra",
                             state="failed", outcome_class="limited", evidence_json="{}")
    daemon._placeable = lambda *args, **kwargs: False             # the check saw no room; the transaction will
    daemon._admit()
    row = daemon.store.get_job(job)
    assert (row["state"], row["wait_reason"]) == ("queued", None) and "codex-2" in row["exclusions"]
    assert kinds(daemon, job)[-1] == "job.exclusions_recorded" and "job.capacity_waiting" not in kinds(daemon, job)
    del daemon._placeable
    daemon._admit()                                               # one tick later it is prepared and admitted
    assert [a["state"] for a in daemon.store.list_attempts(job)][-1] == "reserved"


def test_c3_6_an_event_says_reserved_only_when_an_attempt_was(service):
    """C-3.6, C-3.2, C-6.3 a pass that leaves a job waiting records the wait, never a reservation."""
    daemon, harness = service
    close_every_lane(daemon)
    job = daemon.dispatch("submit", harness.submit_args())["job_id"]
    for _ in range(4):
        daemon._admit()
        due(daemon)
    assert daemon.store.list_attempts(job) == []
    assert "attempt.reserved" not in kinds(daemon) and "job.capacity_waiting" in kinds(daemon, job)
    with daemon.store.transaction("test.open") as tx:
        tx.execute("UPDATE closures SET released_at=?", (utcnow(),))
    daemon._admit()
    attempt = daemon.store.list_attempts(job)[-1]
    reserved = [row for row in daemon.store.list_events(job) if row["kind"] == "attempt.reserved"]
    assert [(row["attempt_id"], row["lane_id"]) for row in reserved] == [(attempt["attempt_id"], "codex-1")]


def test_c6_9_a_full_fleet_is_one_count_per_pass_and_writes_nothing(service, spawns):
    """C-6.9 at `max_active_attempts` the pass ends before its first job: no event, no decision row, no process."""
    daemon, harness = service
    daemon.policy["caps"]["max_active_attempts"] = 1
    running = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon._admit()
    assert [row["state"] for row in daemon.store.list_attempts(running)] == ["reserved"]
    waiting = [daemon.dispatch("submit", harness.submit_args(tier=tier))["job_id"] for tier in ("easy", "standard", "hard")]
    before = (len(daemon.store.list_events()), daemon.store.connection.total_changes)
    spawns.clear()
    for _ in range(10):
        daemon._admit()
    assert (len(daemon.store.list_events()), daemon.store.connection.total_changes) == before
    assert spawns == []
    assert [len(daemon.store.list_decisions(job)) for job in waiting] == [0, 0, 0]
    # A slot frees: the next pass places the oldest job of the first tier, as C-6.9 orders them.
    attempt = daemon.store.list_attempts(running)[-1]
    with daemon.store.transaction("test.finish") as tx:
        tx.execute("UPDATE attempts SET state='succeeded' WHERE attempt_id=?", (attempt["attempt_id"],))
        tx.execute("DELETE FROM leases WHERE holder=?", (attempt["attempt_id"],))
    daemon._admit()
    assert [row["state"] for row in daemon.store.list_attempts(waiting[0])] == ["reserved"]


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
        daemon._table_next = 0.0
        daemon._process_attempt(attempts[0])
        assert daemon.store.get_attempt(attempts[0])["state"] != "running"
    finally:
        for child in guardians:
            if child.poll() is None:
                child.kill()
                child.wait()


def test_c5_12_the_idle_cost_measurement_runs_and_a_full_fleet_writes_nothing(process_inspection_available):
    """C-5.12, C-6.9 `tools/measure_idle_cost.py` drives a real control loop; under a full fleet it sees no row written."""
    spec = importlib.util.spec_from_file_location(
        "measure_idle_cost", Path(__file__).resolve().parents[2] / "tools" / "measure_idle_cost.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    result = tool.measure(window_s=.5, history=20, waiting=6, settle_s=.3, closed_settle_s=.3)
    assert (result["events_written"], result["decisions_written"]) == (0, 0)
    assert all(0 <= value for pair in (result["idle"], result["saturated"]) for value in pair)
