"""The operator's delete path uses the same notice keep window as a pass."""
from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from subfleet.daemon import RETENTION_DORMANT_ENV, RETENTION_INTERVAL_S, Daemon
from subfleet.store import Store
from tests.unit.retention_delete_world import (
    STEP, delete_path, everything, nested_job, request, restore_modes, results, rows, run,
)
from tests.unit.retention_world import World, snapshot, trust_temporary_directories

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
OLD_NOTICE = "2020-01-01T00:00:00Z"
SESSION = "session-gone"
KEEP_S = 7 * 86400


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    yield w
    w.close()
    restore_modes(tmp_path)


def stamp(moment: datetime) -> str:
    """The notices' own spelling (C-1.7): UTC, seconds, `Z`."""
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def header(job_id: str) -> str:
    return f"{job_id}: succeeded; rc=0; deliverable=-; out=/tmp/out-{job_id}.md"


def add_notice(store: Store, job_id: str, session: str | None, created_at: str, state: str = "pending") -> int:
    notice_id = store.add_notice(job_id, header(job_id) + "\nattempt a1: ok, rc=0: done", session, state=state)
    store.update_notice(notice_id, created_at=created_at)
    return notice_id


def assert_unread_pin(line: dict, reason: str = "unread-notice") -> None:
    assert (line["disposition"], line["gate"], line["reason"]) == ("kept", "pins", reason), line


def test_an_expired_unread_notice_allows_deletion(world):
    job = "n-old"
    tree = nested_job(world, job)
    add_notice(world.store, job, SESSION, OLD_NOTICE)
    path = delete_path(world, now=lambda: NOW)

    record = run(path, request(job), notice_keep_s=KEEP_S)
    line = results(record)[job]
    assert line["disposition"] == "deleted" and line["gate"] is None, line
    assert not tree.exists()


def test_a_fresh_unread_notice_keeps_the_tree_unchanged(world):
    job = "n-fresh"
    tree = nested_job(world, job)
    add_notice(world.store, job, SESSION, stamp(NOW - timedelta(days=6)))
    before = snapshot(tree)
    path = delete_path(world, now=lambda: NOW)

    record = run(path, request(job), notice_keep_s=KEEP_S)
    assert_unread_pin(results(record)[job])
    assert snapshot(tree) == before


@pytest.mark.parametrize("since_cutoff", [0, 1], ids=["at-cutoff", "one-second-newer"])
def test_notice_boundary_uses_the_path_clock(world, since_cutoff):
    job = "n-boundary"
    tree = nested_job(world, job)
    add_notice(world.store, job, SESSION, stamp(NOW - timedelta(seconds=KEEP_S - since_cutoff)))
    path = delete_path(world, now=lambda: NOW)

    record = run(path, request(job), notice_keep_s=KEEP_S)
    line = results(record)[job]
    if since_cutoff:
        assert_unread_pin(line)
        assert tree.is_dir()
    else:
        assert line["disposition"] == "deleted", line
        assert not tree.exists()


@pytest.mark.parametrize("step", [{"notice_keep_s": 0}, {}], ids=["explicit-zero", "omitted"])
def test_zero_or_omitted_keep_pins_an_old_unread_notice(world, step):
    job = "n-zero"
    tree = nested_job(world, job)
    add_notice(world.store, job, SESSION, OLD_NOTICE)
    path = delete_path(world, now=lambda: NOW)

    record = run(path, request(job), **step)
    assert_unread_pin(results(record)[job])
    assert tree.is_dir()


def test_an_acknowledged_notice_allows_deletion_with_zero_keep(world):
    job = "n-acknowledged"
    tree = nested_job(world, job)
    add_notice(world.store, job, SESSION, OLD_NOTICE, state="acknowledged")
    path = delete_path(world, now=lambda: NOW)

    record = run(path, request(job), notice_keep_s=0)
    line = results(record)[job]
    assert line["disposition"] == "deleted", line
    assert not tree.exists()


