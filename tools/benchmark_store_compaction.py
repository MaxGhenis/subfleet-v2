"""Reproduce D-STORE-FREE1 measurements using disposable synthetic stores only.

Run from this checkout with its development Python, for example::

    TMPDIR=/private/tmp/store-compact-217 UV_CACHE_DIR=$TMPDIR/uv-cache \
      uv run --no-sync python tools/benchmark_store_compaction.py \
      --scratch-root "$TMPDIR" \
      --output docs/reports/2026-11-07-store-compact-measurements.json

Sizes and the free-space floor use decimal GB. A source never exceeds 3 GB.
Each source uses the real schema, many immutable events and a large freelist
from deleting synthetic decisions, attempts and jobs. A source is removed as
soon as its probe ends, including after a failed or skipped measurement.

Peak named-file allocation is sampled every 10 ms. The monitor also records
filesystem free space, which includes unlinked files but is noisy on a shared
volume. SQLite's copy uses temp_store=MEMORY. Neither the benchmark nor the
operator pass opens a live store or starts a daemon/provider.
"""

from __future__ import annotations

import argparse
import atexit
from contextlib import closing
import dataclasses
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from subfleet.store import Store
from subfleet.compact_store import compact


GB = 1_000_000_000
MIB = 1024 ** 2
TIMESTAMP = "2026-11-07T00:00:00Z"


def execute_command(argv: list[str]) -> dict:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    return {"argv": argv, "returncode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr}


def inventory(root: Path) -> dict[str, int]:
    logical = allocated = 0
    for path in root.rglob("*"):
        try:
            stat = path.stat()
            if path.is_file():
                logical += stat.st_size
                allocated += stat.st_blocks * 512
        except FileNotFoundError:
            pass
    return {"logical_bytes": logical, "allocated_bytes": allocated}


class PeakMonitor:
    def __init__(self, root: Path):
        self.root = root
        self.baseline = inventory(root)
        self.baseline_free = shutil.disk_usage(root).free
        self.peak = dict(self.baseline)
        self.minimum_free = self.baseline_free
        self.samples = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="compact-benchmark-peak")

    def sample(self) -> None:
        sample = inventory(self.root)
        for key, value in sample.items():
            self.peak[key] = max(self.peak[key], value)
        self.minimum_free = min(self.minimum_free, shutil.disk_usage(self.root).free)
        self.samples += 1

    def _run(self) -> None:
        while not self._stop.wait(0.01):
            self.sample()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join()
        self.sample()

    def report(self) -> dict:
        return {"sampling_interval_s": 0.01, "samples": self.samples,
                "baseline": self.baseline, "peak": self.peak,
                "peak_extra_logical_bytes": self.peak["logical_bytes"] - self.baseline["logical_bytes"],
                "peak_extra_allocated_bytes": self.peak["allocated_bytes"] - self.baseline["allocated_bytes"],
                "filesystem_free_delta_bytes_shared_volume": self.baseline_free - self.minimum_free}


def sqlite_metrics(path: Path) -> dict:
    with closing(sqlite3.connect(path)) as connection:
        result = {key: connection.execute(f"PRAGMA {key}").fetchone()[0]
                  for key in ("page_size", "page_count", "freelist_count", "auto_vacuum")}
        result["table_rows"] = {name: connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                                for (name,) in connection.execute(
                                    "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")}
    result["file_bytes"] = path.stat().st_size
    return result


def propagation_experiment(scratch: Path, free_floor_gb: float = 55) -> dict:
    if shutil.disk_usage(scratch).free < free_floor_gb * GB + 4 * MIB:
        return {"status": "skipped", "reason": "free-space floor"}
    with tempfile.TemporaryDirectory(prefix="pragma-propagation-", dir=scratch) as name:
        root = Path(name)
        source, destination = root / "source.sqlite3", root / "destination.sqlite3"
        with closing(sqlite3.connect(source)) as connection:
            connection.execute("CREATE TABLE sample(k INTEGER PRIMARY KEY, payload BLOB)")
            connection.executemany("INSERT INTO sample VALUES(?,?)",
                                   ((number, b"x" * 10_000) for number in range(100)))
            connection.commit()
            connection.execute("DELETE FROM sample WHERE k<50")
            connection.commit()
            initial = connection.execute("PRAGMA auto_vacuum").fetchone()[0]
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            after_set = connection.execute("PRAGMA auto_vacuum").fetchone()[0]
            connection.execute("VACUUM INTO ?", (str(destination),))
        result = {"sqlite_version": sqlite3.sqlite_version,
                  "source_initial_auto_vacuum": initial,
                  "source_after_pragma_auto_vacuum": after_set,
                  "source": sqlite_metrics(source), "destination": sqlite_metrics(destination)}
        assert initial == 0 and after_set == 0 and result["destination"]["auto_vacuum"] == 2
        return result


