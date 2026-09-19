"""C-13 salvage snapshots are private git objects, never worktree mutations."""

import subprocess
from pathlib import Path

import pytest

from subfleet.adapters.base import AdapterError
from subfleet.salvage import git_head, salvage, validate_writable_workdir, working_tree


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-b", "task/example")
    git(tmp_path, "config", "user.name", "Test User")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "tracked.txt").write_text("baseline\n")
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "baseline")
    return tmp_path


def test_salvage_preserves_head_index_worktree(repository):
    """C-13.1 temporary-index salvage leaves HEAD, real index and files untouched."""
    baseline = git_head(repository)
    (repository / "tracked.txt").write_text("staged change\n")
    git(repository, "add", "tracked.txt")
    (repository / "tracked.txt").write_text("unstaged change\n")
    (repository / "untracked.txt").write_text("untracked work\n")
    (repository / "ignored.txt").write_text("ignored private material\n")
    index = (repository / ".git" / "index").read_bytes()
    contents = {p.name: p.read_bytes() for p in repository.iterdir() if p.is_file()}
    status = git(repository, "status", "--porcelain=v1")
    result = salvage(repository, baseline, 1, timestamp="2026-09-05T10:00:00Z")
    assert result is not None
    assert result.ref == "refs/subfleet-salvage/task-example-20260905T100000Z-a1"
    assert git(repository, "rev-parse", result.ref) == result.commit
    assert git(repository, "rev-parse", f"{result.commit}^") == baseline
    assert git(repository, "show", f"{result.ref}:tracked.txt") == "unstaged change"
    assert git(repository, "show", f"{result.ref}:untracked.txt") == "untracked work"
    assert "ignored.txt" not in git(repository, "ls-tree", "-r", "--name-only", result.ref)
    assert git_head(repository) == baseline
    assert (repository / ".git" / "index").read_bytes() == index
    assert contents == {p.name: p.read_bytes() for p in repository.iterdir() if p.is_file()}
    assert git(repository, "status", "--porcelain=v1") == status
    assert not list((repository / ".git").glob("subfleet-salvage-*"))


def test_salvage_compares_tree_against_reserved_baseline(repository):
    """C-13.1 a clean but newly committed tree still differs from reserved baseline."""
    baseline = git_head(repository)
    (repository / "tracked.txt").write_text("provider committed useful work\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "provider progress")
    current_head = git_head(repository)
    assert git(repository, "status", "--porcelain") == ""
    result = salvage(repository, baseline, 2)
    assert result is not None
    assert git(repository, "rev-parse", f"{result.commit}^") == baseline
    assert git_head(repository) == current_head


def test_salvage_unchanged_baseline_has_no_ref(repository):
    """C-13.1 unchanged working trees do not create redundant private commits."""
    assert salvage(repository, git_head(repository), 1) is None
    assert git(repository, "for-each-ref", "refs/subfleet-salvage/") == ""


def test_salvage_uses_reserved_dirty_tree_without_mutating_index(repository):
    """C-13.1 only changes after reservation produce salvage, even with a dirty baseline."""
    baseline = git_head(repository)
    (repository / "tracked.txt").write_text("pre-existing staged work\n")
    git(repository, "add", "tracked.txt")
    (repository / "untracked.txt").write_text("pre-existing untracked work\n")
    index = (repository / ".git" / "index").read_bytes()
    baseline_tree = working_tree(repository, baseline)
    assert baseline_tree != git(repository, "rev-parse", "HEAD^{tree}")
    assert salvage(repository, baseline, 1, baseline_tree=baseline_tree) is None
    (repository / "tracked.txt").write_text("provider progress\n")
    result = salvage(repository, baseline, 1, baseline_tree=baseline_tree)
    assert result is not None
    assert git(repository, "show", f"{result.ref}:tracked.txt") == "provider progress"
    assert git(repository, "show", f"{result.ref}:untracked.txt") == "pre-existing untracked work"
    assert git(repository, "rev-parse", f"{result.commit}^") == baseline
    assert git_head(repository) == baseline
    assert (repository / ".git" / "index").read_bytes() == index


@pytest.mark.parametrize("branch", ["main", "master"])
def test_writable_main_and_master_refused_with_code_7(repository, branch):
    """C-13.2 and C-6.5 writable admission on main/master is refused with a fix."""
    git(repository, "branch", "-m", branch)
    with pytest.raises(AdapterError) as exc:
        validate_writable_workdir(repository)
    assert exc.value.code == 7
    assert "task branch" in exc.value.fix


def test_private_salvage_allowed_from_main(repository):
    """C-13.2 private refs remain legal on main after the job's branch changes."""
    baseline = git_head(repository)
    git(repository, "branch", "-m", "main")
    (repository / "tracked.txt").write_text("save this work\n")
    result = salvage(repository, baseline, 1)
    assert result.ref.startswith("refs/subfleet-salvage/main-")
    assert git_head(repository) == baseline


def test_salvage_replay_is_idempotent_and_keeps_changed_snapshots(repository):
    """C-4.2 and C-13.1 finalization replay preserves each different snapshot."""
    baseline = git_head(repository)
    (repository / "tracked.txt").write_text("first snapshot\n")
    first = salvage(repository, baseline, 1, timestamp="2026-09-05T10:00:00Z")
    replay = salvage(repository, baseline, 1, timestamp="2026-09-05T10:00:00Z")
    assert replay == first
    (repository / "tracked.txt").write_text("later snapshot\n")
    changed = salvage(repository, baseline, 1, timestamp="2026-09-05T10:00:00Z")
    assert changed.ref != first.ref
    assert git(repository, "show", f"{first.ref}:tracked.txt") == "first snapshot"
    assert git(repository, "show", f"{changed.ref}:tracked.txt") == "later snapshot"


def test_salvage_readonly_job_has_no_git_side_effects(tmp_path):
    """C-13.1 read-only jobs never enter the git salvage path."""
    assert salvage(tmp_path, "not-a-commit", 1, writable=False) is None
    assert list(tmp_path.iterdir()) == []


def test_salvage_rejects_nonfinalizing_state(repository):
    """C-13.1 salvage starts only during finalization, loss or kill reconciliation."""
    with pytest.raises(ValueError, match="finalizing"):
        salvage(repository, git_head(repository), 1, state="running")
