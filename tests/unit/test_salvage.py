"""C-13 salvage snapshots are private git objects, never worktree mutations."""

import errno
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import salvage as salvage_module
from subfleet.adapters.base import AdapterError
from subfleet.salvage import (
    SalvageError, add_leftover, check_worktree, foreign_registration, git_head, salvage, validate_writable_workdir,
    working_tree, worktree_registration, worktree_registrations,
)


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


# --- C-6.8: only a finished checkout of the job's commit is reused -------------

def test_c6_8_check_worktree_finds_only_a_finished_unlocked_checkout_of_the_commit(repository, tmp_path_factory):
    """C-6.8 a `.git` link the repository lists, unlocked, at the commit, whose index holds its tree."""
    head = git_head(repository)
    worktree = tmp_path_factory.mktemp("worktrees") / "job"
    assert check_worktree(repository, worktree, head).unfinished == "no directory"
    worktree.mkdir()
    assert check_worktree(repository, worktree, head).unfinished == "no .git link"
    worktree.rmdir()
    git(repository, "worktree", "add", "--detach", str(worktree), head)
    assert check_worktree(repository, worktree, head) == ({"head": head}, None)

    git(repository, "worktree", "lock", "--reason", "initializing", str(worktree))
    check = check_worktree(repository, worktree, head)               # an add that never returned
    assert check == ({"head": head, "locked": "initializing"}, "locked (initializing)")
    git(repository, "worktree", "unlock", str(worktree))
    git(repository, "worktree", "lock", str(worktree))
    assert check_worktree(repository, worktree, head).unfinished == "locked (no reason given)"
    git(repository, "worktree", "unlock", str(worktree))

    index = worktree / git(worktree, "rev-parse", "--git-path", "index")
    index.rename(index.with_name("index.saved"))
    assert check_worktree(repository, worktree, head).unfinished == "its index is not a checkout of the commit"
    index.with_name("index.saved").rename(index)
    assert check_worktree(repository, worktree, head).unfinished is None

    (repository / "second.txt").write_text("second\n")
    git(repository, "add", "second.txt")
    git(repository, "commit", "-m", "second")
    assert check_worktree(repository, worktree, git_head(repository)).unfinished == f"HEAD is {head}"

    elsewhere = tmp_path_factory.mktemp("elsewhere")
    git(elsewhere, "init", "-b", "task/other")
    git(elsewhere, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "--allow-empty", "-m", "other")
    theirs = tmp_path_factory.mktemp("worktrees") / "theirs"
    git(elsewhere, "worktree", "add", "--detach", str(theirs), "HEAD")
    check = check_worktree(repository, theirs, git_head(elsewhere))    # another repository's worktree
    assert check == (None, "the job's repository does not list it")


def test_c6_8_check_worktree_reads_a_registration_whose_directory_is_gone(repository, tmp_path_factory):
    """C-6.8 an add stopped partway can leave git's registration, locked, with no directory."""
    head = git_head(repository)
    worktree = tmp_path_factory.mktemp("worktrees") / "job"
    git(repository, "worktree", "add", "--lock", "--reason", "initializing", "--detach", str(worktree), head)
    shutil.rmtree(worktree)
    assert check_worktree(repository, worktree, head) == ({"head": head, "locked": "initializing"}, "no directory")


def test_c6_8_a_link_at_the_path_is_not_the_worktree_it_points_to(repository, tmp_path_factory):
    """C-6.8 neither the check nor the registration follows a link in the allocated path's place."""
    head = git_head(repository)
    theirs = tmp_path_factory.mktemp("worktrees") / "theirs"
    git(repository, "worktree", "add", "--detach", str(theirs), head)
    ours = tmp_path_factory.mktemp("worktrees") / "job"
    ours.symlink_to(theirs)
    listing = subprocess.run(["git", "-C", str(repository), "worktree", "list", "--porcelain", "-z"],
                             check=True, capture_output=True, text=True).stdout
    assert worktree_registration(listing, ours) is None
    assert worktree_registration(listing, theirs) == {"head": head}
    assert check_worktree(repository, ours, head) == (None, "no .git link")


