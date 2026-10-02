"""C-3.7: reads outside a transaction never wait for the store lock.

On 2026-09-25 a `list` whose statement takes 1 ms waited 34-94 s behind the
store's one lock, which every read and every transaction shared. A store with
read connections sends a read made outside a transaction to one of them; a
thread inside a transaction still reads through the writing connection, so it
sees its own rows; `snapshot()` gives several reads one committed state.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from subfleet import procs
from subfleet.daemon import READ_CONNECTIONS, Daemon
from subfleet.store import SnapshotWriteError, Store


@pytest.fixture(autouse=True)
def daemon_identity(monkeypatch):
    """Store concurrency tests need a daemon identity, not kernel inspection."""
    pid = os.getpid()

    def current_start(asked):
        assert asked == pid
        return "Thu Oct  1 12:00:00 2026"

    monkeypatch.setattr(procs, "boot_id", lambda: "00000000-0000-4000-8000-000000000001")
    monkeypatch.setattr(procs, "proc_start", current_start)


def lease(tx, key: str) -> None:
    tx.execute("INSERT INTO leases VALUES (?,?,?,NULL)", (key, "holder", "2026-09-25T00:00:00Z"))


def keys(store) -> list[str]:
    return [row["lease_key"] for row in store.query("SELECT lease_key FROM leases ORDER BY lease_key")]


@pytest.fixture
def store(tmp_path):
    handle = Store(tmp_path / "state.sqlite3", readers=3)
    yield handle
    handle.close()


class OpenTransaction:
    """A transaction held open on another thread until `finish()`."""

    def __init__(self, store, key: str = "uncommitted"):
        self.store, self.key = store, key
        self.opened, self.release = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._run)

    def _run(self):
        with self.store.transaction("test.open") as tx:
            lease(tx, self.key)
            self.opened.set()
            self.release.wait(10)

    def __enter__(self):
        self.thread.start()
        assert self.opened.wait(5)
        return self

    def finish(self):
        self.release.set()
        self.thread.join(5)

    def __exit__(self, *exc):
        self.finish()


def test_a_read_does_not_wait_for_an_open_transaction(store):
    with store.transaction("test.seed") as tx:
        lease(tx, "committed")
    with OpenTransaction(store) as open_tx:
        started = time.monotonic()
        assert keys(store) == ["committed"]                 # the committed state, at once
        assert store.one("SELECT count(*) n FROM leases")["n"] == 1
        assert time.monotonic() - started < 2
        open_tx.finish()
    assert keys(store) == ["committed", "uncommitted"]       # read after the commit sees it


def test_a_transaction_reads_its_own_uncommitted_rows(store):
    with store.transaction("test.own") as tx:
        lease(tx, "mine")
        assert keys(store) == ["mine"]
        assert store.one("SELECT lease_key FROM leases")["lease_key"] == "mine"
        with store.snapshot():                              # inside a transaction: that transaction
            assert keys(store) == ["mine"]


def test_a_snapshot_is_one_committed_state_and_takes_no_store_lock(store):
    committed = threading.Event()

    def commit():
        with store.transaction("test.between") as tx:
            lease(tx, "between")
        committed.set()

    with store.snapshot():
        assert keys(store) == []
        writer = threading.Thread(target=commit)
        writer.start()
        assert committed.wait(5)                            # the snapshot did not hold the writer back
        assert keys(store) == []                            # and does not see what it committed
        assert store.one("SELECT count(*) n FROM leases")["n"] == 0
        writer.join(5)
    assert keys(store) == ["between"]


def test_a_snapshot_proceeds_while_a_transaction_is_open(store):
    with OpenTransaction(store):
        started = time.monotonic()
        with store.snapshot():
            assert keys(store) == []
        assert time.monotonic() - started < 2


def test_nested_snapshots_share_the_outer_one(store):
    with store.snapshot():
        outer = store._local.snapshot
        with store.snapshot():
            assert store._local.snapshot is outer
        assert store._local.snapshot is outer
    assert getattr(store._local, "snapshot", None) is None


def test_read_connections_are_bounded_and_reused(store):
    errors = []

    def reader():
        try:
            for _ in range(50):
                store.query("SELECT * FROM leases")
                store.one("SELECT 1 AS x")
        except Exception as exc:                            # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=reader) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    assert 1 <= len(store._readers) <= 3
    assert store._idle.qsize() == len(store._readers)       # every one returned


def test_a_read_connection_refuses_writes(store):
    with pytest.raises(sqlite3.OperationalError):
        store.query("INSERT INTO leases VALUES ('x','y','2026-09-25T00:00:00Z',NULL)")
    assert keys(store) == []


def test_a_store_without_readers_reads_under_the_lock(tmp_path):
    plain = Store(tmp_path / "plain.sqlite3")
    try:
        assert not plain._reads_elsewhere()
        with plain.snapshot():                              # the lock stands in for the snapshot
            assert plain._holds_writer()
            assert plain.query("SELECT count(*) n FROM leases") == [{"n": 0}]
        assert not plain._holds_writer()
        assert plain._readers == []
    finally:
        plain.close()


def test_a_read_only_store_opens_no_readers(tmp_path):
    Store(tmp_path / "s.sqlite3").close()
    ro = Store(tmp_path / "s.sqlite3", read_only=True, readers=4)
    try:
        assert ro._max_readers == 0
        assert ro.query("SELECT count(*) n FROM leases") == [{"n": 0}]
    finally:
        ro.close()


def test_close_closes_the_read_connections(tmp_path):
    handle = Store(tmp_path / "state.sqlite3", readers=2)
    handle.query("SELECT 1")
    readers = list(handle._readers)
    handle.close()
    for conn in readers:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_the_daemon_answers_reads_while_a_transaction_is_open(tmp_path):
    daemon = Daemon(tmp_path / "state")
    try:
        assert daemon.store._max_readers == READ_CONNECTIONS
        with OpenTransaction(daemon.store, "lane:held"):
            started = time.monotonic()
            assert daemon.dispatch("list", {"mine": "someone", "running": True}) == {"jobs": []}
            assert daemon.dispatch("notice.pending", {"session_id": "someone"}) == {"notices": []}
            assert daemon.dispatch("lanes", {})["leases"] == []   # the open transaction's row is not visible
            assert time.monotonic() - started < 5
    finally:
        daemon.close()


# --- C-3.7: no transaction inside a snapshot on the same thread (review of 5841d8b, finding 4) ---

@pytest.mark.parametrize("readers", [0, 3])
def test_a_transaction_inside_a_snapshot_is_refused_before_it_writes(tmp_path, readers):
    """A transaction's reads inside a snapshot went to the snapshot and missed its own
    writes, so `add_artifact` recorded a duplicate. It is now refused, with or without
    read connections, so code that passes on one store does not misbehave on the other."""
    handle = Store(tmp_path / "state.sqlite3", readers=readers)
    try:
        with handle.snapshot():
            with pytest.raises(SnapshotWriteError, match="inside a read snapshot"):
                with handle.transaction("test.inside") as tx:
                    lease(tx, "never")
            with pytest.raises(SnapshotWriteError):
                handle.add_artifact("a1", "output", "/x.md", "0" * 64, 1)
            with handle.snapshot():                              # nested: the same refusal
                with pytest.raises(SnapshotWriteError):
                    handle.add_event("test.inside")
        assert keys(handle) == []
        assert handle.query("SELECT count(*) n FROM artifacts") == [{"n": 0}]
        assert handle.query("SELECT count(*) n FROM events WHERE kind LIKE 'test.%'") == [{"n": 0}]
        with handle.transaction("test.after") as tx:            # after the snapshot, as ever
            lease(tx, "after")
        assert keys(handle) == ["after"]
    finally:
        handle.close()


def test_a_snapshot_inside_a_transaction_still_reads_the_transaction(store):
    """The other nesting is allowed: the snapshot is the transaction's own state."""
    with store.transaction("test.outer") as tx:
        lease(tx, "outer")
        with store.snapshot():
            assert keys(store) == ["outer"]
            with store.transaction("test.nested") as nested:     # a savepoint of the outer one
                lease(nested, "nested")
            assert keys(store) == ["nested", "outer"]
    assert keys(store) == ["nested", "outer"]


