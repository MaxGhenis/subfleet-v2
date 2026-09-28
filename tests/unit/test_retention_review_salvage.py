"""Independent retention review probes, with regressions asserting safe outcomes."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import retention
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.daemon import Daemon
from subfleet.retention_salvage import SalvageReachability
from subfleet.salvage import salvage
from subfleet.store import Store

MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def isolated_git(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("githome")
    (home / "gitconfig").write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "probe")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "probe@example.com")


def git(repository, *args, check=True):
    result = subprocess.run(["git", "-C", str(repository), *args], capture_output=True, text=True)
    if check and result.returncode:
        raise AssertionError(f"git {args} failed: {result.stderr}")
    return result.stdout.strip()


def reachable_from_any_ref(repository, commit):
    return git(repository, "for-each-ref", "--contains", commit, "--format=%(refname)")


@dataclasses.dataclass
class World:
    tmp: Path
    repository: Path
    root: Path
    store: Store


@pytest.fixture
def world(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "feature")
    (repository / "tracked").write_text("base")
    (repository / ".gitignore").write_text(".env\nout/\n")
    git(repository, "add", "tracked", ".gitignore")
    git(repository, "commit", "-m", "base")
    root = tmp_path / "state"
    (root / "worktrees").mkdir(parents=True)
    (root / "jobs").mkdir()
    with Store(root / "state.sqlite3") as store:
        store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/home/one", "home"),
                            "/home/one", LaneOwner.V2, False))
        yield World(tmp_path, repository, root, store)


def add_worktree_job(w: World, job_id: str, *, created: str = "2026-01-01T00:00:00Z",
                     prepare=None, with_salvage=True, in_place=False):
    """A finished workspace-write job whose worktree was cut detached, as `_workspace` does."""
    if in_place:
        worktree_path = None
        place = w.repository
    else:
        worktree_path = w.root / "worktrees" / job_id
        git(w.repository, "worktree", "add", "--detach", str(worktree_path), "HEAD")
        place = worktree_path
    baseline = git(place, "rev-parse", "HEAD")
    if prepare is not None:
        prepare(place)
    saved = salvage(place, baseline, 1, timestamp=created) if with_salvage else None
    w.store.add_job(job_id=job_id, request_id=job_id, payload_digest="digest", kind="dispatch",
                    workdir=str(w.repository), worktree=str(worktree_path) if worktree_path else None,
                    in_place=1 if in_place else 0, prompt_path="/prompt", sandbox="workspace-write",
                    state="succeeded", created_at=created, finished_at=created, workdir_head=baseline)
    w.store.add_attempt(attempt_id=f"{job_id}/a1", job_id=job_id, seq=1, lane_id="codex-1",
                        model_requested="gpt-6-astra", state="succeeded")
    attempt = w.root / "jobs" / job_id / "a1"
    attempt.mkdir(parents=True)
    (attempt / "stdout").write_text("log")
    if saved is not None:
        w.store.add_artifact(f"{job_id}/a1", "salvage", saved.ref,
                             hashlib.sha256(saved.commit.encode()).hexdigest(), 0)
        (attempt / "salvage.json").write_text(json.dumps({"result": dataclasses.asdict(saved),
                                                          "checkpoint": git(place, "rev-parse", "HEAD")}))
    return worktree_path, baseline, saved


def run_daemon_retention(w: World, jobs=0):
    """The production wiring (Daemon._retention), as tests/unit/test_retention_salvage.py does."""
    service = SimpleNamespace(store=w.store, root=w.root, policy={"retention": {"jobs": jobs}},
                              conversations=SimpleNamespace(retention_pins=lambda: set()),
                              timers=SimpleNamespace(cancel=threading.Event(), mark=lambda *a, **k: None))
    Daemon._retention(service)


# ---------------------------------------------------------------------------
# 1. Salvage proof and what the removed worktree still held
# ---------------------------------------------------------------------------

def test_job_commits_on_detached_head_remain_reachable(world):
    """The salvage snapshot preserves the detached job's committed history."""
    def job_commits(worktree):
        (worktree / "a").write_text("step one")
        git(worktree, "add", "a")
        git(worktree, "commit", "-m", "job step 1: important rationale")
        (worktree / "b").write_text("step two")
        git(worktree, "add", "b")
        git(worktree, "commit", "-m", "job step 2")

    worktree, baseline, saved = add_worktree_job(world, "job", prepare=job_commits)
    job_head = git(worktree, "rev-parse", "HEAD")
    assert git(worktree, "status", "--porcelain") == ""
    assert saved.ref in reachable_from_any_ref(world.repository, job_head)
    run_daemon_retention(world)
    assert world.store.get_job("job") is None                  # pruned
    assert not worktree.exists()
    assert git(world.repository, "rev-parse", saved.ref + "^") == baseline
    assert git(world.repository, "rev-parse", saved.ref + "^2") == job_head
    assert "job step 1" in git(world.repository, "log", "--all", "--format=%s")
    unreachable = git(world.repository, "fsck", "--unreachable", "--no-reflogs")
    assert job_head not in unreachable


