#!/usr/bin/env python3
"""Measure what the daemon's control loop costs while nothing can be admitted (C-5.12, C-6.12).

Runs a real `Daemon` with the production tick on a temporary state root, in this
process, and reports CPU as a share of one core over fixed windows:

- idle: a store with history (accepted terminal jobs) and an empty queue
- saturated: `max_active_attempts` running attempts, whose guardians are real
  sleeping processes, and a queue of waiting jobs across three tiers and two models
- closed: the same queue with the attempts ended and every lane closed, so each
  job is evaluated on its own clock and no lane will take it (C-6.10)

"daemon" is this process (`getrusage(RUSAGE_SELF)`); "children" is the `ps`,
`sysctl` and `git` it started and reaped (`RUSAGE_CHILDREN`). The last line is the
rows the daemon wrote while the fleet stayed full. No guardian is launched, no
provider runs, and the timers (probe, keepalive, sessions mirror) are switched off.

Usage: uv run python tools/measure_idle_cost.py [--window 10] [--history 300] [--waiting 12]
Needs /bin/ps and /usr/sbin/sysctl reachable (not a sandboxed shell). Nothing here
touches ~/.subfleet; everything lives under a temp root in /tmp. To compare two
revisions, run it from a checkout of each.
"""

from __future__ import annotations

import argparse
import resource
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from subfleet import procs  # noqa: E402
from subfleet.adapters.registry import register  # noqa: E402
from subfleet.contracts import (ClockSource, Closure, ClosureReason, Reading,  # noqa: E402
                                ReadingLabel, attempt_dir)
from subfleet.daemon import Daemon, after, utcnow  # noqa: E402
from tests.fake.conftest import Harness  # noqa: E402
from tests.fake_adapter import FakeAdapter  # noqa: E402

RUNNING = 4


def cpu() -> tuple[float, float]:
    own, children = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime, children.ru_utime + children.ru_stime


def share(window_s: float) -> tuple[float, float]:
    """(daemon, children) CPU over the next `window_s`, as percent of one core."""
    before, started = cpu(), time.monotonic()
    time.sleep(window_s)
    after_, elapsed = cpu(), time.monotonic() - started
    return 100 * (after_[0] - before[0]) / elapsed, 100 * (after_[1] - before[1]) / elapsed


