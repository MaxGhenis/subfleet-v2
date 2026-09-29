"""Hourly maintenance preserves C-8.4 evidence and accounts for actual bytes."""

import json
from pathlib import Path
import threading
import time

import pytest

from subfleet import retention
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.store import Store


@pytest.fixture
def retained(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/home/one", "home"),
                            "/home/one", LaneOwner.V2, False))
        yield store, tmp_path


def job(store, root, identity, *, state="succeeded", size=10):
    store.add_job(job_id=identity, request_id=identity, payload_digest="digest", kind="dispatch",
                  workdir=str(root), prompt_path="/prompt", sandbox="read-only", state=state)
    directory = root / "jobs" / identity
    directory.mkdir(parents=True)
    (directory / "stdout").write_bytes(b"x" * size)
    return directory


@pytest.mark.parametrize("pin", ["active", "quarantined", "pending", "offered", "gate-evidence"])
def test_hourly_retention_pins_required_evidence(retained, pin):
    """C-8.4: each required evidence pin defeats both job and byte pressure."""
    store, root = retained
    directory = job(store, root, "protected", state="running" if pin == "active" else "succeeded")
    job(store, root, "prunable")
    if pin in {"quarantined", "salvage"}:
        store.add_attempt(attempt_id="protected/a1", job_id="protected", seq=1, lane_id="codex-1",
                          model_requested="gpt-6-astra", state="quarantined" if pin == "quarantined" else "succeeded")
    if pin in {"pending", "offered"}:
        store.add_notice("protected", "unread", "operator", state=pin)
    if pin == "salvage":
        store.add_artifact("protected/a1", "salvage", "refs/subfleet-salvage/snapshot", "digest", 0)
    if pin == "gate-evidence":
        store.add_action(action_id="gate", kind="merge", op_key="head:pr", subject="repository",
                         request_json=json.dumps({"rounds": [{"attempt_id": "protected/a1"}]}))
    result = retention.maintenance(store, root, max_jobs=0, max_bytes=0)
    assert result["pruned"] == ["prunable"]
    assert result["protected"] == ["protected"]
    assert result["bytes_after"] == 10
    assert store.get_job("protected") is not None
    assert directory.exists()


def test_salvage_no_longer_pins_its_job(retained):
    """C-8.4 (design rev 2, section 7): retention never touches refs and never removes a
    worktree itself, so an unlanded salvage ref keeps nothing; rows.json keeps its name."""
    store, root = retained
    job(store, root, "salvaged")
    store.add_attempt(attempt_id="salvaged/a1", job_id="salvaged", seq=1, lane_id="codex-1",
                      model_requested="gpt-6-astra", state="succeeded")
    store.add_artifact("salvaged/a1", "salvage", "refs/subfleet-salvage/snapshot", "digest", 0)
    result = retention.maintenance(store, root, max_jobs=0, max_bytes=0)
    assert result["pruned"] == ["salvaged"]
    rows = json.loads((root / "archive" / "salvaged" / "rows.json").read_bytes())["rows"]
    assert [row["path"] for row in rows["artifacts"]] == ["refs/subfleet-salvage/snapshot"]


def test_an_entry_that_changes_after_archiving_is_kept_not_deleted(retained, monkeypatch):
    """C-8.4 J3, C-3.3: deletion runs outside every transaction and removes only what the
    verified archive holds unchanged; a changed file is kept, with the rest of what remains."""
    store, root = retained
    directory = job(store, root, "old", size=20)
    (directory / "stderr").write_bytes(b"y" * 30)
    original = retention.archive.delete_archived

    def write_late(path, manifest, **kwargs):
        assert not store.connection.in_transaction
        (Path(path) / "stderr").write_bytes(b"late output")
        return original(path, manifest, **kwargs)

    monkeypatch.setattr(retention.archive, "delete_archived", write_late)
    result = retention.maintenance(store, root, max_jobs=0, max_bytes=0)
    assert result["pruned"] == ["old"]
    assert result["bytes_before"] == 50 and result["bytes_after"] == 0
    kept = root / "retention-conflicts" / "old"
    assert (kept / "stderr").read_bytes() == b"late output"
    assert not (kept / "stdout").exists() and not directory.exists()
    assert any(event["kind"] == "retention.conflict" for event in store.list_events("old"))
    assert store.list_leases() == []


