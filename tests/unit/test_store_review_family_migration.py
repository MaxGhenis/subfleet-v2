"""D-REVIEW-FAMILY1 I6, D-SCHEMA-SLOTS1: migration 13 follows #314's slots 11 and 12."""

from __future__ import annotations

import json
import sqlite3

import pytest

from subfleet.store import (MIGRATIONS, NOTICE_WAKE_MIGRATION, REVIEW_FAMILY_MIGRATION,
                            REVIEW_SUBJECT_MIGRATION, SCHEMA_VERSION, Store, job_flags)

from test_store_review_subject_migration import FIELDS, rows, version_10_store


# Frozen from local #307 object 2b84500ab, independent of the implementation
# under test. These are schema compatibility, without the wake runtime.
PR307_MIGRATION_11 = (
    "ALTER TABLE notices ADD COLUMN wake_state TEXT "
    "CHECK (wake_state IS NULL OR wake_state IN ('claimed','delivered','undeliverable','stale'))",
    "ALTER TABLE notices ADD COLUMN wake_attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE notices ADD COLUMN wake_next_at REAL",
    "ALTER TABLE notices ADD COLUMN wake_at REAL",
    "ALTER TABLE notices ADD COLUMN wake_reason TEXT",
    "CREATE TABLE IF NOT EXISTS caller_wake_sessions ("
    "session_id TEXT PRIMARY KEY, active_at REAL NOT NULL DEFAULT 0, source TEXT, "
    "wake_streak INTEGER NOT NULL DEFAULT 0, last_wake_at REAL)",
)
WAKE_FIELDS = {"wake_state", "wake_attempts", "wake_next_at", "wake_at", "wake_reason"}


