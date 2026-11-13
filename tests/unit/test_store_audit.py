"""C-3.2, C-3.8: one event per transaction that kept a change, named for what it did."""

import itertools
import json

from hypothesis import HealthCheck, given, settings, strategies as st
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


# --- C-3.8: a nested rollback is no change, for the event and for the generation (C-5.11) ----

def notices(store):
    return store.one("SELECT count(*) AS n FROM service_notices")["n"]


def test_c3_8_audit_as_none_still_moves_the_generation(store):
    """C-3.8, C-5.11 a transaction that kept a change under no name records no event, yet
    moves the generation: a repeated wait still moved a job's clock, and waiters must wake."""
    generation = store.generation
    with store.transaction("job.capacity_waiting", job_id="job-1") as tx:
        change(tx)
        store.audit_as(None)
    assert events(store) == [] and store.generation == generation + 1
    with store.transaction("job.capacity_waiting", job_id="job-1"):
        store.audit_as(None)
    assert store.generation == generation + 1


def test_c3_8_a_transaction_whose_only_change_was_rolled_back_writes_no_event(store):
    """C-3.2, C-3.8 a nested failure caught inside its parent leaves the parent nothing to
    record, and the generation where it was: `total_changes` still counts the child's writes."""
    generation = store.generation
    with store.transaction("outer"):
        try:
            with store.transaction("inner") as nested:
                change(nested)
                raise RuntimeError("boom")
        except RuntimeError:
            pass
    assert events(store) == [] and store.generation == generation
    with store.transaction("kept") as tx:
        try:
            with store.transaction("undone") as nested:
                change(nested)
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        change(tx)
    assert [kind for kind, *_ in events(store)] == ["kept"] and store.generation == generation + 1


def test_c3_8_a_rollback_two_levels_down_leaves_every_ancestor_with_nothing_to_record(store):
    """C-3.2, C-3.8 what a grandchild rolled back is discounted all the way up: a committed
    child hands its count to its parent."""
    generation = store.generation
    with store.transaction("outer"):
        with store.transaction("middle"):
            try:
                with store.transaction("inner") as nested:
                    change(nested)
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
    assert events(store) == [] and notices(store) == 0 and store.generation == generation
    with store.transaction("outer.kept"):
        with store.transaction("middle.kept") as tx:
            try:
                with store.transaction("inner.undone") as nested:
                    change(nested)
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            change(tx)
    assert [kind for kind, *_ in events(store)] == ["middle.kept", "outer.kept"] and notices(store) == 1
    assert store.generation == generation + 1


class Boom(Exception):
    """A failure raised inside a generated transaction."""


#: A body is a list of steps: ("write",), ("rename",) (audit_as a new name), ("silence",)
#: (audit_as(None)), ("raise",), or ("child", body, caught), where `caught` says whether
#: the parent catches a failure escaping the child or lets it propagate.
MAX_DEPTH = 4
LEAVES = st.sampled_from([("write",), ("write",), ("rename",), ("silence",), ("raise",)])


def bodies(depth=1):
    step = LEAVES if depth >= MAX_DEPTH else st.one_of(
        LEAVES, st.tuples(st.just("child"), st.deferred(lambda: bodies(depth + 1)), st.booleans()))
    return st.lists(step, min_size=1 if depth == 1 else 0, max_size=4)


def numbered(body, ids):
    node = {"id": next(ids), "steps": []}
    for step in body:
        node["steps"].append(("child", numbered(step[1], ids), step[2]) if step[0] == "child" else step)
    return node


def title(node):
    """The name the transaction's event carries once every step has ran: its last rename
    (None for a silence), else its own."""
    named = [(index, step[0]) for index, step in enumerate(node["steps"]) if step[0] in ("rename", "silence")]
    if not named:
        return f"t{node['id']}"
    index, what = named[-1]
    return f"t{node['id']}.r{index}" if what == "rename" else None


