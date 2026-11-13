"""Profile a closed benchmark fixture, without rebuilding its 129 jobs."""
import argparse
import cProfile
import hashlib
import json
import os
from pathlib import Path
import pstats
import statistics
import sys
import time


parser = argparse.ArgumentParser()
parser.add_argument("--source", type=Path, required=True)
parser.add_argument("--state-root", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--label", required=True)
parser.add_argument("--samples", type=int, default=7)
args = parser.parse_args()
source = args.source.resolve()
root = args.state_root.resolve()
assert "sf-ia-f1" in str(root) and (root / "state.sqlite3").is_file()
sys.path.insert(0, str(source))
from subfleet.home_paths import SAFE_HOME_PATHS, remove_credentials

remove_credentials(os.environ)
os.environ.update({name: str(root / "isolated" / relative)
                   for name, relative in SAFE_HOME_PATHS.items()})
os.environ["HOME"] = str(root / "isolated" / "home")
from subfleet import daemon as daemon_module
from subfleet.adapters import registry
from subfleet.daemon import Daemon, utcnow
from tests.fake_adapter import FakeAdapter

daemon_module.procs.boot_id = lambda: "idle-admission-benchmark-boot"
daemon_module.procs.proc_start = lambda pid: "idle-admission-benchmark-start"
daemon_module.capacity.read_desktop_account = lambda: None
registry._factories = {"codex": FakeAdapter, "claude": FakeAdapter}
service = Daemon(root, desktop_prober=lambda: None)
try:
    jobs = service.store.query("SELECT * FROM jobs WHERE state IN ('queued','waiting')")
    assert len(jobs) == 129 and all(job["sandbox"] == "workspace-write" for job in jobs)
    assert service.store.one("SELECT count(*) n FROM events")["n"] >= 100000
    assert not service.store.list_attempts()

    def due():
        with service.store.transaction("synthetic.all_due") as tx:
            tx.execute("UPDATE jobs SET next_check_at=? WHERE state IN ('queued','waiting')", (utcnow(),))

    for _ in range(2):
        due()
        for job_id, wait in service._capacity_waits.items():
            service._capacity_waits[job_id] = {**wait, "rechecks": 20}
        service._admit()
    assert all(service._capacity_waits[job["job_id"]]["hold"].get("unprepared") for job in jobs)
    samples = []
    for index in range(args.samples):
        due()
        profiler = cProfile.Profile()
        started_cpu = time.process_time()
        profiler.runcall(service._admit)
        elapsed_cpu = time.process_time() - started_cpu
        stats = pstats.Stats(profiler)
        passes = [(key, value) for key, value in stats.stats.items() if key[2] == "_admit_pass"]
        routes = [(key, value) for key, value in stats.stats.items() if key[2] == "_route"]
        assert len(passes) == len(routes) == 1 and routes[0][1][1] == len(jobs)
        entry = {"index": index, "process_cpu_s": elapsed_cpu, "due_pass_s": passes[0][1][3], "route_calls": routes[0][1][1],
                 "route_cumulative_s": routes[0][1][3]}
        samples.append(entry)
        profiler.dump_stats(str(args.output.with_suffix(f".{index}.prof")))
    assert not service.store.list_attempts()
    result = {"label": args.label, "jobs": len(jobs), "samples": samples,
              "daemon_sha256": hashlib.sha256((source / "subfleet/daemon.py").read_bytes()).hexdigest(),
              "due_pass_s_median": statistics.median(item["due_pass_s"] for item in samples),
              "route_cumulative_s_median": statistics.median(item["route_cumulative_s"] for item in samples)}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
finally:
    service.close()
