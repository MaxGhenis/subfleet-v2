"""C-6.8 ownership reads use the repository, index and filesystem as independent evidence."""
from __future__ import annotations

import errno
import itertools
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import retention_git as rg, worktree_add as wa
from subfleet.salvage import SalvageError, TRANSIENT_ERRNOS


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, env=wa._git_env(None)).stdout


@pytest.fixture
def checkout(tmp_path):
    sequence = itertools.count()

    def make():
        case = tmp_path / str(next(sequence))
        repository, tree = case / "repo", case / "tree"
        repository.mkdir(parents=True)
        git(repository, "init", "--quiet", "--template=")
        git(repository, "config", "user.name", "Test")
        git(repository, "config", "user.email", "test@example.invalid")
        (repository / "tracked").write_text("baseline\n")
        (repository / "nested").mkdir()
        (repository / "nested" / "item").write_text("nested\n")
        (repository / ".gitignore").write_text("ignored\n")
        git(repository, "add", ".")
        git(repository, "commit", "--quiet", "-m", "baseline")
        commit = git(repository, "rev-parse", "HEAD").decode().strip()
        git(repository, "commit", "--quiet", "--allow-empty", "-m", "other")
        other = git(repository, "rev-parse", "HEAD").decode().strip()
        git(repository, "worktree", "add", "--quiet", "--detach", str(tree), commit)
        found, why = rg.registration(tree)
        assert found is not None and why is None
        return repository, tree, found, commit, other

    return make


PROPERTY = settings(max_examples=36, deadline=None,
                    suppress_health_check=[HealthCheck.function_scoped_fixture])


@PROPERTY
@given(gitfile=st.sampled_from(["own", "absent", "other-tree", "other-repo", "link"]),
       lock=st.sampled_from([None, b"initializing\n", b"person\n", b"subfleet retention: job\n"]),
       correct_head=st.booleans(), clean_index=st.booleans())
def test_generated_finished_checkouts_match_pure_oracle(checkout, gitfile, lock,
                                                       correct_head, clean_index):
    """Invariant 3, F2: every reuse condition is independent of the checkout's gitfile."""
    repository, tree, found, commit, other = checkout()
    if not clean_index:
        (tree / "tracked").write_text("staged\n")
        git(tree, "add", "tracked")
    (found.admin / "HEAD").write_text((commit if correct_head else other) + "\n")
    if lock is not None:
        (found.admin / "locked").write_bytes(lock)
    if gitfile == "absent":
        (tree / ".git").unlink()
    elif gitfile in ("other-tree", "other-repo"):
        source = repository
        if gitfile == "other-repo":
            source = repository.parent / "foreign"
            git(repository, "clone", "--quiet", str(repository), str(source))
        foreign = repository.parent / "foreign-tree"
        git(source, "worktree", "add", "--quiet", "--detach", str(foreign), commit)
        (tree / ".git").write_bytes((foreign / ".git").read_bytes())
    elif gitfile == "link":
        saved = repository.parent / "saved-gitfile"
        saved.write_bytes((tree / ".git").read_bytes())
        (tree / ".git").unlink()
        (tree / ".git").symlink_to(saved)
    _, why = wa.finished(tree, found.common, commit, timeout_s=10)
    expected = gitfile == "own" and lock != b"initializing\n" and correct_head and clean_index
    assert (why is None) == expected


@PROPERTY
@given(shape=st.sampled_from(["missing", "empty", "link", "file", "subset", "whole", "extra", "wrong-kind", "half-copy"]),
       index=st.sampled_from(["clean", "modified", "added", "deleted"]))
