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


def test_failed_removal_retains_row_and_remaining_bytes_for_next_pass(retained, monkeypatch):
    """C-8.4, C-3.3: partial deletion retains retry state and counts unreclaimed bytes outside tx."""
    store, root = retained
    directory = job(store, root, "old", size=20)
    (directory / "stderr").write_bytes(b"y" * 30)
    original = retention.shutil.rmtree

    def partial_remove(path):
        assert not store.connection.in_transaction
        (Path(path) / "stdout").unlink()
        raise PermissionError("cannot remove remaining output")

    monkeypatch.setattr(retention.shutil, "rmtree", partial_remove)
    first = retention.maintenance(store, root, max_jobs=0, max_bytes=0)
    assert first["pruned"] == []
    assert first["protected"] == ["old"]
    assert first["bytes_before"] == 50
    assert first["bytes_after"] == 30
    assert first["jobs_after"] == 1
    assert first["errors"][0]["job_id"] == "old"
    assert store.get_job("old") is not None
    assert any(event["kind"] == "retention.remove_error" for event in store.list_events("old"))

    monkeypatch.setattr(retention.shutil, "rmtree", original)
    second = retention.maintenance(store, root, max_jobs=0, max_bytes=0)
    assert second["bytes_before"] == 30
    assert second["bytes_after"] == 0
    assert second["pruned"] == ["old"]
    assert store.get_job("old") is None


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


def test_cancel_after_filesystem_stage_preserves_rows_and_is_retryable(retained, monkeypatch):
    """C-16.4, C-8.4: cancellation after a removal stage prevents further DB writes and later-job deletion."""
    store, root = retained
    first = job(store, root, "a")
    later = job(store, root, "b")
    cancel = threading.Event()
    changes_at_cancel = []
    original = retention.shutil.rmtree

    def remove_then_cancel(path):
        original(path)
        changes_at_cancel.append(store.connection.total_changes)
        cancel.set()

    monkeypatch.setattr(retention.shutil, "rmtree", remove_then_cancel)
    result = retention.maintenance(store, root, max_jobs=0, cancel=cancel)
    assert result["interrupted"] == "cancelled"
    assert result["pruned"] == []
    assert result["protected"] == ["a", "b"]
    assert not first.exists()
    assert later.exists()
    assert store.get_job("a") is not None
    assert store.connection.total_changes == changes_at_cancel[0]
    monkeypatch.setattr(retention.shutil, "rmtree", original)
    resumed = retention.maintenance(store, root, max_jobs=0)
    assert resumed["pruned"] == ["a", "b"]
    assert resumed["bytes_after"] == 0


def test_a_deadline_after_the_first_prune_reports_that_prune(retained, monkeypatch):
    """C-8.4 (review of 19ecb52f): the daemon tells a pass that pruned from one that did
    not by `pruned`, which a deadline after the first committed prune must still carry."""
    from contextlib import contextmanager
    store, root = retained
    job(store, root, "a")
    later = job(store, root, "b")
    expired, real_transaction, real_time = threading.Event(), store.transaction, retention.time

    class Clock:
        def monotonic(self):
            return real_time.monotonic() + (10 ** 6 if expired.is_set() else 0)

        def __getattr__(self, name):
            return getattr(real_time, name)

    @contextmanager
    def transaction(kind="state.changed", **options):
        with real_transaction(kind, **options) as conn:
            yield conn
        if kind == "retention.pruned":
            expired.set()
    monkeypatch.setattr(store, "transaction", transaction)
    monkeypatch.setattr(retention, "time", Clock())
    result = retention.maintenance(store, root, max_jobs=0, deadline=real_time.monotonic() + 600)
    assert result["interrupted"] == "deadline"
    assert result["pruned"] == ["a"]
    assert store.get_job("a") is None and store.get_job("b") is not None and later.exists()
