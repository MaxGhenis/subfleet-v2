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
