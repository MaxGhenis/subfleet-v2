"""C-3.2, C-3.8: one event per transaction that changed something, named for what it did."""

import itertools
import random
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


def notices(store):
    return store.one("SELECT count(*) AS n FROM service_notices")["n"]


def test_c3_8_a_rollback_two_levels_down_leaves_every_ancestor_with_nothing_to_record(store):
    """C-3.2, C-3.8 what a grandchild rolled back is discounted all the way up, not only in its parent.

    The middle transaction caught the failure and committed; before its own
    discount was handed up, the outer one still counted the grandchild's write
    and recorded an event for a transaction that kept nothing.
    """
    with store.transaction("outer"):
        with store.transaction("middle"):
            try:
                with store.transaction("inner") as nested:
                    change(nested)
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
    assert events(store) == [] and notices(store) == 0
    # A row kept beside the same failure is still recorded, by every transaction that kept it.
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


class Boom(Exception):
    """A failure raised inside a random transaction."""


#: A transaction's body is a list of steps: ("write",), ("retitle",), ("raise",),
#: or ("child", body, caught), where `caught` says whether the parent catches a
#: failure escaping the child or lets it propagate.
MAX_DEPTH = 4

#: Trees per seed. Before a committed child handed its discount up, about one tree
#: in two hundred disagreed with the model (16, 13 and 14 of 3,000 for seeds 1 to
#: 3); the three seeds take about a second together.
TREES = 3000


def random_tree(rng, ids, depth=1):
    node = {"id": next(ids), "steps": []}
    for _ in range(rng.randint(1 if depth == 1 else 0, 4)):
        roll = rng.random()
        if roll < 0.1:
            node["steps"].append(("raise",))
        elif roll < 0.55 and depth < MAX_DEPTH:
            node["steps"].append(("child", random_tree(rng, ids, depth + 1), rng.random() < 0.6))
        elif roll < 0.8:
            node["steps"].append(("write",))
        else:
            node["steps"].append(("retitle",))
    return node


def title(node):
    """The name the transaction's event carries once every step has run: its last retitle, else its own."""
    retitles = [index for index, step in enumerate(node["steps"]) if step[0] == "retitle"]
    return f"t{node['id']}.r{retitles[-1]}" if retitles else f"t{node['id']}"


def model(node, decided):
    """(committed, rows kept, events kept) for `node`, worked out without a store.

    A transaction commits when no failure escapes its body. The rows it keeps are
    its own writes plus what its committed children kept, and it records an event
    exactly when it keeps a row; a failure that escapes discards everything,
    children's events included. `decided` maps each transaction that ran to the
    event it must have written at its exit: its title, or None.
    """
    rows, events = [], []
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
            events += child_events
    decided[node["id"]] = title(node) if rows else None
    return True, rows, events + ([title(node)] if rows else [])


def execute(store, node, seen):
    """Run `node` as a store transaction; record in `seen` the event each transaction left at its exit."""
    mark = store.one("SELECT COALESCE(MAX(event_id), 0) AS id FROM events")["id"]
    try:
        with store.transaction(f"t{node['id']}") as tx:
            for index, step in enumerate(node["steps"]):
                if step[0] == "write":
                    tx.execute("INSERT INTO service_notices(session_id,text,state,created_at) "
                               "VALUES('s',?,'pending','now')", (str(node["id"]),))
                elif step[0] == "retitle":
                    store.retitle(f"t{node['id']}.r{index}")
                elif step[0] == "raise":
                    raise Boom(node["id"])
                else:
                    try:
                        execute(store, step[1], seen)
                    except Boom:
                        if not step[2]:
                            raise
    finally:
        # Read inside the parent's still-open transaction, so the child's event is
        # seen even when an ancestor later rolls it back.
        rows = store.query("SELECT kind FROM events WHERE event_id > ? AND (kind = ? OR kind LIKE ?)",
                           (mark, f"t{node['id']}", f"t{node['id']}.%"))
        seen[node["id"]] = [row["kind"] for row in rows]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_c3_8_a_transaction_records_an_event_exactly_when_it_or_a_committed_descendant_kept_a_row(tmp_path, seed):
    """C-3.2, C-3.8 property: over random nesting, an event is written for a transaction iff something it did survives.

    Random trees of transactions up to four deep write rows, retitle, and fail;
    a failure is either caught by the parent or propagates through it. At each
    transaction's exit it has written one event, under its last title, exactly
    when it committed and it or a committed descendant kept a row, and none
    otherwise. After the outermost one ends, the store holds exactly the rows and
    events of the transactions whose every ancestor committed, the store's
    generation has moved once if the outermost one kept anything and not at all
    otherwise, and the audit stacks are back at depth zero.
    """
    rng, ids = random.Random(seed), itertools.count(1)
    with Store(tmp_path / "state.sqlite3") as store:
        store.connection.execute("PRAGMA synchronous=OFF")        # durability is not under test here
        for _ in range(TREES):
            tree = random_tree(rng, ids)
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
            assert seen.keys() == decided.keys(), tree                     # the same transactions ran
            for node_id, event in decided.items():
                assert seen[node_id] == ([event] if event else []), (node_id, tree)
            stored_events = store.query("SELECT kind FROM events WHERE event_id > ? ORDER BY event_id", (last_event,))
            stored_rows = store.query("SELECT text FROM service_notices WHERE notice_id > ? ORDER BY notice_id",
                                      (last_row,))
            assert [row["kind"] for row in stored_events] == kept_events, tree
            assert [int(row["text"]) for row in stored_rows] == rows, tree
            # C-5.11: the generation moves exactly when the outermost transaction kept something.
            assert store.generation == generation + (1 if committed and rows else 0), tree
            assert (len(store._audits), len(store._undone), store._depth) == (0, 0, 0), tree
