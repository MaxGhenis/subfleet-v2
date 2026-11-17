"""D-SCHEMA-SLOTS1: actual release/#307/#314/old-#330 stores share slots 11 to 13.

The SQL fixtures were dumped from each revision's unmodified Store after
extracting its tree with git archive. Keeping the dumps also runs these tests
in shallow CI checkouts where the historical git objects are unavailable.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from subfleet.store import MIGRATIONS, SCHEMA_VERSION, Store


FIXTURES = Path(__file__).parents[1] / "fixtures/store/review-upgrade"
REVIEW_FIELDS = {"caller_family", "review_subject", "review_round"}
#: Slot 13 (#330): everything this PR adds beyond #314's slots.
FAMILY_FIELDS = {"allow_same_family"}
WAKE_FIELDS = {"wake_state", "wake_attempts", "wake_next_at", "wake_at", "wake_reason"}


def archived_store(path, revision):
    with sqlite3.connect(path) as conn:
        conn.executescript((FIXTURES / f"{revision}.sql").read_text())
    return path


def snapshot(store):
    return {row["name"]: store.query(f'SELECT * FROM "{row["name"]}" ORDER BY rowid')
            for row in store.query("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.mark.parametrize("revision", ["release-217-v10", "wake-caller-307-v11", "review-subject-old-v11"])
def test_real_historical_store_upgrades_to_current_and_reopens_unchanged(tmp_path, revision):
    path = archived_store(tmp_path / "historic.sqlite3", revision)
    with Store(path, read_only=True) as source:
        old_job = source.one("SELECT * FROM jobs WHERE job_id='historic-review'")
        old_notice = source.one("SELECT * FROM notices WHERE job_id='historic-review'")
    with Store(path) as upgraded:
        assert upgraded.one("SELECT MAX(version) AS version FROM schema_version")["version"] == SCHEMA_VERSION
        job = upgraded.one("SELECT * FROM jobs WHERE job_id='historic-review'")
        notice = upgraded.one("SELECT * FROM notices WHERE job_id='historic-review'")
        assert REVIEW_FIELDS | FAMILY_FIELDS <= job.keys()
        assert WAKE_FIELDS <= notice.keys()
        assert {key: job[key] for key in old_job} == old_job
        assert {key: notice[key] for key in old_notice} == old_notice
        # This PR installs storage only, without #307's historical-notice wake behavior.
        assert notice["wake_state"] is None
        assert notice["wake_attempts"] == 0
        assert upgraded.query("SELECT * FROM caller_wake_sessions") == []
        assert upgraded.one("SELECT sql FROM sqlite_master WHERE name='notices_caller_wake'")
        assert upgraded.one("SELECT sql FROM sqlite_master WHERE name='jobs_review_subject'")
        after = snapshot(upgraded)
    with Store(path) as reopened:
        assert snapshot(reopened) == after


def test_slot_11_migration_and_schema_equal_307(tmp_path):
    reference = json.loads((FIXTURES / "wake-caller-307-ddl.json").read_text())
    assert MIGRATIONS[11] == tuple(reference["migration"])
    with Store(tmp_path / "fresh.sqlite3") as store:
        for name, ddl in reference["schema"].items():
            actual = store.one("SELECT sql FROM sqlite_master WHERE name=?", (name,))["sql"]
            assert " ".join(actual.split()) == " ".join(ddl.split()), name
    # A full local checkout also catches an edit on #307's branch. The frozen
    # comparison above remains authoritative when CI has no remote branch ref.
    result = subprocess.run(["git", "show", "origin/feat/wake-caller-217:subfleet/store.py"],
                            capture_output=True, text=True, check=False)
    if result.returncode == 0:
        migrations = next(node.value for node in ast.parse(result.stdout).body
                          if isinstance(node, ast.AnnAssign)
                          and isinstance(node.target, ast.Name) and node.target.id == "MIGRATIONS")
        upstream = next(value for key, value in zip(migrations.keys, migrations.values)
                        if isinstance(key, ast.Name) and key.id == "NOTICE_WAKE_MIGRATION")
        assert MIGRATIONS[11] == ast.literal_eval(upstream)
        schema = subprocess.run(["git", "show", "origin/feat/wake-caller-217:subfleet/store_schema.sql"],
                                capture_output=True, text=True, check=True).stdout
        with sqlite3.connect(":memory:") as upstream_store:
            upstream_store.executescript(schema)
            for name, ddl in reference["schema"].items():
                actual = upstream_store.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
                assert " ".join(actual.split()) == " ".join(ddl.split()), name


@pytest.mark.parametrize("revision", ["release-217-v10", "wake-caller-307-v11", "review-subject-old-v11"])
def test_failed_schema_script_rolls_back_upgrade_and_next_open_retries(tmp_path, monkeypatch, revision):
    path = archived_store(tmp_path / "historic.sqlite3", revision)
    with Store(path, read_only=True) as source:
        before = snapshot(source)
        before_schema = source.query("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")
    read_text = Path.read_text
    calls = []

    def fail_once(schema_path, *args, **kwargs):
        script = read_text(schema_path, *args, **kwargs)
        if schema_path.name == "store_schema.sql" and not calls:
            calls.append(True)
            return script + "\nCREATE TABLE schema_failure_marker (value TEXT);\nSELECT missing_schema_column FROM jobs;"
        return script

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", fail_once)
        with pytest.raises(sqlite3.OperationalError, match="missing_schema_column"):
            Store(path)
    assert calls == [True]
    with Store(path, read_only=True) as failed:
        assert snapshot(failed) == before
        assert failed.query("SELECT type,name,sql FROM sqlite_master ORDER BY type,name") == before_schema
    with Store(path) as recovered:
        assert recovered.one("SELECT MAX(version) AS version FROM schema_version")["version"] == SCHEMA_VERSION
        assert REVIEW_FIELDS | FAMILY_FIELDS <= recovered.get_job("historic-review").keys()
        assert WAKE_FIELDS <= recovered.one("SELECT * FROM notices").keys()
        after = snapshot(recovered)
    with Store(path) as reopened:
        assert snapshot(reopened) == after


@pytest.mark.parametrize("recorded", [12, 13])
@pytest.mark.parametrize("revision", ["review-subject-old-v11", "wake-caller-307-v11"])
def test_recorded_version_repairs_missing_slot_columns(tmp_path, revision, recorded):
    path = archived_store(tmp_path / "historic.sqlite3", revision)
    # Old #330 recorded 12 before its schema script failed on an old #314
    # store. A version row alone must not hide any slot's missing columns.
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO schema_version VALUES (?,'2026-11-02T00:00:00Z')", (recorded,))
    with Store(path, read_only=True) as source:
        before = snapshot(source)
    with Store(path) as repaired:
        assert REVIEW_FIELDS | FAMILY_FIELDS <= repaired.get_job("historic-review").keys()
        notice = repaired.one("SELECT * FROM notices WHERE job_id='historic-review'")
        assert WAKE_FIELDS <= notice.keys()
        assert notice["wake_state"] is None and notice["wake_attempts"] == 0
        assert repaired.query("SELECT * FROM caller_wake_sessions") == []
        assert repaired.one("SELECT sql FROM sqlite_master WHERE name='notices_caller_wake'")
        assert repaired.one("SELECT sql FROM sqlite_master WHERE name='jobs_review_subject'")
        after = snapshot(repaired)
        for table, original_rows in before.items():
            assert [{key: row[key] for key in original} for row, original in zip(after[table], original_rows)] == original_rows
            added = after[table][len(original_rows):]
            if recorded == SCHEMA_VERSION or table not in ("schema_version", "events"):
                # Repairing a current-version store adds no version or audit record.
                assert added == [], table
            elif table == "schema_version":
                assert [row["version"] for row in added] == [SCHEMA_VERSION]
            else:
                assert [(row["kind"], json.loads(row["data_json"])) for row in added] == [
                    ("schema.migrated", {"version": SCHEMA_VERSION}), ("schema.applied", {"version": SCHEMA_VERSION})]
    with Store(path) as reopened:
        assert snapshot(reopened) == after
