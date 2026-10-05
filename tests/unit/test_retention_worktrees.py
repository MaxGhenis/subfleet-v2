"""Allocated worktree retention uses isolated repositories and no process inspection.

Since d635 (retention by archive) an allocated worktree is removed only after
retention has archived it and read the archive back; its registration goes with
it, and nothing the job did is lost, clean or dirty, salvaged or not.
"""

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_git as rgit
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
                      sandbox="workspace-write", state="succeeded", workdir_head=git(repository, "rev-parse", "HEAD"))
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


def test_c8_4_c13_4_retention_counts_archives_and_removes_clean_allocated_worktree(owned, monkeypatch):
    """C-8.4, C-13.4, C-3.3: owned worktree bytes bind retention; every git call runs
    outside a transaction, with retention holding the worktree lease; the tree
    and its registration go only after the archive is read back."""
    store, root, repository, worktree = owned
    commands = []
    original = rgit.subprocess.Popen

    def inspect(argv, *args, **kwargs):
        assert not store.connection.in_transaction
        commands.append(argv)
        key = f"worktree:{worktree.resolve()}"
        assert store.one("SELECT holder FROM leases WHERE lease_key=?", (key,))["holder"] == "retention:job"
        assert not store.acquire_lease(key, "new-writer")
        return original(argv, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(rgit.subprocess, "Popen", inspect)
        result = retention.maintenance(store, root, max_jobs=1, max_bytes=100)
    assert result["pruned"] == ["job"]
    assert result["bytes_before"] >= 4096
    assert result["bytes_after"] == 0
    assert result["errors"] == []
    assert store.list_leases() == []
    assert not worktree.exists()
    assert str(worktree) not in git(repository, "worktree", "list", "--porcelain")
    assert not any("prune" in argv for argv in commands)
    assert rarch.check_archive(root, "job")["ok"]


def test_c13_4_retention_never_removes_in_place_workdir(owned, monkeypatch):
    """C-13.4, C-8.4: pruning an in-place job leaves the caller's files and Git index intact."""
    store, root, repository, _ = owned
    store.update_job("job", in_place=1, worktree=str(repository))
    before = (repository / ".git" / "index").read_bytes()
    monkeypatch.setattr(rgit.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("in-place workdir must not be inspected"))
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
    monkeypatch.setattr(rgit.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("external paths cannot reach git"))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == []
    assert result["protected"] == ["job"]
    assert result["errors"]
    assert store.get_job("job") is not None
    assert (repository / "tracked").exists()


def test_c13_4_dirty_allocated_worktree_without_salvage_is_archived_before_removal(owned):
    """C-13.4, C-8.4 (d635): dirty owned work without a salvage snapshot is no
    longer kept for ever: it is archived byte for byte, then removed, and
    restores."""
    store, root, _, worktree = owned
    (worktree / "tracked").write_text("unsalvaged changes")
    (worktree / "new-output.csv").write_text("a,b\n")
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["job"]
    assert not worktree.exists() and store.get_job("job") is None
    rarch.restore(root, "job")
    assert (worktree / "tracked").read_text() == "unsalvaged changes"
    assert (worktree / "new-output.csv").read_text() == "a,b\n"


def test_c13_4_retention_resumes_after_worktree_removed_before_row_commit(owned):
    """C-13.4, C-8.4, C-3.2: a deletion lease of the old retention (no journal)
    is released, and the job is retired from what remains."""
    store, root, repository, worktree = owned
    assert store.acquire_lease(f"worktree:{worktree.resolve()}", "retention:job")
    git(repository, "worktree", "remove", str(worktree))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["job"]
    assert store.get_job("job") is None
    assert store.list_leases() == []


def test_c13_4_dirty_allocated_worktree_with_salvage_keeps_the_salvage_ref(owned):
    """C-13.4, C-8.4: the salvage ref is referenced elsewhere once the archive's
    verified anchor reaches its commit (the anchor is the bundle's only head;
    review of a9a6cbf4, B1); retention removes the tree and never touches the
    ref."""
    store, root, repository, worktree = owned
    (worktree / "tracked").write_text("salvaged changes")
    snapshot = record_salvage(store, worktree)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["job"]
    assert not worktree.exists()
    assert git(repository, "show", snapshot.ref + ":tracked") == "salvaged changes"
    manifest = json.loads((root / "archive" / "job" / "manifest.json").read_text())
    anchor = manifest["git"]
    assert anchor["bundle_heads"] == {anchor["anchor_ref"]: anchor["anchor"]}
    assert snapshot.commit in anchor["salvage_in_anchor"]
    assert [(s["ref"], s["commit"]) for s in manifest["salvage"]] == [(snapshot.ref, snapshot.commit)]


@pytest.mark.parametrize("damage", ["new-edit", "missing-ref"])
def test_c13_4_a_later_edit_is_archived_and_a_missing_salvage_ref_keeps_the_job(owned, damage):
    """C-13.4: an edit after the salvage snapshot is in the archive (nothing is
    judged by an old snapshot); a salvage ref that no longer exists cannot be
    archived, so, as C-8.4 says, it keeps its job."""
    store, root, repository, worktree = owned
    (worktree / "tracked").write_text("snapshot content")
    snapshot = record_salvage(store, worktree)
    if damage == "new-edit":
        (worktree / "tracked").write_text("later operator edits")
        result = retention.maintenance(store, root, max_jobs=0)
        assert result["pruned"] == ["job"]
        rarch.restore(root, "job")
        assert (worktree / "tracked").read_text() == "later operator edits"
        return
    git(repository, "update-ref", "-d", snapshot.ref)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == []
    assert result["protected"] == ["job"]
    assert "salvage not archivable" in result["deferred"]["job"]
    assert worktree.exists()
    assert store.get_job("job") is not None


@pytest.mark.parametrize("writable", [True, False])
@pytest.mark.parametrize("seen_by", ["pins", "fence"])
def test_c8_4_i5_a_worktree_a_live_turn_works_in_is_never_reclaimed(owned, monkeypatch, writable, seen_by):
    """I5 (C-8.4, C-13.4, C-24.5): a conversation turn in a detached job's allocated worktree
    (a person continued the job's session in the app) holds a row of its own on the folder,
    writable or read-only, not `worktree:<folder>`. Retention keeps the job and its worktree
    while the row exists, whether only its pins see the row (`_pins`) or only the selection
    fence does (a recorded path spelled otherwise), runs no git there, and reclaims both once
    it is gone. Each layer is tested alone, the other one blinded: review of 5e9f2fbd (P3-3)
    found the pins' check survived being emptied, since the fence alone kept this green."""
    from subfleet import folders
    store, root, repository, worktree = owned
    key = folders.turn_key(str(worktree.resolve()), "20260929-120000-turn", writable=writable)
    assert store.acquire_lease(key, "20260929-120000-turn")
    if seen_by == "fence":
        monkeypatch.setattr(retention.folders, "turn_folders", lambda read: set())
    else:
        monkeypatch.setattr(retention.folders, "turn_holds", lambda read, folder, kinds=folders.SHARED: [])
    original = subprocess.run
    monkeypatch.setattr(retention.subprocess, "run",
                        lambda *args, **kwargs: pytest.fail("no git while a turn works in the worktree"))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == [] and "job" in result["protected"] and result["errors"] == []
    assert worktree.exists() and store.get_job("job") is not None
    assert [row["lease_key"] for row in store.list_leases()] == [key]      # the fence was not left behind
    store.release_leases("20260929-120000-turn")
    monkeypatch.setattr(retention.subprocess, "run", original)
    assert retention.maintenance(store, root, max_jobs=0)["pruned"] == ["job"]
    assert not worktree.exists()


def test_c8_4_i5_a_turn_row_on_a_longer_folder_name_protects_nothing_here(owned):
    """`folders.turn_holds` names a folder exactly: a turn in `<worktree>:x` (a real path
    may hold a colon) is not a turn in `<worktree>`."""
    from subfleet import folders
    store, root, _, worktree = owned
    assert store.acquire_lease(folders.turn_key(f"{worktree.resolve()}:x", "20260929-120000-turn", writable=True),
                               "20260929-120000-turn")
    assert folders.turn_holds(store.query, str(worktree.resolve())) == []
    assert retention.maintenance(store, root, max_jobs=0)["pruned"] == ["job"]
