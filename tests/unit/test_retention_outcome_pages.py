"""C-8.4: bounded read snapshots and a complete, captured retirement history."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from subfleet import retention_fs
from subfleet import retention_notices as rnotices
from subfleet.store import Store


def _ledger(tmp_path: Path, count: int) -> tuple[Path, Store, str]:
    """Several retirement events may share an archive; no surviving job is needed."""
    root = tmp_path / "state"
    archive = root / "archive" / "gone"
    archive.mkdir(parents=True)
    (archive / "rows.json").write_text(json.dumps({
        "job_id": "gone", "rows": {"notices": [{
            "notice_id": 1, "session_id": "session", "state": "pending",
            "created_at": "2026-10-01T00:00:00Z", "text": "gone: succeeded; rc=0",
        }]},
    }))
    data = json.dumps({
        "session_id": "session", "archive": str(archive), "notice_ids": [1],
        "created_at": "2026-11-01T00:00:00Z", "notice_keep_s": 604800,
    })
    writer = Store(root / "state.sqlite3")
    with writer.transaction(None) as conn:
        conn.executemany(
            "INSERT INTO events(ts,kind,job_id,data_json) VALUES(?,?,?,?)",
            [("2026-11-01T00:00:00Z", rnotices.RETIRED_EVENT, "gone", data)] * count,
        )
    writer.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    return root, writer, data


def _is_page(reader: Store, sql: str) -> bool:
    return reader.read_only and "SELECT event_id, ts, job_id, data_json FROM events" in sql


def _lookup_result(root: Path):
    """Capture either result explicitly so fault checks fail through assertions."""
    try:
        return rnotices.archived_outcomes(root, "session"), None
    except Exception as exc:
        return None, exc


def test_lookup_pages_never_exceed_1000_events(tmp_path, monkeypatch):
    root, writer, _ = _ledger(tmp_path, 2503)
    real_query = Store.query
    pages = []

    def observe_page(reader, sql, params=()):
        rows = real_query(reader, sql, params)
        if _is_page(reader, sql):
            pages.append([row["event_id"] for row in rows])
            assert not reader.connection.in_transaction
        return rows

    monkeypatch.setattr(Store, "query", observe_page)
    try:
        report, error = _lookup_result(root)
        assert error is None, error
        assert report["errors"] == []
        assert len(report["outcomes"]) == 2503
        assert [len(page) for page in pages] == [1000, 1000, 503]
        ids = [event_id for page in pages for event_id in page]
        assert ids == sorted(set(ids))
    finally:
        writer.close()


@pytest.mark.timeout(20)
def test_lookup_read_snapshot_is_bounded(tmp_path, monkeypatch):
    """r3's 6,000 rows and requested 1 ms per row retain a five-second ceiling.

    Keep the production deadlines: slow fetching may fail explicitly before a
    whole page finishes. An explicit deadline is preferable to partial history.
    Writers really commit and checkpoint during the active SQLite statement.
    """
    root, writer, _ = _ledger(tmp_path, 6000)
    real_query = Store.query
    snapshots = []
    fetched_per_snapshot = []
    commits = []
    blocked_checkpoints = []

    def slow_query(reader, sql, params=()):
        if not _is_page(reader, sql):
            return real_query(reader, sql, params)
        original_factory = reader.connection.row_factory
        fetched = 0

        def slow_row(cursor, values):
            nonlocal fetched
            fetched += 1
            if fetched % 25 == 0:
                writer.connection.execute(
                    "INSERT INTO events(ts,kind,data_json) VALUES(?,?,'{}')",
                    ("2026-11-01T00:00:00Z", "test.concurrent-writer"),
                )
                commits.append(True)
                checkpoint = writer.connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                blocked_checkpoints.append(checkpoint[1] > checkpoint[2])
            time.sleep(0.001)
            return original_factory(cursor, values)

        reader.connection.row_factory = slow_row
        started = time.monotonic()
        try:
            return real_query(reader, sql, params)
        finally:
            snapshots.append(time.monotonic() - started)
            fetched_per_snapshot.append(fetched)
            reader.connection.row_factory = original_factory

    monkeypatch.setattr(Store, "query", slow_query)
    try:
        report, error = _lookup_result(root)
        print(f"slow-reader snapshots_s={snapshots}; fetched_rows={fetched_per_snapshot}; "
              f"writer_commits={len(commits)}; checkpoint_backlogs={sum(blocked_checkpoints)}")
        assert snapshots and max(snapshots) < 5, snapshots
        assert max(fetched_per_snapshot) <= 1000, fetched_per_snapshot
        assert commits and any(blocked_checkpoints), (commits, blocked_checkpoints)
        if error is None:
            assert report["errors"] == []
            assert len(report["outcomes"]) == 6000
        else:
            assert isinstance(error, rnotices.OutcomesLookupDeadlineError), error
            assert "OUTCOMES_LOOKUP_DEADLINE" in str(error)
        assert tuple(writer.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()) == (0, 0, 0)
    finally:
        writer.close()


def test_writer_checkpoint_completes_between_two_pages(tmp_path, monkeypatch):
    root, writer, _ = _ledger(tmp_path, 1001)
    real_query = Store.query
    pages = []
    held_checkpoint = []
    between_checkpoint = []

    def checkpoint_between_pages(reader, sql, params=()):
        if not _is_page(reader, sql):
            return real_query(reader, sql, params)
        if pages:
            between_checkpoint.append(tuple(writer.connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()))
            return real_query(reader, sql, params)
        original_factory = reader.connection.row_factory
        inserted = False

        def row_with_writer(cursor, values):
            nonlocal inserted
            if not inserted:
                inserted = True
                writer.connection.execute(
                    "INSERT INTO events(ts,kind,data_json) VALUES(?,?,'{}')",
                    ("2026-11-01T00:00:00Z", "test.between-pages"),
                )
                held_checkpoint.append(tuple(writer.connection.execute(
                    "PRAGMA wal_checkpoint(PASSIVE)").fetchone()))
            return original_factory(cursor, values)

        reader.connection.row_factory = row_with_writer
        try:
            rows = real_query(reader, sql, params)
        finally:
            reader.connection.row_factory = original_factory
        pages.append(rows)
        return rows

    monkeypatch.setattr(Store, "query", checkpoint_between_pages)
    try:
        report, error = _lookup_result(root)
        assert error is None, error
        assert len(report["outcomes"]) == 1001
        assert held_checkpoint and held_checkpoint[0][1] > held_checkpoint[0][2]
        assert between_checkpoint == [(0, 0, 0)]
    finally:
        writer.close()


def test_events_arriving_between_pages_stay_past_captured_upper_id(tmp_path, monkeypatch):
    root, writer, data = _ledger(tmp_path, 1001)
    expected_ids = [row[0] for row in writer.connection.execute(
        "SELECT event_id FROM events WHERE kind=? ORDER BY event_id", (rnotices.RETIRED_EVENT,))]
    real_query = Store.query
    seen_ids = []
    inserted = []

    def append_after_first_page(reader, sql, params=()):
        rows = real_query(reader, sql, params)
        if _is_page(reader, sql):
            seen_ids.extend(row["event_id"] for row in rows)
            if not inserted:
                cursor = writer.connection.execute(
                    "INSERT INTO events(ts,kind,job_id,data_json) VALUES(?,?,?,?)",
                    ("2026-11-02T00:00:00Z", rnotices.RETIRED_EVENT, "gone", data),
                )
                inserted.append(cursor.lastrowid)
        return rows

    monkeypatch.setattr(Store, "query", append_after_first_page)
    try:
        report, error = _lookup_result(root)
        assert error is None, error
        assert inserted and inserted[0] > max(expected_ids)
        assert seen_ids == expected_ids
        assert len(report["outcomes"]) == len(expected_ids)
        # A later invocation captures a new upper id and includes the new event.
        again = rnotices.archived_outcomes(root, "session")
        assert len(again["outcomes"]) == len(expected_ids) + 1
    finally:
        writer.close()


@pytest.mark.timeout(8)
@pytest.mark.parametrize("phase", ["upper-id", "page"])
def test_sqlite_progress_deadline_interrupts_each_statement(tmp_path, monkeypatch, phase):
    root, writer, _ = _ledger(tmp_path, 1)
    real_query = Store.query
    calls = []

    def expensive_query(reader, sql, params=()):
        target = "MAX(event_id)" in sql if phase == "upper-id" else _is_page(reader, sql)
        if reader.read_only and target:
            calls.append(True)
            return real_query(reader, "WITH RECURSIVE work(n) AS (VALUES(1) UNION ALL "
                              "SELECT n+1 FROM work WHERE n<100000000) SELECT sum(n) FROM work")
        return real_query(reader, sql, params)

    monkeypatch.setattr(Store, "query", expensive_query)
    monkeypatch.setattr(rnotices, "OUTCOME_STATEMENT_TIMEOUT_S", 0.02)
    try:
        report, error = _lookup_result(root)
        assert calls == [True]
        assert report is None
        assert isinstance(error, rnotices.OutcomesLookupDeadlineError), error
        assert "OUTCOMES_LOOKUP_DEADLINE" in str(error)
        assert isinstance(error.__cause__, sqlite3.OperationalError)
        assert "interrupted" in str(error.__cause__)
        assert tuple(writer.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()) == (0, 0, 0)
    finally:
        writer.close()


def test_overall_deadline_never_returns_pages_read_so_far(tmp_path, monkeypatch):
    root, writer, _ = _ledger(tmp_path, 1001)
    real_query = Store.query
    clock = [0.0]
    pages = []
    second_page_started = []

    def expire_during_second_page(reader, sql, params=()):
        if _is_page(reader, sql) and pages:
            # The first page has returned and all 1,000 events have been
            # accumulated before the next Store.query call begins.
            second_page_started.append(True)
            clock[0] = 1.0
        rows = real_query(reader, sql, params)
        if _is_page(reader, sql):
            pages.append(rows)
        return rows

    monkeypatch.setattr(Store, "query", expire_during_second_page)
    monkeypatch.setattr(rnotices, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rnotices, "OUTCOME_STATEMENT_TIMEOUT_S", 10.0)
    monkeypatch.setattr(rnotices, "OUTCOME_LOOKUP_TIMEOUT_S", 0.5)
    try:
        report, error = _lookup_result(root)
        assert pages and len(pages[0]) == 1000
        assert second_page_started == [True]
        assert report is None, "a deadline must not return a partial history"
        assert isinstance(error, rnotices.OutcomesLookupDeadlineError), error
        assert "OUTCOMES_LOOKUP_DEADLINE" in str(error) and "overall" in str(error)
    finally:
        writer.close()


@pytest.mark.parametrize("phase,elapsed", [("statement", 2.0), ("overall lookup", 31.0)])
def test_operational_error_after_deadline_is_reported_as_deadline(tmp_path, monkeypatch, phase, elapsed):
    """SQLite busy waits and I/O can elapse without invoking the progress hook."""
    root, writer, _ = _ledger(tmp_path, 1)
    real_query = Store.query
    clock = [0.0]

    def busy_query(reader, sql, params=()):
        if _is_page(reader, sql):
            clock[0] = elapsed
            raise sqlite3.OperationalError("database is locked")
        return real_query(reader, sql, params)

    monkeypatch.setattr(Store, "query", busy_query)
    monkeypatch.setattr(rnotices, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rnotices, "OUTCOME_STATEMENT_TIMEOUT_S", 1.0)
    monkeypatch.setattr(rnotices, "OUTCOME_LOOKUP_TIMEOUT_S", 30.0)
    try:
        report, error = _lookup_result(root)
        assert report is None
        assert isinstance(error, rnotices.OutcomesLookupDeadlineError), error
        assert "OUTCOMES_LOOKUP_DEADLINE" in str(error) and phase in str(error)
        assert isinstance(error.__cause__, sqlite3.OperationalError)
        assert str(error.__cause__) == "database is locked"
    finally:
        writer.close()


def test_overall_deadline_covers_archive_reconstruction(tmp_path, monkeypatch):
    root, writer, _ = _ledger(tmp_path, 3)
    real_read = retention_fs.read_regular
    clock = [0.0]
    reads = []

    def expire_during_archive(path):
        result = real_read(path)
        reads.append(path)
        clock[0] = 1.0
        return result

    monkeypatch.setattr(retention_fs, "read_regular", expire_during_archive)
    monkeypatch.setattr(rnotices, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rnotices, "OUTCOME_LOOKUP_TIMEOUT_S", 0.5)
    try:
        report, error = _lookup_result(root)
        assert len(reads) == 1
        assert report is None, "archive reconstruction must not return a partial history"
        assert isinstance(error, rnotices.OutcomesLookupDeadlineError), error
        assert "OUTCOMES_LOOKUP_DEADLINE" in str(error) and "overall" in str(error)
    finally:
        writer.close()
