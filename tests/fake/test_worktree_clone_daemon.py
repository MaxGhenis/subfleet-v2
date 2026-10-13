"""C-6.6a in the daemon: a writable job's allocated worktree is made of clones
of the caller's checkout, git status is clean, the baseline snapshot reads it
as the commit, and `SUBFLEET_WORKTREE_CLONE=0` restores the plain add."""

import os

import pytest

from subfleet import retention_fs as rfs
from subfleet import worktree_clone
from tests.fake.test_state_contract import state_daemon  # noqa: F401 - fixture
from tests.fake.test_workspace_contract import repository
from tests.fake.test_workspace_transient import log_text
from tests.unit.test_salvage import git

pytestmark = pytest.mark.skipif(rfs._fclonefileat is None, reason="needs fclonefileat (macOS)")


@pytest.fixture(autouse=True)
def any_size_clones(monkeypatch):
    """These trees are small; the size floor is tested on its own."""
    monkeypatch.setattr(worktree_clone, "CLONE_MIN_BYTES", 0)


def private(path):
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return rfs.private_bytes(fd, path.name, os.lstat(path))
    finally:
        os.close(fd)


def big_repository(daemon, harness):
    workdir = repository(daemon, harness)
    (workdir / "ledger").mkdir()
    (workdir / "ledger" / "object").write_bytes(os.urandom(256 * 1024))
    git(workdir, "add", ".")
    git(workdir, "commit", "-m", "ledger")
    return workdir


def test_c6_6a_a_writable_job_runs_in_a_worktree_of_clones(state_daemon):
    daemon, harness = state_daemon
    workdir = big_repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    daemon._admit()
    [attempt] = daemon.store.list_attempts(job_id)
    assert attempt["state"] == "reserved"
    tree = daemon.root / "worktrees" / job_id
    assert git(tree, "rev-parse", "HEAD") == git(workdir, "rev-parse", "HEAD")
    assert git(tree, "status", "--porcelain", "--untracked-files=all") == ""
    assert (tree / "ledger" / "object").read_bytes() == (workdir / "ledger" / "object").read_bytes()
    assert private(tree / "ledger" / "object") == 0
    assert oct(os.stat(tree).st_mode & 0o777) == oct(0o700)
    assert f"worktree {job_id}: clone: 2 of 2 candidate files cloned" in log_text(daemon)


def test_c6_6a_off_in_the_daemon_environment_is_the_plain_add(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    big_repository(daemon, harness)
    monkeypatch.setenv(worktree_clone.CLONE_ENV, "0")
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    daemon._admit()
    [attempt] = daemon.store.list_attempts(job_id)
    assert attempt["state"] == "reserved"
    tree = daemon.root / "worktrees" / job_id
    assert private(tree / "ledger" / "object") > 0
    assert f"worktree {job_id}: plain: 0 of 0 candidate files cloned" in log_text(daemon)


def test_c6_6a_a_signal_killed_git_directory_lookup_waits_as_a_killed_add_waits(state_daemon, monkeypatch):
    """Review of eb8d62d8, finding 4: through `_workspace`, a lookup of the new
    tree's git directory killed by a signal is transient (the job waits, as for a
    killed `worktree add`), and nothing is left behind."""
    import signal
    import subprocess
    import subfleet.daemon as daemon_module
    from tests.fake.test_workspace_transient import due, events
    daemon, harness = state_daemon
    big_repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if "--absolute-git-dir" in cmd:
            return subprocess.CompletedProcess(cmd, -signal.SIGKILL, b"", b"")
        return real_run(cmd, *args, **kwargs)
    monkeypatch.setattr(daemon_module.subprocess, "run", run)
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["wait_reason"]) == ("waiting", "workspace")
    [record] = events(daemon, job_id, "job.workspace_deferred")
    assert "killed by SIGKILL" in record["error"]
    assert not (daemon.root / "worktrees" / job_id).exists()
    monkeypatch.setattr(daemon_module.subprocess, "run", real_run)
    due(daemon, job_id)
    daemon._admit()
    assert [a["state"] for a in daemon.store.list_attempts(job_id)] == ["reserved"]
