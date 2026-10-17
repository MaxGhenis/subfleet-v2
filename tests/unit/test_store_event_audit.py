"""C-3.2: an events row a transaction inserts is that transaction's event.

`Store.transaction` adds its own audit event only for changes other than event
inserts. Until 2026-10-12 it audited every change, the event insert included, so
each `add_event` wrote its payload and then an empty row of the same kind
(`data_json='{}'`), and a reader taking the newest row of a kind found the empty
one first. Stores written then keep those rows; the readers still pass over them.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet.alerts import load_latches
from subfleet.capacity import DESKTOP_IDENTITY_EVENT, last_desktop_identity
from subfleet.daemon import PROBE_RECORD
from subfleet.importer import _Writer
from subfleet.store import Store, _json, utc_now

PROPERTY = settings(max_examples=150, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "state.sqlite3") as opened:
        yield opened


def events_after(store, mark):
    return store.query("SELECT event_id,kind,lane_id,data_json FROM events WHERE event_id>? "
                       "ORDER BY event_id", (mark,))


def newest(store):
    return store.one("SELECT COALESCE(MAX(event_id),0) AS n FROM events")["n"]


def raw_event(conn, kind, data):
    """An event written the way the sessions verbs and the importer write theirs."""
    conn.execute("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)", (utc_now(), kind, _json(data)))


def lease(conn, key):
    """A change to a table other than `events`: the kind only an audit event records."""
    conn.execute("INSERT INTO leases VALUES (?,?,?,?)", (key, "holder", utc_now(), None))


def test_an_event_insert_is_one_row(store):
    """C-3.2: the reproduction from 2026-09-27; `add_event` used to leave a `{}` twin after its payload."""
    mark = newest(store)
    event_id = store.add_event("probe.state", data={"holder": "x"})
    assert events_after(store, mark) == [
        {"event_id": event_id, "kind": "probe.state", "lane_id": None, "data_json": '{"holder":"x"}'}]


@pytest.mark.parametrize("shape", ["add_event", "raw", "nested"])
def test_a_transaction_that_only_records_an_event_adds_no_audit(store, shape):
    """C-3.2: `timers._keepalive_lane` wraps `add_event('timer.keepalive')` in a
    `timer.keepalive` transaction, and the sessions verbs insert their record raw
    inside an `audit_kind` transaction; each left empty rows beside the record."""
    mark = newest(store)
    with store.transaction("timer.keepalive", lane_id="lane-1") as conn:
        if shape == "add_event":
            store.add_event("timer.keepalive", lane_id="lane-1", data={"status": "ok"})
        elif shape == "raw":
            raw_event(conn, "timer.keepalive", {"status": "ok"})
        else:
            with store.transaction("inner"):
                with store.transaction("innermost"):
                    store.add_event("timer.keepalive", lane_id="lane-1", data={"status": "ok"})
    rows = events_after(store, mark)
    assert [(row["kind"], json.loads(row["data_json"])) for row in rows] == [
        ("timer.keepalive", {"status": "ok"})]


def test_a_transaction_that_changes_a_row_and_records_an_event_still_audits(store):
    """C-3.2: `timers._reserve_lane`'s shape: a lease and a `timer.reservation`
    record in one `timer.reserved` transaction. The lease's own transaction and the
    outer one each audit; the record is one row with no twin."""
    mark = newest(store)
    with store.transaction("timer.reserved", lane_id="lane-1"):
        assert store.acquire_lease("lane:lane-1:slot:0", "probe:timer:1")
        store.add_event("timer.reservation", lane_id="lane-1", data={"holder": "probe:timer:1"})
    rows = [(row["kind"], row["lane_id"], json.loads(row["data_json"])) for row in events_after(store, mark)]
    assert rows == [
        ("lease.acquired", None, {"holder": "probe:timer:1", "lease_key": "lane:lane-1:slot:0"}),
        ("timer.reservation", "lane-1", {"holder": "probe:timer:1"}),
        ("timer.reserved", "lane-1", {}),
    ]


@pytest.mark.parametrize("change", ["delete", "update", "insert-then-delete", "noop-update"])
def test_a_change_to_events_rows_that_is_not_an_insert_is_audited(store, change):
    """C-3.2: only rows the transaction added to `events` stand for it. Deleting or
    rewriting an event, or adding one and deleting it again, is a change of state."""
    store.add_event("old", data={"n": 1})
    with store.transaction("events.changed", data={"change": change}) as conn:
        if change == "delete":
            conn.execute("DELETE FROM events WHERE kind='old'")
        elif change == "update":
            conn.execute("UPDATE events SET data_json='{\"n\":2}' WHERE kind='old'")
        elif change == "insert-then-delete":
            raw_event(conn, "fleeting", {"n": 3})
            conn.execute("DELETE FROM events WHERE kind='fleeting'")
        else:
            conn.execute("UPDATE events SET kind=kind WHERE kind='old'")
    # By kind, not by id: the audit of a delete of the newest event takes its freed id.
    rows = store.query("SELECT kind,data_json FROM events WHERE kind IN ('old','fleeting','events.changed') "
                       "ORDER BY event_id")
    old = [] if change == "delete" else [("old", {"n": 2 if change == "update" else 1})]
    assert [(row["kind"], json.loads(row["data_json"])) for row in rows] == [
        *old, ("events.changed", {"change": change})]


def test_a_rewrite_of_the_newest_event_is_not_mistaken_for_an_insert(store):
    """C-3.2: delete the newest event and insert one in its place. SQLite reuses the
    freed id, which is not above the transaction's mark, so the pair is audited."""
    store.add_event("top", data={"n": 1})
    top = newest(store)
    with store.transaction("events.replaced") as conn:
        conn.execute("DELETE FROM events WHERE event_id=?", (top,))
        raw_event(conn, "top", {"n": 2})
    rows = [(row["kind"], json.loads(row["data_json"])) for row in events_after(store, top - 1)]
    assert rows == [("top", {"n": 2}), ("events.replaced", {})]


