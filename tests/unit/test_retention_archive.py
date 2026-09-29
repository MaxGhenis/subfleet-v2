"""Retention by archive (C-8.4, C-13.4, design d635): every "must be fixed" item of
Max's d635 ruling has a regression here, each against real git repositories.

- round trip: a dirty, unpushed, detached job with ignored output and a reset-away
  commit is archived, verified, removed and restored byte for byte, with every commit;
- nothing depends on a repository retention does not control (scratch sources,
  local-only blobs, a deleted source clone);
- omission reads and hashes the whole object (a corrupted loose object);
- anchoring is idempotent (a rollback, then the next pass);
- every commit only the admin directory or its reflogs hold is anchored;
- nothing written after the final check is deleted (it goes to conflicts);
- a slow or busy job defers without stopping the queue;
- removal is staged and resumable;
- no repository-wide `git worktree prune`;
- every pin keeps its job.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import zlib
from pathlib import Path

import pytest

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_fs as rfs
from subfleet import retention_git as rgit
from subfleet.retention_holders import ScanFailed
from tests.unit.retention_world import (
    Clock, World, all_commits, git, inode_groups, snapshot, trust_temporary_directories,
)


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    yield w
    w.close()


def run(w: World, **kwargs):
    kwargs.setdefault("max_jobs", 0)
    kwargs.setdefault("max_bytes", 0)
    kwargs.setdefault("holders", lambda watches, **_: {})
    return retention.maintenance(w.store, w.root, **kwargs)


def make_dirty_detached(w: World, job_id: str) -> dict:
    """A job that committed on a detached HEAD, reset one commit away, left
    staged, modified, untracked and ignored files, and a refs/worktree ref."""
    wt = w.job(job_id)
    git(wt, "config", "--worktree", "user.name", "agent") if False else None
    (wt / "feature.py").write_text("def feature():\n    return 1\n")
    git(wt, "add", "feature.py")
    git(wt, "commit", "--quiet", "-m", "unpushed 1")
    kept = git(wt, "rev-parse", "HEAD")
    (wt / "gone.py").write_text("reset away\n")
    git(wt, "add", "gone.py")
    git(wt, "commit", "--quiet", "-m", "unpushed 2, then reset away")
    reset_away = git(wt, "rev-parse", "HEAD")
    git(wt, "reset", "--quiet", "--hard", kept)
    (wt / "src" / "main.py").write_text("print('changed')\n")           # tracked, modified
    (wt / "staged.txt").write_text("staged version\n")
    git(wt, "add", "staged.txt")
    staged_blob = git(wt, "rev-parse", ":staged.txt")
    (wt / "staged.txt").write_text("edited after staging\n")          # index differs from tree
    (wt / "untracked.txt").write_text("untracked notes\n")
    (wt / "out").mkdir()
    (wt / "out" / "result.parquet").write_bytes(os.urandom(4096))     # ignored output
    (wt / "run.log").write_text("ignored log\n")
    os.symlink("src/main.py", wt / "link-to-main")
    os.symlink("/etc/hosts", wt / "absolute-link")
    (wt / "private").mkdir(mode=0o700)
    (wt / "private" / "secret").write_text("0600 file\n")
    os.chmod(wt / "private" / "secret", 0o600)
    (wt / "script.sh").write_text("#!/bin/sh\necho hi\n")
    os.chmod(wt / "script.sh", 0o755)
    os.link(wt / "untracked.txt", wt / "untracked-hardlink.txt")
    os.mkfifo(wt / "pipe")
    worktree_ref_commit = git(wt, "commit-tree", git(wt, "write-tree"), "-p", kept, "-m", "only refs/worktree")
    git(wt, "update-ref", "refs/worktree/private", worktree_ref_commit)
    return {"worktree": wt, "kept": kept, "reset_away": reset_away, "staged_blob": staged_blob,
            "worktree_ref_commit": worktree_ref_commit}


# --- the round trip ------------------------------------------------------------------

def test_round_trip_dirty_unpushed_detached_job_restores_every_byte_and_commit(world):
    """d635 required case: archive, verify, remove, then the documented restore
    brings back every byte (modes, links, hard links, the FIFO, ignored output)
    and every commit, including one reachable only from the reflog."""
    w = world
    job = make_dirty_detached(w, "job-rt")
    wt = job["worktree"]
    before = snapshot(wt)
    links_before = inode_groups(wt)
    jobdir_before = snapshot(w.root / "jobs" / "job-rt")
    admin_before = snapshot(w.admin("job-rt"))
    result = run(w)
    assert result["pruned"] == ["job-rt"], result
    assert not wt.exists() and not (w.root / "jobs" / "job-rt").exists()
    assert w.store.get_job("job-rt") is None
    # The registration is removed with the tree (no locked registrations pile up),
    # and nothing else in the repository was pruned.
    assert not w.admin("job-rt").exists()
    assert "job-rt" not in git(w.repo, "worktree", "list", "--porcelain")
    archive = w.root / "archive" / "job-rt"
    manifest = json.loads((archive / "manifest.json").read_text())
    omitted = [e["p"] for e in manifest["trees"]["worktree"]["entries"] if e.get("blob")]
    assert "README.md" in omitted and "data.bin" in omitted            # unchanged, on origin/main
    assert "src/main.py" not in omitted                                  # modified: archived
    # Git's own collection cannot take what the archive needs: prune and gc hard.
    git(w.repo, "reflog", "expire", "--expire=now", "--all")
    git(w.repo, "gc", "--quiet", "--prune=now")
    report = rarch.restore(w.root, "job-rt")
    assert set(report["restored"]) == {"worktree", "job", "admin"}
    assert snapshot(wt) == before
    assert inode_groups(wt) == links_before
    assert snapshot(w.root / "jobs" / "job-rt") == jobdir_before
    restored_admin = snapshot(w.admin("job-rt"))
    assert {k: v for k, v in restored_admin.items() if k != "locked"} == \
        {k: v for k, v in admin_before.items() if k != "locked"}
    assert not (w.admin("job-rt") / "locked").exists()
    # The worktree is a working worktree again: same HEAD, same staged blob.
    assert git(wt, "rev-parse", "HEAD") == job["kept"]
    assert git(wt, "rev-parse", ":staged.txt") == job["staged_blob"]
    for commit in (job["kept"], job["reset_away"], job["worktree_ref_commit"]):
        assert git(w.repo, "cat-file", "-t", commit) == "commit"
    assert git(wt, "rev-parse", "refs/worktree/private") == job["worktree_ref_commit"]


def test_bundle_alone_restores_the_commits_in_a_fresh_clone(world):
    """Nothing depends on the source repository's local objects: a fresh clone
    of the remote plus the archive's bundle holds every commit and staged blob."""
    w = world
    job = make_dirty_detached(w, "job-fresh")
    assert run(w)["pruned"] == ["job-fresh"]
    archive = w.root / "archive" / "job-fresh"
    fresh = w.base / "fresh"
    git(w.base, "clone", "--quiet", str(w.remote), str(fresh))
    git(fresh, "fetch", "--quiet", str(archive / "commits.bundle"), "+refs/*:refs/restored/*")
    for oid in (job["kept"], job["reset_away"], job["worktree_ref_commit"], job["staged_blob"]):
        assert subprocess.run(["git", "-C", str(fresh), "cat-file", "-e", oid]).returncode == 0, oid


