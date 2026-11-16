"""D-DAEMON-QUIET1: labels cost one submission lookup per listed job."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st
import pytest

from subfleet.daemon import Daemon
from subfleet.status_json import attach_batches
from subfleet.store import batch_submission_rows


@contextmanager
def event_store():
    """The production event indexes, without unrelated daemon or host state."""
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(
            "CREATE TABLE events (event_id INTEGER PRIMARY KEY, job_id TEXT, kind TEXT, data_json TEXT NOT NULL);"
            "CREATE INDEX events_job ON events(job_id,event_id);"
            "CREATE INDEX events_kind ON events(kind,event_id);")
        store = SimpleNamespace(connection=connection,
                                query=lambda sql, params=(): list(connection.execute(sql, params)))
        yield store


def seed(store, rows):
    store.connection.executemany("INSERT INTO events(job_id,kind,data_json) VALUES (?,?,?)", rows)


def old_batches(store, ids, *, status=False):
    """The previous statement and each caller's parsing/filter, kept verbatim."""
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = store.query(f"SELECT job_id,data_json FROM events WHERE +kind='job.submitted' "
                       f"AND job_id IN ({marks}) AND data_json LIKE '%\"batch\"%'", ids)
    if status:
        found = {row["job_id"]: json.loads(row["data_json"] or "{}").get("batch") for row in rows}
        return {job_id: batch for job_id, batch in found.items() if isinstance(batch, dict)}
    found = {row["job_id"]: json.loads(row["data_json"]).get("batch") for row in rows}
    return {job_id: batch for job_id, batch in found.items() if batch}


def batches(store, ids, *, status=False):
    if status:
        snapshot = {"jobs": [{"job_id": job, "state": "running"} for job in ids]}
        attach_batches(store, snapshot)
        return snapshot["batches"]
    return Daemon._batches(SimpleNamespace(store=store), ids)


def outcome(function, *args, **kwargs):
    try:
        return ("value", function(*args, **kwargs))
    except (ValueError, AttributeError, TypeError) as exc:
        return ("error", type(exc))


BATCHES = (None, False, True, 0, 1, "", "label", [], [1], {}, {"label": "batch"})


@pytest.mark.parametrize("status", [False, True], ids=["list", "status"])
@pytest.mark.parametrize("payload", [json.dumps({"batch": batch}) for batch in BATCHES] + [
    "{}", '{"other": "value"}', '{"nested": {"batch": 1}}',
    '{"b\\u0061tch": {"label": "escaped key"}}', '{"BATCH": 1}',
    '{"batch": invalid}', "invalid without the field", '["batch"]', '"batch"',
])
def test_batch_callers_preserve_old_payload_filters_and_errors(payload, status):
    with event_store() as store:
        seed(store, [("no-submit", "attempt.observed_alive", '{"batch": 99}'),
                     ("submitted", "job.submitted", payload)])
        ids = ["submitted", "no-submit", "unknown"]
        assert outcome(batches, store, ids, status=status) == outcome(old_batches, store, ids, status=status)


@pytest.mark.parametrize("status", [False, True], ids=["list", "status"])
def test_empty_batch_listing_runs_no_statement(status):
    store = SimpleNamespace(query=lambda *args: pytest.fail("an empty listing made a query"))
    assert batches(store, [], status=status) == {}
    assert old_batches(store, [], status=status) == {}


@pytest.mark.parametrize("status", [False, True], ids=["list", "status"])
@pytest.mark.parametrize("first", [{"batch": {"label": "original"}}, {}])
def test_first_submission_defines_batch_even_if_a_malformed_store_repeats_it(status, first):
    """Fabricated histories and archived-id reuse retain the first identity, as existing readers do."""
    with event_store() as store:
        seed(store, [("j", "job.submitted", json.dumps(first)),
                     ("j", "attempt.observed_alive", "{}"),
                     ("j", "job.submitted", '{"batch": {"label": "replacement"}}')])
        assert old_batches(store, ["j"], status=status) == {"j": {"label": "replacement"}}
        assert batches(store, ["j"], status=status) == ({"j": first["batch"]} if first else {})


PAYLOADS = st.sampled_from([json.dumps({"batch": batch}) for batch in BATCHES] + [
    "{}", '{"batch": invalid}', "invalid without the field", '["batch"]',
    '{"b\\u0061tch": {"label": "escaped key"}}', '{"nested": {"batch": 1}}',
])
EVENTS = st.lists(st.tuples(st.integers(0, 7).map(lambda n: f"j{n}"),
                           st.sampled_from(["job.submitted", "attempt.observed_alive", "other"]),
                           PAYLOADS), max_size=60)


@settings(max_examples=150, deadline=None)
@given(events=EVENTS, ids=st.lists(st.integers(0, 9).map(lambda n: f"j{n}"), max_size=20))
def test_batch_lookup_matches_old_statement_for_generated_jobs_and_event_orders(events, ids):
    """Supported stores have at most one submission; arbitrary other events may surround it."""
    submitted = set()
    unique_submissions = []
    for job, kind, payload in events:
        if kind == "job.submitted":
            if job in submitted:
                continue
            submitted.add(job)
        unique_submissions.append((job, kind, payload))
    with event_store() as store:
        seed(store, unique_submissions)
        for status in (False, True):
            assert outcome(batches, store, ids, status=status) == outcome(old_batches, store, ids, status=status)


def test_batch_lookup_bounds_each_statement_to_500_ids_and_uses_events_job():
    with event_store() as store:
        ids = [f"j{n:04}" for n in range(1203)]
        seed(store, [(job, "job.submitted", '{"batch": {"label": "b"}}') for job in ids])
        calls = []

        def query(sql, params):
            calls.append(len(params))
            plan = " ".join(row["detail"] for row in store.query("EXPLAIN QUERY PLAN " + sql, params))
            assert "USING INDEX events_job (job_id=?)" in plan
            return store.query(sql, params)

        rows = batch_submission_rows(query, ids + ids[:20])
        assert {row["job_id"] for row in rows} == set(ids)
        assert len(rows) == len(ids)
        assert calls == [500, 500, 203]


def steps(store, function, ids):
    count = 0

    def progress():
        nonlocal count
        count += 1
        return 0

    store.connection.set_progress_handler(progress, 1)
    try:
        function(store, ids)
    finally:
        store.connection.set_progress_handler(None, 0)
    return count


@pytest.mark.parametrize("has_batch", [False, True], ids=["without-batch", "with-batch"])
def test_later_events_do_not_increase_batch_lookup_steps(has_batch):
    with event_store() as store:
        payload = '{"batch": {"label": "b"}}' if has_batch else "{}"
        for job, later in (("short", 10), ("long", 10000)):
            seed(store, [(job, "job.submitted", payload)] +
                 [(job, "attempt.observed_alive", "{}") for _ in range(later)])
        small, large = (steps(store, batches, [job]) for job in ("short", "long"))
        assert large <= small * 1.1 + 5, (small, large)
        # Ensure the fixture actually distinguishes the previous all-event walk.
        old_small, old_large = (steps(store, old_batches, [job]) for job in ("short", "long"))
        assert old_large > old_small * 100, (old_small, old_large)