def test_c6_8_check_worktree_raises_when_git_does_not_answer(monkeypatch, tmp_path):
    """C-6.8 a listing that did not finish is not "not a worktree": it raises, transient."""
    (tmp_path / ".git").write_text("gitdir: /nowhere\n")
    _stub_run(monkeypatch, subprocess.TimeoutExpired(["git"], 5))
    with pytest.raises(SalvageError) as error:
        check_worktree(tmp_path, tmp_path, "0" * 40, timeout_s=5)
    assert error.value.transient and "timed out after 5 s" in str(error.value)


def test_c6_8_check_worktree_raises_when_the_repository_cannot_be_read(tmp_path):
    """C-6.8 a listing git could not give is not "not registered", which is what lets a directory go."""
    worktree = tmp_path / "job"
    worktree.mkdir()
    with pytest.raises(SalvageError) as error:
        check_worktree(tmp_path / "gone", worktree, "0" * 40)
    assert not error.value.transient and str(error.value).startswith("git worktree failed: ")


def test_c6_8_a_listing_is_read_by_nul_fields_and_one_path_has_one_entry():
    """C-6.8 `-z` keeps a path with a newline whole, and lock reasons as written; two entries for one path raise."""
    listing = ("worktree /repo\0HEAD " + "a" * 40 + "\0branch refs/heads/x\0\0"
               "worktree /w/odd\nworktree /w/job\0HEAD " + "b" * 40 + "\0detached\0locked kept for review\0\0"
               "worktree /w/job\0HEAD " + "c" * 40 + "\0detached\0locked\0\0")
    assert worktree_registrations(listing) == [
        ("/repo", {"head": "a" * 40}),
        ("/w/odd\nworktree /w/job", {"head": "b" * 40, "locked": "kept for review"}),
        ("/w/job", {"head": "c" * 40, "locked": ""}),
    ]
    assert worktree_registration(listing, "/w/odd\nworktree /w/job") == {"head": "b" * 40, "locked": "kept for review"}
    with pytest.raises(SalvageError, match="lists 2 worktrees"):
        worktree_registration(listing + "worktree /w/job\0HEAD " + "d" * 40 + "\0\0", "/w/job")


@pytest.mark.parametrize("entry,reason", [
    (None, None),
    ({"head": "c" * 40}, None),
    ({"head": "c" * 40, "locked": "initializing"}, None),
    ({"head": "0" * 40, "locked": "initializing"}, None),
    ({"head": "0" * 40}, "its HEAD is " + "0" * 40 + ", not " + "c" * 40),
    ({}, "its HEAD is unreadable, not " + "c" * 40),
    ({"head": "d" * 40, "locked": "initializing"}, "its HEAD is " + "d" * 40 + ", not " + "c" * 40),
    ({"head": "c" * 40, "locked": "kept for review"}, "it is locked (kept for review)"),
    ({"head": "c" * 40, "locked": ""}, "it is locked (no reason given)"),
])
def test_c6_8_a_registration_is_an_adds_only_unlocked_or_under_its_lock_at_the_commit(entry, reason):
    """C-6.8 zeros name no commit only under git's own lock: a HEAD git cannot read reads as zeros too."""
    assert foreign_registration(entry, "c" * 40) == reason


@pytest.fixture
def unfinished_add(repository, tmp_path_factory):
    """What `git worktree add` leaves when it stops after its `.git` link: registered, locked, no index."""
    head = git_head(repository)
    worktree = tmp_path_factory.mktemp("worktrees") / "job"
    git(repository, "worktree", "add", "--lock", "--reason", "initializing", "--detach", str(worktree), head)
    admin = Path(git(worktree, "rev-parse", "--absolute-git-dir"))
    (admin / "index").unlink()
    (worktree / "tracked.txt").write_text("base")                   # stopped mid-file
    return SimpleNamespace(repository=repository, worktree=worktree, head=head, admin=admin,
                           entry={"head": head, "locked": "initializing"})


