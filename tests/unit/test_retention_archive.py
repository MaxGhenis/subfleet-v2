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
import time
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


def test_a_remote_on_this_machine_is_no_network_remote():
    """Review of the revision-4 build: a clone reached over ssh or http on this
    machine holds nothing elsewhere."""
    for url in ("ssh://localhost/tmp/x.git", "me@localhost:repo.git", "http://127.0.0.1:8080/x.git",
                "ssh://[::1]/x.git", "git@mac.local:x.git", "https://build.localhost/x.git"):
        assert not rgit.network_url(url), url
    for url in ("https://github.com/o/r.git", "git@github.com:o/r.git", "ssh://git@host.example.com/r.git"):
        assert rgit.network_url(url), url


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

def test_parked_jobs_take_turns_at_the_pass_time(world, monkeypatch):
    """Review of a9a6cbf4, N3 (head of line): two slow jobs, and time for one
    slice a pass. In-flight jobs are sliced least recently sliced first, so
    they take turns instead of the first in name order holding every pass
    until it is done."""
    w = world
    for n, job_id in enumerate(["slow-a", "slow-b"]):
        wt = w.job(job_id, created=f"2026-09-01T00:0{n}:00Z")
        for i in range(4):
            (wt / f"big-{i}.bin").write_bytes(os.urandom(1000))
    # `deadline` is also checked against the real monotonic clock before a
    # pass starts: the fake clock runs far ahead of it.
    clock = Clock(start=time.monotonic() + 1e6)
    current = {}
    sliced = []
    original_run = rarch._Builder.run
    original_read = rfs.read_hashes

    def run_builder(self):
        current["job"] = self.r.job_id
        sliced.append(self.r.job_id)
        try:
            return original_run(self)
        finally:
            current.pop("job", None)

    def slow_read(fd, size, fmt, check=None):
        if current.get("job"):
            clock.advance(40)
        return original_read(fd, size, fmt, check)

    monkeypatch.setattr(rarch._Builder, "run", run_builder)
    monkeypatch.setattr(rfs, "read_hashes", slow_read)
    state = retention.RetentionState()
    for _ in range(4):
        run(w, clock=clock, state=state, slice_s=60, deadline=clock() + 1)
    assert sliced[:4] == ["slow-a", "slow-b", "slow-a", "slow-b"], sliced
    for _ in range(80):
        run(w, clock=clock, state=state, slice_s=60, deadline=clock() + 1)
        if w.store.get_job("slow-a") is None and w.store.get_job("slow-b") is None:
            break
    assert w.store.get_job("slow-a") is None and w.store.get_job("slow-b") is None
    # Turns all the way: neither job had two slices in a row while the other waited.
    both = [job for job in sliced if job in ("slow-a", "slow-b")]
    last_a, last_b = len(both) - 1 - both[::-1].index("slow-a"), len(both) - 1 - both[::-1].index("slow-b")
    alternating = both[:min(last_a, last_b) + 1]
    assert all(x != y for x, y in zip(alternating, alternating[1:])), both
    assert state.sliced == {}


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


def test_an_unmeasurable_job_is_decided_and_backs_off(world):
    """Review of a9a6cbf4, N2 (the reviewer's probe): a job whose size cannot be
    measured (a mode-000 directory) counted as unmeasured for ever, so every
    pass reported more work and the daemon ran one every 5 seconds. Its size is
    now unknown, which decides it; it is measured again only after a backoff
    that doubles."""
    import time as _time
    w = world
    w.job("ok-job", worktree=False)
    w.job("odd-job", worktree=False)
    locked = w.root / "jobs" / "odd-job" / "locked-dir"
    locked.mkdir()
    (locked / "x").write_text("x")
    os.chmod(locked, 0)
    try:
        state, clock = retention.RetentionState(), Clock()

        def one_pass():
            return run(w, max_jobs=100, max_bytes=1 << 30, state=state, clock=clock,
                       deadline=_time.monotonic() + 180)

        first = one_pass()
        assert first["more"] is False, first["pools"]
        assert first["pools"]["detached"]["unknown_size"] == 1 and first["pools"]["detached"]["unmeasured"] == 0
        assert [e["job_id"] for e in first["errors"]] == ["odd-job"] and "odd-job" in first["protected"]
        second = one_pass()                                    # within the backoff: not measured again
        assert second["more"] is False and second["errors"] == [] and "odd-job" in second["protected"]
        assert second["progressed"] is False
        clock.advance(retention.SIZE_ERROR_S + 1)
        third = one_pass()                                     # due again: measured, fails, backs off longer
        assert [e["job_id"] for e in third["errors"]] == ["odd-job"] and third["more"] is False
        assert state.unmeasurable["odd-job"][1] == 2 * retention.SIZE_ERROR_S
        os.chmod(locked, 0o700)
        clock.advance(2 * retention.SIZE_ERROR_S + 1)
        fourth = one_pass()
        assert fourth["errors"] == [] and "odd-job" not in state.unmeasurable
        assert fourth["pools"]["detached"]["unknown_size"] == 0
    finally:
        os.chmod(locked, 0o700)


def test_a_persistent_error_keeps_the_cache_and_backs_off(world, monkeypatch):
    """Review of a9a6cbf4, N10: an error rolled the job back with its archive
    cache dropped, so a lasting error re-read the whole tree every 6 hours. The
    cache stays; the deferral doubles; a restart (a new daemon state) keeps the
    deferral the idle journal recorded; and when the error clears, the files
    read before are not read again."""
    w = world
    wt = w.job("job-err")
    (wt / "big.bin").write_bytes(os.urandom(50000))
    reads = []
    original_read = rfs.read_hashes
    lock_size = len(f"{rgit.LOCK_MARKER}job-err\n")      # retention's lock is written anew by each attempt
    monkeypatch.setattr(rfs, "read_hashes", lambda fd, size, fmt, check=None:
                        (size != lock_size and reads.append(size)) or original_read(fd, size, fmt, check))
    original_bundle = rgit.create_bundle

    def failing_bundle(*args, **kwargs):
        raise rgit.GitError("git bundle create exited 128: fatal: simulated")

    monkeypatch.setattr(rgit, "create_bundle", failing_bundle)
    state, clock = retention.RetentionState(), Clock()
    first = run(w, state=state, clock=clock)
    assert first["pruned"] == [] and first["deferred"]["job-err"].startswith("error")
    journal = rarch.load_journal(w.root, "job-err")
    assert journal["state"] == "idle" and journal["failures"] == 1
    assert (w.root / "retention" / "job-err" / "archive" / "progress.jsonl").exists()   # the cache stays
    assert wt.is_dir() and w.store.get_job("job-err") is not None
    read_once = len(reads)
    assert read_once > 0
    clock.advance(rarch.DEFER_ERROR_S + 1)
    second = run(w, state=state, clock=clock)
    assert second["pruned"] == [] and rarch.load_journal(w.root, "job-err")["failures"] == 2
    assert len(reads) == read_once                                     # cached: nothing read again
    # A restart forgets in-memory deferrals; the idle journal still defers the job.
    restarted = retention.RetentionState()
    clock.advance(rarch.DEFER_ERROR_S + 1)                             # past the first backoff, not the second
    third = run(w, state=restarted, clock=clock)
    assert third["pruned"] == [] and "job-err" in third["deferred"]
    assert rarch.load_journal(w.root, "job-err")["failures"] == 2      # not attempted
    monkeypatch.setattr(rgit, "create_bundle", original_bundle)
    clock.advance(2 * rarch.DEFER_ERROR_S)
    last = run(w, state=restarted, clock=clock)
    assert last["pruned"] == ["job-err"], last["deferred"]
    manifest = json.loads((w.root / "archive" / "job-err" / "manifest.json").read_text())
    stored = [e for t in manifest["trees"].values() for e in t["entries"] if e.get("store") and not e.get("hl")]
    # Only the read-back of each stored file (the lock aside) happened since.
    assert len(reads) == read_once + len([e for e in stored if e["size"] != lock_size])
    assert rarch.check_archive(w.root, "job-err")["ok"]


