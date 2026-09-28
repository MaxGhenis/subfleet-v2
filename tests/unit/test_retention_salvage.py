"""C-8.4: daemon retention frees worktrees only after proving salvage survives."""

import dataclasses
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import retention, retention_salvage
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.daemon import Daemon
from subfleet.retention_salvage import SalvageReachability, prove_worktree_preserved
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
        store.update_job("job", workdir_head=git(repository, "rev-parse", "HEAD"))
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
    """A named common salvage ref survives removal of its exact clean checkout."""
    store, root, repository, worktree, saved = snapshot
    git(worktree, "reset", "--hard", saved.commit)
    original = subprocess.run

    def outside_transaction(*args, **kwargs):
        assert not store.connection.in_transaction
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", outside_transaction)
    run_daemon_retention(store, root)
    assert store.get_job("job") is None, store.list_events("job")
    assert not worktree.exists()
    assert git(repository, "show", saved.ref + ":tracked") == "preserved snapshot"


@pytest.mark.parametrize("held_ref", ["refs/heads/retained", "refs/tags/retained"])
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
    git(worktree, "reset", "--hard", saved.commit)
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


@pytest.mark.parametrize("held_ref", ["refs/stash", "refs/remotes/origin/retained", "refs/subfleet/retained",
                                     "refs/prefetch/retained", "refs/original/retained"])
def test_git_prunable_namespaces_cannot_preserve_salvage(snapshot, held_ref):
    store, root, repository, _, saved = snapshot
    git(repository, "update-ref", held_ref, saved.commit)
    git(repository, "update-ref", "-d", saved.ref)
    assert not SalvageReachability(store, root)(store.list_artifacts("job/a1")[0])


def test_worktree_safety_rejects_unrecorded_head_even_when_named_ref_holds_it(snapshot):
    store, root, repository, worktree, _ = snapshot
    git(worktree, "add", "tracked")
    git(worktree, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "detached progress")
    job = store.get_job("job")
    with pytest.raises(ValueError, match="recorded baseline"):
        prove_worktree_preserved(job, root, [])
    git(repository, "update-ref", "refs/heads/retained", git(worktree, "rev-parse", "HEAD"))
    with pytest.raises(ValueError, match="recorded baseline"):
        prove_worktree_preserved(job, root, [])


def test_replacement_object_cannot_disguise_unpreserved_content(snapshot):
    """C-8.4 allowed HEAD history must preserve the real tree, ignoring refs/replace."""
    store, root, repository, worktree, _ = snapshot
    baseline = git(worktree, "rev-parse", "HEAD")
    (worktree / "tracked").write_text("replacement-only output")
    git(worktree, "add", "tracked")
    git(worktree, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "replacement")
    replacement = git(worktree, "rev-parse", "HEAD")
    git(worktree, "reset", "--soft", baseline)
    git(repository, "replace", baseline, replacement)
    result = retention.maintenance(store, root, max_jobs=0,
                                   salvage_referenced_elsewhere=SalvageReachability(store, root))
    assert result["pruned"] == [] and result["protected"] == ["job"]
    assert (worktree / "tracked").read_text() == "replacement-only output"


@pytest.mark.parametrize("ignored", ["evidence/result.json", " node_modules/result.json",
                                    "build", ".venv", "artifact\nname.txt"])
def test_worktree_safety_rejects_noncache_ignored_entries(snapshot, ignored):
    store, root, repository, worktree, _ = snapshot
    (repository / ".git" / "info" / "exclude").write_text("*\n")
    path = worktree / ignored
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("irreplaceable output")
    with pytest.raises(ValueError, match="ignored worktree entry"):
        prove_worktree_preserved(store.get_job("job"), root, [])
    assert path.read_text() == "irreplaceable output"


@pytest.mark.parametrize("ignored", ["__pycache__/a.pyc", ".pytest_cache/README.md",
                                    ".mypy_cache/result", ".ruff_cache/result",
                                    ".hypothesis/result", ".DS_Store"])
def test_worktree_safety_accepts_explicit_regenerable_caches(snapshot, ignored):
    store, root, repository, worktree, _ = snapshot
    git(worktree, "reset", "--hard", store.get_job("job")["workdir_head"])
    (repository / ".git" / "info" / "exclude").write_text("*\n")
    path = worktree / ignored
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("regenerable")
    prove_worktree_preserved(store.get_job("job"), root, [])


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_nested_git_metadata_inside_allowed_cache_keeps_worktree(snapshot, kind):
    store, root, repository, worktree, _ = snapshot
    (repository / ".git" / "info" / "exclude").write_text("__pycache__/\n")
    nested = worktree / "__pycache__" / "pkg" / ".git"
    nested.parent.mkdir(parents=True)
    if kind == "directory":
        nested.mkdir()
    elif kind == "symlink":
        nested.symlink_to(repository / ".git", target_is_directory=True)
    else:
        nested.write_text("gitdir: elsewhere")
    with pytest.raises(ValueError, match="nested Git metadata"):
        prove_worktree_preserved(store.get_job("job"), root, [])