# --- nothing depends on a repository retention does not control --------------------------

def test_scratch_source_clone_deleted_after_retirement_is_still_restorable(tmp_path, monkeypatch):
    """d635 required case: a job cut from a throwaway clone of a project on a
    network remote (Opus design finding 1: the mstat6 and thesis-review cases).
    No file is omitted; the bundle carries every commit the remote does not.
    After the clone is deleted, every byte comes back, and the commits come back
    from a fresh clone of the remote plus the bundle."""
    monkeypatch.setattr(rgit, "temp_roots", lambda: {"/nonexistent-temporary-root"})
    w = World(tmp_path / "scratch-area")                   # a path named scratch: never trusted
    try:
        job = make_dirty_detached(w, "job-scratch")
        before = snapshot(job["worktree"])
        assert run(w)["pruned"] == ["job-scratch"]
        manifest = json.loads((w.root / "archive" / "job-scratch" / "manifest.json").read_text())
        assert manifest["git"]["scratch"] == "scratch path"
        assert not any(e.get("blob") for e in manifest["trees"]["worktree"]["entries"])
        shutil.rmtree(w.repo)
        out = tmp_path / "restored"
        rarch.restore(w.root, "job-scratch", to=out)
        assert snapshot(out / "worktree") == before
        clone = tmp_path / "from-remote"
        git(tmp_path, "clone", "--quiet", str(w.remote), str(clone))
        git(clone, "fetch", "--quiet", str(w.root / "archive" / "job-scratch" / "commits.bundle"),
            "+refs/*:refs/restored/*")
        for oid in (job["kept"], job["reset_away"], job["worktree_ref_commit"], job["staged_blob"]):
            assert subprocess.run(["git", "-C", str(clone), "cat-file", "-e", oid]).returncode == 0, oid
    finally:
        w.close()


def test_clone_of_a_local_repository_carries_its_whole_history(tmp_path, monkeypatch):
    """A clone whose origin is a local path has no network remote: nothing in it
    is held elsewhere, so the bundle has no prerequisites and the job restores
    with both the clone and its origin deleted."""
    monkeypatch.setattr(rgit, "temp_roots", lambda: {"/nonexistent-temporary-root"})
    w = World(tmp_path / "scratch-local")
    try:
        git(w.repo, "remote", "set-url", "origin", str(w.remote))
        job = make_dirty_detached(w, "job-local-origin")
        before = snapshot(job["worktree"])
        assert run(w)["pruned"] == ["job-local-origin"]
        bundle = w.root / "archive" / "job-local-origin" / "commits.bundle"
        shutil.rmtree(w.repo)
        shutil.rmtree(w.remote)
        rarch.restore(w.root, "job-local-origin", to=tmp_path / "out")
        assert snapshot(tmp_path / "out" / "worktree") == before
        empty = tmp_path / "empty"
        git(tmp_path, "init", "--quiet", str(empty))
        git(empty, "fetch", "--quiet", str(bundle), "+refs/*:refs/restored/*")
        for oid in (job["kept"], job["reset_away"], job["worktree_ref_commit"], job["staged_blob"]):
            assert subprocess.run(["git", "-C", str(empty), "cat-file", "-e", oid]).returncode == 0, oid
    finally:
        w.close()


def test_temporary_directory_source_is_scratch_by_default(tmp_path):
    """Pytest's own directories are temporary: without the test override nothing
    under them may be omitted."""
    w = World(tmp_path)
    try:
        w.job("job-tmp")
        assert run(w)["pruned"] == ["job-tmp"]
        manifest = json.loads((w.root / "archive" / "job-tmp" / "manifest.json").read_text())
        assert manifest["git"]["scratch"] == "temporary directory"
        assert manifest["totals"]["omitted_bytes"] == 0
    finally:
        w.close()


def test_blob_only_in_a_local_branch_is_archived_not_omitted(world):
    """Omit a blob only if a remote-tracking ref reaches it: a commit on a local
    branch that was never pushed does not count, however durable the branch looks."""
    w = world
    (w.repo / "local-only.txt").write_text("committed locally, never pushed\n" * 100)
    git(w.repo, "add", "local-only.txt")
    git(w.repo, "commit", "--quiet", "-m", "local only")
    w.job("job-local")
    assert run(w)["pruned"] == ["job-local"]
    manifest = json.loads((w.root / "archive" / "job-local" / "manifest.json").read_text())
    entries = {e["p"]: e for e in manifest["trees"]["worktree"]["entries"]}
    assert "store" in entries["local-only.txt"] and not entries["local-only.txt"].get("blob")
    assert entries["README.md"].get("blob")            # on origin/main: omitted


