"""C-13.1: a path git cannot index is left out of a snapshot, not a reason it fails.

On 2026-09-27 two finished review attempts held their Codex lanes for hours in
`finalizing`: a test fixture had left an empty repository (`repo `, no commit)
under the reviewer's untracked scratch directory, `git add -A` refused it, and
salvage failed the same way on every retry.
"""
from __future__ import annotations

import os
import subprocess

import hypothesis
import hypothesis.strategies as st
import pytest

from subfleet import retention
from subfleet import salvage as salvage_module
from subfleet.salvage import SalvageError, _add_all, git_head, salvage, snapshot_tree, working_tree
from tests.unit.test_retention_worktrees import owned  # noqa: F401 (a fixture)
from tests.unit.test_salvage import git, repository  # noqa: F401 (a fixture)

NESTED = ".review-scratch/pytest/test_a_checkout_whose_name_end0/repo /"


def empty_repository(path):
    """What the fixture left: a repository with a file and no commit checked out."""
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "inside.txt").write_text("never committed\n")


def fake_add(monkeypatch, rc, stderr):
    """Replace only the snapshot's `git add`; every other git call is real."""
    real = subprocess.run

    def run(argv, *args, **kwargs):
        if "--ignore-errors" in argv:
            if isinstance(rc, BaseException):
                raise rc
            return subprocess.CompletedProcess(argv, rc, b"", stderr)
        return real(argv, *args, **kwargs)
    monkeypatch.setattr(salvage_module.subprocess, "run", run)


# --- the snapshot ------------------------------------------------------------------


def test_salvage_leaves_out_a_nested_repository_with_no_commit(repository):
    baseline = git_head(repository)
    empty_repository(repository / NESTED)
    (repository / ".review-scratch" / "notes.txt").write_text("scratch\n")
    (repository / "tracked.txt").write_text("provider progress\n")
    index = (repository / ".git" / "index").read_bytes()
    result = salvage(repository, baseline, 1, timestamp="2026-09-27T19:19:12Z")
    assert result is not None and result.skipped == (NESTED,)
    names = git(repository, "ls-tree", "-r", "--name-only", result.ref).splitlines()
    assert "tracked.txt" in names and ".review-scratch/notes.txt" in names
    assert not any(name.startswith(NESTED.rstrip("/")) for name in names)
    assert git(repository, "show", f"{result.ref}:tracked.txt") == "provider progress"
    assert (repository / ".git" / "index").read_bytes() == index      # C-13.1: the real index is untouched
    assert (repository / NESTED / "inside.txt").read_text() == "never committed\n"


def test_the_start_snapshot_succeeds_beside_one(repository):
    """C-6.8: admission's reservation snapshot takes the same path."""
    baseline = git_head(repository)
    empty_repository(repository / NESTED)
    tree, skipped = snapshot_tree(repository, baseline)
    assert skipped == (NESTED,)
    assert working_tree(repository, baseline) == tree
    assert git(repository, "rev-parse", f"{baseline}^{{tree}}") == tree      # nothing else changed


def test_when_only_a_skipped_path_changed_no_ref_is_written_and_the_tree_reads_dirty(repository):
    """Nothing to commit; the worktree still reads dirty, which keeps retention's hands off it."""
    baseline = git_head(repository)
    empty_repository(repository / NESTED)
    assert salvage(repository, baseline, 1, timestamp="2026-09-27T19:19:12Z") is None
    assert git(repository, "for-each-ref", "refs/subfleet-salvage/") == ""
    assert git(repository, "status", "--porcelain=v1", "--untracked-files=all")


