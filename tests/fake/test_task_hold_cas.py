"""C-11.10 (D-TASKHOLD-CAS1): compare and write one task hold atomically."""

from __future__ import annotations

import contextlib
from types import MethodType

import pytest

from subfleet import protocol, task_holds
from subfleet.contracts import Exit
from subfleet.store import Store
from tests import task_hold_cas_legacy
from tests.disk_floor_model import stamp
from tests.task_hold_support import fresh_daemon, hold, submit


PREFIX = "battery-backoff:"
OWN_NOTE = PREFIX + " hot"
TABLES = ("leases", "jobs", "attempts", "events", "closures")


def image(service):
    """Values of every relevant row, including every job and every event."""
    return {table: service.store.query(f"SELECT * FROM {table} ORDER BY rowid") for table in TABLES}


def request(service, action, *, prefix=PREFIX, note=OWN_NOTE, until=600):
    args = {"action": action, "task": "build"}
    if action == "hold":
        args.update(until=stamp(until), note=note)
    if prefix is not None:
        args["if_note_prefix"] = prefix
    return service.dispatch("tasks", args)


def assert_condition_refusal(service, action, blocking, **kwargs):
    before = image(service)
    with pytest.raises(protocol.ProtocolError) as captured:
        request(service, action, **kwargs)
    refusal = captured.value
    assert refusal.reason == "task-hold-condition", "CAS-refusal: wrong refusal"
    assert refusal.code == Exit.REFUSED, "CAS-refusal: wrong exit code"
    assert refusal.details == {"task_hold": blocking}, "CAS-refusal: blocking hold lost fields"
    assert image(service) == before, "CAS-refusal: leases, jobs or events changed"
    return refusal


@pytest.mark.parametrize("note", ["operator booking", "x battery-backoff: middle", "Battery-backoff: uppercase"])
@pytest.mark.parametrize("action", ["hold", "release"])
def test_foreign_hold_refuses_without_changing_any_row_or_event(action, note):
    """A new matching note cannot authorize taking a foreign row; match is anchored and case sensitive."""
    with fresh_daemon() as (service, harness, clock, signals):
        build = submit(service, harness, "build", "held-build")
        submit(service, harness, "review", "other-task")
        blocking = hold(service, "build", stamp(500), note=note)["task_hold"]
        assert service.store.get_job(build)["wait_reason"] == "task-held"
        clock[0] = 10
        assert_condition_refusal(service, action, blocking)


@pytest.mark.parametrize("expired_at", [60, 61])
def test_expired_foreign_hold_allows_conditional_hold_but_blocks_release(expired_at):
    """Expiry opens hold replacement exactly at its end, but release still checks the row."""
    with fresh_daemon() as (service, harness, clock, signals):
        blocking = hold(service, "build", stamp(60), note="operator booking")["task_hold"]
        clock[0] = expired_at
        assert_condition_refusal(service, "release", blocking)
        reply = request(service, "hold")
        expected = {"task": "build", "until": stamp(600), "note": OWN_NOTE, "since": stamp(expired_at)}
        assert reply == {"held": "build", "task_hold": expected, "replaced": None, "task_holds": [expected]}, \
            "CAS-expiry: an expired row blocked replacement or preserved its since"


def test_matching_hold_can_be_renewed_and_released_with_trimmed_prefix():
    with fresh_daemon() as (service, harness, clock, signals):
        first = request(service, "hold", prefix="  battery-backoff:  ", note="  battery-backoff: hot  ", until=100)
        clock[0] = 25
        second = request(service, "hold", note=PREFIX + " still hot", until=200)
        assert second["replaced"] == first["task_hold"]
        assert second["task_hold"]["since"] == stamp(0)
        assert second["task_hold"]["note"] == PREFIX + " still hot"
        released = request(service, "release", prefix=" battery-backoff: ")
        assert released == {"released": "build", "was_held": True, "woken": 0, "task_holds": []}
        assert service.store.query("SELECT * FROM leases") == []


@pytest.mark.parametrize("action", ["hold", "release"])
@pytest.mark.parametrize("prefix", ["", "   ", "battery\nbackoff:", "x" * 161, "sk-proj-" + "a" * 64])
def test_invalid_condition_is_refused_before_a_write(action, prefix):
    with fresh_daemon() as (service, harness, clock, signals):
        before = image(service)
        with pytest.raises(protocol.ProtocolError) as captured:
            request(service, action, prefix=prefix)
        assert captured.value.reason == "task-hold-note"
        assert captured.value.code == Exit.INVALID_INPUT
        assert image(service) == before


def test_conditional_hold_note_must_start_its_prefix():
    with fresh_daemon() as (service, harness, clock, signals):
        before = image(service)
        with pytest.raises(protocol.ProtocolError) as captured:
            request(service, "hold", note="operator booking")
        assert captured.value.reason == "task-hold-note"
        assert captured.value.code == Exit.INVALID_INPUT
        assert image(service) == before