def test_local_path_remote_is_not_a_remote(world):
    """A remote whose URL is a local path is another repository retention does
    not control: nothing is omitted on its account."""
    w = world
    git(w.repo, "remote", "set-url", "origin", str(w.remote))
    w.job("job-localremote")
    assert run(w)["pruned"] == ["job-localremote"]
    manifest = json.loads((w.root / "archive" / "job-localremote" / "manifest.json").read_text())
    assert manifest["git"]["scratch"] == "no network remote"
    assert manifest["totals"]["omitted_bytes"] == 0


@pytest.mark.parametrize("marker", ["alternates", "shallow"])
def test_borrowed_or_shallow_object_store_is_scratch(world, marker):
    w = world
    if marker == "alternates":
        (w.repo / ".git" / "objects" / "info" / "alternates").write_text(str(w.remote / "objects") + "\n")
    else:
        (w.repo / ".git" / "shallow").write_text(w.head() + "\n")
    reason = rgit.scratch_reason(w.repo / ".git", w.root, rgit.network_remotes(w.repo / ".git"))
    assert reason in ("borrows objects (alternates)", "shallow clone")


def test_network_url_classification():
    assert rgit.network_url("https://github.com/a/b.git")
    assert rgit.network_url("git@github.com:a/b.git")
    assert rgit.network_url("ssh://git@host/x")
    assert not rgit.network_url("/Users/me/repo")
    assert not rgit.network_url("file:///Users/me/repo")
    assert not rgit.network_url("../sibling")
    assert not rgit.network_url("C:/path")


# --- omission reads and hashes the whole object ------------------------------------------

def _corrupt_loose(repo: Path, oid: str, *, zlib_valid: bool) -> None:
    path = repo / ".git" / "objects" / oid[:2] / oid[2:]
    original = zlib.decompress(path.read_bytes())
    header, _, body = original.partition(b"\0")
    damaged = bytes(b ^ 0x5A for b in body[:64]) + body[64:]
    os.chmod(path, 0o644)
    path.write_bytes(zlib.compress(header + b"\0" + damaged) if zlib_valid else path.read_bytes()[:40])


@pytest.mark.parametrize("zlib_valid", [True, False], ids=["wrong-bytes", "truncated"])
def test_corrupted_loose_object_is_not_trusted_for_omission(world, zlib_valid):
    """d635 required case (Astra finding 1): the working file still hashes to
    HEAD's blob, but the object store's copy is damaged. Omission reads the
    whole object and hashes it; the file is archived instead, and restores."""
    w = world
    (w.repo / "precious.bin").write_bytes(os.urandom(200_000))
    git(w.repo, "add", "precious.bin")
    git(w.repo, "commit", "--quiet", "-m", "precious")
    w.push()
    blob = git(w.repo, "rev-parse", "HEAD:precious.bin")
    wt = w.job("job-corrupt")
    expected = (wt / "precious.bin").read_bytes()
    _corrupt_loose(w.repo, blob, zlib_valid=zlib_valid)
    assert git(w.repo, "cat-file", "-e", blob, check=False) == ""   # git still says it exists
    result = run(w)
    assert result["pruned"] == ["job-corrupt"], result
    manifest = json.loads((w.root / "archive" / "job-corrupt" / "manifest.json").read_text())
    entry = next(e for e in manifest["trees"]["worktree"]["entries"] if e["p"] == "precious.bin")
    assert entry.get("store") and not entry.get("blob")
    rarch.restore(w.root, "job-corrupt", to=w.base / "out")
    assert (w.base / "out" / "worktree" / "precious.bin").read_bytes() == expected


def test_object_reader_hashes_every_byte(world):
    w = world
    blob = git(w.repo, "rev-parse", "HEAD:data.bin")
    with rgit.ObjectReader(w.repo / ".git", "sha1") as reader:
        assert reader.verify(blob)
        assert not reader.verify("0" * 40)
    _corrupt_loose(w.repo, blob, zlib_valid=True)
    with rgit.ObjectReader(w.repo / ".git", "sha1") as reader:
        assert not reader.verify(blob)


# --- anchoring is idempotent; a rollback does not wedge the job ---------------------------------

def test_anchor_is_deterministic_and_create_is_idempotent(world):
    w = world
    wt = w.job("job-anchor")
    admin = w.admin("job-anchor")
    objects = rgit.admin_objects(admin, "sha1")
    first = rgit.make_anchor(w.repo / ".git", "job-anchor", objects, ())
    second = rgit.make_anchor(w.repo / ".git", "job-anchor", objects, ())
    assert first == second
    ref = rgit.anchor_ref("job-anchor", first)
    rgit.create_ref(w.repo / ".git", ref, first)
    rgit.create_ref(w.repo / ".git", ref, first)                       # equal: success
    with pytest.raises(rgit.GitError):
        rgit.create_ref(w.repo / ".git", ref, w.head())                # a different value: refused
    assert git(w.repo, "rev-parse", ref) == first


