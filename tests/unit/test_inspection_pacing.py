"""C-5.10 with C-5.12: an inspection that raises is paced like any other worker that raises.

C-5.12 rations a running attempt's inspection to once per `inspect_interval_s`,
and C-5.10 backs off a worker that raises and counts a normal return as
recovery. A pass that raised must therefore give back the interval it took:
otherwise C-5.10's retry finds the attempt not due, returns at the gate, and
clears the count, so a lasting cause raises and logs once per interval."""

from __future__ import annotations

import concurrent.futures
import threading
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import daemon as daemon_module
from subfleet.daemon import Daemon, worker_retry_delay
from subfleet.procs import ProcessTable
from tests.unit.test_daemon_settle import ATTEMPT, EMPTY, STARTED, daemon, never_census

__all__ = ["daemon"]                                           # the fixture, imported so pytest finds it here


class Inline:
    """The worker pool, run inline so the fake clock decides every retry."""
    def submit(self, fn, *args):
        future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args))
        except Exception as exc:                               # noqa: BLE001
            future.set_exception(exc)
        return future


def own_table(core) -> None:
    """The daemon's own shared table, in place of the fixture's stand-in."""
    core.__dict__.pop("_process_table", None)
    core._table, core._table_lock = (None, 0.0), threading.Lock()


def failing_record(core) -> list:
    """Every inspection that finds the guardian alive raises as it records the group (a store write)."""
    failures = []

    def record(a, table):
        failures.append(a["attempt_id"])
        raise RuntimeError("database or disk is full")
    core._record_owned = record
    return failures


@pytest.mark.parametrize("where", ["record", "lost"])
@pytest.mark.parametrize("interval", [0.5, 1.0, 2.0])
def test_c5_10_an_inspection_that_raises_backs_off_whatever_the_interval(daemon, monkeypatch, interval, where):
    """C-5.10, C-5.12 a running attempt whose inspection raises is retried 0.5 s later, doubling, and logged on
    the 1st, 2nd, 4th ... failure, as on main: the retry repeats the inspection, given the table the failed pass
    had, instead of returning at the gate because that pass took its interval. The store write that fails is
    `_record_owned`'s for a live guardian, or `_lost`'s for a dead one, whose every retry takes a census.

    Review of PR #37 at f2ebda0, 2026-09-27: at the default 1 s interval the retry found the attempt not due,
    returned, and so cleared the count; a lasting cause raised and logged once per interval (119 of each in
    120 s; main 8 and 4), and in the `_lost` case ran a full census each time."""
    clock = [100.0]
    monkeypatch.setattr(daemon_module, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda s: None))
    own_table(daemon)
    daemon.inspect_interval_s = interval
    daemon.workers = Inline()
    daemon._busy_lock, daemon._busy = threading.Lock(), set()
    daemon._worker_failures, daemon._worker_retry_at = {}, {}
    reads, raises, lines, censuses = [], [], [], []
    shown = {4242: (1, 4242, "Ss", STARTED), 4243: (4242, 4242, "S", STARTED)} if where == "record" else {}
    monkeypatch.setattr(daemon_module.procs, "snapshot",
                        lambda: reads.append(clock[0]) or ProcessTable(dict(shown), "boot"))

    def failing(a, *args):
        raises.append(round(clock[0] - 100, 2))
        raise RuntimeError("database or disk is full")
    if where == "record":
        daemon._record_owned = failing
        daemon._contain = never_census
    else:
        monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: "dead")
        daemon._contain = lambda a: censuses.append(1) or EMPTY
        daemon._lost = failing
    monkeypatch.setattr(daemon.log, "error", lambda message, *args: lines.append(message % args))
    while clock[0] < 160.0:                                    # 60 s of 50 ms control ticks
        daemon._schedule(ATTEMPT, daemon._process_attempt, ATTEMPT, paced=True)
        clock[0] += .05
    # Main: raises at 0, 0.55, 1.6, 3.65, 7.7, 15.75, 31.75 s; lines on the 1st, 2nd and 4th.
    assert len(raises) <= 8, raises
    assert daemon._worker_failures[ATTEMPT] == len(raises), (raises, daemon._worker_failures)
    assert len(lines) <= 4, lines
    assert len(censuses) == (len(raises) if where == "lost" else 0)
    # And the retries stay rationed: never more than one table read begun per interval.
    assert all(b - a >= interval - 1e-9 for a, b in zip(reads, reads[1:])), reads


