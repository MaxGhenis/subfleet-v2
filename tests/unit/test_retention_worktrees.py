"""Retention never touches a worktree; a job is pruned only once its worktree is gone for real
(C-8.4, C-13.4; design revision 3, invariants W1 and W2).

The machine's worktree archiver (chief-of-staff's worktree-archive-sweep) reclaims
allocated worktrees on its own schedule. It quarantines a tree by renaming it to
`.disk-guard-removing.<name>` beside the original and may rename it back. So "gone"
means: no entry under the allocated root is the tree or a set-aside copy of it, and
the job's repository registers no existing checkout that is that tree under any name.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from subfleet import retention
from subfleet.store import Store


_RUN = subprocess.run          # the test's own Git, not what the W1 guard watches


def git(cwd, *args):
    result = _RUN(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def owned(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-q", "-b", "feature")
    (repository / "tracked").write_text("base" * 1024)
    git(repository, "add", "tracked")
    git(repository, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "baseline")
    root = tmp_path / "state"
    (root / "worktrees").mkdir(parents=True)
    with Store(root / "state.sqlite3") as store:
        yield store, root, repository


def add_job(store, root, repository, identity, *, order=0, worktree=True, state="succeeded", **fields):
    path = None
    if worktree:
        path = root / "worktrees" / identity
        git(repository, "worktree", "add", "-q", "--detach", str(path), "HEAD")
        (path / "untracked-result.csv").write_text(f"{identity},42\n")
    store.add_job(job_id=identity, request_id=identity, payload_digest="digest", kind="dispatch",
                  workdir=str(repository), worktree=str(path) if path else None, prompt_path="/prompt",
                  sandbox="workspace-write", state=state, created_at=f"2026-01-01T00:00:{order:02d}Z",
                  finished_at="2026-01-02T00:00:00Z", **fields)
    directory = root / "jobs" / identity
    directory.mkdir(parents=True)
    (directory / "stdout").write_bytes(b"output")
    return path


def archive_like_the_sweep(repository, worktree):
    """What the machine's archiver does: set the tree aside (with `git worktree move`, as
    disk-guard quarantines), then remove it and its registration."""
    quarantine = worktree.parent / f".disk-guard-removing.{worktree.name}"
    git(repository, "worktree", "move", str(worktree), str(quarantine))
    return quarantine


def forbid_worktree_writes(monkeypatch, worktrees):
    """W1: fail the test if retention (this process) deletes, moves or writes below a worktree."""
    roots = [str(Path(path).resolve()) for path in worktrees]

    def inside(path):
        path = os.fspath(path)
        return any(path == root or path.startswith(root + "/") for root in roots)

    for name in ("unlink", "rmdir", "remove", "rename", "replace", "chmod", "mkdir"):
        original = getattr(os, name)

        def guard(*args, _original=original, _name=name, **kwargs):
            if any(inside(arg) for arg in args if isinstance(arg, (str, os.PathLike))):
                pytest.fail(f"retention called os.{_name} on a worktree path: {args}")
            return _original(*args, **kwargs)
        monkeypatch.setattr(os, name, guard)
    original_rmtree = shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if inside(path):
            pytest.fail(f"retention removed a worktree tree: {path}")
        return original_rmtree(path, *args, **kwargs)
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    original_run = subprocess.run

    def run(argv, *args, **kwargs):
        text = " ".join(map(str, argv))
        if any(word in text for word in (" worktree remove", " worktree prune", " worktree move", " gc", " prune")):
            pytest.fail(f"retention ran a Git command that changes worktrees or objects: {text}")
        return original_run(argv, *args, **kwargs)
    monkeypatch.setattr(retention.subprocess, "run", run)


def test_w1_w2_a_job_whose_worktree_exists_is_kept_and_its_tree_untouched(owned, monkeypatch):
    """C-13.4 W1, W2: retention leaves the tree and keeps its job, saying why."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    before = sorted(p.name for p in worktree.iterdir())
    forbid_worktree_writes(monkeypatch, [worktree])
    result = retention.maintenance(store, root, max_jobs=0, max_bytes=0)
    assert result["pruned"] == []
    assert "job" in result["protected"]
    assert result["kept"]["job"].startswith("worktree present")
    assert sorted(p.name for p in worktree.iterdir()) == before
    assert store.get_job("job") is not None and (root / "jobs" / "job").is_dir()
    assert store.list_leases() == []


def test_w2_a_quarantined_tree_still_counts_as_present(owned, monkeypatch):
    """Review of rev 2, finding 3: while the archiver has the tree set aside, the job stays;
    if the archiver renames it back, nothing was lost and nothing is orphaned."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    quarantine = archive_like_the_sweep(repository, worktree)
    forbid_worktree_writes(monkeypatch, [quarantine])
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == [] and "disk-guard-removing" in result["kept"]["job"]
    git(repository, "worktree", "move", str(quarantine), str(worktree))      # the archiver backs off
    assert retention.maintenance(store, root, max_jobs=0)["pruned"] == []
    assert store.get_job("job") is not None


def test_w2_a_tree_moved_elsewhere_is_found_through_its_registration(owned, tmp_path):
    """Review of rev 2, finding 3: a registration whose checkout exists under the tree's name
    anywhere keeps the job."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    elsewhere = tmp_path / "set-aside"
    elsewhere.mkdir()
    git(repository, "worktree", "move", str(worktree), str(elsewhere / f"quarantine.{worktree.name}"))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == [] and "under another name" in result["kept"]["job"]