def test_rollback_after_anchoring_then_next_pass_retires(world):
    """d635 required case (Opus finding 2): the first attempt anchors, bundles and
    is rolled back at the second holder check; the next pass retires the job."""
    w = world
    make_dirty_detached(w, "job-rb")
    calls = []

    def busy_at_second_check(watches, **_):
        calls.append({job: bool(watch.inodes) for job, watch in watches.items()})
        return {"job-rb": ["pid 1 (editor): open for writing"]} if len(calls) == 2 else {}

    state = retention.RetentionState()
    clock = Clock()
    first = run(w, holders=busy_at_second_check, state=state, clock=clock)
    assert first["pruned"] == [] and "busy" in first["deferred"]["job-rb"]
    assert calls == [{"job-rb": False}, {"job-rb": True}]
    assert (w.root / "worktrees" / "job-rb").is_dir()                  # put back
    assert w.store.get_job("job-rb") is not None
    assert not (w.admin("job-rb") / "locked").exists()                 # our lock removed
    anchors = git(w.repo, "for-each-ref", "--format=%(refname)", "refs/subfleet-archive/job-rb/")
    assert anchors                                                     # the harmless extra ref stays
    assert run(w, state=state, clock=clock)["pruned"] == []            # deferred for an hour
    clock.advance(rarch.DEFER_BUSY_S + 1)
    second = run(w, state=state, clock=clock)
    assert second["pruned"] == ["job-rb"], second


def test_pinned_at_commit_rolls_back_then_retires_when_unpinned(world):
    """A pin that appears after selection is honoured inside the commit
    transaction (the conversation service's pins are asked again there)."""
    w = world
    wt = w.job("job-pin-late")
    before = snapshot(wt)
    asked = []

    def pins():
        asked.append(w.store.connection.in_transaction)
        return {"job-pin-late"} if len(asked) == 3 else set()

    state, clock = retention.RetentionState(), Clock()
    first = run(w, pins=pins, state=state, clock=clock)
    assert first["pruned"] == []
    assert asked[-1] is True                                           # asked inside the commit
    assert snapshot(wt) == before and w.store.get_job("job-pin-late")
    clock.advance(rarch.DEFER_PINNED_S + 1)
    assert run(w, pins=lambda: set(), state=state, clock=clock)["pruned"] == ["job-pin-late"]


# --- every commit the admin directory names is anchored ------------------------------------

def test_every_commit_only_the_admin_directory_names_survives_gc(world):
    """Opus design finding 3 and round-4 finding 2: commits held only by the
    retired worktree's reflog, ORIG_HEAD, MERGE_HEAD, FETCH_HEAD, refs/worktree,
    refs/bisect, and blobs only in its index, all survive the registration's
    removal and a hard gc, because the anchor reaches them."""
    w = world
    wt = w.job("job-admin")
    admin = w.admin("job-admin")
    (wt / "x.txt").write_text("reflog only\n")
    git(wt, "add", "x.txt")
    git(wt, "commit", "--quiet", "-m", "reflog only")
    reflog_only = git(wt, "rev-parse", "HEAD")
    git(wt, "reset", "--quiet", "--hard", "HEAD~1")        # git writes ORIG_HEAD; replaced below

    def commit(message: str) -> str:
        return git(wt, "commit-tree", git(wt, "write-tree"), "-p", w.head(), "-m", message)

    orig_head, merge_head, fetch_head, bisect, wref = (commit(m) for m in
                                                       ("orig", "merge", "fetch", "bisect", "worktree-ref"))
    (admin / "ORIG_HEAD").write_text(orig_head + "\n")
    (admin / "MERGE_HEAD").write_text(merge_head + "\n")
    (admin / "FETCH_HEAD").write_text(f"{fetch_head}\t\tbranch 'x' of https://example.invalid/r\n")
    git(wt, "update-ref", "refs/bisect/bad", bisect)
    git(wt, "update-ref", "refs/worktree/keep", wref)
    (wt / "staged-only.txt").write_text("only in the index\n")
    git(wt, "add", "staged-only.txt")
    staged = git(wt, "rev-parse", ":staged-only.txt")
    os.unlink(wt / "staged-only.txt")
    assert run(w)["pruned"] == ["job-admin"]
    git(w.repo, "worktree", "prune")
    git(w.repo, "reflog", "expire", "--expire=now", "--all")
    git(w.repo, "gc", "--quiet", "--prune=now")
    fresh = w.base / "fresh-admin"
    git(w.base, "clone", "--quiet", str(w.remote), str(fresh))
    git(fresh, "fetch", "--quiet", str(w.root / "archive" / "job-admin" / "commits.bundle"), "+refs/*:refs/r/*")
    for oid in (orig_head, merge_head, fetch_head, bisect, wref, reflog_only, staged):
        assert subprocess.run(["git", "-C", str(fresh), "cat-file", "-e", oid]).returncode == 0, oid


# --- nothing written after the final check is deleted --------------------------------------

def test_file_written_after_the_final_check_is_kept_in_conflicts(world, monkeypatch):
    """d635 required case (round-4 finding 1): after the rows are deleted, a late
    writer adds a file and rewrites another inside the quarantined tree, and
    commits in it. Deletion re-validates each signature: both files go to the
    conflicts folder, the late commit is anchored, the registration is kept."""
    w = world
    wt = w.job("job-late")
    original_publish = rarch.Retirement.publish
    late = {}

    def publish_then_write(self):
        original_publish(self)
        tree = self.q_worktree
        (tree / "late-results.csv").write_text("written after the final check\n")
        (tree / "README.md").write_text("rewritten after the final check\n")
        (tree / "c.txt").write_text("late commit\n")
        git(tree, "--git-dir", str(w.admin("job-late")), "--work-tree", str(tree), "add", "c.txt")
        git(tree, "--git-dir", str(w.admin("job-late")), "--work-tree", str(tree), "commit", "--quiet", "-m", "late")
        late["commit"] = git(tree, "--git-dir", str(w.admin("job-late")), "rev-parse", "HEAD")

    monkeypatch.setattr(rarch.Retirement, "publish", publish_then_write)
    result = run(w)
    assert result["pruned"] == ["job-late"]
    conflicts = w.root / "retention-conflicts" / "job-late"
    assert (conflicts / "worktree" / "late-results.csv").read_text() == "written after the final check\n"
    assert (conflicts / "worktree" / "README.md").read_text() == "rewritten after the final check\n"
    assert not (w.root / "retention" / "job-late").exists()
    assert w.admin("job-late").exists()                                 # kept: it changed too
    assert (w.admin("job-late") / "locked").exists()
    events = [e for e in w.store.list_events("job-late") if e["kind"] == "retention.conflict"]
    assert events
    late_anchor = json.loads(events[0]["data_json"]).get("late_anchor")
    assert late_anchor
    git(w.repo, "worktree", "prune")
    git(w.repo, "gc", "--quiet", "--prune=now")
    assert git(w.repo, "cat-file", "-t", late["commit"]) == "commit"