def schema(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()


def apply_pr307_migration_11(path):
    # #307's own code also marks unread notices stale; this head never does
    # (D-SCHEMA-SLOTS1: slot 11 is storage only), so the history it left stays.
    with sqlite3.connect(path) as conn:
        for statement in PR307_MIGRATION_11:
            conn.execute(statement)
        conn.execute("UPDATE notices SET wake_state='stale',wake_reason='before-migration' "
                     "WHERE wake_state IS NULL AND state IN ('pending','offered')")
        conn.execute("CREATE INDEX IF NOT EXISTS notices_caller_wake ON notices(created_at, notice_id)\n"
                     "  WHERE job_id IS NOT NULL AND state='pending' AND wake_state IS NULL")
        conn.execute("INSERT INTO schema_version VALUES (11,'independent-migration')")
        conn.execute("INSERT INTO events(ts,kind,data_json) "
                     "VALUES ('independent-migration','schema.migrated','{\"version\":11}')")


def test_i6_schema_10_and_actual_pr307_11_reach_identical_schema_and_keep_existing_rows(tmp_path):
    direct = version_10_store(tmp_path / "direct.sqlite3")
    through_11 = version_10_store(tmp_path / "through-11.sqlite3")
    for path in (direct, through_11):
        with sqlite3.connect(path) as conn:
            for state in ("pending", "offered", "acknowledged", "surfaced"):
                conn.execute("INSERT INTO notices(job_id,session_id,text,state,created_at) "
                             "VALUES ('old-3','kept',?,?, 'before')", (state + " text", state))
    before = rows(direct)
    apply_pr307_migration_11(through_11)
    after_307 = rows(through_11)
    assert (NOTICE_WAKE_MIGRATION, REVIEW_SUBJECT_MIGRATION, REVIEW_FAMILY_MIGRATION, SCHEMA_VERSION) == (11, 12, 13, 13)
    assert MIGRATIONS[11] == PR307_MIGRATION_11
    migrated_rows = []
    for path in (direct, through_11):
        with Store(path) as store:
            assert store.one("SELECT MAX(version) AS version FROM schema_version")["version"] == SCHEMA_VERSION
            for old in before["jobs"]:
                job = store.get_job(old["job_id"])
                assert {field: job[field] for field in FIELDS} == dict.fromkeys(FIELDS)
                assert job["allow_same_family"] is False
        after = rows(path)
        assert after.keys() == before.keys() | {"caller_wake_sessions"}
        assert after["caller_wake_sessions"] == []
        assert after["jobs"] == [{**job, **dict.fromkeys(FIELDS), "allow_same_family": 0}
                                 for job in before["jobs"]]
        # Notice history is kept exactly: the direct path gains empty wake
        # columns, and #307's own stale marks survive the upgrade unchanged.
        reference = before["notices"] if path == direct else after_307["notices"]
        for old, new in zip(reference, after["notices"], strict=True):
            assert {key: new[key] for key in old} == old
            if path == direct:
                assert {key: new[key] for key in WAKE_FIELDS} == {
                    "wake_state": None, "wake_attempts": 0, "wake_next_at": None,
                    "wake_at": None, "wake_reason": None}
        for table in before.keys() - {"jobs", "notices", "schema_version", "events"}:
            assert after[table] == before[table], table
        assert after["events"][:len(before["events"])] == before["events"]
        assert [json.loads(row["data_json"])["version"] for row in after["events"]
                if row["kind"] == "schema.migrated"] == [11, 12, 13]
        with Store(path):
            pass
        assert rows(path) == after
        migrated_rows.append(after)
    assert schema(direct) == schema(through_11)


def test_schema_11_with_pr307_notices_retains_new_pending_wakes_and_activity(tmp_path):
    path = version_10_store(tmp_path / "pr307.sqlite3")
    apply_pr307_migration_11(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO caller_wake_sessions VALUES ('kept',123,'fixture',2,122)")
        conn.execute("INSERT INTO notices(job_id,session_id,text,state,created_at,wake_state,"
                     "wake_attempts,wake_at,wake_reason) "
                     "VALUES ('old-3','kept','kept text','pending','before',NULL,1,122,'kept reason')")
    before = rows(path)
    with Store(path) as store:
        assert store.get_job("old-0")["caller_family"] is None
        assert store.get_job("old-0")["allow_same_family"] is False
        assert store.one("SELECT MAX(version) AS version FROM schema_version")["version"] == SCHEMA_VERSION
        assert store.query("SELECT * FROM caller_wake_sessions") == before["caller_wake_sessions"]
    after = rows(path)
    assert after["notices"] == before["notices"]
    assert {row[1] for row in schema(path)} >= {"caller_wake_sessions", "notices_caller_wake"}


def test_replayed_migration_11_keeps_a_pending_wake_on_an_existing_column(tmp_path):
    path = version_10_store(tmp_path / "partial.sqlite3")
    with sqlite3.connect(path) as conn:
        conn.execute(PR307_MIGRATION_11[0])
        conn.execute("INSERT INTO notices(job_id,text,state,created_at,wake_state) "
                     "VALUES ('old-3','keep pending wake','pending','before',NULL)")
    with Store(path) as store:
        notice = store.one("SELECT * FROM notices")
        assert notice["wake_state"] is None
        assert notice["wake_reason"] is None
        assert notice["wake_attempts"] == 0
        assert store.query("SELECT * FROM caller_wake_sessions") == []


@pytest.mark.parametrize("allow", [False, True])
def test_review_family_authorization_is_boolean_on_every_job_reader(tmp_path, allow):
    with Store(tmp_path / "fresh.sqlite3") as store:
        store.add_job(job_id="j", request_id="r", payload_digest="d", kind="dispatch",
                      workdir="/work", prompt_path="/prompt", sandbox="read-only",
                      allow_same_family=allow)
        for job in (store.get_job("j"), store.get_job_by_request("r"), *store.list_jobs()):
            assert job["allow_same_family"] is allow
        assert store.one("SELECT allow_same_family FROM jobs")["allow_same_family"] == int(allow)


def test_read_only_schema_10_supplies_false_without_mutation_or_attempt_field(tmp_path):
    path = version_10_store(tmp_path / "old.sqlite3")
    before = rows(path)
    with Store(path, read_only=True) as store:
        for job in (store.get_job("old-0"), store.get_job_by_request("old-0"), *store.list_jobs()):
            assert job["allow_same_family"] is False
        assert "allow_same_family" not in store.get_attempt("old-3/a1")
    assert rows(path) == before
    assert job_flags({})["allow_same_family"] is False


@pytest.mark.parametrize("value", [-1, 2, "yes", None])
def test_same_family_authorization_refuses_non_boolean_storage(tmp_path, value):
    with Store(tmp_path / "fresh.sqlite3") as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.add_job(job_id="j", request_id="r", payload_digest="d", kind="dispatch",
                          workdir="/work", prompt_path="/prompt", sandbox="read-only",
                          allow_same_family=value)
