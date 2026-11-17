"""Reproduce D-DAEMON-QUIET1 measurements using synthetic state only.

Run from the repository with ``uv run --no-sync python <this file> --output <path>``.
The baseline methods are extracted from the task's original commit, so both
versions run against identical fixtures in the same Python/SQLite runtime.
The measurement never constructs a Daemon or inspects a host process.
"""

from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from types import MethodType, SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from subfleet import daemon as daemon_module, procs, status_json  # noqa: E402
from subfleet.contracts import Credential, Lane, LaneOwner  # noqa: E402
from subfleet.daemon import Daemon  # noqa: E402
from subfleet.store import Store  # noqa: E402
from subfleet.timers import Timers  # noqa: E402

BASELINE_COMMIT = "a8624a4899a2"
NOW = datetime(2026, 11, 6, 13, 25, tzinfo=timezone.utc)
BIRTH = "Fri Nov  6 13:25:00 2026"


def historical_function(path: str, name: str, namespace: dict, *, owner: str | None = None):
    source = subprocess.run(
        ["git", "show", f"{BASELINE_COMMIT}:{path}"], cwd=REPO,
        text=True, check=True, capture_output=True,
    ).stdout
    body = ast.parse(source).body
    if owner:
        body = next(node.body for node in body if isinstance(node, ast.ClassDef) and node.name == owner)
    node = next(node for node in body if isinstance(node, ast.FunctionDef) and node.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    scope = dict(namespace)
    exec(compile(module, f"{BASELINE_COMMIT}:{path}", "exec"), scope)
    return scope[name]


def synthetic_store(path: Path, jobs: int, events_per_job: int) -> None:
    with Store(path) as store:
        conn = store.connection
        with store.transaction("benchmark.seed"):
            conn.executemany(
                "INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,sandbox,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [(f"synthetic-{n:03}", f"request-{n}", "synthetic", "run",
                  ("running", "waiting", "queued")[n % 3], str(path.parent),
                  str(path.parent / "prompt.md"), "read-only", NOW.isoformat()) for n in range(jobs)],
            )

            def events():
                for n in range(jobs):
                    jid = f"synthetic-{n:03}"
                    batch = {"batch": {"id": "synthetic", "label": "measurement", "index": n + 1, "size": jobs}}
                    yield NOW.isoformat(), "job.submitted", jid, json.dumps(batch if n % 2 else {})
                    for _ in range(events_per_job - 1):
                        yield NOW.isoformat(), "attempt.observed_alive", jid, "{}"

            conn.executemany("INSERT INTO events(ts,kind,job_id,data_json) VALUES (?,?,?,?)", events())
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


@contextmanager
def batch_version(core: Daemon, batches, attach):
    original_batches = core._batches
    original_attach = status_json.attach_batches
    core._batches = MethodType(batches, core)
    status_json.attach_batches = attach
    try:
        yield
    finally:
        core._batches = original_batches
        status_json.attach_batches = original_attach


def sqlite_pages(path: Path, statements: list[str]) -> dict:
    """Replay traced SQL with a fresh SQLite page cache, using shell .stats.

    Misses count SQLite page loads from the OS, not physical disk reads. The OS
    file cache is deliberately left alone. Each statement's counters are reset
    by the shell, so totals are summed over all statements in an operation.
    """
    text = "PRAGMA cache_size=-2000;\n.stats on\n" + "\n".join(sql + ";" for sql in statements)
    result = subprocess.run(["sqlite3", "-batch", str(path)], input=text, text=True,
                            capture_output=True, check=True)
    return {"cache_hits": sum(map(int, re.findall(r"Page cache hits:\s+(\d+)", result.stdout))),
            "cache_misses": sum(map(int, re.findall(r"Page cache misses:\s+(\d+)", result.stdout))),
            "statements": len(statements)}


def operation_measurements(path: Path, root: Path, reps: int, batches, attach) -> dict:
    with Store(path, read_only=True, initialize=False) as store:
        store.connection.execute("PRAGMA cache_size=-2000")
        core = object.__new__(Daemon)
        core.store = store
        timer = object.__new__(Timers)
        timer.store, timer.root, timer.policy = store, root, {}
        timer.now, timer.probe_record = lambda: NOW, None
        timer.alerts = SimpleNamespace(active=lambda: [])
        snapshot = {"jobs": store.list_jobs(), "attempts": [], "lanes": []}

        def listing():
            return Daemon.dispatch.__wrapped__(core, "list", {"running": True})

        def status():
            return timer.publish_status(dict(snapshot))

        result = {}
        with batch_version(core, batches, attach):
            for name, operation in (("list", listing), ("status_write", status)):
                answer = operation()  # Warm OS file cache before timed repetitions.
                timings = []
                for _ in range(reps):
                    began = time.perf_counter()
                    assert operation() == answer
                    timings.append(time.perf_counter() - began)
                statements = []
                store.connection.set_trace_callback(statements.append)
                operation()
                store.connection.set_trace_callback(None)
                result[name] = {"seconds": timings, "median_seconds": statistics.median(timings),
                                "sqlite_pages_fresh_connection": sqlite_pages(path, statements)}
                result[name]["jobs_returned"] = len(answer["jobs"] if name == "list" else answer["jobs"]["live"])
        return result