def test_new_file_before_commit_rolls_back_the_job(world, monkeypatch):
    """A write after archiving but before the final check puts the job back."""
    w = world
    wt = w.job("job-active")
    original = rarch.Retirement.final_check

    def write_then_check(self):
        (self.q_worktree / "new.txt").write_text("still running\n")
        return original(self)

    monkeypatch.setattr(rarch.Retirement, "final_check", write_then_check)
    result = run(w)
    assert result["pruned"] == [] and "changed after archive" in result["deferred"]["job-active"]
    assert (wt / "new.txt").read_text() == "still running\n"
    assert w.store.get_job("job-active") is not None


# --- slow and busy jobs defer; the queue moves ---------------------------------------------

def test_a_slow_archive_parks_while_other_jobs_retire_in_the_same_pass(world, monkeypatch):
    """d635 required case (round-4 finding 3): the oldest job is slow to read; it
    is parked with its progress, and the jobs behind it still retire in the same
    pass. Later passes resume it without reading again what they have read."""
    w = world
    for n, job_id in enumerate(["slow", "b", "c"]):
        w.job(job_id, created=f"2026-09-01T00:0{n}:00Z")
    clock = Clock()
    current = {}
    original_run = rarch._Builder.run
    original_read = rfs.read_hashes
    reads = []

    def run_builder(self):
        current["job"] = self.r.job_id
        try:
            return original_run(self)
        finally:
            current.pop("job", None)

    def slow_read(fd, size, fmt, check=None):
        if current.get("job") == "slow":
            reads.append(size)
            clock.advance(40)
        return original_read(fd, size, fmt, check)

    monkeypatch.setattr(rarch._Builder, "run", run_builder)
    monkeypatch.setattr(rfs, "read_hashes", slow_read)
    state = retention.RetentionState()
    first = run(w, clock=clock, state=state, slice_s=60)
    assert set(first["pruned"]) == {"b", "c"}, first
    assert first["in_flight"] == ["slow"] and first["more"] is True
    assert (w.root / "retention" / "slow" / "worktree").is_dir()       # parked in quarantine
    read_first = len(reads)
    for _ in range(12):
        result = run(w, clock=clock, state=state, slice_s=60)
        if result["pruned"]:
            break
    assert result["pruned"] == ["slow"], result
    assert w.store.get_job("slow") is None
    # Each file was read once for its hashes and each archived one once more to
    # verify it: resuming did not start over.
    manifest = json.loads((w.root / "archive" / "slow" / "manifest.json").read_text())
    files = [e for t in manifest["trees"].values() for e in t["entries"] if e["sig"]["t"] == "f"]
    stored = {e["store"] for e in files if e.get("store")}
    assert len(reads) == len(files) + len(stored), (len(reads), len(files), len(stored), read_first)


def test_a_busy_oldest_job_does_not_block_the_queue(world):
    w = world
    for n, job_id in enumerate(["busy", "next"]):
        w.job(job_id, created=f"2026-09-01T00:0{n}:00Z")
    result = run(w, holders=lambda watches, **_: {"busy": ["pid 9 (zsh): cwd"]} if "busy" in watches else {})
    assert result["pruned"] == ["next"]
    assert "busy" in result["deferred"]["busy"]
    assert (w.root / "worktrees" / "busy").is_dir()


def test_a_failed_process_listing_defers_the_batch(world):
    w = world
    wt = w.job("job-scan")

    def fail(watches, **_):
        raise ScanFailed("lsof exited 1")

    result = run(w, holders=fail)
    assert result["pruned"] == [] and "holder scan failed" in result["deferred"]["job-scan"]
    assert wt.is_dir() and w.store.get_job("job-scan")


def test_batch_bounds_a_pass_and_takes_the_oldest_first(world):
    w = world
    for n in range(5):
        w.job(f"j{n}", worktree=False, created=f"2026-09-01T00:0{n}:00Z")
    result = run(w, batch=2)
    assert result["pruned"] == ["j0", "j1"] and result["more"] is True
    assert run(w, batch=2)["pruned"] == ["j2", "j3"]


def test_a_pool_over_its_count_is_pruned_without_sizing_everything(world, monkeypatch):
    """The installed release sized every job first and timed out (d635): a pool
    already over its job count needs no size to act."""
    w = world
    for n in range(4):
        w.job(f"k{n}", worktree=False, created=f"2026-09-01T00:0{n}:00Z")
    sized = []
    original = retention._size
    monkeypatch.setattr(retention, "_size", lambda path, **kw: sized.append(path) or original(path, **kw))
    result = run(w, max_jobs=1, max_bytes=10**12, deadline=__import__("time").monotonic() + 60)
    assert result["pruned"] == ["k0", "k1", "k2"]
    assert sized == []


# --- removal is staged and resumable -------------------------------------------------------