def test_ignored_files_in_salvage_bearing_worktree_are_preserved(world):
    """Ignored non-cache output is not in salvage and therefore pins its job."""
    def work(worktree):
        (worktree / "tracked").write_text("changed")
        (worktree / ".env").write_text("TOKEN_FOR_THIS_EXPERIMENT=1")
        (worktree / "out").mkdir()
        (worktree / "out" / "results.csv").write_text("hours of computed output")

    worktree, _, saved = add_worktree_job(world, "job", prepare=work)
    assert git(world.repository, "ls-tree", "-r", "--name-only", saved.ref).split() == [".gitignore", "tracked"]
    run_daemon_retention(world)
    assert world.store.get_job("job") is not None
    assert (worktree / "out" / "results.csv").read_text() == "hours of computed output"
    assert (worktree / ".env").read_text() == "TOKEN_FOR_THIS_EXPERIMENT=1"
    assert any("ignored" in json.dumps(event) for event in world.store.list_events("job"))


def test_nested_repository_in_worktree_is_preserved(world):
    """add -A records a nested repository as a gitlink, so the exact-tree check passes."""
    def work(worktree):
        (worktree / "tracked").write_text("changed")
        nested = worktree / "vendor-clone"
        nested.mkdir()
        git(nested, "init", "-b", "patch")
        (nested / "fix.py").write_text("unpushed fix")
        git(nested, "add", "fix.py")
        git(nested, "commit", "-m", "unpushed nested commit")
        (nested / "wip.py").write_text("uncommitted nested work")

    worktree, _, saved = add_worktree_job(world, "job", prepare=work)
    entries = git(world.repository, "ls-tree", saved.ref)
    assert "160000 commit" in entries                           # only a gitlink was "preserved"
    run_daemon_retention(world)
    assert world.store.get_job("job") is not None
    assert (worktree / "vendor-clone" / "wip.py").read_text() == "uncommitted nested work"
    assert git(worktree / "vendor-clone", "log", "-1", "--format=%s") == "unpushed nested commit"


@pytest.mark.parametrize("holder", ["stash", "remote-tracking", "prefetch", "original"])
def test_fallback_rejects_refs_that_git_prunes(world, holder):
    """Transient Git refs never satisfy durable salvage reachability."""
    worktree, _, saved = add_worktree_job(world, "job", prepare=lambda wt: (wt / "tracked").write_text("changed"))
    repository = world.repository
    if holder == "stash":
        git(repository, "switch", "--detach", saved.commit)
        (repository / "experiment").write_text("x")
        git(repository, "stash", "push", "-u", "-m", "try on top of salvage")
        git(repository, "switch", "feature")
    elif holder == "remote-tracking":
        origin = world.tmp / "origin.git"
        git(world.tmp, "init", "--bare", str(origin))
        git(repository, "remote", "add", "origin", str(origin))
        git(repository, "push", "origin", f"{saved.commit}:refs/heads/keep")
        git(repository, "fetch", "origin")
    elif holder == "prefetch":   # written and replaced by `git maintenance run --task=prefetch`
        git(repository, "update-ref", "refs/prefetch/remotes/origin/keep", saved.commit)
    else:                        # filter-branch backups, routinely deleted
        git(repository, "update-ref", "refs/original/refs/heads/keep", saved.commit)
    git(repository, "update-ref", "-d", saved.ref)
    assert not SalvageReachability(world.store, world.root)(world.store.list_artifacts("job/a1")[0])

    if holder == "stash":
        git(repository, "stash", "drop")
        assert reachable_from_any_ref(repository, saved.commit) == ""
    elif holder == "remote-tracking":
        git(origin, "update-ref", "-d", "refs/heads/keep")
        git(repository, "fetch", "--prune", "origin")
        assert reachable_from_any_ref(repository, saved.commit) == ""


