"""C-3.1, D-QOS-BOOKED1: `jobs.provider_qos` arrives by the store's numbered, additive migration.

The column is nullable and NULL means `utility`: every job submitted before it, and every
job that never asked (`run` without `--qos`, gate rounds, turns, probes' jobs), requested
the clamp (C-5.1). An old store opens, keeps its rows, and its jobs read as `utility`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from subfleet import store as store_module
from subfleet.offline import Offline
from subfleet.store import SCHEMA_VERSION, Store, job_flags, shell_flags

from test_store_migration_2 import _migration_adding, _schema_before_migration


def test_d_qos_booked1_the_column_is_the_newest_migration():
    step = _migration_adding("provider_qos")
    assert step == SCHEMA_VERSION == store_module.PROVIDER_QOS_MIGRATION
    assert store_module.MIGRATIONS[step] == (
        "ALTER TABLE jobs ADD COLUMN provider_qos TEXT "
        "CHECK (provider_qos IS NULL OR provider_qos IN ('utility','default'))",)


def old_store(path: Path) -> Path:
    """A store as the release before the column left it, with one job of each era."""
    with sqlite3.connect(path) as conn:
        _schema_before_migration(conn, _migration_adding("provider_qos"))
        conn.execute("INSERT INTO lanes(lane_id,provider,account_key,credential_ref,credential_kind,created_at,updated_at) "
                     "VALUES ('codex-1','codex','codex:fixture','/fake','home','now','now')")
        for job_id in ("old-dispatch", "old-turn"):
            conn.execute("INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,sandbox,"
                         "caller_session,created_at) VALUES (?,?,?,?,'succeeded','/work','/prompt','read-only',"
                         "'caller','2026-10-27T00:00:00Z')",
                         (job_id, job_id, "digest", "turn" if job_id == "old-turn" else "dispatch"))
    return path


def test_d_qos_booked1_an_old_store_opens_and_its_jobs_read_as_utility(tmp_path):
    path = old_store(tmp_path / "old.sqlite3")
    with sqlite3.connect(path) as conn:
        assert "provider_qos" not in {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    with Store(path) as store:
        assert store.one("SELECT MAX(version) AS v FROM schema_version")["v"] == SCHEMA_VERSION
        # Raw SQL keeps what was stored: nothing.
        assert [row["provider_qos"] for row in store.query("SELECT provider_qos FROM jobs ORDER BY job_id")] == [
            None, None]
        # Domain reads say what NULL means.
        assert store.get_job("old-dispatch")["provider_qos"] == "utility"
        assert store.get_job_by_request("old-turn")["provider_qos"] == "utility"
        assert {job["provider_qos"] for job in store.list_jobs()} == {"utility"}
        migrated = [row["data_json"] for row in store.query("SELECT data_json FROM events WHERE kind='schema.migrated'")]
        assert migrated == [f'{{"version":{SCHEMA_VERSION}}}']
    with Store(path) as store:                       # reopening changes nothing
        assert store.get_job("old-dispatch")["provider_qos"] == "utility"
        assert [row["version"] for row in store.query("SELECT version FROM schema_version ORDER BY version")][-1] == SCHEMA_VERSION


def test_d_qos_booked1_runs_show_offline_reads_utility_on_an_old_store_too(tmp_path):
    """`runs show` with the daemon down reads the store read-only, migrated or not (C-17.5)."""
    path = old_store(tmp_path / "state.sqlite3")
    offline = Offline(tmp_path)
    assert offline.show_job("old-dispatch")["provider_qos"] == "utility"
    with Store(path):
        pass
    assert Offline(tmp_path).show_job("old-dispatch")["provider_qos"] == "utility"


def test_d_qos_booked1_a_job_record_reads_null_and_a_missing_column_as_utility():
    """#277 review r1, P2: `job_flags` makes every job reader's record; `shell_flags`,
    which attempt rows take, adds no job field."""
    assert job_flags({"job_id": "j"}) == {"job_id": "j", "provider_qos": "utility"}
    assert job_flags({"job_id": "j", "provider_qos": None})["provider_qos"] == "utility"
    assert job_flags({"provider_qos": "default", "needs_shell": 1}) == {"provider_qos": "default", "needs_shell": True}
    assert shell_flags({"attempt_id": "j/a1", "has_shell": 0}) == {"attempt_id": "j/a1", "has_shell": False}


@pytest.mark.parametrize("read_only", [False, True], ids=["migrated", "read-only-schema9"])
def test_d_qos_booked1_attempt_rows_gain_no_job_field(tmp_path, read_only):
    """#277 review r1, P2: a read-only Store leaves a schema-9 file unmigrated, so its job
    readers supply `utility` for the missing column; its attempt readers add nothing."""
    path = old_store(tmp_path / "state.sqlite3")
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,reserved_at) "
                     "VALUES ('old-dispatch/a1','old-dispatch',1,'codex-1','gpt-fixture','succeeded',"
                     "'2026-10-27T00:00:00Z')")
    with Store(path, read_only=read_only) as store:
        version = store.one("SELECT MAX(version) AS v FROM schema_version")["v"]
        assert version == (SCHEMA_VERSION - 1 if read_only else SCHEMA_VERSION)
        assert store.get_job("old-dispatch")["provider_qos"] == "utility"
        assert "provider_qos" not in store.get_attempt("old-dispatch/a1")
        assert [row for row in store.list_attempts("old-dispatch") if "provider_qos" in row] == []
    attempts = Offline(tmp_path).show_job("old-dispatch")["attempts"]
    assert len(attempts) == 1 and "provider_qos" not in attempts[0]


@pytest.mark.parametrize("value", ["utility", "default", None])
def test_d_qos_booked1_a_new_job_keeps_its_request(tmp_path, value):
    with Store(tmp_path / "new.sqlite3") as store:
        store.add_job(job_id="j", request_id="j", payload_digest="d", kind="dispatch", workdir="/w",
                      prompt_path="/p", sandbox="read-only", provider_qos=value)
        assert store.one("SELECT provider_qos FROM jobs")["provider_qos"] == value
        assert store.get_job("j")["provider_qos"] == (value or "utility")


@pytest.mark.parametrize("value", ["inherit", "background", "DEFAULT", ""])
def test_d_qos_booked1_the_schema_refuses_any_other_request(tmp_path, value):
    with Store(tmp_path / "new.sqlite3") as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.add_job(job_id="j", request_id="j", payload_digest="d", kind="dispatch", workdir="/w",
                          prompt_path="/p", sandbox="read-only", provider_qos=value)
        assert store.get_job("j") is None