def test_interrupted_removal_resumes_and_leaves_no_half_tree(world, monkeypatch):
    """An interruption half way through deletion (after the rows are gone)
    resumes on the next pass from the journal; directories partly emptied,
    made writable, and hard links whose ctime changed are not mistaken for
    changes (design review finding 8)."""
    w = world
    wt = w.job("job-resume")
    (wt / "ro").mkdir()
    for n in range(20):
        (wt / "ro" / f"f{n}").write_text(str(n))
    os.chmod(wt / "ro", 0o555)
    (wt / "a").write_text("linked")
    os.link(wt / "a", wt / "b")
    cancel = threading.Event()
    original = os.unlink
    original_reclaim = rarch.Retirement.reclaim
    count = [0]

    def unlink_then_cancel(*args, **kwargs):
        count[0] += 1
        if count[0] == 5:
            cancel.set()
        return original(*args, **kwargs)

    def reclaim(self):
        monkeypatch.setattr(rfs.os, "unlink", unlink_then_cancel)
        try:
            return original_reclaim(self)
        finally:
            monkeypatch.setattr(rfs.os, "unlink", original)

    monkeypatch.setattr(rarch.Retirement, "reclaim", reclaim)
    first = run(w, cancel=cancel)
    assert first["interrupted"] == "cancelled"
    assert w.store.get_job("job-resume") is None                      # committed before deletion
    journal = json.loads((w.root / "retention" / "job-resume" / "journal.json").read_text())
    assert journal["state"] == "reclaiming"
    monkeypatch.setattr(rarch.Retirement, "reclaim", original_reclaim)
    second = run(w)
    assert second["reclaimed"] == ["job-resume"], second
    assert not (w.root / "retention" / "job-resume").exists()
    assert not (w.root / "retention-conflicts" / "job-resume").exists()
    rarch.restore(w.root, "job-resume", to=w.base / "back")
    assert sorted(os.listdir(w.base / "back" / "worktree" / "ro")) == sorted(f"f{n}" for n in range(20))


@pytest.mark.parametrize("step", ["begin", "lock", "quarantine", "archive", "final_check", "commit", "publish"])
def test_a_crash_at_any_step_is_recovered_by_the_next_pass(world, monkeypatch, step):
    """I5: before commit the next pass carries on (or puts everything back);
    after commit it finishes the deletion. Nothing is lost either way."""
    w = world
    job = make_dirty_detached(w, "job-crash")
    before = snapshot(job["worktree"])
    original = getattr(rarch.Retirement, step)

    class Crash(BaseException):
        pass

    def crash(self, *args, **kwargs):
        if step in ("begin", "lock", "quarantine", "archive", "final_check"):
            raise Crash()
        result = original(self, *args, **kwargs)
        raise Crash()

    monkeypatch.setattr(rarch.Retirement, step, crash)
    with pytest.raises(Crash):
        run(w)
    monkeypatch.setattr(rarch.Retirement, step, original)
    for _ in range(3):
        result = run(w)
        if w.store.get_job("job-crash") is None and not (w.root / "retention" / "job-crash").exists():
            break
    assert w.store.get_job("job-crash") is None, result
    assert not (w.root / "retention" / "job-crash" / "journal.json").exists()
    rarch.restore(w.root, "job-crash")
    assert snapshot(job["worktree"]) == before


# --- no repository-wide prune ------------------------------------------------------------

def test_retention_never_prunes_other_registrations(world):
    """R2h-4, R2s-2: an unrelated registration whose tree is missing survives."""
    w = world
    other = w.base / "elsewhere" / "other-tree"
    other.parent.mkdir()
    git(w.repo, "worktree", "add", "--quiet", "--detach", str(other), "HEAD")
    shutil.rmtree(other)                                             # missing, not locked: prunable
    w.job("job-noprune")
    assert run(w)["pruned"] == ["job-noprune"]
    assert (w.repo / ".git" / "worktrees" / "other-tree").is_dir()


def test_discarding_a_broken_allocation_removes_only_its_own_registration(world):
    from subfleet.daemon import Daemon
    w = world
    other = w.base / "elsewhere" / "other"
    other.parent.mkdir()
    git(w.repo, "worktree", "add", "--quiet", "--detach", str(other), "HEAD")
    shutil.rmtree(other)
    broken = w.root / "worktrees" / "broken"
    git(w.repo, "worktree", "add", "--quiet", "--detach", str(broken), "HEAD")
    Daemon._discard_worktree(str(w.repo), str(broken), 30)
    assert not broken.exists()
    assert not (w.repo / ".git" / "worktrees" / "broken").exists()
    assert (w.repo / ".git" / "worktrees" / "other").is_dir()


# --- every pin keeps its job ---------------------------------------------------------------

PINS = ["running", "queued", "gate-review", "live-attempt", "quarantined", "unread-notice", "parent",
        "job-lease", "attempt-lease", "worktree-lease", "salvage-unresolvable", "gate-evidence",
        "conversation", "turn-keep-days", "explicit", "resume-fence"]


@pytest.mark.parametrize("pin", PINS)
def test_every_pin_keeps_its_job(world, pin):
    """d635 required case: a pinned job keeps its rows, trees and registration,
    and retention writes nothing for it."""
    w = world
    state = {"running": "running", "queued": "queued"}.get(pin, "succeeded")
    kind = {"gate-review": "gate-review", "turn-keep-days": "turn"}.get(pin, "dispatch")
    wt = w.job("pinned", state=state, kind=kind)
    if kind == "turn":
        w.store.update_job("pinned", finished_at=retention._utc(), request_id="turn:m:0", name="turn-c")
    w.job("free", worktree=False)
    before = snapshot(wt)
    kwargs = {}
    if pin in ("live-attempt", "quarantined", "attempt-lease", "salvage-unresolvable"):
        w.attempt("pinned", {"live-attempt": "running", "quarantined": "quarantined"}.get(pin, "succeeded"))
    if pin == "unread-notice":
        w.store.add_notice("pinned", "done", "session-1")
    if pin == "parent":
        w.store.add_job(job_id="child", request_id="child", payload_digest="d", kind="resume", workdir=str(wt),
                        prompt_path="/p", sandbox="read-only", state="running", parent_job_id="pinned")
    if pin == "job-lease":
        w.store.acquire_lease("out:/somewhere", "pinned")
    if pin == "attempt-lease":
        w.store.acquire_lease("export:x", "pinned/a1")
    if pin == "worktree-lease":
        w.store.acquire_lease(f"worktree:{wt.resolve()}", "someone-else")
    if pin == "salvage-unresolvable":
        w.store.add_artifact("pinned/a1", "salvage", "refs/subfleet-salvage/pinned/1", "d", 0)
    if pin == "gate-evidence":
        w.store.add_action(action_id="g", kind="gate-merge", op_key="k", subject="pinned")
    if pin == "conversation":
        kwargs["pins"] = lambda: {"pinned"}
    if pin == "turn-keep-days":
        kwargs["turn_max_jobs"] = 0
        kwargs["turn_max_bytes"] = 0
        kwargs["turn_keep_s"] = 14 * 86400
    if pin == "explicit":
        kwargs["referenced_job_ids"] = ["pinned"]
    if pin == "resume-fence":
        w.store.acquire_lease("retire:pinned", "resume:request-1")
    result = run(w, **kwargs)
    assert "pinned" not in result["pruned"], result
    assert "pinned" in result["protected"]
    assert w.store.get_job("pinned") is not None
    assert snapshot(wt) == before
    assert not (w.root / "archive" / "pinned").exists()
    assert not (w.root / "retention" / "pinned").exists()
    assert not (w.admin("pinned") / "locked").exists()


