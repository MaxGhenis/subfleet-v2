"""The code review of design revision 3 (in-session Opus, 2026-09-29): one test per finding,
and tests that kill the safety-relevant mutants it found surviving (docs/desktop/retention-archive.md, section 8)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from subfleet import retention
from subfleet import retention_archive as archive
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.store import Store
from tests.unit.test_retention_properties import add, lane, tree
from tests.unit.test_retention_worktrees import add_job, archive_like_the_sweep, git

_RUN = subprocess.run


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-q", "-b", "main")
    (repository / "tracked").write_text("base")
    git(repository, "add", "tracked")
    git(repository, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "base")
    root = tmp_path / "state"
    (root / "worktrees").mkdir(parents=True)
    with Store(root / "state.sqlite3") as store:
        lane(store)
        yield store, root, repository
    for directory, _, files in os.walk(tmp_path):
        for name in files:
            try:
                os.chflags(os.path.join(directory, name), 0)
            except OSError:
                pass


def half_retired(store, root, identity, files):
    """A pass that committed (rows gone) and archived, then stopped before deleting."""
    directory = add(store, root, identity, order=0, files=files)
    snapshot = archive.rows_snapshot(store.query, identity)
    archive.archive(directory, root / "archive", identity, snapshot, free_floor=0)
    store.acquire_lease(f"retire:{identity}", f"retention:{identity}")
    with store.transaction() as conn:
        conn.execute("DELETE FROM jobs WHERE job_id=?", (identity,))
    return directory


# --- finding 1: one stuck job never stops retention ---------------------------------------------

def test_f1_an_undeletable_file_is_kept_and_the_pass_goes_on(world):
    store, root, _ = world
    directory = half_retired(store, root, "a-stuck", {"stdout": b"x", "locked": b"immutable"})
    os.chflags(directory / "locked", stat.UF_IMMUTABLE)
    add(store, root, "b-next", order=1)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["b-next"]
    assert (root / "retention-conflicts" / "a-stuck" / "locked").read_bytes() == b"immutable"
    assert store.list_leases() == []


def test_f1_a_stray_file_in_an_archive_does_not_wedge_recovery(world):
    store, root, _ = world
    directory = add(store, root, "job", order=0)
    final = archive.archive(directory, root / "archive", "job", archive.rows_snapshot(store.query, "job"),
                            free_floor=0)
    (final / ".DS_Store").write_bytes(b"finder")
    store.acquire_lease("retire:job", "retention:job")
    add(store, root, "other", order=1)
    result = retention.maintenance(store, root, max_jobs=1)
    assert store.list_leases() == [] and store.get_job("job") is not None
    assert (directory / "stdout").exists() and (final / ".DS_Store").exists()
    assert "an archive already exists" in result["deferred"]["job"]


def test_f1_a_malformed_manifest_is_isolated(world):
    store, root, _ = world
    directory = half_retired(store, root, "job", {"stdout": b"x"})
    manifest = root / "archive" / "job" / "manifest.json"
    os.chmod(manifest, 0o600)
    manifest.write_text(json.dumps({"version": 1}))
    add(store, root, "next", order=1)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["next"] and store.list_leases() == []
    assert tree(root / "retention-conflicts" / "job") == {"stdout": b"x"}
    assert not directory.exists()


# --- finding 2: rows present over a directory that is no longer whole ----------------------------

def test_f2_rows_back_over_a_half_deleted_directory_are_made_whole_from_the_archive(world):
    store, root, _ = world
    files = {"stdout": b"out", "a1/deliverable.md": b"# the only copy"}
    directory = add(store, root, "job", order=0, files=files)
    archive.archive(directory, root / "archive", "job", archive.rows_snapshot(store.query, "job"), free_floor=0)
    store.acquire_lease("retire:job", "retention:job")
    (directory / "a1" / "deliverable.md").unlink()       # deleted, then the rows came back
    retention.maintenance(store, root, max_jobs=10)
    assert tree(directory) == files
    assert store.get_job("job") is not None and store.list_leases() == []
    assert not (root / "archive" / "job").exists()
    assert (root / "retention-conflicts" / "job" / "stdout").read_bytes() == b"out"


def test_f2_the_store_is_flushed_to_disk_before_the_first_unlink(world, monkeypatch):
    store, root, _ = world
    add(store, root, "job", order=0)
    order = []
    original_sync, original_delete = retention._sync_store, archive.delete_archived
    monkeypatch.setattr(retention, "_sync_store", lambda s: order.append("sync") or original_sync(s))
    monkeypatch.setattr(archive, "delete_archived",
                        lambda *a, **k: order.append("delete") or original_delete(*a, **k))
    assert retention.maintenance(store, root, max_jobs=0)["pruned"] == ["job"]
    assert order == ["sync", "delete"]


# --- finding 3: the gone test ---------------------------------------------------------------------

def test_f3_relative_gitdir_paths_are_resolved_from_the_registration(world, tmp_path):
    store, root, repository = world
    worktree = add_job(store, root, repository, "job")
    admin = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-dir"))
    (tmp_path / "keep").mkdir()
    git(repository, "worktree", "move", str(worktree), str(tmp_path / "keep" / "moved"))
    (admin / "gitdir").write_text(os.path.relpath(tmp_path / "keep" / "moved" / ".git", admin) + "\n")
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == [] and "moved" in result["kept"]["job"]


def test_f3_a_removed_workdir_still_leads_to_its_repository(world, tmp_path):
    store, root, repository = world
    (repository / "pkg").mkdir()
    worktree = add_job(store, root, repository, "job")
    store.update_job("job", workdir=str(repository / "pkg"))
    shutil.rmtree(repository / "pkg")
    (tmp_path / "keep").mkdir()
    git(repository, "worktree", "move", str(worktree), str(tmp_path / "keep" / "moved"))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == [] and "moved" in result["kept"]["job"]


def test_f8_an_allocated_tree_its_row_never_recorded_keeps_the_job(world):
    store, root, repository = world
    tree_path = root / "worktrees" / "job"
    git(repository, "worktree", "add", "-q", "--detach", str(tree_path), "HEAD")
    store.add_job(job_id="job", request_id="job", payload_digest="d", kind="dispatch", workdir=str(repository),
                  prompt_path="/p", sandbox="workspace-write", state="cancelled", finished_at="2026-01-02T00:00:00Z")
    (root / "jobs" / "job").mkdir(parents=True)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == [] and "job" in result["kept"]
    shutil.rmtree(tree_path)
    assert retention.maintenance(store, root, max_jobs=0)["pruned"] == ["job"]


# --- findings 4 and 5: deletion -----------------------------------------------------------------------

def test_f4_a_write_to_a_surviving_hard_link_during_the_run_is_kept(tmp_path, monkeypatch):
    job_dir = tmp_path / "jobs" / "job"
    job_dir.mkdir(parents=True)
    (job_dir / "link1").write_bytes(b"AAAA")
    os.link(job_dir / "link1", job_dir / "link2")
    final = archive.archive(job_dir, tmp_path / "archive", "job",
                            {"job_id": "job", "rows": {}, "sha256": hashlib.sha256(archive.canonical({})).hexdigest()},
                            free_floor=0)
    manifest = archive.load(final)
    original = os.unlink

    first = []

    def unlink_then_write(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if not first and path in ("link1", "link2"):
            first.append(path)
            target = job_dir / ("link2" if path == "link1" else "link1")
            before = target.stat()
            target.write_bytes(b"BBBB")                   # same size, new bytes, into the surviving link
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        return result
    monkeypatch.setattr(archive.os, "unlink", unlink_then_write)
    kept = archive.delete_archived(job_dir, manifest)
    survivor = "link2" if first == ["link1"] else "link1"
    assert kept == [survivor] and (job_dir / survivor).read_bytes() == b"BBBB"


def test_f5_a_symlinked_path_component_never_redirects_deletion(tmp_path):
    job_dir = tmp_path / "jobs" / "job"
    (job_dir / "a" / "b").mkdir(parents=True)
    (job_dir / "a" / "b" / "result.csv").write_bytes(b"1,2")
    final = archive.archive(job_dir, tmp_path / "archive", "job",
                            {"job_id": "job", "rows": {}, "sha256": hashlib.sha256(archive.canonical({})).hexdigest()},
                            free_floor=0)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.rename(job_dir / "a" / "b", elsewhere / "b")       # the same inodes, outside the tree
    os.rmdir(job_dir / "a")
    (job_dir / "a").symlink_to(elsewhere)
    archive.delete_archived(job_dir, archive.load(final))
    assert (elsewhere / "b" / "result.csv").read_bytes() == b"1,2"


def test_ctime_alone_marks_a_file_as_changed(tmp_path):
    """J3 (surviving mutant M22): same size, mtime put back, new bytes: kept."""
    job_dir = tmp_path / "jobs" / "job"
    job_dir.mkdir(parents=True)
    (job_dir / "f").write_bytes(b"AAAA")
    final = archive.archive(job_dir, tmp_path / "archive", "job",
                            {"job_id": "job", "rows": {}, "sha256": hashlib.sha256(archive.canonical({})).hexdigest()},
                            free_floor=0)
    before = (job_dir / "f").stat()
    time.sleep(0.01)
    (job_dir / "f").write_bytes(b"BBBB")
    os.utime(job_dir / "f", ns=(before.st_atime_ns, before.st_mtime_ns))
    assert archive.delete_archived(job_dir, archive.load(final)) == ["f"]


# --- finding 7: an errored terminal job does not make live walks "unfinished" ----------------------

def test_f7_live_sizing_never_leaves_a_pass_unfinished(tmp_path, monkeypatch):
    with Store(tmp_path / "state.sqlite3") as store:
        lane(store)
        add(store, tmp_path, "bad", order=0)
        os.chmod(tmp_path / "jobs" / "bad", 0)
        for order in range(1, 4):
            add(store, tmp_path, f"live{order}", order=order, state="running",
                files={f"d{i}/f": b"x" for i in range(10)})
        clock = [0.0]

        def tick():
            clock[0] += 1.0
            return clock[0]
        monkeypatch.setattr(retention.time, "monotonic", tick)
        try:
            result = retention.maintenance(store, tmp_path, deadline=clock[0] + 20)
        finally:
            os.chmod(tmp_path / "jobs" / "bad", 0o755)
        assert "interrupted" not in result and result["errors"][0]["job_id"] == "bad"


# --- surviving mutants: pins and the lease at commit, the fence ---------------------------------

def test_gate_evidence_that_arrives_mid_pass_keeps_the_job(world, monkeypatch):
    """M9: a pin that does not change the job's rows (gate evidence) is still re-checked at commit."""
    store, root, _ = world
    directory = add(store, root, "job", order=0)
    original = archive.archive

    def archive_then_gate(*args, **kwargs):
        final = original(*args, **kwargs)
        store.add_action(action_id="gate", kind="gate-merge", op_key="k", subject="pr",
                         request_json=json.dumps({"evidence": ["job/a1"]}))
        return final
    monkeypatch.setattr(archive, "archive", archive_then_gate)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == [] and store.get_job("job") is not None and directory.exists()


