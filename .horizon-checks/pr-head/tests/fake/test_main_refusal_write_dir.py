"""C-13.2: the main/master refusal is about where a job writes, not where its caller stands."""

import pytest

from subfleet.adapters.base import AdapterError
from subfleet.daemon import Daemon
from tests.fake.test_state_contract import state_daemon
from tests.fake.test_workspace_contract import repository
from tests.unit.test_salvage import git


@pytest.mark.parametrize("branch", ["main", "master"])
def test_c13_2_a_job_with_its_own_worktree_may_be_cut_from_main(state_daemon, branch):
    """C-6.6, C-13.2 not in place: a detached worktree from the caller's head; the caller's checkout is untouched."""
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    git(workdir, "branch", "-m", branch)
    (workdir / "dirty.txt").write_text("the caller's uncommitted work\n")
    before = (git(workdir, "rev-parse", "HEAD"), git(workdir, "status", "--porcelain=v1"),
              (workdir / ".git" / "index").read_bytes())
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    daemon._admit()
    [attempt] = daemon.store.list_attempts(job_id)
    allocated = daemon.root / "worktrees" / job_id
    assert attempt["state"] == "reserved" and daemon.store.get_job(job_id)["worktree"] == str(allocated)
    assert git(allocated, "rev-parse", "HEAD") == before[0]
    assert git(allocated, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"          # detached: no branch to be on
    assert not (allocated / "dirty.txt").exists()
    assert before == (git(workdir, "rev-parse", "HEAD"), git(workdir, "status", "--porcelain=v1"),
                      (workdir / ".git" / "index").read_bytes())
    assert git(workdir, "rev-parse", "--abbrev-ref", "HEAD") == branch


@pytest.mark.parametrize("branch", ["main", "master"])
def test_c13_2_an_in_place_job_on_main_is_still_refused(state_daemon, branch):
    """C-6.5, C-13.2 writing where the caller stands, on main, is what the rule exists to stop."""
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    git(workdir, "branch", "-m", branch)
    with pytest.raises(AdapterError) as error:
        daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", in_place=True))
    assert error.value.code == 7 and branch in str(error.value) and daemon.store.list_jobs() == []


def test_c13_2_an_allocated_worktree_switched_to_main_is_refused_before_it_runs_again(state_daemon):
    """C-13.2 a retry is checked where it writes: its own worktree, now on main, refuses at admission and at launch."""
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)                                        # the caller is on task/example
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    daemon._admit()
    [attempt] = daemon.store.list_attempts(job_id)
    allocated = daemon.root / "worktrees" / job_id
    git(allocated, "checkout", "-b", "main")                                      # what a provider could do
    with pytest.raises(AdapterError) as error:
        daemon._workspace(daemon.store.get_job(job_id))
    assert error.value.code == 7 and "main" in str(error.value)
    Daemon._launch(daemon, attempt)                                               # the fixture forbids real launches
    assert daemon._children == {}
    daemon._finalize(daemon.store.get_attempt(attempt["attempt_id"]))
    job = daemon.store.get_job(job_id)
    assert job["state"] == "failed" and job["rc"] == 7
