"""C-13 salvage snapshots are private git objects, never worktree mutations."""

import contextlib
import errno
import itertools
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import salvage as salvage_module
from subfleet.adapters.base import AdapterError
from subfleet.salvage import (
    SalvageError, git_head, salvage, sparse_checkout, validate_writable_workdir, working_tree,
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



def test_a_checkout_whose_name_ends_in_a_space_is_found(tmp_path):
    """C-6.5: git's output loses only its line end, so a top-level directory named
    `repo ` is that directory, not one that does not exist."""
    from subfleet.salvage import git_toplevel
    repo = tmp_path / "repo "
    (repo / "pkg").mkdir(parents=True)
    git(repo, "init", "-q")
    # TMPDIR may be inside the caller's checkout. A bare parent has no worktree
    # and stops discovery there; the nested checkout still resolves normally.
    git(tmp_path, "init", "--bare", "-q")
    top = git_toplevel(repo / "pkg")
    assert top == os.path.realpath(repo) and os.path.isdir(top)
    assert git_toplevel(tmp_path) is None


# --- C-13.1 in a sparse checkout ------------------------------------------------
#
# A sparse checkout keeps the files its patterns leave out off disk. The tests
# cut a sparse worktree the way chief-of-staff's README says to (that
# repository tracks 330,000 state files, 15 GB as a full checkout), and where
# they compare, a full worktree from the same commit. The snapshot of the
# sparse one must be the full one's with the same edits: nothing left out is
# read as deleted, and whatever the job wrote, in the patterns or outside
# them, is recorded.

SPARSE_LAYOUT = {
    "README.md": b"top\n",
    ".gitignore": b"ignored.txt\n",
    "bin/tool": b"#!/bin/sh\necho tool\n",
    "bin/lib/helper.py": b"VALUE = 1\n",
    "tests/test_tool.py": b"def test_tool(): pass\n",
    "state/.gitignore": b"*.tmp\n",
    "state/a.json": b'{"a": 1}\n',
    "state/b.json": b'{"b": 2}\n',
    "state/deep/c.json": b'{"c": 3}\n',
    "notes/todo.md": b"todo\n",
}
ON_DISK = [".gitignore", "README.md", "bin/lib/helper.py", "bin/tool", "tests/test_tool.py"]
LEFT_OUT = ["notes/todo.md", "state/.gitignore", "state/a.json", "state/b.json", "state/deep/c.json"]
#: The arguments of `git sparse-checkout set`: chief-of-staff's own recipe
#: (patterns, no cone), a cone, and a cone whose index holds a directory in
#: place of the files under it (a "sparse index").
SPARSE_MODES = {
    "patterns": ("--no-cone", "/bin/", "/tests/", "/README.md", "/.gitignore"),
    "cone": ("--cone", "bin", "tests"),
    "cone-sparse-index": ("--cone", "--sparse-index", "bin", "tests"),
}
IDENTITY = ("-c", "user.name=Test User", "-c", "user.email=test@example.invalid")


def _layout_repo(path, layout):
    """A repository on `task/source` holding one commit of ``layout`` (path to bytes)."""
    path.mkdir(parents=True)
    git(path, "init", "-b", "task/source")
    for name, body in layout.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    git(path, "add", "-A")
    git(path, *IDENTITY, "commit", "-m", "baseline")
    return path


def _cut(repo, target, sparse=None, *, branch=None):
    """A linked worktree of ``repo`` at its head, on ``branch`` or detached. With
    ``sparse`` (the arguments of `git sparse-checkout set`) it is cut as
    chief-of-staff's README says: no checkout, the patterns, then the checkout."""
    name = ("-b", branch) if branch else ("--detach",)
    if sparse is None:
        git(repo, "worktree", "add", *name, str(target), "HEAD")
        return target
    git(repo, "worktree", "add", "--no-checkout", *name, str(target), "HEAD")
    git(target, "sparse-checkout", "set", *sparse)
    git(target, "checkout", *((branch,) if branch else ("--detach", "HEAD")))
    return target


def _files(worktree):
    return sorted(path.relative_to(worktree).as_posix() for path in Path(worktree).rglob("*")
                  if ".git" not in path.relative_to(worktree).parts[:1]
                  and (path.is_file() or path.is_symlink()))


def _tree_files(repo, tree):
    """Path to blob for every file of ``tree``."""
    listed = git(repo, "ls-tree", "-r", "-z", tree)
    return {entry.split("\t", 1)[1]: entry.split()[2] for entry in listed.split("\0") if entry}


def _changes(repo, old, new):
    """`diff-tree`'s name and status lines between two trees, sorted."""
    return sorted(git(repo, "diff-tree", "-r", "--name-status", "--no-renames", old, new).splitlines())


def _real_index(worktree):
    return Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "index"))


def _off_disk_paths(worktree):
    """The paths `_off_disk` says the sparse checkout keeps off disk, in index order."""
    return [os.fsdecode(path) for _, _, path in salvage_module._off_disk(worktree)]


def _git_add_all(worktree, *flags):
    """git on its own: HEAD read into an empty index, then `add -A` with ``flags``.
    Returns add's exit status, its stderr, and the tree the index then holds."""
    with tempfile.TemporaryDirectory() as temporary:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        subprocess.run(["git", "-C", str(worktree), "read-tree", "HEAD"], env=env, check=True,
                       capture_output=True)
        added = subprocess.run(["git", "-C", str(worktree), "add", "-A", *flags], env=env,
                               capture_output=True, text=True)
        tree = subprocess.run(["git", "-C", str(worktree), "write-tree"], env=env, check=True,
                              capture_output=True, text=True).stdout.strip()
    return added.returncode, added.stderr, tree


