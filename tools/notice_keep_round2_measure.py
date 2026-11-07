"""Offline repeatable D-NOTICE-KEEP1 round-2 cost probes.

Run with the synced development interpreter, TMPDIR under Darwin's user temp
directory, and --output pointing into the report's evidence directory. Fixture
creation is excluded from timings. No live state or daemon is consulted.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import tarfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from subfleet import retention_notices as notices
from subfleet.store import Store, notice_rows

NOW = datetime(2026, 11, 12, 6, 0, 0, 500000, tzinfo=UTC)
OLD = notices.stamp(NOW - timedelta(days=8))


def seed(store: Store, count: int, *, mixed: bool) -> None:
    with store.transaction() as tx:
        tx.executemany("INSERT INTO service_notices(notice_id,session_id,text,state,created_at) "
                       "VALUES(?,?,?,'pending',?)",
                       ((number, f"s{number % 68}", "outcome" if not mixed else
                         ("ordinary ping " if number % 10 < 8 else "outcome " if number % 10 == 8 else "pin ")
                         + str(number) + "x" * 2048, OLD) for number in range(1, count + 1)))
        rows = tx.execute("SELECT * FROM service_notices").fetchall()
        events = []
        for row in rows:
            if mixed and row["notice_id"] % 10 < 8:
                continue
            data = {"service_notice_id": row["notice_id"], "session_id": row["session_id"], "created_at": OLD}
            kind = "job.pin_noticed" if mixed and row["notice_id"] % 10 == 9 else notices.RETIRED_EVENT
            if kind == notices.RETIRED_EVENT:
                data["outcome_sha256"] = hashlib.sha256(row["text"].encode()).hexdigest()
            events.append((OLD, kind, f"j{row['notice_id']}", json.dumps(data)))
        tx.executemany("INSERT INTO events(ts,kind,job_id,data_json) VALUES(?,?,?,?)", events)


def association_samples(scratch: Path, samples: int) -> dict:
    listing, expiry = [], []
    for sample in range(samples):
        for name, count, mixed, receipts in (("listing", 2000, True, listing), ("expiry", 10000, False, expiry)):
            with Store(scratch / f"{name}-{sample}.sqlite3") as store:
                seed(store, count, mixed=mixed)
                query, transaction = store.query, store.transaction
                reads, writes = [], []

                def measured_query(sql, params=()):
                    reads.append(sql)
                    return query(sql, params)

                @contextmanager
                def measured_transaction(*args, **kwargs):
                    started = time.perf_counter()
                    with transaction(*args, **kwargs) as tx:
                        yield tx
                    writes.append(time.perf_counter() - started)

                store.query, store.transaction = measured_query, measured_transaction
                started = time.perf_counter()
                result = notice_rows(store.query) if mixed else notices.expire_outcomes(store, 7 * 86400, now=NOW)
                elapsed = time.perf_counter() - started
                assert len(result) == count
                receipts.append({"rows": count, "total_s": elapsed, "write_s": sum(writes),
                                 "write_transactions": len(writes), "read_queries": len(reads)})
    return {"listing": {"fixture": "2000 unresolved service notices: 1600 pings, 200 pin notices, 200 retirement outcomes; 2 KiB text",
                        "samples": listing, "median_s": statistics.median(row["total_s"] for row in listing)},
            "expiry": {"fixture": "10000 due retirement outcomes; text='outcome'; 68 sessions",
                       "samples": expiry, "median_s": statistics.median(row["total_s"] for row in expiry),
                       "median_write_s": statistics.median(row["write_s"] for row in expiry)}}


def lookup_sample(scratch: Path, size_mib: int) -> dict:
    state = scratch / "lookup-state"
    state.mkdir()
    database = state / "state.sqlite3"
    archive = state / "archive" / "lookup-probe"
    archive.mkdir(parents=True)
    first_line = "lookup-job: succeeded; rc=0; deliverable=-; out=/synthetic/output"
    session = "benchmark-session"
    outcome = notices.outcome_text([first_line], [OLD], OLD, 7 * 86400, str(archive), archive.name)
    (archive / "rows.json").write_text(json.dumps({"job_id": "lookup-job", "rows": {"notices": [
        {"notice_id": 1, "session_id": session, "text": first_line + "\noriginal notice body",
         "state": "pending", "created_at": OLD}]}}))
    with Store(database) as store:
        with store.transaction() as tx:
            tx.execute("CREATE TABLE synthetic_padding(payload BLOB)")
            for _ in range(size_mib):
                tx.execute("INSERT INTO synthetic_padding VALUES(zeroblob(1048576))")
            tx.execute("INSERT INTO events(ts,kind,job_id,data_json) VALUES(?,?,?,?)",
                       (OLD, notices.RETIRED_EVENT, "lookup-job", json.dumps({"service_notice_id": 1,
                        "session_id": session, "created_at": OLD, "notice_ids": [1], "notice_keep_s": 7 * 86400,
                        "outcome_sha256": hashlib.sha256(outcome.encode()).hexdigest(), "archive": str(archive)})))
    assert not database.with_name("state.sqlite3-wal").exists()
    size = database.stat().st_size
    real_query = Store.query
    reading = threading.Event()
    writer_done = threading.Event()
    writer_result = {}

    def query_while_writing(reader, sql, params=()):
        if reader.read_only and "FROM events WHERE kind=?" in sql:
            # Establish a read snapshot before the second Store begins opening.
            reader.connection.execute("BEGIN")
            try:
                rows = real_query(reader, sql, params)
                assert reader.connection.in_transaction
                reading.set()
                assert writer_done.wait(20), "writer failed to commit during lookup read"
                writer_result["reader_transaction_open_at_commit"] = reader.connection.in_transaction
                assert real_query(reader, sql, params) == rows
                return rows
            finally:
                reader.connection.rollback()
        return real_query(reader, sql, params)

    def writer():
        assert reading.wait(20), "lookup did not reach read"
        started = time.perf_counter()
        try:
            with Store(database) as store:
                with store.transaction("round2.probe-writer") as tx:
                    tx.execute("INSERT INTO synthetic_padding VALUES(?)", (b"writer committed",))
            writer_result.update(committed=True, seconds=time.perf_counter() - started)
        except Exception as exc:
            writer_result.update(committed=False, seconds=time.perf_counter() - started, error=repr(exc))
        finally:
            writer_done.set()

    # Time the actual lookup alone, then hold its event-read boundary for the
    # concurrent writer probe so writer scheduling is reported separately.
    started = time.perf_counter()
    report = notices.archived_outcomes(state, session)
    lookup_s = time.perf_counter() - started
    assert report["errors"] == []
    assert [row["first_lines"] for row in report["outcomes"]] == [[first_line]]
    Store.query = query_while_writing
    thread = threading.Thread(target=writer)
    thread.start()
    started = time.perf_counter()
    try:
        report = notices.archived_outcomes(state, session)
        assert [row["first_lines"] for row in report["outcomes"]] == [[first_line]]
    finally:
        Store.query = real_query
        thread.join(25)
    assert not thread.is_alive()
    return {"database_bytes": size, "fixture": "synthetic padding plus one retirement event and valid archived unread notice",
            "outcomes_returned": len(report["outcomes"]), "lookup_s": lookup_s,
            "lookup_with_writer_s": time.perf_counter() - started, "writer": writer_result,
            "review_1GB_lookup_s": 124.19,
            "writer_waited_for_lookup_to_close": not writer_result.get("committed", False)}


def interleaved_samples(scratch: Path, reference: str, samples: int) -> dict:
    """Alternate original-reference/current subprocesses to expose host noise."""
    original = scratch / "original-reference"
    original.mkdir()
    archive = subprocess.run(["git", "archive", reference, "subfleet"], cwd=ROOT, check=True, capture_output=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as saved:
        saved.extractall(original, filter="data")
    (original / "tools").mkdir()
    script = Path(__file__).resolve()
    (original / "tools" / script.name).write_bytes(script.read_bytes())
    runs = []
    for pair in range(samples):
        order = ("before", "after") if pair % 2 == 0 else ("after", "before")
        for phase in order:
            executable = (original if phase == "before" else ROOT) / "tools" / script.name
            receipt = scratch / f"{phase}-{pair}.json"
            command = [sys.executable, str(executable), "--phase", f"interleaved-{phase}-{pair}",
                       "--samples", "1", "--output", str(receipt)]
            subprocess.run(command, cwd=executable.parents[1], check=True, capture_output=True, text=True)
            runs.append({"pair": pair, "phase": phase, "receipt": json.loads(receipt.read_text()), "command": command})
    summary = {}
    for phase in ("before", "after"):
        receipts = [run["receipt"]["association"] for run in runs if run["phase"] == phase]
        summary[phase] = {"listing_median_s": statistics.median(row["listing"]["median_s"] for row in receipts),
                          "expiry_median_s": statistics.median(row["expiry"]["median_s"] for row in receipts),
                          "expiry_median_write_s": statistics.median(row["expiry"]["median_write_s"] for row in receipts)}
    return {"reference": reference, "order": "before/after on even pairs; after/before on odd pairs",
            "samples_per_phase": samples, "runs": runs, "summary": summary}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--lookup-mib", type=int, default=0)
    parser.add_argument("--lookup-only", action="store_true", help="skip association measurements")
    parser.add_argument("--compare-ref", help="alternate fresh processes for this original git reference and current code")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="notice-keep-r2-measure-", dir=os.environ["TMPDIR"]) as temporary:
        scratch = Path(temporary)
        result = {"phase": args.phase, "measured_at": datetime.now(UTC).isoformat(), "python": sys.version,
                  "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                    for name in ("subfleet/store.py", "subfleet/retention_notices.py")},
                  "samples": args.samples}
        if args.compare_ref:
            result["interleaved"] = interleaved_samples(scratch, args.compare_ref, args.samples)
        else:
            if not args.lookup_only:
                result["association"] = association_samples(scratch, args.samples)
            if args.lookup_mib:
                result["lookup"] = lookup_sample(scratch, args.lookup_mib)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