def test_a_slice_moves_forward_when_one_file_outlasts_it(world, monkeypatch):
    """Review of a9a6cbf4, N3: a slice stopped in the middle of a file whose read
    outlasted it, and the next slice read it from the start again, for ever.
    One file's read now runs to its end; the slice stops between files."""
    w = world
    wt = w.job("job-bigfile")
    (wt / "big.bin").write_bytes(os.urandom(300 * 1024))
    monkeypatch.setattr(rfs, "CHUNK", 1024)                            # a check every 64 KiB
    clock = Clock()
    original = rfs.read_hashes

    def slow(fd, size, fmt, check=None):
        if size == 300 * 1024:
            clock.advance(100)                                         # longer than the whole slice
        return original(fd, size, fmt, check)

    monkeypatch.setattr(rfs, "read_hashes", slow)
    state = retention.RetentionState()
    for _ in range(12):
        result = run(w, state=state, clock=clock, slice_s=60)
        assert result["progressed"], result
        if result["pruned"]:
            break
    assert result["pruned"] == ["job-bigfile"], result


def test_a_slice_moves_forward_when_the_cached_rewalk_outlasts_it(world, monkeypatch):
    """Review of a9a6cbf4, N3: every slice re-walks the tree from the start; when
    walking the entries already archived took the whole slice, no slice reached
    a new one. A slice now parks only after it has done new work."""
    w = world
    wt = w.job("job-wide")
    for n in range(4):
        (wt / f"f{n:02}.txt").write_text(f"file {n}\n")
    clock = Clock()
    original = rfs.walk

    def slow_walk(fd, check=None):
        for item in original(fd, check):
            if getattr(check, "__name__", "") == "_tick":
                clock.advance(10)                                      # each entry costs 10 s
            yield item

    monkeypatch.setattr(rfs, "walk", slow_walk)
    state = retention.RetentionState()
    for _ in range(150):
        result = run(w, state=state, clock=clock, slice_s=60)
        assert result["progressed"], result
        if result["pruned"]:
            break
    assert result["pruned"] == ["job-wide"], result


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


def test_a_published_archive_keeps_no_progress_log(world, monkeypatch):
    """N6: published archives keep no progress log. Publish moves it beside
    the journal for per-file reclamation checks, including after a crash
    between the archive rename and the progress-log move."""
    w = world
    make_dirty_detached(w, "job-log")
    w.job("job-log-2")
    real = rfs.sync_path
    crashed = []

    class Crash(BaseException):
        pass

    def crash_after_the_rename(path):
        real(path)
        if Path(path) == w.root / "archive" and not crashed:
            crashed.append(path)
            raise Crash()

    monkeypatch.setattr(rfs, "sync_path", crash_after_the_rename)
    with pytest.raises(Crash):
        run(w)
    monkeypatch.setattr(rfs, "sync_path", real)
    published = [p for p in (w.root / "archive").iterdir()]
    assert len(published) == 1 and (published[0] / rarch.PROGRESS).exists()   # renamed, not yet cleaned
    run(w)
    for job_id in ("job-log", "job-log-2"):
        archive = w.root / "archive" / job_id
        assert (archive / "manifest.json").exists() and not (archive / rarch.PROGRESS).exists(), job_id
        assert rarch.check_archive(w.root, job_id)["ok"]
    rarch.restore(w.root, "job-log", to=w.base / "back")


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


@pytest.mark.parametrize("leftover", ["plain-directory", "detached-worktree"])
def test_rebuilding_a_conversation_worktree_removes_only_its_own_registration(world, leftover):
    """Review of a9a6cbf4, B2: `_cut_worktree` rebuilt a directory that was not
    a worktree on its branch with a repository-wide `git worktree prune`, which
    dropped a job's registration whose tree was missing, and the commit only
    its HEAD held. Now only the conversation's own registration goes."""
    import types

    from subfleet.conversations.service import ConversationService
    from subfleet.salvage import git_toplevel
    w = world
    job_tree = w.root / "worktrees" / "job-x"
    git(w.repo, "worktree", "add", "--quiet", "--detach", str(job_tree), "HEAD")
    (job_tree / "g").write_text("unpushed")
    git(job_tree, "add", "g")
    git(job_tree, "commit", "--quiet", "-m", "only in this worktree's HEAD")
    only = git(job_tree, "rev-parse", "HEAD")
    shutil.rmtree(job_tree)                                  # tree missing, registration unlocked
    target = w.root / "worktrees" / "conversation-c1"
    if leftover == "plain-directory":
        target.mkdir()                                       # an add cut short before git wrote anything
    else:
        git(w.repo, "worktree", "add", "--quiet", "--detach", str(target), "HEAD")   # registered, off its branch
    stub = types.SimpleNamespace(
        store=types.SimpleNamespace(subdirectory=lambda name: w.root / name,
                                    update_conversation=lambda cid, **fields: fields),
        daemon=types.SimpleNamespace(policy={}))
    stub._git_toplevel = lambda directory: git_toplevel(directory, timeout_s=60)
    stub._git_timeout_s = lambda key="workspace_git_timeout_s": 60.0
    record = ConversationService._cut_worktree(stub, {"conversation_id": "c1", "workspace": str(w.repo)})
    assert record["worktree"]["branch"] == "subfleet/c1"
    assert git(target, "symbolic-ref", "--short", "HEAD") == "subfleet/c1"
    assert (w.repo / ".git" / "worktrees" / "job-x").is_dir()
    assert git(w.repo, "rev-parse", "worktrees/job-x/HEAD") == only


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