def test_nothing_changed_writes_nothing_and_a_rollback_keeps_nothing(store):
    """C-3.2: no change, no event; a transaction that raises leaves no row and no generation."""
    store.add_event("seed")
    mark, generation = newest(store), store.generation
    with store.transaction("idle") as conn:
        conn.execute("UPDATE leases SET holder='x' WHERE lease_key='absent'")
    with pytest.raises(RuntimeError):
        with store.transaction("doomed"):
            store.add_event("doomed", data={"n": 1})
            raise RuntimeError("boom")
    assert events_after(store, mark) == []
    assert store.generation == generation


def test_an_event_only_commit_still_moves_the_generation(store):
    """C-5.11: a waiter that derives its answer from events must see a new event."""
    generation = store.generation
    store.add_event("desktop.identity", data={"identity": "a"})
    assert store.generation == generation + 1
    with store.transaction("outer"):
        store.add_event("desktop.identity", data={"identity": "b"})
        store.add_event("desktop.identity", data={"identity": "c"})
    assert store.generation == generation + 2


# --- properties ---------------------------------------------------------------

KINDS = st.sampled_from(["probe.state", "timer.reservation", "timer.keepalive", "gate.state"])
DATA = st.dictionaries(st.sampled_from(["holder", "status", "n"]), st.integers(0, 9), max_size=2)


@PROPERTY
@given(st.lists(st.tuples(KINDS, DATA, st.integers(0, 3)), max_size=12))
def test_n_add_event_calls_add_exactly_n_rows(calls):
    """C-3.2: whatever transactions enclose them, N `add_event` calls add N rows, each its own payload."""
    with tempfile.TemporaryDirectory() as root, Store(Path(root) / "state.sqlite3") as store:
        mark = newest(store)

        def record(kind, data, depth):
            if depth:
                with store.transaction(kind):
                    record(kind, data, depth - 1)
            else:
                store.add_event(kind, data=data)

        for kind, data, depth in calls:
            record(kind, data, depth)
        rows = events_after(store, mark)
        assert len(rows) == len(calls)
        assert [(row["kind"], json.loads(row["data_json"])) for row in rows] == [
            (kind, data) for kind, data, _depth in calls]


# A program is a list of top-level steps. A transaction step is
# ("tx", kind, data, children); its children are events ("event", how, kind,
# data), writes to `leases` ("write",), and transactions. ("fail", children) is a
# transaction that raises once its children have run, and its parent catches it.
EVENT = st.tuples(st.just("event"), st.sampled_from(["add_event", "raw"]), KINDS, DATA)
WRITE = st.just(("write",))


def transactions(children):
    return st.tuples(st.just("tx"), KINDS, st.one_of(st.none(), DATA), st.lists(children, max_size=4))


def failures(children):
    return st.tuples(st.just("fail"), st.lists(children, max_size=3))


