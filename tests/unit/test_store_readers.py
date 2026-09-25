"""C-3.7: reads outside a transaction never wait for the store lock.

On 2026-09-25 a `list` whose statement takes 1 ms waited 34-94 s behind the
store's one lock, which every read and every transaction shared. A store with
read connections sends a read made outside a transaction to one of them; a
thread inside a transaction still reads through the writing connection, so it
sees its own rows; `snapshot()` gives several reads one committed state.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from subfleet.daemon import READ_CONNECTIONS, Daemon
from subfleet.store import Store


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