@pytest.mark.parametrize("state", ["queued", "running"])
def test_a_job_still_to_run_inside_the_tree_keeps_it(world, state):
    """Design review (Opus 9): a job an agent submitted from inside its worktree,
    not yet ended, needs the tree; once it has ended, the tree may go."""
    w = world
    wt = w.job("host")
    (wt / "sub").mkdir()
    w.store.add_job(job_id="guest", request_id="guest", payload_digest="d", kind="dispatch",
                    workdir=str(wt / "sub"), prompt_path="/p", sandbox="read-only", state=state)
    first = run(w, referenced_job_ids=["guest"])
    assert "host" not in first["pruned"] and first["pin_reasons"]["host"] == "worktree-in-use"
    w.store.update_job("guest", state="succeeded")
    assert run(w, referenced_job_ids=["guest"])["pruned"] == ["host"]


def test_a_crash_between_the_two_quarantine_renames_moves_the_rest(world, monkeypatch):
    """Recovery of `quarantining` finishes the move, so the job directory is
    archived with the tree rather than left behind."""
    w = world
    wt = w.job("half-moved")
    original = os.rename
    calls = []

    class Crash(BaseException):
        pass

    def rename_once(src, dst, *args, **kwargs):
        calls.append(str(src))
        if len(calls) == 2 and str(src).endswith("/jobs/half-moved"):
            raise Crash()
        return original(src, dst, *args, **kwargs)

    monkeypatch.setattr(rarch.os, "rename", rename_once)
    with pytest.raises(Crash):
        run(w)
    monkeypatch.setattr(rarch.os, "rename", original)
    assert (w.root / "jobs" / "half-moved").exists() and not wt.exists()
    assert run(w)["pruned"] == ["half-moved"]
    manifest = json.loads((w.root / "archive" / "half-moved" / "manifest.json").read_text())
    assert set(manifest["trees"]) == {"worktree", "job", "admin"}
    assert not (w.root / "jobs" / "half-moved").exists()


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
    archive = w.root / "archive" / "job-salvage"
    manifest = json.loads((archive / "manifest.json").read_text())
    anchor = manifest["git"]["anchor"]
    # The anchor is the bundle's only head; the salvage commit is its ancestor.
    assert rgit.bundle_heads(archive / "commits.bundle") == {manifest["git"]["anchor_ref"]: anchor}
    assert manifest["git"]["salvage_in_anchor"] == [tree_commit]
    assert git(w.repo, "rev-parse", "refs/subfleet-salvage/job-salvage/1") == tree_commit
    rows = json.loads((archive / "rows.json").read_text())
    assert rows["rows"]["artifacts"][0]["path"] == "refs/subfleet-salvage/job-salvage/1"
    fresh = w.base / "fresh-salvage"
    git(w.base, "clone", "--quiet", str(w.remote), str(fresh))
    git(fresh, "fetch", "--quiet", str(archive / "commits.bundle"), "+refs/*:refs/r/*")
    assert git(fresh, "cat-file", "-t", tree_commit) == "commit"
    report = rarch.restore(w.root, "job-salvage", to=w.base / "restored", repository=fresh)
    assert report["refs"] == {"refs/subfleet-restored/job-salvage/subfleet-salvage/job-salvage/1": tree_commit}
    assert git(fresh, "rev-parse", "refs/subfleet-restored/job-salvage/subfleet-salvage/job-salvage/1") == tree_commit


@pytest.mark.parametrize("registration", [True, False], ids=["with-registration", "without-registration"])
def test_salvage_a_network_remote_already_holds_retires(world, registration):
    """Review of a9a6cbf4, B1: a salvage ref pushed as a PR branch and fetched is
    reachable from `refs/remotes/*`, so `git bundle` left it out: the bundle
    lacked it (with a registration) or was empty (without one), and the job
    was rolled back every attempt, for ever. The anchor is now built whenever
    the repository is known and is the bundle's only head; the salvage commit
    is its ancestor, and the job retires in the first pass."""
    w = world
    wt = w.job("job-pushed")
    w.attempt("job-pushed")
    (wt / "work.txt").write_text("salvaged work\n")
    commit = git(wt, "commit-tree", git(wt, "write-tree"), "-p", w.head(), "-m", "salvage")
    ref = "refs/subfleet-salvage/job-pushed/1"
    git(w.repo, "update-ref", ref, commit)
    w.store.add_artifact("job-pushed/a1", "salvage", ref, "d", 0)
    w.push(f"{ref}:refs/heads/pr-job-pushed")
    assert git(w.repo, "branch", "-r", "--contains", commit)
    if not registration:
        shutil.rmtree(w.admin("job-pushed"))         # pruned earlier, e.g. by an old repository-wide prune
    result = run(w, state=retention.RetentionState(), clock=Clock())
    assert result["pruned"] == ["job-pushed"], result["deferred"]
    archive = w.root / "archive" / "job-pushed"
    manifest = json.loads((archive / "manifest.json").read_text())
    assert manifest["git"]["salvage_in_anchor"] == [commit]
    assert rgit.bundle_heads(archive / "commits.bundle") == {manifest["git"]["anchor_ref"]: manifest["git"]["anchor"]}
    assert git(w.repo, "rev-parse", ref) == commit                  # the ref itself is never touched
    assert rarch.check_archive(w.root, "job-pushed")["ok"]
    report = rarch.restore(w.root, "job-pushed", to=w.base / "restored")
    assert report["refs"] == {"refs/subfleet-restored/job-pushed/subfleet-salvage/job-pushed/1": commit}
    assert (w.base / "restored" / "worktree" / "work.txt").read_text() == "salvaged work\n"


def test_an_anchor_a_network_remote_already_holds_needs_no_bundle(world):
    """If the anchor itself is reachable from `refs/remotes/*` (its ref pushed
    and fetched back), every commit it reaches is held and a bundle would be
    empty: the archive records that instead of failing."""
    w = world
    make_dirty_detached(w, "job-held")
    calls = []

    def busy_at_second_check(watches, **_):
        calls.append(1)
        return {"job-held": ["pid 1 (editor): open for writing"]} if len(calls) == 2 else {}

    state, clock = retention.RetentionState(), Clock()
    assert run(w, holders=busy_at_second_check, state=state, clock=clock)["pruned"] == []
    anchor_ref = git(w.repo, "for-each-ref", "--format=%(refname)", "refs/subfleet-archive/job-held/")
    w.push(f"{anchor_ref}:refs/heads/mirrored-anchor")
    clock.advance(rarch.DEFER_BUSY_S + 1)
    result = run(w, state=state, clock=clock)
    assert result["pruned"] == ["job-held"], result["deferred"]
    manifest = json.loads((w.root / "archive" / "job-held" / "manifest.json").read_text())
    assert manifest["git"]["anchor_held"] is True and manifest["git"]["bundle"] is None
    assert not (w.root / "archive" / "job-held" / "commits.bundle").exists()
    assert rarch.check_archive(w.root, "job-held")["ok"]