def test_another_threads_commit_during_a_snapshot_is_how_a_race_arrives(store):
    """What the admission tests' `race()` now does: commit from another thread."""
    done = []

    def commit():
        with store.transaction("test.race") as tx:
            lease(tx, "raced")
        done.append(True)
    with store.snapshot():
        assert keys(store) == []
        other = threading.Thread(target=commit)
        other.start()
        other.join(5)
        assert done == [True] and keys(store) == []
    assert keys(store) == ["raced"]


# --- C-3.7: the read pool (review of 5841d8b, finding 2) ---------------------------------------

class Holding:
    """Threads each holding a snapshot (or a one-statement read) open until `finish()`."""

    def __init__(self, store, count: int, kind: str = "snapshot"):
        self.store, self.kind = store, kind
        self.inside, self.release, self.errors = [], threading.Event(), []
        self.threads = [threading.Thread(target=self._run, name=f"hold-{kind}-{n}") for n in range(count)]

    def _run(self):
        try:
            if self.kind == "snapshot":
                with self.store.snapshot():
                    self.store.one("SELECT count(*) n FROM leases")
                    self.inside.append(time.monotonic())
                    self.release.wait(30)
            else:
                with self.store._reading() as conn:
                    conn.execute("SELECT 1").fetchone()
                    self.inside.append(time.monotonic())
                    self.release.wait(30)
        except Exception as exc:                            # noqa: BLE001
            self.errors.append(exc)

    def __enter__(self):
        for thread in self.threads:
            thread.start()
        deadline = time.monotonic() + 20
        while len(self.inside) < len(self.threads) and time.monotonic() < deadline and not self.errors:
            time.sleep(.01)
        assert not self.errors and len(self.inside) == len(self.threads), (self.errors, len(self.inside))
        return self

    def __exit__(self, *exc):
        self.release.set()
        for thread in self.threads:
            thread.join(10)
        assert not self.errors