@contextlib.contextmanager
def _empty_index_read():
    """Make every snapshot take the read into an empty index (no seed)."""
    real = salvage_module._seed_index
    salvage_module._seed_index = lambda *args, **kwargs: False
    try:
        yield
    finally:
        salvage_module._seed_index = real


def _both_reads(worktree, baseline):
    """The snapshot read both ways: they must agree (C-6.8), so one tree."""
    seeded = working_tree(worktree, baseline)
    with _empty_index_read():
        assert working_tree(worktree, baseline) == seeded
    return seeded


@pytest.fixture(params=sorted(SPARSE_MODES))
def sparse_pair(tmp_path, request):
    """One repository; a sparse worktree and a full one cut from the same commit."""
    repo = _layout_repo(tmp_path / "repo", SPARSE_LAYOUT)
    sparse = _cut(repo, tmp_path / "sparse", SPARSE_MODES[request.param], branch="task/sparse")
    full = _cut(repo, tmp_path / "full", branch="task/full")
    assert sparse_checkout(sparse) and not sparse_checkout(full)
    assert _files(sparse) == ON_DISK and _files(full) == sorted(SPARSE_LAYOUT)
    return repo, sparse, full


def test_c13_1_sparse_an_untracked_file_outside_the_patterns_is_recorded(sparse_pair):
    """C-13.1, C-6.8 the 2026-10-09 failure: a review's scratch file outside the
    patterns failed the snapshot, and with it the writable job's preparation
    ("git add failed: The following paths and/or pathspecs matched paths that
    exist outside of your sparse-checkout definition")."""
    repo, sparse, _ = sparse_pair
    (sparse / ".review-scratch" / "opus").mkdir(parents=True)
    (sparse / ".review-scratch" / "opus" / "baseline.txt").write_text("scratch\n")
    baseline = git_head(sparse)
    tree = _both_reads(sparse, baseline)
    assert _changes(repo, f"{baseline}^{{tree}}", tree) == ["A\t.review-scratch/opus/baseline.txt"]
    result = salvage(sparse, baseline, 1, timestamp="2026-10-09T13:33:38Z")
    assert result.ref == "refs/subfleet-salvage/task-sparse-20261009T133338Z-a1" and result.tree == tree
    assert git(repo, "show", f"{result.ref}:.review-scratch/opus/baseline.txt") == "scratch"
    # Every file the patterns leave out is in the salvage commit, byte for byte.
    kept = _tree_files(repo, result.commit)
    assert {name: kept[name] for name in LEFT_OUT} == {
        name: git(repo, "rev-parse", f"{baseline}:{name}") for name in LEFT_OUT}


def test_c13_1_sparse_a_worktree_nobody_edited_snapshots_to_its_baseline(sparse_pair):
    """C-13.1 nothing changed: no file left out is read as deleted, and there is no salvage ref."""
    repo, sparse, _ = sparse_pair
    baseline = git_head(sparse)
    assert not (sparse / "state").exists() and not (sparse / "notes").exists()
    assert _both_reads(sparse, baseline) == git(sparse, "rev-parse", "HEAD^{tree}")
    assert salvage(sparse, baseline, 1) is None
    with _empty_index_read():
        assert salvage(sparse, baseline, 1) is None
    assert git(repo, "for-each-ref", "refs/subfleet-salvage/") == ""


def test_c13_1_sparse_git_add_refuses_an_untracked_file_outside_the_patterns(sparse_pair):
    """What git does on its own, which the snapshot is built around: plain `add -A`
    exits 1 on an untracked file outside the patterns."""
    _, sparse, _ = sparse_pair
    (sparse / "scratch").mkdir()
    (sparse / "scratch" / "note.txt").write_text("note\n")
    status, stderr, _ = _git_add_all(sparse)
    assert status == 1 and "outside of your sparse-checkout definition" in stderr
    assert "scratch/note.txt" in stderr


def test_c13_1_sparse_git_add_passes_over_a_tracked_file_written_outside_the_patterns(sparse_pair):
    """What git does on its own: plain `add -A` succeeds and records nothing for a
    tracked file written outside the patterns, though `git status` shows the edit."""
    _, sparse, _ = sparse_pair
    (sparse / "state").mkdir()
    (sparse / "state" / "a.json").write_text('{"a": "the job wrote this"}\n')
    assert git(sparse, "status", "--porcelain").split() == ["M", "state/a.json"]
    status, _, tree = _git_add_all(sparse)
    assert status == 0 and tree == git(sparse, "rev-parse", "HEAD^{tree}")


def test_c13_1_sparse_git_add_sparse_alone_reads_every_file_left_out_as_deleted(sparse_pair):
    """What git does on its own, and why `--sparse` is not the whole fix: over a
    baseline with no skip-worktree bits, `add -A --sparse` in a worktree nobody
    edited records each file the patterns keep off disk as deleted."""
    repo, sparse, _ = sparse_pair
    status, _, tree = _git_add_all(sparse, "--sparse")
    assert status == 0
    assert _changes(repo, "HEAD^{tree}", tree) == [f"D\t{name}" for name in LEFT_OUT]