def test_a_cached_bundle_is_rebuilt_when_remote_tracking_refs_move(world, monkeypatch):
    """Review of a9a6cbf4, N12: the bundle cache was keyed on the held globs, not
    their values; a branch deleted on the server after the first attempt left
    the cached bundle with prerequisites no remote-tracking ref held."""
    w = world
    wt = w.job("job-cache")
    (wt / "f.txt").write_text("x")
    git(wt, "add", "f.txt")
    git(wt, "commit", "--quiet", "-m", "on a pushed branch")
    topic = git(wt, "rev-parse", "HEAD")
    w.push(f"{topic}:refs/heads/topic")                              # the commit is held by origin/topic
    calls = []

    def busy_at_second_check(watches, **_):
        calls.append(1)
        return {"job-cache": ["pid 1 (editor): open for writing"]} if len(calls) == 2 else {}

    state, clock = retention.RetentionState(), Clock()
    assert run(w, holders=busy_at_second_check, state=state, clock=clock)["pruned"] == []
    git(w.remote, "update-ref", "-d", "refs/heads/topic")              # the server drops the branch
    git(w.repo, "fetch", "--quiet", "--prune", str(w.remote), "+refs/heads/*:refs/remotes/origin/*")
    made = []
    original = rgit.create_bundle
    monkeypatch.setattr(rgit, "create_bundle", lambda *a, **k: made.append(1) or original(*a, **k))
    clock.advance(rarch.DEFER_BUSY_S + 1)
    assert run(w, state=state, clock=clock)["pruned"] == ["job-cache"]
    assert made == [1]                                                 # rebuilt, not reused
    fresh = w.base / "fresh-cache"
    git(w.base, "clone", "--quiet", str(w.remote), str(fresh))
    git(fresh, "fetch", "--quiet", str(w.root / "archive" / "job-cache" / "commits.bundle"), "+refs/*:refs/r/*")
    assert git(fresh, "cat-file", "-t", topic) == "commit"


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


def _nested_host(w: World) -> tuple[Path, Path, str]:
    """Job A's tree holds a clone; job B's worktree is registered in it, with a
    commit only B's HEAD holds (live: four job worktrees registered in
    `decide-d051/policyengine-core/.git`)."""
    a_wt = w.job("job-a", created="2026-09-01T00:00:00Z")
    nested = a_wt / "core"
    git(w.base, "clone", "--quiet", str(w.remote), str(nested))
    git(nested, "remote", "set-url", "origin", "https://git.example.invalid/project.git")
    b_wt = w.root / "worktrees" / "job-b"
    git(nested, "worktree", "add", "--quiet", "--detach", str(b_wt), "HEAD")
    (b_wt / "b-work.txt").write_text("B's committed work\n")
    git(b_wt, "add", "b-work.txt")
    git(b_wt, "commit", "--quiet", "-m", "only B's HEAD")
    w.store.add_job(job_id="job-b", request_id="req-b", payload_digest="d", kind="dispatch", workdir=str(nested),
                    workdir_head=git(nested, "rev-parse", "HEAD"), worktree=str(b_wt), prompt_path="/p",
                    sandbox="workspace-write", state="succeeded", created_at="2026-09-02T00:00:00Z")
    (w.root / "jobs" / "job-b").mkdir()
    (w.root / "jobs" / "job-b" / "stdout").write_text("b\n")
    return a_wt, b_wt, git(b_wt, "rev-parse", "HEAD")


def _in_own_bundle(w: World, job_id: str, commit: str) -> bool:
    """Whether a fresh clone of the remote plus the job's own bundle holds `commit`."""
    fresh = w.base / f"fresh-{job_id}"
    git(w.base, "clone", "--quiet", str(w.remote), str(fresh))
    git(fresh, "fetch", "--quiet", str(w.root / "archive" / job_id / "commits.bundle"), "+refs/*:refs/restored/*")
    return subprocess.run(["git", "-C", str(fresh), "cat-file", "-e", commit]).returncode == 0


def test_a_job_registered_inside_another_jobs_tree_retires_first_with_its_own_anchor(world):
    """N4 (final review of e50716e8): the host is pinned while the job
    registered inside its tree has rows, so the two never retire in one pass
    and the hosted job's archive has its own anchor and bundle."""
    w = world
    a_wt, b_wt, b_commit = _nested_host(w)
    assert retention.nested_hosts(w.store.list_jobs(), w.root.resolve()) == {"job-a": {"job-b"}}
    first = run(w)
    assert first["pruned"] == ["job-b"], first["deferred"]
    assert first["pin_reasons"]["job-a"] == "nested-host: job-b" and a_wt.is_dir()     # names what it waits for
    manifest = json.loads((w.root / "archive" / "job-b" / "manifest.json").read_text())
    assert manifest["git"]["anchor"] and manifest["git"]["bundle"] == "commits.bundle"
    assert manifest["git"]["head"] == b_commit
    assert _in_own_bundle(w, "job-b", b_commit)
    second = run(w)
    assert second["pruned"] == ["job-a"], second["deferred"]


def test_an_in_flight_host_is_refused_at_commit_and_its_guest_waits_for_it(world, monkeypatch):
    """N4: a host already in flight when the job inside it is seen (a pass
    before this rule) is refused at commit and put back; the hosted job, whose
    registration was away with the host, is kept meanwhile rather than retired
    without its anchor; then it retires first, and the host after it."""
    w = world
    a_wt, b_wt, b_commit = _nested_host(w)
    clock = Clock()
    state = retention.RetentionState()
    real_hosts, real_read = retention.nested_hosts, rfs.read_hashes

    def slow(fd, size, fmt, check=None):
        clock.advance(40)
        return real_read(fd, size, fmt, check)

    monkeypatch.setattr(retention, "nested_hosts", lambda jobs, root: {})
    monkeypatch.setattr(rfs, "read_hashes", slow)
    first = run(w, clock=clock, state=state, slice_s=60)
    assert first["in_flight"] == ["job-a"], first
    assert first["deferred"]["job-b"].startswith("nested-host"), first["deferred"]
    assert b_wt.is_dir() and w.store.get_job("job-b") and not a_wt.exists()
    monkeypatch.setattr(retention, "nested_hosts", real_hosts)
    monkeypatch.setattr(rfs, "read_hashes", real_read)
    second = run(w, clock=clock, state=state, slice_s=60)
    assert second["pruned"] == [] and second["deferred"]["job-a"].startswith("pinned: nested-host"), second
    assert a_wt.is_dir() and (a_wt / "core" / ".git" / "worktrees" / "job-b").is_dir()
    clock.advance(rarch.DEFER_PINNED_S + 1)
    third = run(w, clock=clock, state=state)
    assert third["pruned"] == ["job-b"], third["deferred"]
    assert _in_own_bundle(w, "job-b", b_commit)
    fourth = run(w, clock=clock, state=state)
    assert fourth["pruned"] == ["job-a"], fourth["deferred"]


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


