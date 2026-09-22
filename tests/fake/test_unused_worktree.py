"""C-13.4: a worktree admission cut for a job that ended before any attempt.

`_admit_pass` prepares a writable job's workspace (C-6.8) before it evaluates
the route, so a job that waits for capacity already has `worktrees/<job id>/`,
registered in its caller's repository. `jobs.worktree` is written only when an
attempt is reserved, and retention owned a worktree only through that column.
"""

import os

import pytest

from subfleet import retention
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


def test_c13_4_killed_waiting_job_leaks_its_worktree(full):
    """C-13.4 (defect): kill a writable job waiting on a full fleet; its worktree outlives it and its row."""
    daemon, harness = full
    job, path = waiting_with_worktree(daemon, harness)
    daemon.dispatch("kill", {"job_id": job})
    assert daemon.store.get_job(job)["state"] == "cancelled"
    daemon._admit()
    assert path.exists() and registered(harness.workdir, path)
    # Retention prunes the row once its notice is read and leaves the worktree
    # behind with nothing that names it.
    daemon.store.connection.execute("UPDATE notices SET state='acknowledged' WHERE job_id=?", (job,))
    result = retention.maintenance(daemon.store, daemon.root, max_jobs=0)
    assert job in result["pruned"] and daemon.store.get_job(job) is None
    assert path.exists() and registered(harness.workdir, path)
