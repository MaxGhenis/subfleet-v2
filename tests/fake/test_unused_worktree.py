"""C-13.4: a worktree admission cut for a job that ended before any attempt.

`_admit_pass` prepares a writable job's workspace (C-6.8) before it evaluates
the route, so a job that waits for capacity already has `worktrees/<job id>/`,
registered in its caller's repository. `jobs.worktree` is written only when an
attempt is reserved, and retention owned a worktree only through that column:
a job killed, refused, or failed before then left the worktree behind, and once
retention pruned its row nothing named it (on the live state root, two
`thesis-tsa-signers` jobs that failed on 2026-09-19 with no attempt, their rows
pruned on 2026-09-21, their worktrees still registered on 2026-09-22).
"""

import json
import os
import subprocess

import pytest

from subfleet import daemon as daemon_module, retention
from subfleet.daemon import after
from tests.fake.test_state_contract import state_daemon  # noqa: F401 (fixture)
from tests.fake.test_writable_fanout import fleet, writable  # noqa: F401 (fixture)
from tests.unit.test_salvage import git


@pytest.fixture
def full(fleet):
    """One slot, taken by a read-only job, so the next writable job waits on `fleet-full`."""
    daemon, harness, table = fleet
    daemon.policy["caps"]["max_active_attempts"] = 1
    blocker = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon._admit()
    assert daemon.store.get_job(blocker)["state"] == "running"
    return daemon, harness


def registered(repository, path):
    """Is `path` a worktree of `repository` (`git worktree list`)?"""
    listing = git(repository, "worktree", "list", "--porcelain").splitlines()
    return f"worktree {os.path.realpath(path)}" in listing


def events(daemon, job, kind):
    return [json.loads(row["data_json"]) for row in daemon.store.query(
        "SELECT data_json FROM events WHERE job_id=? AND kind=? ORDER BY event_id", (job, kind))]


def waiting_with_worktree(daemon, harness):
    job = writable(daemon, harness, harness.workdir, in_place=False)
    daemon._admit()
    row = daemon.store.get_job(job)
    path = daemon.root / "worktrees" / job
    assert (row["state"], row["wait_reason"]) == ("waiting", "capacity")
    assert daemon._holds[job]["reason"] == "fleet-full"
    # The mechanism: the pass cut the worktree, and nothing records it.
    assert row["worktree"] is None and daemon.store.list_attempts(job) == []
    assert (path / ".git").is_file() and registered(harness.workdir, path)
    return job, path


def acknowledge(daemon, job):
    with daemon.store.transaction("test.acknowledged", job_id=job) as tx:
        tx.execute("UPDATE notices SET state='acknowledged' WHERE job_id=?", (job,))


def test_c13_4_killed_waiting_job_worktree_is_removed_on_the_next_pass(full):
    """C-13.4 the defect: kill a writable job waiting on a full fleet; the next pass removes its worktree."""
    daemon, harness = full
    head = git(harness.workdir, "rev-parse", "HEAD")
    job, path = waiting_with_worktree(daemon, harness)
    daemon.dispatch("kill", {"job_id": job})
    assert daemon.store.get_job(job)["state"] == "cancelled"
    # Before the fix both survived this pass, and retention's pruning below.
    daemon._admit()
    assert not path.exists() and not registered(harness.workdir, path)
    assert events(daemon, job, "retention.unused_worktree_removed") == [{"worktree": str(path)}]
    assert not daemon.store.one("SELECT 1 FROM leases WHERE lease_key=?", (f"worktree:{path}",))
    # The caller's checkout is untouched.
    assert git(harness.workdir, "rev-parse", "HEAD") == head
    assert (harness.workdir / "tracked.txt").read_text() == "baseline\n"
    acknowledge(daemon, job)
    result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, unused_before=after(1))
    assert job in result["pruned"] and result["errors"] == []


def test_c13_4_job_failed_before_any_attempt_loses_its_worktree(full):
    """C-13.4 every terminal path before reservation, here `_fail_queued` (C-6.8, C-6.12 use it)."""
    daemon, harness = full
    job, path = waiting_with_worktree(daemon, harness)
    daemon._fail_queued(daemon.store.get_job(job), "refused at admission: test")
    assert daemon.store.get_job(job)["state"] == "failed"
    daemon._admit()
    assert not path.exists() and not registered(harness.workdir, path)