def test_ok_reflog_only_and_detached_head_are_not_proof(world):
    worktree, _, saved = add_worktree_job(world, "job", prepare=lambda wt: (wt / "tracked").write_text("changed"))
    git(world.repository, "config", "core.logAllRefUpdates", "always")
    proof = SalvageReachability(world.store, world.root)
    artifact = world.store.list_artifacts("job/a1")[0]
    git(world.repository, "update-ref", saved.ref, git(world.repository, "rev-parse", "HEAD"))  # moved; old in reflog
    assert git(world.repository, "rev-parse", saved.ref + "@{1}") == saved.commit
    assert not proof(artifact)
    git(world.repository, "update-ref", "-d", saved.ref)
    git(world.repository, "switch", "--detach", saved.commit)                                  # main HEAD holds it
    assert not proof(artifact)


def test_ok_packed_refs_and_shallow_repository(world, tmp_path):
    worktree, _, saved = add_worktree_job(world, "job", prepare=lambda wt: (wt / "tracked").write_text("changed"))
    git(world.repository, "pack-refs", "--all")
    assert not (world.repository / ".git" / Path(saved.ref)).exists()
    assert SalvageReachability(world.store, world.root)(world.store.list_artifacts("job/a1")[0])

    # A shallow caller repository: the proof never walks past the shallow boundary.
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "--depth", "1", f"file://{world.repository}", str(shallow)],
                   capture_output=True, check=True)
    shallow_world = World(tmp_path, shallow, world.root, world.store)
    worktree2, _, saved2 = add_worktree_job(shallow_world, "shallow-job",
                                            prepare=lambda wt: (wt / "tracked").write_text("shallow change"))
    assert (shallow / ".git" / "shallow").exists()
    artifact = world.store.list_artifacts("shallow-job/a1")[0]
    assert SalvageReachability(world.store, world.root)(artifact)
    git(shallow, "update-ref", "-d", saved2.ref)
    assert not SalvageReachability(world.store, world.root)(artifact)


def test_ok_moved_or_removed_repository_stays_pinned(world, tmp_path):
    worktree, _, saved = add_worktree_job(world, "job", prepare=lambda wt: (wt / "tracked").write_text("changed"))
    world.repository.rename(tmp_path / "moved")
    artifact = world.store.list_artifacts("job/a1")[0]
    assert not SalvageReachability(world.store, world.root)(artifact)
    run_daemon_retention(world)
    assert world.store.get_job("job") is not None
    assert worktree.exists()


def test_ok_ref_deleted_between_proof_and_removal_keeps_dirty_worktree(world, monkeypatch):
    worktree, _, saved = add_worktree_job(world, "job", prepare=lambda wt: (wt / "tracked").write_text("changed"))
    original = SalvageReachability.__call__

    def prove_then_delete(self, artifact):
        result = original(self, artifact)
        git(world.repository, "update-ref", "-d", saved.ref)
        return result

    monkeypatch.setattr(SalvageReachability, "__call__", prove_then_delete)
    run_daemon_retention(world)
    assert world.store.get_job("job") is not None
    assert (worktree / "tracked").read_text() == "changed"


def test_ok_edit_after_salvage_keeps_worktree(world):
    worktree, _, _ = add_worktree_job(world, "job", prepare=lambda wt: (wt / "tracked").write_text("changed"))
    (worktree / "later.txt").write_text("a person kept working here after the job")
    run_daemon_retention(world)
    assert world.store.get_job("job") is not None
    assert (worktree / "later.txt").exists()


def test_ok_in_place_salvage_prunes_only_the_job_directory(world):
    _, _, saved = add_worktree_job(world, "job", in_place=True,
                                   prepare=lambda place: (place / "tracked").write_text("in place change"))
    run_daemon_retention(world)
    assert world.store.get_job("job") is None
    assert (world.repository / "tracked").read_text() == "in place change"
    assert git(world.repository, "show", saved.ref + ":tracked") == "in place change"


# ---------------------------------------------------------------------------
# 4. Interrupted and resumed passes
# ---------------------------------------------------------------------------

def test_killed_legacy_worktree_remove_is_finished_without_pressure(world):
    """A retention lease authorizes recovery of the old remover's partial tree."""
    worktree, _, _ = add_worktree_job(world, "job", with_salvage=False)
    (worktree / "tracked").unlink()  # old `git worktree remove` died mid-removal
    with world.store.transaction() as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                   (f"worktree:{worktree}", "retention:job", "2026-01-01T00:00:00Z"))
    result = retention.maintenance(world.store, world.root, max_jobs=10)
    assert result["pruned"] == ["job"]
    assert world.store.get_job("job") is None
    assert not worktree.exists()
    assert not world.store.query("SELECT * FROM leases WHERE holder='retention:job'")
    assert str(worktree) not in git(world.repository, "worktree", "list", "--porcelain")


