"""Allocated worktree retention uses isolated repositories and no process inspection."""

import hashlib
from pathlib import Path
import subprocess

import pytest

from subfleet import retention
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.salvage import salvage
from subfleet.store import Store


def git(cwd, *args):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def owned(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "feature")
    (repository / "tracked").write_text("base" * 1024)
    git(repository, "add", "tracked")
    git(repository, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "baseline")
    root = tmp_path / "state"
    root.mkdir()
    worktree = root / "worktrees" / "job"
    git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    with Store(root / "state.sqlite3") as store:
        store.add_job(job_id="job", request_id="request", payload_digest="digest", kind="dispatch",
                      workdir=str(repository), worktree=str(worktree), prompt_path="/prompt",
                      sandbox="workspace-write", state="succeeded")
        artifact_dir = root / "jobs" / "job"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "stdout").write_bytes(b"output")
        yield store, root, repository, worktree


def record_salvage(store, worktree):
    store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/home/one", "home"), "/home/one", LaneOwner.V2, False))
    store.add_attempt(attempt_id="job/a1", job_id="job", seq=1, lane_id="codex-1", model_requested="gpt-6-astra", state="succeeded")
    result = salvage(worktree, git(worktree, "rev-parse", "HEAD"), 1, timestamp="2026-09-05T12:00:00Z")
    store.add_artifact("job/a1", "salvage", result.ref, hashlib.sha256(result.commit.encode()).hexdigest(), 0)
    return result


def test_c8_4_c13_4_retention_counts_and_removes_clean_allocated_worktree(owned, monkeypatch):
    """Owned bytes bind retention; retirement holds a lease and Git stays outside tx."""
    store, root, repository, worktree = owned
    commands = []
    original = subprocess.run
    original_rename = Path.rename
    retired = []

    def inspect(argv, **kwargs):
        assert not store.connection.in_transaction
        commands.append(argv)
        return original(argv, **kwargs)

    def rename(source, destination):
        assert not store.connection.in_transaction
        if source == worktree:
            key = f"worktree:{worktree.resolve()}"
            assert store.one("SELECT holder FROM leases WHERE lease_key=?", (key,))["holder"] == "retention:job"
            assert not store.acquire_lease(key, "new-writer")
            retired.append(destination)
        return original_rename(source, destination)

    monkeypatch.setattr(retention.subprocess, "run", inspect)
    monkeypatch.setattr(Path, "rename", rename)
    result = retention.maintenance(store, root, max_jobs=1, max_bytes=100)
    assert result["pruned"] == ["job"]
    assert result["bytes_before"] >= 4096
    assert result["bytes_after"] == 0
    assert result["errors"] == []
    assert store.list_leases() == []
    assert not worktree.exists()
    assert str(worktree) not in git(repository, "worktree", "list", "--porcelain")
    assert retired == [root / "trash" / "job" / "worktree"]
    assert not any("prune" in command and "worktree" in command for command in commands)
    assert not any("remove" in command and "worktree" in command for command in commands)


def test_c13_4_retention_never_removes_in_place_workdir(owned, monkeypatch):
    """C-13.4, C-8.4: pruning an in-place job leaves the caller's files and Git index intact."""
    store, root, repository, _ = owned
    store.update_job("job", in_place=1, worktree=str(repository))
    before = (repository / ".git" / "index").read_bytes()
    monkeypatch.setattr(retention.subprocess, "run", lambda *args, **kwargs: pytest.fail("in-place workdir must not be removed or inspected"))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["job"]
    assert (repository / "tracked").read_text() == "base" * 1024
    assert (repository / ".git" / "index").read_bytes() == before