def test_c13_1_sparse_a_tracked_file_written_outside_the_patterns_is_recorded(sparse_pair):
    """C-13.1 work the old snapshot lost without a word: a tracked file the job wrote
    where the patterns keep none."""
    repo, sparse, _ = sparse_pair
    (sparse / "state").mkdir()
    (sparse / "state" / "a.json").write_text('{"a": "the job wrote this"}\n')
    baseline = git_head(sparse)
    tree = _both_reads(sparse, baseline)
    assert _changes(repo, f"{baseline}^{{tree}}", tree) == ["M\tstate/a.json"]
    assert git(repo, "show", f"{tree}:state/a.json") == '{"a": "the job wrote this"}'


def _edit(worktree):
    """The same work in a sparse worktree and a full one: every kind of change in
    the patterns, and new and tracked files outside them."""
    (worktree / "bin" / "tool").write_text("#!/bin/sh\necho changed\n")
    (worktree / "bin" / "lib" / "helper.py").unlink()
    (worktree / "tests" / "test_new.py").write_text("def test_new(): pass\n")
    (worktree / "README.md").chmod(0o755)
    (worktree / "bin" / "ignored.txt").write_text("ignored in the patterns\n")
    (worktree / "reports").mkdir()
    (worktree / "reports" / "out.md").write_text("report\n")
    (worktree / "reports" / "ignored.txt").write_text("ignored outside the patterns\n")
    (worktree / "state" / "deep").mkdir(parents=True, exist_ok=True)
    (worktree / "state" / "b.json").write_text('{"b": "rewritten"}\n')
    (worktree / "state" / "deep" / "new.json").write_text("{}\n")
    # `state/.gitignore` ignores this. In the sparse worktree that file is off disk,
    # and git reads the rule from its index entry while the entry is skip-worktree.
    (worktree / "state" / "scratch.tmp").write_text("ignored by a file left out\n")


EDITED = ["A\treports/out.md", "A\tstate/deep/new.json", "A\ttests/test_new.py",
          "D\tbin/lib/helper.py", "M\tREADME.md", "M\tbin/tool", "M\tstate/b.json"]


def test_c13_1_sparse_edits_in_and_outside_the_patterns_equal_a_full_checkouts(sparse_pair):
    """C-13.1 the salvage of a sparse worktree is a full checkout's with the same edits."""
    repo, sparse, full = sparse_pair
    baseline = git_head(sparse)
    for worktree in (sparse, full):
        _edit(worktree)
    assert _both_reads(sparse, baseline) == working_tree(full, baseline)
    ours = salvage(sparse, baseline, 1, timestamp="2026-10-09T10:00:00Z")
    theirs = salvage(full, baseline, 1, timestamp="2026-10-09T10:00:00Z")
    assert ours.tree == theirs.tree and ours.ref != theirs.ref
    assert _changes(repo, baseline, ours.commit) == sorted(EDITED)
    kept = _tree_files(repo, ours.tree)
    for name in ("notes/todo.md", "state/.gitignore", "state/a.json", "state/deep/c.json"):
        assert kept[name] == git(repo, "rev-parse", f"{baseline}:{name}")
    assert git(repo, "show", f"{ours.ref}:state/b.json") == '{"b": "rewritten"}'


def test_c13_1_sparse_snapshot_leaves_head_index_and_files_alone(sparse_pair):
    """C-13.1 in a sparse worktree too: HEAD, the real index's bytes, its
    skip-worktree bits, `git status` and every file are as they were."""
    _, sparse, _ = sparse_pair
    _edit(sparse)
    git(sparse, "add", "--", "bin/tool")
    index = _real_index(sparse)

    def state():
        return (git_head(sparse), index.read_bytes(), git(sparse, "ls-files", "-v"),
                git(sparse, "status", "--porcelain=v1"), _files(sparse),
                {name: (sparse / name).read_bytes() for name in _files(sparse)})
    before = state()
    assert salvage(sparse, before[0], 1) is not None
    with _empty_index_read():
        working_tree(sparse, before[0])
    assert state() == before
    assert not list(index.parent.glob("subfleet-salvage-*"))


def test_c13_1_sparse_a_newer_commit_checked_out_is_snapshotted_whole(sparse_pair):
    """C-13.1 a file left out takes the real index's content, not the baseline's: a
    job that checks out newer work gets the snapshot a full checkout of it gives,
    with what that commit changed, added and removed outside the patterns, a
    directory it made a file and a file it made a directory among them."""
    repo, sparse, full = sparse_pair
    baseline = git_head(sparse)
    (repo / "state" / "a.json").write_text('{"a": "upstream"}\n')
    (repo / "state" / "added.json").write_text("{}\n")
    (repo / "state" / "deep" / "c.json").unlink()
    shutil.rmtree(repo / "notes")
    (repo / "notes").write_text("a file where a directory was\n")
    (repo / "state" / "b.json").unlink()
    (repo / "state" / "b.json").mkdir()
    (repo / "state" / "b.json" / "inner.json").write_text("a directory where a file was\n")
    (repo / "bin" / "tool").write_text("#!/bin/sh\necho upstream\n")
    git(repo, "add", "-A")
    git(repo, *IDENTITY, "commit", "-m", "upstream")
    upstream = git_head(repo)
    for worktree in (sparse, full):
        git(worktree, "checkout", "--detach", upstream)
    assert not (sparse / "state").exists()
    assert (_both_reads(sparse, baseline) == working_tree(full, baseline)
            == git(repo, "rev-parse", f"{upstream}^{{tree}}"))