def test_w2_a_job_goes_once_the_archiver_has_removed_its_tree(owned, monkeypatch):
    """C-8.4 W2: once the tree and its registration are gone, the job's records are retired."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    quarantine = archive_like_the_sweep(repository, worktree)
    git(repository, "worktree", "remove", "--force", str(quarantine))
    forbid_worktree_writes(monkeypatch, [worktree, quarantine])
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["job"]
    assert (root / "archive" / "job" / "manifest.json").is_file()
    assert store.list_leases() == []


def test_w2_a_tree_deleted_by_hand_leaves_a_prunable_registration(owned):
    """A registration whose checkout no longer exists (prunable) does not hold the job."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    shutil.rmtree(worktree)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["job"]
    assert str(worktree) in git(repository, "worktree", "list", "--porcelain")     # retention pruned nothing


def test_w2_a_repository_that_cannot_be_read_keeps_the_job(owned, monkeypatch):
    """Fail closed: when the repository exists but Git cannot answer, the tree may exist."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    elsewhere = worktree.parent.parent / "elsewhere"
    elsewhere.mkdir()
    git(repository, "worktree", "move", str(worktree), str(elsewhere / "x"))
    monkeypatch.setattr(retention, "_registered_checkouts", lambda workdir: None)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == [] and "could not be read" in result["kept"]["job"]


def test_w2_a_repository_that_is_gone_takes_its_registrations_with_it(owned):
    """R1-7: the tree and the repository both gone: nothing can hold the job."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    shutil.rmtree(worktree)
    shutil.rmtree(repository)
    assert retention.maintenance(store, root, max_jobs=0)["pruned"] == ["job"]


def test_retention_takes_no_path_lease(owned, monkeypatch):
    """Disk-guard treats every `worktree:/...` or `out:/...` lease as a tree in use; retention's
    fence is `retire:<job>`, which names no path."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    shutil.rmtree(worktree)
    seen = []
    original = retention.archive.archive

    def archive(*args, **kwargs):
        seen.append([(row["lease_key"], row["holder"]) for row in store.list_leases()])
        return original(*args, **kwargs)
    monkeypatch.setattr(retention.archive, "archive", archive)
    retention.maintenance(store, root, max_jobs=0)
    assert seen == [[("retire:job", "retention:job")]]


def test_a_legacy_removal_lease_is_released_and_the_job_retired(owned):
    """C-8.4: the installed code's `worktree:` lease held by `retention:<job>` is released,
    and the job is then retired as any other once its tree is gone."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    assert store.acquire_lease(f"worktree:{worktree.resolve()}", "retention:job")
    git(repository, "worktree", "remove", "--force", str(worktree))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["job"]
    assert store.get_job("job") is None
    assert store.list_leases() == []


def test_a_recent_job_keeps_its_record_for_a_day(owned):
    """Review of rev 2, finding 5: with the daemon's MIN_AGE_S, a job that ended an hour ago
    is not pruned however far over budget its pool is."""
    from datetime import UTC, datetime, timedelta
    store, root, repository = owned
    store.add_job(job_id="recent", request_id="recent", payload_digest="digest", kind="dispatch",
                  workdir=str(repository), prompt_path="/prompt", sandbox="read-only", state="succeeded",
                  finished_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    (root / "jobs" / "recent").mkdir(parents=True)
    result = retention.maintenance(store, root, max_jobs=0, max_bytes=0, min_age_s=retention.MIN_AGE_S)
    assert result["pruned"] == [] and "recent" in result["protected"]
    assert retention.maintenance(store, root, max_jobs=0, max_bytes=0)["pruned"] == ["recent"]


def test_c13_4_retention_never_touches_in_place_workdir(owned, monkeypatch):
    """C-13.4, C-8.4: pruning an in-place job leaves the caller's files and Git index intact."""
    store, root, repository = owned
    add_job(store, root, repository, "job", worktree=False)
    store.update_job("job", in_place=1, worktree=str(repository))
    before = (repository / ".git" / "index").read_bytes()
    forbid_worktree_writes(monkeypatch, [repository])
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["job"]
    assert (repository / "tracked").read_text() == "base" * 1024
    assert (repository / ".git" / "index").read_bytes() == before


@pytest.mark.parametrize("path_kind", ["outside", "symlink", "container-symlink"])
def test_c13_4_retention_rejects_worktree_paths_outside_owned_root(owned, path_kind):
    """C-13.4, C-2.1: resolved paths and symlinked containers never count as an owned tree."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    git(repository, "worktree", "remove", "--force", str(worktree))
    if path_kind == "outside":
        store.update_job("job", worktree=str(repository))
    elif path_kind == "symlink":
        worktree.symlink_to(repository, target_is_directory=True)
    else:
        (root / "worktrees").rmdir()
        (root / "worktrees").symlink_to(repository.parent, target_is_directory=True)
        store.update_job("job", worktree=str(root / "worktrees" / "repository"))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == []
    assert "job" in result["protected"]
    assert result["errors"] or result["kept"]
    assert store.get_job("job") is not None
    assert (repository / "tracked").exists()


def test_w2_a_workdir_that_is_no_longer_a_repository_registers_nothing(owned):
    """A workdir whose `.git` is gone cannot hold a registration; the job is not kept forever."""
    store, root, repository = owned
    worktree = add_job(store, root, repository, "job")
    shutil.rmtree(worktree)
    shutil.rmtree(repository / ".git")
    assert retention.maintenance(store, root, max_jobs=0)["pruned"] == ["job"]