def test_generated_removal_proof_matches_pure_oracle(checkout, shape, index):
    """Invariant 4, F1: an index is checked before even a missing or linked path."""
    _, tree, found, commit, _ = checkout()
    if index == "modified":
        (tree / "tracked").write_text("staged\n")
        git(tree, "add", "tracked")
    elif index == "added":
        (tree / "staged-new").write_text("saved\n")
        git(tree, "add", "staged-new")
    elif index == "deleted":
        git(tree, "rm", "--quiet", "tracked")
    if shape in ("missing", "empty", "link", "file"):
        shutil.rmtree(tree)
        if shape == "empty":
            tree.mkdir()
        elif shape == "link":
            tree.symlink_to(found.common, target_is_directory=True)
        elif shape == "file":
            tree.write_text("not an add\n")
    elif shape == "subset":
        shutil.rmtree(tree / "nested")
        (tree / "tracked").unlink(missing_ok=True)
    elif shape == "extra":
        (tree / "extra").write_text("preserve\n")
    elif shape == "wrong-kind":
        shutil.rmtree(tree / "nested")
        (tree / "nested").write_text("file in place of directory\n")
    elif shape == "half-copy":
        (tree / "copied").mkdir()
        (tree / "copied" / "new").write_text("job's kept copy\n")
    kept = {"copied": "directory", "copied/new": "file"}
    before_index = (found.admin / "index").read_bytes()
    why = wa.prove(tree, found.admin, found.common, commit, timeout_s=10, kept=kept)
    expected = shape in ("missing", "empty", "subset", "whole", "half-copy") and index in ("clean", "deleted")
    assert (why is None) == expected
    assert (found.admin / "index").read_bytes() == before_index
    assert os.path.lexists(tree) == (shape != "missing")


@pytest.mark.parametrize("shape", ["missing", "empty", "link", "file"])
@pytest.mark.parametrize("index", ["modified", "added"])
def test_staged_index_blocks_removal_at_every_path_shape(checkout, shape, index):
    """F1 checks the index for every path shape, including a non-directory path."""
    _, tree, found, commit, _ = checkout()
    name = "tracked" if index == "modified" else "added"
    (tree / name).write_text("valuable staged bytes\n")
    git(tree, "add", name)
    shutil.rmtree(tree)
    if shape == "empty":
        tree.mkdir()
    elif shape == "link":
        tree.symlink_to(found.common)
    elif shape == "file":
        tree.write_text("not a checkout\n")
    assert "index stages" in wa.prove(tree, found.admin, found.common, commit, timeout_s=10)
    removed, why = wa.drop_registration(found.common, tree, commit, timeout_s=10)
    assert removed is None and "index stages" in why
    assert (found.admin / "index").is_file()


@PROPERTY
@given(reason=st.sampled_from([None, b"initializing\n", b"person\n", b"subfleet retention: job\n", b""]))
def test_generated_registration_drop_removes_only_add_lock(checkout, reason):
    """Invariant 5: an empty census is the caller's prerequisite; other locks always stay."""
    _, tree, found, commit, _ = checkout()
    if reason is not None:
        (found.admin / "locked").write_bytes(reason)
    removed, why = wa.drop_registration(found.common, tree, commit, timeout_s=10)
    allowed = reason in (None, b"initializing\n")
    assert (removed == found.admin and why is None) == allowed
    assert found.admin.exists() != allowed
    assert tree.is_dir()


@pytest.mark.parametrize("phase", ["gitfile", "backlink", "admin-scan", "lock", "tree-scan", "contents"])
@pytest.mark.parametrize("code", sorted(TRANSIENT_ERRNOS))
def test_transient_ownership_reads_preserve_errno_and_cause(checkout, monkeypatch, phase, code):
    """Invariant 6, F7: resource pressure at every ownership boundary remains retryable."""
    _, tree, found, commit, _ = checkout()
    failure = OSError(code, os.strerror(code))
    real_regular, real_scandir, real_open = rg.rfs.read_regular, os.scandir, os.open

    def regular(path, *args, **kwargs):
        if phase == "gitfile" and Path(path) == tree / ".git" or phase == "backlink" and Path(path) == found.admin / "gitdir" or phase == "lock" and Path(path) == found.admin / "locked":
            raise failure
        return real_regular(path, *args, **kwargs)

    def scan(path):
        if phase == "tree-scan" and Path(path) == tree:
            raise failure
        return real_scandir(path)

    def opened(path, *args, **kwargs):
        if phase == "contents" and path == str(tree / "tracked"):
            raise failure
        return real_open(path, *args, **kwargs)

    def iterdir(path):
        raise failure

    monkeypatch.setattr(rg.rfs, "read_regular", regular)
    monkeypatch.setattr(os, "scandir", scan)
    if phase == "admin-scan":
        monkeypatch.setattr(Path, "iterdir", iterdir)
    if phase == "contents":
        monkeypatch.setattr(os, "open", opened)
    with pytest.raises(SalvageError) as error:
        if phase in ("gitfile", "backlink", "lock"):
            wa.finished(tree, found.common, commit, timeout_s=10)
        elif phase == "admin-scan":
            wa.drop_registration(found.common, tree, commit, timeout_s=10)
        else:
            wa.prove(tree, found.admin, found.common, commit, timeout_s=10)
    assert error.value.transient
    assert error.value.__cause__ is failure


