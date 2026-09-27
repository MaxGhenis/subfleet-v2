"""`subfleet.pool.Pool`: a call starts at once while the pool has a thread to
spare, and waits, in order, only while every thread runs a call (C-16.1, C-16.5).

Invariants, for every sequence of submits and releases:
- at most `max_workers` calls run at once;
- a call submitted while fewer than `max_workers` calls are unfinished starts
  without any other call ending;
- a call submitted while `max_workers` are unfinished waits, and the calls that
  wait start in the order they were submitted, one as each running call ends;
- every call submitted runs exactly once, and none after `shutdown` cancels it.
"""

from __future__ import annotations

from collections import deque
import itertools
import sys
import threading
import time

from hypothesis import given, settings, strategies as st
import pytest

from subfleet.pool import Pool


BOUND = 5.0          # generous: the free-threaded build starts a thread in 50 ms at p99 under load


def threads_named(prefix: str) -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name.startswith(prefix + "_")]


class Held:
    """A call that says when it starts and returns when released."""

    def __init__(self, recorder: "Recorder", name: str, release: bool = False):
        self.recorder, self.name = recorder, name
        self.started, self.release = threading.Event(), threading.Event()
        if release:
            self.release.set()

    def __call__(self) -> str:
        self.recorder.enter(self.name)
        self.started.set()
        try:
            assert self.release.wait(30), f"{self.name} never released"
        finally:
            self.recorder.leave()
        return self.name


class Recorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = self.peak = 0
        self.starts: list[str] = []

    def enter(self, name: str) -> None:
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.starts.append(name)

    def leave(self) -> None:
        with self.lock:
            self.running -= 1


def test_a_call_starts_at_once_while_a_thread_is_free_or_can_start():
    recorder = Recorder()
    pool = Pool(3, "free")
    calls = [Held(recorder, f"c{i}") for i in range(3)]
    try:
        for call in calls:
            pool.submit(call)
            assert call.started.wait(BOUND), f"{call.name} waited with room in the pool"
        assert len(threads_named("free")) == 3
    finally:
        for call in calls:
            call.release.set()
        pool.shutdown(wait=True)
    assert not threads_named("free")


def test_calls_past_the_cap_wait_in_order_and_start_as_calls_end():
    recorder = Recorder()
    pool = Pool(2, "cap")
    calls = [Held(recorder, f"c{i}") for i in range(5)]
    futures = [pool.submit(call) for call in calls]
    try:
        assert calls[0].started.wait(BOUND) and calls[1].started.wait(BOUND)
        time.sleep(.1)
        assert not any(call.started.is_set() for call in calls[2:])
        for index in range(3):
            calls[index].release.set()
            assert futures[index].result(BOUND) == f"c{index}"
            assert calls[index + 2].started.wait(BOUND)
            assert not any(call.started.is_set() for call in calls[index + 3:])
    finally:
        for call in calls:
            call.release.set()
        pool.shutdown(wait=True)
    assert recorder.starts[2:] == ["c2", "c3", "c4"] and recorder.peak == 2


def test_threads_are_reused_rather_than_started_per_call():
    """An idle thread takes the next call. (One that arrives while the last thread
    is still between its call and idle starts another thread, rather than wait.)"""
    pool = Pool(8, "reuse")
    try:
        for i in range(50):
            assert pool.submit(lambda i=i: i).result(BOUND) == i
            limit = time.monotonic() + BOUND
            while len(pool._idle) != 1 and time.monotonic() < limit:   # noqa: SLF001 - its own state
                time.sleep(.001)
        assert len(threads_named("reuse")) == 1
    finally:
        pool.shutdown(wait=True)


def test_a_call_that_raises_hands_its_exception_to_its_future_and_the_thread_serves_on():
    pool = Pool(1, "raise")
    try:
        with pytest.raises(ZeroDivisionError):
            pool.submit(lambda: 1 / 0).result(BOUND)
        with pytest.raises(KeyboardInterrupt):
            pool.submit(lambda: (_ for _ in ()).throw(KeyboardInterrupt())).result(BOUND)
        assert pool.submit(lambda: "after").result(BOUND) == "after"
        assert len(threads_named("raise")) == 1
    finally:
        pool.shutdown(wait=True)


def test_a_thread_that_cannot_start_raises_and_leaves_nothing_queued(monkeypatch):
    """C-16.1: a reader refused for want of a thread must never run later. The
    standard pool queued the call before it tried to start a thread."""
    pool = Pool(4, "nothread")
    ran = threading.Event()
    try:
        monkeypatch.setattr(threading.Thread, "start", lambda self: (_ for _ in ()).throw(
            RuntimeError("can't start new thread")))
        with pytest.raises(RuntimeError, match="can't start new thread"):
            pool.submit(ran.set)
        monkeypatch.undo()
        assert pool.submit(lambda: "next").result(BOUND) == "next"
        time.sleep(.1)
        assert not ran.is_set()
    finally:
        monkeypatch.undo()
        pool.shutdown(wait=True)


def test_shutdown_cancels_what_waits_when_asked_and_lets_the_running_call_finish():
    recorder = Recorder()
    pool = Pool(1, "stop")
    running, queued = Held(recorder, "running"), Held(recorder, "queued")
    first, second = pool.submit(running), pool.submit(queued)
    assert running.started.wait(BOUND)
    done = threading.Event()
    stopper = threading.Thread(target=lambda: (pool.shutdown(wait=True, cancel_futures=True), done.set()))
    stopper.start()
    try:
        limit = time.monotonic() + BOUND
        while not second.cancelled() and time.monotonic() < limit:
            time.sleep(.01)
        assert second.cancelled(), "shutdown(cancel_futures=True) left a waiting call"
        with pytest.raises(RuntimeError, match="after shutdown"):
            pool.submit(lambda: None)
        assert not done.is_set(), "shutdown(wait=True) returned while a call ran"
    finally:
        running.release.set()
        stopper.join(BOUND)
    assert done.is_set() and first.result() == "running" and second.cancelled()
    assert recorder.starts == ["running"] and not threads_named("stop")


