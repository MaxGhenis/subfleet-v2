"""C-3.2, C-3.8: one event per transaction that kept a change, named for what it did."""

import json

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


def test_c3_8_audit_as_names_the_event_and_its_ids(store):
    """C-3.8 the event carries the kind, data and ids given last, and is still exactly one row."""
    with store.transaction("job.capacity_waiting", job_id="job-1") as tx:
        change(tx)
        store.audit_as("attempt.reserved", attempt_id="job-1/a1", lane_id="codex-1")
    assert events(store) == [("attempt.reserved", "job-1", "job-1/a1", "codex-1")]
    assert store.list_events()[-1]["data_json"] == "{}"


def test_c3_8_ids_not_given_stay_as_the_transaction_opened(store):
    """C-3.8 `audit_as(kind, data)` keeps working for its existing caller, ids untouched."""
    with store.transaction("job.task_held", job_id="job-1", lane_id="codex-1") as tx:
        change(tx)
        store.audit_as("job.task_hold_moved", {"task": "t", "until": "u"})
    row = store.list_events()[-1]
    assert (row["kind"], row["job_id"], row["attempt_id"], row["lane_id"]) == (
        "job.task_hold_moved", "job-1", None, "codex-1")
    assert json.loads(row["data_json"]) == {"task": "t", "until": "u"}


def test_c3_8_an_unnamed_transaction_keeps_the_name_it_opened_under(store):
    """C-3.8, C-3.2 the default outcome needs no call, and a transaction that changed nothing writes no event."""
    with store.transaction("job.capacity_waiting", job_id="job-1") as tx:
        change(tx)
    with store.transaction("job.capacity_waiting", job_id="job-2"):
        store.audit_as("attempt.reserved", attempt_id="job-2/a1")
    assert events(store) == [("job.capacity_waiting", "job-1", None, None)]


def test_c3_8_audit_as_none_records_nothing_for_the_transaction(store):
    """C-3.2, C-3.8 a transaction named None records no event of its own, its change kept."""
    with store.transaction("job.capacity_waiting", job_id="job-1") as tx:
        change(tx)
        store.audit_as(None)
    assert events(store) == []
    assert store.one("SELECT count(*) AS n FROM service_notices")["n"] == 1


def test_c3_8_audit_as_names_only_the_innermost_open_transaction(store):
    """C-3.8 a nested transaction's name is its own; the outer event is untouched by it."""
    with store.transaction("outer", job_id="job-1") as tx:
        with store.transaction("inner", job_id="job-2") as nested:
            change(nested)
            store.audit_as("inner.renamed", attempt_id="job-2/a1")
        change(tx)
    assert events(store) == [("inner.renamed", "job-2", "job-2/a1", None), ("outer", "job-1", None, None)]


def test_c3_8_audit_as_outside_a_transaction_or_with_a_stray_field_is_refused(store):
    """C-3.8 there is nothing to name outside a transaction, and only the event's own ids can be set."""
    with pytest.raises(RuntimeError):
        store.audit_as("attempt.reserved")
    with pytest.raises(RuntimeError):
        store.audit_as("attempt.reserved", attempt_id="a")
    with store.transaction("job.capacity_waiting") as tx:
        change(tx)
        with pytest.raises(ValueError):
            store.audit_as("attempt.reserved", ts="yesterday")
        with pytest.raises(ValueError):
            store.audit_as("attempt.reserved", attempt_id="a", event_id=1)
    # A refused call changed nothing: the transaction keeps its own name and ids.
    assert events(store) == [("job.capacity_waiting", None, None, None)]


def test_c3_8_a_rolled_back_transaction_leaves_no_name_behind(store):
    """C-3.2, C-3.8 a failure writes no event and does not leak its name or ids into the next transaction."""
    with pytest.raises(RuntimeError):
        with store.transaction("first") as tx:
            change(tx)
            store.audit_as("first.renamed", attempt_id="x/a1")
            raise RuntimeError("boom")
    with store.transaction("second") as tx:
        change(tx)
    assert events(store) == [("second", None, None, None)]