@pytest.mark.parametrize("other", ["TREE", "utfss", "e\u0301"])
def test_filesystem_identity_never_folds_distinct_siblings(checkout, other):
    """Invariant 8, F6: case and Unicode spellings follow the volume's own identity."""
    repository, tree, found, _, _ = checkout()
    first = tree.parent / ("utfß" if other == "utfss" else "é" if other == "e\u0301" else "tree")
    if first != tree:
        git(repository, "worktree", "move", str(tree), str(first))
        tree = first
    sibling = tree.parent / other
    sibling.mkdir(exist_ok=True)
    same = os.path.samefile(tree, sibling)
    assert rg.same_entry(tree, sibling) == same
    assert (rg.registration_of(found.common, sibling, strict=True) == found.admin) == same
    alias = tree.parent / "alias"
    alias.symlink_to(tree, target_is_directory=True)
    assert not rg.same_entry(tree, alias)
    assert rg.registration_of(found.common, alias, strict=True) is None


@PROPERTY
@given(change=st.sampled_from(["clean", "modified", "deleted", "untracked", "ignored", "wrong-kind", "staged-edit", "staged-add", "staged-delete"]))
def test_proof_verdict_agrees_with_git_status_for_generated_checkouts(checkout, change):
    """Invariant 9: git's status identifies ownership changes, preserving partial checkout bytes."""
    _, tree, found, commit, _ = checkout()
    if change in ("modified", "staged-edit"):
        (tree / "tracked").write_text("partly written\n")
    elif change in ("deleted", "staged-delete"):
        (tree / "tracked").unlink()
    elif change in ("untracked", "ignored", "staged-add"):
        (tree / ("ignored" if change == "ignored" else "added")).write_text("new bytes\n")
    elif change == "wrong-kind":
        (tree / "tracked").unlink()
        (tree / "tracked").symlink_to("nested/item")
    if change.startswith("staged-"):
        git(tree, "add", "-A")
    status = subprocess.run(["git", f"--git-dir={found.admin}", f"--work-tree={tree}",
                             "status", "--porcelain=v2", "--untracked-files=all", "--ignored"],
                            check=True, capture_output=True, env=wa._git_env(None)).stdout
    # Porcelain v2's XY: staged edits/adds and worktree type changes are not
    # checkout leftovers. Missing/modified tracked bytes can be a partial checkout.
    records = status.splitlines()
    oracle = all(not record.startswith((b"? ", b"! ")) and
                 (not record.startswith(b"1 ") or record[2:3] in (b".", b"D") and b"T" not in record[2:4])
                 for record in records)
    assert (wa.prove(tree, found.admin, found.common, commit, timeout_s=10) is None) == oracle


def test_kept_copy_names_patch_and_untracked_kinds(checkout):
    repository, tree, found, commit, _ = checkout()
    (repository / "tracked").write_text("own patch\n")
    (repository / "new-directory").mkdir()
    (repository / "new-directory" / "file").write_text("own copied file\n")
    (repository / "new-link").symlink_to("tracked")
    from subfleet import uncommitted
    plan = uncommitted.plan(repository, commit, scratch_root=repository.parent / "scratch", timeout_s=10)
    job = repository.parent / "job"
    uncommitted.keep(plan, job)
    kept = wa.kept_paths(job, timeout_s=10)
    assert kept == {"tracked": "file", "new-directory": "directory", "new-directory/file": "file", "new-link": "link"}
    (tree / "new-directory").mkdir()
    (tree / "new-directory" / "file").write_text("half copied\n")
    assert wa.prove(tree, found.admin, found.common, commit, timeout_s=10, kept=kept) is None
    (tree / "new-link").write_text("wrong kind for saved link\n")
    assert wa.prove(tree, found.admin, found.common, commit, timeout_s=10, kept=kept) is not None