TREE = st.recursive(st.one_of(EVENT, WRITE), transactions, max_leaves=12)
TREE_WITH_FAILURES = st.recursive(st.one_of(EVENT, WRITE),
                                  lambda children: st.one_of(transactions(children), failures(children)),
                                  max_leaves=12)
TOP = st.one_of(st.tuples(st.just("event"), st.just("add_event"), KINDS, DATA), transactions(TREE))
TOP_WITH_FAILURES = st.one_of(st.tuples(st.just("event"), st.just("add_event"), KINDS, DATA),
                              transactions(TREE_WITH_FAILURES))


class Rollback(Exception):
    pass


class Runner:
    """Runs a program on a real store; each transaction's data names it (`tx`) so its audit is findable."""

    def __init__(self, store):
        self.store, self.leases, self.transactions = store, 0, 0

    def run(self, step, conn=None):
        tag = step[0]
        if tag == "event":
            _tag, how, kind, data = step
            if how == "add_event" or conn is None:
                self.store.add_event(kind, data=data)
            else:
                raw_event(conn, kind, data)
        elif tag == "write":
            self.leases += 1
            lease(conn, f"lease:{self.leases}")
        elif tag == "tx":
            _tag, kind, data, children = step
            self.transactions += 1
            audit = {**(data or {}), "tx": self.transactions}
            with self.store.transaction(kind, data=audit) as inner:
                for child in children:
                    self.run(child, inner)
        else:
            try:
                with self.store.transaction("doomed") as inner:
                    for child in step[1]:
                        self.run(child, inner)
                    raise Rollback
            except Rollback:
                pass


def children_of(step):
    return step[3] if step[0] == "tx" else step[1] if step[0] == "fail" else ()


def any_change(step):
    """Whether anything at all is written under this step, kept or not."""
    return step[0] in ("event", "write") or any(any_change(child) for child in children_of(step))


def rolled_back_change(step):
    """Whether a failure anywhere under this step wrote anything (which it then rolled back)."""
    return any(any_change(child) if child[0] == "fail" else rolled_back_change(child)
               for child in children_of(step))


def expected_rows(program):
    """The reference: an event is its own row; a transaction adds its audit, after its
    children's rows, exactly when something under it wrote to a table other than events."""
    rows, counter = [], [0]

    def walk(step):
        tag = step[0]
        if tag == "event":
            rows.append((step[2], step[3]))
            return False
        if tag == "write":
            return True
        _tag, kind, data, children = step
        counter[0] += 1
        audit = {**(data or {}), "tx": counter[0]}
        wrote = False
        for child in children:
            wrote |= walk(child)
        if wrote:
            rows.append((kind, audit))
        return wrote

    for step in program:
        walk(step)
    return rows, sum(any_change(step) for step in program)


@PROPERTY
@given(st.lists(TOP, max_size=6))
def test_every_transaction_audits_exactly_its_changes_other_than_event_inserts(program):
    """C-3.2 against a reference model: the events table is each event once, in the order
    written, plus one audit per transaction that wrote a row elsewhere; the generation
    (C-5.11) moves once per top-level step that changed anything."""
    with tempfile.TemporaryDirectory() as root, Store(Path(root) / "state.sqlite3") as store:
        mark, generation = newest(store), store.generation
        runner = Runner(store)
        for step in program:
            runner.run(step)
        rows, changed = expected_rows(program)
        assert [(row["kind"], json.loads(row["data_json"])) for row in events_after(store, mark)] == rows
        assert store.generation - generation == changed


def kept_events(step):
    """The events a step keeps, in order: nothing under a failure survives it."""
    if step[0] == "event":
        return [(step[2], step[3])]
    if step[0] == "fail":
        return []
    return [event for child in children_of(step) for event in kept_events(child)]