def _reduce_to_remnant(admin: Path, *, extra: str | None = None) -> bytes:
    """What a temporary directory's cleaner leaves: every file gone but the
    index, the directories kept (live: the four `mstat6-g*` jobs)."""
    index = (admin / "index").read_bytes()
    for path in sorted(admin.rglob("*"), key=lambda p: -len(p.parts)):
        if path.is_file() and path.name != "index":
            path.unlink()
        elif path.is_dir() and path != admin / "logs":
            path.rmdir()
    if extra:
        (admin / extra).write_text("0" * 40 + "\n")
    return index


def test_a_registration_reduced_to_its_index_and_logs_is_archived_not_kept(world):
    """N5 (final review of e50716e8): an admin directory holding only `index`
    and `logs/` (no `gitdir`, `commondir` or `HEAD`) is no registration. The
    tree and the remnant's bytes are archived, and the remnant is removed with
    the tree, instead of the job being deferred every day for ever."""
    w = world
    wt = w.job("job-remnant")
    (wt / "notes.txt").write_text("work only this tree holds\n")
    admin = w.admin("job-remnant")
    index = _reduce_to_remnant(admin)
    assert sorted(os.listdir(admin)) == ["index", "logs"]
    assert rgit.registration(wt) == (None, "admin-remnant")
    before = snapshot(wt)
    result = run(w)
    assert result["pruned"] == ["job-remnant"], result["deferred"]
    manifest = json.loads((w.root / "archive" / "job-remnant" / "manifest.json").read_text())
    assert manifest["git"]["admin_remnant"] is True and manifest["git"]["admin"] == str(admin)
    assert {e["p"] for e in manifest["trees"]["admin"]["entries"]} == {"", "index", "logs"}
    assert not admin.exists() and not wt.exists()
    rarch.restore(w.root, "job-remnant", to=w.base / "back")
    assert snapshot(w.base / "back" / "worktree") == before
    assert (w.base / "back" / "admin" / "index").read_bytes() == index


def test_an_admin_directory_that_lost_its_backlink_but_holds_more_keeps_the_job(world):
    """Only the remnant is taken for no registration; an admin directory that
    lost its backlink but still names something (here ORIG_HEAD) keeps its
    job, as before."""
    w = world
    wt = w.job("job-half")
    _reduce_to_remnant(w.admin("job-half"), extra="ORIG_HEAD")
    reg, why = rgit.registration(wt)
    assert reg is None and why.startswith("admin-unreadable")
    result = run(w)
    assert result["pruned"] == [] and result["deferred"]["job-half"].startswith("registration"), result
    assert wt.is_dir() and (w.admin("job-half") / "ORIG_HEAD").exists()


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


# --- the survey and the command line ---------------------------------------------------

def _tree_state(path: Path) -> dict:
    out = {}
    for directory, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            p = Path(directory) / name
            if name.startswith("state.sqlite3"):
                continue
            st = p.lstat()
            out[str(p)] = (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino)
    return out


def test_survey_is_read_only_and_predicts_the_pass(world):
    """The live dry run applies the pass's eligibility and writes nothing: not
    the state root, not the source repository (no index refresh, no ref)."""
    from subfleet.retention_survey import survey
    w = world
    make_dirty_detached(w, "s-dirty")
    w.job("s-clean")
    w.job("s-pinned")
    w.store.add_notice("s-pinned", "unread", "session-1")
    before = {**_tree_state(w.root), **_tree_state(w.repo)}
    report = survey(w.root, holders=False, sample_throughput=False, budgets={"detached": (0, 0), "turn": (0, 0)})
    assert {**_tree_state(w.root), **_tree_state(w.repo)} == before
    predicted = {c["job_id"] for c in report["candidates"]}
    assert predicted == {"s-dirty", "s-clean"}
    assert report["kept"]["jobs_by_reason"]["unread-notice"] == 1
    assert report["would_retire"]["with_worktree"] == 2
    assert report["would_retire"]["omittable_bytes_upper_bound"] > 0
    result = run(w)
    assert set(result["pruned"]) == predicted