def test_shutdown_without_cancel_runs_what_waits():
    recorder = Recorder()
    pool = Pool(1, "drain")
    calls = [Held(recorder, f"c{i}", release=True) for i in range(4)]
    blocker = Held(recorder, "blocker")
    pool.submit(blocker)
    futures = [pool.submit(call) for call in calls]
    assert blocker.started.wait(BOUND)
    stopper = threading.Thread(target=pool.shutdown)
    stopper.start()
    blocker.release.set()
    stopper.join(BOUND)
    assert not stopper.is_alive() and [f.result(0) for f in futures] == ["c0", "c1", "c2", "c3"]


def test_a_done_callback_run_by_shutdown_may_call_the_pool():
    """Cancelled futures' callbacks run outside the pool's lock (a reader's
    `finish` takes the daemon's connection lock, and could submit)."""
    pool = Pool(1, "callback")
    gate = threading.Event()
    pool.submit(gate.wait, 30)
    queued = pool.submit(lambda: None)
    answers = []
    queued.add_done_callback(lambda _: answers.append(pytest.raises(RuntimeError, pool.submit, lambda: None)))
    stopper = threading.Thread(target=pool.shutdown, kwargs={"wait": False, "cancel_futures": True})
    stopper.start()
    stopper.join(BOUND)
    gate.set()
    pool.shutdown(wait=True)
    assert not stopper.is_alive() and queued.cancelled() and len(answers) == 1


def test_shutdown_from_one_of_its_own_threads_returns():
    pool = Pool(2, "self")
    assert pool.submit(pool.shutdown, wait=True).result(BOUND) is None


def test_max_workers_must_be_positive():
    with pytest.raises(ValueError):
        Pool(0, "zero")


OPS = st.lists(st.one_of(st.just(("hold",)), st.just(("quick",)), st.tuples(st.just("release"), st.integers(0, 7))),
               max_size=24)


@settings(max_examples=60, deadline=None)
@given(max_workers=st.integers(1, 4), ops=OPS)
def test_every_call_starts_while_the_pool_has_room_and_only_then(max_workers, ops):
    """The invariants above, against a model of the pool, after every step. A
    release lets a running call end while the next submit arrives, which is the
    moment the standard pool's idle count drifted."""
    recorder = Recorder()
    prefix = f"prop{next(_EXAMPLES)}"
    pool = Pool(max_workers, prefix)
    calls: dict[str, Held] = {}
    futures = {}
    running: list[str] = []       # the model: calls started and unfinished, and those waiting
    waiting: deque[str] = deque()
    names = itertools.count()

    def settle():
        """Quick calls end on their own: each frees its thread for the next waiting call."""
        while True:
            quick = [name for name in running if name.startswith("quick")]
            if not quick:
                return
            for name in quick:
                assert futures[name].result(BOUND) == name
                running.remove(name)
                if waiting:
                    running.append(waiting.popleft())

    try:
        for op in ops:
            if op[0] == "release":
                held = [name for name in running if name.startswith("hold")]
                if not held:
                    continue
                name = held[op[1] % len(held)]
                calls[name].release.set()
                assert futures[name].result(BOUND) == name
                running.remove(name)
                if waiting:
                    running.append(waiting.popleft())
            else:
                name = f"{op[0]}{next(names)}"
                calls[name] = Held(recorder, name, release=op[0] == "quick")
                futures[name] = pool.submit(calls[name])
                (running if len(running) < max_workers else waiting).append(name)
            settle()
            for name in running:
                assert calls[name].started.wait(BOUND), f"{name} waited while the pool had room"
            assert not [name for name in waiting if calls[name].started.is_set()], "a call started past the cap"
            assert recorder.peak <= max_workers
    finally:
        for call in calls.values():
            call.release.set()
        pool.shutdown(wait=True)
    assert sorted(recorder.starts) == sorted(calls) and len(recorder.starts) == len(set(recorder.starts))
    assert all(future.result(0) == name for name, future in futures.items())
    assert not threads_named(prefix)


_EXAMPLES = itertools.count()


def test_a_call_never_waits_while_the_pool_has_room_under_churn():
    """Black-box race check. Each round a call ends while held calls (as connection
    readers are) arrive, then one more call must start. The same rounds strand a
    call in the standard pool within seconds on the free-threaded build, and on the
    GIL build with a 1 us switch interval, set here so its threads interleave there
    too (2026-09-27, `docs/reports/2026-09-27-free-threaded-ping-starvation.md`)."""
    interval = sys.getswitchinterval()
    if getattr(sys, "_is_gil_enabled", lambda: True)():
        sys.setswitchinterval(1e-6)
    try:
        limit = time.monotonic() + 8
        rounds = 0
        while rounds < 150 and time.monotonic() < limit:
            rounds += 1
            pool = Pool(64, "churn")
            release, started = threading.Event(), threading.Event()
            try:
                pool.submit(lambda: None)
                for _ in range(8):
                    pool.submit(release.wait, 30)
                pool.submit(started.set)
                assert started.wait(BOUND), f"round {rounds}: a call waited with 54 threads to spare"
            finally:
                release.set()
                pool.shutdown(wait=True)
    finally:
        sys.setswitchinterval(interval)