def incremental_page_budget_experiment(scratch: Path, free_floor_gb: float) -> dict:
    """Compare fixed page budgets before choosing the daemon's one-step limit."""
    if shutil.disk_usage(scratch).free < free_floor_gb * GB + 256 * MIB:
        return {"status": "skipped", "reason": "free-space floor"}
    results = []
    with tempfile.TemporaryDirectory(prefix="incremental-budget-", dir=scratch) as name:
        root = Path(name)
        for pages in (16, 64, 128, 256):
            path = root / f"n{pages}.sqlite3"
            with closing(sqlite3.connect(path, isolation_level=None)) as connection:
                connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("CREATE TABLE events(id INTEGER PRIMARY KEY,payload BLOB)")
                connection.execute("BEGIN")
                connection.executemany("INSERT INTO events VALUES(?,?)",
                                       ((number, b"x" * 16384) for number in range(4096)))
                connection.execute("COMMIT")
                connection.execute("DELETE FROM events WHERE id%4!=0")
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                free_before = connection.execute("PRAGMA freelist_count").fetchone()[0]
                durations, reclaimed = [], []
                for _ in range(20):
                    before = connection.execute("PRAGMA freelist_count").fetchone()[0]
                    started = time.perf_counter()
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(f"PRAGMA incremental_vacuum({pages})").fetchall()
                    connection.execute("COMMIT")
                    durations.append(time.perf_counter() - started)
                    reclaimed.append(before - connection.execute("PRAGMA freelist_count").fetchone()[0])
                file_before_checkpoint = path.stat().st_size
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                results.append({"pages": pages, "free_before": free_before,
                                "reclaimed_per_step": reclaimed, "elapsed_s": durations,
                                "minimum_s": min(durations), "p50_s": percentile(durations, 0.5),
                                "p95_s": percentile(durations, 0.95), "maximum_s": max(durations),
                                "file_bytes_before_final_checkpoint": file_before_checkpoint,
                                "file_bytes_after_final_checkpoint": path.stat().st_size})
            for file in root.iterdir():
                file.unlink()
    return {"status": "measured", "sqlite_version": sqlite3.sqlite_version,
            "rows_before_delete": 4096, "payload_bytes": 16384, "delete_fraction": 0.75,
            "synchronous": "FULL", "journal_mode": "WAL", "steps_per_candidate": 20,
            "candidates": results,
            "caveat": "These raw transactions compare page counts without the daemon VM deadline; times include commit sync and OS scheduling."}


def build_store(root: Path, target_bytes: int, freelist_fraction: float = 0.21) -> dict:
    path = root / "state.sqlite3"
    with Store(path):
        pass
    # Fast seeding is deliberately outside the pass timing. Final source mode
    # matches production; all these rows are benchmark fixtures.
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA secure_delete=OFF")
        connection.execute("PRAGMA cache_size=-262144")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO lanes(lane_id,provider,account_key,credential_ref,credential_kind,created_at,updated_at)"
            " VALUES('synthetic-lane','codex','synthetic-account','synthetic-ref','home',?,?)",
            (TIMESTAMP, TIMESTAMP))
        job_count = 1024
        jobs = [(f"synthetic-{number:06}", f"synthetic-{number:06}", str(root), TIMESTAMP)
                for number in range(job_count)]
        connection.executemany(
            "INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,sandbox,created_at)"
            " VALUES(?,?,'synthetic','dispatch','succeeded',?,'/synthetic/prompt','read-only',?)", jobs)
        connection.executemany(
            "INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,reserved_at,evidence_json)"
            " VALUES(?,?,1,'synthetic-lane','synthetic-model','succeeded',?,?)",
            ((f"{job}/a1", job, TIMESTAMP, json.dumps({"padding": "x" * 1024}))
             for job, *_ in jobs))
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        event_payload = json.dumps({"padding": "event" * 6550}, separators=(",", ":"))
        decision_payload = json.dumps({"padding": "decision" * 4095}, separators=(",", ":"))
        event_number = decision_number = 0

        def size() -> int:
            return connection.execute("PRAGMA page_count").fetchone()[0] * page_size

        while size() < target_bytes * (1 - freelist_fraction) * 0.5 / 0.79:
            connection.executemany(
                "INSERT INTO events(ts,kind,job_id,attempt_id,lane_id,data_json) VALUES(?,?,?,?,?,?)",
                ((TIMESTAMP, f"synthetic.kind.{number % 17}", f"synthetic-{number % job_count:06}",
                  f"synthetic-{number % job_count:06}/a1", "synthetic-lane", event_payload)
                 for number in range(event_number, event_number + 1024)))
            event_number += 1024
        while size() < target_bytes * (1 - freelist_fraction):
            connection.executemany(
                "INSERT INTO decisions(job_id,attempt_id,evaluated_at,policy_hash,decision_json)"
                " VALUES(?,NULL,?,'synthetic',?)",
                ((f"synthetic-{number % (job_count // 2):06}", TIMESTAMP, decision_payload)
                 for number in range(decision_number, decision_number + 128)))
            decision_number += 128
        while size() < target_bytes:
            connection.executemany(
                "INSERT INTO decisions(job_id,attempt_id,evaluated_at,policy_hash,decision_json)"
                " VALUES(?,NULL,?,'synthetic',?)",
                ((f"synthetic-{job_count // 2 + number % (job_count // 2):06}", TIMESTAMP, decision_payload)
                 for number in range(128)))
        connection.commit()
        connection.execute("DELETE FROM decisions WHERE job_id>='synthetic-000512'")
        connection.execute("DELETE FROM attempts WHERE job_id>='synthetic-000512'")
        connection.execute("DELETE FROM jobs WHERE job_id>='synthetic-000512'")
        connection.commit()
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # Finish fixture I/O before starting the pass timer; journal_mode=OFF was
    # used only to create disposable rows cheaply.
    with path.open("rb") as source:
        os.fsync(source.fileno())
    result = sqlite_metrics(path)
    assert result["auto_vacuum"] == 0
    assert result["freelist_count"] > 0
    assert result["file_bytes"] < 3 * GB
    return result


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