def test_a_lease_taken_by_someone_else_mid_pass_blocks_the_commit(world, monkeypatch):
    """M10: the commit re-reads its `retire:` lease."""
    store, root, _ = world
    add(store, root, "job", order=0)
    original = archive.archive

    def archive_then_steal(*args, **kwargs):
        final = original(*args, **kwargs)
        with store.transaction() as conn:
            conn.execute("UPDATE leases SET holder='someone-else' WHERE lease_key='retire:job'")
        return final
    monkeypatch.setattr(archive, "archive", archive_then_steal)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == [] and store.get_job("job") is not None


def test_a_pin_seen_at_the_fence_stops_everything_before_archiving(world, monkeypatch):
    """M15: the fence transaction re-checks pins; nothing is archived for a job pinned by then."""
    store, root, _ = world
    add(store, root, "job", order=0)
    calls = []

    def pins():
        calls.append(1)
        return {"job"} if len(calls) > 1 else set()
    archived = []
    monkeypatch.setattr(archive, "archive", lambda *a, **k: archived.append(a) or pytest.fail("archived"))
    result = retention.maintenance(store, root, max_jobs=0, pins=pins)
    assert result["pruned"] == [] and archived == [] and store.list_leases() == []


# --- surviving mutants: verification ----------------------------------------------------------------

