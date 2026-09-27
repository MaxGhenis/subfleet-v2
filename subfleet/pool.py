"""A thread pool that never leaves a call waiting while it has room to run it.

The daemon's pools promise that a call starts at once while fewer than the
pool's threads are running calls: a connection's reader when fewer than
`MAX_CONNECTIONS` connections are read (C-16.1), a `wait` for every connection
that could send one, a lookup beside slow requests (C-16.5).
`concurrent.futures.ThreadPoolExecutor` does not keep that promise. It reuses an
idle thread by counting permits: a thread adds one each time it finds its queue
empty, and `submit` takes one instead of starting a thread. When a thread finds
the queue empty just before a `submit` puts a call there, and adds its permit
just after that `submit` found none and started a thread, the old thread takes
the call and the new one finds the queue empty and adds a second permit: two
permits, one idle thread. Every later `submit` is then one thread short, so the
last call submitted waits for a running call to end although the pool has room;
when the running calls are connection readers, that is until a client closes.
A `ping` behind 40 idle connections went unanswered this way on the
free-threaded interpreter (2026-09-27, C-16.1). The GIL only narrows the
window: with a switch interval of 1 us the same pool strands a call in seconds.

`Pool` keeps its queue, its idle threads and its thread count under one lock,
so the count cannot drift: a call is handed straight to an idle thread, or
starts a new thread while there are fewer than `max_workers`, and is queued
only when every one of the `max_workers` threads has a call. A thread that
finishes takes the next queued call before it goes idle. So a queued call waits
only for a call to end, never for a thread the pool had room to start.

Like the standard pool, `submit` raises `RuntimeError` after `shutdown` and when
no thread can start; a call that could not start a thread is not queued. Threads
are started as calls need them and kept until `shutdown`, which lets the idle
ones go, cancels the queued calls if asked, and waits for the running ones if
asked. They are daemon threads: an idle one never holds up the interpreter's
exit, and whatever owns a pool shuts it down and waits for its calls first
(`Daemon.close`, `ConversationService.close`, `Timers.stop`).
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import Executor, Future
import threading
from typing import Any, Callable


class _Call:
    __slots__ = ("future", "fn", "args", "kwargs")

    def __init__(self, fn: Callable[..., Any], args: tuple, kwargs: dict):
        self.future: Future = Future()
        self.fn, self.args, self.kwargs = fn, args, kwargs

    def run(self) -> None:
        if not self.future.set_running_or_notify_cancel():
            return                              # cancelled while it was queued
        try:
            result = self.fn(*self.args, **self.kwargs)
        except BaseException as exc:            # as the standard pool: the future carries it
            self.future.set_exception(exc)
            self = None                         # noqa: PLW0642 - no cycle through the traceback
        else:
            self.future.set_result(result)


class _Idle:
    """An idle thread's hand-off: `call` is set under the pool's lock before
    `given`, and None (with `given` set) tells the thread to end."""

    __slots__ = ("given", "call")

    def __init__(self) -> None:
        self.given = threading.Event()
        self.call: _Call | None = None


class Pool(Executor):
    """At most `max_workers` threads; a call starts at once while one is free or
    can be started, and waits in order, FIFO, only while all of them run calls."""

    def __init__(self, max_workers: int, thread_name_prefix: str):
        if max_workers <= 0:
            raise ValueError("max_workers must be greater than 0")
        self._max_workers = max_workers
        self._prefix = thread_name_prefix
        self._lock = threading.Lock()
        self._queue: deque[_Call] = deque()     # only while every thread has a call
        self._idle: list[_Idle] = []            # only while the queue is empty
        self._threads: set[threading.Thread] = set()
        self._shutdown = False

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        call = _Call(fn, args, kwargs)
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if self._idle:
                idle = self._idle.pop()         # the most recently idle: its stack is warm
                idle.call = call
                idle.given.set()
            elif len(self._threads) < self._max_workers:
                thread = threading.Thread(target=self._work, args=(call,), daemon=True,
                                          name=f"{self._prefix}_{len(self._threads)}")
                thread.start()                  # raises when no thread can start: nothing is queued
                self._threads.add(thread)
            else:
                self._queue.append(call)
        return call.future

    def _work(self, call: _Call | None) -> None:
        while call is not None:
            call.run()
            call = None                         # hold nothing of it while idle (a reader's socket)
            with self._lock:
                if self._queue:
                    call = self._queue.popleft()
                    continue
                if self._shutdown:
                    return
                idle = _Idle()
                self._idle.append(idle)
            idle.given.wait()
            call = idle.call

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            self._shutdown = True
            cancelled = list(self._queue) if cancel_futures else []
            if cancel_futures:
                self._queue.clear()
            for idle in self._idle:
                idle.call = None
                idle.given.set()
            self._idle.clear()
            threads = list(self._threads)
        for call in cancelled:                  # outside the lock: a done callback may call this pool
            call.future.cancel()
        if wait:
            current = threading.current_thread()
            for thread in threads:
                if thread is not current:
                    thread.join()
