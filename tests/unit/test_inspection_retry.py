"""C-5.10/C-5.11/C-5.12 integration regressions from 246926d, 504a81a and 0aaca39."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from subfleet import daemon as daemon_module, procs
from tests.unit.test_daemon_settle import ATTEMPT, daemon as settle_daemon
from tests.unit.test_daemon_inspection_load import BOOT, STARTED, GUARDIAN, TABLE, FakePs


@pytest.fixture
def daemon(settle_daemon):
    """The real worker scheduler around the process-free settle core."""
    core = settle_daemon
    core.__dict__.pop("_process_table", None)
    core._table, core._table_lock = (None, 0.0), threading.Lock()
    core.inspect_interval_s = 30
    core._inspect_retry = set()
    core._native, core._v1_unread = set(), {}
    core._busy_lock, core._busy = threading.Lock(), set()
    core._worker_failures, core._worker_retry_at = {}, {}
    with core.store.transaction("test.identity") as tx:
        tx.execute("UPDATE attempts SET boot_id=?,proc_start=? WHERE attempt_id=?", (BOOT, STARTED, ATTEMPT))
    with ThreadPoolExecutor(max_workers=2) as workers:
        core.workers = workers
        yield core


def expire(core):
    core._inspect_next[ATTEMPT] = 0
    core._table = (core._table[0], 0.0)


def failing_record(core, monkeypatch) -> list:
    """Every inspection that finds the guardian alive raises as it records the group (a store write)."""
    failures = []

    def record(a, table):
        failures.append(a["attempt_id"])
        raise RuntimeError("database or disk is full")
    monkeypatch.setattr(core, "_record_owned", record)
    return failures


def settle_key(core, key: str) -> None:
    """Wait, bounded, until `_schedule`'s worker for `key` has finished and been counted."""
    deadline = time.monotonic() + 30
    while True:
        with core._busy_lock:
            if key not in core._busy:
                return
        assert time.monotonic() < deadline, f"{key} is still running"
        time.sleep(.01)


def scheduled(core) -> int:
    """One pass of the attempt through C-5.10's `_schedule`, its retry due now; its failure count after."""
    core._worker_retry_at[ATTEMPT] = 0
    core._schedule(ATTEMPT, core._process_attempt, ATTEMPT, paced=True)
    settle_key(core, ATTEMPT)
    return core._worker_failures.get(ATTEMPT, 0)


def test_a_retry_that_finds_another_read_running_keeps_its_backoff(daemon, monkeypatch):
    """C-5.10, C-5.11: a retry that finds another attempt's `ps` read running has not repeated the inspection
    that raised, so it neither clears C-5.10's count nor adds to it, and the next failure is the second.

    Review of the merge, 2026-09-26: it returned normally, which `_schedule` counts as recovery. Whenever a
    read took 0.5 s or more every retry landed inside the next read, so the count went 1, 0, 1, the backoff
    never grew, and every failure was logged as the first (118 raises and log lines in 120 s at 0.72 s a read,
    against 8 raises and 4 lines)."""
    ps = FakePs()
    monkeypatch.setattr(procs, "_read", ps)
    failures = failing_record(daemon, monkeypatch)
    logged = []
    monkeypatch.setattr(daemon.log, "error", lambda msg, *args: logged.append(msg % args))
    counts = [scheduled(daemon)]                           # the inspection raises
    daemon._table = (daemon._table[0], 0.0)                # the table has expired ...
    assert daemon._table_lock.acquire(blocking=False)      # ... and another attempt is reading `ps`
    try:
        counts.append(scheduled(daemon))                   # the retry cannot inspect
    finally:
        daemon._table_lock.release()
    counts.append(scheduled(daemon))                       # the next one inspects, and raises again
    assert counts == [1, 1, 2]
    assert len(failures) == 2 and len(ps.tables()) == 2
    assert [line for line in logged if ATTEMPT in line] == [
        f"worker {ATTEMPT} failed: RuntimeError (1 in a row, next try in 0.5 s)",
        f"worker {ATTEMPT} failed: RuntimeError (2 in a row, next try in 1 s)"]