def measure_incremental(root: Path, maximum_steps: int) -> dict:
    from subfleet import store as store_module

    path = root / "state.sqlite3"
    with Store(path, initialize=False) as store:
        with store.transaction("benchmark.synthetic_retention") as transaction:
            transaction.execute("DELETE FROM decisions")
            transaction.execute("DELETE FROM attempts")
            transaction.execute("DELETE FROM jobs")
        before = {key: store.connection.execute(f"PRAGMA {key}").fetchone()[0]
                  for key in ("page_size", "page_count", "freelist_count", "auto_vacuum")}
        durations, freed, lock_holds = [], [], []

        class MeasuringLock:
            """Observe the store mutex, including commit sync, separately from checkpoint I/O."""
            def __init__(self, original):
                self.original = original
                self.started = 0

            def acquire(self, *args, **kwargs):
                acquired = self.original.acquire(*args, **kwargs)
                if acquired:
                    self.started = time.perf_counter()
                return acquired

            def release(self):
                lock_holds.append(time.perf_counter() - self.started)
                self.original.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *_):
                self.release()

            def __getattr__(self, name):
                return getattr(self.original, name)

        original_lock = store._lock
        store._lock = MeasuringLock(original_lock)
        for _ in range(maximum_steps):
            started = time.perf_counter()
            pages = store.incremental_vacuum()
            durations.append(time.perf_counter() - started)
            freed.append(pages)
            if (not pages and store.connection.execute("PRAGMA freelist_count").fetchone()[0]
                    <= store_module.INCREMENTAL_VACUUM_THRESHOLD):
                break
        store._lock = original_lock
        after = {key: store.connection.execute(f"PRAGMA {key}").fetchone()[0]
                 for key in ("page_size", "page_count", "freelist_count", "auto_vacuum")}
        # Production uses only its existing PASSIVE automatic checkpoints. This
        # checkpoint measures physical shrink after readers release the WAL.
        store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {"configured_pages": store_module.INCREMENTAL_VACUUM_PAGES,
            "configured_threshold": store_module.INCREMENTAL_VACUUM_THRESHOLD,
            "before": before, "after": after, "steps": len(durations),
            "successful_steps": sum(bool(pages) for pages in freed),
            "skipped_steps": sum(not pages for pages in freed),
            "pages_returned": sum(freed), "pages_per_step": freed,
            "step_elapsed_s": durations,
            "store_lock_hold_s": lock_holds,
            "store_lock_hold_p50_s": percentile(lock_holds, 0.5),
            "store_lock_hold_p95_s": percentile(lock_holds, 0.95),
            "store_lock_hold_max_s": max(lock_holds),
            "step_p50_s": percentile(durations, 0.5),
            "step_p95_s": percentile(durations, 0.95),
            "step_max_s": max(durations), "file_bytes_after_checkpoint": path.stat().st_size}