@pytest.mark.parametrize("stage", ["after-worktree-rename", "after-job-rename"])
def test_interruption_after_destructive_step_commits_row_and_releases_lease(world, monkeypatch, stage):
    worktree, _, _ = add_worktree_job(world, "job", with_salvage=False)
    cancel = threading.Event()
    original_rename = Path.rename
    directory = world.root / "jobs" / "job"
    target = worktree if stage == "after-worktree-rename" else directory

    def rename(path, *args, **kwargs):
        out = original_rename(path, *args, **kwargs)
        if path == target:
            cancel.set()
        return out

    monkeypatch.setattr(Path, "rename", rename)
    result = retention.maintenance(world.store, world.root, max_jobs=0, cancel=cancel)
    assert result["interrupted"] == "cancelled"
    assert result["pruned"] == ["job"]
    assert not worktree.exists()
    assert not directory.exists()
    assert world.store.get_job("job") is None
    assert not world.store.query("SELECT * FROM leases WHERE holder='retention:job'")
    assert [e for e in world.store.list_events("job") if e["kind"] == "retention.pruned"]

    monkeypatch.setattr(Path, "rename", original_rename)
    later = retention.maintenance(world.store, world.root, max_jobs=10)
    assert later["pruned"] == []
    assert not (world.root / "trash" / "job").exists()


# ---------------------------------------------------------------------------
# 3. Size cache
# ---------------------------------------------------------------------------

def add_sized_job(store, root, identity, order, size):
    store.add_job(job_id=identity, request_id=identity, payload_digest="digest", kind="dispatch",
                  workdir=str(root), prompt_path="/prompt", sandbox="read-only", state="succeeded",
                  created_at=f"2026-01-01T00:00:{order:02d}Z", finished_at="2026-01-02T00:00:00Z")
    directory = root / "jobs" / identity
    directory.mkdir(parents=True)
    with (directory / "payload.bin").open("wb") as stream:
        stream.truncate(size)
    return directory


def test_cached_size_matches_a_fresh_pass_after_files_are_removed(tmp_path):
    """The cache changes how much is pruned, not only which job goes first."""
    root = tmp_path / "state"
    root.mkdir()
    with Store(root / "state.sqlite3") as store:
        add_sized_job(store, root, "j1", 1, 1 * MIB)
        add_sized_job(store, root, "j2", 2, 1 * MIB)
        big = add_sized_job(store, root, "j3", 3, 2 * MIB)
        first = retention.maintenance(store, root, max_bytes=4 * MIB)
        assert first["pruned"] == [] and first["bytes_after"] == 4 * MIB
        (big / "payload.bin").unlink()          # e.g. an operator frees space by hand
        add_sized_job(store, root, "j4", 4, 1 * MIB)
        store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        fresh_root = tmp_path / "fresh"
        shutil.copytree(root, fresh_root)
        with Store(fresh_root / "state.sqlite3") as fresh:
            assert retention.maintenance(fresh, fresh_root, max_bytes=4 * MIB)["pruned"] == []
        cached = retention.maintenance(store, root, max_bytes=4 * MIB)
        assert cached["pruned"] == []
        assert cached["bytes_after"] == 3 * MIB


# ---------------------------------------------------------------------------
# 5. Fairness: a deferred older candidate and the "newest N" order
# ---------------------------------------------------------------------------

def test_transient_proof_failure_demotion_expires_after_cooldown(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    with Store(root / "state.sqlite3") as store:
        store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/home/one", "home"),
                            "/home/one", LaneOwner.V2, False))
        for order, identity in enumerate(["old", "mid", "new"]):
            add_sized_job(store, root, identity, order, 1024)
        store.add_attempt(attempt_id="old/a1", job_id="old", seq=1, lane_id="codex-1",
                          model_requested="gpt-6-astra", state="succeeded")
        store.add_artifact("old/a1", "salvage", "refs/subfleet-salvage/old", "digest", 0)
        calls = []

        def proof(artifact):
            calls.append(artifact["attempt_id"])
            return len(calls) > 1                # one transient Git timeout, then provable

        first = retention.maintenance(store, root, max_jobs=2, salvage_referenced_elsewhere=proof)
        assert first["pruned"] == ["mid"]        # correct: "old" was unprovable this pass
        add_sized_job(store, root, "newest", 9, 1024)
        later = time.monotonic() + retention._RETRY_COOLDOWN + 1
        monkeypatch.setattr(retention.time, "monotonic", lambda: later)
        second = retention.maintenance(store, root, max_jobs=2, salvage_referenced_elsewhere=proof)
        assert second["pruned"] == ["old"]
        assert len(calls) >= 2 and set(calls) == {"old/a1"}
        assert {job["job_id"] for job in store.list_jobs()} == {"new", "newest"}


