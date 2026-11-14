"""Repeatable, offline D-NOTICE-KEEP1 round-3 event lookup measurements.

Run with the synced interpreter and Darwin's user temporary directory in
TMPDIR. Fixtures use the current Store schema; their construction is excluded
from lookup timings. The requested session has no matching retirement events,
so these measurements isolate ledger traversal and exclude archive reads.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from subfleet import retention_notices as notices
from subfleet.store import Store


def seed_fixture(scratch: Path, count: int) -> tuple[Path, dict[str, Any]]:
    state = scratch / f"events-{count}"
    state.mkdir()
    database = state / "state.sqlite3"
    stamp = "2026-11-05T00:00:00Z"
    payload = json.dumps({"service_notice_id": 1, "session_id": "another-session",
                          "created_at": stamp, "outcome_sha256": "0" * 64,
                          "notice_ids": [1], "notice_keep_s": 604800, "archive": None},
                         sort_keys=True, separators=(",", ":"))
    seed_started = time.perf_counter()
    with Store(database) as store:
        with store.transaction() as tx:
            tx.executemany("INSERT INTO events(ts,kind,job_id,data_json) VALUES(?,?,?,?)",
                           ((stamp, notices.RETIRED_EVENT, f"retired-{number}", payload)
                            for number in range(count)))
    seed_s = time.perf_counter() - seed_started
    return state, {"retirement_events": count, "fixture_seed_s": seed_s,
                   "database_bytes": database.stat().st_size}


def lookup_sample(state: Path, count: int, sample_number: int) -> dict[str, Any]:
    database = state / "state.sqlite3"
    real_query = Store.query
    queries: list[dict[str, Any]] = []
    statements: dict[str, tuple[str, Any]] = {}

    def measured_query(reader: Store, sql: str, params=()):
        started = time.perf_counter()
        rows = None
        failure = None
        try:
            rows = real_query(reader, sql, params)
            return rows
        except Exception as exc:
            failure = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            elapsed = time.perf_counter() - started
            if reader.read_only and "FROM events" in sql:
                category = "upper_bound" if "upper_event_id" in sql else "page"
                queries.append({"category": category, "rows": len(rows) if rows is not None else None,
                                "seconds": elapsed, "completed": rows is not None, "error": failure})
                statements.setdefault(category, (sql, params))

    Store.query = measured_query
    result = None
    failure = None
    started = time.perf_counter()
    try:
        result = notices.archived_outcomes(state, "no-matching-session")
    except notices.OutcomesLookupError as exc:
        message = str(exc)
        failure = {"type": type(exc).__name__, "message": message,
                   "scope": "overall lookup" if "overall lookup exceeded" in message else
                   "statement" if "statement exceeded" in message else "other lookup failure",
                   "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else None,
                   "cause_message": str(exc.__cause__) if exc.__cause__ else None}
    finally:
        total_s = time.perf_counter() - started
        Store.query = real_query
    if result is not None:
        assert result["outcomes"] == [], result
        assert result["errors"] == [], result

    plans = {}
    with Store(database, read_only=True) as store:
        for category, (sql, params) in statements.items():
            plans[category] = store.query("EXPLAIN QUERY PLAN " + sql, params)
    pages = [query for query in queries if query["category"] == "page"]
    upper = [query for query in queries if query["category"] == "upper_bound"]
    completed_pages = [query for query in pages if query["completed"]]
    rows_read = sum(query["rows"] for query in completed_pages)
    if result is not None:
        assert len(upper) == 1, queries
        assert rows_read == count, queries
    assert all(query["rows"] <= notices.OUTCOME_PAGE_SIZE for query in completed_pages), queries
    return {"retirement_events": count, "sample": sample_number,
            "status": "succeeded" if result is not None else "failed",
            "lookup_elapsed_s": total_s, "complete_lookup_s": total_s if result is not None else None,
            "result_returned": result is not None, "error": failure,
            "query_materialization_s": sum(query["seconds"] for query in queries),
            "maximum_statement_s": max((query["seconds"] for query in queries), default=0),
            "maximum_page_s": max((query["seconds"] for query in pages), default=0),
            "upper_bound_s": sum(query["seconds"] for query in upper),
            "rows_materialized_in_completed_queries": rows_read,
            "pages_attempted": len(pages), "pages_completed": len(completed_pages),
            "upper_bound_statements": len(upper), "query_errors": [
                query for query in queries if query["error"] is not None],
            "outcomes": len(result["outcomes"]) if result is not None else None,
            "query_plans": plans}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", type=int, nargs="+", default=[6000, 100000, 1000000, 3000000])
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if any(count <= 0 for count in args.counts):
        parser.error("--counts must be positive")
    if len(set(args.counts)) != len(args.counts):
        parser.error("--counts must be distinct")
    if args.samples <= 0:
        parser.error("--samples must be positive")
    scratch = Path(tempfile.mkdtemp(prefix="sf-nk-f3-lookup-measure-", dir=os.environ["TMPDIR"]))
    report = {"measured_at": datetime.now(UTC).isoformat(),
              "fixture": "Retirement events for another session; current schema; construction excluded; reused between samples",
              "timing_scope": "Store.query includes SQLite execution, fetchall and Python dictionary conversion; failed queries are timed too",
              "failure_semantics": "A named deadline failure returns no history; materialized query rows are diagnostic counts only",
              "page_size": notices.OUTCOME_PAGE_SIZE,
              "statement_deadline_s": notices.OUTCOME_STATEMENT_TIMEOUT_S,
              "lookup_deadline_s": notices.OUTCOME_LOOKUP_TIMEOUT_S,
              "samples_per_fixture": args.samples, "fixtures": [], "samples": []}

    def save_report() -> None:
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    try:
        save_report()
        for count in args.counts:
            state, fixture = seed_fixture(scratch, count)
            report["fixtures"].append(fixture)
            save_report()
            for sample_number in range(1, args.samples + 1):
                sample = lookup_sample(state, count, sample_number)
                report["samples"].append(sample)
                save_report()
                print(json.dumps(sample, sort_keys=True), flush=True)
        report["finished_at"] = datetime.now(UTC).isoformat()
        save_report()
    finally:
        assert "sf-nk-f3" in str(scratch), scratch
        shutil.rmtree(scratch)


if __name__ == "__main__":
    main()
