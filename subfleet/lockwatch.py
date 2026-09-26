"""Who holds a lock, for how long, and who is waiting for it (C-3.6).

`WatchedLock` is a re-entrant lock that records which thread holds it and
when that thread's outermost hold began. A `LockWatch` reads the record and
reports, rate-limited:

- a hold longer than `hold_s`, with the holder's live Python stack, taken
  while the hold lasts (the watch's own thread samples it);
- the end of such a hold, with its length;
- a thread that has waited `wait_s` for the lock, with the number of waiters,
  the longest wait, and the holder's live stack;
- a store read connection in use longer than `hold_s` (`watch_reads`), with
  its thread's live stack, since no checkpoint passes its snapshot (C-3.7).

Nothing here changes who gets the lock or when: the bookkeeping is done while
the lock is held, and a waiter's report is written between two timed waits
for it. A report that raises is dropped: it never fails an acquire, and never
makes a release raise after what the lock guarded (a committed transaction)
is done. On 2026-09-25 a `list` that takes 1 ms took 34-94 s in the daemon,
with 18 threads waiting on the store lock, and nothing said which thread held
it or what it was doing; this module is how the daemon says so.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable

#: C-3.6: a hold this long is reported with the holder's live stack.
HOLD_REPORT_S = 2.0
#: C-3.6: a thread that has waited this long for the lock reports itself.
WAIT_REPORT_S = 5.0
#: C-3.6: at most one line per lock and kind of report in this window; the
#: next line, or a summary once the window has passed, counts the rest.
REPORT_EVERY_S = 60.0
#: How often the watch samples the holder of each lock it watches.
SAMPLE_EVERY_S = 0.5
#: Frames kept from a stack: the innermost ones, where the time goes.
STACK_LIMIT = 40


def thread_name(ident: int | None) -> str:
    for thread in threading.enumerate():
        if thread.ident == ident:
            return f"{thread.name} ({ident})"
    return f"thread {ident}"


def format_stack(frame, limit: int = STACK_LIMIT) -> str:
    import traceback            # only when there is something to report: every CLI imports the store
    return "".join(traceback.format_stack(frame, limit=limit))


def live_stack(ident: int, limit: int = STACK_LIMIT) -> str:
    """The Python stack thread `ident` is running now, innermost frame last."""
    frame = sys._current_frames().get(ident)
    if frame is None:
        return "    (no Python frame: the thread has ended)\n"
    return format_stack(frame, limit)


class WatchedLock:
    """A re-entrant lock that remembers its holder (see the module docstring)."""

    def __init__(self, name: str):
        self.name = name
        self._lock = threading.RLock()
        self._depth = 0
        #: (thread ident, monotonic start) of the outermost hold, or None. One
        #: attribute, so a reader without the lock never pairs one hold's
        #: thread with another's start.
        self.held: tuple[int, float] | None = None
        #: thread ident -> monotonic start, for each thread waiting now.
        self.waiting: dict[int, float] = {}
        self.watch: LockWatch | None = None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        me = threading.get_ident()
        if not self._lock.acquire(False):
            if not blocking:
                return False
            if not self._wait(me, timeout):
                return False
        if self._depth == 0:
            self.held = (me, time.monotonic())
        self._depth += 1
        return True

    def _wait(self, me: int, timeout: float) -> bool:
        started = time.monotonic()
        limit = None if timeout is None or timeout < 0 else started + timeout
        self.waiting[me] = started
        try:
            while True:
                watch = self.watch
                step = -1 if watch is None else watch.wait_s
                if limit is not None:
                    left = max(0.0, limit - time.monotonic())
                    step = left if step < 0 else min(step, left)
                if self._lock.acquire(True, step):
                    return True
                if limit is not None and time.monotonic() >= limit:
                    return False
                if watch is not None:
                    try:
                        watch.waited(self, me, time.monotonic() - started, sys._getframe(1))
                    except Exception:                  # noqa: BLE001 - a report never fails an acquire
                        pass
        finally:
            self.waiting.pop(me, None)

    def release(self) -> None:
        held = self.held
        if held is None or held[0] != threading.get_ident():
            raise RuntimeError(f"cannot release un-acquired {self.name} lock")
        self._depth -= 1
        if self._depth:
            self._lock.release()
            return
        self.held = None
        self._lock.release()
        watch = self.watch
        if watch is not None:
            seconds = time.monotonic() - held[1]
            if seconds >= watch.hold_s:
                # The lock is already let go, and what it guarded is done (a
                # transaction committed): a report that fails must not say otherwise.
                try:
                    watch.released(self, held, seconds, sys._getframe(1))
                except Exception:                      # noqa: BLE001 - diagnostics only
                    pass

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


class LockWatch:
    """Reports long holds and long waits on the `WatchedLock`s it watches."""

    def __init__(self, report: Callable[[str], None], *, hold_s: float = HOLD_REPORT_S,
                 wait_s: float = WAIT_REPORT_S, every_s: float = REPORT_EVERY_S,
                 sample_s: float = SAMPLE_EVERY_S, clock: Callable[[], float] = time.monotonic):
        self.report, self.clock = report, clock
        self.hold_s, self.wait_s, self.every_s, self.sample_s = hold_s, wait_s, every_s, sample_s
        self.locks: list[WatchedLock] = []
        self._sampled: dict[int, tuple[int, float]] = {}    # id(lock) -> the hold last sampled
        # (name, read holds) sampled like the locks (C-3.7); the holds reported so far.
        self.readers: list[tuple[str, Callable[[], list[tuple[int, float, str]]]]] = []
        self._reads_sampled: set[tuple[str, int, float]] = set()
        # (lock name, kind) -> [next time a line may be written, lines held back, worst held back]
        self._limits: dict[tuple[str, str], list] = {}
        self._limits_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def add(self, lock: WatchedLock) -> WatchedLock:
        lock.watch = self
        self.locks.append(lock)
        return lock

    def watch_reads(self, name: str, holds: Callable[[], list[tuple[int, float, str]]]) -> None:
        """Also sample `holds()`, the (thread ident, monotonic start, kind) of each
        read connection in use (`Store.read_holds`): one held past `hold_s` is
        reported once, with its thread's live stack. A long read holds the
        write-ahead log back: no checkpoint passes its snapshot (C-3.7)."""
        self.readers.append((name, holds))

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="subfleet-lockwatch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        for lock in self.locks:
            if lock.watch is self:
                lock.watch = None

    def _run(self) -> None:
        while not self._stop.wait(self.sample_s):
            try:
                self.sample()
            except Exception:                              # noqa: BLE001 - diagnostics only
                pass

    def sample(self) -> None:
        """One look at every lock: report each hold that has passed `hold_s`
        once, with the stack its thread is running now; then write the
        summaries the rate limit has held back past their window."""
        now = self.clock()
        for lock in list(self.locks):
            held = lock.held
            if held is None or now - held[1] < self.hold_s or self._sampled.get(id(lock)) == held:
                continue
            self._sampled[id(lock)] = held
            ident, since = held
            seconds = now - since
            self._emit(lock.name, "hold", seconds, lambda: (
                f"{lock.name} lock held {seconds:.1f} s so far by {thread_name(ident)}; "
                f"{self._waiting(lock, now)}. Holder's stack:\n{live_stack(ident)}"))
        for name, holds in list(self.readers):
            current = {(name, ident, since): kind for ident, since, kind in holds()}
            self._reads_sampled = {key for key in self._reads_sampled if key[0] != name or key in current}
            for key, kind in current.items():
                ident, since = key[1], key[2]
                if now - since < self.hold_s or key in self._reads_sampled:
                    continue
                self._reads_sampled.add(key)
                seconds = now - since
                self._emit(name, "read-hold", seconds, lambda name=name, ident=ident, kind=kind, seconds=seconds: (
                    f"{name} read connection held {seconds:.1f} s so far by {thread_name(ident)} for a "
                    f"{kind}; no checkpoint of the write-ahead log passes it. Its stack:\n{live_stack(ident)}"))
        self._flush(now)

    def released(self, lock: WatchedLock, held: tuple[int, float], seconds: float,
                 frame=None) -> None:
        """Called by the thread that held `lock`, just after letting it go; `frame`
        is where it let go, reported when no sample caught the hold."""
        ident = held[0]
        sampled = self._sampled.get(id(lock)) == held
        self._emit(lock.name, "released", seconds, lambda: (
            f"{lock.name} lock released after {seconds:.1f} s by {thread_name(ident)}"
            + ("" if sampled or frame is None else ". Where it was held:\n"
               + format_stack(frame))))

    def waited(self, lock: WatchedLock, me: int, seconds: float, frame=None) -> None:
        """Called by a thread still waiting for `lock` after `seconds`; `frame` is
        where it asked for the lock."""
        now = self.clock()
        held = lock.held

        def render() -> str:
            holder = ("no holder at this instant" if held is None else
                      f"held {now - held[1]:.1f} s by {thread_name(held[0])}")
            text = (f"{thread_name(me)} has waited {seconds:.1f} s for the {lock.name} lock; "
                    f"{self._waiting(lock, now)}; {holder}."
                    + ("" if frame is None else " Waiter's stack:\n"
                       + format_stack(frame, 12)))
            if held is not None:
                text += f"Holder's stack:\n{live_stack(held[0])}"
            return text
        self._emit(lock.name, "wait", seconds, render)

    @staticmethod
    def _waiting(lock: WatchedLock, now: float) -> str:
        starts = list(lock.waiting.values())
        if not starts:
            return "none waiting"
        return f"{len(starts)} waiting, the longest {now - min(starts):.1f} s"

    def note(self, name: str, kind: str, seconds: float, render: Callable[[], str]) -> None:
        """Report something else about a watched lock's store (the read pool's
        waits, C-3.7), under the same rate limit as the lock's own reports."""
        self._emit(name, kind, seconds, render)

    def _emit(self, name: str, kind: str, seconds: float, render: Callable[[], str]) -> None:
        now = self.clock()
        with self._limits_lock:
            limit = self._limits.setdefault((name, kind), [0.0, 0, 0.0])
            if now < limit[0]:
                limit[1] += 1
                limit[2] = max(limit[2], seconds)
                return
            held_back, worst = limit[1], limit[2]
            limit[:] = [now + self.every_s, 0, 0.0]
        text = render()
        if held_back:
            text = (f"({held_back} more {kind} report(s) held back in the last {self.every_s:g} s, "
                    f"the longest {worst:.1f} s) " + text)
        self.report(text)

    def _flush(self, now: float) -> None:
        lines = []
        with self._limits_lock:
            for (name, kind), limit in self._limits.items():
                if limit[1] and now >= limit[0]:
                    lines.append(f"{limit[1]} {kind} report(s) on the {name} lock held back in the "
                                 f"last {self.every_s:g} s, the longest {limit[2]:.1f} s")
                    limit[:] = [now + self.every_s, 0, 0.0]
        for line in lines:
            self.report(line)
