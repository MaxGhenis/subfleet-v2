"""Properties of the store's read paths, over generated cases (C-3.7).

`hypothesis` is not a dependency of this checkout, so each property is a loop
over cases drawn from a seeded `random.Random`; a failure names its seed and
case, and re-running that seed reproduces it.

- No transaction inside a snapshot: on one thread, a transaction begun inside
  a `snapshot()` opened outside any transaction raises `SnapshotWriteError`
  and writes nothing; every other nesting is allowed, and every read made
  inside a transaction sees that transaction's own writes.
"""

from __future__ import annotations

import random

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