def model(node, decided):
    """(committed, rows kept, events kept) for `node`, worked out without a store.

    A transaction commits when no failure escapes its body. The rows it keeps are its own
    writes plus what its committed children kept, and it records an event, under its last
    name, exactly when it keeps a row and that name is not None; a failure that escapes
    discards everything, children's events included. `decided` maps each transaction that
    ran to the event it must have written at its exit, or None."""
    rows, events_kept = [], []
    for step in node["steps"]:
        if step[0] == "write":
            rows.append(node["id"])
        elif step[0] == "raise":
            decided[node["id"]] = None
            return False, [], []
        elif step[0] == "child":
            committed, child_rows, child_events = model(step[1], decided)
            if not committed and not step[2]:
                decided[node["id"]] = None
                return False, [], []
            rows += child_rows
            events_kept += child_events
    name = title(node) if rows else None
    decided[node["id"]] = name
    return True, rows, events_kept + ([name] if name else [])


def execute(store, node, seen):
    """Run `node` as a store transaction; record in `seen` the events each one left at its exit."""
    mark = store.one("SELECT COALESCE(MAX(event_id), 0) AS id FROM events")["id"]
    try:
        with store.transaction(f"t{node['id']}") as tx:
            for index, step in enumerate(node["steps"]):
                if step[0] == "write":
                    tx.execute("INSERT INTO service_notices(session_id,text,state,created_at) "
                               "VALUES('s',?,'pending','now')", (str(node["id"]),))
                elif step[0] == "rename":
                    store.audit_as(f"t{node['id']}.r{index}")
                elif step[0] == "silence":
                    store.audit_as(None)
                elif step[0] == "raise":
                    raise Boom(node["id"])
                else:
                    try:
                        execute(store, step[1], seen)
                    except Boom:
                        if not step[2]:
                            raise
    finally:
        # Read inside the parent's still-open transaction, so a child's event is seen
        # even when an ancestor later rolls it back.
        rows = store.query("SELECT kind FROM events WHERE event_id > ? AND (kind = ? OR kind LIKE ?)",
                           (mark, f"t{node['id']}", f"t{node['id']}.%"))
        seen[node["id"]] = [row["kind"] for row in rows]


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(body=bodies())
def test_c3_8_a_transaction_records_an_event_exactly_when_it_or_a_committed_descendant_kept_a_row(store, body):
    """C-3.2, C-3.8, C-5.11 property (invariants 4 and 5), over generated nesting up to four deep.

    Each transaction writes rows, renames itself, silences itself (`audit_as(None)`) and
    fails; a failure is caught by its parent or propagates. At its exit a transaction has
    written one event, under its last name, exactly when it committed, it or a committed
    descendant kept a row, and the name is not None. After the outermost one ends the store
    holds exactly the rows and events of the transactions whose every ancestor committed;
    the generation moved once if the outermost one kept a row, whatever its name, and not
    at all otherwise; and the audit stacks are back at depth zero."""
    tree = numbered(body, itertools.count(1))
    decided, seen = {}, {}
    committed, rows, kept_events = model(tree, decided)
    last_event = store.one("SELECT COALESCE(MAX(event_id), 0) AS id FROM events")["id"]
    last_row = store.one("SELECT COALESCE(MAX(notice_id), 0) AS id FROM service_notices")["id"]
    generation = store.generation
    try:
        execute(store, tree, seen)
    except Boom:
        assert not committed, tree
    else:
        assert committed, tree
    assert seen.keys() == decided.keys(), tree
    for node_id, name in decided.items():
        assert seen[node_id] == ([name] if name else []), (node_id, tree)
    stored_events = store.query("SELECT kind FROM events WHERE event_id > ? ORDER BY event_id", (last_event,))
    stored_rows = store.query("SELECT text FROM service_notices WHERE notice_id > ? ORDER BY notice_id", (last_row,))
    assert [row["kind"] for row in stored_events] == kept_events, tree
    assert [int(row["text"]) for row in stored_rows] == rows, tree
    assert store.generation == generation + (1 if committed and rows else 0), tree
    assert (len(store._audits), len(store._undone), store._depth) == (0, 0, 0), tree
