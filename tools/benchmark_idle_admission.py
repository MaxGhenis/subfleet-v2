"""Reproduce D-ADMIT-IDLE1 without personal state or provider processes.

Run using the checkout's Python, selecting the code under test with --source.
The process hosts a real Daemon and its real control loop; periodic provider
probes, conversation cataloguing and retention are disabled to isolate admission.
Every filesystem and database fixture is beneath --state-root.
"""
from __future__ import annotations

import argparse
from collections import Counter
import cProfile
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import pstats
import resource
import subprocess
import sys
import threading
import time


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--jobs", type=int, default=100)
    parser.add_argument("--events", type=int, default=1_000_000)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--label", default="benchmark")
    return parser.parse_args()


def main():
    args = arguments()
    source = args.source.resolve()
    root = args.state_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(source))
    # Set the entire personal-path registry before importing daemon code. Nothing
    # in this process uses the operator's credential, registry or daemon roots.
    from subfleet.home_paths import SAFE_HOME_PATHS, remove_credentials
    remove_credentials(os.environ)
    os.environ.update({name: str(root / "isolated" / relative)
                       for name, relative in SAFE_HOME_PATHS.items()})
    os.environ["HOME"] = str(root / "isolated" / "home")
    Path(os.environ["HOME"]).mkdir(parents=True)

    from subfleet import daemon as daemon_module
    from subfleet.adapters import registry
    from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner
    from subfleet.daemon import Daemon, after, utcnow
    from tests.fake.conftest import Harness
    from tests.fake_adapter import FakeAdapter

    daemon_module.procs.boot_id = lambda: "idle-admission-benchmark-boot"
    daemon_module.procs.proc_start = lambda pid: "idle-admission-benchmark-start"
    daemon_module.capacity.read_desktop_account = lambda: None
    registry._factories = {"codex": FakeAdapter, "claude": FakeAdapter}
    fixture = Harness(root)
    policy_path = root / "policy.json"
    policy = json.loads(policy_path.read_text())
    policy.setdefault("admission", {})["disk"]["enabled"] = False
    policy_path.write_text(json.dumps(policy))
    repo = fixture.workdir
    for command in (("init", "-b", "task/benchmark"), ("config", "user.name", "Synthetic Benchmark"),
                    ("config", "user.email", "synthetic@example.invalid")):
        subprocess.run(["git", "-C", str(repo), *command], check=True, capture_output=True)
    (repo / "tracked.txt").write_text("synthetic fixture\n")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "synthetic fixture"],
                   check=True, capture_output=True)
    service = Daemon(root, desktop_prober=lambda: None)
    try:
        for number in range(1, 5):
            lane_id = f"codex-{number}"
            home = root / f"lane-home-{number}"
            home.mkdir(exist_ok=True)
            # The first lane already exists with immutable Harness credentials.
            if number > 1:
                service.store.put_lane(Lane(lane_id, "codex", f"codex:synthetic-{number}",
                    Credential("codex", str(home), "home"), str(home), LaneOwner.V2, False))
            service.store.add_closure(Closure(lane_id, "account", after(86400),
                ClosureReason.OPERATOR_HOLD, ClockSource.REPORTED, "synthetic-benchmark"))
        jobs = []
        for number in range(args.jobs):
            # Half exercise C-6.20's unprepared wait. The others follow the real
            # workspace/route/attempt.reserved transaction without reserving.
            sandbox = "workspace-write" if number % 2 == 0 else "read-only"
            result = service.dispatch("submit", fixture.submit_args(
                sandbox=sandbox, pinned_model="terra", caller_session=None))
            jobs.append(result["job_id"])
        with service.store.transaction("synthetic.events") as tx:
            tx.execute("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i < ?) "
                       "INSERT INTO events(ts,kind,data_json) SELECT ?, 'synthetic.padding', '{}' FROM n",
                       (args.events, utcnow()))

        # The next two real passes establish the unchanged verdict at its
        # steady ceiling, excluding ramp-up from the two-minute measurement.
        service._admit()
        for job_id in jobs:
            assert job_id in service._capacity_waits, service.store.get_job(job_id)
            service._capacity_waits[job_id]["rechecks"] = 20
            service.store.update_job(job_id, next_check_at=utcnow())
        service._admit()
        ceilings = []
        for job_id in jobs:
            wait = service._capacity_waits[job_id]
            job = service.store.get_job(job_id)
            checked = datetime.fromisoformat(wait["checked_at"].replace("Z", "+00:00"))
            due = datetime.fromisoformat(job["next_check_at"].replace("Z", "+00:00"))
            ceilings.append((due - checked).total_seconds())
        # Preserve the actual rule's ceiling and distribute its clocks evenly,
        # reproducing a queue in which jobs do not share one due instant.
        ceiling = max(ceilings)
        for number, job_id in enumerate(jobs):
            due = datetime.now(timezone.utc) + timedelta(seconds=(number + .5) * ceiling / len(jobs))
            service.store.update_job(job_id, next_check_at=due.isoformat().replace("+00:00", "Z"))

        local = threading.local()
        counts = Counter()
        caller_counts = Counter()
        passes = []
        original_lstat = os.lstat
        original_pass = service._admit_pass

        def counted_lstat(*positional, **keyword):
            if getattr(local, "active", False):
                counts["lstat"] += 1
                if getattr(local, "callers", False):
                    frame = sys._getframe(1)
                    chain = []
                    for _ in range(8):
                        if frame is None:
                            break
                        chain.append(f"{frame.f_code.co_filename}:{frame.f_code.co_name}:{frame.f_lineno}")
                        frame = frame.f_back
                    caller_counts[" <- ".join(chain)] += 1
            return original_lstat(*positional, **keyword)

        def counted_pass(holds, tally, *, kind="detached"):
            if kind != "detached":
                return original_pass(holds, tally, kind=kind)
            started = time.perf_counter()
            before = counts["lstat"]
            local.active = True
            try:
                return original_pass(holds, tally, kind=kind)
            finally:
                local.active = False
                passes.append({"duration_s": time.perf_counter() - started,
                               "lstat": counts["lstat"] - before,
                               "placed": tally.get("placed", 0)})

        os.lstat = counted_lstat
        service._admit_pass = counted_pass
        profiles = {}
        if args.profile:
            # Two separately labelled snapshots: all due and all not due.
            for mode in ("due", "not_due"):
                for job_id in jobs:
                    service.store.update_job(job_id, next_check_at=utcnow() if mode == "due" else after(3600))
                profiler = cProfile.Profile()
                caller_counts.clear()
                local.callers = True
                profiler.runcall(service._admit)
                local.callers = False
                stats = pstats.Stats(profiler)
                top = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:15]
                profiles[mode] = {
                    "pass": passes[-1],
                    "top15_cumulative": [{"function": f"{key[0]}:{key[1]}({key[2]})",
                        "primitive_calls": value[0], "calls": value[1], "self_s": value[2],
                        "cumulative_s": value[3]} for key, value in top],
                    "lstat_callers": dict(caller_counts.most_common()),
                }
                profiler.dump_stats(str(args.output.with_suffix(f".{mode}.prof")))
            for number, job_id in enumerate(jobs):
                due = datetime.now(timezone.utc) + timedelta(seconds=(number + .5) * ceiling / len(jobs))
                service.store.update_job(job_id, next_check_at=due.isoformat().replace("+00:00", "Z"))

        passes.clear()
        counts.clear()
        # Run the actual daemon control loop and worker scheduler. Unrelated
        # timer and conversation work cannot manufacture admission wakes.
        service._recovery_complete.set()
        service.timers.tick = lambda: None
        service.conversations.tick = lambda: None
        service._last_maintenance = time.monotonic() + args.seconds + 3600
        initial_event = service.store.one("SELECT max(event_id) n FROM events")["n"]
        initial_events = service.store.one("SELECT count(*) n FROM events")["n"]
        initial_usage = resource.getrusage(resource.RUSAGE_SELF)
        start_cpu = time.process_time()
        start_wall = time.monotonic()
        if args.seconds > 0:
            controller = threading.Thread(target=service._control, name="synthetic-daemon-control")
            controller.start()
            service._control_thread = controller
            while time.monotonic() - start_wall < args.seconds:
                time.sleep(min(.25, max(0, args.seconds - (time.monotonic() - start_wall))))
            service.stopping.set()
            controller.join()
            # Finish the final detached worker before reading counters.
            with service._pass_locks["detached"]:
                pass
        wall = time.monotonic() - start_wall
        cpu = time.process_time() - start_cpu
        usage = resource.getrusage(resource.RUSAGE_SELF)
        committed = service.store.query("SELECT kind,count(*) n FROM events WHERE event_id > ? GROUP BY kind",
                                        (initial_event,))
        waiting_jobs = service.store.one(
            "SELECT count(*) n FROM jobs WHERE state='waiting' AND wait_reason='capacity'")["n"]
        attempts = service.store.one("SELECT count(*) n FROM attempts")["n"]
        assert waiting_jobs == args.jobs and attempts == 0, (waiting_jobs, attempts)
        result = {
            "label": args.label, "source": str(source), "python": sys.version,
            "jobs": len(jobs), "lanes": 4, "operator_hold_closures": 4,
            "initial_events": initial_events, "steady_ceiling_s": ceiling,
            "waiting_jobs_end": waiting_jobs, "attempts_end": attempts,
            "profiles_only": args.seconds == 0,
            "seconds_wall": wall, "seconds_cpu": cpu,
            "cpu_s_per_min": cpu * 60 / wall if args.seconds else None,
            "user_cpu_s": usage.ru_utime - initial_usage.ru_utime,
            "system_cpu_s": usage.ru_stime - initial_usage.ru_stime,
            "detached_passes": len(passes),
            "detached_passes_per_min": len(passes) * 60 / wall if args.seconds else None,
            "events_committed": sum(row["n"] for row in committed),
            "events_per_hour": sum(row["n"] for row in committed) * 3600 / wall if args.seconds else None,
            "event_kinds": committed,
            "lstat_calls": counts["lstat"],
            "lstat_per_pass": counts["lstat"] / len(passes) if passes else None,
            "pass_duration_s_mean": sum(item["duration_s"] for item in passes) / len(passes) if passes else None,
            "pass_duration_s_max": max((item["duration_s"] for item in passes), default=None),
            "profiles": profiles,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({key: value for key, value in result.items() if key != "profiles"}), flush=True)
    finally:
        if "original_lstat" in locals():
            os.lstat = original_lstat
        service.close()


if __name__ == "__main__":
    main()