def watched(store):
    from subfleet.lockwatch import LockWatch
    lines = []
    watch = LockWatch(lines.append, every_s=0)               # every report, no rate limit
    watch.add(store._lock)
    return lines


def test_more_snapshots_than_connections_leave_one_statement_reads_a_connection(tmp_path):
    """Ten snapshots held open on a pool of six: four hold pooled connections, six
    wait `read_wait_s` and open their own; a one-statement read, as a hook, `ping`,
    the wait hub or the control loop makes, still answers at once from the two
    connections no snapshot may take. Before: the seventh reader waited, unbounded
    and unlogged, for a snapshot to end (1.53 s in the review's probe)."""
    store = Store(tmp_path / "state.sqlite3", readers=6, read_wait_s=.3)
    lines = watched(store)
    try:
        with Holding(store, 10):
            pool = store.read_pool()
            assert pool["in_use"] == 10 and pool["snapshots"] == 10 and pool["own_connections"] == 6
            assert pool["open"] <= 6
            before = dict(store.pool_waits)
            started = time.monotonic()
            assert store.one("SELECT count(*) n FROM leases") == {"n": 0}
            assert store.query("SELECT 1 AS one") == [{"one": 1}]
            assert time.monotonic() - started < .25                 # well under read_wait_s: no wait at all
            assert store.pool_waits == before                         # and from the pool, not its own
        assert len([line for line in lines if "for a snapshot and, none free after 0.3 s, opened one of its own"
                    in line]) == 6, lines
        assert all("at most 4 of the" in line for line in lines if "read connection" in line)
    finally:
        store.close()
    assert store._in_use == {}


def test_no_read_waits_for_a_connection_longer_than_the_bound(tmp_path):
    """Every pooled connection busy with statements: the next read waits `read_wait_s`,
    opens its own, says so, and closes it after."""
    store = Store(tmp_path / "state.sqlite3", readers=3, read_wait_s=.2)
    lines = watched(store)
    try:
        with Holding(store, 3, kind="statement"):
            started = time.monotonic()
            assert store.one("SELECT 7 AS n") == {"n": 7}
            waited = time.monotonic() - started
            assert .15 <= waited < 2, waited
            assert store.read_pool()["in_use"] == 3                 # its own connection is closed again
            assert store.pool_waits["own_connections"] == 1 and store.pool_waits["waits"] == 1
        assert any("waited" in line and "for a statement and, none free after 0.2 s, opened one of its own" in line
                   and "hold-statement-" in line for line in lines), lines
    finally:
        store.close()


def test_a_wait_the_pool_answers_is_logged_too(tmp_path):
    store = Store(tmp_path / "state.sqlite3", readers=1, read_wait_s=5)
    lines = watched(store)
    try:
        with Holding(store, 1, kind="statement") as holding:
            threading.Timer(.2, holding.release.set).start()
            started = time.monotonic()
            assert store.one("SELECT 1 AS n") == {"n": 1}
            assert .1 <= time.monotonic() - started < 4
        assert store.pool_waits["waits"] == 1 and store.pool_waits["own_connections"] == 0
        assert any("waited" in line and "for a statement;" in line for line in lines), lines
    finally:
        store.close()


