"""C-3.6: the daemon says who holds a store lock too long, and who waits too long.

On 2026-09-25 a `list` whose SQL takes 1 ms took 34-94 s in the daemon; a
native sample showed 18 threads waiting on the store's RLock and could not say
which Python code held it. These tests pin what `subfleet.lockwatch` reports:
a hold past `hold_s` once, with the stack its holder is running at that
moment; the end of the hold; a waiter past `wait_s`, with the holder's live
stack; and no more than one line per lock and kind in a window, with the rest
counted. They also pin that the watched lock is still a correct re-entrant
lock, and that the daemon wires it to `daemon.log` along with SIGUSR1 dumps.
"""

from __future__ import annotations

import os
import signal
import threading
import time

import pytest

from subfleet import daemon as daemon_module
from subfleet import lockwatch
from subfleet.daemon import Daemon
from subfleet.lockwatch import LockWatch, WatchedLock
from subfleet.store import Store


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def watched(**options):
    lines: list[str] = []
    watch = LockWatch(lines.append, **{"hold_s": .05, "wait_s": .05, "every_s": 60,
                                       "sample_s": 3600, **options})
    lock = watch.add(WatchedLock("store"))
    return lock, watch, lines


def hold_in_marker_function(lock, holding: threading.Event, release: threading.Event):
    with lock:
        holding.set()
        release.wait(10)


def wait_in_marker_function(lock, got: threading.Event):
    with lock:
        got.set()


def holder(lock):
    holding, release = threading.Event(), threading.Event()
    thread = threading.Thread(target=hold_in_marker_function, args=(lock, holding, release),
                              name="test-holder")
    thread.start()
    assert holding.wait(5)
    return thread, release


def test_a_long_hold_is_reported_once_with_the_stack_it_is_running():
    lock, watch, lines = watched()
    thread, release = holder(lock)
    time.sleep(.1)
    watch.sample()
    watch.sample()                                   # the same hold: no second line
    release.set()
    thread.join(5)
    assert len(lines) == 2, lines
    held, released = lines
    assert held.startswith("store lock held ") and "by test-holder (" in held
    assert "none waiting" in held
    # The live stack: where the holder is now, not where the watch is.
    assert "hold_in_marker_function" in held and "release.wait(10)" in held
    assert "sample" not in held.split("Holder's stack:")[1]
    assert released.startswith("store lock released after ") and "by test-holder" in released
    # The sample already showed the stack; the end of the hold says only how long.
    assert "Where it was held" not in released


def test_a_long_hold_no_sample_caught_says_where_it_was_held_when_it_ends():
    lock, watch, lines = watched()
    thread, release = holder(lock)
    time.sleep(.1)
    release.set()
    thread.join(5)
    assert len(lines) == 1, lines
    assert lines[0].startswith("store lock released after ")
    assert "Where it was held" in lines[0] and "hold_in_marker_function" in lines[0]


def test_a_short_hold_is_not_reported():
    lock, watch, lines = watched(hold_s=5)
    with lock:
        watch.sample()
    assert lines == []


def test_a_long_wait_reports_the_waiter_the_count_and_the_holders_live_stack():
    lock, watch, lines = watched(hold_s=60)
    thread, release = holder(lock)
    got = threading.Event()
    waiter = threading.Thread(target=wait_in_marker_function, args=(lock, got), name="test-waiter")
    waiter.start()
    deadline = time.monotonic() + 5
    while not lines and time.monotonic() < deadline:
        time.sleep(.01)
    assert not got.is_set()                          # reporting does not hand over the lock
    release.set()
    thread.join(5)
    assert got.wait(5)
    waiter.join(5)
    assert lines, "the waiter never reported"
    line = lines[0]
    assert line.startswith("test-waiter (") and "has waited" in line and "for the store lock" in line
    assert "1 waiting, the longest" in line and "by test-holder (" in line
    waiter_part, holder_part = line.split("Holder's stack:")
    assert "wait_in_marker_function" in waiter_part
    assert "hold_in_marker_function" in holder_part and "release.wait(10)" in holder_part