@pytest.mark.parametrize("path_kind", ["outside", "symlink", "container-symlink"])
def test_c13_4_retention_rejects_worktree_paths_outside_owned_root(owned, monkeypatch, path_kind):
    """C-13.4, C-2.1: resolved paths and symlinked containers cannot authorize external deletion."""
    store, root, repository, worktree = owned
    git(repository, "worktree", "remove", str(worktree))
    if path_kind == "outside":
        store.update_job("job", worktree=str(repository))
    elif path_kind == "symlink":
        worktree.symlink_to(repository, target_is_directory=True)
    else:
        (root / "worktrees").rmdir()
        (root / "worktrees").symlink_to(repository.parent, target_is_directory=True)
        store.update_job("job", worktree=str(root / "worktrees" / "repository"))
    monkeypatch.setattr(retention.subprocess, "run", lambda *args, **kwargs: pytest.fail("external paths cannot reach Git removal"))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == []
    assert result["protected"] == ["job"]
    assert result["errors"]
    assert store.get_job("job") is not None
    assert (repository / "tracked").exists()


def test_c13_4_dirty_allocated_worktree_without_salvage_is_preserved(owned):
    """C-13.4, C-8.4: dirty owned work is retained when no salvage artifact records its snapshot."""
    store, root, _, worktree = owned
    (worktree / "tracked").write_text("unsalvaged changes")
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == []
    assert result["protected"] == ["job"]
    assert "no recorded salvage" in result["errors"][0]["error"]
    assert (worktree / "tracked").read_text() == "unsalvaged changes"
    assert store.get_job("job") is not None
    assert store.list_leases() == []


def test_c13_4_retention_resumes_after_worktree_removed_before_row_commit(owned):
    """C-13.4, C-8.4, C-3.2: a retained deletion lease is recoverable after Git removal."""
    store, root, repository, worktree = owned
    assert store.acquire_lease(f"worktree:{worktree.resolve()}", "retention:job")
    git(repository, "worktree", "remove", str(worktree))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["job"]
    assert store.get_job("job") is None
    assert store.list_leases() == []


def test_c13_4_dirty_allocated_worktree_removed_only_after_salvage_is_retained(owned, monkeypatch):
    """C-13.4, C-8.4: retirement requires the current dirty tree in a retained salvage ref."""
    store, root, repository, worktree = owned
    (worktree / "tracked").write_text("salvaged changes")
    snapshot = record_salvage(store, worktree)
    assert retention.maintenance(store, root, max_jobs=0)["protected"] == ["job"]
    git(repository, "update-ref", "refs/heads/retained-checkpoint", snapshot.commit)
    commands = []
    original = subprocess.run

    def inspect(argv, **kwargs):
        assert not store.connection.in_transaction
        commands.append(argv)
        return original(argv, **kwargs)

    monkeypatch.setattr(retention.subprocess, "run", inspect)
    result = retention.maintenance(store, root, max_jobs=0, salvage_referenced_elsewhere=lambda artifact: True)
    assert result["pruned"] == ["job"]
    assert not worktree.exists()
    assert git(repository, "show", snapshot.ref + ":tracked") == "salvaged changes"
    assert not any("prune" in command and "worktree" in command for command in commands)
    assert not any("remove" in command and "worktree" in command for command in commands)


@pytest.mark.parametrize("damage", ["new-edit", "missing-ref"])
def test_c13_4_dirty_worktree_needs_matching_existing_salvage(owned, damage):
    """C-13.4: an old or missing salvage ref cannot authorize discarding later dirty work."""
    store, root, repository, worktree = owned
    (worktree / "tracked").write_text("snapshot content")
    snapshot = record_salvage(store, worktree)
    if damage == "new-edit":
        (worktree / "tracked").write_text("later operator edits")
    else:
        git(repository, "update-ref", "-d", snapshot.ref)
    result = retention.maintenance(store, root, max_jobs=0, salvage_referenced_elsewhere=lambda artifact: True)
    assert result["pruned"] == []
    assert result["protected"] == ["job"]
    assert "not preserved" in result["errors"][0]["error"]
    assert worktree.exists()
    assert store.get_job("job") is not None
