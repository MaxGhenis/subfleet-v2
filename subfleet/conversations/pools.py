"""The conversation service's worker pools (C-25.3): the file pool, the history pool
and the wake poller.

Each knows which of its ops are running, so the service's `close()` can wait for them
with a bound, and starts none once it has stopped. `shutdown(wait=True)` waits without
one, and one op blocked for good (a session-tree walk with no budget, a read from a
stalled mount, a write held inside the store's write guard) held `close()` for good.
What `close()` outlasts, `Daemon.close()` waits for with no bound before it lets
`daemon.lock` go (C-5.8a).
"""

from __future__ import annotations

import concurrent.futures
import threading


class _Op:
    """One op submitted to a pool: refused if its submit raised."""

    __slots__ = ("refused",)

    def __init__(self):
        self.refused = False


class CountedPool(concurrent.futures.ThreadPoolExecutor):
    """A thread pool that counts the ops its workers are running.

    An op counts from when a worker starts it, decided under the lock `stop()` takes,
    not from its submit: CPython queues the work before it starts a thread, so a submit
    whose thread could not start raised while an existing worker still ran the op,
    which nothing counted (reviews of 6c1f8bb). Such an op never runs now, nor does one
    a worker reaches after `stop()`."""

    def __init__(self, workers: int, name: str):
        super().__init__(workers, thread_name_prefix=name)
        self._guard = threading.RLock()
        self._idle = threading.Condition(self._guard)      # notified when an op ends
        self._running: set[_Op] = set()
        self._stopped = False

    def submit(self, fn, /, *args, **kwargs):
        op = _Op()

        def run():
            with self._guard:
                if op.refused or self._stopped:
                    return None             # its submit raised, or stop() came first: never run
                self._running.add(op)
            try:
                return fn(*args, **kwargs)
            finally:
                with self._idle:
                    self._running.discard(op)
                    self._idle.notify_all()

        with self._guard:                   # a worker cannot start `run` before this has decided
            try:
                return super().submit(run)
            except BaseException:
                op.refused = True
                raise

    def stop(self) -> set[_Op]:
        """Take nothing more, drop what has not started, and return the ops running."""
        with self._guard:
            self._stopped = True
            self.shutdown(wait=False, cancel_futures=True)
            return set(self._running)

    def await_running(self, ops: set[_Op], timeout: float) -> set[_Op]:
        """Wait up to `timeout` s for `ops` to end; return the ones still running."""
        with self._idle:
            self._idle.wait_for(lambda: not ops & self._running, timeout=timeout)
            return ops & self._running
