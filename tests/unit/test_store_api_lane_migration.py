"""C-31.1, C-31.4, C-3.1: schema 11 lets `lanes.provider` hold `claude-api` and gives
each lane a `credit_usd`.

SQLite cannot widen a CHECK in place, so the step rebuilds `lanes` from the schema
file's definition. These tests build a real schema-10 store (the narrow CHECK, no
`credit_usd`), with rows in every table that refers to `lanes`, and open it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from subfleet import store as store_module
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.store import API_LANE_MIGRATION, SCHEMA_VERSION, Store

SCHEMA = Path(store_module.__file__).with_name("store_schema.sql")
NARROW = "CHECK (provider IN ('codex','claude'))"
WIDE = "CHECK (provider IN ('codex','claude','claude-api'))"


def schema_10() -> str:
    """The schema file as release 10 left `lanes`: the narrow CHECK and no budget column."""
    text = SCHEMA.read_text()
    assert WIDE in text
    text = text.replace(WIDE, NARROW, 1)
    start = text.index("  updated_at TEXT NOT NULL,\n  -- C-31.4")
    end = text.index("credit_usd REAL CHECK (credit_usd IS NULL OR credit_usd > 0)\n", start)
    end = end + len("credit_usd REAL CHECK (credit_usd IS NULL OR credit_usd > 0)\n")
    return text[:start] + "  updated_at TEXT NOT NULL\n" + text[end:]


def old_store(path: Path) -> Path:
    with sqlite3.connect(path) as conn:
        conn.executescript(schema_10())
        conn.execute("INSERT INTO schema_version VALUES (?,?)", (API_LANE_MIGRATION - 1, "2026-11-01T00:00:00Z"))
        conn.execute("INSERT INTO lanes(lane_id,provider,account_key,credential_ref,credential_kind,created_at,"
                     "updated_at,identity,label,identity_status,plan) VALUES "
                     "('claude-1','claude','claude:a@example.org','claude-quota-a@example.org','keychain-token',"
                     "'t0','t0','acct:org','a@example.org','verified','max')")
        conn.execute("INSERT INTO lanes(lane_id,provider,account_key,credential_ref,credential_kind,created_at,"
                     "updated_at) VALUES ('codex-1','codex','codex:x','/home/x','home','t0','t0')")
        conn.execute("INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,sandbox,"
                     "created_at) VALUES ('j','j','d','dispatch','succeeded','/w','/p','read-only','t0')")
        conn.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,reserved_at) "
                     "VALUES ('j/a1','j',1,'claude-1','claude-opus-5-5','succeeded','t0')")
        conn.execute("INSERT INTO readings(lane_id,scope,window,utilization,resets_at,label,source,observed_at) "
                     "VALUES ('codex-1','account','five_hour',0.5,NULL,'provider','wham','t0')")
        conn.execute("INSERT INTO closures(lane_id,scope,until_at,reason,clock_source,source_event,created_at) "
                     "VALUES ('claude-1','account','2099-01-01T00:00:00Z','operator-hold','reported','operator','t0')")
    return path


def lanes_sql(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='lanes'").fetchone()[0]


def test_c31_1_the_budget_column_is_the_newest_migration():
    assert SCHEMA_VERSION == API_LANE_MIGRATION == 11
    assert store_module.MIGRATIONS[API_LANE_MIGRATION] == (
        "ALTER TABLE lanes ADD COLUMN credit_usd REAL CHECK (credit_usd IS NULL OR credit_usd > 0)",)
    assert WIDE in SCHEMA.read_text() and NARROW not in SCHEMA.read_text()


def test_c31_1_a_schema_10_store_migrates_and_keeps_every_row(tmp_path):
    path = old_store(tmp_path / "state.sqlite3")
    assert NARROW in lanes_sql(path)
    with Store(path) as store:
        assert store.one("SELECT MAX(version) AS v FROM schema_version")["v"] == SCHEMA_VERSION
        rows = {row["lane_id"]: row for row in store.lane_rows()}
        # C-3.1: every column keeps its place, and the budget arrives last.
        names = [row[1] for row in store.connection.execute('PRAGMA table_info("lanes")')]
        assert names[-4:] == ["identity", "label", "identity_status", "credit_usd"] or names[-1] == "credit_usd"
        assert set(rows) == {"claude-1", "codex-1"}
        assert rows["claude-1"]["identity"] == "acct:org" and rows["claude-1"]["plan"] == "max"
        assert rows["claude-1"]["identity_status"] == "verified" and rows["claude-1"]["credit_usd"] is None
        assert store.get_attempt("j/a1")["lane_id"] == "claude-1"
        assert [row["lane_id"] for row in store.query("SELECT lane_id FROM readings")] == ["codex-1"]
        assert [row["lane_id"] for row in store.query("SELECT lane_id FROM closures")] == ["claude-1"]
        assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        indexes = {row["name"] for row in store.query("SELECT name FROM sqlite_master WHERE type='index' "
                                                       "AND tbl_name='lanes'")}
        assert {"lanes_account", "lanes_identity"} <= indexes
        # The widened CHECK admits the credit lane, and only it besides the two.
        store.put_lane(Lane("claude-api-1", "claude-api", "claude-api:a@example.org",
                            Credential("claude-api", "claude-api-a@example.org", "keychain-token"),
                            None, LaneOwner.V2, False, True, "org-1", "a@example.org"),
                       plan="api-credit", identity_status="verified", credit_usd=200)
        assert store.one("SELECT credit_usd FROM lanes WHERE lane_id='claude-api-1'")["credit_usd"] == 200.0
        with pytest.raises(sqlite3.IntegrityError):
            store.put_lane(Lane("gemini-1", "gemini", "gemini:x", Credential("gemini", "x", "env"),
                                None, LaneOwner.V2, False, True))
        # Foreign keys still hold after the rebuild.
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute("INSERT INTO readings(lane_id,scope,window,utilization,resets_at,label,source,"
                               "observed_at) VALUES ('no-such-lane','account','five_hour',0.1,NULL,'provider',"
                               "'wham','t1')")
        migrated = [row["data_json"] for row in store.query("SELECT data_json FROM events WHERE kind='schema.migrated'")]
        assert migrated == [f'{{"version":{API_LANE_MIGRATION}}}']
    assert WIDE in lanes_sql(path)
    with Store(path) as store:                       # reopening changes nothing
        assert len(store.lane_rows()) == 3
        assert [row["version"] for row in store.query("SELECT version FROM schema_version")][-1] == SCHEMA_VERSION


def test_c31_4_the_schema_refuses_a_nonpositive_budget(tmp_path):
    with Store(tmp_path / "new.sqlite3") as store:
        for value in (0, -1):
            with pytest.raises(sqlite3.IntegrityError):
                store.put_lane(Lane(f"claude-api-{abs(value)}", "claude-api", "claude-api:a@example.org",
                                    Credential("claude-api", "claude-api-a@example.org", "keychain-token"),
                                    None, LaneOwner.V2, False, True), credit_usd=value)


def test_c31_1_a_read_only_handle_on_a_schema_10_store_reads_lanes(tmp_path):
    """A read-only Store never migrates (C-3.1); lane reads need no `credit_usd`."""
    path = old_store(tmp_path / "state.sqlite3")
    with Store(path, read_only=True) as store:
        lanes = store.list_lanes()
        assert {lane.lane_id for lane in lanes} == {"claude-1", "codex-1"}
        assert "credit_usd" not in store.lane_rows()[0]
    assert NARROW in lanes_sql(path)


def test_c31_1_a_failed_reference_check_leaves_the_store_untouched(tmp_path, monkeypatch):
    """A dangling reference the rebuild would commit rolls the whole migration back."""
    path = old_store(tmp_path / "state.sqlite3")
    with sqlite3.connect(path) as conn:              # a dangling row, as foreign keys off could leave
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("INSERT INTO readings(lane_id,scope,window,utilization,resets_at,label,source,observed_at) "
                     "VALUES ('gone-1','account','five_hour',0.5,NULL,'provider','wham','t0')")
    with pytest.raises(store_module.SchemaVersionError, match="dangling lane reference"):
        Store(path)
    assert NARROW in lanes_sql(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == API_LANE_MIGRATION - 1