def test_salvage_is_bundled_and_its_ref_kept(world):
    """C-8.4: a salvage ref pins its job unless it is referenced elsewhere; the
    archive's verified bundle is that elsewhere. The ref itself is never touched."""
    w = world
    wt = w.job("job-salvage")
    w.attempt("job-salvage")
    (wt / "work.txt").write_text("salvaged\n")
    tree_commit = git(wt, "commit-tree", git(wt, "write-tree"), "-p", w.head(), "-m", "salvage")
    git(w.repo, "update-ref", "refs/subfleet-salvage/job-salvage/1", tree_commit)
    w.store.add_artifact("job-salvage/a1", "salvage", "refs/subfleet-salvage/job-salvage/1", "d", 0)
    result = run(w)
    assert result["pruned"] == ["job-salvage"], result
    heads = rgit.bundle_heads(w.root / "archive" / "job-salvage" / "commits.bundle")
    assert heads["refs/subfleet-salvage/job-salvage/1"] == tree_commit
    assert git(w.repo, "rev-parse", "refs/subfleet-salvage/job-salvage/1") == tree_commit
    rows = json.loads((w.root / "archive" / "job-salvage" / "rows.json").read_text())
    assert rows["rows"]["artifacts"][0]["path"] == "refs/subfleet-salvage/job-salvage/1"


def test_in_place_salvage_still_pins(world):
    w = world
    w.store.add_job(job_id="inplace", request_id="inplace", payload_digest="d", kind="dispatch", workdir=str(w.repo),
                    worktree=str(w.repo), in_place=1, prompt_path="/p", sandbox="workspace-write", state="succeeded")
    (w.root / "jobs" / "inplace").mkdir()
    w.attempt("inplace")
    w.store.add_artifact("inplace/a1", "salvage", "refs/subfleet-salvage/inplace/1", "d", 0)
    result = run(w)
    assert result["pruned"] == [] and result["pin_reasons"]["inplace"] == "salvage"


def test_resume_fence_and_retention_exclude_each_other(world):
    """Astra design finding 9: a resume reading its source and retention
    retiring it cannot overlap; whichever holds `retire:<job>` first wins."""
    w = world
    w.job("src")
    w.store.acquire_lease("retire:src", "resume:req")
    first = run(w)
    assert first["pruned"] == [] and first["pin_reasons"]["src"] == "retire-lease"
    w.store.release_leases("resume:req")
    assert run(w)["pruned"] == ["src"]


def test_stale_resume_fence_is_released(world):
    w = world
    w.job("src2")
    with w.store.transaction() as conn:
        conn.execute("INSERT INTO leases VALUES ('retire:src2','resume:old','2026-01-01T00:00:00Z',NULL)")
    assert run(w)["pruned"] == ["src2"]


# --- confinement and symlinks -------------------------------------------------------------

def test_symlinks_are_archived_as_links_and_never_followed(world):
    w = world
    outside = w.base / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("outside the tree\n")
    wt = w.job("job-links")
    os.symlink(outside, wt / "dir-link")
    os.symlink(outside / "keep.txt", wt / "file-link")
    assert run(w)["pruned"] == ["job-links"]
    assert (outside / "keep.txt").read_text() == "outside the tree\n"
    rarch.restore(w.root, "job-links", to=w.base / "back")
    assert os.readlink(w.base / "back" / "worktree" / "dir-link") == str(outside)


def test_a_swapped_directory_sends_nothing_outside(world, tmp_path):
    """R3-5: a directory replaced by a symlink after archiving is set aside, and
    deletion never reaches through it."""
    w = world
    outside = tmp_path / "victim"
    outside.mkdir()
    (outside / "data.txt").write_text("must survive\n")
    wt = w.job("job-swap")
    (wt / "sub").mkdir()
    (wt / "sub" / "data.txt").write_text("in tree\n")
    original = rarch.Retirement.publish

    def swap(self):
        original(self)
        shutil.rmtree(self.q_worktree / "sub")
        os.symlink(outside, self.q_worktree / "sub")

    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(rarch.Retirement, "publish", swap)
    try:
        assert run(w)["pruned"] == ["job-swap"]
    finally:
        mp.undo()
    assert (outside / "data.txt").read_text() == "must survive\n"
    assert os.path.islink(w.root / "retention-conflicts" / "job-swap" / "worktree" / "sub")


