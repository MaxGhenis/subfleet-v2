"""Hourly maintenance preserves C-8.4 evidence and accounts for actual bytes."""

import json
from pathlib import Path
import threading
import time

import pytest

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_fs as rfs
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


def test_an_entry_deletion_cannot_remove_is_set_aside_not_left_half_deleted(retained, monkeypatch):
    """C-8.4, C-3.3 (d635): the rows go only after the archive is verified; an
    entry verified deletion cannot unlink is moved to the job's conflicts folder,
    so no half-deleted tree is left behind, and its bytes are in the archive."""
    store, root = retained
    directory = job(store, root, "old", size=20)
    (directory / "stderr").write_bytes(b"y" * 30)
    original = rfs.os.unlink

    def refuse_stderr(name, *args, **kwargs):
        assert not store.connection.in_transaction
        if str(name) == "stderr":
            raise PermissionError(1, "cannot remove remaining output")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(rfs.os, "unlink", refuse_stderr)
    first = retention.maintenance(store, root, max_jobs=0, max_bytes=0)
    assert first["pruned"] == ["old"]
    assert first["bytes_before"] == 50 and first["bytes_after"] == 0
    assert store.get_job("old") is None
    assert not (root / "retention" / "old").exists()
    assert (root / "retention-conflicts" / "old" / "job" / "stderr").read_bytes() == b"y" * 30
    kinds = [event["kind"] for event in store.list_events("old")]
    assert "retention.pruned" in kinds and "retention.conflict" in kinds
    assert rarch.check_archive(root, "old")["ok"]


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


def test_cancel_mid_archive_keeps_rows_and_the_next_pass_finishes(retained, monkeypatch):
    """C-16.4, C-8.4: cancellation while a job is being archived leaves its rows
    and its journal; nothing is deleted, and the next pass finishes the work."""
    store, root = retained
    job(store, root, "a")
    job(store, root, "b")
    cancel = threading.Event()
    original = rfs.clone_or_copy

    def clone_then_cancel(*args, **kwargs):
        method = original(*args, **kwargs)
        cancel.set()
        return method

    monkeypatch.setattr(rfs, "clone_or_copy", clone_then_cancel)
    first = retention.maintenance(store, root, max_jobs=0, cancel=cancel)
    assert first["interrupted"] == "cancelled"
    assert first["pruned"] == []
    assert store.get_job("a") is not None and store.get_job("b") is not None
    monkeypatch.setattr(rfs, "clone_or_copy", original)
    resumed = retention.maintenance(store, root, max_jobs=0)
    assert sorted(resumed["pruned"]) == ["a", "b"]
    assert resumed["bytes_after"] == 0
    assert not (root / "jobs" / "a").exists() and not (root / "retention" / "a").exists()


def test_a_deadline_after_the_first_prune_reports_that_prune(retained, monkeypatch):
    """C-8.4 (review of 19ecb52f): the daemon tells a pass that pruned from one that did
    not by `pruned`, which a deadline between batches must still carry.
    A started batch finishes committing; the next job has not started yet."""
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
    result = retention.maintenance(store, root, max_jobs=0, deadline=real_time.monotonic() + 600, batch=1)
    assert result["interrupted"] == "deadline"
    assert result["pruned"] == ["a"]
    assert result["progressed"] is True
    assert store.get_job("a") is None and store.get_job("b") is not None and later.exists()


def test_an_interrupted_sizing_pass_retains_advancement_and_resumes_at_the_next_job(retained, monkeypatch):
    store, root = retained
    for name in ('a', 'b', 'c'):
        job(store, root, name)
    state = retention.RetentionState()
    now, measured = [1000.0], []
    original = retention._size
    def size(path, **kwargs):
        measured.append(path.name)
        found = original(path, **kwargs)
        now[0] += 2
        return found
    monkeypatch.setattr(retention, '_size', size)
    first = retention.maintenance(store, root, state=state, clock=lambda: now[0], deadline=1001,
                                  max_jobs=3, max_bytes=15)
    assert first['interrupted'] == 'deadline' and first['progressed'] is True
    assert first['pruned'] == [] and measured == ['a'] and set(state.sizes) == {'a'}
    second = retention.maintenance(store, root, state=state, clock=lambda: now[0], deadline=1100,
                                   max_jobs=3, max_bytes=15)
    assert measured == ['a', 'b']
    assert second['pruned'] and second['progressed'] is True