def test_directory_symlink_counts_link_without_external_contents(retained):
    """C-8.4, C-2.1: artifact links count their own bytes without traversing or deleting targets."""
    store, root = retained
    directory = job(store, root, "old", size=10)
    external = root / "external"
    external.mkdir()
    (external / "payload").write_bytes(b"x" * 1000)
    link = directory / "external-link"
    link.symlink_to(external, target_is_directory=True)
    expected_bytes = 10 + link.lstat().st_size
    result = retention.maintenance(store, root, max_bytes=10)
    assert result["bytes_before"] == expected_bytes
    assert result["bytes_after"] == 0
    assert result["pruned"] == ["old"]
    assert (external / "payload").read_bytes() == b"x" * 1000


def test_unreadable_tree_is_pinned_and_reported(retained, monkeypatch):
    """C-8.4: an unreadable directory cannot silently count as zero and become eligible for pruning."""
    store, root = retained
    job(store, root, "old")

    def unreadable(path):
        raise PermissionError("cannot enumerate artifacts")

    monkeypatch.setattr(retention.os, "scandir", unreadable)
    result = retention.maintenance(store, root, max_jobs=0, max_bytes=0)
    assert result["pruned"] == []
    assert result["protected"] == ["old"]
    assert result["errors"][0]["job_id"] == "old"
    assert store.get_job("old") is not None


@pytest.mark.parametrize("reason", ["cancelled", "deadline"])
def test_interrupted_retention_does_no_scan_or_mutation(retained, monkeypatch, reason):
    """C-16.4, C-8.4: a cancelled or expired maintenance pass preserves jobs without scanning files."""
    store, root = retained
    directory = job(store, root, "old")
    job(store, root, "active", state="running")
    cancel = threading.Event()
    if reason == "cancelled":
        cancel.set()
    deadline = time.monotonic() - 1 if reason == "deadline" else None
    before = store.connection.total_changes
    monkeypatch.setattr(retention, "_measure", lambda *args, **kwargs: pytest.fail("interrupted pass must not scan"))
    result = retention.maintenance(store, root, max_jobs=0, cancel=cancel, deadline=deadline)
    assert result["interrupted"] == reason
    assert result["pruned"] == []
    assert result["protected"] == ["active", "old"]
    assert result["bytes_before"] is None
    assert result["bytes_after"] is None
    assert store.connection.total_changes == before
    assert directory.exists()


def test_retention_byte_scan_observes_cancellation(retained, monkeypatch):
    """C-16.4, C-8.4: cancellation interrupts a directory walk without publishing partial byte totals."""
    store, root = retained
    directory = job(store, root, "old")
    cancel = threading.Event()
    before = store.connection.total_changes
    visited = []
    original = retention.os.scandir

    def interrupted_scan(path):
        visited.append(path)
        cancel.set()
        return original(path)

    monkeypatch.setattr(retention.os, "scandir", interrupted_scan)
    result = retention.maintenance(store, root, max_jobs=0, cancel=cancel)
    assert result["interrupted"] == "cancelled"
    assert len(visited) == 1
    assert result["bytes_before"] is None
    assert result["pruned"] == []
    assert store.connection.total_changes == before
    assert directory.exists()


def test_cancel_after_filesystem_stage_preserves_rows_and_is_retryable(retained, monkeypatch):
    """C-16.4, C-8.4 J2: cancellation while a job directory is archived commits nothing,
    deletes nothing, and the next pass retires every job."""
    store, root = retained
    first = job(store, root, "a")
    later = job(store, root, "b")
    cancel = threading.Event()
    original = retention.archive.archive

    def archive_then_cancel(*args, **kwargs):
        cancel.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(retention.archive, "archive", archive_then_cancel)
    result = retention.maintenance(store, root, max_jobs=0, cancel=cancel)
    assert result["interrupted"] == "cancelled"
    assert result["pruned"] == []
    assert result["protected"] == ["a", "b"]
    assert first.exists() and later.exists()
    assert store.get_job("a") is not None and store.get_job("b") is not None
    assert not (root / "archive" / "a").exists()
    monkeypatch.setattr(retention.archive, "archive", original)
    resumed = retention.maintenance(store, root, max_jobs=0)
    assert resumed["pruned"] == ["a", "b"]
    assert resumed["bytes_after"] == 0
    assert store.list_leases() == []