def test_missing_worktree_safety_makes_no_git_calls(snapshot, monkeypatch):
    store, root, repository, worktree, _ = snapshot
    git(repository, "worktree", "remove", "--force", str(worktree))
    monkeypatch.setattr(retention_salvage, "_git", lambda *a, **kw: pytest.fail("missing tree called Git"))
    prove_worktree_preserved(store.get_job("job"), root, [])


@pytest.mark.parametrize("changed", [False, True])
def test_nested_git_scan_resumes_but_revalidates_prior_directories(tmp_path, monkeypatch, changed):
    worktree = tmp_path / "worktree"
    (worktree / "cache" / "package").mkdir(parents=True)
    original = retention_salvage._checkpoint
    checkpoints = 0

    def interrupt_after_first_directory(cancel, deadline):
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 2:
            raise retention._Interrupted("deadline")

    monkeypatch.setattr(retention_salvage, "_checkpoint", interrupt_after_first_directory)
    progress = []
    with pytest.raises(retention._Interrupted):
        retention_salvage._prove_no_nested_git(worktree, None, None,
                                              on_progress=lambda: progress.append(True))
    assert progress == [True]
    scan = retention_salvage._NESTED_GIT_SCANS[worktree]
    assert [path for path, _ in scan.inspected] == [worktree]
    if changed:
        (worktree / "new-repository" / ".git").mkdir(parents=True)
    monkeypatch.setattr(retention_salvage, "_checkpoint", original)
    if changed:
        with pytest.raises(ValueError, match="directory changed"):
            retention_salvage._prove_no_nested_git(worktree, None, None)
        with pytest.raises(ValueError, match="nested Git metadata"):
            retention_salvage._prove_no_nested_git(worktree, None, None)
    else:
        retention_salvage._prove_no_nested_git(worktree, None, None)
        assert len(scan.inspected) == 3  # the root was not traversed again
    assert worktree not in retention_salvage._NESTED_GIT_SCANS


def test_nested_git_scan_validates_real_directory_when_root_is_a_symlink(tmp_path):
    worktree = tmp_path / "real"
    worktree.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(worktree, target_is_directory=True)

    def introduce_nested_repo():
        (worktree / "new-repository" / ".git").mkdir(parents=True)

    with pytest.raises(ValueError, match="directory changed"):
        retention_salvage._prove_no_nested_git(alias, None, None, on_progress=introduce_nested_repo)


@settings(max_examples=12, deadline=None)
@given(payload=st.binary(max_size=64), hazard=st.sampled_from(["ignored", "nested-file", "nested-directory"]))
def test_property_retention_never_deletes_unsnapshotted_files(payload, hazard):
    """C-8.4: arbitrary ignored/nested data without a durable copy stays intact."""
    with tempfile.TemporaryDirectory(prefix="retention-safety-property-") as directory:
        root = Path(directory) / "state"
        repository = Path(directory) / "repository"
        root.mkdir()
        repository.mkdir()
        git(repository, "init", "-b", "feature")
        (repository / "tracked").write_text("baseline")
        git(repository, "add", "tracked")
        git(repository, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "baseline")
        worktree = root / "worktrees" / "job"
        git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
        parent = worktree / ("evidence" if hazard == "ignored" else "node_modules/pkg")
        parent.mkdir(parents=True)
        output = parent / "output.bin"
        output.write_bytes(payload)
        (repository / ".git" / "info" / "exclude").write_text("evidence/\nnode_modules/\n")
        if hazard == "nested-file":
            (parent / ".git").write_text("gitdir: unavailable")
        elif hazard == "nested-directory":
            (parent / ".git").mkdir()
        with Store(root / "state.sqlite3") as store:
            store.add_job(job_id="job", request_id="request", payload_digest="digest", kind="dispatch",
                          workdir=str(repository), worktree=str(worktree), prompt_path="/prompt",
                          sandbox="workspace-write", state="succeeded")
            store.update_job("job", workdir_head=git(repository, "rev-parse", "HEAD"))
            (root / "jobs" / "job").mkdir(parents=True)
            result = retention.maintenance(store, root, max_jobs=0)
            assert result["pruned"] == [] and result["protected"] == ["job"]
            assert output.read_bytes() == payload
            assert store.get_job("job") is not None and store.list_leases() == []
            assert result["errors"]