@pytest.mark.parametrize("cannot", ["table", "boot identity", "guardian", "recording table"])
def test_a_retry_that_cannot_inspect_keeps_its_backoff(daemon, monkeypatch, cannot):
    """C-5.10, C-5.11, C-4.2: nor is it recovery when the retry is given a table whose read failed, or whose boot
    identity cannot be read, or cannot inspect the guardian (its liveness is unknown), or cannot read a table
    to record the group from. Each decides nothing, and until the interval ends the retry waits at the pacing
    gate, which is no recovery either. The first pass that inspects to the end raises again: the second failure."""
    ps = FakePs()
    monkeypatch.setattr(procs, "_read", ps)
    failures = failing_record(daemon, monkeypatch)
    snapshot, liveness = procs.snapshot, procs.liveness
    counts = [scheduled(daemon)]                           # the inspection raises
    daemon._table = (daemon._table[0], 0.0)                # the next interval's read ...
    if cannot == "table":
        ps.failing.add("table")                            # ... fails
    elif cannot == "boot identity":
        procs.forget_boot_id()
        ps.failing.add("sysctl")                           # ... cannot read its boot identity
    elif cannot == "guardian":
        ps.hidden.add(GUARDIAN)                            # ... does not show the guardian,
        ps.failing.add("ps -p")                            # which cannot be asked about singly
    else:
        ps.hidden.add(GUARDIAN)                            # ... does not show the guardian, which is alive,
        reads = []                                         # and no second table can be read to record from

        def shared_read_only():
            reads.append(1)
            if len(reads) > 1:
                raise procs.InspectionError("ps inspection unavailable")
            return snapshot()
        monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: "alive")
        monkeypatch.setattr(daemon_module.procs, "snapshot", shared_read_only)
    counts.append(scheduled(daemon))                       # the retry cannot inspect
    assert daemon.store.get_attempt(ATTEMPT)["state"] == "running" and len(failures) == 1
    counts.append(scheduled(daemon))                       # inside the interval: the pacing gate
    ps.failing.clear(), ps.hidden.clear()
    monkeypatch.setattr(daemon_module.procs, "snapshot", snapshot)
    monkeypatch.setattr(daemon_module.procs, "liveness", liveness)
    expire(daemon)
    counts.append(scheduled(daemon))                       # inspects, and raises again
    assert counts == [1, 1, 1, 2]
    assert len(failures) == 2


def test_an_inspection_that_runs_to_its_end_ends_the_retry(daemon, monkeypatch):
    """C-5.10, C-5.11: once an inspection that raised has been repeated to its end, the attempt's passes are ordinary
    again: one that stops at the pacing gate is a success, so a later failure elsewhere in the pass is the first of
    its run, not the next of the old one."""
    monkeypatch.setattr(procs, "_read", FakePs())
    failures = failing_record(daemon, monkeypatch)
    counts = [scheduled(daemon)]                           # the inspection raises
    monkeypatch.setattr(daemon, "_record_owned", lambda a, table: None)
    counts.append(scheduled(daemon))                       # repeated to its end: recovered
    assert ATTEMPT not in daemon._inspect_retry
    read_json = daemon._read_json

    def unreadable(path):
        raise OSError("input/output error")                # the per-tick receipt read fails once
    monkeypatch.setattr(daemon, "_read_json", unreadable)
    counts.append(scheduled(daemon))
    monkeypatch.setattr(daemon, "_read_json", read_json)
    counts.append(scheduled(daemon))                       # the pacing gate: an ordinary success
    assert counts == [1, 0, 1, 0] and len(failures) == 1


def test_retry_state_is_dropped_once_an_attempt_is_no_longer_live(daemon, monkeypatch):
    """C-5.11: an attempt whose inspection raised is remembered until one runs to its end; if it ends first, the
    control loop's pruning forgets it with the rest of its pacing state."""
    monkeypatch.setattr(procs, "_read", FakePs())
    failing_record(daemon, monkeypatch)
    with pytest.raises(RuntimeError):
        daemon._process_attempt(ATTEMPT)
    assert ATTEMPT in daemon._inspect_retry
    daemon._forget_paced({ATTEMPT})
    assert ATTEMPT in daemon._inspect_retry                  # still live: kept
    with daemon.store.transaction("test.finished") as tx:
        tx.execute("UPDATE attempts SET state='succeeded' WHERE attempt_id=?", (ATTEMPT,))
    daemon._forget_paced({a["attempt_id"] for a in daemon.store.query(daemon_module.LIVE_ATTEMPTS)})
    assert ATTEMPT not in daemon._inspect_retry
