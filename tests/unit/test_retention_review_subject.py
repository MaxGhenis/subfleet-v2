"""D-REVIEW-SUBJECT1 I7: archive rows retain metadata and schema-10 archives restore."""

from __future__ import annotations

import json
import sqlite3

import pytest

from subfleet import retention, retention_archive
from subfleet import store as store_module
from subfleet import protocol, review_subject
from subfleet.store import Store
from tests.unit.retention_world import World, snapshot, trust_temporary_directories


FIELDS = ("caller_family", "review_subject", "review_round")


def retire(world):
    return retention.maintenance(world.store, world.root, max_jobs=0, max_bytes=0,
                                 holders=lambda watches, **_: {})


def test_i7_retired_job_rows_keep_caller_family_subject_and_round(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    world = World(tmp_path)
    try:
        world.job("review", worktree=False)
        world.store.update_job("review", task="review", caller_family="codex",
                               review_subject="https://github.com/owner/repo/pull/217", review_round=3)
        before = world.store.one("SELECT * FROM jobs WHERE job_id='review'")
        job_before = snapshot(world.root / "jobs" / "review")
        assert retire(world)["pruned"] == ["review"]
        archive = world.root / "archive" / "review"
        saved = json.loads((archive / "rows.json").read_text())["rows"]["jobs"]
        assert saved == [before]
        assert {field: saved[0][field] for field in FIELDS} == {
            "caller_family": "codex", "review_subject": "https://github.com/owner/repo/pull/217",
            "review_round": 3}
        assert world.store.get_job("review") is None
        assert retention_archive.check_archive(world.root, "review")["ok"]
        report = retention_archive.restore(world.root, "review")
        assert set(report["restored"]) == {"job"}
        assert snapshot(world.root / "jobs" / "review") == job_before
    finally:
        world.close()


@pytest.mark.parametrize("task,state,accepted,subject,round_number,expected", [
    ("review", "succeeded", True, "review-key", 4, {"review_subject": "review-key", "review_round": 4}),
    ("review", "failed", True, "review-key", 4, {}),
    ("review", "cancelled", True, "review-key", 4, {}),
    ("review", "succeeded", False, "review-key", 4, {}),
    ("review", "succeeded", True, None, None, {}),
    ("review", "succeeded", True, "", 4, {}),
    ("review", "succeeded", True, "review-key", 1.5, {}),
    ("code", "succeeded", True, "review-key", 4, {}),
])
def test_retirement_audit_keeps_only_accepted_review_rounds(tmp_path, monkeypatch,
                                                          task, state, accepted, subject, round_number, expected):
    trust_temporary_directories(monkeypatch)
    world = World(tmp_path)
    try:
        world.job("review-history", worktree=False, state=state)
        attempt = world.attempt("review-history") if accepted else None
        world.store.update_job("review-history", task=task, accepted_attempt_id=attempt,
                               review_subject=subject, review_round=round_number)
        assert retire(world)["pruned"] == ["review-history"]
        event, = [json.loads(event["data_json"]) for event in world.store.list_events("review-history")
                  if event["kind"] == "retention.pruned"]
        assert {key: event[key] for key in ("review_subject", "review_round") if key in event} == expected
        with world.store.transaction(None) as tx:
            if expected:
                with pytest.raises(protocol.ProtocolError) as error:
                    review_subject.assign_round(tx, "review-key")
                assert error.value.reason == "review-round-limit"
            else:
                assert review_subject.assign_round(tx, "review-key") == 1
    finally:
        world.close()


def test_i7_restore_reads_an_archive_written_from_schema_10(tmp_path, monkeypatch):
    """Generate an actual archive while the store has no review metadata columns.

    Restore recovers files and commits, as before; archived database rows remain
    audit data in rows.json rather than being reinserted into the active store.
    """
    trust_temporary_directories(monkeypatch)
    world = World(tmp_path)
    try:
        world.job("old-review", worktree=False)
        before = snapshot(world.root / "jobs" / "old-review")
        world.close()
        with sqlite3.connect(world.root / "state.sqlite3") as conn:
            conn.execute("DROP INDEX jobs_review_subject")
            for field in FIELDS:
                conn.execute(f"ALTER TABLE jobs DROP COLUMN {field}")
            conn.execute("UPDATE schema_version SET version=10")
        with monkeypatch.context() as old_build:
            old_build.setattr(store_module, "SCHEMA_VERSION", 10)
            world.store = Store(world.root / "state.sqlite3", initialize=False)
            assert retire(world)["pruned"] == ["old-review"]
            world.close()
        archive = world.root / "archive" / "old-review"
        archive_bytes = (archive / "rows.json").read_bytes()
        saved = json.loads(archive_bytes)["rows"]["jobs"][0]
        assert set(FIELDS).isdisjoint(saved)
        world.store = Store(world.root / "state.sqlite3")
        assert retention_archive.check_archive(world.root, "old-review")["ok"]
        report = retention_archive.restore(world.root, "old-review")
        assert set(report["restored"]) == {"job"}
        assert snapshot(world.root / "jobs" / "old-review") == before
        assert (archive / "rows.json").read_bytes() == archive_bytes
    finally:
        world.close()