def test_c6_8_sparse_an_unmerged_index_takes_the_empty_read_and_still_agrees(sparse_pair):
    """C-6.8 a conflict in the patterns (unmerged entries, which `read-tree -m`
    refuses) takes the read into an empty index; it keeps the files left out,
    as merged, and equals the full checkout's."""
    repo, sparse, full = sparse_pair
    baseline = git_head(sparse)
    git(repo, "checkout", "-b", "theirs")
    (repo / "bin" / "tool").write_text("#!/bin/sh\necho theirs\n")
    (repo / "state" / "a.json").write_text('{"a": "theirs"}\n')
    git(repo, *IDENTITY, "commit", "-am", "theirs")
    git(repo, "checkout", "task/source")
    for worktree in (sparse, full):
        (worktree / "bin" / "tool").write_text("#!/bin/sh\necho ours\n")
        git(worktree, *IDENTITY, "commit", "-am", "ours")
        merged = subprocess.run(["git", "-C", str(worktree), *IDENTITY, "merge", "theirs"],
                                capture_output=True)
        assert merged.returncode == 1
        assert "UU bin/tool" in git(worktree, "status", "--porcelain=v1").splitlines()
    assert not (sparse / "state").exists()
    tree = working_tree(sparse, baseline)
    assert tree == working_tree(full, baseline)
    assert git(repo, "show", f"{tree}:state/a.json") == '{"a": "theirs"}'


def test_c13_1_sparse_a_worktree_with_no_index_is_refused_not_read_as_deleted(sparse_pair):
    """C-13.1 with no index nothing says which absent files the patterns left out,
    and git reads them all as deleted; the snapshot refuses."""
    repo, sparse, _ = sparse_pair
    _real_index(sparse).unlink()
    with pytest.raises(SalvageError, match="no index") as error:
        working_tree(sparse, git_head(sparse))
    assert not error.value.transient
    with pytest.raises(SalvageError, match="no index"):
        salvage(sparse, git_head(sparse), 1)
    assert git(repo, "for-each-ref", "refs/subfleet-salvage/") == ""


@pytest.mark.parametrize("broken", [("ls-files", "-s"), ("update-index", "--index-info"),
                                    ("update-index", "--skip-worktree")])
def test_c13_1_sparse_a_snapshot_that_cannot_keep_the_files_left_out_fails(sparse_pair, monkeypatch,
                                                                             broken):
    """C-13.1 fails closed: when the real index cannot be listed, or its entries
    cannot be written into the temporary one, or their bits cannot be set there,
    the snapshot raises. Going on would record every file left out as deleted."""
    repo, sparse, _ = sparse_pair
    real = salvage_module._git_bytes

    def git_bytes(workdir, *args, **kwargs):
        refused = args[0] == broken[0] and broken[1] in args
        return None if refused else real(workdir, *args, **kwargs)
    monkeypatch.setattr(salvage_module, "_git_bytes", git_bytes)
    with pytest.raises(SalvageError, match="sparse checkout"):
        salvage(sparse, git_head(sparse), 1)
    assert git(repo, "for-each-ref", "refs/subfleet-salvage/") == ""


def test_c13_1_sparse_a_subdirectory_snapshots_the_whole_worktree(sparse_pair):
    """C-13.1 an in-place job may run from a subdirectory: the files left out are
    listed from the top level, so the snapshot is the same from either."""
    _, sparse, full = sparse_pair
    baseline = git_head(sparse)
    for worktree in (sparse, full):
        _edit(worktree)
    assert (_both_reads(sparse / "bin", baseline) == _both_reads(sparse, baseline)
            == working_tree(full / "bin", baseline))


def test_c13_1_sparse_a_file_written_where_a_directory_was_left_out(sparse_pair):
    """C-13.1 a job that writes a file named as a directory the patterns left out
    replaces that directory, as it would in a full checkout."""
    repo, sparse, full = sparse_pair
    baseline = git_head(sparse)
    (sparse / "notes").write_text("now a file\n")
    shutil.rmtree(full / "notes")
    (full / "notes").write_text("now a file\n")
    tree = _both_reads(sparse, baseline)
    assert tree == working_tree(full, baseline)
    assert _changes(repo, f"{baseline}^{{tree}}", tree) == ["A\tnotes", "D\tnotes/todo.md"]


@pytest.mark.parametrize("target", ["nowhere", "elsewhere"])
def test_c13_1_sparse_a_symlink_where_a_directory_was_left_out(sparse_pair, tmp_path, target):
    """C-13.1 a symlink named as a directory the patterns left out, dangling or to a
    directory holding a file of the same name: the files that were under it are
    gone and the link is recorded, as in a full checkout."""
    repo, sparse, full = sparse_pair
    baseline = git_head(sparse)
    if target == "elsewhere":
        (tmp_path / target).mkdir()
        (tmp_path / target / "a.json").write_text("not the tracked file\n")
    (sparse / "state").symlink_to(tmp_path / target)
    shutil.rmtree(full / "state")
    (full / "state").symlink_to(tmp_path / target)
    tree = _both_reads(sparse, baseline)
    assert tree == working_tree(full, baseline)
    assert _changes(repo, f"{baseline}^{{tree}}", tree) == [
        "A\tstate", "D\tstate/.gitignore", "D\tstate/a.json", "D\tstate/b.json", "D\tstate/deep/c.json"]


