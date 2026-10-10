"""Serial offline submit/admission benchmark; no provider or daemon is started.

Use a fresh Darwin-user TMPDIR and the repository's development Python.
Only seeded store construction is synthetic; timed submit and _admit are real.
"""
from pathlib import Path
import argparse
import collections
import statistics
import tempfile
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.fake.test_admission_latency import fleet_daemon, submit
from tests.fake.test_canonical_identity import configure
from subfleet import folders
from tests import canonical_identity_privacy


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(values) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def benchmark(live, samples):
    with tempfile.TemporaryDirectory(prefix="identity-cost-") as directory:
        base = Path(directory)
        assert "tmp" not in base.parts
        with fleet_daemon(base / "state") as (service, harness, patch):
            configure(service, patch)
            patch.setattr(service, "_desktop_in_use", lambda: False)
            patch.setattr(service, "_record_desktop_use", lambda: None)
            seed = submit(service, harness, caller_session=None, pinned_lane="codex-1")
            template = service.store.get_job(seed)
            columns = tuple(template)
            manifest = (service.root / "jobs" / seed / "manifest.json").read_bytes()
            with service.store.transaction("fixture.seed") as tx:
                tx.execute("UPDATE jobs SET state='succeeded' WHERE job_id=?", (seed,))
                values = []
                for n in range(4499):
                    row = dict(template, job_id=f"benchmark-{n:04}", request_id=f"benchmark-{n:04}",
                               state="queued" if n < live else "succeeded",
                               out_path=str(base / f"result-{n}.md") if n < live else None)
                    values.append(tuple(row[column] for column in columns))
                    if n < live:
                        jobdir = service.root / "jobs" / row["job_id"]
                        jobdir.mkdir()
                        (jobdir / "manifest.json").write_bytes(manifest)
                tx.executemany(f"INSERT INTO jobs({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", values)
            durations = collections.defaultdict(list)
            counts = collections.defaultdict(list)
            identity, calls = folders.identity, []
            def counted(path):
                calls.append(str(path))
                return identity(path)
            patch.setattr(folders, "identity", counted)
            for n in range(samples):
                calls.clear()
                start = time.perf_counter()
                job = submit(service, harness, caller_session=None, out_path=str(base / f"submit-{n}.md"))
                durations["submit"].append(time.perf_counter() - start)
                counts["submit"].append(len(calls))
                # Keep exactly 4,500 rows, and restore the same queue before each sample.
                with service.store.transaction("fixture.reset") as tx:
                    tx.execute("DELETE FROM jobs WHERE job_id=?", (job,))
                calls.clear()
                start = time.perf_counter()
                service._admit()
                durations["admit"].append(time.perf_counter() - start)
                counts["admit"].append(len(calls))
                assert len(service.store.list_attempts()) == live
                with service.store.transaction("fixture.reset") as tx:
                    tx.execute("DELETE FROM artifacts")
                    tx.execute("DELETE FROM decisions")
                    tx.execute("DELETE FROM attempts")
                    tx.execute("DELETE FROM leases")
                    tx.execute("UPDATE jobs SET state='queued',started_at=NULL WHERE job_id LIKE 'benchmark-%' AND out_path IS NOT NULL")
                service._pending_launches.clear()
                service._busy.clear()
            for operation in ("submit", "admit"):
                data = durations[operation]
                print(f"live={live} {operation} samples={samples} median={statistics.median(data):.6f}s "
                      f"p95={percentile(data, .95):.6f}s identity_calls={counts[operation]}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--live", type=int, nargs="+", default=[50, 300])
    args = parser.parse_args()
    canonical_identity_privacy.pytest_configure(None)
    try:
        for live in args.live:
            benchmark(live, args.samples)
    finally:
        canonical_identity_privacy.pytest_unconfigure(None)