def test_retention_command_lists_checks_and_restores(world, monkeypatch, capsys):
    from subfleet import cli
    w = world
    wt = w.job("cli-job")
    (wt / "note.txt").write_text("kept by the archive\n")
    assert run(w)["pruned"] == ["cli-job"]
    monkeypatch.setenv("SUBFLEET_HOME", str(w.root))
    assert cli.main(["retention", "archives", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)["archives"]
    assert [a["job_id"] for a in listed] == ["cli-job"]
    assert cli.main(["retention", "restore", "cli-job", "--check", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cli.main(["retention", "restore", "cli-job", "--to", str(w.base / "cli-out"), "--json"]) == 0
    assert (w.base / "cli-out" / "worktree" / "note.txt").read_text() == "kept by the archive\n"
    assert cli.main(["retention", "restore", "cli-job", "--to", str(w.base / "cli-out")]) != 0


# --- guards the mutation run needs a failing test for -----------------------------------

def test_a_write_while_a_file_is_archived_puts_the_job_back(world, monkeypatch):
    """The re-fstat around each file's clone and read: a write in between
    means the clone may not be the bytes read, so the job goes back."""
    w = world
    wt = w.job("job-writing")
    (wt / "out.log").write_text("first\n")
    original = rfs.clone_or_copy

    def clone_then_write(src_fd, dst_dir_fd, name, check=None):
        method = original(src_fd, dst_dir_fd, name, check)
        target = w.root / "retention" / "job-writing" / "worktree" / "out.log"
        if target.exists() and os.fstat(src_fd).st_ino == target.stat().st_ino and target.read_text() == "first\n":
            with open(target, "a") as stream:          # the writer still has it open
                stream.write("second\n")
        return method

    monkeypatch.setattr(rfs, "clone_or_copy", clone_then_write)
    result = run(w)
    assert result["pruned"] == [] and result["deferred"]["job-writing"].startswith("changed")
    assert (wt / "out.log").read_text() == "first\nsecond\n"


def test_an_archive_that_does_not_read_back_authorizes_nothing(world, monkeypatch):
    """I8: a stored file that does not read back (here overwritten after it was
    cloned) fails verification; nothing is deleted."""
    w = world
    wt = w.job("job-badcopy")
    original = rfs.clone_or_copy

    def clone_then_spoil(src_fd, dst_dir_fd, name, check=None):
        method = original(src_fd, dst_dir_fd, name, check)
        fd = os.open(name, os.O_WRONLY, dir_fd=dst_dir_fd)
        try:
            os.write(fd, b"X")
        finally:
            os.close(fd)
        return method

    monkeypatch.setattr(rfs, "clone_or_copy", clone_then_spoil)
    state, clock = retention.RetentionState(), Clock()
    result = run(w, state=state, clock=clock)
    assert result["pruned"] == [] and "did not read back" in result["deferred"]["job-badcopy"]
    assert wt.is_dir() and w.store.get_job("job-badcopy") is not None
    # The cache is kept (N10), but not the copy that did not read back: the
    # next attempt clones that file again and retires the job.
    monkeypatch.setattr(rfs, "clone_or_copy", original)
    clock.advance(rarch.DEFER_ERROR_S + 1)
    assert run(w, state=state, clock=clock)["pruned"] == ["job-badcopy"]
    assert rarch.check_archive(w.root, "job-badcopy")["ok"]


def test_a_prune_during_archiving_cannot_drop_the_registration(world, monkeypatch):
    """The lock written before the tree moves: a `git worktree prune` (or a gc)
    while the tree is away leaves the registration and what it names."""
    w = world
    wt = w.job("job-prune")
    (wt / "x").write_text("x")
    git(wt, "add", "x")
    git(wt, "commit", "--quiet", "-m", "only here")
    only = git(wt, "rev-parse", "HEAD")
    original = rarch._Builder.run

    def prune_then_run(self):
        git(w.repo, "worktree", "prune")
        git(w.repo, "gc", "--quiet", "--prune=now")
        return original(self)

    monkeypatch.setattr(rarch._Builder, "run", prune_then_run)
    assert run(w)["pruned"] == ["job-prune"]
    fresh = w.base / "fresh-prune"
    git(w.base, "clone", "--quiet", str(w.remote), str(fresh))
    git(fresh, "fetch", "--quiet", str(w.root / "archive" / "job-prune" / "commits.bundle"), "+refs/*:refs/r/*")
    assert git(fresh, "cat-file", "-t", only) == "commit"


def test_a_row_that_changes_before_the_commit_is_in_rows_json(world, monkeypatch):
    """The commit transaction compares the rows it deletes with rows.json; a
    row added in between is written into rows.json before anything is deleted."""
    w = world
    w.job("job-rows", worktree=False)
    original = rarch.job_rows
    calls = []

    def rows_then_change(conn_or_store, job_id):
        rows = original(conn_or_store, job_id)
        calls.append(1)
        if len(calls) == 1:
            w.store.add_notice(job_id, "late notice", None, state="acknowledged")
        return rows

    monkeypatch.setattr(rarch, "job_rows", rows_then_change)
    assert run(w)["pruned"] == ["job-rows"]
    saved = json.loads((w.root / "archive" / "job-rows" / "rows.json").read_text())["rows"]
    assert [n["text"] for n in saved["notices"]] == ["late notice"]


# --- what the archive adds (final review of e50716e8, N1) ---------------------------------------

def test_the_space_the_archive_adds_is_reported_wherever_freed_bytes_are(world, monkeypatch, capsys):
    """The pass result, each pool, both events and `retention archives` report
    `added_bytes`: the bundle, manifest, summary and rows of the published
    archive (and byte copies). In a repository with no network remote the
    bundle carries the whole history, and that is where it shows."""
    from subfleet import cli
    w = world
    git(w.repo, "remote", "remove", "origin")
    for n in range(3):
        (w.repo / f"history{n}.bin").write_bytes(os.urandom(50_000))
        git(w.repo, "add", ".")
        git(w.repo, "commit", "--quiet", "-m", f"history {n}")
    w.job("job-added")
    result = run(w)
    assert result["pruned"] == ["job-added"], result["deferred"]
    archive = w.root / "archive" / "job-added"
    sizes = {name: (archive / name).stat().st_size for name in rarch.ADDED if (archive / name).exists()}
    assert set(sizes) == set(rarch.ADDED) and sizes["commits.bundle"] > 150_000     # the whole history
    added = sum(sizes.values())
    assert result["added_bytes"] == added and result["pools"]["detached"]["added_bytes"] == added
    for kind in ("retention.pruned", "retention.reclaimed"):
        events = [json.loads(e["data_json"]) for e in w.store.list_events("job-added") if e["kind"] == kind]
        assert [e["added_bytes"] for e in events if e] == [added], kind
    (listed,) = rarch.list_archives(w.root)
    assert listed["added_bytes"] == added
    monkeypatch.setenv("SUBFLEET_HOME", str(w.root))
    assert cli.main(["retention", "archives"]) == 0
    text = capsys.readouterr().out
    assert f"added {added:,} B" in text and f"{added:,} bytes the archives added" in text


def test_byte_copies_count_as_added(world, monkeypatch):
    """A volume without clones: every stored file is a byte copy, new space."""
    monkeypatch.setattr(rfs, "FORCE_COPY", True)
    w = world
    wt = w.job("job-copy")
    (wt / "notes.bin").write_bytes(os.urandom(30_000))
    result = run(w)
    assert result["pruned"] == ["job-copy"]
    totals = json.loads((w.root / "archive" / "job-copy" / "manifest.json").read_text())["totals"]
    assert totals["copies"] == totals["stored_files"] > 0 and totals["copied_bytes"] == totals["archived_bytes"]
    archive = w.root / "archive" / "job-copy"
    metadata = sum((archive / n).stat().st_size for n in rarch.ADDED if (archive / n).exists())
    assert result["added_bytes"] == metadata + totals["copied_bytes"]


def _remote_less(w: World, history: int = 150_000) -> None:
    """The source repository has no network remote (live: ~/chief-of-staff),
    with `history` bytes of incompressible history."""
    git(w.repo, "remote", "remove", "origin")
    (w.repo / "history.bin").write_bytes(os.urandom(history))
    git(w.repo, "add", ".")
    git(w.repo, "commit", "--quiet", "-m", "history")


def test_a_remote_less_repository_over_the_history_limit_keeps_its_job(world):
    """N1b (final review of e50716e8): with no network remote the bundle is the
    whole history, paid again by every job; over the policy's limit the job is
    kept as before retention by archive (`remote-less-history <size>`), before
    anything moves. Under it, or with a network remote, it retires."""
    w = world
    _remote_less(w)
    wt = w.job("job-big")
    before = snapshot(wt)
    measured = rgit.history_bytes(w.repo / ".git", [w.head()], [])
    assert measured > 150_000
    result = run(w, remote_less_history_bytes=100_000)
    assert result["pruned"] == []
    assert result["deferred"]["job-big"].startswith(f"remote-less-history: {measured} bytes"), result["deferred"]
    assert snapshot(wt) == before and w.store.get_job("job-big") and not (w.root / "retention" / "job-big").exists()
    assert not git(w.repo, "for-each-ref", "refs/subfleet-archive")               # nothing anchored either
    assert run(w, remote_less_history_bytes=0)["pruned"] == []                    # 0: every such job with history
    # The bundle also carries the anchor and a pack's own overhead.
    retired = run(w, remote_less_history_bytes=measured + 64 * 1024, state=retention.RetentionState())
    assert retired["pruned"] == ["job-big"], retired["deferred"]
    assert (w.root / "archive" / "job-big" / "commits.bundle").stat().st_size <= measured + 64 * 1024


def test_the_history_limit_is_for_repositories_without_a_network_remote(world):
    w = world
    (w.repo / "history.bin").write_bytes(os.urandom(150_000))
    git(w.repo, "add", ".")
    git(w.repo, "commit", "--quiet", "-m", "history")
    w.push()
    w.job("job-pushed")
    assert run(w, remote_less_history_bytes=0)["pruned"] == ["job-pushed"]


def test_a_bundle_over_the_limit_that_the_estimate_missed_keeps_the_job(world, monkeypatch):
    """The bundle decides: if git's measure said less than the bundle made,
    the bundle is dropped, the job put back, and its next check remembers."""
    w = world
    _remote_less(w)
    w.job("job-under")
    monkeypatch.setattr(rgit, "history_bytes", lambda *args, **kwargs: 1)
    clock = Clock()
    state = retention.RetentionState()
    first = run(w, remote_less_history_bytes=100_000, clock=clock, state=state)
    assert first["pruned"] == [] and first["deferred"]["job-under"].startswith("remote-less-history: a "), first
    journal = json.loads((w.root / "retention" / "job-under" / "journal.json").read_text())
    assert journal["state"] == "idle" and journal["history_bytes"] > 100_000
    assert not list((w.root / "retention" / "job-under" / "archive").glob("commits.bundle*"))
    clock.advance(rarch.DEFER_PERMANENT_S + 1)
    second = run(w, remote_less_history_bytes=100_000, clock=clock, state=state)
    assert second["deferred"]["job-under"].startswith(f"remote-less-history: {journal['history_bytes']} bytes")
    assert (w.root / "worktrees" / "job-under").is_dir()


def test_the_survey_keeps_what_the_history_limit_keeps(world):
    from subfleet.retention_survey import sample, survey
    w = world
    _remote_less(w)
    w.job("job-survey")
    (w.root / "policy.json").write_text(json.dumps({"retention": {"remote_less_history_bytes": 100_000}}))
    over = {"detached": (0, 0), "turn": (0, 0)}
    report = survey(w.root, holders=False, sample_throughput=False, budgets=over)
    assert report["kept"]["jobs_by_reason"] == {"remote-less-history": 1}, report["kept"]
    sampled = sample(w.root, 3)
    assert sampled["kept_in_sample"]["job-survey"].startswith("remote-less-history: ")
    (w.root / "policy.json").write_text(json.dumps({"retention": {"remote_less_history_bytes": 10 ** 9}}))
    report = survey(w.root, holders=False, sample_throughput=False, budgets=over)
    assert report["would_retire"]["jobs"] == 1
    assert report["would_retire"]["bundle_bytes_estimate"] > 150_000
    assert report["would_retire"]["added_bytes_estimate"] > report["would_retire"]["bundle_bytes_estimate"]


def test_a_job_whose_source_was_another_jobs_tree_root_is_not_kept_for_that(world):
    """Review of the N4 fix: a job submitted from another job's tree root
    registers its worktree in that tree's repository, outside the tree, so
    neither pins nor waits for it; with its own tree gone and the other job
    retiring, it retires too instead of being deferred every hour for ever."""
    w = world
    h_wt = w.job("job-h", created="2026-09-01T00:00:00Z")
    g_wt = w.root / "worktrees" / "job-g"
    git(h_wt, "worktree", "add", "--quiet", "--detach", str(g_wt), "HEAD")
    w.store.add_job(job_id="job-g", request_id="req-g", payload_digest="d", kind="dispatch", workdir=str(h_wt),
                    workdir_head=w.head(), worktree=str(g_wt), prompt_path="/p", sandbox="workspace-write",
                    state="succeeded", created_at="2026-09-02T00:00:00Z")
    (w.root / "jobs" / "job-g").mkdir()
    shutil.rmtree(g_wt)                                                     # its tree is gone
    assert retention.nested_hosts(w.store.list_jobs(), w.root.resolve()) == {}
    result = run(w)
    assert sorted(result["pruned"]) == ["job-g", "job-h"], result["deferred"]


def _host_gone(w: World) -> tuple[Path, Path]:
    a_wt, b_wt, _ = _nested_host(w)
    shutil.rmtree(a_wt)                                    # the host's tree, and its rows, gone for good
    for table in ("jobs",):
        w.store.connection.execute(f"DELETE FROM {table} WHERE job_id='job-a'")
    w.store.connection.commit()
    shutil.rmtree(w.root / "jobs" / "job-a")
    return a_wt, b_wt


def test_a_guest_whose_host_is_gone_for_good_is_kept_a_day_with_where_its_registration_went(world):
    """Review of the N4 fix: the reason names the gone tree and what brings
    the registration back, and the job waits a day, not an hour."""
    from subfleet.retention_survey import survey
    w = world
    a_wt, b_wt = _host_gone(w)
    clock = Clock()
    state = retention.RetentionState()
    result = run(w, clock=clock, state=state)
    reason = result["deferred"]["job-b"]
    assert reason.startswith("nested-host: its registration") and f"was inside {a_wt}, which is gone" in reason
    assert state.deferred["job-b"][0] - clock() == rarch.DEFER_PERMANENT_S
    assert b_wt.is_dir() and w.store.get_job("job-b")
    report = survey(w.root, holders=False, sample_throughput=False, budgets={"detached": (0, 0), "turn": (0, 0)})
    assert report["kept"]["jobs_by_reason"] == {"nested-host": 1}, report["kept"]


def test_a_host_that_is_there_without_the_registration_holds_nothing_up(world):
    """Review of the N4 fix: the host's tree is there but the guest's
    registration in it is gone (a prune): nothing will bring it back, so the
    guest retires, and the host after it."""
    w = world
    a_wt, b_wt, _ = _nested_host(w)
    shutil.rmtree(a_wt / "core" / ".git" / "worktrees" / "job-b")
    first = run(w)
    assert first["pruned"] == ["job-b"], first["deferred"]
    assert run(w)["pruned"] == ["job-a"]


def test_a_remote_that_holds_nothing_counts_as_none(world):
    """Review of N1b: a network remote added but never fetched has no
    remote-tracking ref, so the bundle is the whole history: the limit applies."""
    w = world
    _remote_less(w)
    git(w.repo, "remote", "add", "origin", "https://git.example.invalid/project.git")
    w.job("job-unfetched")
    result = run(w, remote_less_history_bytes=100_000)
    assert result["pruned"] == [] and result["deferred"]["job-unfetched"].startswith("remote-less-history: "), result


def test_a_remote_that_holds_only_unrelated_history_counts_as_none(world):
    """Review of the revision-4 build: a remote fetched only for an unrelated
    branch (`gh-pages`) holds none of the job's history."""
    w = world
    _remote_less(w)
    server = w.base / "pages.git"
    git(w.base, "init", "--quiet", "--bare", str(server))
    pages = w.base / "pages"
    git(w.base, "init", "--quiet", str(pages))
    (pages / "index.html").write_text("<p>docs</p>\n")
    git(pages, "add", ".")
    git(pages, "commit", "--quiet", "-m", "pages")
    git(pages, "push", "--quiet", str(server), "HEAD:refs/heads/gh-pages")
    git(w.repo, "remote", "add", "origin", "https://git.example.invalid/project.git")
    git(w.repo, "fetch", "--quiet", str(server), "+refs/heads/gh-pages:refs/remotes/origin/gh-pages")
    w.job("job-pages")
    result = run(w, remote_less_history_bytes=100_000)
    assert result["deferred"]["job-pages"].startswith("remote-less-history: "), result


def test_a_remote_whose_refs_are_from_long_ago_holds_none_of_a_new_baseline(world):
    """Confirmation review: a remote fetched once, long ago, reaches an old
    commit but not the one the job started from; the bundle then carries all
    the history since, again for every job, so the limit applies. A job whose
    baseline the remote holds carries only its own work and retires, however
    large that is."""
    w = world
    for n in range(3):                                      # unpushed history since the last fetch
        (w.repo / f"since-{n}.bin").write_bytes(os.urandom(60_000))
        git(w.repo, "add", ".")
        git(w.repo, "commit", "--quiet", "-m", f"since {n}")
    w.job("job-stale")
    result = run(w, remote_less_history_bytes=100_000)
    assert result["deferred"]["job-stale"].startswith("remote-less-history: "), result
    w.push()                                                # now the baseline is held
    wt = w.job("job-own-work")
    (wt / "big.bin").write_bytes(os.urandom(300_000))       # the job's own large work
    git(wt, "add", "big.bin")
    git(wt, "commit", "--quiet", "-m", "own work")
    second = run(w, remote_less_history_bytes=100_000, state=retention.RetentionState())
    assert sorted(second["pruned"]) == ["job-own-work", "job-stale"], second["deferred"]     # both baselines held now


def test_this_machine_by_any_of_its_names():
    import socket
    name = socket.gethostname()
    for host in ("localhost", "127.0.0.1", "127.1", "[::1]:22", "me@localhost", "0", "nas.local", name,
                 name.lower().removesuffix(".local") + ".local", "build.localhost"):
        assert rfs.this_machine(host), host
    for host in ("github.com", "git@github.com", "10.0.0.5", "files.pythonhosted.org"):
        assert not rfs.this_machine(host), host


def test_a_bundle_kept_from_an_earlier_attempt_is_checked_against_the_limit_now(world):
    """Final check of the review: a bundle an earlier attempt made and kept is
    held to the limit as it stands when it is used (lowered since, here)."""
    w = world
    _remote_less(w)
    w.job("job-reuse")
    measured = rgit.history_bytes(w.repo / ".git", [w.head()], [])
    checks = []

    def busy_at_second_check(watches, **_):
        checks.append(1)
        return {"job-reuse": ["pid 1 (python): open for writing"]} if len(checks) == 2 else {}

    clock = Clock()
    state = retention.RetentionState()
    first = run(w, holders=busy_at_second_check, clock=clock, state=state, remote_less_history_bytes=10 ** 9)
    assert first["pruned"] == [] and "busy" in first["deferred"]["job-reuse"], first["deferred"]
    assert (w.root / "retention" / "job-reuse" / "archive" / "commits.bundle").exists()
    clock.advance(rarch.DEFER_BUSY_S + 1)
    second = run(w, clock=clock, state=state, remote_less_history_bytes=measured + 1)
    assert second["pruned"] == [] and second["deferred"]["job-reuse"].startswith("remote-less-history: a "), second


def test_this_machine_by_its_short_name(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "mbp.lan")
    assert rfs.this_machine("mbp") and rfs.this_machine("git@mbp.lan") and rfs.this_machine("mbp.local")
    assert not rfs.this_machine("other.lan")


def test_the_survey_keeps_a_job_whose_tree_is_gone_as_the_pass_does(world):
    """Review of the revision-4 build: for a job whose tree is gone, the survey
    runs the pass's checks too (here the history limit)."""
    from subfleet.retention_survey import survey
    w = world
    _remote_less(w)
    wt = w.job("job-gone")
    shutil.rmtree(wt)
    (w.root / "policy.json").write_text(json.dumps({"retention": {"remote_less_history_bytes": 100_000}}))
    report = survey(w.root, holders=False, sample_throughput=False, budgets={"detached": (0, 0), "turn": (0, 0)})
    assert report["kept"]["jobs_by_reason"] == {"remote-less-history": 1}, report["kept"]
    assert run(w, remote_less_history_bytes=100_000)["deferred"]["job-gone"].startswith("remote-less-history")


def test_a_repositorys_history_is_measured_once_a_pass(world, monkeypatch):
    """Review of N1b: once one job of a repository is over the limit, the
    pass keeps its other jobs on that measure instead of measuring each."""
    w = world
    _remote_less(w)
    for n in range(3):
        wt = w.job(f"job-{n}", created=f"2026-09-01T00:0{n}:00Z")
        (wt / f"own-{n}.txt").write_text(f"job {n}'s own commit\n")      # each its own HEAD
        git(wt, "add", ".")
        git(wt, "commit", "--quiet", "-m", f"job {n}")
    calls = []
    real = rgit.history_bytes

    def counting(*args, **kwargs):
        calls.append(args[1])
        return real(*args, **kwargs)

    monkeypatch.setattr(rgit, "history_bytes", counting)
    result = run(w, remote_less_history_bytes=100_000)
    assert sorted(result["deferred"]) == ["job-0", "job-1", "job-2"]
    assert all(r.startswith("remote-less-history: ") for r in result["deferred"].values())
    assert len(calls) == 1, calls


def test_the_survey_remembers_a_bundle_that_came_out_over_the_limit(world):
    """Review of N1b: the survey reads the size an earlier attempt's bundle
    had (kept in its idle journal), as the pass does."""
    from subfleet.retention_survey import survey
    w = world
    _remote_less(w)
    w.job("job-edge")
    measured = rgit.history_bytes(w.repo / ".git", [w.head()], [])
    first = run(w, remote_less_history_bytes=measured + 1)            # the bundle carries a little more
    assert first["deferred"]["job-edge"].startswith("remote-less-history: a "), first["deferred"]
    (w.root / "policy.json").write_text(json.dumps({"retention": {"remote_less_history_bytes": measured + 1}}))
    report = survey(w.root, holders=False, sample_throughput=False, budgets={"detached": (0, 0), "turn": (0, 0)})
    assert report["kept"]["jobs_by_reason"] == {"remote-less-history": 1}, report["kept"]