def test_the_rate_limit_writes_one_line_per_window_and_counts_the_rest():
    clock = Clock()
    lock, watch, lines = watched(every_s=60, clock=clock)
    for seconds in (3.0, 7.5, 4.0):
        watch.released(lock, (1, 0.0), seconds)
    assert len(lines) == 1 and "released after 3.0 s" in lines[0]
    watch.sample()                                   # still inside the window: nothing
    assert len(lines) == 1
    clock.now += 61
    watch.sample()                                   # the window has passed: the summary
    assert lines[1] == ("2 released report(s) on the store lock held back in the last 60 s, "
                        "the longest 7.5 s")
    watch.released(lock, (1, 0.0), 2.5)             # a new window: nothing held back since
    clock.now += 61
    watch.released(lock, (1, 0.0), 9.0)
    assert len(lines) == 3 and "released after 9.0 s" in lines[2]
    assert lines[2].startswith("(1 more released report(s) held back in the last 60 s, the longest 2.5 s) ")


def test_kinds_and_locks_are_limited_separately():
    lock, watch, lines = watched()
    other = watch.add(WatchedLock("conversations"))
    watch.released(lock, (1, 0.0), 3)
    watch.released(other, (1, 0.0), 3)
    watch.waited(lock, 1, 6)
    assert len(lines) == 3


def test_the_lock_is_reentrant_and_keeps_the_outer_holds_start():
    lock = WatchedLock("store")
    with lock:
        first = lock.held
        assert first[0] == threading.get_ident()
        with lock:
            assert lock.held == first
        assert lock.held == first
    assert lock.held is None


def test_only_the_holder_may_release():
    lock = WatchedLock("store")
    with pytest.raises(RuntimeError):
        lock.release()
    thread, release = holder(lock)
    try:
        with pytest.raises(RuntimeError):
            lock.release()
    finally:
        release.set()
        thread.join(5)
    assert lock.acquire(blocking=False)
    lock.release()


def test_nonblocking_and_timed_acquire_still_work():
    lock, watch, lines = watched(wait_s=.02)
    thread, release = holder(lock)
    try:
        assert lock.acquire(blocking=False) is False
        started = time.monotonic()
        assert lock.acquire(timeout=.1) is False
        assert time.monotonic() - started >= .09
        assert lock.waiting == {}                    # a waiter that gave up is not counted
    finally:
        release.set()
        thread.join(5)
    assert lock.acquire(timeout=1)
    lock.release()