# ---------------------------------------------------------------------------
# 2. Pins, including ones that appear mid-pass
# ---------------------------------------------------------------------------

def test_ok_every_pin_holds_and_midpass_pins_are_rechecked(world, monkeypatch):
    store, root = world.store, world.root
    ids = ["queued", "running", "quarantined", "leased-job", "leased-attempt", "turn-kept",
           "conversation", "gate-evidence", "gate-review", "notice", "parent", "child",
           "notice-during-proof", "free"]
    for order, identity in enumerate(ids):
        state = {"queued": "queued", "running": "running"}.get(identity, "succeeded")
        kind = {"turn-kept": "turn", "gate-review": "gate-review"}.get(identity, "dispatch")
        store.add_job(job_id=identity, request_id=identity, payload_digest="digest", kind=kind,
                      workdir=str(root), prompt_path="/prompt", sandbox="read-only", state=state,
                      created_at=f"2026-01-01T00:00:{order:02d}Z",
                      finished_at="2099-01-01T00:00:00Z" if identity == "turn-kept" else "2026-01-02T00:00:00Z",
                      parent_job_id="parent" if identity == "child" else None)
        (root / "jobs" / identity).mkdir(parents=True, exist_ok=True)
        store.add_attempt(attempt_id=f"{identity}/a1", job_id=identity, seq=1, lane_id="codex-1",
                          model_requested="gpt-6-astra",
                          state="quarantined" if identity == "quarantined" else "succeeded")
    with store.transaction() as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES('out:/x','leased-job','t')")
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES('lane:x:slot:0','leased-attempt/a1','t')")
        tx.execute("INSERT INTO actions(action_id,kind,op_key,subject,state,request_json,result_json,created_at,updated_at) "
                   "VALUES('a','gate.merge','k','pr','confirmed',?, 'null','t','t')",
                   (json.dumps({"evidence": ["gate-evidence/a1"]}),))
    store.add_notice("notice", "done", session_id="session-1")
    store.add_artifact("notice-during-proof/a1", "salvage", "refs/subfleet-salvage/x", "d", 0)

    def proof(artifact):
        # A notice addressed to a session appears while the Git proof runs.
        store.add_notice("notice-during-proof", "late", session_id="session-2")
        return True

    result = retention.maintenance(store, root, max_jobs=0, turn_max_jobs=0, turn_keep_s=86400,
                                   pins=lambda: {"conversation"}, salvage_referenced_elsewhere=proof)
    assert result["pruned"] == ["child", "free"]
    remaining = {job["job_id"] for job in store.list_jobs()}
    assert remaining == set(ids) - {"child", "free"}


# ---------------------------------------------------------------------------
# Pre-existing liveness: missing directories wedge a job (live: 20260925-130918-ar-9612-review)
# ---------------------------------------------------------------------------

def test_ok_hand_deleted_worktree_is_pruned_when_the_repository_remains(world):
    worktree, _, _ = add_worktree_job(world, "job", with_salvage=False)
    shutil.rmtree(worktree)                      # an operator frees space by hand
    result = retention.maintenance(world.store, world.root, max_jobs=0)
    assert result["pruned"] == ["job"]


def test_job_whose_worktree_and_workdir_are_gone_is_pruned(world, monkeypatch):
    """Live: 20260925-130918-ar-9612-review errs every pass (pre-existing)."""
    worktree, _, _ = add_worktree_job(world, "job", with_salvage=False)
    shutil.rmtree(worktree)
    shutil.rmtree(world.repository)
    monkeypatch.setattr(retention, "_git", lambda *args, **kwargs: pytest.fail("missing tree needs no Git"))
    result = retention.maintenance(world.store, world.root, max_jobs=0)
    assert result["pruned"] == ["job"]
    assert result["errors"] == []
    assert world.store.get_job("job") is None
    assert not (world.root / "jobs" / "job").exists()