@pytest.mark.parametrize("original,destination", [("file", "file"), ("link", "link"),
                                                 ("file", "link"), ("link", "file")])
def test_kept_patch_authorizes_its_actual_kinds_only(checkout, original, destination):
    """A copied patch permits its real type transition, never an unrelated type."""
    repository, tree, found, commit, _ = checkout()
    if original == "link":
        (repository / "tracked").unlink()
        (repository / "tracked").symlink_to("nested/item")
        git(repository, "add", "tracked")
        git(repository, "commit", "--quiet", "-m", "link baseline")
        commit = git(repository, "rev-parse", "HEAD").decode().strip()
        git(tree, "reset", "--hard", commit)
    (repository / "tracked").unlink()
    if destination == "file":
        (repository / "tracked").write_text("kept regular-file bytes\n")
    else:
        (repository / "tracked").symlink_to(".gitignore")
    from subfleet import uncommitted
    plan = uncommitted.plan(repository, commit, scratch_root=repository.parent / "scratch", timeout_s=10)
    job = repository.parent / "job"
    uncommitted.keep(plan, job)
    kept = wa.kept_paths(job, timeout_s=10)
    assert kept["tracked"] == destination
    assert wa.prove(tree, found.admin, found.common, commit, timeout_s=10, kept=kept) is None
    uncommitted.apply(job, tree, timeout_s=10)
    assert wa.prove(tree, found.admin, found.common, commit, timeout_s=10, kept=kept) is None
    if original == destination:
        (tree / "tracked").unlink()
        if destination == "file":
            (tree / "tracked").symlink_to("unrelated-target")
        else:
            (tree / "tracked").write_text("unrelated regular-file bytes\n")
        assert wa.prove(tree, found.admin, found.common, commit, timeout_s=10, kept=kept) is not None


def test_drop_registration_keeps_unrelated_missing_tree_registration(checkout):
    repository, tree, found, commit, _ = checkout()
    unrelated = repository.parent / "unrelated"
    git(repository, "worktree", "add", "--quiet", "--detach", str(unrelated), commit)
    registration, _ = rg.registration(unrelated)
    shutil.rmtree(unrelated)
    removed, why = wa.drop_registration(found.common, tree, commit, timeout_s=10)
    assert removed == found.admin and why is None
    assert registration.admin.is_dir()


def test_registration_drop_reports_failed_remove(checkout, monkeypatch):
    _, tree, found, commit, _ = checkout()
    monkeypatch.setattr(wa.shutil, "rmtree", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError(errno.EACCES, "denied")))
    removed, why = wa.drop_registration(found.common, tree, commit, timeout_s=10)
    assert removed is None and why is not None
    assert found.admin.is_dir()


def test_unreadable_lock_never_proves_finished_or_removable(checkout, monkeypatch):
    _, tree, found, commit, _ = checkout()
    real_regular = rg.rfs.read_regular

    def regular(path, *args, **kwargs):
        if Path(path) == found.admin / "locked":
            raise PermissionError(errno.EACCES, "denied")
        return real_regular(path, *args, **kwargs)

    monkeypatch.setattr(rg.rfs, "read_regular", regular)
    assert wa.finished(tree, found.common, commit, timeout_s=10)[1] is not None
    removed, why = wa.drop_registration(found.common, tree, commit, timeout_s=10)
    assert removed is None and why is not None
    assert found.admin.is_dir()


def test_transient_git_head_failure_is_not_a_missing_head(checkout, monkeypatch):
    _, tree, found, commit, _ = checkout()
    monkeypatch.setattr(wa, "_git", lambda *args, **kwargs: subprocess.CompletedProcess(
        ["git"], 128, b"", f"fatal: cannot read index: {os.strerror(errno.EMFILE)}".encode()))
    with pytest.raises(SalvageError) as error:
        wa.finished(tree, found.common, commit, timeout_s=10)
    assert error.value.transient


