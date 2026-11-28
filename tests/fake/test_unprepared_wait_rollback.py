"""C-6.10: an unprepared capacity wait keeps its last committed look on rollback."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

import subfleet.daemon
from tests.fake.test_capacity_wait_backoff import backoff, make_due, submit
from tests.fake.test_idle_capacity_events import clock, held_fleet  # noqa: F401
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401
from tests.unit.test_salvage import git


def unprepared_job(service, harness):
    git(harness.workdir, "init", "-b", "task/unprepared-rollback")
    git(harness.workdir, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "--allow-empty", "-m", "baseline")
    return submit(service, harness, pinned_model="terra", sandbox="workspace-write")


def waits(service, job):
    return [event for event in service.store.list_events(job) if event["kind"] == "job.unprepared_wait"]


@contextmanager
def failed_unprepared_wait(service, monkeypatch, fault):
    real = service.store.transaction
    conn = service.store.conn

    def deny(action, first, *_):
        if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @contextmanager
    def transaction(kind, **options):
        if kind != "job.unprepared_wait":
            with real(kind, **options) as tx:
                yield tx
            return

        if fault == "audit":
            conn.execute("CREATE TEMP TRIGGER fail_unprepared_wait BEFORE INSERT ON events "
                         "WHEN NEW.kind='job.unprepared_wait' "
                         "BEGIN SELECT RAISE(ABORT, 'synthetic unprepared audit failure'); END")
        elif fault == "commit":
            conn.set_authorizer(deny)
        try:
            with real(kind, **options) as tx:
                yield tx
                if fault == "body":
                    raise RuntimeError("synthetic unprepared wait failure")
        finally:
            if fault == "audit":
                conn.execute("DROP TRIGGER fail_unprepared_wait")
            elif fault == "commit":
                conn.set_authorizer(None)

    with monkeypatch.context() as patch:
        patch.setattr(service.store, "transaction", transaction)
        yield


def why_recheck(service, job):
    return service.dispatch("why", {"job_id": job})["job"]["recheck"]


@pytest.mark.parametrize("fault", [None, "body", "audit", "commit"], ids=["control", "body", "audit", "commit"])
def test_failed_repeat_look_keeps_recorded_wait(held_fleet, monkeypatch, fault):  # noqa: F811
    service, harness, _, _ = held_fleet
    job = unprepared_job(service, harness)
    service._admit()

    previous = service._capacity_waits[job]
    assert previous["signature"].startswith("unprepared:")
    assert previous["rechecks"] == 0
    assert backoff(service, job) == 1
    assert len(waits(service, job)) == 1
    assert service.store.list_attempts(job) == []
    previous_recheck = service.dispatch("why", {"job_id": job})["job"]["recheck"]
    assert previous_recheck["rechecks"] == 0

    make_due(service, job)
    before = service.store.get_job(job)
    events = waits(service, job)
    if fault is not None:
        expected = RuntimeError if fault == "body" else sqlite3.DatabaseError
        with failed_unprepared_wait(service, monkeypatch, fault), pytest.raises(expected):
            service._admit()

        assert service.store.get_job(job) == before
        assert waits(service, job) == events
        assert service._capacity_waits[job] is previous
        assert why_recheck(service, job) == previous_recheck

    service._admit()
    assert service._capacity_waits[job]["rechecks"] == 1
    assert backoff(service, job) == 2
    assert len(waits(service, job)) == len(events) + 1


@pytest.mark.parametrize("fault", ["body", "audit", "commit"])
def test_failed_first_look_leaves_no_wait(held_fleet, monkeypatch, fault):  # noqa: F811
    service, harness, _, _ = held_fleet
    job = unprepared_job(service, harness)
    before = service.store.get_job(job)

    expected = RuntimeError if fault == "body" else sqlite3.DatabaseError
    with failed_unprepared_wait(service, monkeypatch, fault), pytest.raises(expected):
        service._admit()

    assert job not in service._capacity_waits
    assert why_recheck(service, job) is None
    assert waits(service, job) == []
    assert service.store.get_job(job) == before

    service._admit()
    assert service._capacity_waits[job]["rechecks"] == 0
    assert backoff(service, job) == 1
    assert len(waits(service, job)) == 1


def test_one_capacity_wait_rollback_helper():
    assert Path(subfleet.daemon.__file__).read_text().count("def _capacity_wait_rollback(") == 1