def test_c13_1_sparse_every_kind_of_entry_left_out_is_kept(tmp_path):
    """C-13.1 a symlink, an executable and a gitlink outside the patterns are kept
    with their modes, in a worktree nobody edited and beside a job's edits."""
    repo = _layout_repo(tmp_path / "repo", SPARSE_LAYOUT)
    (repo / "notes" / "link").symlink_to("todo.md")
    (repo / "state" / "run.sh").write_text("#!/bin/sh\n")
    (repo / "state" / "run.sh").chmod(0o755)
    git(repo, "add", "-A")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{'a' * 40},vendor/lib")
    git(repo, *IDENTITY, "commit", "-m", "a symlink, an executable and a gitlink")
    sparse = _cut(repo, tmp_path / "sparse", SPARSE_MODES["patterns"], branch="task/sparse")
    full = _cut(repo, tmp_path / "full", branch="task/full")
    baseline = git_head(sparse)
    modes = {os.fsdecode(path): mode for mode, _, path in salvage_module._off_disk(sparse)}
    assert (modes["notes/link"], modes["state/run.sh"], modes["vendor/lib"]) == (b"120000", b"100755", b"160000")
    assert _both_reads(sparse, baseline) == git(sparse, "rev-parse", "HEAD^{tree}")
    for worktree in (sparse, full):
        _edit(worktree)
    tree = _both_reads(sparse, baseline)
    assert tree == working_tree(full, baseline)
    listed = git(repo, "ls-tree", "-r", tree, "--", "notes/link", "state/run.sh", "vendor/lib")
    assert [line.split()[0] for line in listed.splitlines()] == ["120000", "100755", "160000"]


def test_c13_1_sparse_a_file_written_then_removed_outside_the_patterns_reads_as_git_reads_it(sparse_pair):
    """C-13.1 intended, and where a sparse worktree is not a full one: a tracked file
    outside the patterns that the job wrote and then removed is off disk with its
    skip-worktree bit, exactly as if never written. git reports it unchanged, and
    so does the snapshot. To delete it, the job runs `git rm --sparse`: the index
    entry goes, and the snapshot records the deletion."""
    repo, sparse, _ = sparse_pair
    baseline = git_head(sparse)
    (sparse / "state").mkdir()
    (sparse / "state" / "a.json").write_text("written\n")
    shutil.rmtree(sparse / "state")
    assert git(sparse, "status", "--porcelain") == ""
    assert _both_reads(sparse, baseline) == git(sparse, "rev-parse", "HEAD^{tree}")
    git(sparse, "rm", "--sparse", "--cached", "--", "state/a.json")
    assert _changes(repo, f"{baseline}^{{tree}}", _both_reads(sparse, baseline)) == ["D\tstate/a.json"]


def test_c13_1_sparse_index_mode_holds_a_directory_and_its_real_index_is_untouched(tmp_path):
    """C-13.1 the `cone-sparse-index` worktrees above do hold a sparse index: a
    directory entry stands for the files left out, the listing reads them as
    files, and the snapshot rewrites nothing in the real index."""
    repo = _layout_repo(tmp_path / "repo", SPARSE_LAYOUT)
    sparse = _cut(repo, tmp_path / "sparse", SPARSE_MODES["cone-sparse-index"], branch="task/sparse")
    assert "040000" in git(sparse, "ls-files", "--sparse", "-s").split()
    assert _off_disk_paths(sparse) == LEFT_OUT
    before = _real_index(sparse).read_bytes()
    assert working_tree(sparse, git_head(sparse)) == git(sparse, "rev-parse", "HEAD^{tree}")
    assert _real_index(sparse).read_bytes() == before


def test_c13_1_sparse_only_a_skip_worktree_entry_whose_file_is_gone_counts_as_off_disk(sparse_pair,
                                                                                         monkeypatch):
    """C-13.1 which entries the snapshot keeps as they are: skip-worktree (`S`, or
    `s` when also assume-unchanged), at stage 0, the file absent. A file that is
    there is read from disk whatever its bit says; any other record is not one."""
    _, sparse, _ = sparse_pair
    (sparse / "state").mkdir()
    (sparse / "state" / "a.json").write_text("there\n")
    one, two = "1" * 40, "2" * 40
    listing = "\0".join([
        f"S 100644 {one} 0\tstate/b.json",            # left out, gone: kept
        f"s 100755 {two} 0\tnotes/with\ttab.md",      # also assume-unchanged, gone: kept
        f"S 100644 {one} 0\tstate/a.json",            # the job wrote it: read from disk
        f"H 100644 {one} 0\tnotes/todo.md",           # no bit: a deletion if it is gone
        f"S 100644 {one} 2\tnotes/unmerged.md",       # not stage 0 (git tags these M, not S)
        f"S 100644 {one}\tnotes/short.md",            # not a record this reads
        "S ", "",
    ]).encode()
    real = salvage_module._git_bytes
    monkeypatch.setattr(salvage_module, "_git_bytes", lambda workdir, *args, **kwargs: (
        listing if args[:2] == ("ls-files", "-s") else real(workdir, *args, **kwargs)))
    assert salvage_module._off_disk(sparse) == [
        (b"100644", one.encode(), b"state/b.json"), (b"100755", two.encode(), b"notes/with\ttab.md")]