def test_ambiguous_backlink_is_not_ownership(checkout):
    _, tree, found, commit, _ = checkout()
    (tree / "tracked").write_text("valuable staged bytes\n")
    git(tree, "add", "tracked")
    duplicate = found.admin.parent / "ambiguous"
    shutil.copytree(found.admin, duplicate)
    (tree / ".git").unlink()
    assert rg.registration_of(found.common, tree) is None
    with pytest.raises(OSError, match="more than one registration"):
        rg.registration_of(found.common, tree, strict=True)
    removed, why = wa.drop_registration(found.common, tree, commit, timeout_s=10)
    assert removed is None and why is not None
    assert tree.is_dir() and (found.admin / "index").is_file() and (duplicate / "index").is_file()


def test_object_and_index_reads_ignore_a_missing_configured_worktree(checkout):
    repository, tree, found, commit, _ = checkout()
    git(repository, "config", "core.worktree", str(repository.parent / "missing-submodule"))
    assert wa.commit_kinds(found.common, commit, timeout_s=10)["tracked"] == "file"
    assert wa.finished(tree, found.common, commit, timeout_s=10)[1] is None
    assert wa.prove(tree, found.admin, found.common, commit, timeout_s=10) is None


def test_gitfile_naming_another_worktree_is_rejected_before_reading_its_index(checkout, monkeypatch):
    """F2: a listed registration's HEAD cannot validate a different gitfile's clean index."""
    repository, tree, found, commit, _ = checkout()
    (tree / "tracked").write_text("valuable staged edit\n")
    git(tree, "add", "tracked")
    foreign = repository.parent / "another-tree"
    git(repository, "worktree", "add", "--quiet", "--detach", str(foreign), commit)
    (tree / ".git").write_bytes((foreign / ".git").read_bytes())
    seen = []
    monkeypatch.setattr(wa, "_git", lambda *args, **kwargs: seen.append(args))
    assert wa.finished(tree, found.common, commit, timeout_s=10)[1] is not None
    assert seen == []


@pytest.mark.parametrize("reason", [b"initializing", b"initializing\n"])
def test_initializing_lock_never_proves_finished(checkout, reason):
    _, tree, found, commit, _ = checkout()
    assert wa.finished(tree, found.common, commit, timeout_s=10)[1] is None
    (found.admin / "locked").write_bytes(reason)
    assert wa.finished(tree, found.common, commit, timeout_s=10)[1] is not None


def test_unique_same_tree_commit_prevents_registration_and_tree_removal(checkout):
    repository, tree, found, commit, _ = checkout()
    git(tree, "commit", "--quiet", "--allow-empty", "-m", "only this worktree names it")
    other = git(tree, "rev-parse", "HEAD").decode().strip()
    assert other != commit and git(repository, "for-each-ref", "--contains", other) == b""
    assert wa.finished(tree, found.common, commit, timeout_s=10)[1] is not None
    assert "HEAD" in wa.prove(tree, found.admin, found.common, commit, timeout_s=10)
    removed, why = wa.drop_registration(found.common, tree, commit, timeout_s=10)
    assert removed is None and "HEAD" in why
    assert found.admin.is_dir() and tree.is_dir()


@pytest.mark.parametrize("raw", [None, b"0" * 40 + b"\n", b"0" * 64 + b"\n"])
def test_incomplete_head_is_owned_only_under_initializing_lock(checkout, raw):
    _, tree, found, commit, _ = checkout()
    (found.admin / "index").unlink()  # git has not written its checkout index at this point
    if raw is None:
        (found.admin / "HEAD").unlink()
    else:
        (found.admin / "HEAD").write_bytes(raw)
    assert wa.prove(tree, found.admin, found.common, commit, timeout_s=10) is not None
    (found.admin / "locked").write_bytes(b"initializing\n")
    assert wa.prove(tree, found.admin, found.common, commit, timeout_s=10) is None
    removed, why = wa.drop_registration(found.common, tree, commit, timeout_s=10)
    assert removed == found.admin and why is None


@pytest.mark.parametrize("shape", ["missing-tree", "missing-gitfile"])
def test_registration_scan_never_follows_symlinked_admin_container(checkout, shape):
    repository, tree, found, commit, _ = checkout()
    container = found.admin.parent
    outside = repository.parent / "outside-admins"
    container.rename(outside)
    container.symlink_to(outside, target_is_directory=True)
    if shape == "missing-tree":
        shutil.rmtree(tree)
    else:
        (tree / ".git").unlink()
    with pytest.raises(OSError, match="registration container"):
        rg.registration_of(found.common, tree, strict=True)
    assert wa.prove(tree, found.admin, found.common, commit, timeout_s=10) is not None
    removed, why = wa.drop_registration(found.common, tree, commit, timeout_s=10)
    assert removed is None and why is not None
    assert (outside / found.admin.name / "index").is_file()


