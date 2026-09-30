"""C-13.1: a nested repository with no commit is left out of a snapshot, not a reason it fails.

On 2026-09-27 two finished review attempts held their Codex lanes for hours in
`finalizing`: a test fixture had left an empty repository (`repo `, no commit)
under the reviewer's untracked scratch directory, `git add -A` refused it, and
salvage failed the same way on every retry.

Review of c1f95838: `--ignore-errors` and a parse of git's "unable to index
file" lines also let through an object git could not write (a read-only
`.git/objects/xx`, a full disk), so a salvage ref could lack real work and say
nothing. Now such repositories are found (`ls-files -o`, HEAD in each) and
excluded, and every other failure fails as before.
"""
from __future__ import annotations

import errno
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import hypothesis
import hypothesis.strategies as st
import pytest

from subfleet import retention
from subfleet import salvage as salvage_module
from subfleet.salvage import (
    SalvageError, _add_all, _git, git_head, path_text, salvage, snapshot_tree, utf8_text, working_tree,
)
from tests.unit.test_retention_worktrees import owned  # noqa: F401 (a fixture)
from tests.unit.test_salvage import git, repository  # noqa: F401 (a fixture)

NESTED = ".review-scratch/pytest/test_a_checkout_whose_name_end0/repo /"
ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a read-only directory")


def empty_repository(path):
    """What the fixture left: a repository with a file and no commit checked out."""
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "inside.txt").write_text("never committed\n")


