"""C-13 salvage snapshots are private git objects, never worktree mutations."""

import errno
import subprocess
from pathlib import Path

import pytest

from subfleet import salvage as salvage_module
from subfleet.adapters.base import AdapterError
from subfleet.salvage import SalvageError, git_head, salvage, validate_writable_workdir, working_tree


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


# --- C-6.8: a git call that did not finish is never read as an answer ----------

def _stub_run(monkeypatch, outcome):
    seen = {}

    def run(cmd, **kwargs):
        seen.update(cmd=cmd, timeout=kwargs.get("timeout"))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    monkeypatch.setattr("subfleet.salvage.subprocess.run", run)
    return seen


@pytest.mark.parametrize("probe", [salvage_module.git_head, salvage_module.git_branch])
def test_c6_8_timeout_raises_transient_even_for_an_optional_probe(monkeypatch, tmp_path, probe):
    """C-6.8 "no HEAD" and "no branch" are answers; a timeout is not one."""
    _stub_run(monkeypatch, subprocess.TimeoutExpired(["git"], 7))
    with pytest.raises(SalvageError) as error:
        probe(tmp_path, timeout_s=7)
    assert error.value.transient and "timed out after 7 s" in str(error.value)
    assert isinstance(error.value.__cause__, subprocess.TimeoutExpired)


def test_c6_8_timeout_never_lets_a_writable_job_past_the_main_refusal(monkeypatch, tmp_path):
    """C-6.8, C-13.2 the branch check fails closed when git does not answer."""
    _stub_run(monkeypatch, subprocess.TimeoutExpired(["git"], 1))
    with pytest.raises(SalvageError):
        validate_writable_workdir(tmp_path, timeout_s=1)


def test_c6_8_transient_oserror_is_transient_and_others_keep_their_meaning(monkeypatch, tmp_path):
    """C-6.8 EAGAIN waits; a missing git binary is still "no HEAD" for an optional probe."""
    _stub_run(monkeypatch, OSError(errno.EAGAIN, "Resource temporarily unavailable"))
    with pytest.raises(SalvageError) as error:
        git_head(tmp_path)
    assert error.value.transient
    _stub_run(monkeypatch, FileNotFoundError(errno.ENOENT, "No such file or directory", "git"))
    assert git_head(tmp_path) is None
    with pytest.raises(SalvageError) as error:
        working_tree(tmp_path, "HEAD")
    assert not error.value.transient and "FileNotFoundError" in str(error.value)


def test_c6_8_a_failed_git_command_is_not_transient(repository):
    """C-6.8 git answering "bad object" says something about the repository."""
    with pytest.raises(SalvageError) as error:
        working_tree(repository, "0" * 40)
    assert not error.value.transient and "read-tree failed" in str(error.value)


def test_c6_8_cap_is_the_argument_then_the_environment_then_sixty(monkeypatch, tmp_path):
    """C-6.8 the default is 60 s, `SUBFLEET_GIT_TIMEOUT_S` overrides it, an argument overrides both."""
    done = subprocess.CompletedProcess(["git"], 0, "abc\n", "")
    monkeypatch.delenv(salvage_module.GIT_TIMEOUT_ENV, raising=False)
    seen = _stub_run(monkeypatch, done)
    assert git_head(tmp_path) == "abc" and seen["timeout"] == 60
    monkeypatch.setenv(salvage_module.GIT_TIMEOUT_ENV, "240")
    git_head(tmp_path)
    assert seen["timeout"] == 240
    git_head(tmp_path, timeout_s=3)
    assert seen["timeout"] == 3
    for junk in ("", "soon", "0", "-5"):
        monkeypatch.setenv(salvage_module.GIT_TIMEOUT_ENV, junk)
        git_head(tmp_path)
        assert seen["timeout"] == 60


def test_c6_8_salvage_threads_its_cap_through_every_git_call(monkeypatch, repository):
    """C-6.8 finalization's snapshot runs under the same configurable cap."""
    baseline = git_head(repository)
    (repository / "tracked.txt").write_text("changed\n")
    real_run, caps = subprocess.run, []

    def run(cmd, **kwargs):
        caps.append(kwargs.get("timeout"))
        return real_run(cmd, **kwargs)
    monkeypatch.setattr("subfleet.salvage.subprocess.run", run)
    assert salvage(repository, baseline, 1, timestamp="2026-09-20T10:00:00Z", timeout_s=42)
    assert caps and set(caps) == {42}


def test_c6_8_git_tree_names_a_commits_tree_or_nothing(repository):
    """C-6.8 the read-only baseline: a tree hash, or None when git cannot name one."""
    head = git_head(repository)
    assert salvage_module.git_tree(repository, head) == git(repository, "rev-parse", f"{head}^{{tree}}")
    assert salvage_module.git_tree(repository, "0" * 40) is None