@pytest.mark.parametrize("damage", ["content", "mode", "extra-member", "missing-member"])
def test_verification_compares_every_member_with_the_manifest(tmp_path, damage):
    """M13, M17, M31: a manifest that no longer describes the tar never verifies."""
    job_dir = tmp_path / "jobs" / "job"
    job_dir.mkdir(parents=True)
    (job_dir / "a").write_bytes(b"a" * 10)
    (job_dir / "b").write_bytes(b"b" * 10)
    final = archive.archive(job_dir, tmp_path / "archive", "job",
                            {"job_id": "job", "rows": {}, "sha256": hashlib.sha256(archive.canonical({})).hexdigest()},
                            free_floor=0)
    manifest = json.loads((final / "manifest.json").read_bytes())
    entries = manifest["tree"]["entries"]
    if damage == "content":
        entries[1]["sha256"] = "0" * 64
    elif damage == "mode":
        entries[1]["mode"] ^= 0o100
    elif damage == "extra-member":
        del entries[2]
    else:
        entries.append({**entries[2], "path": "c"})
    os.chmod(final / "manifest.json", 0o600)
    (final / "manifest.json").write_bytes(archive.canonical(manifest))
    with pytest.raises(archive.ArchiveCorrupt):
        archive.verify(final)


# --- surviving mutants: failing closed when a repository cannot be read -----------------------------

