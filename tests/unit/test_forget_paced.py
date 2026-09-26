"""C-5.11: forgetting the pacing of attempts that ended never trips over a worker.

`_forget_paced` runs on the control loop while worker threads add and remove
entries in the same dicts (`_liveness_next`, `_census_next`). It used to walk
the dicts themselves, which raises "dictionary changed size during iteration"
when a worker adds an entry mid-walk (review of 5841d8b). It walks a copy.
"""

from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

from subfleet.daemon import Daemon


class Growing(dict):
    """A dict to which a worker adds an entry while it is being walked."""

    def __iter__(self):
        for key in super().__iter__():                 # the dict's own iterator, as a walk uses
            self[f"late-{key}"] = 0.0                  # the worker's insert, mid-walk
            yield key


def test_an_entry_added_while_forgetting_does_not_raise():
    liveness, census = Growing({"a1": 1.0, "a2": 2.0}), Growing({"a1": 1.0})
    core = SimpleNamespace(_liveness_next=liveness, _census_next=census)
    Daemon._forget_paced(core, live={"a1"})
    assert "a2" not in liveness and "a1" in liveness and "a1" in census


def test_forgetting_races_workers_without_raising():
    """Bounded: a worker adds and removes entries while forgetting runs 2,000 times,
    with the interpreter switching threads as often as it can."""
    core = SimpleNamespace(_liveness_next={}, _census_next={})
    stop, errors = threading.Event(), []

    def worker():
        n = 0
        while not stop.is_set() and n < 2_000_000:
            n += 1
            key = f"a{n % 500}"
            core._liveness_next[key] = float(n)
            core._census_next[key] = float(n)
            if n % 3 == 0:
                core._liveness_next.pop(f"a{(n + 250) % 500}", None)
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    thread = threading.Thread(target=worker)
    try:
        thread.start()
        for n in range(2000):
            try:
                Daemon._forget_paced(core, live={f"a{k}" for k in range(0, 500, 7)})
            except RuntimeError as exc:
                errors.append(exc)
                break
    finally:
        stop.set()
        thread.join(30)
        sys.setswitchinterval(interval)
    assert errors == []
