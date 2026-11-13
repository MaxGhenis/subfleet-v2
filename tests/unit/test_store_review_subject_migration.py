"""D-REVIEW-SUBJECT1 I6: schema-10 rows survive the additive review metadata migration."""

from __future__ import annotations

import json
import sqlite3

import pytest

from subfleet.store import REVIEW_SUBJECT_MIGRATION, SCHEMA_VERSION, Store

from test_store_migration_2 import _migration_adding, _schema_before_migration


FIELDS = {"caller_family", "review_subject", "review_round"}


def version_10_store(path):
    with sqlite3.connect(path) as conn:
        _schema_before_migration(conn, _migration_adding("caller_family"))
        conn.execute("INSERT INTO lanes(lane_id,provider,account_key,credential_ref,credential_kind,created_at,updated_at) "
                     "VALUES ('codex-1','codex','codex:fixture','/fake','home','before','before')")
        for index, state in enumerate(("queued", "running", "waiting", "succeeded", "failed", "cancelled", "lost")):
            identity = f"old-{index}"
            conn.execute("INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,sandbox,"
                         "caller_session,caller_pid,name,created_at) VALUES (?,?,?,'dispatch',?,'/work','/prompt',"
                         "'read-only','old-session',123,'old name','before')",
                         (identity, identity, "digest", state))
        conn.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,reserved_at) "
                     "VALUES ('old-3/a1','old-3',1,'codex-1','fixture','succeeded','before')")
        conn.execute("UPDATE jobs SET accepted_attempt_id='old-3/a1' WHERE job_id='old-3'")
        conn.execute("INSERT INTO events(ts,kind,job_id,data_json) "
                     "VALUES ('before','fixture.before','old-3','{\"kept\":true}')")
    return path


def rows(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return {table[0]: [dict(row) for row in conn.execute(f'SELECT * FROM "{table[0]}" ORDER BY rowid')]
                for table in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}


def test_review_subject_metadata_is_the_next_nullable_migration():
    assert REVIEW_SUBJECT_MIGRATION == SCHEMA_VERSION == 11
    assert {_migration_adding(field) for field in FIELDS} == {REVIEW_SUBJECT_MIGRATION}


def test_i6_migration_preserves_every_existing_row_and_adds_only_null_job_fields(tmp_path):
    path = version_10_store(tmp_path / "old.sqlite3")
    before = rows(path)
    assert FIELDS.isdisjoint(before["jobs"][0])
    with Store(path) as store:
        assert store.one("SELECT MAX(version) AS v FROM schema_version")["v"] == SCHEMA_VERSION
        indexed = store.query("PRAGMA index_info(jobs_review_subject)")
        assert [column["name"] for column in indexed] == ["review_subject"]
    after = rows(path)
    assert before.keys() == after.keys()
    assert after["jobs"] == [{**job, **dict.fromkeys(FIELDS)} for job in before["jobs"]]
    for table in before.keys() - {"jobs", "schema_version", "events"}:
        assert after[table] == before[table], table
    assert after["schema_version"][:-1] == before["schema_version"]
    assert after["schema_version"][-1]["version"] == REVIEW_SUBJECT_MIGRATION
    assert after["events"][:len(before["events"])] == before["events"]
    assert [(event["kind"], json.loads(event["data_json"])) for event in after["events"][len(before["events"]):]] == [
        ("schema.migrated", {"version": REVIEW_SUBJECT_MIGRATION}),
        ("schema.applied", {"version": SCHEMA_VERSION}),
    ]
    with Store(path):
        pass
    assert rows(path) == after


def test_read_only_schema_10_jobs_supply_null_review_metadata_without_migration(tmp_path):
    path = version_10_store(tmp_path / "old.sqlite3")
    before = rows(path)
    with Store(path, read_only=True) as store:
        for job in (store.get_job("old-0"), store.get_job_by_request("old-0"), *store.list_jobs()):
            assert {field: job[field] for field in FIELDS} == dict.fromkeys(FIELDS)
        assert FIELDS.isdisjoint(store.get_attempt("old-3/a1"))
    assert rows(path) == before


def test_fresh_and_migrated_stores_have_the_same_review_columns_and_index(tmp_path):
    old = version_10_store(tmp_path / "old.sqlite3")
    with Store(old) as migrated, Store(tmp_path / "fresh.sqlite3") as fresh:
        columns = lambda store: {row["name"]: (row["type"], row["notnull"], row["dflt_value"])
                                 for row in store.query("PRAGMA table_info(jobs)")}
        assert columns(migrated) == columns(fresh)
        assert {field: columns(fresh)[field] for field in FIELDS} == {
            "caller_family": ("TEXT", 0, None), "review_subject": ("TEXT", 0, None),
            "review_round": ("INTEGER", 0, None)}
        sql = "SELECT sql FROM sqlite_master WHERE type='index' AND name='jobs_review_subject'"
        assert migrated.one(sql) == fresh.one(sql)


@pytest.mark.parametrize("family,round_number", [(None, None), ("claude", 1), ("codex", 4)])
def test_review_metadata_values_survive_all_job_readers(tmp_path, family, round_number):
    with Store(tmp_path / "new.sqlite3") as store:
        store.add_job(job_id="j", request_id="r", payload_digest="d", kind="dispatch", task="review",
                      workdir="/work", prompt_path="/prompt", sandbox="read-only", caller_family=family,
                      review_subject="chosen subject", review_round=round_number)
        for job in (store.get_job("j"), store.get_job_by_request("r"), *store.list_jobs()):
            assert (job["caller_family"], job["review_subject"], job["review_round"]) == (
                family, "chosen subject", round_number)


@pytest.mark.parametrize("field,value", [("caller_family", "gemini"), ("caller_family", "CODEX"),
                                        ("review_round", 0), ("review_round", 5)])
def test_review_metadata_schema_refuses_invalid_values(tmp_path, field, value):
    with Store(tmp_path / "new.sqlite3") as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.add_job(job_id="j", request_id="r", payload_digest="d", kind="dispatch", workdir="/work",
                          prompt_path="/prompt", sandbox="read-only", **{field: value})
        assert store.get_job("j") is None
