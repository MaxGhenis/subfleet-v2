"""C-8.4: ordinary read-only SQLite needs usable WAL coordination files.

These cases use real directory permissions and SQLite connections. No mocked
permission error substitutes for the closed-store compatibility boundary.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing

import pytest

from subfleet import retention_notices as rnotices
from subfleet.store import Store


@pytest.fixture
def closed_outcome(tmp_path):
    root = tmp_path / "state"
    archive = root / "archive" / "retired-job"
    archive.mkdir(parents=True)
    written = "2026-11-01T00:00:00Z"
    header = "retired-job: succeeded; rc=0; deliverable=result.md"
    (archive / "rows.json").write_text(json.dumps({
        "job_id": "retired-job",
        "rows": {"notices": [{
            "notice_id": 1, "session_id": "returning-session", "state": "pending",
            "created_at": written, "text": header + "\nattempt a1: ok",
        }]},
    }))
    with Store(root / "state.sqlite3") as store:
        store.connection.execute(
            "INSERT INTO events(ts,kind,job_id,data_json) VALUES(?,?,?,?)",
            (written, rnotices.RETIRED_EVENT, "retired-job", json.dumps({
                "session_id": "returning-session", "notice_ids": [1],
                "created_at": written, "notice_keep_s": 7 * 86400,
                "archive": str(archive),
            })),
        )
    assert not (root / "state.sqlite3-wal").exists()
    assert not (root / "state.sqlite3-shm").exists()
    yield root, header


def _require_enforced_permissions():
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory mode permissions")


def _assert_outcome(root, header):
    report = rnotices.archived_outcomes(root, "returning-session")
    assert report["errors"] == []
    assert [item["job_id"] for item in report["outcomes"]] == ["retired-job"]
    assert [item["first_lines"] for item in report["outcomes"]] == [[header]]
    assert report["read_only"] is True and report["read_only_scope"] == "records"


def test_outcomes_readonly_folder_without_sidecars_fails_with_actionable_named_error(closed_outcome):
    _require_enforced_permissions()
    root, _ = closed_outcome
    original_mode = stat.S_IMODE(root.stat().st_mode)
    main_bytes = (root / "state.sqlite3").read_bytes()
    root.chmod(0o555)
    try:
        # Prove that this test actually runs across the filesystem boundary.
        with pytest.raises(PermissionError):
            (root / "permission-probe").write_bytes(b"not writable")
        with pytest.raises(rnotices.OutcomesLookupError) as caught:
            rnotices.archived_outcomes(root, "returning-session")
        message = str(caught.value)
        assert "OUTCOMES_LOOKUP_READONLY_DIRECTORY" in message
        assert str(root) in message
        assert "writ" in message.lower() and "director" in message.lower()
        assert "-wal" in message and "-shm" in message
        assert not (root / "state.sqlite3-wal").exists()
        assert not (root / "state.sqlite3-shm").exists()
        assert (root / "state.sqlite3").read_bytes() == main_bytes
    finally:
        root.chmod(original_mode)


def test_outcomes_readonly_folder_with_usable_sidecars_succeeds(closed_outcome):
    _require_enforced_permissions()
    root, header = closed_outcome
    database = root / "state.sqlite3"
    main_bytes = database.read_bytes()
    # An ordinary read-only connection creates usable coordination files while
    # the folder is writable. Close it before testing the fully closed store.
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True,
                                 isolation_level=None)) as keeper:
        keeper.execute("SELECT COUNT(*) FROM events").fetchone()
    assert (root / "state.sqlite3-wal").exists()
    assert (root / "state.sqlite3-shm").exists()
    original_mode = stat.S_IMODE(root.stat().st_mode)
    root.chmod(0o555)
    try:
        with pytest.raises(PermissionError):
            (root / "permission-probe").write_bytes(b"not writable")
        _assert_outcome(root, header)
        assert database.read_bytes() == main_bytes
        assert not (root / "daemon.lock").exists()
    finally:
        root.chmod(original_mode)


def test_outcomes_writable_folder_without_sidecars_succeeds_and_creates_them(closed_outcome):
    root, header = closed_outcome
    database = root / "state.sqlite3"
    main_bytes = database.read_bytes()
    before_files = {path.name for path in root.iterdir()}
    _assert_outcome(root, header)
    assert (root / "state.sqlite3-wal").exists()
    assert (root / "state.sqlite3-shm").exists()
    assert {path.name for path in root.iterdir()} - before_files == {
        "state.sqlite3-wal", "state.sqlite3-shm",
    }
    assert database.read_bytes() == main_bytes
    assert not (root / "daemon.lock").exists()