def committed_repository(path):
    empty_repository(path)
    git(path, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "add", ".")
    git(path, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-qm", "c")


def fake_add(monkeypatch, rc, stderr):
    """Replace only the snapshot's plain `git add -A`; every other git call is real."""
    real = subprocess.run

    def run(argv, *args, **kwargs):
        if argv[3:5] == ["add", "-A"]:
            if isinstance(rc, BaseException):
                raise rc
            return subprocess.CompletedProcess(argv, rc, b"", stderr)
        return real(argv, *args, **kwargs)
    monkeypatch.setattr(salvage_module.subprocess, "run", run)


def block_object(repository, path):
    """Make git unable to write `path`'s blob: its `.git/objects/xx` is read-only
    (after a `sudo git`, say). Returns the directory, for `unblock`."""
    blob = git(repository, "hash-object", str(path))
    fanout = repository / ".git" / "objects" / blob[:2]
    fanout.mkdir(exist_ok=True)
    fanout.chmod(0o555)
    return fanout


def unblock(*directories):
    for directory in directories:
        directory.chmod(0o755)


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
    """Nothing to commit; the worktree still reads dirty, which keeps retention's hands off it,
    and a caller that asks is still told what was left out."""
    baseline = git_head(repository)
    empty_repository(repository / NESTED)
    left_out: list[str] = []
    assert salvage(repository, baseline, 1, timestamp="2026-09-27T19:19:12Z", left_out=left_out) is None
    assert left_out == [NESTED]
    assert git(repository, "for-each-ref", "refs/subfleet-salvage/") == ""
    assert git(repository, "status", "--porcelain=v1", "--untracked-files=all")


def test_an_embedded_repository_with_a_commit_is_still_recorded_as_before(repository):
    """Only a repository with no commit is left out: one with a commit is a gitlink,
    beside it or not."""
    baseline = git_head(repository)
    committed_repository(repository / "vendor" / "lib")
    result = salvage(repository, baseline, 1, timestamp="2026-09-27T19:19:12Z")
    assert result.skipped == ()
    assert git(repository, "ls-tree", result.ref, "vendor/lib").startswith("160000 commit ")
    empty_repository(repository / "vendor" / "empty")
    tree, skipped = snapshot_tree(repository, baseline)
    assert skipped == ("vendor/empty/",)
    assert git(repository, "ls-tree", tree, "vendor/lib").startswith("160000 commit ")


@pytest.mark.parametrize("name", ["glob*[x]?", "it's", "trailing ", "café", "-dash"])
def test_a_name_git_would_read_as_a_pattern_is_excluded_literally(repository, name):
    """The exclusion is `:(literal)`: a name with glob characters leaves out only itself,
    never a sibling it would match as a pattern (`globAxy/`)."""
    baseline = git_head(repository)
    empty_repository(repository / "scratch" / name)
    (repository / "scratch" / "globAxy").mkdir()
    (repository / "scratch" / "globAxy" / "kept.txt").write_text("kept\n")
    tree, skipped = snapshot_tree(repository, baseline)
    assert skipped == (f"scratch/{name}/",)
    assert git(repository, "ls-tree", "-r", "--name-only", tree, "scratch").splitlines() == ["scratch/globAxy/kept.txt"]


def test_from_a_subdirectory_the_whole_checkout_is_snapshot(repository):
    """A job may start in `repo/pkg` (C-6.6): the repositories are found, and excluded, from the
    top level, as `add -A` records the whole tree."""
    baseline = git_head(repository)
    (repository / "pkg").mkdir()
    empty_repository(repository / "elsewhere" / "repo")
    (repository / "elsewhere" / "work.txt").write_text("work\n")
    tree, skipped = snapshot_tree(repository / "pkg", baseline)
    assert skipped == ("elsewhere/repo/",)
    assert git(repository, "ls-tree", "-r", "--name-only", tree).splitlines() == [
        ".gitignore", "elsewhere/work.txt", "tracked.txt"]


def test_one_whose_git_directory_is_elsewhere_is_left_out_too(repository, tmp_path_factory):
    """`git init --separate-git-dir` leaves a `.git` file; HEAD is read through it."""
    baseline = git_head(repository)
    gitdir = tmp_path_factory.mktemp("separate") / "gitdir"
    (repository / "linked").mkdir()
    git(repository / "linked", "init", "-q", "--separate-git-dir", str(gitdir))
    (repository / "linked" / "f").write_text("x\n")
    assert snapshot_tree(repository, baseline)[1] == ("linked/",)


def test_one_under_an_ignored_directory_is_not_a_failure_to_begin_with(repository):
    baseline = git_head(repository)
    (repository / ".gitignore").write_text("ignored.txt\nbuild/\n")
    empty_repository(repository / "build" / "repo")
    tree, skipped = snapshot_tree(repository, baseline)
    assert skipped == ()
    assert "build" not in git(repository, "ls-tree", "--name-only", tree).splitlines()



@pytest.mark.parametrize("seeded", [True, False], ids=["seeded-index", "empty-index"])
def test_a_tracked_file_replaced_by_a_repository_with_no_commit_is_left_out(repository, monkeypatch, seeded):
    """Adversarial review of the round-3 branch: `rm tracked.txt; git init tracked.txt`.
    `ls-files -o` does not list `tracked.txt/` (the index still names `tracked.txt`), but
    `add -A` records the file's removal and then refuses the directory, so the salvage
    failed for good and the job's retry failed at admission. `diff-files` names the path;
    its exclusion names only the directory, so the removal is still recorded. With the
    real index as the seed or without (`_seed_index`), the tree is the same."""
    if not seeded:
        monkeypatch.setattr(salvage_module, "_seed_index", lambda *args, **kwargs: False)
    baseline = git_head(repository)
    (repository / "tracked.txt").unlink()
    empty_repository(repository / "tracked.txt")
    (repository / "new.txt").write_text("unrelated work\n")
    result = salvage(repository, baseline, 1, timestamp="2026-09-29T09:00:00Z")
    assert result is not None and result.skipped == ("tracked.txt/",)
    assert git(repository, "ls-tree", "-r", "--name-only", result.ref).splitlines() == [".gitignore", "new.txt"]
    assert working_tree(repository, baseline) == result.tree      # the retry's start snapshot, too
    assert (repository / "tracked.txt" / "inside.txt").read_text() == "never committed\n"


def test_a_tracked_file_replaced_by_a_committed_repository_still_fails_closed(repository):
    """Only a repository whose HEAD does not resolve is left out: one with a commit where a
    tracked file was is for `add -A` to judge, as before."""
    baseline = git_head(repository)
    (repository / "tracked.txt").unlink()
    committed_repository(repository / "tracked.txt")
    tree, skipped = snapshot_tree(repository, baseline)
    assert skipped == ()
    assert git(repository, "ls-tree", tree, "tracked.txt").startswith("160000 commit ")


# --- every other failure fails as before (F1) ----------------------------------------


@ROOT
@pytest.mark.parametrize("beside", [False, True], ids=["alone", "beside-an-empty-repository"])
def test_an_object_git_could_not_write_fails_the_snapshot(repository, beside):
    """The review's reproduction, run: a read-only `.git/objects/xx`. With `--ignore-errors`
    git printed "unable to index file 'new.txt'", exited 1 with no `fatal:` line, and the
    snapshot went on without the file; for a tracked file the baseline blob stayed, so the
    ref showed it unchanged. Beside a repository that is left out, too, it still fails."""
    baseline = git_head(repository)
    (repository / "new.txt").write_text("work\n")
    (repository / "tracked.txt").write_text("changed\n")
    if beside:
        empty_repository(repository / NESTED)
    blocked = [block_object(repository, repository / name) for name in ("new.txt", "tracked.txt")]
    try:
        with pytest.raises(SalvageError, match="insufficient permission") as caught:
            salvage(repository, baseline, 1, timestamp="2026-09-27T19:19:12Z")
        assert not caught.value.transient
        # What git itself does with --ignore-errors: exit 1, and a tree without the work.
        lenient = lenient_add(repository, baseline)
        assert lenient.returncode == 1 and b"fatal:" not in lenient.stderr
    finally:
        unblock(*blocked)
    assert git(repository, "for-each-ref", "refs/subfleet-salvage/") == ""


@ROOT
def test_an_unreadable_file_fails_the_snapshot_as_before(repository):
    baseline = git_head(repository)
    secret = repository / "unreadable.txt"
    secret.write_text("work\n")
    secret.chmod(0)
    try:
        with pytest.raises(SalvageError, match="Permission denied"):
            snapshot_tree(repository, baseline)
    finally:
        secret.chmod(0o644)


@pytest.mark.parametrize("rc,stderr", [
    (128, b"fatal: adding files failed\n"),
    (1, b"error: something git could not do\n"),
    (1, b"error: 'x/' does not have a commit checked out\nerror: unable to index file 'x/'\n"),
    (1, b""),
])
def test_any_other_add_failure_still_fails(repository, monkeypatch, rc, stderr):
    """What git prints never decides what is left out: a failure naming a path that is not
    a nested repository with no commit fails."""
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


# --- failures another try may not meet (F5) ----------------------------------------


def test_a_ref_lock_another_git_process_holds_is_transient(repository):
    """Real git: a concurrent `update-ref`, `fetch` or `gc` holds the salvage ref's lock."""
    baseline = git_head(repository)
    (repository / "new.txt").write_text("work\n")
    held = repository / ".git" / "refs" / "subfleet-salvage" / "task-example-20260927T191912Z-a1.lock"
    held.parent.mkdir(parents=True)
    held.touch()
    with pytest.raises(SalvageError, match="File exists") as caught:
        salvage(repository, baseline, 1, timestamp="2026-09-27T19:19:12Z")
    assert caught.value.transient
    held.unlink()
    assert salvage(repository, baseline, 1, timestamp="2026-09-27T19:19:12Z").ref.endswith("-a1")


@pytest.mark.parametrize("stderr", [
    b'error: open("Unable to create \'a.lock\': File exists"): Permission denied\nfatal: adding files failed\n',
    b'error: open("x: No space left on device.txt"): Permission denied\nfatal: adding files failed\n',
    b'error: open("a write error. Out of diskspace"): Permission denied\nfatal: adding files failed\n',
    # A `hint:` or `warning:` line can end with a path git names (an embedded repository).
    b"warning: adding embedded git repository: x: No space left on device\n"
    b"hint: \tgit rm --cached x: No space left on device\n"
    b"error: 'y/' does not have a commit checked out\nfatal: adding files failed\n",
    b"warning: adding embedded git repository: a write error. Out of diskspace\nfatal: adding files failed\n",
])
def test_a_file_name_that_quotes_those_words_is_not_read_as_them(repository, monkeypatch, stderr):
    """git quotes a file name inside its own line; the markers are read only at the end
    of git's own `error:` and `fatal:` lines."""
    (repository / "new.txt").write_text("work\n")
    fake_add(monkeypatch, 128, stderr)
    with pytest.raises(SalvageError, match="git add failed") as caught:
        snapshot_tree(repository, git_head(repository))
    assert not caught.value.transient


def test_a_ref_that_cannot_be_created_for_good_is_not_transient(repository):
    """`cannot lock ref` alone also says the ref exists or its name conflicts; no retry
    changes that, so only a held lock file is transient."""
    head = git_head(repository)
    git(repository, "update-ref", "refs/subfleet-salvage/x", head)
    for ref in ("refs/subfleet-salvage/x", "refs/subfleet-salvage/x/y"):
        with pytest.raises(SalvageError, match="cannot lock ref") as caught:
            _git(repository, "update-ref", ref, head, "0" * 40)
        assert not caught.value.transient


@pytest.mark.parametrize("stderr", [
    b"error: unable to create temporary file: No space left on device\n"
    b"error: new.txt: failed to insert into database\nerror: unable to index file 'new.txt'\n"
    b"fatal: adding files failed\n",
    b"error: file write error: No space left on device\nfatal: adding files failed\n",
    b"fatal: Unable to create '/r/.git/packed-refs.lock': File exists.\n",
    b"fatal: sha1 file '/r/.git/objects/pack/tmp_pack_x' write error. Out of diskspace\n",
])
def test_a_full_disk_or_a_held_lock_in_add_is_transient(repository, monkeypatch, stderr):
    (repository / "new.txt").write_text("work\n")
    fake_add(monkeypatch, 128, stderr)
    with pytest.raises(SalvageError, match="git add failed") as caught:
        snapshot_tree(repository, git_head(repository))
    assert caught.value.transient


#: daemon.log, 2026-09-22 (job 20260922-164440-pb-fixverify-v4), as git wrote it.
OUT_OF_DISKSPACE = (b"fatal: sha1 file '/Users/maxghenis/PolicyEngine/_wk/pb-triage-verify-v4/.git/"
                    b"subfleet-salvage-d_u32mma/index.lock' write error. Out of diskspace\n")


def fake_write_tree(monkeypatch, stderr):
    """Replace only the snapshot's `write-tree` with a failure; every other git call is real."""
    real = subprocess.run

    def run(argv, *args, **kwargs):
        if argv[3:4] == ["write-tree"]:
            return subprocess.CompletedProcess(argv, 128, b"", stderr)
        return real(argv, *args, **kwargs)
    monkeypatch.setattr(salvage_module.subprocess, "run", run)


def test_the_full_disk_the_live_daemon_failed_a_job_on_is_transient(repository, monkeypatch):
    """Live (daemon.log, 2026-09-22): `workspace preparation failed: SalvageError: git
    write-tree failed: fatal: sha1 file '….lock' write error. Out of diskspace`. That is
    git's other wording for a full disk (a write that stored nothing), and it was read as a
    failure no retry clears, so the job failed at once."""
    (repository / "new.txt").write_text("work\n")
    fake_write_tree(monkeypatch, OUT_OF_DISKSPACE)
    with pytest.raises(SalvageError, match="write-tree failed: .* Out of diskspace$") as caught:
        snapshot_tree(repository, git_head(repository))
    assert caught.value.transient


#: `strerror` of every errno C-6.8 calls transient, as git ends a line with it (`error_errno`,
#: `die_errno`), and of some it does not.
TRANSIENT_STRERRORS = [os.strerror(code) for code in sorted(salvage_module.TRANSIENT_ERRNOS)]
PERMANENT_STRERRORS = [os.strerror(code) for code in (errno.EACCES, errno.ENOENT, errno.EROFS, errno.EISDIR)]


@pytest.mark.parametrize("stderr", [
    # A ref's lock file the disk had no room for (refs/files-backend.c; git names no errno).
    b"fatal: update_ref failed for ref 'refs/subfleet-salvage/x': "
    b"couldn't write '/r/.git/refs/subfleet-salvage/x.lock'\n",
    # The index, likewise (`add`, `read-tree`; no errno either).
    b"fatal: unable to write new index file\n",
    b"fatal: Out of memory, malloc failed (tried to allocate 1048576 bytes)\n",
    # A reftable ref write that failed on a full disk: no errno either (adversarial review
    # of the round-3 branch, reproduced with git 2.55 and RLIMIT_FSIZE).
    b"fatal: update_ref failed for ref 'refs/subfleet-salvage/x': reftable: transaction failure: I/O error\n",
    *(f"error: unable to create temporary file: {text}\nfatal: adding files failed\n".encode()
      for text in TRANSIENT_STRERRORS),
])
def test_git_s_other_words_for_a_full_disk_or_a_machine_under_pressure_are_transient(repository, monkeypatch,
                                                                                     stderr):
    """Review of 43b8bf29, F4: git's own wording for the errnos C-6.8 calls transient, and
    its errno-less words for a full disk at a lock file or the index, were read as
    failures no retry clears: a full disk was recorded after one try, not three, and
    failed a job at admission at once."""
    (repository / "new.txt").write_text("work\n")
    fake_add(monkeypatch, 128, stderr)
    with pytest.raises(SalvageError, match="git add failed") as caught:
        snapshot_tree(repository, git_head(repository))
    assert caught.value.transient


@pytest.mark.parametrize("text", PERMANENT_STRERRORS)
def test_an_errno_that_says_something_about_the_repository_is_not_transient(repository, monkeypatch, text):
    (repository / "new.txt").write_text("work\n")
    fake_add(monkeypatch, 128, f"error: open(\"new.txt\"): {text}\nfatal: adding files failed\n".encode())
    with pytest.raises(SalvageError, match="git add failed") as caught:
        snapshot_tree(repository, git_head(repository))
    assert not caught.value.transient


def reftable(tmp_path):
    """A repository whose refs are a reftable stack (git 2.45 and later), or a skip."""
    path = tmp_path / "reftable"
    made = subprocess.run(["git", "init", "-q", "--ref-format=reftable", str(path)], capture_output=True)
    if made.returncode:
        pytest.skip("this git cannot make a reftable repository")
    (path / "tracked.txt").write_text("baseline\n")
    git(path, "add", ".")
    git(path, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-qm", "baseline")
    return path


def test_a_reftable_lock_another_git_process_holds_is_transient(tmp_path):
    """Real git, review of 43b8bf29, F4: under reftable a held lock is `cannot lock
    references` (reproduced with git 2.55, a held `tables.list.lock`), and a salvage waits
    it out; a ref that exists or whose name conflicts says so in other words, for good."""
    repository = reftable(tmp_path)
    baseline = git_head(repository)
    (repository / "new.txt").write_text("work\n")
    held = repository / ".git" / "reftable" / "tables.list.lock"
    held.touch()
    with pytest.raises(SalvageError, match="cannot lock references$") as caught:
        salvage(repository, baseline, 1, timestamp="2026-09-28T19:19:12Z")
    assert caught.value.transient
    held.unlink()
    ref = salvage(repository, baseline, 1, timestamp="2026-09-28T19:19:12Z").ref
    for name in (ref, f"{ref}/y"):
        with pytest.raises(SalvageError, match="update-ref failed") as refused:
            _git(repository, "update-ref", name, baseline, "0" * 40)
        assert not refused.value.transient


#: A line of git's that is not its own `error:` or `fatal:` line: a hint, a warning, what a
#: remote or a hook printed, or a continuation.
NOT_GITS_OWN_LINE = st.tuples(
    st.sampled_from(["hint: ", "warning: ", "remote: ", " ", "\t", "Another git process: "]),
    st.text(st.characters(blacklist_characters="\n", blacklist_categories=("Cs",)), max_size=40),
    st.sampled_from([": " + text for text in TRANSIENT_STRERRORS] + [
        "Unable to create 'x.lock': File exists.", "cannot lock references", "couldn't write 'x.lock'",
        "reftable: transaction failure: I/O error",
        "unable to write new index file", "write error. Out of diskspace"])).map("".join)


@hypothesis.settings(deadline=None)
@hypothesis.given(st.lists(NOT_GITS_OWN_LINE, max_size=6), st.integers(0, 6))
def test_only_gits_own_error_and_fatal_lines_are_read_for_a_transient_failure(lines, where):
    """Invariants over any stderr: however many other lines end with the words (a file name
    in a hint or a warning), they never make a failure transient; one `error:` or
    `fatal:` line that ends with them does, wherever it falls."""
    assert not salvage_module._transient_git("\n".join(lines) + "\n")
    assert not salvage_module._transient_git("\n".join(lines).encode("utf-8", "surrogateescape"))
    own = f"fatal: unable to write sha1 file: {os.strerror(errno.ENOSPC)}"
    assert salvage_module._transient_git("\n".join(lines[:where] + [own] + lines[where:]) + "\n")


# --- a seed git cannot read (review of ceacf18b, P3-1) ---------------------------------

#: The review's corrupt index: a valid `DIRC` signature and version 2, then entries that
#: are garbage. git 2.55 is killed by SIGSEGV reading it (`read-tree -m` and `status`).
CORRUPT_INDEX = b"DIRC\x00\x00\x00\x02garbage" * 3


def git_crashes_seeding_from(repository, head) -> bool:
    """Whether this machine's git is killed by a signal reading `CORRUPT_INDEX` as the
    snapshot's seed (`read-tree -m` into a copy of it), in a directory of its own."""
    with tempfile.TemporaryDirectory(dir=repository / ".git") as temporary:
        index = Path(temporary) / "index"
        index.write_bytes(CORRUPT_INDEX)
        probe = subprocess.run(["git", "-C", str(repository), "read-tree", "-m", head], capture_output=True,
                               env={**os.environ, "GIT_INDEX_FILE": str(index)})
    return probe.returncode < 0


def git_version() -> str:
    return subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip()


def test_an_index_git_crashes_reading_seeds_nothing_and_the_snapshot_reads_the_baseline_afresh(repository):
    """Review of ceacf18b, P3-1: git was killed by SIGSEGV seeding the snapshot from a copy
    of this index, and that crash, transient as a git killed by a signal is, failed the
    snapshot on every try: salvage was recorded with no ref after three, and admission
    failed the job after eight deferrals. Any failure of a seeding step is now "no seed",
    and the baseline is read into an empty index in a fresh directory: the same tree, what
    it leaves out included, and the real index is left as it was."""
    baseline = git_head(repository)
    (repository / "tracked.txt").write_text("provider progress\n")
    (repository / "new.txt").write_text("new work\n")
    empty_repository(repository / "scratch" / "empty")
    expected = snapshot_tree(repository, baseline)                   # seeded from a healthy index
    if not git_crashes_seeding_from(repository, baseline):
        pytest.skip(f"{git_version()} is not killed reading the review's corrupt index, so the crash "
                    "this test is for cannot happen here (the stubbed test below covers the fallback)")
    (repository / ".git" / "index").write_bytes(CORRUPT_INDEX)
    assert snapshot_tree(repository, baseline) == expected
    assert working_tree(repository, baseline) == expected[0]
    result = salvage(repository, baseline, 1, timestamp="2026-09-29T09:00:00Z")
    assert (result.tree, result.skipped) == (expected[0], ("scratch/empty/",))
    assert git(repository, "show", f"{result.ref}:new.txt") == "new work"
    assert (repository / ".git" / "index").read_bytes() == CORRUPT_INDEX
    assert not list((repository / ".git").glob("subfleet-salvage-*"))


@pytest.mark.parametrize("step,outcome", [
    (["read-tree", "-m"], -11),                                  # SIGSEGV, as git 2.55 on the corrupt index
    (["read-tree", "-m"], -9),                                   # SIGKILL: the kernel's memory-pressure kill
    (["ls-files", "-v"], -11),                                   # clearing the copy's skip bits
    (["rev-parse", "--git-path"], 128),                          # finding the real index
    (["read-tree", "-m"], OSError(errno.EMFILE, "Too many open files")),
], ids=["read-tree-segv", "read-tree-killed", "ls-files-segv", "git-path-exit-128", "read-tree-emfile"])
def test_any_failure_of_a_seeding_step_is_no_seed(repository, monkeypatch, step, outcome):
    """P3-1 on any git: a seeding step that fails, transient or not, leaves the snapshot
    unseeded, never failed. The fallback reads the baseline in a directory of its own, so
    the `index.lock` a crashed git leaves beside the seed (which the base's fallback
    tripped on, as a lock another git holds) is not in its way."""
    baseline = git_head(repository)
    (repository / "tracked.txt").write_text("provider progress\n")
    expected = snapshot_tree(repository, baseline)
    seeds, reads = seeding_fails(monkeypatch, step, outcome)
    assert snapshot_tree(repository, baseline) == expected
    assert len(reads) == 1 and reads[0] not in seeds
    assert not list((repository / ".git").glob("subfleet-salvage-*"))


@pytest.mark.parametrize("step", [["rev-parse", "--git-path"], ["read-tree", "-m"], ["ls-files", "-v"]],
                         ids=["git-path", "read-tree", "ls-files"])
def test_a_seeding_step_stopped_at_its_cap_is_no_answer_and_reads_nothing_unseeded(repository, monkeypatch,
                                                                                   step):
    """Review of the P3-1 fix: a seeding step that reached its cap did not fail, it did not
    finish. Under the load that stops the fast seeded read, the unseeded one, which hashes
    every tracked file, is slower still (C-6.8's incident: 0.5 s seeded, 39 to 51 s and
    then past the cap unseeded), so falling back spent a second cap on each try before the
    same failure. The snapshot raises as a timeout did before the fix: transient, retried
    under C-6.8's backoff or `SALVAGE_TRIES`."""
    baseline = git_head(repository)
    (repository / "tracked.txt").write_text("provider progress\n")
    seeds, reads = seeding_fails(monkeypatch, step, subprocess.TimeoutExpired(["git"], 60))
    with pytest.raises(SalvageError, match="timed out after") as raised:
        snapshot_tree(repository, baseline)
    assert raised.value.transient and raised.value.timed_out
    assert reads == [] and len(seeds) == 1
    assert not list((repository / ".git").glob("subfleet-salvage-*"))


def seeding_fails(monkeypatch, step, outcome):
    """Make git's `step` in a snapshot end with `outcome` (a return code or an exception to
    raise), leaving an `index.lock` beside a temporary index as a git that died does.
    Returns the directories the failing step ran in and those an unseeded read ran in."""
    real, seeds, reads = subprocess.run, [], []

    def run(argv, *args, **kwargs):
        index = (kwargs.get("env") or {}).get("GIT_INDEX_FILE")
        if list(argv[3:3 + len(step)]) == step:
            if index:
                Path(index + ".lock").write_bytes(b"")               # what a git that died leaves
            seeds.append(Path(index).parent if index else None)
            if isinstance(outcome, BaseException):
                raise outcome
            return subprocess.CompletedProcess(argv, outcome, b"", b"")
        if list(argv[3:5]) != ["read-tree", "-m"] and list(argv[3:4]) == ["read-tree"]:
            reads.append(Path(index).parent)                        # the unseeded read
        return real(argv, *args, **kwargs)
    monkeypatch.setattr(salvage_module.subprocess, "run", run)
    return seeds, reads


# --- the locale (F3) --------------------------------------------------------------


def translates(locale: str) -> bool:
    probe = subprocess.run(["git", "rev-parse", "--verify", "no-such-ref"], capture_output=True, text=True,
                           env={**os.environ, "LC_ALL": locale, "LANGUAGE": "de"}, cwd=tempfile.gettempdir())
    return "fatal:" not in probe.stderr and bool(probe.stderr)


def test_git_is_read_in_its_own_words_whatever_the_daemons_locale(repository, monkeypatch):
    """Review of c1f95838, run: under `de_DE.UTF-8` git wrote "Fehler: ... hat keinen Commit
    ausgecheckt" and the snapshot failed as before; a lock message went unrecognised."""
    if not translates("de_DE.UTF-8"):
        pytest.skip("git does not translate its messages on this machine")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.setenv("LANGUAGE", "de")
    baseline = git_head(repository)
    empty_repository(repository / NESTED)
    assert snapshot_tree(repository, baseline)[1] == (NESTED,)
    (repository / "new.txt").write_text("work\n")
    held = repository / ".git" / "refs" / "subfleet-salvage" / "task-example-20260927T191912Z-a1.lock"
    held.parent.mkdir(parents=True)
    held.touch()
    with pytest.raises(SalvageError, match="File exists") as caught:
        salvage(repository, baseline, 1, timestamp="2026-09-27T19:19:12Z")
    assert caught.value.transient and "fatal:" in str(caught.value)



def test_a_repository_the_daemons_environment_names_is_not_where_salvage_writes(repository, tmp_path_factory,
                                                                                   monkeypatch):
    """Adversarial review of the round-3 branch: with `GIT_DIR` (and the rest) inherited by
    the daemon, as from a `subfleet daemon start` run in a git hook, git went to that
    repository before `-C`: the salvage wrote its ref there and reported success. They
    are dropped from every salvage call's environment; a caller's temporary index is kept."""
    other = tmp_path_factory.mktemp("other")
    git(other, "init", "-q")
    for name, value in [("GIT_DIR", str(other / ".git")), ("GIT_WORK_TREE", str(other)),
                        ("GIT_INDEX_FILE", str(other / ".git" / "index")),
                        ("GIT_OBJECT_DIRECTORY", str(other / ".git" / "objects"))]:
        monkeypatch.setenv(name, value)
    env = salvage_module._git_env(None)
    assert not set(env) & {*salvage_module.GIT_LOCATION_ENV, "GIT_INDEX_FILE"}
    assert salvage_module._git_env({**os.environ, "GIT_INDEX_FILE": "/t/index"})["GIT_INDEX_FILE"] == "/t/index"
    baseline = git_head(repository)
    (repository / "tracked.txt").write_text("provider progress\n")
    result = salvage(repository, baseline, 1, timestamp="2026-09-29T09:00:00Z")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        monkeypatch.delenv(name)
    assert git(repository, "show", f"{result.ref}:tracked.txt") == "provider progress"
    assert git(other, "for-each-ref") == ""


@pytest.mark.parametrize("name", ["GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS",
                                  "GIT_ICASE_PATHSPECS"])
def test_a_way_to_read_pathspecs_the_daemons_environment_names_keeps_the_exclusion(repository, monkeypatch,
                                                                                    name):
    """Review of ceacf18b, P3-3: with `GIT_LITERAL_PATHSPECS=1` inherited, git read the
    exclusion `:(top,exclude,literal)scratch/empty/` as a file of that name, so `add -A`
    failed on the empty repository again and the salvage was recorded at once with no
    ref: the incident's case. Salvage's git drops all four variables, and the
    repository is left out and named as without them."""
    baseline = git_head(repository)
    (repository / "tracked.txt").write_text("provider progress\n")
    empty_repository(repository / "scratch" / "empty")
    monkeypatch.setenv(name, "1")
    result = salvage(repository, baseline, 1, timestamp="2026-09-29T09:00:00Z")
    assert result.skipped == ("scratch/empty/",)
    assert name not in salvage_module._git_env(None)
    assert name not in salvage_module._git_env({**os.environ, "GIT_INDEX_FILE": "/t/index"})
    monkeypatch.delenv(name)
    assert git(repository, "show", f"{result.ref}:tracked.txt") == "provider progress"
    assert git(repository, "ls-tree", "-r", "--name-only", result.ref).split() == [".gitignore", "tracked.txt"]


# --- names (F4) -------------------------------------------------------------------


def test_a_name_that_is_not_utf8_is_carried_and_excluded_byte_for_byte(repository, monkeypatch):
    """APFS refuses such names, so the listing is faked; the name reaches git's pathspec
    in its own bytes and comes back as `os.fsdecode` names it."""
    real, specs = subprocess.run, []

    def run(argv, *args, **kwargs):
        if argv[3:5] == ["add", "-A"]:
            specs.append(kwargs.get("input"))
            return subprocess.CompletedProcess(argv, 1 if len(specs) == 1 else 0, b"", b"")
        return real(argv, *args, **kwargs)
    monkeypatch.setattr(salvage_module.subprocess, "run", run)
    monkeypatch.setattr(salvage_module, "_uncommitted_repositories", lambda *a, **k: [b"caf\xe9/"])
    skipped = _add_all(repository, {})
    assert specs == [None, b":/\0:(top,exclude,literal)caf\xe9/\0"]
    assert [os.fsencode(path) for path in skipped] == [b"caf\xe9/"]
    assert path_text(skipped[0]) == "caf\\xe9/"


@hypothesis.settings(deadline=None)
@hypothesis.given(st.one_of(st.binary(max_size=16), st.text(max_size=12).map(os.fsencode)))
def test_path_text_is_one_line_of_valid_utf8_and_leaves_a_printable_name_alone(raw):
    """Invariants over any file name, as `os.fsdecode` carries it (bytes that are not
    UTF-8 as surrogates): it renders as valid UTF-8 with no line break or NUL, the
    same every time, and a printable UTF-8 name renders as itself."""
    text = path_text(os.fsdecode(raw))
    text.encode("utf-8")                                   # never raises
    assert not {"\n", "\r", "\0"} & set(text) and path_text(os.fsdecode(raw)) == text
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError:
        return
    if name.isprintable():
        assert text == name


# --- error text that is not UTF-8 (review of cda4c161, N1) ----------------------------


def on_a_branch_whose_name_is_not_utf8(repository) -> str:
    """Check out `caf\\xe9`: git allows any byte above 0x7f in a ref name, and APFS
    refuses it only as a file name, so the ref is packed. Returns the name as
    `os.fsdecode` carries it."""
    head = git_head(repository)
    with open(repository / ".git" / "packed-refs", "ab") as packed:
        packed.write(head.encode() + b" refs/heads/caf\xe9\n")
    (repository / ".git" / "HEAD").write_bytes(b"ref: refs/heads/caf\xe9\n")
    return os.fsdecode(b"caf\xe9")


def test_git_carries_a_name_that_is_not_utf8_in_what_it_prints(repository):
    """Real git: `_git` read both streams as strict UTF-8 (`text=True`), so a branch name,
    or a name git quoted in an error, that is not UTF-8 raised `UnicodeDecodeError`,
    which no caller catches (not a `SalvageError`, not an `OSError`): finalization
    raised on every try, and admission too. Both are read as `os.fsdecode` reads them."""
    name = on_a_branch_whose_name_is_not_utf8(repository)
    assert salvage_module.git_branch(repository) == name
    with pytest.raises(SalvageError) as caught:
        _git(repository, "cat-file", "-t", os.fsdecode(b"no-caf\xe9"))    # git echoes the name
    assert str(caught.value) == "git cat-file failed: fatal: Not a valid object name no-caf\\xe9"
    assert not caught.value.transient


def test_a_salvage_on_a_branch_whose_name_is_not_utf8_writes_its_ref(repository):
    baseline = git_head(repository)
    on_a_branch_whose_name_is_not_utf8(repository)
    (repository / "tracked.txt").write_text("provider progress\n")
    result = salvage(repository, baseline, 1, timestamp="2026-09-28T12:00:00Z")
    assert result.ref == "refs/subfleet-salvage/caf-20260928T120000Z-a1"
    assert git(repository, "show", f"{result.ref}:tracked.txt") == "provider progress"



def test_a_branch_whose_sanitized_name_is_one_long_part_still_gets_its_ref(repository):
    """Adversarial review of the round-3 branch: each part of `a/b/...` fits a file name,
    but sanitized to one part (`a-b-...`) it did not, and `update-ref` failed on every
    salvage from that branch. The branch part of the ref is cut to `BRANCH_SLUG_MAX`."""
    baseline = git_head(repository)
    branch = "/".join(["x" * 60] * 5)                          # 304 characters, five parts
    git(repository, "checkout", "-q", "-b", branch)
    (repository / "tracked.txt").write_text("provider progress\n")
    result = salvage(repository, baseline, 1, timestamp="2026-09-29T09:00:00Z")
    name = result.ref.removeprefix("refs/subfleet-salvage/")
    assert "/" not in name and name.endswith("-20260929T090000Z-a1")
    assert len(name) + len("-") + 12 + len(".lock") <= 255     # `_hold`'s `-<tree>` and git's lock fit
    assert git(repository, "show", f"{result.ref}:tracked.txt") == "provider progress"


def test_an_add_that_quotes_a_name_that_is_not_utf8_fails_with_valid_utf8(repository, monkeypatch):
    """git's `error()` prints a path in its own bytes (Linux allows such names; APFS does
    not, so only `add -A` is faked). The message carried it as a surrogate, which
    `json_bytes` could not encode: the receipt could not be written, on any try."""
    from subfleet.daemon import json_bytes
    (repository / "new.txt").write_text("work\n")
    fake_add(monkeypatch, 128, b'error: open("caf\xe9.txt"): Permission denied\n'
                               b"error: unable to index file 'caf\xe9.txt'\nfatal: adding files failed\n")
    with pytest.raises(SalvageError) as caught:
        snapshot_tree(repository, git_head(repository))
    assert str(caught.value) == ('git add failed: error: open("caf\\xe9.txt"): Permission denied\n'
                                 "error: unable to index file 'caf\\xe9.txt'\nfatal: adding files failed")
    assert not caught.value.transient
    json_bytes({"error": str(caught.value)})                   # never raises


@hypothesis.settings(deadline=None)
@hypothesis.given(st.binary(max_size=24))
def test_utf8_text_of_what_the_file_system_names_is_pythons_own_backslashreplace(raw):
    """Differential, over any bytes: `utf8_text(os.fsdecode(raw))` is what Python's own
    codec makes of them, `raw.decode("utf-8", "backslashreplace")`: valid UTF-8, each
    byte that is not escaped as `\\xNN`, every valid character (line breaks too) kept."""
    assert utf8_text(os.fsdecode(raw)) == raw.decode("utf-8", "backslashreplace")


#: Any `str`: every code point, lone surrogates included (`st.characters` leaves them out).
ANY_TEXT = st.lists(st.one_of(st.integers(0xD800, 0xDFFF), st.integers(0, 0x10FFFF)), max_size=24).map(
    lambda points: "".join(map(chr, points)))


@hypothesis.settings(deadline=None)
@hypothesis.given(ANY_TEXT)
def test_utf8_text_is_always_valid_utf8_and_leaves_valid_text_alone(text):
    """Invariants over any `str`, lone surrogates of every kind included: the result
    encodes as strict UTF-8, so does any `SalvageError`'s message, and text with no
    surrogate is returned unchanged (so is the result: it is idempotent)."""
    result = utf8_text(text)
    result.encode("utf-8")                                     # never raises
    str(SalvageError(text)).encode("utf-8")
    assert utf8_text(result) == result
    if not any(0xD800 <= ord(char) <= 0xDFFF for char in text):
        assert result == text


# --- the rule, against git's own outcome (property) -----------------------------------

KINDS = ("file", "empty-repository", "bare-init", "committed-repository", "separate-empty-repository",
         "dot-git-not-a-repository")
PLACES = ("", "untracked/", "tracked/", "ignored/")
NAME = st.text(st.sampled_from("abc xy'*?[é-"), min_size=1, max_size=5).filter(
    lambda n: n.strip(". ") == n.strip(" ") and n not in (".", ".."))
LAYOUT = st.lists(st.tuples(st.sampled_from(PLACES), NAME, st.sampled_from(KINDS)), max_size=6,
                  unique_by=lambda entry: (entry[0], entry[1].casefold()))
NO_COMMIT = {"empty-repository", "bare-init", "separate-empty-repository"}


def build(layout, scratch: Path) -> tuple[Path, str, set[str]]:
    """A checkout with `layout` over a baseline commit, and the nested repositories in it
    that have no commit and are not ignored: what the generator made, not what the rule says."""
    top = scratch / "repo"
    top.mkdir()
    git(top, "init", "-q", "-b", "task/p")
    (top / ".gitignore").write_text("ignored/\n")
    (top / "tracked").mkdir()
    (top / "tracked" / "kept.txt").write_text("baseline\n")
    git(top, "add", ".")
    git(top, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-qm", "baseline")
    expected = set()
    for index, (place, name, kind) in enumerate(layout):
        path = top / place / name
        path.mkdir(parents=True)
        if kind == "file":
            path.rmdir()
            path.write_text(f"file {index}\n")
            continue
        if kind == "dot-git-not-a-repository":
            (path / ".git").mkdir()
            (path / "inside.txt").write_text(f"plain {index}\n")
            continue
        if kind == "separate-empty-repository":
            git(path, "init", "-q", "--separate-git-dir", str(scratch / f"gitdir-{index}"))
        else:
            git(path, "init", "-q")
        if kind != "bare-init":
            (path / "inside.txt").write_text(f"inside {index}\n")
        if kind == "committed-repository":
            git(path, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "add", ".")
            git(path, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-qm", "c")
        if kind in NO_COMMIT and place != "ignored/":
            expected.add(f"{place}{name}/")
    (top / "tracked" / "kept.txt").write_text("changed\n")
    return top, git(top, "rev-parse", "HEAD"), expected


def lenient_add(top: Path, baseline: str) -> subprocess.CompletedProcess:
    """git's own `add -A --ignore-errors` into a scratch index, then `write-tree` into
    `.stdout` (on success): what git itself can index of the working tree."""
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(scratch, "index"), "LC_ALL": "C"}
        subprocess.run(["git", "-C", str(top), "read-tree", baseline], env=env, check=True)
        added = subprocess.run(["git", "-C", str(top), "add", "-A", "--ignore-errors"], env=env,
                               capture_output=True)
        tree = subprocess.run(["git", "-C", str(top), "write-tree"], env=env, capture_output=True)
    return subprocess.CompletedProcess(added.args, added.returncode, tree.stdout.strip(), added.stderr)


def plain_add_fails(top: Path, baseline: str) -> bool:
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(scratch, "index")}
        subprocess.run(["git", "-C", str(top), "read-tree", baseline], env=env, check=True)
        return subprocess.run(["git", "-C", str(top), "add", "-A"], env=env, capture_output=True).returncode != 0


@hypothesis.settings(deadline=None, max_examples=40, suppress_health_check=[hypothesis.HealthCheck.too_slow])
@hypothesis.given(LAYOUT)
def test_the_snapshot_is_what_git_itself_can_index_and_leaves_out_only_what_it_refuses(layout):
    """Invariants over any layout of files and nested repositories (with and without a
    commit, a `.git` file, a `.git` that is not a repository; at the top, under an
    untracked, a tracked and an ignored directory; names with spaces, quotes, glob
    characters and accents), each checked against real git:
    - the snapshot's tree is the tree git's own `add -A --ignore-errors` writes;
    - it leaves out exactly the nested repositories the layout made with no commit
      (outside the ignored directory);
    - it leaves anything out exactly when a plain `add -A` fails;
    - the real index is untouched."""
    scratch = Path(tempfile.mkdtemp(prefix="unindexable-", dir=os.environ.get("TMPDIR")))
    try:
        top, baseline, expected = build(layout, scratch)
        index = (top / ".git" / "index").read_bytes()
        tree, skipped = snapshot_tree(top, baseline)
        lenient = lenient_add(top, baseline)
        assert tree.encode() == lenient.stdout
        assert len(skipped) == len(set(skipped)) and set(skipped) == expected
        assert bool(skipped) == plain_add_fails(top, baseline)
        assert (top / ".git" / "index").read_bytes() == index
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@ROOT
@hypothesis.settings(deadline=None, max_examples=25, suppress_health_check=[hypothesis.HealthCheck.too_slow])
@hypothesis.given(st.lists(st.tuples(st.text(st.sampled_from("abcdef"), min_size=1, max_size=4), st.booleans()),
                           min_size=1, max_size=5, unique_by=lambda entry: entry[0]),
                  st.booleans())
def test_an_object_git_cannot_write_always_fails_the_snapshot(files, beside):
    """Invariant over any set of new files, some whose blobs git cannot write (their
    `.git/objects/xx` read-only, real git), with or without a nested repository with no
    commit beside them: the snapshot fails, not transiently, exactly when git failed to
    write an object (git's own `add -A --ignore-errors` exits non-zero), and otherwise
    is git's own tree."""
    scratch = Path(tempfile.mkdtemp(prefix="unwritable-", dir=os.environ.get("TMPDIR")))
    blocked, blobs = [], []
    try:
        top, baseline, _ = build([("untracked/", "repo", "empty-repository")] if beside else [], scratch)
        for name, block in files:
            path = top / f"new-{name}.txt"
            path.write_text(f"new work {name}\n")
            blobs.append(git(top, "hash-object", str(path)))
            if block:
                blocked.append(block_object(top, path))
        lenient = lenient_add(top, baseline)
        # What git did, read from the repository: every blob is in it, and a tree was written.
        wrote_everything = bool(lenient.stdout) and all(
            subprocess.run(["git", "-C", str(top), "cat-file", "-e", blob]).returncode == 0 for blob in blobs)
        if blocked:
            assert not wrote_everything          # the fixture really blocks a write
        try:
            tree, _ = snapshot_tree(top, baseline)
        except SalvageError as exc:
            assert not wrote_everything and not exc.transient
        else:
            assert wrote_everything and tree.encode() == lenient.stdout
    finally:
        unblock(*blocked)
        shutil.rmtree(scratch, ignore_errors=True)


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
