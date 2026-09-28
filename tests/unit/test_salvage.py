"""C-13 salvage snapshots are private git objects, never worktree mutations."""

import errno
import os
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
    assert git(repository, "rev-parse", f"{result.commit}^2") == current_head
    assert git_head(repository) == current_head


def test_salvage_preserves_history_even_when_files_return_to_baseline(repository):
    """C-13.1 commits and their messages survive even after a committed revert."""
    baseline = git_head(repository)
    git(repository, "checkout", "--detach")
    (repository / "tracked.txt").write_text("intermediate work\n")
    git(repository, "commit", "-am", "an important intermediate step")
    intermediate = git_head(repository)
    git(repository, "revert", "--no-edit", intermediate)
    head = git_head(repository)
    result = salvage(repository, baseline, 1, timestamp="2026-09-05T10:00:00Z")
    assert result is not None
    assert git(repository, "rev-parse", result.commit + "^2") == head
    assert git(repository, "merge-base", "--is-ancestor", intermediate, result.ref) == ""
    assert salvage(repository, baseline, 1, timestamp="2026-09-05T10:00:00Z") == result


def test_salvage_replay_with_same_files_preserves_new_commits(repository):
    """A repeated snapshot cannot hide new history merely because its tree matches."""
    baseline = git_head(repository)
    git(repository, "checkout", "--detach")
    (repository / "tracked.txt").write_text("snapshot\n")
    first = salvage(repository, baseline, 1, timestamp="2026-09-05T10:00:00Z")
    git(repository, "commit", "-am", "commit previously dirty files")
    head = git_head(repository)
    second = salvage(repository, baseline, 1, timestamp="2026-09-05T10:00:00Z")
    assert first.tree == second.tree and first.ref != second.ref
    assert git(repository, "rev-parse", second.commit + "^2") == head
    assert salvage(repository, baseline, 1, timestamp="2026-09-05T10:00:00Z") == second


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


def _scratch_tree(path, baseline):
    """The snapshot as read into an empty index: every tracked file hashed."""
    index = path / ".git" / "scratch-index"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    try:
        for args in (("read-tree", baseline), ("add", "-A")):
            subprocess.run(["git", "-C", str(path), *args], env=env, check=True,
                           capture_output=True)
        return subprocess.run(["git", "-C", str(path), "write-tree"], env=env, check=True,
                              capture_output=True, text=True).stdout.strip()
    finally:
        index.unlink(missing_ok=True)