@pytest.mark.parametrize("layout", [{"a/f": b""}, {"a/f": b"x", "a/deep/g": b"y", "b/h": b"z"}])
def test_c13_1_sparse_index_holding_only_directories_snapshots_to_a_well_formed_tree(tmp_path, layout):
    """C-13.1 the case the first property found: a sparse index whose every entry is
    a directory outside the cone. `update-index --index-info` added a file beside
    the directory entry that stood for it, and the seeded snapshot was a tree
    naming that directory twice (`git fsck`: duplicateEntries). The temporary
    index is a full one, so the snapshot is the baseline's tree, and a file the
    job writes under such a directory is recorded once."""
    repo = _layout_repo(tmp_path / "repo", layout)
    sparse = _cut(repo, tmp_path / "sparse", ("--cone", "--sparse-index"), branch="task/sparse")
    full = _cut(repo, tmp_path / "full", branch="task/full")
    assert _files(sparse) == []
    assert {line.split()[1] for line in git(sparse, "ls-files", "--sparse", "-s", "-v").splitlines()} == {"040000"}
    baseline, index = git_head(sparse), _real_index(sparse).read_bytes()
    assert _both_reads(sparse, baseline) == git(sparse, "rev-parse", "HEAD^{tree}")
    for worktree in (sparse, full):
        (worktree / "a").mkdir(exist_ok=True)
        (worktree / "a" / "f").write_text("the job wrote this\n")
        (worktree / "a" / "new").write_text("and this\n")
    tree = _both_reads(sparse, baseline)
    assert tree == working_tree(full, baseline)
    assert _changes(repo, f"{baseline}^{{tree}}", tree) == ["A\ta/new", "M\ta/f"]
    assert _real_index(sparse).read_bytes() == index
    checked = subprocess.run(["git", "-C", str(repo), "fsck", "--no-dangling"], capture_output=True, text=True)
    assert "duplicateEntries" not in checked.stdout + checked.stderr


def test_c13_1_sparse_the_temporary_index_is_told_to_be_full_beside_any_setting_already_there():
    """C-13.1 `index.sparse` is turned off through git's numbered environment settings,
    after any the daemon's environment already carries; a count that is not a
    number is read as none."""
    assert salvage_module._full_index({"KEEP": "1"}) == {
        "KEEP": "1", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "index.sparse", "GIT_CONFIG_VALUE_0": "false"}
    given = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "c",
             "GIT_CONFIG_KEY_1": "d.e", "GIT_CONFIG_VALUE_1": "f"}
    assert salvage_module._full_index(given) == {
        **given, "GIT_CONFIG_COUNT": "3", "GIT_CONFIG_KEY_2": "index.sparse", "GIT_CONFIG_VALUE_2": "false"}
    assert salvage_module._full_index({"GIT_CONFIG_COUNT": "junk"})["GIT_CONFIG_COUNT"] == "1"


def test_c13_1_sparse_a_file_written_outside_the_patterns_is_read_whatever_git_is_told_to_expect(sparse_pair):
    """C-13.1 `sparse.expectFilesOutsideOfPatterns` tells git to trust a skip-worktree
    bit even where the file is there, so `git status` no longer shows the job's
    edit. The snapshot goes by the file: what is on disk is read from disk."""
    repo, sparse, _ = sparse_pair
    git(sparse, "config", "--worktree", "sparse.expectFilesOutsideOfPatterns", "true")
    (sparse / "state").mkdir()
    (sparse / "state" / "a.json").write_text('{"a": "the job wrote this"}\n')
    assert git(sparse, "status", "--porcelain") == ""
    assert git(sparse, "ls-files", "-v", "--", "state/a.json") == "S state/a.json"
    baseline = git_head(sparse)
    tree = _both_reads(sparse, baseline)
    assert _changes(repo, f"{baseline}^{{tree}}", tree) == ["M\tstate/a.json"]