def test_a_lock_nobody_watches_reports_nothing_and_still_excludes():
    lock = WatchedLock("store")
    counter = {"n": 0}

    def bump():
        for _ in range(2000):
            with lock:
                value = counter["n"]
                counter["n"] = value + 1
    threads = [threading.Thread(target=bump) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert counter["n"] == 16000
    assert lock.held is None and lock.waiting == {}


def test_a_stopped_watch_detaches_from_its_locks():
    lock, watch, lines = watched()
    watch.start()
    watch.stop()
    assert lock.watch is None
    watch.stop()                                     # twice is harmless


def test_the_watch_thread_samples_on_its_own():
    lock, watch, lines = watched(sample_s=.02)
    watch.start()
    thread, release = holder(lock)
    try:
        deadline = time.monotonic() + 5
        while not lines and time.monotonic() < deadline:
            time.sleep(.01)
    finally:
        release.set()
        thread.join(5)
        watch.stop()
    assert lines and lines[0].startswith("store lock held ")


def test_the_store_serializes_on_a_watched_lock(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    try:
        assert isinstance(store._lock, WatchedLock)
        with store.transaction("test.outer") as tx:
            with store.transaction("test.inner"):
                assert store._lock.held[0] == threading.get_ident()
                tx.execute("INSERT INTO leases VALUES ('k','h','2026-09-25T00:00:00Z',NULL)")
        assert store._lock.held is None
        assert store.query("SELECT holder FROM leases") == [{"holder": "h"}]
    finally:
        store.close()


@pytest.fixture
def daemon(tmp_path):
    core = Daemon(tmp_path / "state")
    yield core
    core.close()


def log_text(core) -> str:
    core._log_handler.flush()
    return (core.root / "daemon.log").read_text(errors="replace")


def test_the_daemon_writes_long_store_holds_to_its_log(daemon, monkeypatch):
    watch = daemon.lock_watch
    assert daemon.store._lock.watch is watch
    assert daemon.conversations.store._lock.watch is watch
    watch.hold_s = .05
    with daemon.store.transaction("test.slow"):
        time.sleep(.1)
    text = log_text(daemon)
    assert "store lock released after" in text
    assert "test_the_daemon_writes_long_store_holds_to_its_log" in text


def test_sigusr1_writes_every_threads_stack_to_the_daemon_log(daemon):
    marker = threading.Event()

    def parked_in_a_marker_function():
        marker.wait(10)
    thread = threading.Thread(target=parked_in_a_marker_function, name="parked")
    thread.start()
    try:
        os.kill(os.getpid(), signal.SIGUSR1)
        deadline = time.monotonic() + 5
        while "parked_in_a_marker_function" not in log_text(daemon) and time.monotonic() < deadline:
            time.sleep(.02)
    finally:
        marker.set()
        thread.join(5)
    text = log_text(daemon)
    assert "parked_in_a_marker_function" in text
    assert "test_sigusr1_writes_every_threads_stack_to_the_daemon_log" in text
    assert "Thread 0x" in text


def test_a_closed_daemon_gives_up_sigusr1_and_a_newer_one_keeps_it(tmp_path):
    older = Daemon(tmp_path / "older")
    newer = Daemon(tmp_path / "newer")
    try:
        assert daemon_module._STACK_DUMPS() is newer
        older.close()
        assert daemon_module._STACK_DUMPS() is newer      # not the older one's to remove
    finally:
        newer.close()
    assert daemon_module._STACK_DUMPS is None


def test_the_defaults_are_the_contracts():
    assert (lockwatch.HOLD_REPORT_S, lockwatch.WAIT_REPORT_S) == (2.0, 5.0)
    assert lockwatch.REPORT_EVERY_S == 60.0


def test_timers_write_to_the_store_outside_their_own_lock(daemon, monkeypatch):
    """C-3.7: the control loop takes `Timers._lock` every tick, so a store write
    must never wait for the store lock while holding it."""
    timers = daemon.timers
    held = []
    add_event = daemon.store.add_event
    monkeypatch.setattr(daemon.store, "add_event", lambda *a, **k: held.append(
        timers._lock._is_owned()) or add_event(*a, **k))
    timers.mark("retention")
    timers.started = True
    timers.request("keepalive")
    assert held and not any(held)


def test_a_report_that_raises_never_fails_a_committed_transaction(tmp_path):
    """Review of 5841d8b: an error inside a lock-watch report could make
    `transaction()` raise after its rows were committed."""
    from subfleet.store import Store

    def broken(text):
        raise OSError("log gone")
    store = Store(tmp_path / "state.sqlite3", readers=2)
    watch = LockWatch(broken, hold_s=0, wait_s=.01, every_s=0)
    watch.add(store._lock)
    try:
        with store.transaction("test.reported") as tx:            # held >= hold_s: released() reports
            tx.execute("INSERT INTO leases VALUES ('k','h','t',NULL)")
        assert store.query("SELECT lease_key FROM leases") == [{"lease_key": "k"}]
        holding, go = threading.Event(), threading.Event()

        def hold():
            with store.transaction("test.hold"):
                holding.set()
                go.wait(5)
        holder = threading.Thread(target=hold)
        holder.start()
        assert holding.wait(5)
        threading.Timer(.1, go.set).start()
        with store.transaction("test.waited") as tx:              # waits past wait_s: waited() reports
            tx.execute("INSERT INTO leases VALUES ('k2','h','t',NULL)")
        holder.join(5)
        assert len(store.query("SELECT lease_key FROM leases")) == 2
    finally:
        store.close()