def _dirty(repository):
    """Every kind of change a snapshot must record over its baseline."""
    (repository / "kept.txt").write_text("unchanged\n")
    (repository / "gone.txt").write_text("to be deleted\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "more files")
    (repository / "tracked.txt").write_text("modified, unstaged\n")
    (repository / "staged.txt").write_text("new and staged\n")
    git(repository, "add", "staged.txt")
    (repository / "gone.txt").unlink()
    (repository / "untracked.txt").write_text("new, untracked\n")
    (repository / "ignored.txt").write_text("ignored\n")


def test_c6_8_seeded_snapshot_equals_the_scratch_snapshot(repository):
    """Seeding from the real index changes what is hashed, never the tree."""
    _dirty(repository)
    baseline = git_head(repository)
    index = (repository / ".git" / "index").read_bytes()
    assert working_tree(repository, baseline) == _scratch_tree(repository, baseline)
    assert (repository / ".git" / "index").read_bytes() == index


def test_c6_8_snapshot_against_an_older_baseline_equals_the_scratch_snapshot(repository):
    """read-tree -m keeps stat data only where content matches the baseline."""
    older = git_head(repository)
    _dirty(repository)
    assert working_tree(repository, older) == _scratch_tree(repository, older)


def test_c6_8_snapshot_without_a_real_index_reads_the_baseline(repository):
    _dirty(repository)
    baseline = git_head(repository)
    expected = _scratch_tree(repository, baseline)
    saved = repository / ".git" / "index.saved"
    (repository / ".git" / "index").rename(saved)
    try:
        assert working_tree(repository, baseline) == expected
    finally:
        saved.rename(repository / ".git" / "index")


def test_c6_8_snapshot_with_unmerged_entries_reads_the_baseline(repository):
    """read-tree -m refuses an index with conflicts; the scratch read does not."""
    git(repository, "checkout", "-b", "other")
    (repository / "tracked.txt").write_text("other side\n")
    git(repository, "commit", "-am", "other")
    git(repository, "checkout", "task/example")
    (repository / "tracked.txt").write_text("this side\n")
    git(repository, "commit", "-am", "this")
    subprocess.run(["git", "-C", str(repository), "merge", "other"], capture_output=True)
    assert "UU tracked.txt" in git(repository, "status", "--porcelain")
    baseline = git_head(repository)
    index = (repository / ".git" / "index").read_bytes()
    assert working_tree(repository, baseline) == _scratch_tree(repository, baseline)
    assert (repository / ".git" / "index").read_bytes() == index


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root reads unreadable files")
def test_c6_8_seeded_snapshot_does_not_reread_unchanged_files(repository):
    """The speed-up, made observable: an unchanged file the snapshot cannot
    read is still recorded from the real index's stat data. The scratch
    read has to hash it and fails (incident: 2026-09-23, axiom-corpus, 59,822
    tracked files and 14 GB; the scratch snapshot took 50 s warm and timed
    out at the 60 s cap eight times under load, so no writable job could be
    admitted there)."""
    git(repository, "config", "core.trustctime", "false")  # chmod moves ctime only
    baseline = git_head(repository)
    unreadable = repository / "tracked.txt"
    # Older than the index, so git does not treat it as racily clean (an
    # entry as new as its index is hashed again, by design).
    hour_ago = unreadable.stat().st_mtime - 3600
    os.utime(unreadable, (hour_ago, hour_ago))
    git(repository, "update-index", "--refresh")
    expected = git(repository, "rev-parse", "HEAD^{tree}")
    unreadable.chmod(0)
    try:
        assert working_tree(repository, baseline) == expected
        with pytest.raises(subprocess.CalledProcessError):
            _scratch_tree(repository, baseline)
    finally:
        unreadable.chmod(0o644)


@pytest.mark.parametrize("from_subdirectory", [False, True])
@pytest.mark.parametrize("marks", [("--assume-unchanged",), ("--skip-worktree",),
                                   ("--assume-unchanged", "--skip-worktree")])
def test_c6_8_snapshot_records_paths_the_real_index_marks_unchanged(repository, marks,
                                                                    from_subdirectory):
    """A real index's assume-unchanged and skip-worktree bits must not carry
    into the seeded snapshot: add -A skips such paths, and the empty-index
    read (no bits) records their edits and deletions."""
    (repository / "local.cfg").write_text("original\n")
    (repository / "gone.cfg").write_text("original\n")
    (repository / "sub").mkdir()
    (repository / "sub" / "file.txt").write_text("in the job's directory\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "config files")
    for mark in marks:
        git(repository, "update-index", mark, "local.cfg", "gone.cfg")
    (repository / "local.cfg").write_text("edited while marked\n")
    (repository / "gone.cfg").unlink()
    baseline = git_head(repository)
    index = (repository / ".git" / "index").read_bytes()
    # An in-place job may run from a subdirectory; the marks outside it count.
    workdir = repository / "sub" if from_subdirectory else repository
    tree = working_tree(workdir, baseline)
    assert tree == _scratch_tree(repository, baseline)
    assert git(repository, "show", f"{tree}:local.cfg") == "edited while marked"
    assert "gone.cfg" not in git(repository, "ls-tree", "--name-only", tree)
    assert (repository / ".git" / "index").read_bytes() == index


def test_c6_8_snapshot_keeps_the_real_index_mtime_for_racy_entries(repository):
    """An edit that keeps size, mtime and inode is caught only by git's racy
    check (an entry as new as its index is hashed again). The copy must keep
    the real index's mtime for that check to fire."""
    git(repository, "config", "core.trustctime", "false")
    path = repository / "racy.txt"
    path.write_text("aaaa\n")
    old = path.stat().st_mtime_ns - 3_600 * 10**9
    os.utime(path, ns=(old, old))           # older than the index: not smudged
    git(repository, "add", "racy.txt")
    git(repository, "commit", "-m", "racy file")
    baseline = git_head(repository)
    path.write_text("bbbb\n")               # same size, same inode
    os.utime(path, ns=(old, old))           # same mtime
    os.utime(repository / ".git" / "index", ns=(old, old))  # index as old as the entry
    tree = working_tree(repository, baseline)
    assert git(repository, "show", f"{tree}:racy.txt") == "bbbb"



def test_a_checkout_whose_name_ends_in_a_space_is_found(tmp_path):
    """C-6.5: git's output loses only its line end, so a top-level directory named
    `repo ` is that directory, not one that does not exist."""
    from subfleet.salvage import git_toplevel
    repo = tmp_path / "repo "
    (repo / "pkg").mkdir(parents=True)
    git(repo, "init", "-q")
    top = git_toplevel(repo / "pkg")
    assert top == os.path.realpath(repo) and os.path.isdir(top)
    assert git_toplevel(tmp_path) is None