def probe(scratch: Path, size_gb: float, free_floor_gb: float, maximum_steps: int,
          freelist_fraction: float = 0.21, progress=None) -> dict:
    target_bytes = round(size_gb * GB)
    free = shutil.disk_usage(scratch).free
    # Fixture construction has no journal. Reserve the full source, its live
    # replacement, and the pass margin, while preserving 55 GB free. The
    # margin also covers batch rounding and filesystem preallocation.
    reserve = target_bytes + math.ceil(target_bytes * (1 - freelist_fraction)) + 512 * MIB
    result = {"requested_size_gb": size_gb, "target_bytes": target_bytes,
              "fixture_fsynced_before_timer": True,
              "fixture_requested_freelist_fraction": freelist_fraction,
              "incremental_steps_requested": maximum_steps,
              "free_before_probe": free, "probe_reserve_bytes": reserve,
              "free_floor_bytes": round(free_floor_gb * GB)}
    if free - reserve < free_floor_gb * GB:
        return dict(result, status="skipped", reason="would cross the configured free-space floor")
    with tempfile.TemporaryDirectory(prefix=f"compact-{size_gb:g}gb-", dir=scratch) as name:
        root = Path(name)
        build_started = time.perf_counter()
        result["before"] = build_store(root, target_bytes, freelist_fraction)
        result["build_elapsed_s"] = time.perf_counter() - build_started
        print(f"built {size_gb:g} GB: {result['before']['page_count']} pages, "
              f"{result['before']['freelist_count']} free", flush=True)
        started = time.perf_counter()
        old_tempdir, old_sqlite_tmpdir = tempfile.tempdir, os.environ["SQLITE_TMPDIR"]
        try:
            tempfile.tempdir = str(root)
            os.environ["SQLITE_TMPDIR"] = str(root)
            with PeakMonitor(root) as monitor:
                report = compact(root, apply=True, fault=lambda stage: print(
                    f"{size_gb:g} GB stage {stage}: {time.perf_counter() - started:.3f}s", flush=True))
        finally:
            tempfile.tempdir = old_tempdir
            os.environ["SQLITE_TMPDIR"] = old_sqlite_tmpdir
        result["compact_elapsed_s"] = time.perf_counter() - started
        result["pass_report"] = dataclasses.asdict(report)
        result["space"] = monitor.report()
        result["after"] = sqlite_metrics(root / "state.sqlite3")
        assert result["after"]["table_rows"] == result["before"]["table_rows"]
        assert result["after"]["auto_vacuum"] == 2
        assert result["after"]["file_bytes"] < result["before"]["file_bytes"]
        assert result["space"]["peak_extra_allocated_bytes"] <= report.needed_bytes
        result["files_after_pass"] = sorted(path.name for path in root.iterdir())
        assert result["files_after_pass"] == ["state.sqlite3"]
        if progress:
            progress(dict(result, status="compacted; incremental measurement in progress"))
        result["incremental"] = measure_incremental(root, maximum_steps)
        result["status"] = "measured"
        print(f"compacted {size_gb:g} GB in {result['compact_elapsed_s']:.3f}s; "
              f"peak extra allocation {result['space']['peak_extra_allocated_bytes']} bytes", flush=True)
    result["synthetic_store_removed"] = not root.exists()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes-gb", type=float, nargs="+", default=[0.5, 1, 2])
    parser.add_argument("--free-floor-gb", type=float, default=55)
    parser.add_argument("--incremental-steps", type=int, default=256)
    parser.add_argument("--freelist-fraction", type=float, default=0.21,
                        help="Fraction of source pages seeded as reclaimable (default 0.21)")
    parser.add_argument("--reuse-experiments", type=Path,
                        help="Reuse already-recorded pragma and page-budget experiments from this JSON")
    parser.add_argument("--pid-file", type=Path,
                        help="Write only this benchmark's PID here; remove it on normal or interrupted exit")
    parser.add_argument("--append-probes", action="store_true",
                        help="Preserve completed probes from --reuse-experiments in the output")
    arguments = parser.parse_args()
    scratch = arguments.scratch_root.resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    if not all(0 < size < 3 for size in arguments.sizes_gb):
        parser.error("each source must be greater than zero and less than 3 GB")
    if not 0 < arguments.freelist_fraction < 0.99:
        parser.error("freelist fraction must be between zero and 0.99")
    if arguments.append_probes and not arguments.reuse_experiments:
        parser.error("--append-probes requires --reuse-experiments")
    # Keep the pass's preflight scratch and SQLite temps within this probe's
    # scratch area as well. No inherited live state-root value is consulted.
    os.environ["TMPDIR"] = str(scratch)
    os.environ["SQLITE_TMPDIR"] = str(scratch)
    tempfile.tempdir = str(scratch)
    if arguments.pid_file:
        arguments.pid_file.write_text(str(os.getpid()) + "\n")
        atexit.register(arguments.pid_file.unlink, missing_ok=True)
    print(f"benchmark PID {os.getpid()}; compact module imported; scratch {scratch}", flush=True)
    getconf = execute_command(["getconf", "DARWIN_USER_TEMP_DIR"])
    darwin_temp = getconf["stdout"].strip()
    commands = [getconf, execute_command(["df", "-k", darwin_temp or str(scratch)])]
    previous = json.loads(arguments.reuse_experiments.read_text()) if arguments.reuse_experiments else {}
    result = {"python": sys.version, "sqlite_version": sqlite3.sqlite_version,
              "invocation": sys.argv, "commands": commands,
              "space_formula": "live_bytes + max(512 MiB, ceil(live_bytes * 0.02)) + WAL bytes",
              "peak_measurement_limitations": "10 ms named-file sampling can miss shorter allocations; filesystem free delta includes unrelated shared-volume activity",
              "fixture_seeding": {"journal_mode": "OFF", "synchronous": "OFF",
                                  "secure_delete": "OFF",
                                  "cache_size_kib": 262144, "event_padding_bytes": 32750,
                                  "requested_freelist_fraction": arguments.freelist_fraction,
                                  "final_journal_mode": "WAL", "final_synchronous": "FULL"},
              "propagation_experiment": previous.get("propagation_experiment") or propagation_experiment(scratch, arguments.free_floor_gb),
              "incremental_page_budget_experiment": previous.get("incremental_page_budget_experiment") or incremental_page_budget_experiment(scratch, arguments.free_floor_gb),
              "probes": previous.get("probes", []) if arguments.append_probes else [],
              "previous_runs": previous.get("previous_runs", []) +
                  ([{"invocation": previous.get("invocation"),
                     "fixture_seeding": previous.get("fixture_seeding")}]
                   if arguments.reuse_experiments else []),
              "incomplete_probes": previous.get("incomplete_probes", []),
              "module_files_sha256": {str(path.relative_to(Path(__file__).resolve().parents[1])):
                  hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in (Path(compact.__code__.co_filename).resolve(),
                               Path(__file__).resolve().parents[1] / "subfleet/store.py")}}
    arguments.output.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        arguments.output.write_text(json.dumps(result, indent=2, default=str) + "\n")

    save()
    for size in arguments.sizes_gb:
        def partial(probe_result):
            result["active_probe"] = probe_result
            save()

        try:
            result["probes"].append(probe(scratch, size, arguments.free_floor_gb,
                                         arguments.incremental_steps, arguments.freelist_fraction, partial))
            result.pop("active_probe", None)
        except Exception as error:
            result["probes"].append({"requested_size_gb": size, "status": "failed",
                                     "error": f"{type(error).__name__}: {error}"})
            save()
            raise
        save()
    measured = [probe for probe in result["probes"] if probe["status"] == "measured"]
    if measured:
        # A 90%-free stress probe copies much less live data than production.
        # Prefer a source whose free fraction resembles the measured live
        # store when extrapolating by source pages.
        live_free_fraction = 507_352 / 2_402_524
        similar = [probe for probe in measured if abs(
            probe["before"]["freelist_count"] / probe["before"]["page_count"] - live_free_fraction) < 0.05]
        largest = max(similar or measured, key=lambda probe: probe["before"]["page_count"])
        result["live_extrapolation"] = {
            "label": "extrapolation, not a live-store measurement",
            "live_page_count": 2_402_524, "live_freelist_count": 507_352,
            "live_page_size": 4096,
            "live_bytes": (2_402_524 - 507_352) * 4096,
            "reclaimable_bytes": 507_352 * 4096,
            "needed_bytes_excluding_unknown_live_WAL": (2_402_524 - 507_352) * 4096 + 512 * MIB,
            "estimated_elapsed_s_linear_in_source_pages": largest["compact_elapsed_s"] * 2_402_524 / largest["before"]["page_count"],
            "basis_requested_size_gb": largest["requested_size_gb"],
            "basis_free_fraction": largest["before"]["freelist_count"] / largest["before"]["page_count"],
            "caveat": "Synthetic payload/index mix, cache state, load and storage latency differ from production; the estimate is not a duration guarantee."}
        save()
    print(json.dumps({"output": str(arguments.output), "statuses": [probe["status"] for probe in result["probes"]]}), flush=True)


if __name__ == "__main__":
    main()
