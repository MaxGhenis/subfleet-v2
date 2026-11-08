"""D-ADMIT-IDLE1: daemon-owned writes wake admission only after commit."""

from __future__ import annotations

import pytest

from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner
from subfleet.store import Store


def add_job(store, identity="job", **fields):
    return store.add_job(job_id=identity, request_id=identity, payload_digest="digest", kind="dispatch",
                         workdir="/work", prompt_path="/prompt", sandbox="read-only", **fields)


def add_lane(store, identity="codex-1"):
    store.put_lane(Lane(identity, "codex", identity, Credential("codex", "/fake", "home"),
                        "/fake", LaneOwner.V2, False))


@pytest.fixture
def watched(tmp_path):
    with Store(tmp_path / "store.sqlite3") as store:
        add_job(store)
        add_lane(store)
        store.add_closure(Closure("codex-1", "account", "2099-01-01T00:00:00Z",
                                  ClosureReason.OPERATOR_HOLD, ClockSource.REPORTED, "operator"))
        notices = []

        def committed(category):
            assert not store.connection.in_transaction
            assert store._depth == 0
            assert store._lock.held is None
            notices.append(category)

        store.watch_admission(committed)
        yield store, notices


def test_admission_wake_coalesces_nested_changes_after_outer_commit(watched):
    store, notices = watched
    store.acquire_lease("lane:codex-1:slot:0", "attempt")
    with store.transaction("test.outer") as tx:
        add_job(store, "new")
        store.release_leases("attempt")
        with store.transaction("test.nested"):
            store.update_lane("codex-1", enabled=0)
            tx.execute("UPDATE closures SET released_at='2026-11-07T00:00:00Z'")
        assert notices == []
    assert notices == ["closure", "jobs", "lane", "lease"]


def test_admission_wake_outer_rollback_and_empty_transaction_are_silent(watched):
    store, notices = watched
    with pytest.raises(RuntimeError, match="rollback"):
        with store.transaction() as tx:
            tx.execute("UPDATE closures SET released_at='2026-11-07T00:00:00Z'")
            add_job(store, "rolled-back")
            raise RuntimeError("rollback")
    with store.transaction():
        pass
    assert notices == []
    assert store.get_job("rolled-back") is None


def test_admission_wake_savepoint_rollback_preserves_only_outer_notices(watched):
    store, notices = watched
    with store.transaction():
        store.update_lane("codex-1", enabled=0)
        with pytest.raises(RuntimeError):
            with store.transaction() as tx:
                add_job(store, "rolled-back")
                tx.execute("UPDATE closures SET released_at='2026-11-07T00:00:00Z'")
                # The same category was already pending before this savepoint.
                store.update_lane("codex-1", enabled=1)
                raise RuntimeError("savepoint")
        assert notices == []
    assert notices == ["lane"]
    assert store.get_lane("codex-1").enabled is False
    assert store.one("SELECT released_at FROM closures")["released_at"] is None


@pytest.mark.parametrize("state,reason,new_state,new_reason", [
    ("running", None, "queued", None),
    ("running", None, "waiting", "capacity"),
    ("waiting", "capacity", "queued", None),
    ("waiting", "task-held", "waiting", "capacity"),
    ("waiting", "task-held", "queued", None),
])
def test_admission_wake_retry_or_task_release_enters_queue(watched, state, reason, new_state, new_reason):
    store, notices = watched
    store.update_job("job", state=state, wait_reason=reason)
    notices.clear()
    with store.transaction() as tx:
        tx.execute("UPDATE jobs SET state=?,wait_reason=? WHERE job_id='job'", (new_state, new_reason))
    assert notices == ["jobs"]


@pytest.mark.parametrize("sql,category", [
    ("UPDATE closures SET released_at='2026-11-07T00:00:00Z'", "closure"),
    ("UPDATE closures SET until_at='2099-02-01T00:00:00Z'", "closure"),
    ("DELETE FROM closures", "closure"),
    ("UPDATE lanes SET enabled=0", "lane"),
    ("UPDATE lanes SET owner='v1'", "lane"),
])
def test_admission_wake_raw_capacity_mutations(watched, sql, category):
    store, notices = watched
    with store.transaction() as tx:
        tx.execute(sql)
    assert notices == [category]


def test_admission_wake_new_and_removed_lane_and_closure(watched):
    store, notices = watched
    add_lane(store, "codex-2")
    assert notices == ["lane"]
    store.add_closure(Closure("codex-2", "account", "2099-01-01T00:00:00Z",
                              ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, None))
    assert notices == ["lane", "closure"]
    with store.transaction() as tx:
        tx.execute("DELETE FROM closures WHERE lane_id='codex-2'")
        tx.execute("DELETE FROM lanes WHERE lane_id='codex-2'")
    assert notices == ["lane", "closure", "closure", "lane"]


def test_admission_wake_wait_clocks_readings_and_unchanged_capacity_are_silent(watched):
    store, notices = watched
    with store.transaction() as tx:
        tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at='2099-01-01T00:00:00Z'")
        tx.execute("UPDATE jobs SET next_check_at='2099-01-01T00:00:30Z'")
        tx.execute("UPDATE lanes SET enabled=enabled,updated_at='2099-01-01T00:00:00Z'")
        tx.execute("UPDATE closures SET until_at=until_at")
        tx.execute("INSERT INTO readings(lane_id,scope,window,label,source,observed_at) "
                   "VALUES('codex-1','account','admission','unknown','test','2026-11-07T00:00:00Z')")
        tx.execute("DELETE FROM leases WHERE holder='absent'")
    store.add_event("test.quiet")
    assert notices == []


def test_admission_wake_triggers_are_connection_local_and_callback_replaceable(watched):
    store, old_notices = watched
    assert not store.query("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'admission_%'")
    assert len(store.query("SELECT name FROM sqlite_temp_master WHERE type='trigger' AND name LIKE 'admission_%'")) == 9
    new_notices = []
    store.watch_admission(new_notices.append)
    add_job(store, "new")
    assert old_notices == []
    assert new_notices == ["jobs"]
