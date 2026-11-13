"""C-3.8: opt-in, bounded, cancellable free-page maintenance."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from subfleet import store as store_module
from subfleet.store import INCREMENTAL_VACUUM_PAGES, INCREMENTAL_VACUUM_THRESHOLD, Store
from subfleet.timers import Timers

SEED_ROWS = 64
PAYLOAD_BYTES = 128 * 1024


@pytest.fixture(autouse=True)
def remove_synthetic_stores(tmp_path):
    yield
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / ("state.sqlite3" + suffix)).unlink(missing_ok=True)


def open_store(tmp_path, *, incremental=True):
    path = tmp_path / "state.sqlite3"
    if incremental:
        with sqlite3.connect(path) as conn:
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    return Store(path)


def seed_free_pages(store, *, rows=SEED_ROWS):
    """A synthetic table with scattered free pages and live payloads to move."""
    if shutil.disk_usage(store.path.parent).free < 55_000_000_000 + rows * PAYLOAD_BYTES * 3:
        pytest.skip("synthetic store would leave less than 55 GB free")
    conn = store.connection
    conn.execute("CREATE TABLE vacuum_probe(id INTEGER PRIMARY KEY,payload BLOB)")
    conn.execute("BEGIN IMMEDIATE")
    # A small number of overflow-page payloads keeps the free-page pressure
    # real without thousands of Python/SQLite round trips on a loaded host.
    conn.executemany("INSERT INTO vacuum_probe VALUES (?,?)", ((i, bytes([i % 256]) * PAYLOAD_BYTES) for i in range(rows)))
    conn.execute("COMMIT")
    conn.execute("DELETE FROM vacuum_probe WHERE id % 4 != 0")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return conn.execute("PRAGMA freelist_count").fetchone()[0]


def digest(store):
    result = hashlib.sha256()
    for row in store.connection.execute("SELECT id,payload FROM vacuum_probe ORDER BY id"):
        result.update(str(row[0]).encode())
        result.update(row[1])
    return result.hexdigest()


def test_none_store_issues_no_maintenance_statement(tmp_path):
    """Invariant 7 / mutant 5: NONE maintenance changes no SQL behavior."""
    with open_store(tmp_path, incremental=False) as store:
        seen = []
        store.connection.set_trace_callback(seen.append)
        assert store.incremental_vacuum() == 0
        assert seen == []


def test_readonly_store_issues_no_maintenance_statement(tmp_path):
    with open_store(tmp_path):
        pass
    with Store(tmp_path / "state.sqlite3", read_only=True) as store:
        seen = []
        store.connection.set_trace_callback(seen.append)
        assert store.incremental_vacuum() == 0
        assert seen == []


def test_below_threshold_does_not_take_a_write_lock(tmp_path):
    with open_store(tmp_path) as store:
        seed_free_pages(store, rows=32)
        seen = []
        store.connection.set_trace_callback(seen.append)
        assert store.incremental_vacuum() == 0
        assert seen == ["PRAGMA freelist_count"]


def test_each_step_reclaims_at_most_128_pages(tmp_path, monkeypatch):
    """Invariant 6 / mutant 6: an unbounded vacuum exceeds the page budget."""
    monkeypatch.setattr(store_module, "INCREMENTAL_VACUUM_BUDGET_S", 10)
    with open_store(tmp_path) as store:
        free = seed_free_pages(store)
        assert free > INCREMENTAL_VACUUM_THRESHOLD + INCREMENTAL_VACUUM_PAGES
        before_digest = digest(store)
        generation = store.generation
        seen = []
        store.connection.set_trace_callback(seen.append)
        reclaimed = store.incremental_vacuum()
        # Check the page limit independently of host latency: even an
        # unbounded operation rolled back by its deadline must fail here.
        assert seen.count("PRAGMA incremental_vacuum(128)") == 1
        assert 0 < reclaimed <= INCREMENTAL_VACUUM_PAGES == 128
        assert free - store.connection.execute("PRAGMA freelist_count").fetchone()[0] == reclaimed
        assert seen.count("BEGIN IMMEDIATE") == 1
        assert seen.count("COMMIT") == 1
        assert digest(store) == before_digest
        assert store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert store.generation == generation


def test_further_deletes_shrink_the_file_through_passive_checkpoints(tmp_path, monkeypatch):
    """Invariant 5: repeated steps return free pages without changing rows."""
    monkeypatch.setattr(store_module, "INCREMENTAL_VACUUM_BUDGET_S", 10)
    with open_store(tmp_path) as store:
        free = seed_free_pages(store)
        original_size = store.path.stat().st_size
        expected = digest(store)
        reclaimed = 0
        steps = (free - INCREMENTAL_VACUUM_THRESHOLD + INCREMENTAL_VACUUM_PAGES - 1) // INCREMENTAL_VACUUM_PAGES
        for _ in range(steps):
            reclaimed += store.incremental_vacuum()
        # The timer's PASSIVE checkpoint returns pages even when there were
        # too few WAL frames to trigger an automatic checkpoint.
        assert store.path.stat().st_size < original_size
        assert reclaimed >= free - INCREMENTAL_VACUUM_THRESHOLD
        assert store.connection.execute("PRAGMA freelist_count").fetchone()[0] <= INCREMENTAL_VACUUM_THRESHOLD
        assert digest(store) == expected


def test_checkpoint_runs_after_releasing_the_writer_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "INCREMENTAL_VACUUM_BUDGET_S", 10)
    with open_store(tmp_path) as store:
        seed_free_pages(store)
        reached, resume, wrote = threading.Event(), threading.Event(), threading.Event()

        def checkpoint():
            assert not store._holds_writer()
            reached.set()
            assert resume.wait(5)

        monkeypatch.setattr(store, "_checkpoint_incremental_vacuum", checkpoint)
        vacuum = threading.Thread(target=store.incremental_vacuum)
        vacuum.start()
        try:
            assert reached.wait(5)

            def writer():
                with store.transaction():
                    store.connection.execute("INSERT INTO leases VALUES ('checkpoint','writer','now',NULL)")
                wrote.set()

            writer_thread = threading.Thread(target=writer)
            writer_thread.start()
            writer_thread.join(5)
            assert wrote.is_set()
        finally:
            resume.set()
            vacuum.join(5)
        assert not vacuum.is_alive()


def test_busy_store_writer_is_skipped(tmp_path):
    with open_store(tmp_path) as store:
        opened, release = threading.Event(), threading.Event()

        def writer():
            with store.transaction():
                opened.set()
                assert release.wait(5)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            assert opened.wait(5)
            started = time.monotonic()
            assert store.incremental_vacuum() == 0
            assert time.monotonic() - started < 1
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive()


def test_busy_sqlite_writer_is_skipped_and_settings_are_restored(tmp_path):
    with open_store(tmp_path) as store:
        free = seed_free_pages(store)
        with sqlite3.connect(store.path, isolation_level=None) as other:
            other.execute("BEGIN IMMEDIATE")
            try:
                started = time.monotonic()
                assert store.incremental_vacuum() == 0
                assert time.monotonic() - started < 1
            finally:
                other.rollback()
        assert store.connection.execute("PRAGMA freelist_count").fetchone()[0] == free
        assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert not store.connection.in_transaction


def test_cancelled_step_issues_no_statement(tmp_path):
    with open_store(tmp_path) as store:
        cancel = threading.Event()
        cancel.set()
        seen = []
        store.connection.set_trace_callback(seen.append)
        assert store.incremental_vacuum(cancel=cancel) == 0
        assert seen == []


class ConnectionProxy:
    def __init__(self, connection, before_progress):
        self.connection, self.before_progress = connection, before_progress

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def set_progress_handler(self, handler, instructions):
        if handler is None:
            return self.connection.set_progress_handler(None, instructions)

        def progress():
            self.before_progress()
            return handler()

        return self.connection.set_progress_handler(progress, instructions)


def test_vm_deadline_rolls_back_and_restores_connection(tmp_path, monkeypatch):
    with open_store(tmp_path) as store:
        free = seed_free_pages(store)
        expected = digest(store)
        clock = [100.0]
        monkeypatch.setattr(store_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        store.connection = ConnectionProxy(store.connection, lambda: clock.__setitem__(0, 101.0))
        assert store.incremental_vacuum() == 0
        assert store.connection.execute("PRAGMA freelist_count").fetchone()[0] == free
        assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert not store.connection.in_transaction
        assert digest(store) == expected
        # The progress handler was removed; subsequent SQL is usable.
        assert store.connection.execute("SELECT count(*) FROM vacuum_probe").fetchone()[0] == SEED_ROWS // 4


def test_close_interrupts_a_step_before_closing_the_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "INCREMENTAL_VACUUM_BUDGET_S", 10)
    store = open_store(tmp_path)
    seed_free_pages(store)
    reached, resume = threading.Event(), threading.Event()

    def pause():
        reached.set()
        assert resume.wait(5)

    store.connection = ConnectionProxy(store.connection, pause)
    results, errors = [], []

    def vacuum():
        try:
            results.append(store.incremental_vacuum())
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=vacuum)
    closer = threading.Thread(target=store.close)
    worker.start()
    try:
        assert reached.wait(5)
        closer.start()
        assert store._closing.wait(5)
    finally:
        resume.set()
        worker.join(5)
        if closer.ident is not None:
            closer.join(5)
        else:
            store.close()
    assert not worker.is_alive() and not closer.is_alive()
    assert errors == [] and results == [0]
    # Closing releases every connection after the interrupted transaction.
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM vacuum_probe").fetchone()[0] == SEED_ROWS // 4


def test_probe_cycle_runs_one_step_before_provider_work():
    calls = []
    timer = Timers.__new__(Timers)
    timer.cancel = threading.Event()
    timer.store = SimpleNamespace(incremental_vacuum=lambda **options: calls.append(options))

    def stop_before_providers():
        raise RuntimeError("provider work boundary")

    timer._identities = stop_before_providers
    with pytest.raises(RuntimeError, match="provider work boundary"):
        timer.probe_cycle()
    assert calls == [{"cancel": timer.cancel}]
