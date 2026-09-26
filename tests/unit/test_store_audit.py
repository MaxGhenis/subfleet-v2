"""C-3.2, C-3.8: one event per transaction that changed something, named for what it did."""

import sqlite3

import pytest

from subfleet.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "state.sqlite3") as value:
        yield value


def events(store):
    return [(row["kind"], row["job_id"], row["attempt_id"], row["lane_id"]) for row in store.list_events()
            if not row["kind"].startswith("schema.")]


def change(tx):
    tx.execute("INSERT INTO service_notices(session_id,text,state,created_at) VALUES('s','t','pending','now')")


def test_c3_8_a_transaction_is_retitled_for_the_outcome_it_reached(store):
    """C-3.8 the event carries the name and references given last, and is still exactly one row."""
    with store.transaction("job.capacity_waiting", job_id="job-1") as tx:
        change(tx)
        store.retitle("attempt.reserved", attempt_id="job-1/a1", lane_id="codex-1")
    assert events(store) == [("attempt.reserved", "job-1", "job-1/a1", "codex-1")]


def test_c3_8_an_unretitled_transaction_keeps_the_name_it_opened_under(store):
    """C-3.8, C-3.2 the default outcome needs no call, and a transaction that changed nothing writes no event."""
    with store.transaction("job.capacity_waiting", job_id="job-1") as tx:
        change(tx)
    with store.transaction("job.capacity_waiting", job_id="job-2"):
        store.retitle("attempt.reserved")
    assert events(store) == [("job.capacity_waiting", "job-1", None, None)]


def test_c3_8_retitle_names_only_the_innermost_open_transaction(store):
    """C-3.8 a nested transaction's name is its own; the outer event is untouched by it and restored after it."""
    with store.transaction("outer", job_id="job-1") as tx:
        with store.transaction("inner") as nested:
            change(nested)
            store.retitle("inner.renamed")
        change(tx)
    assert [kind for kind, *_ in events(store)] == ["inner.renamed", "outer"]


def test_c3_8_retitle_outside_a_transaction_or_with_a_stray_field_is_refused(store):
    """C-3.8 there is nothing to name outside a transaction, and only the event's own columns can be set."""
    with pytest.raises(sqlite3.OperationalError):
        store.retitle("attempt.reserved")
    with store.transaction("job.capacity_waiting") as tx:
        change(tx)
        with pytest.raises(ValueError):
            store.retitle("attempt.reserved", ts="yesterday")
    assert [kind for kind, *_ in events(store)] == ["job.capacity_waiting"]


def test_c3_8_a_rolled_back_transaction_leaves_no_name_behind(store):
    """C-3.2, C-3.8 a failure writes no event and does not leak its title into the next transaction."""
    with pytest.raises(RuntimeError):
        with store.transaction("first") as tx:
            change(tx)
            store.retitle("first.renamed")
            raise RuntimeError("boom")
    with store.transaction("second") as tx:
        change(tx)
    assert [kind for kind, *_ in events(store)] == ["second"]


def test_c3_8_a_transaction_whose_only_change_was_rolled_back_writes_no_event(store):
    """C-3.2, C-3.8 a nested failure caught inside its parent leaves the parent with nothing to record.

    `total_changes` still counts the rolled-back child's writes; before this was
    discounted, the parent wrote an event for a transaction that changed nothing.
    """
    with store.transaction("outer") as tx:
        try:
            with store.transaction("inner") as nested:
                change(nested)
                raise RuntimeError("boom")
        except RuntimeError:
            pass
    with store.transaction("kept") as tx:
        try:
            with store.transaction("undone") as nested:
                change(nested)
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        change(tx)
    assert [kind for kind, *_ in events(store)] == ["kept"]
