"""C-8.4: daemon retention frees worktrees only after proving salvage survives."""

import dataclasses
import hashlib
import json
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from subfleet import retention, retention_salvage
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.daemon import Daemon
from subfleet.retention_salvage import SalvageReachability
from subfleet.salvage import salvage
from subfleet.store import Store


def git(repository, *args):
    return subprocess.run(["git", "-C", str(repository), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture
def snapshot(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "feature")
    (repository / "tracked").write_text("base")
    git(repository, "add", "tracked")
    git(repository, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base")
    root = tmp_path / "state"
    root.mkdir()
    worktree = root / "worktrees" / "job"
    git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "tracked").write_text("preserved snapshot")
    saved = salvage(worktree, git(worktree, "rev-parse", "HEAD"), 1, timestamp="2026-09-05T12:00:00Z")
    with Store(root / "state.sqlite3") as store:
        store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/home/one", "home"),
                            "/home/one", LaneOwner.V2, False))
        store.add_job(job_id="job", request_id="request", payload_digest="digest", kind="dispatch",
                      workdir=str(repository), worktree=str(worktree), prompt_path="/prompt",
                      sandbox="workspace-write", state="succeeded")
        store.add_attempt(attempt_id="job/a1", job_id="job", seq=1, lane_id="codex-1",
                          model_requested="gpt-6-astra", state="succeeded")
        store.add_artifact("job/a1", "salvage", saved.ref, hashlib.sha256(saved.commit.encode()).hexdigest(), 0)
        attempt = root / "jobs" / "job" / "a1"
        attempt.mkdir(parents=True)
        (attempt / "salvage.json").write_text(json.dumps({"result": dataclasses.asdict(saved)}))
        yield store, root, repository, worktree, saved


def run_daemon_retention(store, root):
    # Exercise the production wiring without constructing or starting a daemon.
    service = SimpleNamespace(store=store, root=root, policy={"retention": {"jobs": 0}},
                              conversations=SimpleNamespace(retention_pins=lambda: set()),
                              timers=SimpleNamespace(cancel=threading.Event(), mark=lambda *args, **kwargs: None))
    Daemon._retention(service)


def test_daemon_prunes_shared_salvage_and_keeps_its_commit(snapshot, monkeypatch):
    """A named common salvage ref alone survives removal of its dirty worktree."""
    store, root, repository, worktree, saved = snapshot
    original = subprocess.run

    def outside_transaction(*args, **kwargs):
        assert not store.connection.in_transaction
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", outside_transaction)
    run_daemon_retention(store, root)
    assert store.get_job("job") is None, store.list_events("job")
    assert not worktree.exists()
    assert git(repository, "show", saved.ref + ":tracked") == "preserved snapshot"


@pytest.mark.parametrize("held_ref", ["refs/heads/retained", "refs/subfleet/retained"])
def test_daemon_accepts_other_named_ref_but_keeps_unreachable_salvage(snapshot, held_ref):
    """An object or detached HEAD is not proof; a named descendant ref is."""
    store, root, repository, worktree, saved = snapshot
    git(worktree, "reset", "--hard", saved.commit)
    git(repository, "update-ref", "-d", saved.ref)
    run_daemon_retention(store, root)
    assert store.get_job("job") is not None
    assert worktree.exists()
    assert git(repository, "cat-file", "-t", saved.commit) == "commit"

    (worktree / "next").write_text("descendant")
    git(worktree, "add", "next")
    git(worktree, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "descendant")
    git(repository, "update-ref", held_ref, git(worktree, "rev-parse", "HEAD"))
    run_daemon_retention(store, root)
    assert store.get_job("job") is None, store.list_events("job")
    assert not worktree.exists()
    assert git(repository, "show", held_ref + "^:tracked") == "preserved snapshot"


@pytest.mark.parametrize("damage", ["digest", "moved-ref", "receipt-digest", "receipt-fifo", "receipt-oversize", "receipt-escape"])
def test_unknown_or_damaged_salvage_stays_pinned(snapshot, damage):
    store, root, repository, worktree, saved = snapshot
    artifact = store.list_artifacts("job/a1")[0]
    if damage == "digest":
        artifact = {**artifact, "sha256": "0" * 64}
    elif damage == "moved-ref":
        git(repository, "update-ref", saved.ref, git(repository, "rev-parse", "HEAD"))
    else:
        git(repository, "update-ref", "refs/heads/retained", saved.commit)
        git(repository, "update-ref", "-d", saved.ref)
        receipt = root / "jobs" / "job" / "a1" / "salvage.json"
        if damage == "receipt-digest":
            receipt.write_text(json.dumps({"result": {"ref": saved.ref, "commit": "0" * 40}}))
        elif damage == "receipt-fifo":
            import os
            receipt.unlink()
            os.mkfifo(receipt)
        elif damage == "receipt-oversize":
            receipt.write_text(" " * (64 * 1024 + 1))
        else:
            external = root / "external-receipt.json"
            receipt.rename(external)
            receipt.symlink_to(external)
    assert not SalvageReachability(store, root)(artifact)
    assert worktree.exists()


def test_proof_is_fresh_and_rejects_per_worktree_refs(snapshot):
    store, root, repository, _, saved = snapshot
    artifact = store.list_artifacts("job/a1")[0]
    proof = SalvageReachability(store, root)
    assert proof(artifact)
    git(repository, "update-ref", "-d", saved.ref)
    git(repository, "update-ref", "refs/worktree/local", saved.commit)
    assert not proof(artifact)


def test_repository_inside_removed_worktree_is_not_a_durable_copy(snapshot):
    store, root, repository, worktree, _ = snapshot
    git(repository, "worktree", "remove", "--force", str(worktree))
    repository.rename(worktree)
    store.update_job("job", workdir=str(worktree))
    assert not SalvageReachability(store, root)(store.list_artifacts("job/a1")[0])


def test_git_failure_is_not_reachability(snapshot, monkeypatch):
    store, root, _, _, _ = snapshot

    def failed(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", .001)

    monkeypatch.setattr(retention_salvage, "_git", failed)
    assert not SalvageReachability(store, root)(store.list_artifacts("job/a1")[0])


@pytest.mark.parametrize("reason", ["cancelled", "deadline"])
def test_salvage_proof_observes_pass_interruption(snapshot, monkeypatch, reason):
    store, root, _, _, _ = snapshot
    cancel = threading.Event()
    if reason == "cancelled":
        cancel.set()
    deadline = time.monotonic() - 1 if reason == "deadline" else None
    monkeypatch.setattr(retention_salvage, "_git", lambda *args, **kwargs: pytest.fail("interrupted proof ran Git"))
    with pytest.raises(retention._Interrupted, match=reason):
        SalvageReachability(store, root, cancel=cancel, deadline=deadline)(store.list_artifacts("job/a1")[0])