def test_an_embedded_repository_with_a_commit_is_still_recorded_as_before(repository):
    """Only what git cannot index is left out: a nested repository with a commit is a gitlink."""
    baseline = git_head(repository)
    nested = repository / "vendor" / "lib"
    empty_repository(nested)
    git(nested, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "add", ".")
    git(nested, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-qm", "c")
    result = salvage(repository, baseline, 1, timestamp="2026-09-27T19:19:12Z")
    assert result.skipped == ()
    assert git(repository, "ls-tree", result.ref, "vendor/lib").startswith("160000 commit ")


# --- every other failure fails as before --------------------------------------------


@pytest.mark.parametrize("rc,stderr", [
    (128, b"fatal: adding files failed\n"),
    (1, b"error: something git could not do\n"),
    (1, b"error: unable to index file 'x'\nfatal: Unable to create '.git/index.lock': File exists.\n"),
    (1, b""),
])
def test_any_other_add_failure_still_fails(repository, monkeypatch, rc, stderr):
    (repository / "new.txt").write_text("work\n")
    fake_add(monkeypatch, rc, stderr)
    with pytest.raises(SalvageError, match="git add failed") as caught:
        salvage(repository, git_head(repository), 1)
    assert not caught.value.transient


def test_an_add_that_times_out_is_transient(repository, monkeypatch):
    (repository / "new.txt").write_text("work\n")
    fake_add(monkeypatch, subprocess.TimeoutExpired(["git"], 1), b"")
    with pytest.raises(SalvageError, match="timed out") as caught:
        salvage(repository, git_head(repository), 1)
    assert caught.value.transient


def test_a_path_that_is_not_utf8_is_carried_not_a_decoding_error(repository, monkeypatch):
    """APFS refuses such names, so the add is faked; git names the path in its raw bytes."""
    fake_add(monkeypatch, 1, b"error: 'caf\xe9/' does not have a commit checked out\n"
                             b"error: unable to index file 'caf\xe9/'\n")
    tree, skipped = snapshot_tree(repository, git_head(repository))
    assert [os.fsencode(path) for path in skipped] == [b"caf\xe9/"]


# --- the parse, as a property ---------------------------------------------------------

PATH = st.text(st.characters(blacklist_characters="\n\r", blacklist_categories=("Cs",)), min_size=1, max_size=20)
LINE = st.one_of(
    PATH.map(lambda p: ("skip", p)),
    st.sampled_from(["error: 'x' does not have a commit checked out", "warning: adding embedded git repository: sub",
                     "hint: see git help submodule", "error: open(\"y\"): Permission denied"]).map(lambda t: ("other", t)),
    st.sampled_from(["fatal: adding files failed", "fatal: Unable to create index.lock"]).map(lambda t: ("fatal", t)),
)


@hypothesis.settings(deadline=None, max_examples=200)
@hypothesis.given(st.lists(LINE, max_size=8), st.sampled_from([1, 128]))
def test_the_skipped_paths_are_exactly_the_named_ones_and_a_fatal_line_always_fails(lines, rc):
    """Invariants over any stderr of a failed `add --ignore-errors`: the result is
    exactly the paths named by "unable to index" lines, in order; it raises iff
    no such line was written or any line is fatal; it never raises anything but
    SalvageError."""
    text = "".join((f"error: unable to index file '{v}'" if k == "skip" else v) + "\n" for k, v in lines)
    run = lambda argv, *a, **kw: subprocess.CompletedProcess(argv, rc, b"", os.fsencode(text))  # noqa: E731
    named = tuple(v for k, v in lines if k == "skip")
    fails = not named or any(k == "fatal" for k, _ in lines)
    original = salvage_module.subprocess.run
    salvage_module.subprocess.run = run
    try:
        if fails:
            with pytest.raises(SalvageError):
                _add_all("/nonexistent", {})
        else:
            assert _add_all("/nonexistent", {}) == named
    finally:
        salvage_module.subprocess.run = original


# --- retention keeps what the snapshot left out ---------------------------------------


def test_c13_4_retention_keeps_a_worktree_whose_snapshot_left_a_path_out(owned):  # noqa: F811
    """The salvage ref holds everything else; the left-out path exists only in the
    worktree, so retention must not remove it (it recomputes the tree with a plain
    `add -A`, which refuses the path, and a refusal protects the job)."""
    import hashlib
    from subfleet.contracts import Credential, Lane, LaneOwner
    store, root, repository, worktree = owned
    empty_repository(worktree / NESTED)
    (worktree / "tracked").write_text("provider progress")
    store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/home/one", "home"), "/home/one",
                        LaneOwner.V2, False))
    store.add_attempt(attempt_id="job/a1", job_id="job", seq=1, lane_id="codex-1", model_requested="gpt-6-astra",
                      state="succeeded")
    result = salvage(worktree, git(worktree, "rev-parse", "HEAD"), 1, timestamp="2026-09-27T19:19:12Z")
    assert result.skipped == (NESTED,)
    store.add_artifact("job/a1", "salvage", result.ref, hashlib.sha256(result.commit.encode()).hexdigest(), 0)
    outcome = retention.maintenance(store, root, max_jobs=0, salvage_referenced_elsewhere=lambda artifact: True)
    assert outcome["pruned"] == [] and outcome["protected"] == ["job"]
    assert (worktree / NESTED / "inside.txt").read_text() == "never committed\n"
