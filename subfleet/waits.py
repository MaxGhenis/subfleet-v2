"""One reader for every `wait` (C-15.5).

A `wait` long poll used to re-read every job it watched on every wake-up, and
every worker pass woke every waiter: with ~21 PostToolUse hooks each waiting
on a job, that was one store read per waiter per wake, behind the one store
lock (2026-09-24, 2026-09-25). Here each waiter registers the jobs it waits on
and an event, and one thread, the hub, answers for all of them:

- when poked (`Daemon._notify`, after a state change) or every `poll_s`
  (0.1 s), it compares the store's generation with the one it last read;
- when the generation moved, or `recheck_s` (1 s) has passed, or a waiter has
  registered since, it reads the state of every watched job, and which of them
  hold an `out:` lease, in one statement each, whatever the number of waiters;
- it sets the event of each waiter whose jobs may all be returned: ended, and
  for a job that succeeded, its export done; a job the store no longer has is
  returned too, so its waiter says so as it always did.

A waiter registers before its first read, so a commit it did not see always
comes after a registration the hub sees: no wake-up is lost. It then reads its
answer itself, once, when woken. The cost of waiting is one read per commit for
all waiters together.

How soon a wait ends: the hub looks at the generation at once when poked and
otherwise at most `poll_s` after its last look ended, so a wait whose last job
ends by a commit on this store returns within `poll_s` plus the hub's read in
progress when the commit landed, the hub's read that sees it, and the waiter's
own read. A commit the generation does not count (another process's) is seen
within `recheck_s` plus those reads. Neither read has a time bound of its own:
each takes what SQLite takes, plus up to `READ_WAIT_S` for a read connection.

The hub never leaves a waiter asleep. A pass that raises wakes every waiter to
read for itself, and the hub goes on; if its thread ends anyway, it wakes every
waiter as it goes, and the next registration or the next wait starts another.
A thread that ended less than `recheck_s` after it started is not restarted
at once: until it is, each wait lasts at most `recheck_s` and its waiter reads
again, so a hub that cannot stay up turns its waiters into slow pollers.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any

#: C-15.5: how often the hub looks at the store's generation without a poke.
WAIT_POLL_S = 0.1
#: How many ids one statement names (SQLite allows 32766 variables).
CHUNK = 500

TERMINAL = ("succeeded", "failed", "cancelled", "lost")


class _Wake(threading.Event):
    """A waiter's event. Waiting on it first makes sure the hub is running, so a
    waiter never sleeps on a hub whose thread has ended."""

    def __init__(self, hub: WaitHub):
        super().__init__()
        self._hub = hub

    def wait(self, timeout: float | None = None) -> bool:
        if not self._hub.ensure_running():
            timeout = self._hub.recheck_s if timeout is None else min(timeout, self._hub.recheck_s)
        return super().wait(timeout)


class _Waiter:
    __slots__ = ("jobs", "event")

    def __init__(self, jobs: frozenset[str], hub: WaitHub):
        self.jobs = jobs
        self.event = _Wake(hub)


class WaitHub:
    def __init__(self, store: Any, *, poll_s: float = WAIT_POLL_S, recheck_s: float = 1.0,
                 clock: Callable[[], float] = time.monotonic,
                 on_error: Callable[[BaseException], None] | None = None):
        self.store, self.clock, self.on_error = store, clock, on_error
        self.poll_s, self.recheck_s = poll_s, recheck_s
        self._lock = threading.Lock()
        self._waiters: dict[int, _Waiter] = {}
        self._next = 0
        self._dirty = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        #: Passes that read the store (for tests and `daemon.status`).
        self.reads = 0
        #: Times the hub's thread ended other than by `stop`, and was started again.
        self.deaths = 0
        self.restarts = 0

    # --- waiters --------------------------------------------------------------

    @contextmanager
    def watching(self, job_ids: Iterable[str]) -> Iterator[threading.Event]:
        """Register for `job_ids`; the event is set when they may all be returned."""
        waiter = _Waiter(frozenset(job_ids), self)
        with self._lock:
            token, self._next = self._next, self._next + 1
            self._waiters[token] = waiter
            self._dirty = True
            if self._stop.is_set():
                waiter.event.set()
        self.ensure_running()
        self._wake.set()
        try:
            yield waiter.event
        finally:
            with self._lock:
                self._waiters.pop(token, None)

    def ensure_running(self) -> bool:
        """Start the hub's thread unless it runs, or the hub is stopped. False when
        it is down and ended too soon after its last start to be started again
        yet (the caller's wait is then cut to `recheck_s`)."""
        with self._lock:
            if self._stop.is_set() or (self._thread is not None and self._thread.is_alive()):
                return True
            now = self.clock()
            if self._started_at is not None and now - self._started_at < self.recheck_s:
                return False
            if self._started_at is not None:
                self.restarts += 1
            self._started_at, self._dirty = now, True
            self._thread = threading.Thread(target=self._run, name="subfleet-waits", daemon=True)
            self._thread.start()
            return True

    def poke(self) -> None:
        """Something may have changed: look now rather than at the next poll."""
        self._wake.set()

    def stop(self) -> None:
        """Wake every waiter (each then sees the daemon stopping) and end the hub."""
        with self._lock:
            self._stop.set()
            waiters = list(self._waiters.values())
            thread = self._thread
        for waiter in waiters:
            waiter.event.set()
        self._wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    @property
    def watched(self) -> int:
        with self._lock:
            return len(self._waiters)

    # --- the hub --------------------------------------------------------------

    def _run(self) -> None:
        died: BaseException | None = None
        try:
            self._loop()
        except BaseException as exc:                        # noqa: BLE001 - reported below
            died = exc
        finally:
            # Stopped or not, no waiter stays asleep on a thread that has ended:
            # each reads for itself, and its next wait starts another hub.
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
                if died is not None:
                    self.deaths += 1
                waiters = list(self._waiters.values())
            for waiter in waiters:
                waiter.event.set()
            if died is not None:
                self._report(died)

    def _report(self, exc: BaseException) -> None:
        if self.on_error is not None:
            try:
                self.on_error(exc)
            except Exception:                               # noqa: BLE001 - a report never stops the hub
                pass

    def _loop(self) -> None:
        seen, recheck_at = None, 0.0
        while not self._stop.is_set():
            self._wake.wait(self.poll_s)
            self._wake.clear()
            with self._lock:
                if self._stop.is_set():
                    break
                waiters = list(self._waiters.values())
                dirty, self._dirty = self._dirty, False
            if not waiters:
                seen = None                     # the next waiter is read for at once
                continue
            try:
                generation, now = self.store.generation, self.clock()
                if generation == seen and not dirty and now < recheck_at:
                    continue
                seen, recheck_at = generation, now + self.recheck_s
                ready = self.ready(set().union(*(waiter.jobs for waiter in waiters)))
            except Exception as exc:                        # noqa: BLE001 - see below
                # A pass that failed (a store closing, a busy file, a defect)
                # must not leave waiters asleep: each reads for itself and
                # reports what it finds, and the hub goes on.
                self._report(exc)
                ready = None
            for waiter in waiters:
                if ready is None or waiter.jobs <= ready:
                    waiter.event.set()

    def ready(self, job_ids: set[str]) -> set[str]:
        """The jobs a `wait` may return now: ended (a succeeded one with its
        export done), or unknown to the store."""
        self.reads += 1
        ids = sorted(job_ids)
        states: dict[str, str] = {}
        exporting: set[str] = set()
        for start in range(0, len(ids), CHUNK):
            chunk = ids[start:start + CHUNK]
            marks = ",".join("?" for _ in chunk)
            for row in self.store.query(f"SELECT job_id,state FROM jobs WHERE job_id IN ({marks})", chunk):
                states[row["job_id"]] = row["state"]
            exporting.update(row["holder"] for row in self.store.query(
                f"SELECT holder FROM leases WHERE lease_key LIKE 'out:%' AND holder IN ({marks})", chunk))
        return ({job for job in ids if job not in states}
                | {job for job, state in states.items()
                   if state in TERMINAL and not (state == "succeeded" and job in exporting)})