def liveness_measurement(path: Path, record_owned) -> dict:
    """One fresh census each fake second in [0, 3600), with stable ownership."""
    with Store(path) as store:
        home = str(path.parent / "synthetic-home")
        store.put_lane(Lane("codex-1", "codex", "codex:synthetic", Credential("codex", home, "home"),
                            home, LaneOwner.V2, False))
        store.add_job(job_id="synthetic", request_id="synthetic", payload_digest="synthetic", kind="run",
                      state="running", workdir=str(path.parent), prompt_path=str(path.parent / "prompt.md"),
                      sandbox="read-only", created_at=NOW.isoformat())
        evidence = {"owned_identities": {
            str(pid): {"pid": pid, "boot_id": "synthetic-boot", "proc_start": BIRTH}
            for pid in (4242, 4243)}}
        store.add_attempt(attempt_id="synthetic/a1", job_id="synthetic", seq=1, lane_id="codex-1",
                          model_requested="astra", state="running", guardian_pid=4242, child_pid=4243,
                          pgid=4242, boot_id="synthetic-boot", proc_start=BIRTH,
                          started_at=NOW.isoformat(), evidence_json=json.dumps(evidence))
        core = object.__new__(Daemon)
        core.store = store
        core.inspect_interval_s = 1.0
        core._last_observed_alive_write = {}
        clock = [0]
        real_stamp, real_monotonic = daemon_module.utcnow_ms, daemon_module.time.monotonic
        historical_stamp = record_owned.__globals__["utcnow_ms"]
        frozen_stamp = lambda: (NOW + timedelta(seconds=clock[0])).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        daemon_module.utcnow_ms = frozen_stamp
        # The historical function has its own copied globals, so freeze both.
        record_owned.__globals__["utcnow_ms"] = frozen_stamp
        daemon_module.time.monotonic = lambda: float(clock[0])
        try:
            for second in range(3600):
                clock[0] = second
                table = procs.ProcessTable({4242: (1, 4242, "S", BIRTH), 4243: (4242, 4242, "S", BIRTH)}, "synthetic-boot")
                record_owned(core, dict(store.get_attempt("synthetic/a1")), table)
        finally:
            record_owned.__globals__["utcnow_ms"] = historical_stamp
            daemon_module.utcnow_ms, daemon_module.time.monotonic = real_stamp, real_monotonic
        counts = {row["kind"]: row["n"] for row in store.query(
            "SELECT kind,COUNT(*) n FROM events WHERE attempt_id='synthetic/a1' GROUP BY kind")}
        return {"fake_duration_s": 3600, "inspection_interval_s": 1,
                "observed_alive_commits_per_attempt_hour": counts.get("attempt.observed_alive", 0),
                "ownership_events": {kind: n for kind, n in counts.items() if kind in
                                     ("attempt.processes_recorded", "attempt.counted_recorded")},
                "last_saved_observation": json.loads(store.get_attempt("synthetic/a1")["evidence_json"])["last_observed_alive_at"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=100)
    parser.add_argument("--events-per-job", type=int, default=5000)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--skip-liveness", action="store_true")
    parser.add_argument("--liveness-only", action="store_true",
                        help="Keep existing batch measurements in --output and replace liveness measurements.")
    parser.add_argument("--scratch-root", type=Path,
                        default=Path(os.environ.get("TMPDIR", tempfile.gettempdir())))
    args = parser.parse_args()
    old_batches = historical_function("subfleet/daemon.py", "_batches", vars(daemon_module), owner="Daemon")
    old_attach = historical_function("subfleet/status_json.py", "attach_batches", vars(status_json))
    old_owned = historical_function("subfleet/daemon.py", "_record_owned", vars(daemon_module), owner="Daemon")
    args.scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="daemon-quiet-measure-", dir=args.scratch_root) as scratch:
        root = Path(scratch)
        path = root / "synthetic.sqlite3"
        if args.liveness_only:
            results = json.loads(args.output.read_text())
        else:
            synthetic_store(path, args.jobs, args.events_per_job)
            results = {
                "baseline_commit": BASELINE_COMMIT, "platform": platform.platform(),
                "python": platform.python_version(), "sqlite_python": sqlite3.sqlite_version,
                "sqlite_shell": subprocess.run(["sqlite3", "--version"], text=True, capture_output=True, check=True).stdout.strip(),
                "fixture": {"open_jobs": args.jobs, "events_per_job": args.events_per_job,
                            "job_events": args.jobs * args.events_per_job, "batch_labeled_jobs": args.jobs // 2,
                            "sqlite_file_bytes": path.stat().st_size, "page_cache_kib": 2000},
                "method": "actual list dispatch and Timers.publish_status; daemon/timer cores assembled without startup; stable empty alerts and no lanes or conversations; median of warmed repetitions; page counts replay traced SQL in fresh cache (OS cache retained)",
            }
            for name, batches, attach in (("before", old_batches, old_attach),
                                          ("after", Daemon._batches, status_json.attach_batches)):
                results[name] = operation_measurements(path, root / name, args.repetitions, batches, attach)
        if not args.skip_liveness:
            results["liveness"] = {"before": liveness_measurement(root / "alive-before.sqlite3", old_owned),
                                   "after": liveness_measurement(root / "alive-after.sqlite3", Daemon._record_owned)}
            assert results["liveness"]["before"]["observed_alive_commits_per_attempt_hour"] == 3600
            assert results["liveness"]["after"]["observed_alive_commits_per_attempt_hour"] == 60
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
