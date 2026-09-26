"""Properties of the store's read paths, over generated cases (C-3.7).

`hypothesis` is not a dependency of this checkout, so each property is a loop
over cases drawn from a seeded `random.Random`; a failure names its seed and
case, and re-running that seed reproduces it.

- No transaction inside a snapshot: on one thread, a transaction begun inside
  a `snapshot()` opened outside any transaction raises `SnapshotWriteError`
  and writes nothing; every other nesting is allowed, and every read made
  inside a transaction sees that transaction's own writes.
- The read pool's bounds: at most `readers` pooled connections exist, none is
  handed to two readers at once, snapshots hold at most `snapshot_share` of
  them, no checkout takes longer than `read_wait_s` (plus the time to open a
  connection), a read's own connection is closed when it ends, and every one
  it opened is counted; afterwards every pooled connection is idle.
"""

from __future__ import annotations

import random
import sqlite3
import threading
import time

import pytest

from subfleet.store import SnapshotWriteError, Store

CASES = 250


def program(rng: random.Random, depth: int = 0) -> list[tuple]:
    """A random block: snapshots, transactions, writes and reads, nested."""
    ops = []
    for _ in range(rng.randrange(1, 4)):
        roll = rng.random()
        if depth < 4 and roll < .25:
            ops.append(("snapshot", program(rng, depth + 1)))
        elif depth < 4 and roll < .5:
            ops.append(("transaction", program(rng, depth + 1)))
        elif roll < .75:
            ops.append(("write",))
        else:
            ops.append(("read",))
    return ops


class Model:
    """What the store must answer: committed rows, and each open transaction's own."""

    def __init__(self, store: Store):
        self.store, self.committed, self.pending, self.serial = store, 0, [], 0

    def count(self) -> int:
        return self.store.one("SELECT count(*) n FROM leases")["n"]

    def run(self, block: list[tuple], frames: tuple[str, ...], trail: list[str]) -> None:
        # frames: "S" a snapshot opened outside any transaction, "P" one opened
        # inside a transaction (it reads the transaction), "T" a transaction.
        for op in block:
            trail.append(op[0])
            in_transaction = "T" in frames
            if op[0] == "snapshot":
                with self.store.snapshot():
                    self.run(op[1], frames + ("P" if in_transaction else "S",), trail)
            elif op[0] == "transaction":
                if "S" in frames:
                    before = self.count()
                    with pytest.raises(SnapshotWriteError):
                        with self.store.transaction("test.refused") as tx:
                            tx.execute("INSERT INTO leases VALUES ('refused','x','t',NULL)")
                    assert self.count() == before, trail
                    continue
                self.pending.append(0)
                with self.store.transaction("test.property"):
                    self.run(op[1], frames + ("T",), trail)
                mine = self.pending.pop()
                if self.pending:
                    self.pending[-1] += mine                  # a savepoint joins its outer transaction
                else:
                    self.committed += mine
            elif op[0] == "write":
                if not in_transaction:
                    continue
                self.serial += 1
                self.store.connection.execute("INSERT INTO leases VALUES (?,?,?,NULL)",
                                              (f"k{self.serial}", "x", "t"))
                self.pending[-1] += 1
            else:
                expected = self.committed + (sum(self.pending) if in_transaction else 0)
                assert self.count() == expected, trail


@pytest.mark.parametrize("readers", [0, 2])
def test_no_transaction_begins_inside_a_snapshot_on_its_thread(tmp_path, readers):
    store = Store(tmp_path / "state.sqlite3", readers=readers)
    model = Model(store)
    try:
        for case in range(CASES):
            rng = random.Random(case)
            trail: list[str] = []
            try:
                model.run(program(rng), (), trail)
            except AssertionError as exc:
                raise AssertionError(f"case {case} ({readers} readers): {' '.join(trail)}") from exc
        assert model.count() == model.committed
        assert getattr(store._local, "in_snapshot", False) is False
    finally:
        store.close()