def leftover(add, **changes):
    return add_leftover(add.repository, add.worktree, add.head, changes.get("entry", add.entry))


def test_c6_8_an_unfinished_adds_leftover_is_recognised(unfinished_add):
    """C-6.8 under the add's lock: its own `.git` link and nothing but the commit's paths, contents not compared."""
    assert leftover(unfinished_add) is None
    (unfinished_add.worktree / ".git").unlink()                     # a removal the checkout raced
    assert leftover(unfinished_add) is None
    shutil.rmtree(unfinished_add.worktree)
    assert leftover(unfinished_add) is None                         # only the registration
    unfinished_add.worktree.mkdir()
    assert leftover(unfinished_add, entry=None) is None             # an empty directory holds nothing
    unfinished_add.worktree.rmdir()
    unfinished_add.worktree.symlink_to(unfinished_add.repository)
    assert leftover(unfinished_add, entry=None) is None             # a link is unlinked, never followed


def test_c6_8_an_unfinished_add_holding_anything_else_is_kept(unfinished_add, tmp_path_factory):
    """C-6.8 a file the commit does not have, a staged change, or a `.git` that is not this registration's."""
    add = unfinished_add
    (add.worktree / "notes.txt").write_text("someone's\n")
    assert leftover(add) == f"it holds notes.txt, which {add.head} does not"
    (add.worktree / "notes.txt").unlink()
    (add.worktree / "nested").mkdir()
    (add.worktree / "nested" / ".git").mkdir()
    assert leftover(add) == f"it holds nested, which {add.head} does not"
    shutil.rmtree(add.worktree / "nested")

    git(add.worktree, "read-tree", add.head)
    assert leftover(add) is None                                    # an index of the commit stages nothing
    (add.worktree / "new.txt").write_text("staged\n")
    git(add.worktree, "add", "new.txt")
    (add.worktree / "new.txt").unlink()
    assert leftover(add) == "its index stages new.txt"
    git(add.worktree, "rm", "--cached", "--quiet", "new.txt")
    git(add.worktree, "rm", "--cached", "--quiet", "tracked.txt")
    assert leftover(add) is None                                    # a deletion takes nothing the commit lacks

    link = add.worktree / ".git"
    original = link.read_text()
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    git(elsewhere, "init", "-q")
    link.write_text(f"gitdir: {elsewhere / '.git'}\n")
    assert leftover(add) == "its .git names another repository or worktree"
    other = tmp_path_factory.mktemp("worktrees") / "other"
    git(add.repository, "worktree", "add", "--detach", str(other), add.head)
    link.write_text((other / ".git").read_text())
    assert leftover(add) == "its .git names another repository or worktree"
    link.unlink()
    link.mkdir()
    assert leftover(add) == "its .git is not a worktree link"
    link.rmdir()
    link.write_text(original)
    assert leftover(add) is None


def test_c6_8_leftover_checks_follow_relative_worktree_links(repository, tmp_path_factory):
    """C-6.8 `worktree.useRelativePaths` writes both links relative to their own directories."""
    head = git_head(repository)
    worktree = tmp_path_factory.mktemp("worktrees") / "job"
    git(repository, "-c", "worktree.useRelativePaths=true", "worktree", "add", "--lock", "--reason", "initializing",
        "--detach", str(worktree), head)
    assert not (worktree / ".git").read_text().startswith("gitdir: /")
    assert add_leftover(repository, worktree, head, {"head": head, "locked": "initializing"}) is None


def test_c6_8_what_is_not_under_an_adds_lock_is_kept(repository, tmp_path_factory):
    """C-6.8 files with no registration, or under a finished one; a file in the directory's place; unreadable parts."""
    head = git_head(repository)
    worktree = tmp_path_factory.mktemp("worktrees") / "job"
    worktree.mkdir()
    (worktree / "tracked.txt").write_text("baseline\n")
    kept = "it holds files, and no add that never finished (git's `initializing` lock) names it"
    assert add_leftover(repository, worktree, head, None) == kept
    assert add_leftover(repository, worktree, head, {"head": head}) == kept
    worktree.chmod(0)
    try:
        assert add_leftover(repository, worktree, head, {"head": head, "locked": "initializing"}) == (
            "it cannot be read (Permission denied)")
    finally:
        worktree.chmod(0o700)
    shutil.rmtree(worktree)
    worktree.write_text("someone's\n")
    assert add_leftover(repository, worktree, head, None) == "it is not a directory"


