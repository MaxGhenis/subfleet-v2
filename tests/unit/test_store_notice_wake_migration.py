"""C-3.1, C-15.1: quiet caller delivery records survive an additive migration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from subfleet.store import NOTICE_WAKE_MIGRATION, SCHEMA_VERSION, Store

from test_store_migration_2 import _migration_adding, _schema_before_migration


WAKE_COLUMNS = {"wake_state", "wake_attempts", "wake_next_at", "wake_at", "wake_reason"}


def old_store(path: Path) -> Path:
    """The previous release's store, including notices each delivery layer left."""
    with sqlite3.connect(path) as conn:
        _schema_before_migration(conn, NOTICE_WAKE_MIGRATION)
        for index, state in enumerate(("pending", "offered", "surfaced", "acknowledged"), 1):
            job_id = f"job-{index}"
            conn.execute(
                "INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,sandbox,"
                "caller_session,created_at) VALUES (?,?,?,'dispatch','succeeded','/work','/prompt',"
                "'read-only','caller','2026-11-11T12:00:00Z')", (job_id, job_id, "digest"))
            conn.execute(
                "INSERT INTO notices(job_id,session_id,text,state,created_at,offered_at,acknowledged_at) "
                "VALUES (?,'caller',?,?, '2026-11-11T12:05:00Z',?,?)",
                (job_id, f"header for {job_id}", state,
                 "2026-11-11T12:06:00Z" if state != "pending" else None,
                 "2026-11-11T12:07:00Z" if state == "acknowledged" else None))
    return path


def test_d_wake_caller1_notice_delivery_is_a_numbered_additive_migration(tmp_path):
    assert _migration_adding("wake_state") == NOTICE_WAKE_MIGRATION == SCHEMA_VERSION
    path = old_store(tmp_path / "old.sqlite3")
    with sqlite3.connect(path) as conn:
        before = conn.execute("SELECT * FROM notices ORDER BY notice_id").fetchall()
        columns = [row[1] for row in conn.execute("PRAGMA table_info(notices)")]
        assert not WAKE_COLUMNS.intersection(columns)
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='caller_wake_sessions'").fetchone() is None
    with Store(path) as store:
        after = store.query("SELECT * FROM notices ORDER BY notice_id")
        assert [[row[column] for column in columns] for row in after] == [list(row) for row in before]
        assert [row["state"] for row in store.list_notices("caller", pending=True)] == ["pending", "offered"]
        assert all({key: row[key] for key in WAKE_COLUMNS} == {
            "wake_state": None, "wake_attempts": 0, "wake_next_at": None,
            "wake_at": None, "wake_reason": None} for row in after)
        assert store.query("SELECT * FROM caller_wake_sessions") == []
        assert store.query("SELECT version FROM schema_version ORDER BY version") == [
            {"version": NOTICE_WAKE_MIGRATION - 1}, {"version": NOTICE_WAKE_MIGRATION}]


def test_d_wake_caller1_reopening_preserves_retry_delivery_and_activity_records(tmp_path):
    path = old_store(tmp_path / "old.sqlite3")
    with Store(path) as store:
        with store.transaction():
            store.update_notice(1, wake_attempts=3, wake_next_at=2000.0, wake_reason="no-inbox-socket")
            store.update_notice(2, wake_state="delivered", wake_attempts=1, wake_at=1500.0)
            store.connection.execute(
                "INSERT INTO caller_wake_sessions VALUES ('caller',1400.0,'resume',2,1500.0)")
        notices = store.query("SELECT * FROM notices ORDER BY notice_id")
        sessions = store.query("SELECT * FROM caller_wake_sessions")
        versions = store.query("SELECT * FROM schema_version ORDER BY version")
        migrated = store.query("SELECT * FROM events WHERE kind='schema.migrated'")
    with Store(path) as store:
        assert store.query("SELECT * FROM notices ORDER BY notice_id") == notices
        assert store.query("SELECT * FROM caller_wake_sessions") == sessions
        assert store.query("SELECT * FROM schema_version ORDER BY version") == versions
        assert store.query("SELECT * FROM events WHERE kind='schema.migrated'") == migrated
        assert len(migrated) == 1


def test_d_wake_caller1_fresh_and_migrated_stores_have_the_same_delivery_schema(tmp_path):
    old = old_store(tmp_path / "old.sqlite3")
    with Store(old) as migrated, Store(tmp_path / "fresh.sqlite3") as fresh:
        for table in ("notices", "caller_wake_sessions"):
            assert migrated.query(f'PRAGMA table_info("{table}")') == fresh.query(f'PRAGMA table_info("{table}")')
        assert migrated.one("SELECT sql FROM sqlite_master WHERE name='notices_caller_wake'") == fresh.one(
            "SELECT sql FROM sqlite_master WHERE name='notices_caller_wake'")
        assert "job_id IS NOT NULL AND state IN ('pending','offered')" in fresh.one(
            "SELECT sql FROM sqlite_master WHERE name='notices_caller_wake'")["sql"]


@pytest.mark.parametrize("wake_state", [None, "claimed", "delivered", "undeliverable"])
def test_d_wake_caller1_delivery_states_and_session_defaults_are_constrained(tmp_path, wake_state):
    with Store(old_store(tmp_path / "old.sqlite3")) as store:
        store.update_notice(1, wake_state=wake_state)
        assert store.one("SELECT wake_state FROM notices WHERE notice_id=1")["wake_state"] == wake_state
        with pytest.raises(sqlite3.IntegrityError):
            store.update_notice(1, wake_state="retry-whenever")
        with store.transaction():
            store.connection.execute("INSERT INTO caller_wake_sessions(session_id) VALUES ('new-caller')")
        assert store.one("SELECT * FROM caller_wake_sessions WHERE session_id='new-caller'") == {
            "session_id": "new-caller", "active_at": 0.0, "source": None,
            "wake_streak": 0, "last_wake_at": None}
