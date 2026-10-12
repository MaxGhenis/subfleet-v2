"""Through the real `Daemon._process_attempt`, does a slow boot-identity read hold every attempt's worker, or only
the reader's, as for `ps` (C-5.12)? And while `sysctl` hangs, how long is another attempt's receipt left unread?

The probe of the final review of PR #37 (2026-09-26), committed as it was run for
`docs/reports/2026-09-24-process-inspection-cost/boot-wait.txt`, only its location
and this docstring changed. It borrows `tests/unit/test_daemon_settle.py`'s daemon
fixture; `boot_id` and `snapshot` are faked, so nothing real is inspected or
signalled. It asserts nothing: it prints each attempt's pass duration in seconds.

Usage, from the root of the tree to measure:
    uv run pytest -q -s -p no:cacheprovider tools/probe_boot_wait.py
"""
from __future__ import annotations

import threading
import time

from subfleet import daemon as daemon_module
from subfleet.procs import InspectionError, ProcessTable
from tests.unit.test_daemon_settle import (  # noqa: F401  (the fixture is used by name)
    ATTEMPT, JOB, STARTED, add_running, attempt, daemon, never_census, publish_receipt)

SLOW = 2.0


def _run_concurrently(core, attempts):
    took = {}

    def tick(aid):
        began = time.monotonic()
        core._process_attempt(aid)
        took[aid] = round(time.monotonic() - began, 2)
    threads = [threading.Thread(target=tick, args=(aid,)) for aid in attempts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return took


def test_probe_slow_boot_read_holds_every_asker(daemon, monkeypatch):
    del daemon._process_table                                 # the daemon's own shared table
    daemon._table, daemon._table_lock = (None, 0.0), threading.Lock()
    attempts = [ATTEMPT, add_running(daemon, JOB + "-b", 5252), add_running(daemon, JOB + "-c", 6262),
                add_running(daemon, JOB + "-d", 7272)]
    ps_reads, boot_reads = [], []

    def snapshot():
        ps_reads.append(1)
        return ProcessTable({pid: (1, pid, "Ss", STARTED) for pid in (4242, 5252, 6262, 7272)})

    def slow_boot_id():
        boot_reads.append(1)
        time.sleep(SLOW)                                      # a slow sysctl (its cap is 10 s)
        return "boot"
    monkeypatch.setattr(daemon_module.procs, "snapshot", snapshot)
    monkeypatch.setattr(daemon_module.procs, "boot_id", slow_boot_id)
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: "alive")
    daemon._contain = never_census
    took = _run_concurrently(daemon, attempts)
    print(f"\n[slow sysctl {SLOW}s] ps reads={len(ps_reads)} boot reads={len(boot_reads)} per-attempt tick durations: {took}")
    held = [aid for aid, seconds in took.items() if seconds >= SLOW * .9]
    print(f"[slow sysctl] workers held for the whole boot read: {len(held)} of {len(attempts)}")


def test_probe_sysctl_outage_delays_every_attempts_receipt(daemon, monkeypatch):
    """A hung sysctl (every read fails after its cap): each interval's table fails its boot read once, but
    every attempt that asks waits for it, so a receipt published meanwhile is read only after the wait."""
    del daemon._process_table
    daemon._table, daemon._table_lock = (None, 0.0), threading.Lock()
    other = add_running(daemon, JOB + "-b", 5252)

    def snapshot():
        return ProcessTable({pid: (1, pid, "Ss", STARTED) for pid in (4242, 5252)})

    def hung_boot_id():
        time.sleep(SLOW)
        raise InspectionError("sysctl inspection unavailable")
    monkeypatch.setattr(daemon_module.procs, "snapshot", snapshot)
    monkeypatch.setattr(daemon_module.procs, "boot_id", hung_boot_id)
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: "unknown")
    daemon._contain = never_census
    took = _run_concurrently(daemon, [ATTEMPT, other])
    print(f"\n[hung sysctl {SLOW}s] per-attempt tick durations: {took}; states: "
          f"{[daemon.store.get_attempt(a)['state'] for a in (ATTEMPT, other)]}")