def test_a_cancelled_size_attempt_without_a_saved_size_is_not_advancement(retained, monkeypatch):
    store, root = retained
    job(store, root, 'a')
    state = retention.RetentionState()
    def cancel_size(*args, **kwargs):
        raise retention._Interrupted('cancelled')
    monkeypatch.setattr(retention, '_size', cancel_size)
    result = retention.maintenance(store, root, state=state)
    assert result['interrupted'] == 'cancelled' and result['progressed'] is False
    assert not state.sizes


@pytest.mark.parametrize('lease_kind', ['retire', 'worktree'])
def test_a_journal_less_leftover_lease_takes_priority_over_older_jobs(retained, lease_kind):
    store, root = retained
    for index, name in enumerate(('a', 'b', 'leftover')):
        job(store, root, name)
        with store.transaction('fixture.age') as conn:
            conn.execute('UPDATE jobs SET created_at=? WHERE job_id=?', (f'2026-01-0{index+1}T00:00:00Z', name))
    # The old driver removed one file before it died. Archive the remaining bytes.
    (root / 'jobs' / 'leftover' / 'stdout').unlink()
    (root / 'jobs' / 'leftover' / 'stderr').write_bytes(b'remaining')
    key = 'retire:leftover' if lease_kind == 'retire' else f'worktree:{root / "worktrees" / "leftover"}'
    store.acquire_lease(key, 'retention:leftover')
    result = retention.maintenance(store, root, max_jobs=0, batch=1)
    assert result['pruned'] == ['leftover']
    assert store.get_job('a') and store.get_job('b')
    assert rarch.check_archive(root, 'leftover')['ok']


def test_orphan_priority_survives_an_interrupted_pass(retained, monkeypatch):
    store, root = retained
    job(store, root, 'a')
    job(store, root, 'z')
    store.acquire_lease('retire:z', 'retention:z')
    state, cancel = retention.RetentionState(), threading.Event()
    original = retention._Pass._reasons
    def reasons(run, *args, **kwargs):
        cancel.set()
        return original(run, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(retention._Pass, '_reasons', reasons)
        result = retention.maintenance(store, root, state=state, max_jobs=0, cancel=cancel)
    assert result['interrupted'] == 'cancelled' and 'z' in state.leftovers
    result = retention.maintenance(store, root, state=state, max_jobs=0, batch=1)
    assert result['pruned'] == ['z']


def test_partial_verified_deletion_advances_until_only_a_blocked_remnant_is_left(retained, monkeypatch):
    """A committed journal can delete new files on a retry without pruning rows
    again. That is progress; an unchanged blocked remnant waits the hour."""
    store, root = retained
    job(store, root, 'a')
    for name in ('b', 'c'):
        (root / 'jobs/a' / name).write_bytes(name.encode())
    blocked = {'stdout', 'b', 'c'}
    original = rfs.Reclaim._one
    publish, published = rarch.Retirement.publish, []
    def publish_after_failure(retirement):
        if not published:
            published.append('failed')
            raise OSError('fixture interrupts publication after the commit')
        return publish(retirement)
    def one(reclaim, fd, rel, name):
        if name not in blocked:
            original(reclaim, fd, rel, name)
    def no_conflicts(*args, **kwargs):
        raise PermissionError('fixture blocks setting aside the remnant')
    monkeypatch.setattr(rfs.Reclaim, '_one', one)
    monkeypatch.setattr(rfs.Reclaim, '_conflict_dir', no_conflicts)
    monkeypatch.setattr(rarch.Retirement, 'publish', publish_after_failure)
    first = retention.maintenance(store, root, max_jobs=0)
    assert first['pruned'] == ['a'] and first['more'] is True
    assert first['in_flight'] == ['a']
    second = retention.maintenance(store, root, max_jobs=0)
    assert second['pruned'] == [] and second['progressed'] is True and second['more'] is True
    assert rarch.check_archive(root, 'a')['ok']
    blocked.difference_update({'stdout', 'b'})
    third = retention.maintenance(store, root, max_jobs=0)
    assert third['pruned'] == [] and third['progressed'] is True and third['more'] is True
    assert not (root / 'retention/a/job/stdout').exists()
    assert not (root / 'retention/a/job/b').exists()
    fourth = retention.maintenance(store, root, max_jobs=0)
    assert fourth['pruned'] == [] and fourth['progressed'] is False and fourth['more'] is True
    blocked.clear()
    fifth = retention.maintenance(store, root, max_jobs=0)
    assert fifth['reclaimed'] == ['a'] and fifth['more'] is False
    assert rarch.check_archive(root, 'a')['ok']