def test_c5_10_a_pass_that_raised_is_repeated_on_its_table_not_skipped(daemon, monkeypatch):
    """C-5.10, C-5.12 a pass that raised leaves no inspection clock, so the next pass repeats the inspection (and
    fails again) rather than returning at the gate as not due; it is given the table the failed pass had, which
    has not expired, so a failure costs no `ps` of its own. As the release line's
    `test_a_failing_inspection_is_retried_not_skipped`."""
    own_table(daemon)
    daemon.inspect_interval_s = 30
    reads = []
    monkeypatch.setattr(daemon_module.procs, "snapshot", lambda: reads.append(1) or ProcessTable(
        {4242: (1, 4242, "Ss", STARTED), 4243: (4242, 4242, "S", STARTED)}, "boot"))
    failures = failing_record(daemon)
    daemon._contain = never_census
    for n in range(1, 4):
        with pytest.raises(RuntimeError):
            daemon._process_attempt(ATTEMPT)
        assert len(failures) == n
        assert ATTEMPT not in daemon._inspect_next
    assert reads == [1]
    daemon._record_owned = lambda a, table: None               # the cause has passed
    daemon._process_attempt(ATTEMPT)                           # the inspection runs to its end, and is paced again
    assert reads == [1] and daemon._inspect_next[ATTEMPT] == daemon._table[1]
    daemon._record_owned = lambda a, table: pytest.fail("inspected before the interval ended")
    daemon._process_attempt(ATTEMPT)


TICK, EPS = .05, 1e-6


@settings(max_examples=40, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(interval=st.floats(.1, 4), fails=st.lists(st.booleans(), min_size=1, max_size=12))
def test_c5_10_c5_12_a_running_attempt_s_inspection_is_paced_whether_or_not_it_raises(daemon, monkeypatch,
                                                                                      interval, fails):
    """C-5.10, C-5.12 as properties, for one running attempt through the real `_schedule` on a fake clock, whose
    k-th inspection raises when `fails[k % len(fails)]` (a store write in `_record_owned`), for any interval:

      P1 reads of the shared table begin at least an interval apart;
      P2 the pass after one that raised repeats the inspection;
      P3 so C-5.10's count after a raise is the number of inspections in a row that raised;
      P4 that pass runs at the first tick at or after C-5.10's retry time;
      P5 after an inspection that ran to its end, the next begins at the first tick at or after its table expires.

    P2 to P4 failed on f2ebda0 for every interval above half a second (review of PR #37's port, 2026-09-27)."""
    clock = [100.0]
    monkeypatch.setattr(daemon_module, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda s: None))
    own_table(daemon)
    daemon._inspect_next, daemon.inspect_interval_s = {}, interval
    daemon.workers = Inline()
    daemon._busy_lock, daemon._busy = threading.Lock(), set()
    daemon._worker_failures, daemon._worker_retry_at = {}, {}
    daemon._contain = never_census
    monkeypatch.setattr(daemon.log, "error", lambda message, *args: None)
    reads, inspections, passes = [], [], []            # passes: (when, inspected, raised, count after, due after)
    monkeypatch.setattr(daemon_module.procs, "snapshot", lambda: reads.append(clock[0]) or ProcessTable(
        {4242: (1, 4242, "Ss", STARTED), 4243: (4242, 4242, "S", STARTED)}, "boot"))

    def record(a, table):
        k = len(inspections)
        inspections.append(clock[0])
        if fails[k % len(fails)]:
            raise RuntimeError("database or disk is full")
    daemon._record_owned = record

    def one_pass(aid):
        before = len(inspections)
        try:
            Daemon._process_attempt(daemon, aid)
        except Exception:
            passes.append([clock[0], len(inspections) > before, True, None])
            raise
        passes.append([clock[0], len(inspections) > before, False, None])
    for n in range(int(30 / TICK)):                     # 30 s of control ticks
        clock[0] = 100.0 + n * TICK
        ran = len(passes)
        daemon._schedule(ATTEMPT, one_pass, ATTEMPT, paced=True)
        if len(passes) > ran:
            passes[-1][3] = daemon._worker_failures.get(ATTEMPT, 0)
            passes[-1].append(daemon._inspect_next.get(ATTEMPT))

    assert all(b - a >= interval - EPS for a, b in zip(reads, reads[1:])), ("P1", reads)
    run = 0
    for (at, inspected, raised, count, due), (later, again, _, _, _) in zip(passes, passes[1:]):
        if raised:
            run += 1
            assert count == run, ("P3", passes)
            assert again, ("P2", at, later, passes)
            retry = at + worker_retry_delay(count)
            assert retry - EPS <= later <= retry + TICK + EPS, ("P4", at, later, count)
        elif inspected:
            run = 0
            following = [p[0] for p in passes if p[0] > at and p[1]]
            if following:
                assert due - EPS <= following[0] <= due + TICK + EPS, ("P5", at, due, following[0])