# --- the read pool's bounds -------------------------------------------------------

POOL_CASES = 24
#: Opening a connection and being scheduled again, on a loaded machine.
SLACK_S = 1.5
#: Longer than any bounded wait plus slack: an unbounded wait behind it shows.
LONG_HOLD_S = 2.5


class Tracker:
    """Wraps a store's checkout and checkin to watch the pool from outside."""

    def __init__(self, store: Store):
        self.lock = threading.Lock()
        self.pooled: set[int] = set()
        self.snapshots = self.max_snapshots = self.max_pooled = 0
        self.durations: list[float] = []
        self.own: list[sqlite3.Connection] = []
        self.double: list[int] = []
        checkout, checkin = store._checkout, store._checkin

        def tracked_checkout(kind):
            started = time.monotonic()
            conn, pooled = checkout(kind)
            with self.lock:
                self.durations.append(time.monotonic() - started)
                if pooled:
                    if id(conn) in self.pooled:
                        self.double.append(id(conn))
                    self.pooled.add(id(conn))
                    self.max_pooled = max(self.max_pooled, len(self.pooled))
                    if kind == "snapshot":
                        self.snapshots += 1
                        self.max_snapshots = max(self.max_snapshots, self.snapshots)
                else:
                    self.own.append(conn)
            return conn, pooled

        def tracked_checkin(conn, pooled, kind):
            with self.lock:
                if pooled:
                    self.pooled.discard(id(conn))
                    if kind == "snapshot":
                        self.snapshots -= 1
            checkin(conn, pooled, kind)
        store._checkout, store._checkin = tracked_checkout, tracked_checkin


def work(rng: random.Random, long_holds: bool) -> list[tuple]:
    ops = []
    for _ in range(rng.randrange(1, 6)):
        hold = LONG_HOLD_S if long_holds and rng.random() < .15 else rng.choice((0, 0, .001, .01, .05))
        ops.append((rng.choice(("snapshot", "snapshot", "statement")), hold, rng.randrange(1, 4)))
    return ops


def perform(store: Store, ops: list[tuple], errors: list) -> None:
    try:
        for kind, hold, reads in ops:
            if kind == "snapshot":
                with store.snapshot():
                    for _ in range(reads):
                        store.one("SELECT count(*) n FROM leases")
                    time.sleep(hold)
            else:
                with store._reading() as conn:
                    conn.execute("SELECT count(*) FROM leases").fetchone()
                    time.sleep(hold)
    except Exception as exc:                                 # noqa: BLE001
        errors.append(exc)


def test_the_read_pool_stays_within_its_bounds(tmp_path):
    for case in range(POOL_CASES):
        rng = random.Random(1000 + case)
        readers, wait = rng.choice((1, 2, 3, 6)), rng.choice((.05, .1, .2))
        store = Store(tmp_path / f"pool-{case}.sqlite3", readers=readers, read_wait_s=wait)
        tracker, errors = Tracker(store), []
        long_holds = case % 4 == 0
        threads = [threading.Thread(target=perform, args=(store, work(rng, long_holds), errors))
                   for _ in range(rng.randrange(1, 13))]
        where = f"case {case}: {readers} readers, wait {wait}, {len(threads)} threads"
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
            assert not errors and not any(thread.is_alive() for thread in threads), (where, errors)
            assert len(store._readers) <= readers, where
            assert tracker.max_pooled <= readers and not tracker.double, where
            assert tracker.max_snapshots <= store.snapshot_share, where
            assert max(tracker.durations) <= wait + SLACK_S, (where, max(tracker.durations))
            assert store.pool_waits["own_connections"] == len(tracker.own), where
            assert store.pool_waits["waits"] >= len(tracker.own), where
            assert store._in_use == {} and store._idle.qsize() == len(store._readers), where
            for conn in tracker.own:                          # each read's own connection is closed
                with pytest.raises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")
        finally:
            store.close()