def measure(window_s: float = 10, history: int = 300, waiting: int = 12, settle_s: float = 1,
            closed_settle_s: float = 40) -> dict:
    with tempfile.TemporaryDirectory(prefix="sfm-", dir="/tmp") as directory:
        root = Path(directory)
        harness = Harness(root)
        register("codex", FakeAdapter)
        daemon = Daemon(root, desktop_prober=lambda: None)
        daemon._launch = lambda a: None          # a reserved attempt stays reserved; nothing is started
        # The timers are periodic work with costs of their own, and the sessions mirror
        # would read this machine's real transcripts: what is measured is the control loop.
        daemon.timers.intervals.clear()
        daemon.policy["caps"].update(max_active_attempts=RUNNING, max_in_flight_per_lane=RUNNING * 2)
        daemon.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                         ReadingLabel.PROVIDER, "fixture", utcnow()))
        for index in range(history):
            job_id = f"20260901-000000-history-{index}"
            daemon.store.add_job(job_id=job_id, request_id=f"history-{index}", payload_digest="digest", kind="run",
                                 state="succeeded", workdir=str(root), prompt_path=str(root / "prompt.md"),
                                 sandbox="read-only")
            daemon.store.add_attempt(attempt_id=job_id + "/a1", job_id=job_id, seq=1, lane_id="codex-1",
                                     model_requested="gpt-6-astra", state="succeeded", evidence_json="{}")
            daemon.store.update_job(job_id, accepted_attempt_id=job_id + "/a1")
        server = threading.Thread(target=daemon.serve_forever, daemon=True)
        server.start()
        guardians: list[subprocess.Popen] = []
        try:
            time.sleep(settle_s)
            idle = share(window_s)
            for index in range(RUNNING):
                job_id = daemon.dispatch("submit", harness.submit_args(name=f"running-{index}"))["job_id"]
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"],
                                         start_new_session=True)
                guardians.append(child)
                attempt_dir(root, job_id, 1).mkdir(parents=True, exist_ok=True)
                daemon.store.add_attempt(attempt_id=f"{job_id}/a1", job_id=job_id, seq=1, lane_id="codex-1",
                                         model_requested="gpt-6-astra", state="running",
                                         guardian_pid=child.pid, child_pid=child.pid, pgid=child.pid,
                                         boot_id=procs.boot_id(), proc_start=procs.proc_start(child.pid),
                                         started_at=utcnow(), evidence_json="{}")
                daemon.store.update_job(job_id, state="running", started_at=utcnow())
            for index in range(waiting):
                daemon.dispatch("submit", harness.submit_args(
                    name=f"waiting-{index}", tier=("easy", "standard", "hard")[index % 3],
                    pinned_model=("astra", "terra")[index % 2]))
            time.sleep(settle_s)
            rows = lambda: (daemon.store.one("SELECT count(*) n FROM events")["n"],      # noqa: E731
                            daemon.store.one("SELECT count(*) n FROM decisions")["n"])
            before = rows()
            saturated = share(window_s)
            written = tuple(now - then for now, then in zip(rows(), before))
            # The rows first, so the daemon never sees a running attempt whose guardian is gone.
            with daemon.store.transaction("measure.attempts_ended") as tx:
                tx.execute("UPDATE jobs SET state='failed',rc=1 WHERE job_id IN "
                           "(SELECT job_id FROM attempts WHERE state='running')")
                tx.execute("DELETE FROM leases WHERE holder IN (SELECT attempt_id FROM attempts WHERE state='running')")
                tx.execute("UPDATE attempts SET state='failed' WHERE state='running'")
            for child in guardians:
                child.kill()
                child.wait()
            daemon.store.put_closure(Closure("codex-1", "account", after(86400), ClosureReason.PROVIDER_LIMIT,
                                             ClockSource.REPORTED, "fixture"))
            # C-6.10's recheck clock takes 31 s (1, 2, 4, 8, 16) to reach its 30 s ceiling;
            # what is measured is the wait once it has, not the ramp.
            time.sleep(closed_settle_s)
            before = rows()
            closed = share(window_s)
            closed_written = tuple(now - then for now, then in zip(rows(), before))
        finally:
            for child in guardians:
                child.kill()
                child.wait()
            daemon.stopping.set()
            server.join(timeout=10)
    return {"window_s": window_s, "idle": idle, "saturated": saturated, "closed": closed,
            "events_written": written[0], "decisions_written": written[1],
            "closed_events_written": closed_written[0], "closed_decisions_written": closed_written[1]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--window", type=float, default=10, help="seconds per measurement (default 10)")
    parser.add_argument("--history", type=int, default=300, help="accepted terminal jobs already in the store")
    parser.add_argument("--waiting", type=int, default=12, help="jobs queued behind the full fleet")
    args = parser.parse_args(argv)
    result = measure(args.window, args.history, args.waiting)
    print("| state | daemon | children |\n|---|---:|---:|")
    print(f"| idle, empty queue | {result['idle'][0]:.1f}% | {result['idle'][1]:.1f}% |")
    print(f"| {RUNNING} running, {args.waiting} waiting | {result['saturated'][0]:.1f}% | {result['saturated'][1]:.1f}% |")
    print(f"| every lane closed, {args.waiting} waiting | {result['closed'][0]:.1f}% | {result['closed'][1]:.1f}% |")
    print(f"\nrows written in {args.window:g} s under a full fleet: "
          f"{result['events_written']} events, {result['decisions_written']} decisions")
    print(f"rows written in {args.window:g} s with every lane closed: "
          f"{result['closed_events_written']} events, {result['closed_decisions_written']} decisions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