def test_c13_4_c6_8_preparation_failing_after_the_cut_loses_the_worktree(fleet, monkeypatch):
    """C-6.8 a permanent failure after `git worktree add` (here the baseline snapshot) fails the job; the next pass collects."""
    daemon, harness, _ = fleet
    job = writable(daemon, harness, harness.workdir, in_place=False)
    path = daemon.root / "worktrees" / job

    def denied(*args, **kwargs):
        assert path.is_dir()                     # cut already
        raise PermissionError(13, "Permission denied", str(path))
    monkeypatch.setattr(daemon_module, "working_tree", denied)
    daemon._admit()
    assert daemon.store.get_job(job)["state"] == "failed" and path.is_dir()
    monkeypatch.undo()
    daemon._admit()
    assert not path.exists() and not registered(harness.workdir, path)


def test_c13_4_c7_3_killing_a_parent_collects_its_waiting_childs_worktree(full):
    """C-7.3 a dependent child waiting with a cut worktree is cancelled with its parent; its worktree goes too."""
    daemon, harness = full
    parent = daemon.store.query("SELECT job_id FROM jobs WHERE state='running'")[0]["job_id"]
    child = writable(daemon, harness, harness.workdir, in_place=False, parent_job_id=parent)
    daemon._admit()
    path = daemon.root / "worktrees" / child
    assert daemon.store.get_job(child)["state"] == "waiting" and registered(harness.workdir, path)
    daemon.dispatch("kill", {"job_id": parent})
    assert daemon.store.get_job(child)["state"] == "cancelled"
    daemon._admit()
    assert not path.exists() and not registered(harness.workdir, path)


def test_c13_4_kill_between_the_cut_and_the_reservation_is_collected(full, monkeypatch):
    """C-13.4 a kill that lands after this pass cut the worktree and before it reserved: the next pass removes it."""
    daemon, harness = full
    job = writable(daemon, harness, harness.workdir, in_place=False)
    path = daemon.root / "worktrees" / job
    cut = daemon._workspace

    def cut_then_kill(row):
        prepared = cut(row)
        assert path.is_dir()
        daemon.dispatch("kill", {"job_id": row["job_id"]})
        return prepared
    monkeypatch.setattr(daemon, "_workspace", cut_then_kill)
    daemon._admit()
    assert daemon.store.get_job(job)["state"] == "cancelled" and path.is_dir()
    monkeypatch.undo()
    daemon._admit()
    assert not path.exists() and not registered(harness.workdir, path)


def test_c13_4_a_pass_is_not_raced_by_pruning_the_job_it_is_about_to_cut(full, monkeypatch):
    """C-13.4 the fence: retention leaves alone a job that ended after the admission pass in flight began.

    Without it, retention could prune the killed job's row between this pass
    reading the queue and cutting its worktree; the cut would then leave a
    worktree that no row, and so no later pass, ever names. Retention runs
    here through the daemon's own `_retention`, a few seconds after the kill.
    """
    daemon, harness = full
    job = writable(daemon, harness, harness.workdir, in_place=False)
    path = daemon.root / "worktrees" / job
    cut, real = daemon._workspace, retention.maintenance
    seen = {}

    def over_the_limits(*args, **kwargs):
        seen["kwargs"] = kwargs
        return real(*args, **{**kwargs, "max_jobs": 0})

    def prune_then_cut(row):
        assert daemon._admission_began is not None
        daemon.dispatch("kill", {"job_id": row["job_id"]})
        acknowledge(daemon, row["job_id"])
        with monkeypatch.context() as later:
            later.setattr(daemon_module, "utcnow", lambda: after(5))
            later.setattr(daemon_module, "maintenance", over_the_limits)
            daemon._retention()
        seen["row"] = daemon.store.get_job(row["job_id"])
        return cut(row)
    monkeypatch.setattr(daemon, "_workspace", prune_then_cut)
    daemon._admit()
    monkeypatch.undo()
    assert seen["kwargs"]["unused_before"] <= seen["row"]["finished_at"] < after(5)
    assert seen["row"] is not None and path.is_dir() and daemon._admission_began is None
    daemon._admit()
    assert not path.exists() and not registered(harness.workdir, path)
    result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, unused_before=after(1))
    assert job in result["pruned"] and not path.exists()


def test_c13_4_a_pass_skips_a_job_that_ended_after_it_read_the_queue(full, monkeypatch):
    """C-13.4 the row is read again before the cut, so a job killed mid-pass is not cut a worktree at all."""
    daemon, harness = full
    job = writable(daemon, harness, harness.workdir, in_place=False)
    path = daemon.root / "worktrees" / job
    collect = daemon._collect_unused_worktrees

    def kill_after_the_queue_is_read(waiting):
        assert job in waiting                    # this pass listed it as queued
        daemon.dispatch("kill", {"job_id": job})
        collect(waiting)
    monkeypatch.setattr(daemon, "_collect_unused_worktrees", kill_after_the_queue_is_read)
    monkeypatch.setattr(daemon, "_workspace", lambda row: pytest.fail(f"cut a worktree for {row['job_id']}"))
    daemon._admit()
    assert daemon.store.get_job(job)["state"] == "cancelled" and not path.exists()


