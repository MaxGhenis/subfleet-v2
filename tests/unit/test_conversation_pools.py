"""The conversation service's worker pools (C-25.3, `subfleet/conversations/pools.py`).

A `CountedPool` counts an op from when a worker starts it, decided under the lock
`stop()` takes, so the service's `close()` can wait for exactly the ops that were
running, with a bound. Invariants, for every interleaving of submits, failed thread
starts, workers and `stop()`:

- every op that runs is one `stop()` counted, or ended before `stop()`: none runs
  uncounted, so none outlives a wait that did not know of it;
- an op whose submit raised never runs;
- no op starts once `stop()` has returned, and the pool takes none after;
- once the counted ops end, `await_running` returns nothing left.

Each deterministic test below fails without one part of that (the review of
cc7504e, finding 3, found three parts no test had caught). Every hold is let go in a
`finally`, so a failing assertion leaves no worker parked for the rest of the run.
"""

from __future__ import annotations

import random
import sys
import threading
import time
import types
from unittest import mock

import hypothesis
import pytest
from hypothesis import strategies as st

from subfleet.conversations.pools import CountedPool

#: The worker-side wrapper `submit` gives each op. A worker whose Python stack holds it
#: while the test holds the pool's guard is blocked on that guard: it is the first thing
#: the wrapper does.
RUN_CODE = next(const for const in CountedPool.submit.__code__.co_consts
                if isinstance(const, types.CodeType) and const.co_name == "run")


