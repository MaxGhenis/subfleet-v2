"""D-REMOTE-JOBS1: remote adoption recognizes provider URLs and adds its own table."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from subfleet import store as store_module
from subfleet.store import NOTICE_WAKE_MIGRATION, Store

from test_store_migration_2 import _schema_before_migration


@pytest.fixture
def store_before_remote_jobs(tmp_path):
    path = tmp_path / "state.sqlite3"
    with closing(sqlite3.connect(path)) as conn, conn:
        _schema_before_migration(conn, NOTICE_WAKE_MIGRATION + 1)
        conn.executemany(
            "INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,sandbox,created_at) "
            "VALUES (?,?,?,'dispatch',?,'/work','/prompt','read-only','2026-11-28T01:00:00Z')",
            [("old-running", "request-running", "digest-a", "running"),
             ("old-succeeded", "request-succeeded", "digest-b", "succeeded")],
        )
    return path


def test_remote_jobs_migration_preserves_existing_jobs(store_before_remote_jobs):
    path = store_before_remote_jobs
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        before = [dict(row) for row in conn.execute("SELECT * FROM jobs ORDER BY job_id")]
        assert conn.execute("SELECT max(version) FROM schema_version").fetchone()[0] == NOTICE_WAKE_MIGRATION
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='remote_jobs'").fetchone() is None

    with Store(path) as store:
        assert store.query("PRAGMA table_info(remote_jobs)")
        step = store_module.REMOTE_JOBS_MIGRATION
        assert step in store_module.MIGRATIONS
        assert store.one("SELECT max(version) AS version FROM schema_version") == {"version": step}
        assert store.query("SELECT * FROM jobs ORDER BY job_id") == before
        assert store.query("SELECT * FROM remote_jobs") == []
        migrated = store.query("SELECT data_json FROM events WHERE kind='schema.migrated' ORDER BY event_id")
        assert [json.loads(row["data_json"]) for row in migrated] == [
            {"version": version} for version in range(NOTICE_WAKE_MIGRATION + 1, step + 1)
        ]


def test_reopening_a_store_after_remote_jobs_migration_is_a_no_op(store_before_remote_jobs):
    path = store_before_remote_jobs
    queries = (
        "SELECT * FROM schema_version ORDER BY version",
        "SELECT * FROM events ORDER BY event_id",
        "SELECT * FROM jobs ORDER BY job_id",
        "SELECT * FROM remote_jobs",
    )
    with Store(path) as store:
        after_first_open = [store.query(query) for query in queries]
    with Store(path) as store:
        assert [store.query(query) for query in queries] == after_first_open


def test_fresh_store_has_remote_jobs_migration_schema(tmp_path, store_before_remote_jobs):
    with Store(tmp_path / "fresh.sqlite3") as fresh, Store(store_before_remote_jobs) as migrated:
        columns = fresh.query("PRAGMA table_info(remote_jobs)")
        assert columns
        assert columns == migrated.query("PRAGMA table_info(remote_jobs)")
        assert fresh.query("SELECT version FROM schema_version") == [{"version": store_module.REMOTE_JOBS_MIGRATION}]


@pytest.mark.parametrize(
    "url,provider,provider_ref,canonical_url",
    [
        pytest.param("https://claude.ai/code/session_Aa012", "claude-cloud", "session_Aa012",
                     "https://claude.ai/code/session_Aa012", id="claude"),
        pytest.param("https://chatgpt.com/codex/tasks/task_Aa012_b", "codex-cloud", "task_Aa012_b",
                     "https://chatgpt.com/codex/tasks/task_Aa012_b", id="codex"),
        pytest.param("https://claude.ai/code/session_Aa012?label=a@example.test#extra", "claude-cloud",
                     "session_Aa012", "https://claude.ai/code/session_Aa012", id="strip-query-and-fragment"),
    ],
)
def test_parse_remote_url_recognizes_provider_and_canonicalizes(url, provider, provider_ref, canonical_url):
    from subfleet.remote_jobs import parse_remote_url

    assert parse_remote_url(url) == (provider, provider_ref, canonical_url)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://example.org/code/session_Aa012", id="other-host"),
        pytest.param("https://claude.ai.example.org/code/session_Aa012", id="lookalike-host"),
        pytest.param("http://claude.ai/code/session_Aa012", id="http"),
        pytest.param("https://claude.ai/code/", id="missing-id"),
        pytest.param("https://claude.ai/code/session_", id="empty-claude-suffix"),
        pytest.param("https://chatgpt.com/codex/tasks/task_", id="empty-codex-suffix"),
        pytest.param("https://claude.ai/code/session_Aa_012", id="invalid-claude-id"),
        pytest.param("https://chatgpt.com/codex/tasks/task_Aa-012", id="invalid-codex-id"),
        pytest.param("https://claude.ai/code/task_Aa012", id="wrong-provider-id"),
        pytest.param("https://claude.ai/code/session_Aa012/another", id="extra-path"),
        pytest.param("https://claude.ai/code/session_Aa012\n", id="path-whitespace"),
        pytest.param("https://claude.ai:443/code/session_Aa012", id="explicit-port"),
        pytest.param("https://user@claude.ai/code/session_Aa012", id="url-userinfo"),
    ],
)
def test_parse_remote_url_refuses_other_hosts_and_shapes(url):
    from subfleet.remote_jobs import parse_remote_url

    with pytest.raises(ValueError):
        parse_remote_url(url)