def test_c13_4_one_pass_removes_within_its_budget(full, monkeypatch):
    """C-13.4 a killed batch does not stall the queue: past the budget, the rest wait for the next pass."""
    daemon, harness = full
    monkeypatch.setattr(daemon_module, "UNUSED_WORKTREE_BUDGET_S", 0)
    jobs = [writable(daemon, harness, harness.workdir, in_place=False) for _ in range(3)]
    for job in jobs:                             # what admission's workspace preparation does
        daemon._workspace(daemon.store.get_job(job))
    paths = [daemon.root / "worktrees" / job for job in jobs]
    assert all(registered(harness.workdir, path) for path in paths)
    for job in jobs:
        daemon.dispatch("kill", {"job_id": job})
    for remaining in (2, 1, 0):
        daemon._admit()
        assert sum(path.exists() for path in paths) == remaining


def test_c13_4_a_restart_drops_the_fence_a_stopped_collection_kept(full):
    """C-13.4 the admission collector's fence outlives a stop; the next start drops it and finishes the removal."""
    daemon, harness = full
    job, path = waiting_with_worktree(daemon, harness)
    daemon.dispatch("kill", {"job_id": job})
    assert daemon.store.acquire_lease(f"worktree:{path}", f"admission-collect:{job}")
    daemon._reset_admission_state()
    daemon._admit()
    assert not path.exists() and daemon.store.list_leases(f"admission-collect:{job}") == []


def test_c13_4_a_restart_drops_a_stale_fence_whose_tree_is_gone(full):
    """C-13.4 a collection stopped after its removal keeps its fence; the next start drops it, so pruning can proceed."""
    daemon, harness = full
    job, path = waiting_with_worktree(daemon, harness)
    daemon.dispatch("kill", {"job_id": job})
    acknowledge(daemon, job)
    git(harness.workdir, "worktree", "remove", str(path))
    assert daemon.store.acquire_lease(f"worktree:{path}", f"admission-collect:{job}")
    blocked = retention.maintenance(daemon.store, daemon.root, max_jobs=0, unused_before=after(1))
    assert job not in blocked["pruned"]
    daemon._reset_admission_state()
    daemon._admit()
    assert daemon.store.list_leases(f"admission-collect:{job}") == []
    result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, unused_before=after(1))
    assert job in result["pruned"]


def test_c13_4_c6_8_the_add_runs_in_the_c_locale(fleet, monkeypatch):
    """C-6.8, C-13.4 an add stopped partway leaves git's own lock reason, `initializing`, in any language."""
    daemon, harness, _ = fleet
    job = writable(daemon, harness, harness.workdir, in_place=False)
    real, seen = daemon_module.subprocess.run, []

    def record(argv, **kwargs):
        if argv[3:5] == ["worktree", "add"]:
            seen.append(kwargs.get("env") or {})
        return real(argv, **kwargs)
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.setattr(daemon_module.subprocess, "run", record)
    daemon._workspace(daemon.store.get_job(job))
    assert len(seen) == 1 and seen[0]["LC_ALL"] == "C"


def test_c13_4_a_stopping_daemon_starts_no_removal(full):
    """C-13.4 on stop the collector leaves the set as it is for the next start."""
    daemon, harness = full
    job, path = waiting_with_worktree(daemon, harness)
    daemon.dispatch("kill", {"job_id": job})
    daemon.stopping.set()
    daemon._collect_unused_worktrees(set())
    assert path.is_dir() and job in daemon._unused_worktrees


def test_c13_4_a_restart_forgets_no_unused_worktree(full):
    """C-13.4 the set is in memory; the first pass after a start reads what is on disk."""
    daemon, harness = full
    job, path = waiting_with_worktree(daemon, harness)
    daemon.dispatch("kill", {"job_id": job})
    daemon._reset_admission_state()          # what a new process remembers
    assert job not in daemon._unused_worktrees
    daemon._admit()
    assert not path.exists() and not registered(harness.workdir, path)


def test_c13_4_a_waiting_job_keeps_its_worktree(full):
    """C-6.8 a waiting job reuses the worktree it was cut on every pass; nothing collects it while it waits."""
    daemon, harness = full
    job, path = waiting_with_worktree(daemon, harness)
    marker = (path / ".git").read_text()
    for _ in range(3):
        daemon.store.update_job(job, next_check_at=None)
        daemon._admit()
    assert daemon.store.get_job(job)["state"] == "waiting"
    assert (path / ".git").read_text() == marker and registered(harness.workdir, path)