def until(predicate, what: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        time.sleep(0.005)


def workers_in_run(pool: CountedPool) -> int:
    """How many of `pool`'s workers are inside an op's wrapper now."""
    frames = sys._current_frames()
    count = 0
    for thread in threading.enumerate():
        if not thread.name.startswith(pool._thread_name_prefix):
            continue
        frame = frames.get(thread.ident)
        while frame is not None and frame.f_code is not RUN_CODE:
            frame = frame.f_back
        count += frame is not None
    return count


def test_an_op_a_worker_reaches_after_stop_never_runs():
    """A worker has taken the op from the queue and waits for the guard when stop()
    runs: it must find the pool stopped and never run the op, which stop() did not
    count. (Without the check, it ran uncounted, after close() had stopped waiting.)"""
    pool = CountedPool(2, "pools-after-stop")
    ran = []
    try:
        with pool._guard:
            future = pool.submit(lambda: ran.append("ran"))
            until(lambda: workers_in_run(pool) == 1, "a worker to reach the guard")
            running = pool.stop()
        assert running == set()
        assert future.result(30) is None and ran == []
    finally:
        pool.shutdown(wait=True)


def test_an_op_whose_submit_raised_never_runs_even_with_a_free_worker():
    """The busy worker is let go while submit is still starting a thread for a new op,
    which then fails. submit holds the guard across the executor's submit, and a worker
    needs it to end an op as well as to start one, so no worker can take the new op
    before it is marked refused. (Without the guard there, the freed worker ran it at
    once, and it ran uncounted.)"""
    pool = CountedPool(2, "pools-free-worker")
    entered, go, ran = threading.Event(), threading.Event(), []
    first = pool.submit(lambda: (entered.set(), go.wait(30)))
    real_start = threading.Thread.start

    def start(thread):
        if thread.name.startswith("pools-free-worker"):
            go.set()                        # the busy worker may free up and take the new op
            deadline = time.monotonic() + 2.0
            while not ran and time.monotonic() < deadline:
                time.sleep(0.005)           # an unguarded submit has let it run by now
            raise RuntimeError("can't start new thread")
        return real_start(thread)

    try:
        assert entered.wait(30), "the first op never started"
        with mock.patch.object(threading.Thread, "start", start):
            with pytest.raises(RuntimeError):
                pool.submit(lambda: ran.append("ran"))
        assert first.result(30) == (None, True)
        pool.shutdown(wait=True)            # the worker drains the queue, that op included
        assert ran == []
    finally:
        go.set()
        pool.shutdown(wait=True)


class SlowAdd(set):
    """`_running`, whose `add` waits for the test: a worker caught counting an op."""

    def __init__(self):
        super().__init__()
        self.adding, self.go = threading.Event(), threading.Event()

    def add(self, item):
        self.adding.set()
        assert self.go.wait(30)
        super().add(item)


def test_stop_counts_an_op_a_worker_is_counting():
    """stop() takes the guard a worker counts an op under, so an op a worker has found
    the pool open for is in the set stop() returns. (A stop() without the guard
    returned without it, and the op ran with nobody waiting for it.)"""
    pool = CountedPool(1, "pools-counting")
    pool._running = running = SlowAdd()
    body = threading.Event()
    future = pool.submit(lambda: body.wait(30))
    result = []
    stopper = threading.Thread(target=lambda: result.append(pool.stop()))
    try:
        assert running.adding.wait(30), "the worker never counted the op"
        stopper.start()
        stopper.join(1.0)                   # one that does not wait for the guard has returned by now
        running.go.set()
        stopper.join(30)
        assert not stopper.is_alive() and len(result) == 1 and len(result[0]) == 1, result
    finally:
        running.go.set()
        body.set()
        if stopper.ident is not None:
            stopper.join(30)
        pool.shutdown(wait=True)
    assert future.result(30) is True


def test_await_running_takes_a_zero_bound_and_an_empty_set_at_once():
    pool = CountedPool(1, "pools-zero")
    entered, go = threading.Event(), threading.Event()
    pool.submit(lambda: (entered.set(), go.wait(30)))
    try:
        assert entered.wait(30)
        running = pool.stop()
        assert len(running) == 1
        started = time.monotonic()
        assert pool.await_running(running, 0) == running
        assert pool.await_running(set(), 10) == set()
        assert time.monotonic() - started < 5
    finally:
        go.set()
        pool.shutdown(wait=True)
    assert pool.await_running(running, 30) == set()


def test_a_stopped_pool_drops_what_is_queued_and_takes_nothing_more():
    pool = CountedPool(1, "pools-queue")
    entered, go, ran = threading.Event(), threading.Event(), []
    busy = pool.submit(lambda: (entered.set(), go.wait(30)))
    try:
        assert entered.wait(30)
        queued = pool.submit(lambda: ran.append("queued"))
        assert len(pool.stop()) == 1
        assert queued.cancelled()
        with pytest.raises(RuntimeError):
            pool.submit(lambda: ran.append("late"))
    finally:
        go.set()
        pool.shutdown(wait=True)
    assert busy.result(30) == (None, True) and ran == []


_PREFIXES = iter(range(1_000_000))


@hypothesis.settings(max_examples=40, deadline=None)
@hypothesis.given(workers=st.integers(1, 3), submitters=st.integers(1, 4),
                  ops=st.lists(st.integers(1, 12), min_size=4, max_size=4),
                  fail_rate=st.sampled_from([0.0, 0.3, 0.7]), pause=st.floats(0, 0.01),
                  seed=st.integers(0, 2**32 - 1))
def test_every_op_that_runs_was_counted_and_no_refused_op_runs(workers, submitters, ops, fail_rate, pause, seed):
    """Property: submitters race stop(), some of their thread starts failing. Every op
    body is held until after stop(), so none ends before it: each that ever runs must
    be one stop() counted, none whose submit raised may run, and once they are let go
    the wait finds none left."""
    prefix = f"pools-property-{next(_PREFIXES)}"
    rng = random.Random(seed)               # drawn here: the submitters' threads must not draw from hypothesis
    pool = CountedPool(workers, prefix)
    guard, release = threading.Lock(), threading.Event()
    ran, refused = set(), set()
    real_start = threading.Thread.start

    def start(thread):
        if thread.name.startswith(prefix):
            with guard:
                fail = rng.random() < fail_rate
            if fail:
                raise RuntimeError("can't start new thread")
        return real_start(thread)

    def body(token):
        with guard:
            ran.add(token)
        assert release.wait(30)

    def submit_some(who):
        for n in range(ops[who]):
            try:
                pool.submit(body, (who, n))
            except RuntimeError:
                with guard:
                    refused.add((who, n))

    threads = [threading.Thread(target=submit_some, args=(who,)) for who in range(submitters)]
    try:
        with mock.patch.object(threading.Thread, "start", start):
            for thread in threads:
                real_start(thread)
            time.sleep(pause)
            running = pool.stop()
            for thread in threads:
                thread.join(30)
        release.set()
        assert pool.await_running(running, 30) == set()
        pool.shutdown(wait=True)
    finally:
        release.set()
        for thread in threads:
            thread.join(30)
        pool.shutdown(wait=True)
    # Each op that ran was counted; one begun after stop() returned would be one more.
    assert len(ran) == len(running), (len(ran), len(running))
    assert not ran & refused, ran & refused