def test_conditional_release_with_no_row_is_successful_and_writes_nothing():
    with fresh_daemon() as (service, harness, clock, signals):
        submit(service, harness, "build", "queued-build")
        before = image(service)
        assert request(service, "release") == {"released": "build", "was_held": False, "woken": 0, "task_holds": []}
        assert image(service) == before


def test_conditional_release_with_no_row_leaves_stale_held_jobs_unchanged():
    """No row means no mutation even when a stale task-held waiter remains."""
    with fresh_daemon() as (service, harness, clock, signals):
        build = submit(service, harness, "build", "stale-held-build")
        request(service, "hold", until=60)
        assert service.store.get_job(build)["wait_reason"] == "task-held"
        with service.store.transaction("test.external_hold_delete") as tx:
            tx.execute("DELETE FROM leases WHERE lease_key=?", (task_holds.lease_key("build"),))
        before = image(service)
        assert request(service, "release") == {"released": "build", "was_held": False, "woken": 0, "task_holds": []}
        assert image(service) == before


@pytest.mark.parametrize("expired_at", [0, 60, 61])
def test_matching_release_deletes_both_active_and_expired_rows(expired_at):
    with fresh_daemon() as (service, harness, clock, signals):
        request(service, "hold", until=60)
        clock[0] = expired_at
        reply = request(service, "release")
        assert reply == {"released": "build", "was_held": expired_at < 60, "woken": 0, "task_holds": []}
        assert service.store.query("SELECT * FROM leases") == []
        assert service.store.query("SELECT kind FROM events WHERE kind='task.released'") == [{"kind": "task.released"}]


@pytest.mark.parametrize("action", ["hold", "release"])
def test_foreign_commit_before_writer_is_seen_by_condition(action, monkeypatch):
    """A second SQLite connection commits after request start, before BEGIN IMMEDIATE."""
    with fresh_daemon() as (service, harness, clock, signals):
        submit(service, harness, "build", "held-build")
        request(service, "hold", until=100)
        original = service.store.transaction
        writer = Store(service.store.path, initialize=False)
        foreign = {"task": "build", "until": stamp(400), "note": "operator booking", "since": stamp(9)}
        after_foreign = []

        @contextlib.contextmanager
        def transaction(kind="state.changed", **kwargs):
            if kind == {"hold": "task.held", "release": "task.released"}[action] and not after_foreign:
                # An actual committed row replaces the one the request saw at its start.
                with writer.transaction("test.foreign_hold") as tx:
                    tx.execute("UPDATE leases SET holder=?, acquired_at=?, expires_at=? WHERE lease_key=?",
                               (task_holds.HOLDER + foreign["note"], foreign["since"], foreign["until"],
                                task_holds.lease_key("build")))
                after_foreign.append(image(service))
            with original(kind, **kwargs) as tx:
                yield tx

        monkeypatch.setattr(service.store, "transaction", transaction)
        clock[0] = 10
        try:
            with pytest.raises(protocol.ProtocolError) as captured:
                request(service, action)
            assert after_foreign, "CAS-writer: the foreign transaction never ran"
            assert captured.value.reason == "task-hold-condition", "CAS-writer: wrong refusal"
            assert captured.value.code == Exit.REFUSED, "CAS-writer: wrong exit code"
            assert captured.value.details == {"task_hold": foreign}, "CAS-writer: stale hold named"
            assert image(service) == after_foreign[0], "CAS-writer: replaced or deleted the newly committed foreign hold"
        finally:
            writer.close()


def test_unconditional_handlers_match_frozen_a8624a489_rows_replies_and_events():
    """A byte-for-byte old-handler fixture drives the same initial store and clock."""
    with fresh_daemon() as (current, harness, unused_clock, signals):
        submit(current, harness, "build", "held-build")
        submit(current, harness, "review", "other-task")
        with fresh_daemon() as (legacy, legacy_harness, clock, legacy_signals):
            current.store.connection.backup(legacy.store.connection)
            legacy._hold_task = MethodType(task_hold_cas_legacy._hold_task, legacy)
            legacy._release_task = MethodType(task_hold_cas_legacy._release_task, legacy)
            schedule = [
                (0, "release", None, None),
                (0, "hold", 60, "operator booking"),
                (10, "hold", 100, "replaced while in force"),
                (100, "hold", 200, "replacement at expiry"),
                (205, "release", None, None),
                (206, "release", None, None),
                (210, "hold", 300, "another booking"),
                (220, "release", None, None),
            ]
            for instant, action, until, note in schedule:
                clock[0] = instant
                args = {"action": action, "task": "build"}
                if action == "hold":
                    args.update(until=stamp(until), note=note)
                assert current.dispatch("tasks", args) == legacy.dispatch("tasks", args), ("CAS-legacy: replies", args)
                assert image(current) == image(legacy), ("CAS-legacy: rows or events", args)