def test_c13_4_a_worktree_an_attempt_used_is_never_collected_early(fleet):
    """C-13.4 once an attempt is reserved, the worktree waits for retention's ordinary rule (salvage first)."""
    daemon, harness, _ = fleet
    job = writable(daemon, harness, harness.workdir, in_place=False)
    daemon._admit()
    path = daemon.root / "worktrees" / job
    row = daemon.store.get_job(job)
    assert row["state"] == "running" and row["worktree"] == str(path)
    (path / "tracked.txt").write_text("provider work\n")
    # The attempt ends without salvage and the job is cancelled.
    attempt = daemon.store.list_attempts(job)[0]["attempt_id"]
    with daemon.store.transaction("test.ended", job_id=job) as tx:
        tx.execute("UPDATE attempts SET state='interrupted' WHERE attempt_id=?", (attempt,))
        tx.execute("UPDATE jobs SET state='cancelled',rc=130 WHERE job_id=?", (job,))
        tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (job, attempt))
    daemon._reset_admission_state()
    daemon._admit()
    assert (path / "tracked.txt").read_text() == "provider work\n"
    assert retention.collect_unused_worktree(daemon.store, daemon.root, job, holder="test:job") is False
    # And the job's record, rather than a NULL column, still decides after this.
    with daemon.store.transaction("test.cleared", job_id=job) as tx:
        tx.execute("UPDATE jobs SET worktree=NULL WHERE job_id=?", (job,))
    daemon._reset_admission_state()
    daemon._admit()
    assert retention.collect_unused_worktree(daemon.store, daemon.root, job, holder="test:job") is False
    assert (path / "tracked.txt").read_text() == "provider work\n"


def test_c13_4_a_changed_unused_worktree_is_kept_and_reported(full, caplog):
    """C-13.4 no attempt means no salvage: a dirty worktree is kept, never forced, and the row is not pruned."""
    daemon, harness = full
    job, path = waiting_with_worktree(daemon, harness)
    (path / "tracked.txt").write_text("someone's edit\n")
    (path / "notes.txt").write_text("untracked\n")
    daemon.dispatch("kill", {"job_id": job})
    daemon._admit()
    assert (path / "tracked.txt").read_text() == "someone's edit\n" and (path / "notes.txt").exists()
    assert registered(harness.workdir, path)
    kept = events(daemon, job, "retention.unused_worktree_kept")
    assert len(kept) == 1 and "something its commit does not" in kept[0]["error"]
    assert f"job {job} ended before any attempt; its worktree was kept" in caplog.text
    assert not daemon.store.one("SELECT 1 FROM leases WHERE lease_key=?", (f"worktree:{path}",))
    # Tried once per start; the next pass leaves it to retention.
    daemon._admit()
    assert len(events(daemon, job, "retention.unused_worktree_kept")) == 1
    acknowledge(daemon, job)
    result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, unused_before=after(1))
    assert job not in result["pruned"] and job in result["protected"]
    assert any(error["job_id"] == job and "something its commit does not" in error["error"] for error in result["errors"])
    assert daemon.store.get_job(job) is not None and (path / "notes.txt").exists()


def test_c13_4_process_daemon_removes_the_worktree_of_a_killed_waiting_job(daemon):
    """C-13.4 end to end: the running daemon's own passes remove it; the caller's repository is left clean."""
    workdir = daemon.workdir
    git(workdir, "init", "-b", "task/example")
    (workdir / "tracked.txt").write_text("baseline\n")
    git(workdir, "add", ".")
    git(workdir, "-c", "user.name=Fake", "-c", "user.email=fake@example.test", "commit", "-m", "baseline")
    policy = json.loads((daemon.root / "policy.json").read_text())
    policy["caps"]["max_active_attempts"] = 1
    (daemon.root / "policy.json").write_text(json.dumps(policy))
    daemon.start()
    blocker = daemon.submit("ok", delay_s=60)
    daemon.attempt_state(blocker, "running")
    job = daemon.submit(sandbox="workspace-write", in_place=False)
    path = daemon.root / "worktrees" / job
    daemon.until(lambda: daemon.job(job)["state"] == "waiting" and registered(workdir, path))
    assert daemon.job(job)["worktree"] is None and daemon.attempts(job) == []
    daemon.call("kill", job_id=job)
    assert daemon.finished(job)["state"] == "cancelled"
    daemon.until(lambda: not path.exists() and not registered(workdir, path))
    assert git(workdir, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert git(workdir, "status", "--porcelain") == ""
    # The blocker is not waited for: containing a running provider can take
    # longer than `until` allows under load, and the fixture's close kills it.
    daemon.call("kill", job_id=blocker)
