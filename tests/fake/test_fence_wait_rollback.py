"""C-3.8, C-6.10: a rolled-back retention-fence look consumes no wait verdict."""

import contextlib
import sqlite3

import pytest

from subfleet import folders
from subfleet.contracts import ClockSource, Closure, ClosureReason
from subfleet.daemon import ADMISSION_EVENT, after
from tests.fake.test_capacity_wait_backoff import fleet  # noqa: F401
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401


@contextlib.contextmanager
def fail_fence_transaction(service, monkeypatch, fault):
    """Fault only `job.fenced`, at its audit INSERT or its actual COMMIT."""
    if fault == "audit":
        service.store.conn.execute(
            "CREATE TEMP TRIGGER fail_fence_wait BEFORE INSERT ON events "
            "WHEN NEW.kind='job.fenced' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic fence audit failure'); END")
        try:
            yield
        finally:
            service.store.conn.execute("DROP TRIGGER fail_fence_wait")
        return

    original = service.store.transaction

    def deny_commit(action, first, second, database, trigger):
        if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @contextlib.contextmanager
    def transaction(kind="state.changed", **kwargs):
        try:
            if kind == "job.fenced":
                service.store.conn.set_authorizer(deny_commit)
            with original(kind, **kwargs) as tx:
                yield tx
        finally:
            if kind == "job.fenced":
                service.store.conn.set_authorizer(None)

    with monkeypatch.context() as patch:
        patch.setattr(service.store, "transaction", transaction)
        yield


@pytest.mark.parametrize("fault", ["audit", "commit"])
@pytest.mark.parametrize("previous_record", [True, False], ids=["restore", "remove"])
def test_c3_8_a_rolled_back_fence_look_does_not_consume_the_reserving_wait(
        fleet, monkeypatch, fault, previous_record):  # noqa: F811
    """A failed early fence B must not back off or suppress reservation's first B.

    The job first commits a no-lane verdict A. A retention fence then appears;
    its early look rolls back. The next look discovers that fence only in the
    reserving transaction (the race the second check covers), where its exact
    `lease-held` signature must still be new. With no in-memory A, as after a
    restart, rollback must remove B rather than keep a phantom first look.
    """
    service, harness = fleet
    service.store.put_closure(Closure("codex-1", "account", after(3600), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))
    job_id = service.dispatch("submit", harness.submit_args(pinned_model="terra"))["job_id"]
    service._admit()
    assert service.store.get_job(job_id)["state"] == "waiting"
    assert sum(row["kind"] == ADMISSION_EVENT for row in service.store.list_events(job_id)) == 1
    if not previous_record:
        service._capacity_waits.pop(job_id)
    previous = service._capacity_waits.get(job_id)
    with service.store.transaction("fixture.release") as tx:
        tx.execute("UPDATE closures SET released_at=? WHERE lane_id='codex-1'", (after(-1),))
    fence = folders.exclusive_key(folders.canonical(harness.workdir))
    assert service.store.acquire_lease(fence, "retention:fixture")
    service.store.update_job(job_id, next_check_at=after(-1))
    before = service.store.get_job(job_id)
    decisions = service.store.list_decisions(job_id)
    events = service.store.list_events(job_id)

    with fail_fence_transaction(service, monkeypatch, fault):
        with pytest.raises(sqlite3.DatabaseError):
            service._admit()
    restored = service._capacity_waits.get(job_id)
    assert service.store.get_job(job_id) == before
    assert service.store.list_decisions(job_id) == decisions
    assert service.store.list_events(job_id) == events

    # A fence can arrive after the early read; exercise the real transaction's
    # mandatory second read without changing that transaction or its verdict.
    monkeypatch.setattr(service, "_fence_hold", lambda *args, **kwargs: False)
    service._admit()
    assert service._holds[job_id]["reason"] == "lease-held"
    assert service._holds[job_id]["leases"] == [fence]
    assert service.store.list_attempts(job_id) == []
    assert {
        "record_after_failed_look": restored,
        "rechecks": service._capacity_waits[job_id]["rechecks"],
        "wait_events": sum(row["kind"] == ADMISSION_EVENT for row in service.store.list_events(job_id)),
    } == {"record_after_failed_look": previous, "rechecks": 0, "wait_events": 2}