def test_close_waits_for_a_read_in_progress_and_refuses_later_ones(tmp_path):
    """Review of 5841d8b: `close` closed read connections still in use."""
    store = Store(tmp_path / "state.sqlite3", readers=2)
    with store.transaction("test.seed") as tx:
        lease(tx, "a")
    results, entered, go_on = [], threading.Event(), threading.Event()

    def reader():
        try:
            with store.snapshot():
                results.append(keys(store))
                entered.set()
                go_on.wait(10)
                results.append(keys(store))              # after close began: still its connection
        except Exception as exc:                          # noqa: BLE001
            results.append(exc)
    thread = threading.Thread(target=reader)
    thread.start()
    assert entered.wait(10)
    threading.Timer(.3, go_on.set).start()
    started = time.monotonic()
    store.close()
    assert time.monotonic() - started >= .2                # it waited for the read
    thread.join(10)
    assert results == [["a"], ["a"]]
    with pytest.raises(sqlite3.ProgrammingError):
        store.query("SELECT 1")
    assert store._in_use == {} and store._readers == []


def test_close_does_not_wait_forever_for_a_read(tmp_path, monkeypatch):
    from subfleet import store as store_module
    monkeypatch.setattr(store_module, "CLOSE_WAIT_S", .2)
    store = Store(tmp_path / "state.sqlite3", readers=2)
    with Holding(store, 1) as holding:
        started = time.monotonic()
        store.close()
        assert time.monotonic() - started < 3
        assert len(store._in_use) == 1                     # still reading, on its own open connection
        holding.release.set()
    assert store._in_use == {}


def test_a_read_opening_a_pooled_connection_as_the_store_closes_is_refused_and_closes_it(tmp_path, monkeypatch):
    """Review of 20435d2: a read that was opening a new pooled connection when `close`
    ran (it is not yet in use, so `close` does not wait for it) found its place in
    the pool gone and died of `ValueError: list.index(x): x not in list`, leaving
    the connection it opened to the garbage collector. It is now refused as any
    read after `close` is, and closes that connection."""
    store = Store(tmp_path / "state.sqlite3", readers=2)
    real, opening, go_on, opened = store._open_reader, threading.Event(), threading.Event(), []

    def slow_open():
        opening.set()
        assert go_on.wait(10)
        opened.append(real())
        return opened[-1]
    monkeypatch.setattr(store, "_open_reader", slow_open)
    outcome = []

    def reader():
        try:
            outcome.append(store.query("SELECT 1 AS one"))
        except Exception as exc:                            # noqa: BLE001
            outcome.append(exc)
    thread = threading.Thread(target=reader)
    thread.start()
    assert opening.wait(10)
    started = time.monotonic()
    store.close()
    assert time.monotonic() - started < 2                   # nothing was in use to wait for
    go_on.set()
    thread.join(10)
    assert len(outcome) == 1 and isinstance(outcome[0], sqlite3.ProgrammingError), outcome
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")                       # closed, not left to the collector
    assert store._in_use == {} and store._readers == []


def test_snapshots_give_back_the_slot_they_take_on_every_path(tmp_path):
    """Review of 20435d2: nothing failed when a snapshot kept its slot. Snapshots on a
    pool of three share one slot, so a slot kept by any snapshot makes every later
    one wait `read_wait_s` and open its own connection, which `pool_waits` counts.
    The paths: a snapshot that ends, one whose block raises, and one that took the
    slot but found every pooled connection busy and opened its own."""
    store = Store(tmp_path / "state.sqlite3", readers=3, read_wait_s=.1)
    try:
        assert store.snapshot_share == 1

        def snapshots(count: int) -> None:
            for _ in range(count):
                with store.snapshot():
                    store.one("SELECT count(*) n FROM leases")
        snapshots(4)
        for _ in range(3):
            with pytest.raises(ZeroDivisionError), store.snapshot():
                store.one("SELECT 1 AS n")
                1 / 0
        snapshots(4)
        assert store.pool_waits["waits"] == 0, store.pool_waits
        with Holding(store, 3, kind="statement"):         # every pooled connection busy
            snapshots(1)                                    # the slot, then its own connection
        assert store.pool_waits["waits"] == 1 and store.pool_waits["own_connections"] == 1, store.pool_waits
        snapshots(4)
        assert store.pool_waits["waits"] == 1, store.pool_waits
        assert store.read_pool()["in_use"] == 0
    finally:
        store.close()