@pytest.mark.parametrize("replacement", ["directory-to-file", "file-to-directory"])
def test_kept_patch_kind_transitions_are_owned_before_copy_marker(checkout, replacement):
    repository, tree, found, commit, _ = checkout()
    if replacement == "directory-to-file":
        shutil.rmtree(repository / "nested")
        (repository / "nested").write_text("replacement file\n")
    else:
        (repository / "tracked").unlink()
        (repository / "tracked").mkdir()
        (repository / "tracked" / "child").write_text("replacement directory\n")
    git(repository, "add", "-A")
    from subfleet import uncommitted
    plan = uncommitted.plan(repository, commit, scratch_root=repository.parent / "scratch", timeout_s=10)
    job = repository.parent / "job"
    uncommitted.keep(plan, job)
    kept = wa.kept_paths(job, timeout_s=10)
    if replacement == "directory-to-file":
        shutil.rmtree(tree / "nested")
        (tree / "nested").write_text("partly copied replacement\n")
    else:
        (tree / "tracked").unlink()
        (tree / "tracked").mkdir()
        (tree / "tracked" / "child").write_text("partly copied replacement\n")
    assert not uncommitted.applied(job, tree)
    assert wa.prove(tree, found.admin, found.common, commit, timeout_s=10, kept=kept) is None


@pytest.mark.parametrize("kind", ["link", "directory", "fifo"])
def test_nonregular_index_never_proves_finished_or_removable(checkout, kind):
    _, tree, found, commit, _ = checkout()
    index, saved = found.admin / "index", tree.parent / "saved-index"
    index.rename(saved)
    original = saved.read_bytes()
    if kind == "link":
        index.symlink_to(saved)
    elif kind == "directory":
        index.mkdir()
    else:
        os.mkfifo(index)
    assert wa.finished(tree, found.common, commit, timeout_s=10)[1] is not None
    assert wa.prove(tree, found.admin, found.common, commit, timeout_s=10) is not None
    removed, why = wa.drop_registration(found.common, tree, commit, timeout_s=10)
    assert removed is None and why is not None
    assert found.admin.is_dir() and tree.is_dir()
    assert saved.read_bytes() == original


def test_gitfile_unlink_failure_keeps_every_worktree_file(checkout, monkeypatch):
    _, tree, found, _, _ = checkout()
    real_unlink = os.unlink
    failure = PermissionError(errno.EPERM, "denied")

    def unlink(path, *args, **kwargs):
        if path == ".git":
            raise failure
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    assert wa.remove_tree(tree) is failure
    assert (tree / ".git").is_file()
    assert (tree / "tracked").read_text() == "baseline\n"
    assert (tree / "nested" / "item").read_text() == "nested\n"
    assert found.admin.is_dir()


def test_case_folding_never_overrides_filesystem_identity(checkout, monkeypatch):
    """F6 also runs on a case-insensitive host: model two distinct volume entries."""
    _, tree, _, _, _ = checkout()
    first, second = tree.parent / "case", tree.parent / "CASE"
    identities = {first: os.lstat(tree / "tracked"), second: os.lstat(tree / "nested" / "item")}
    real_lstat, real_samefile = os.lstat, os.path.samefile

    def lstat(path, *args, **kwargs):
        return identities[Path(path)] if Path(path) in identities else real_lstat(path, *args, **kwargs)

    def samefile(left, right):
        if Path(left) in identities and Path(right) in identities:
            return os.path.samestat(identities[Path(left)], identities[Path(right)])
        return real_samefile(left, right)

    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os.path, "samefile", samefile)
    assert not rg.same_entry(first, second)


def test_unrelated_add_before_its_backlink_does_not_hide_owned_registration(checkout):
    _, tree, found, _, _ = checkout()
    incomplete = found.admin.parent / "unrelated-starting-add"
    incomplete.mkdir()
    (incomplete / "locked").write_bytes(b"initializing\n")
    assert rg.registration_of(found.common, tree, strict=True) == found.admin