def test_c13_1_a_checkout_that_is_not_sparse_is_read_exactly_as_before(repository, monkeypatch):
    """C-13.1 unchanged where `core.sparseCheckout` is off: the real index is not
    listed for files off disk, `add -A` gets no `--sparse`, no git is given a
    setting, and a skip-worktree file that is gone is still a deletion (the bit
    hides it from git, not from the snapshot)."""
    (repository / "gone.cfg").write_text("original\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "config")
    git(repository, "update-index", "--skip-worktree", "gone.cfg")
    (repository / "gone.cfg").unlink()
    assert not sparse_checkout(repository)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    real_run, commands, settings = subprocess.run, [], []

    def run(cmd, **kwargs):
        commands.append(cmd[3:])
        settings.append("GIT_CONFIG_COUNT" in (kwargs.get("env") or {}))
        return real_run(cmd, **kwargs)
    monkeypatch.setattr("subfleet.salvage.subprocess.run", run)
    tree = working_tree(repository, git_head(repository))
    monkeypatch.undo()
    assert "gone.cfg" not in _tree_files(repository, tree)
    assert ["add", "-A"] in commands and not any("--sparse" in command for command in commands)
    assert not any(settings)
    assert not any(command[:2] == ["ls-files", "-s"] for command in commands)
    assert not any(command[:1] == ["update-index"] and "--index-info" in command for command in commands)


@pytest.mark.parametrize("where", ["bin/scratch", "scratch"])
def test_c13_1_sparse_a_nested_repository_with_no_commit_is_still_left_out(sparse_pair, where):
    """C-13.1 `add -A --sparse` refuses a nested repository with no commit as `add -A`
    does. In the patterns or outside them, the snapshot leaves it out, says so,
    records the file beside it, and is the full checkout's."""
    repo, sparse, full = sparse_pair
    baseline = git_head(sparse)
    for worktree in (sparse, full):
        nested = worktree / where / "nested"
        nested.mkdir(parents=True)
        git(nested, "init", "-q")
        (nested / "inside.txt").write_text("never committed\n")
        (worktree / where / "beside.txt").write_text("recorded\n")
    ours, theirs = [], []
    tree = working_tree(sparse, baseline, left_out=ours)
    assert tree == working_tree(full, baseline, left_out=theirs)
    assert ours == theirs == [f"{where}/nested/"]
    assert _changes(repo, f"{baseline}^{{tree}}", tree) == [f"A\t{where}/beside.txt"]
    with _empty_index_read():
        assert working_tree(sparse, baseline) == tree


def test_c26_14_sparse_a_turn_diff_lists_only_what_the_turn_wrote(sparse_pair):
    """C-26.14 a conversation's diff takes the same snapshot: in a sparse workspace
    its start is the head's tree, and its end differs only where the turn wrote."""
    from subfleet.conversations import diff as turn_diff
    _, sparse, _ = sparse_pair
    head, start = turn_diff.snapshot(sparse)
    assert start == git(sparse, "rev-parse", "HEAD^{tree}")
    (sparse / "bin" / "tool").write_text("#!/bin/sh\necho turn\n")
    (sparse / "reports").mkdir()
    (sparse / "reports" / "r.md").write_text("r\n")
    (sparse / "state").mkdir()
    (sparse / "state" / "a.json").write_text('{"a": "turn"}\n')
    end = turn_diff.end_snapshot(sparse, head_before=head, start_tree=start)
    result = turn_diff.build(sparse, start, end["end_tree"])
    assert sorted((item["status"], item["path"]) for item in result["files"]) == [
        ("added", "reports/r.md"), ("modified", "bin/tool"), ("modified", "state/a.json")]


# --- C-13.1 in a sparse checkout: the properties ---------------------------------
#
# 1. Nobody edited it: for any files, any patterns and any of the three kinds of
#    sparse checkout, the snapshot is the baseline's tree, read either way, from
#    the top level or a subdirectory, and the real index is not rewritten.
# 2. Sparse is full: for any edits, git's own commands among them, the snapshot
#    of a sparse worktree is the snapshot of a full checkout with the same edits.
# 3. The snapshot reads the worktree as git does: for any edits to files, the
#    paths the snapshot changes are the paths `git status` reports.

QUIET = dict(deadline=None, derandomize=True, database=None)
_SERIAL = itertools.count()
_TOPS = ["a", "b", "c", "sp ace"]
_LEAVES = ["f", "g.txt", "with space", "-dash", "it's", "日本", "tab\there", "new\nline", '"quoted"']
_body = st.binary(max_size=24)
_layouts = st.dictionaries(
    st.tuples(st.sampled_from(_TOPS), st.sampled_from(["", "x/", "x/y/"]), st.sampled_from(_LEAVES))
    .map(lambda parts: f"{parts[0]}/{parts[1]}{parts[2]}"),
    _body, min_size=1, max_size=8)
_root_files = st.dictionaries(st.sampled_from(["README.md", "top.txt"]), _body, max_size=2)


def _sparse_arguments(mode, kept):
    """`git sparse-checkout set` arguments that keep the top-level files and the
    top-level directories ``kept``, in ``mode``."""
    if mode == "patterns":
        return ("--no-cone", "/README.md", "/top.txt", *(f"/{name}/" for name in kept))
    return (*SPARSE_MODES[mode][:-2], *kept)


@pytest.fixture(scope="module")
def sparse_scratch(tmp_path_factory):
    """One directory for every example of a property; each example removes its own."""
    return tmp_path_factory.mktemp("sparse-properties")


@settings(max_examples=20, **QUIET)
@given(files=_layouts, root_files=_root_files, kept=st.sets(st.sampled_from(_TOPS)),
       mode=st.sampled_from(sorted(SPARSE_MODES)))
def test_c13_1_sparse_property_a_worktree_nobody_edited_snapshots_to_its_baseline(
        sparse_scratch, files, root_files, kept, mode):
    """C-13.1 for any tracked files, any patterns (keeping all, some or none of the
    directories) and each kind of sparse checkout: the snapshot of a worktree
    nobody edited is its baseline's tree. Read from the real index or into an
    empty one, from the top level or from a directory the patterns keep, with
    the real index left byte for byte as it was."""
    root = sparse_scratch / f"nobody-{next(_SERIAL)}"
    try:
        repo = _layout_repo(root / "repo", {**files, **root_files})
        sparse = _cut(repo, root / "sparse", _sparse_arguments(mode, sorted(kept)))
        left_out = sorted(name for name in files if name.split("/")[0] not in kept)
        assert _files(sparse) == sorted(set(files) - set(left_out) | set(root_files))
        assert sorted(_off_disk_paths(sparse)) == left_out
        baseline, index = git_head(sparse), _real_index(sparse).read_bytes()
        expected = git(sparse, "rev-parse", "HEAD^{tree}")
        assert _both_reads(sparse, baseline) == expected
        for inside in sorted(kept & {name.split("/")[0] for name in files})[:1]:
            assert _both_reads(sparse / inside, baseline) == expected
        assert _real_index(sparse).read_bytes() == index
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def sparse_source(sparse_scratch):
    """The repository the two edit properties cut their worktrees from."""
    return _layout_repo(sparse_scratch / "source", SPARSE_LAYOUT)


_TRACKED_IN = ["bin/tool", "bin/lib/helper.py", "tests/test_tool.py", "README.md"]
_TRACKED_OUT = ["state/a.json", "state/deep/c.json", "notes/todo.md", "state/.gitignore"]
_DIRS_IN = ["bin", "bin/lib", "bin/new/deeper", "tests"]
_DIRS_OUT = ["state", "state/deep", "state/new", "notes", "reports/2026"]
_names = st.sampled_from(["a.txt", "b.py", "d", "ignored.txt", "scratch.tmp"])
_file_edits = st.one_of(
    st.tuples(st.just("write"), st.sampled_from(_TRACKED_IN + _TRACKED_OUT), _body),
    st.tuples(st.just("create"), st.sampled_from(_DIRS_IN + _DIRS_OUT), _names, _body),
    st.tuples(st.just("remove"), st.sampled_from(_TRACKED_IN)),
    st.tuples(st.just("chmod"), st.sampled_from(_TRACKED_IN)),
    st.tuples(st.just("symlink"), st.sampled_from(_DIRS_IN + _DIRS_OUT), _names),
)
_git_edits = st.one_of(
    st.tuples(st.just("stage"), st.sampled_from(_TRACKED_IN + _TRACKED_OUT)),
    st.tuples(st.just("commit")),
)


def _apply(worktree, edit):
    kind = edit[0]
    if kind == "write":
        target = worktree / edit[1]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(edit[2])
    elif kind in ("create", "symlink"):
        directory = worktree / edit[1]
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / edit[2]
        if target.is_symlink() or target.exists():
            target.unlink()
        if kind == "create":
            target.write_bytes(edit[3])
        else:
            target.symlink_to("../README.md")
    elif kind == "remove":
        (worktree / edit[1]).unlink(missing_ok=True)
    elif kind == "chmod":
        if (worktree / edit[1]).is_file():
            (worktree / edit[1]).chmod(0o755)
    elif kind == "stage":
        # `--sparse` lets git stage a path outside the patterns; a full checkout ignores it.
        subprocess.run(["git", "-C", str(worktree), "add", "--sparse", "-A", "--", edit[1]],
                       capture_output=True)
    elif kind == "commit":
        git(worktree, "add", "-A", "--", "bin", "tests")
        git(worktree, *IDENTITY, "commit", "--allow-empty", "-m", "step")


@contextlib.contextmanager
def _worktrees(source, scratch, mode, *, full):
    """A sparse worktree of ``source`` (and a full one) for one example."""
    root = scratch / f"edits-{next(_SERIAL)}"
    try:
        sparse = _cut(source, root / "sparse", SPARSE_MODES[mode])
        yield sparse, _cut(source, root / "full") if full else None
    finally:
        shutil.rmtree(root, ignore_errors=True)
        git(source, "worktree", "prune")


@settings(max_examples=25, **QUIET)
@given(mode=st.sampled_from(sorted(SPARSE_MODES)),
       edits=st.lists(st.one_of(_file_edits, _git_edits), max_size=8))
def test_c13_1_sparse_property_the_snapshot_is_a_full_checkouts_with_the_same_edits(
        sparse_source, sparse_scratch, mode, edits):
    """C-13.1 for any sequence of edits (files written, created, removed, made
    executable or replaced by a symlink, in the patterns and outside them, staged
    or committed along the way): the sparse worktree's snapshot, read either way,
    is the tree a full checkout with the same edits snapshots to."""
    with _worktrees(sparse_source, sparse_scratch, mode, full=True) as (sparse, full):
        baseline = git_head(sparse)
        for edit in edits:
            _apply(sparse, edit)
            _apply(full, edit)
        assert _both_reads(sparse, baseline) == working_tree(full, baseline)


def _status_paths(worktree):
    """The paths `git status` reports, untracked files one by one."""
    listed = subprocess.run(["git", "-C", str(worktree), "status", "--porcelain=v1", "-z",
                             "--untracked-files=all"], check=True, capture_output=True).stdout
    return sorted(os.fsdecode(record[3:]) for record in listed.split(b"\0") if record)


@settings(max_examples=25, **QUIET)
@given(mode=st.sampled_from(sorted(SPARSE_MODES)),
       edits=st.lists(st.one_of(_file_edits, st.tuples(st.just("remove"), st.sampled_from(_TRACKED_OUT))),
                      max_size=8))
def test_c13_1_sparse_property_the_snapshot_changes_the_paths_git_status_reports(
        sparse_source, sparse_scratch, mode, edits):
    """C-13.1 for any edits to files, including a file outside the patterns written
    and then removed: the paths at which the snapshot differs from the baseline
    are exactly the paths `git status` reports in the sparse worktree."""
    with _worktrees(sparse_source, sparse_scratch, mode, full=False) as (sparse, _):
        baseline = git_head(sparse)
        for edit in edits:
            _apply(sparse, edit)
        tree = _both_reads(sparse, baseline)
        changed = git(sparse, "diff-tree", "-r", "--name-only", "--no-renames", "-z",
                      f"{baseline}^{{tree}}", tree)
        assert sorted(name for name in changed.split("\0") if name) == _status_paths(sparse)