def test_capacity_views_hold_no_read_connection_while_they_build(tmp_path, monkeypatch):
    """Twelve views at once, each build slowed to 0.4 s: the reads are done in a
    snapshot, the builds after it, so a hook's `list` and `notice.pending` and a
    `ping` answer at once throughout, and no build holds a read connection."""
    from subfleet import capacity
    daemon = Daemon(tmp_path / "state")
    real, during = capacity.build_view, []

    def slow_build(*args, **kwargs):
        me = threading.get_ident()
        during.append(any(ident == me for ident, _, _ in daemon.store.read_holds()))
        time.sleep(.4)
        return real(*args, **kwargs)
    monkeypatch.setattr(capacity, "build_view", slow_build)
    try:
        builders = [threading.Thread(target=daemon._capacity_view) for _ in range(12)]
        for builder in builders:
            builder.start()
        while len(during) < 6:
            time.sleep(.01)
        worst = 0.0
        for _ in range(10):
            started = time.monotonic()
            assert daemon.dispatch("list", {"mine": "someone", "running": True}) == {"jobs": []}
            assert daemon.dispatch("notice.pending", {"session_id": "someone"}) == {"notices": []}
            assert daemon.dispatch("ping", {})["pong"] is True
            worst = max(worst, time.monotonic() - started)
        for builder in builders:
            builder.join(30)
        assert worst < 1.0, worst                              # READ_WAIT_S is 1 s: nothing waited it out
        assert during == [False] * 12                          # no build held a read connection
        assert daemon.store.read_pool()["in_use"] == 0
    finally:
        daemon.close()


# --- C-3.7: the write-ahead log stays bounded (review of 5841d8b, low finding) ---------------

def wal_size(path) -> int:
    wal = path.with_name(path.name + "-wal")
    return wal.stat().st_size if wal.exists() else 0


def test_the_writer_limits_the_wal_it_leaves_behind(store):
    from subfleet.store import WAL_SIZE_LIMIT
    assert store.connection.execute("PRAGMA journal_size_limit").fetchone()[0] == WAL_SIZE_LIMIT
    assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_long_snapshot_holds_the_wal_back_without_holding_a_commit_and_it_shrinks_after(tmp_path, monkeypatch):
    """Checkpoints are SQLite's automatic, PASSIVE ones: a commit never waits for a
    reader, it only checkpoints short of the oldest open snapshot. When that
    snapshot ends, the next commits checkpoint everything, restart the log and cut
    the file back to `WAL_SIZE_LIMIT`."""
    from subfleet import store as store_module
    monkeypatch.setattr(store_module, "WAL_SIZE_LIMIT", 256 * 1024)
    path = tmp_path / "state.sqlite3"
    handle = Store(path, readers=2)
    pad = "x" * 1024
    try:
        with Holding(handle, 1):                             # a snapshot, open, with its first read done
            worst = 0.0
            for n in range(2500):
                started = time.monotonic()
                with handle.transaction("test.grow") as tx:
                    tx.execute("INSERT INTO leases VALUES (?,?,?,NULL)", (f"k{n}", pad, "t"))
                worst = max(worst, time.monotonic() - started)
            held_back = wal_size(path)
            assert held_back > 2 * 1024 * 1024, held_back    # past 1000 pages: the checkpoint stopped short
            assert worst < 2.0, worst                          # no commit waited for the reader
        for n in range(3):
            with handle.transaction("test.after") as tx:
                tx.execute("INSERT INTO leases VALUES (?,?,?,NULL)", (f"after{n}", "y", "t"))
        assert wal_size(path) <= 256 * 1024 + 64 * 1024, wal_size(path)
    finally:
        handle.close()


def test_a_long_read_is_reported_once_with_its_stack(store):
    from subfleet.lockwatch import LockWatch
    lines = []
    watch = LockWatch(lines.append, hold_s=.1, every_s=0)
    watch.watch_reads("store", store.read_holds)
    with Holding(store, 1):
        time.sleep(.2)
        watch.sample()
        watch.sample()                                          # the same hold: once
    reports = [line for line in lines if "read connection held" in line]
    assert len(reports) == 1, lines
    assert "hold-snapshot-0" in reports[0] and "for a snapshot" in reports[0]
    assert "no checkpoint of the write-ahead log passes it" in reports[0] and "_run" in reports[0]
    watch.sample()
    assert watch._reads_sampled == set()                        # forgotten once it ends
