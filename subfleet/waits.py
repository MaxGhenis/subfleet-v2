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
all waiters together, and a wait ends within `poll_s` plus one read of the
commit that ends its last job.
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


class _Waiter:
    __slots__ = ("jobs", "event")

    def __init__(self, jobs: frozenset[str]):
        self.jobs = jobs
        self.event = threading.Event()


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
        #: Passes that read the store (for tests and `daemon.status`).
        self.reads = 0

    # --- waiters --------------------------------------------------------------

    @contextmanager
    def watching(self, job_ids: Iterable[str]) -> Iterator[threading.Event]:
        """Register for `job_ids`; the event is set when they may all be returned."""
        waiter = _Waiter(frozenset(job_ids))
        with self._lock:
            token, self._next = self._next, self._next + 1
            self._waiters[token] = waiter
            self._dirty = True
            if self._stop.is_set():
                waiter.event.set()
            elif self._thread is None:
                self._thread = threading.Thread(target=self._run, name="subfleet-waits", daemon=True)
                self._thread.start()
        self._wake.set()
        try:
            yield waiter.event
        finally:
            with self._lock:
                self._waiters.pop(token, None)

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
            generation, now = self.store.generation, self.clock()
            if generation == seen and not dirty and now < recheck_at:
                continue
            seen, recheck_at = generation, now + self.recheck_s
            try:
                ready = self.ready(set().union(*(waiter.jobs for waiter in waiters)))
            except Exception as exc:                        # noqa: BLE001 - see below
                # A read that failed (a store closing, a busy file) must not leave
                # waiters asleep: each reads for itself and reports what it finds.
                if self.on_error is not None:
                    self.on_error(exc)
                ready = None
            for waiter in waiters:
                if ready is None or waiter.jobs <= ready:
                    waiter.event.set()
        with self._lock:
            waiters = list(self._waiters.values())
        for waiter in waiters:
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
