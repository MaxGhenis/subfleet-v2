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

Each window starts once the daemon has settled after what was just set up (`settle`),
so it does not measure admission's first look at a new queue. It is not quite the
steady state either: in the saturated window the waiting jobs' recheck clocks
(C-6.10) are still lengthening, and only the closed window waits for them to reach
their ceiling first.

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


def looked_at_every_job(daemon: Daemon) -> bool:
    """C-6.10, C-6.11: the last whole admission pass left no job waiting for its first look.

    A look at a job sets its clock, and the pass records the clock with the hold.
    A pass that finds the fleet full looks at no job after that one and holds them
    as `fleet-full` without a clock, so while the fleet is full a new queue gets
    its first looks one job a pass, spread over seconds under load. A job held
    behind an older one of its tier (C-6.9) is not looked at at all.
    """
    return all("next_check_at" in hold or hold["reason"] == "behind-older-job"
               for hold in daemon._holds.values())


def settle(daemon: Daemon, quiet_s: float, deadline_s: float) -> bool:
    """Wait until the daemon has finished reacting to what was just set up; False at the deadline.

    Settled means that two admission passes have noted what they left since the
    call, so the latest began after it; that the latest left no job waiting for its
    first look (`looked_at_every_job`); and that the decision count has then not
    changed for `quiet_s`. The event count is not waited on: every look at a
    waiting job writes one event, because the look moves its `next_check_at`, and
    C-6.10 spaces those looks out without ever stopping them. What a look leaves
    out is a decision row, unless its verdict changed.
    """
    decisions = lambda: daemon.store.one("SELECT count(*) n FROM decisions")["n"]   # noqa: E731
    deadline = time.monotonic() + deadline_s
    # Replaced whole when a pass that ran to its end notes what it left (C-6.11); a
    # pass that raises leaves it as it was.
    seen, passes = daemon._admission, 0
    last, since = decisions(), time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(.05)
        if daemon._admission is not seen:
            seen, passes = daemon._admission, passes + 1
        count = decisions()
        if count != last or passes < 2 or not looked_at_every_job(daemon):
            last, since = count, time.monotonic()
        elif time.monotonic() - since >= quiet_s:
            return True
    return False


def measure(window_s: float = 10, history: int = 300, waiting: int = 12, settle_s: float = 1,
            closed_settle_s: float = 40, deadline_s: float = 60) -> dict:
    """`settle_s` is how long the decision count must stay unchanged before a window
    starts, and `deadline_s` how long to wait for that before measuring anyway;
    `settled` in the result says which windows had it."""
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
        settled = {}
        try:
            settled["idle"] = settle(daemon, settle_s, deadline_s)
            idle = share(window_s)
            for index in range(RUNNING):
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"],
                                         start_new_session=True)
                guardians.append(child)
                # `ps` can miss a pid for a few milliseconds after the fork.
                started = procs.proc_start_retry(child.pid, alive=lambda c=child: c.poll() is None)
                # The job and its attempt are written together, running: a job
                # submitted first would be queued, and the admission pass running
                # beside this would reserve the same attempt id.
                job_id = f"20260924-000000-running-{index}"
                with daemon.store.transaction("measure.running") as tx:
                    tx.execute("INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,"
                               "sandbox,pinned_model,created_at,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (job_id, f"running-{index}", "digest", "dispatch", "running", str(root),
                                str(root / "prompt.md"), "read-only", "astra", utcnow(), utcnow()))
                    tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,"
                               "guardian_pid,child_pid,pgid,boot_id,proc_start,started_at,reserved_at,evidence_json) "
                               "VALUES(?,?,1,'codex-1','gpt-6-astra','running',?,?,?,?,?,?,?,'{}')",
                               (f"{job_id}/a1", job_id, child.pid, child.pid, child.pid, procs.boot_id(),
                                started, utcnow(), utcnow()))
                attempt_dir(root, job_id, 1).mkdir(parents=True, exist_ok=True)
            for index in range(waiting):
                daemon.dispatch("submit", harness.submit_args(
                    name=f"waiting-{index}", tier=("easy", "standard", "hard")[index % 3],
                    pinned_model=("astra", "terra")[index % 2]))
            # Admission's first look at the new queue writes a decision row per job it
            # evaluates; the window is for the looks after that.
            settled["saturated"] = settle(daemon, settle_s, deadline_s)
            rows = lambda: (daemon.store.one("SELECT count(*) n FROM events")["n"],      # noqa: E731
                            daemon.store.one("SELECT count(*) n FROM decisions")["n"])
            before = rows()
            saturated = share(window_s)
            written = tuple(now - then for now, then in zip(rows(), before))
            # The closure first: were the lane still open when the fleet empties, the
            # next pass would place waiting jobs, which stay reserved (nothing is
            # launched) and fill the fleet again.
            daemon.store.put_closure(Closure("codex-1", "account", after(86400), ClosureReason.PROVIDER_LIMIT,
                                             ClockSource.REPORTED, "fixture"))
            # The rows next, so no inspection that begins after this sees a running attempt
            # whose guardian is gone (one already in flight can).
            # Every capacity wait is made due, as recovery makes it (C-6.10): these attempts
            # held no lease, so their end brings no wait forward, and each job would see the
            # change only when its own clock, up to 16 s out by now, came due.
            with daemon.store.transaction("measure.attempts_ended") as tx:
                tx.execute("UPDATE jobs SET state='failed',rc=1 WHERE job_id IN "
                           "(SELECT job_id FROM attempts WHERE state='running')")
                tx.execute("DELETE FROM leases WHERE holder IN (SELECT attempt_id FROM attempts WHERE state='running')")
                tx.execute("UPDATE attempts SET state='failed' WHERE state='running'")
                tx.execute("UPDATE jobs SET next_check_at=? WHERE state='waiting' AND wait_reason='capacity'",
                           (utcnow(),))
            for child in guardians:
                child.kill()
                child.wait()
            # C-6.10's recheck clock takes 31 s (1, 2, 4, 8, 16) to reach its 30 s ceiling;
            # what is measured is the wait once it has, not the ramp.
            time.sleep(closed_settle_s)
            settled["closed"] = settle(daemon, settle_s, deadline_s)
            before = rows()
            closed = share(window_s)
            closed_written = tuple(now - then for now, then in zip(rows(), before))
        finally:
            for child in guardians:
                child.kill()
                child.wait()
            daemon.stopping.set()
            server.join(timeout=10)
    return {"window_s": window_s, "idle": idle, "saturated": saturated, "closed": closed, "settled": settled,
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
    unsettled = [name for name, done in result["settled"].items() if not done]
    if unsettled:
        print(f"not settled by the deadline, measured anyway: {', '.join(unsettled)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