@PROPERTY
@given(st.lists(TOP_WITH_FAILURES, max_size=6))
def test_a_rolled_back_child_never_hides_a_kept_change(program):
    """C-3.2 with savepoint rollbacks: a rolled-back subtree leaves no row; a kept event
    is one row; a transaction that kept a write elsewhere has one audit; one under which
    no failure wrote anything and that kept no write elsewhere has none. One whose only
    writes elsewhere were rolled back may audit or not (intended for now: `total_changes`
    still counts rolled-back writes, so today it does)."""
    with tempfile.TemporaryDirectory() as root, Store(Path(root) / "state.sqlite3") as store:
        mark = newest(store)
        runner = Runner(store)
        for step in program:
            runner.run(step)
        audits, payloads = {}, []
        for row in events_after(store, mark):
            data = json.loads(row["data_json"])
            assert row["kind"] != "doomed", f"a rolled-back transaction's audit survived: {row}"
            if "tx" in data:
                audits.setdefault(data["tx"], []).append(row)
            else:
                payloads.append((row["kind"], data))
        assert payloads == [event for step in program for event in kept_events(step)]
        assert all(len(found) == 1 for found in audits.values())

        counter = [0]

        def check(step, doomed):
            """Numbers transactions as `Runner` does; returns whether the step kept a write elsewhere."""
            tag = step[0]
            if tag in ("event", "write"):
                return tag == "write" and not doomed
            if tag == "fail":
                for child in step[1]:
                    check(child, True)
                return False
            counter[0] += 1
            number = counter[0]
            kept = False
            for child in step[3]:
                kept |= check(child, doomed)
            if doomed:
                assert number not in audits, f"transaction {number} was rolled back yet left its audit"
            elif kept:
                assert number in audits, f"transaction {number} kept a write elsewhere and has no audit"
            elif not rolled_back_change(step):
                assert number not in audits, f"transaction {number} kept only events yet audited"
            return kept

        for step in program:
            check(step, False)


@PROPERTY
@given(st.lists(st.tuples(KINDS, DATA, st.sampled_from([None, "lane-1"])), max_size=10), st.booleans())
def test_the_importers_raw_event_and_add_event_agree(facts, enclosed):
    """C-3.2 differential: `importer._Writer.event` inserts its row raw and `Store.add_event`
    through a transaction; both write one row per fact, enclosed in a transaction or not,
    and an enclosing transaction that records only facts adds no audit."""
    def written(write):
        with tempfile.TemporaryDirectory() as root, Store(Path(root) / "state.sqlite3") as store:
            mark = newest(store)
            if enclosed:
                with store.transaction("import.facts"):
                    write(store)
            else:
                write(store)
            return [(row["kind"], row["lane_id"], json.loads(row["data_json"]))
                    for row in events_after(store, mark)]

    def through_store(store):
        for kind, data, lane_id in facts:
            store.add_event(kind, lane_id=lane_id, data=data)

    def through_importer(store):
        writer = _Writer(store, dry_run=False)
        for kind, data, lane_id in facts:
            writer.event(kind, data=data, lane_id=lane_id)

    expected = [(kind, lane_id, data) for kind, data, lane_id in facts]
    assert written(through_store) == written(through_importer) == expected


# --- stores written before the change -----------------------------------------

def test_a_store_holding_the_old_twins_still_reads_and_writes(tmp_path):
    """C-3.1, C-3.2: no schema change, so an older store opens as it was. Its empty
    rows stay, and the readers that take a kind's newest row still pass over them."""
    path = tmp_path / "state.sqlite3"
    with Store(path) as store:
        # What `add_event` wrote before 2026-10-12: the payload, then `{}` of its kind.
        for kind, data in ((DESKTOP_IDENTITY_EVENT, {"identity": "acct:org", "label": "a@example.org"}),
                           ("probe.state", {"holder": "probe:1", "state": "quarantined"}),
                           ("alert-latch", {"key": "lane-down:codex-1", "active": True})):
            with store.transaction("seed") as conn:
                raw_event(conn, kind, data)
                raw_event(conn, kind, {})
        schema = store.query("SELECT * FROM schema_version")
    with Store(path) as store:
        assert store.query("SELECT * FROM schema_version") == schema
        identity = last_desktop_identity(store.query(
            "SELECT * FROM events WHERE kind=? ORDER BY event_id DESC LIMIT 8", (DESKTOP_IDENTITY_EVENT,)))
        assert identity == {"identity": "acct:org", "label": "a@example.org"}
        found = store.query(PROBE_RECORD, ("probe:1",))
        assert [json.loads(row["data_json"])["state"] for row in found if row["hit"]] == ["quarantined"]
        latches = load_latches(store.query("SELECT data_json FROM events WHERE kind='alert-latch' ORDER BY event_id"))
        assert latches == {"lane-down:codex-1": {"key": "lane-down:codex-1", "active": True}}
        mark = newest(store)
        store.add_event(DESKTOP_IDENTITY_EVENT, data={"identity": "acct2:org", "label": "b@example.org"})
        assert len(events_after(store, mark)) == 1
        with Store(path, read_only=True) as reader:
            assert last_desktop_identity(reader.query(
                "SELECT * FROM events WHERE kind=? ORDER BY event_id DESC LIMIT 8",
                (DESKTOP_IDENTITY_EVENT,)))["label"] == "b@example.org"