def test_dry_run_uses_the_window_and_changes_nothing(world):
    old, fresh = "n-old", "n-fresh"
    nested_job(world, old)
    nested_job(world, fresh)
    add_notice(world.store, old, SESSION, OLD_NOTICE)
    add_notice(world.store, fresh, SESSION, stamp(NOW - timedelta(days=6)))
    before, rows_before = everything(world), rows(world)
    path = delete_path(world, now=lambda: NOW)

    record = run(path, request(old, fresh), mode="dry-run", notice_keep_s=KEEP_S)
    lines = results(record)
    assert lines[old]["disposition"] == "would-delete", lines[old]
    assert_unread_pin(lines[fresh])
    assert everything(world) == before and rows(world) == rows_before


def partial(path, *, due: bool = False, policy: dict | None = None) -> Daemon:
    d = object.__new__(Daemon)
    d._retention_delete = path
    d._last_maintenance = time.monotonic() - (RETENTION_INTERVAL_S + 5 if due else 0)
    d.policy = policy if policy is not None else {}
    d._unrun_lock = threading.Lock()
    d._unrun_dirty = False
    d.timers = SimpleNamespace(cancel=threading.Event())
    d.log = logging.getLogger("retention-delete-daemon-test")
    d._retention_pins = lambda: set()
    d.swept = 0

    def sweep() -> int:
        d.swept += 1
        d._unrun_dirty = False
        return 0
    d._sweep_unrun_worktrees = sweep
    return d


@pytest.mark.parametrize("policy_keep, disposition", [
    ({"notice_keep_days": 7}, "deleted"),
    ({"notice_keep_days": 0}, "kept"),
    ({}, "deleted"),
], ids=["seven-days", "zero", "default"])
def test_daemon_steps_use_the_policy_notice_window(world, monkeypatch, policy_keep, disposition):
    monkeypatch.delenv(RETENTION_DORMANT_ENV, raising=False)
    job = "n-old"
    tree = nested_job(world, job)
    add_notice(world.store, job, SESSION, OLD_NOTICE)
    path = delete_path(world, now=lambda: NOW)
    d = partial(path, policy={"retention": {**STEP, **policy_keep}})

    path.start(request(job), mode="apply")
    for _ in range(400):
        d._retention_delete_turn()
        record = path.status()["apply"]
        if record["status"] not in ("running", "stopping"):
            break
    assert record["status"] == "done", record
    line = results(record)[job]
    assert line["disposition"] == disposition, line
    if disposition == "kept":
        assert_unread_pin(line)
        assert tree.is_dir()
    else:
        assert line["gate"] is None and not tree.exists(), line


@pytest.mark.parametrize("point, boundary", [("kept", "rename"), ("renamed", "removal")],
                         ids=["before-rename", "before-removal"])
@pytest.mark.parametrize("created_at, disposition", [
    (stamp(NOW - timedelta(hours=1)), "kept"),
    (OLD_NOTICE, "deleted"),
], ids=["fresh", "old"])
def test_pin_reasks_use_the_same_notice_window(world, point, boundary, created_at, disposition):
    job = "n-late-notice"
    tree = nested_job(world, job)
    before = snapshot(tree)
    notice_id = None

    def on_step(job_id, state):
        nonlocal notice_id
        if (job_id, state) == (job, point) and notice_id is None:
            notice_id = add_notice(world.store, job, SESSION, created_at)

    path = delete_path(world, now=lambda: NOW, on_step=on_step)
    record = run(path, request(job), notice_keep_s=KEEP_S)
    line = results(record)[job]
    assert notice_id is not None, line
    assert line["disposition"] == disposition, line
    if disposition == "kept":
        assert_unread_pin(line, f"unread-notice (asked again before the {boundary})")
        assert snapshot(tree) == before
    else:
        assert not tree.exists()
    assert path.journals == set()