def test_a_git_failure_other_than_no_repository_keeps_the_job(world, monkeypatch):
    """N11: only "not a git repository" means nothing is registered."""
    store, root, repository = world
    worktree = add_job(store, root, repository, "job")
    shutil.rmtree(worktree)

    def failing(argv, *args, **kwargs):
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 128, "", "fatal: unable to read config file")
        return _RUN(argv, *args, **kwargs)
    monkeypatch.setattr(retention.subprocess, "run", failing)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == [] and "could not be read" in result["kept"]["job"]


def test_an_unreadable_registration_directory_keeps_the_job(world):
    """N12: registrations that cannot be listed may hold the tree."""
    store, root, repository = world
    worktree = add_job(store, root, repository, "job")
    shutil.rmtree(worktree)
    admins = repository / ".git" / "worktrees"
    os.chmod(admins, 0)
    try:
        result = retention.maintenance(store, root, max_jobs=0)
    finally:
        os.chmod(admins, 0o755)
    assert result["pruned"] == [] and "could not be read" in result["kept"]["job"]


# --- weak test (D1): the dry run with worktree jobs ------------------------------------------------

def test_d1_the_dry_run_agrees_with_a_real_pass_when_worktrees_are_involved(world):
    store, root, repository = world
    add_job(store, root, repository, "present", order=0)
    gone = add_job(store, root, repository, "gone", order=1)
    quarantined = add_job(store, root, repository, "quarantined", order=2)
    add(store, root, "plain", order=3)
    git(repository, "worktree", "remove", "--force", str(gone))
    archive_like_the_sweep(repository, quarantined)
    with Store(root / "state.sqlite3", read_only=True) as reader:
        preview = retention.maintenance(reader, root, max_jobs=0, dry_run=True)
    result = retention.maintenance(store, root, max_jobs=0)
    assert [row["job_id"] for row in preview["would_retire"]] == result["pruned"] == ["gone", "plain"]
    assert set(result["kept"]) == {"present", "quarantined"}


def test_f1_a_deletion_that_fails_in_a_normal_pass_is_kept_and_the_pass_goes_on(world, monkeypatch):
    """`_finish` isolates a failing deletion in the pass itself, not only in recovery."""
    store, root, _ = world
    add(store, root, "a", order=0)
    add(store, root, "b", order=1)
    original = archive.delete_archived

    def refuse_a(job_dir, manifest, **kwargs):
        if job_dir.name == "a":
            raise PermissionError(1, "Operation not permitted")
        return original(job_dir, manifest, **kwargs)
    monkeypatch.setattr(archive, "delete_archived", refuse_a)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["a", "b"] and store.list_leases() == []
    assert (root / "retention-conflicts" / "a" / "stdout").exists() and not (root / "jobs" / "b").exists()


def test_f1_an_unexpected_error_in_recovery_is_isolated(world):
    """A manifest that verifies but is malformed raises something other than OSError or
    ArchiveCorrupt; recovery still reports it, keeps the directory and releases the lease."""
    store, root, _ = world
    directory = half_retired(store, root, "job", {"stdout": b"x"})
    path = root / "archive" / "job" / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["tree"] = "not a tree"
    os.chmod(path, 0o600)
    path.write_bytes(archive.canonical(manifest))
    add(store, root, "next", order=1)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == ["next"] and store.list_leases() == []
    assert any("recovery" in error["error"] for error in result["errors"])
    assert tree(root / "retention-conflicts" / "job") == {"stdout": b"x"} and not directory.exists()


def test_w2_a_tree_set_aside_by_a_plain_rename_under_a_suffixed_name_is_present(world):
    """A copy renamed beside the original as `<anything>.<name>`, without telling Git, is
    still the tree: the suffix rule, not the registration or the quarantine name, finds it."""
    store, root, repository = world
    worktree = add_job(store, root, repository, "job")
    os.rename(worktree, root / "worktrees" / f".set-aside.{worktree.name}")
    result = retention.maintenance(store, root, max_jobs=0)
    assert result["pruned"] == [] and ".set-aside.job" in result["kept"]["job"]
