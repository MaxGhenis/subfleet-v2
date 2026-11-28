"""C-3.7, D-ST2: full-ledger reads sort by index; operator snapshots search live rows.

On 2026-10-06 the 2.1.10 daemon wrote about 30 MB/s to the disk (2 TB in 2 d 8 h,
measured by proc_pid_rusage). Each capacity snapshot sorted every attempt (39 MB
with its evidence) and every job in SQLite temp files (`etilqs_*`), because
neither `ORDER BY` had an index and the page cache is 2 MB. On a copy of the live
tables, five snapshots wrote 220 MB in 5.0 s without the indexes and 0 MB in
1.1 s with them.
"""
import pytest

from subfleet import daemon
from subfleet.store import Store


def plan(store, sql, params=()):
    return [row["detail"] for row in store.query("EXPLAIN QUERY PLAN " + sql, params)]


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "state.sqlite3") as opened:
        yield opened


def test_list_attempts_reads_through_the_reserved_index(store, monkeypatch):
    issued = []
    query = store.query
    monkeypatch.setattr(store, "query", lambda sql, params=(): issued.append(sql) or query(sql, params))
    store.list_attempts()
    [sql] = issued
    steps = plan(store, sql)
    assert not any("TEMP B-TREE" in step for step in steps), steps
    assert any("attempts_reserved" in step for step in steps), steps


def test_the_operator_snapshot_searches_live_jobs_and_attempts_by_index(store):
    steps = " / ".join(plan(store, daemon.STATUS_JOBS))
    assert "jobs_state" in steps and "sqlite_autoindex_jobs_1" in steps, steps
    assert "attempts_live" in steps and "attempts_quarantined" in steps, steps
    assert "SCAN jobs" not in steps and "SCAN attempts" not in steps, steps


def test_reopening_an_existing_store_adds_the_quarantine_index(store):
    path = store.path
    store.connection.execute("DROP INDEX attempts_quarantined")
    store.close()
    with Store(path) as reopened:
        steps = " / ".join(plan(reopened, daemon.STATUS_ATTEMPTS))
        assert "attempts_live" in steps and "attempts_quarantined" in steps, steps
        assert "SCAN attempts" not in steps, steps


def test_the_route_reads_search_by_index_and_never_scan_a_table(store):
    """A route read (every admission pass) finds its few rows by index and may sort
    them in memory; `jobs_created` must not lure `ROUTE_JOBS` into ordering by a scan
    of every job (2.3 ms against 0.05 ms on the live store, 2026-10-06)."""
    attempts = plan(store, daemon.ROUTE_ATTEMPTS)
    assert attempts[0].startswith("SEARCH attempts USING INDEX attempts_live"), attempts
    jobs = " / ".join(plan(store, daemon.ROUTE_JOBS))
    assert "jobs_parent" in jobs and "attempts_live" in jobs, jobs
    assert "SCAN jobs" not in jobs and "jobs_created" not in jobs, jobs