def test_nested_linked_worktree_keeps_the_job(world):
    """A linked worktree inside the job's tree has its admin directory in another
    repository, which retention does not archive: the job is kept."""
    w = world
    wt = w.job("job-nested")
    other_repo = w.base / "home" / "other"
    git(w.base, "init", "--quiet", "-b", "main", str(other_repo))
    (other_repo / "f").write_text("x")
    git(other_repo, "add", "f")
    git(other_repo, "commit", "--quiet", "-m", "x")
    git(other_repo, "worktree", "add", "--quiet", "--detach", str(wt / "nested"), "HEAD")
    result = run(w)
    assert result["pruned"] == [] and "nested-linked-worktree" in result["deferred"]["job-nested"]
    assert (wt / "nested" / "f").exists()


def test_nested_repository_is_archived_byte_for_byte(world):
    w = world
    wt = w.job("job-nested-repo")
    nested = wt / "vendor" / "lib"
    nested.mkdir(parents=True)
    git(nested, "init", "--quiet", "-b", "main")
    (nested / "code.c").write_text("int main(){}\n")
    git(nested, "add", ".")
    git(nested, "commit", "--quiet", "-m", "nested")
    commit = git(nested, "rev-parse", "HEAD")
    before = snapshot(wt)
    assert run(w)["pruned"] == ["job-nested-repo"]
    rarch.restore(w.root, "job-nested-repo")
    assert snapshot(wt) == before
    assert git(nested, "rev-parse", "HEAD") == commit


def test_job_without_worktree_is_archived_with_its_rows(world):
    w = world
    w.job("ro", worktree=False, files={"stdout": b"deliverable", "a1/last.md": b"answer"})
    w.attempt("ro")
    assert run(w)["pruned"] == ["ro"]
    rows = json.loads((w.root / "archive" / "ro" / "rows.json").read_text())["rows"]
    assert rows["jobs"][0]["job_id"] == "ro" and rows["attempts"][0]["attempt_id"] == "ro/a1"
    rarch.restore(w.root, "ro")
    assert (w.root / "jobs" / "ro" / "a1" / "last.md").read_bytes() == b"answer"


def test_missing_worktree_with_a_live_registration_is_anchored(world):
    """R1-7: the tree is gone but its registration names commits; they are anchored."""
    w = world
    wt = w.job("job-gone")
    (wt / "f.txt").write_text("x")
    git(wt, "add", "f.txt")
    git(wt, "commit", "--quiet", "-m", "only here")
    only = git(wt, "rev-parse", "HEAD")
    shutil.rmtree(wt)
    assert run(w)["pruned"] == ["job-gone"]
    heads = rgit.bundle_heads(w.root / "archive" / "job-gone" / "commits.bundle")
    assert heads
    fresh = w.base / "fresh-gone"
    git(w.base, "clone", "--quiet", str(w.remote), str(fresh))
    git(fresh, "fetch", "--quiet", str(w.root / "archive" / "job-gone" / "commits.bundle"), "+refs/*:refs/r/*")
    assert git(fresh, "cat-file", "-t", only) == "commit"


def test_registration_locked_by_someone_else_keeps_the_job(world):
    w = world
    wt = w.job("job-locked")
    (w.admin("job-locked") / "locked").write_text("on an external drive\n")
    result = run(w)
    assert result["pruned"] == [] and "locked by someone else" in result["deferred"]["job-locked"]
    assert wt.is_dir() and (w.admin("job-locked") / "locked").read_text() == "on an external drive\n"


def test_rollback_into_an_occupied_path_keeps_both_and_the_lock(world, monkeypatch):
    """Astra design finding 4: when the original path is occupied at rollback, the
    job's tree goes to conflicts and retention's lock stays on the registration."""
    w = world
    wt = w.job("job-occupied")
    original = rarch.Retirement.final_check

    def occupy_then_fail(self):
        os.makedirs(wt)
        (wt / "occupant").write_text("someone else\n")
        raise rarch.Defer("changed after archive", rarch.DEFER_CHANGED_S, "test")

    monkeypatch.setattr(rarch.Retirement, "final_check", occupy_then_fail)
    result = run(w)
    assert result["pruned"] == []
    assert (wt / "occupant").exists()
    aside = list((w.root / "retention-conflicts" / "job-occupied").iterdir())
    assert aside and (aside[0] / "README.md").exists()
    assert (w.admin("job-occupied") / "locked").exists()


def test_archive_check_detects_damage(world):
    w = world
    w.job("job-check", files={"stdout": b"x" * 1000})
    assert run(w)["pruned"] == ["job-check"]
    assert rarch.check_archive(w.root, "job-check")["ok"]
    files = w.root / "archive" / "job-check" / "files"
    victim = next(files.iterdir())
    os.chmod(victim, 0o600)
    victim.write_bytes(b"y" * victim.stat().st_size)
    assert not rarch.check_archive(w.root, "job-check")["ok"]
    with pytest.raises(rarch.RestoreError):
        rarch.restore(w.root, "job-check", to=w.base / "no")


def test_byte_copy_path_when_clones_are_unavailable(world, monkeypatch):
    monkeypatch.setattr(rfs, "FORCE_COPY", True)
    w = world
    job = make_dirty_detached(w, "job-copy")
    before = snapshot(job["worktree"])
    assert run(w)["pruned"] == ["job-copy"]
    manifest = json.loads((w.root / "archive" / "job-copy" / "manifest.json").read_text())
    assert manifest["totals"]["copies"] > 0 and manifest["totals"]["clones"] == 0
    rarch.restore(w.root, "job-copy")
    assert snapshot(job["worktree"]) == before


@pytest.mark.real_lsof
@pytest.mark.skipif(not os.access("/usr/sbin/lsof", os.X_OK), reason="needs lsof")
def test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree(world):
    """E3: after the rename into quarantine, lsof still names the holder."""
    w = world
    wt = w.job("job-cwd")
    holder = subprocess.Popen(["/bin/sleep", "600"], cwd=wt)
    try:
        result = retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0)
        assert result["pruned"] == [] and "busy" in result["deferred"]["job-cwd"], result
        assert wt.is_dir()
    finally:
        holder.kill()
        holder.wait()