def test_c6_8_a_leftover_without_its_link_still_has_its_index_read(unfinished_add):
    """C-6.8 with `.git` gone, the registration's own admin directory is found and its index read."""
    add = unfinished_add
    git(add.worktree, "read-tree", add.head)
    (add.worktree / "tracked.txt").write_text("staged edit\n")
    git(add.worktree, "add", "tracked.txt")
    assert leftover(add) == "its index stages tracked.txt"            # a staged modification, not only an addition
    (add.worktree / ".git").unlink()
    assert leftover(add) == "its index stages tracked.txt"
    (add.admin / "gitdir").write_text("/somewhere/else/.git\n")
    assert leftover(add) == "no registration of the job's repository can be told apart as its own"


def test_c6_8_a_linked_dot_git_is_not_a_worktree_link(unfinished_add):
    """C-6.8 `.git` as a link, even to a file that says the right thing, is not what an add writes."""
    add = unfinished_add
    link = add.worktree / ".git"
    copy = add.worktree.with_name("dot-git")
    copy.write_text(link.read_text())
    link.unlink()
    link.symlink_to(copy)
    assert leftover(add) == "its .git is not a worktree link"


def test_c6_8_each_path_must_be_of_the_kind_the_commit_has_there(repository, tmp_path_factory):
    """C-6.8 a file where the commit has a directory, or a link where it has a file, is not the checkout's."""
    (repository / "d").mkdir()
    (repository / "d" / "b.txt").write_text("b\n")
    git(repository, "add", "d")
    git(repository, "commit", "-m", "a directory")
    head = git_head(repository)
    worktree = tmp_path_factory.mktemp("worktrees") / "job"
    git(repository, "worktree", "add", "--lock", "--reason", "initializing", "--detach", str(worktree), head)
    entry = {"head": head, "locked": "initializing"}
    assert add_leftover(repository, worktree, head, entry) is None
    shutil.rmtree(worktree / "d")
    (worktree / "d").write_text("someone's\n")
    assert add_leftover(repository, worktree, head, entry) == f"it holds d as a file, where {head} has a directory"
    (worktree / "d").unlink()
    (worktree / "tracked.txt").unlink()
    (worktree / "tracked.txt").symlink_to("/etc/hosts")
    assert add_leftover(repository, worktree, head, entry) == f"it holds tracked.txt as a link, where {head} has a file"


def _case_insensitive(directory):
    probe = directory / "Case-Probe"
    probe.write_text("")
    try:
        return (directory / "case-probe").exists()
    finally:
        probe.unlink()


def test_c6_8_a_registration_is_found_as_the_filesystem_names_it(repository, tmp_path_factory):
    """C-6.8 on a case-insensitive volume git finds a worktree by any case of its path, and so must the check.

    A worktree registered as `WT/Job` is the one at `wt/job`: its lock is
    seen, so it is kept rather than removed by a `git worktree remove` that
    would find it anyway.
    """
    base = tmp_path_factory.mktemp("cases")
    if not _case_insensitive(base):
        pytest.skip("the volume compares names by case")
    head = git_head(repository)
    (base / "wt").mkdir()
    git(repository, "worktree", "add", "--detach", str(base / "WT" / "Job"), head)
    git(repository, "worktree", "lock", "--reason", "keep", str(base / "WT" / "Job"))
    check = check_worktree(repository, base / "wt" / "job", head)
    assert check.registration == {"head": head, "locked": "keep"}
    assert add_leftover(repository, base / "wt" / "job", head, check.registration) == "it is locked (keep)"
