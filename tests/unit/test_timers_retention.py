"""Hourly maintenance preserves C-8.4 evidence and accounts for actual bytes."""

import json
import logging
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from subfleet import retention, retention_trash
from subfleet import daemon as daemon_module
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.daemon import Daemon
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


@pytest.mark.parametrize("pin", ["active", "quarantined", "pending", "offered", "salvage", "gate-evidence"])
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


def test_failed_trash_removal_keeps_retry_journal_after_row_commit(retained, monkeypatch):
    """C-8.4: a deletion error preserves resumable trash and releases job ownership."""
    store, root = retained
    directory = job(store, root, "old", size=20)
    (directory / "stderr").write_bytes(b"y" * 30)
    original = retention_trash._reclaim

    def partial_remove(path, *args, **kwargs):
        assert not store.connection.in_transaction
        if path == root / "trash" / "old" / "job":
            raise PermissionError("cannot remove remaining output")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(retention_trash, "_reclaim", partial_remove)
    first = retention.maintenance(store, root, max_jobs=0, max_bytes=0)
    assert first["pruned"] == ["old"]
    assert first["protected"] == []
    assert first["bytes_before"] == 50
    assert first["bytes_after"] == 0
    assert first["jobs_after"] == 0
    assert first["errors"][0]["job_id"] == "old"
    assert store.get_job("old") is None
    assert not store.query("SELECT * FROM leases WHERE holder='retention:old'")
    assert (root / "trash" / "old" / "job" / "stderr").read_bytes() == b"y" * 30
    assert (root / "trash" / "old" / "manifest.json").exists()
    assert any(event["kind"] == "retention.trash_error" for event in store.list_events("old"))

    monkeypatch.setattr(retention_trash, "_reclaim", original)
    second = retention.maintenance(store, root, max_jobs=100, max_bytes=100)
    assert second["bytes_before"] == 0
    assert second["bytes_after"] == 0
    assert second["pruned"] == []
    assert store.get_job("old") is None
    assert not (root / "trash" / "old").exists()


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

    def unreadable_walk(path, *, followlinks, onerror):
        onerror(PermissionError("cannot enumerate artifacts"))
        return iter(())

    monkeypatch.setattr(retention.os, "walk", unreadable_walk)
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
    monkeypatch.setattr(retention, "_size", lambda *args, **kwargs: pytest.fail("interrupted pass must not scan"))
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
    original = retention.os.walk

    def interrupted_walk(path, **kwargs):
        for item in original(path, **kwargs):
            visited.append(item[0])
            cancel.set()
            yield item

    monkeypatch.setattr(retention.os, "walk", interrupted_walk)
    result = retention.maintenance(store, root, max_jobs=0, cancel=cancel)
    assert result["interrupted"] == "cancelled"
    assert len(visited) == 1
    assert result["bytes_before"] is None
    assert result["pruned"] == []
    assert store.connection.total_changes == before
    assert directory.exists()


def test_cancel_after_filesystem_stage_commits_current_job_and_preserves_later_jobs(retained, monkeypatch):
    """C-8.4: finish an atomic prune before observing cancellation for the next job."""
    store, root = retained
    first = job(store, root, "a")
    later = job(store, root, "b")
    cancel = threading.Event()
    original = Path.rename

    def rename_then_cancel(path, target):
        result = original(path, target)
        if path == first:
            cancel.set()
        return result

    monkeypatch.setattr(Path, "rename", rename_then_cancel)
    result = retention.maintenance(store, root, max_jobs=0, cancel=cancel)
    assert result["interrupted"] == "cancelled"
    assert result["pruned"] == ["a"]
    assert result["protected"] == ["b"]
    assert not first.exists()
    assert later.exists()
    assert store.get_job("a") is None
    assert not store.query("SELECT * FROM leases WHERE holder='retention:a'")
    monkeypatch.setattr(Path, "rename", original)
    resumed = retention.maintenance(store, root, max_jobs=0)
    assert resumed["pruned"] == ["b"]
    assert resumed["bytes_after"] == 0


@pytest.mark.parametrize("pruned", [[], ["old"]])
def test_daemon_retention_progress_is_audited_and_retried_soon(retained, monkeypatch, caplog, pruned):
    """Both partial scans and completed prunes are catch-up rather than timer failures."""
    store, root = retained
    marks = []
    service = SimpleNamespace(
        store=store, root=root, policy={}, log=logging.getLogger("retention-test"),
        conversations=SimpleNamespace(retention_pins=lambda: set()),
        timers=SimpleNamespace(cancel=threading.Event(), mark=lambda *a, **kw: marks.append((a, kw))),
        _last_maintenance=0,
    )
    result = {"interrupted": "deadline", "made_progress": True, "pruned": pruned, "bytes_after": None}
    monkeypatch.setattr(daemon_module, "maintenance", lambda *a, **kw: result)
    monkeypatch.setattr(daemon_module.time, "monotonic", lambda: 7200)
    monkeypatch.setattr(daemon_module, "after", lambda seconds: f"in:{seconds}")
    with caplog.at_level(logging.INFO, logger="retention-test"):
        Daemon._retention(service)
    assert marks == [(("retention",), {"next_due": "in:5"})]
    assert service._last_maintenance + 3600 == 7205
    assert "retention catch-up" in caplog.text
    events = store.query("SELECT data_json FROM events WHERE kind='retention.progress' AND data_json!='{}'")
    assert len(events) == 1
    assert json.loads(events[0]["data_json"]) == result


def test_daemon_retention_deadline_without_progress_remains_a_failure(retained, monkeypatch):
    store, root = retained
    service = SimpleNamespace(
        store=store, root=root, policy={},
        conversations=SimpleNamespace(retention_pins=lambda: set()),
        timers=SimpleNamespace(cancel=threading.Event()), _last_maintenance=0,
    )
    monkeypatch.setattr(daemon_module, "maintenance", lambda *a, **kw: {
        "interrupted": "deadline", "made_progress": False, "pruned": [],
    })
    with pytest.raises(TimeoutError, match="retention deadline reached"):
        Daemon._retention(service)
    assert service._last_maintenance == 0
