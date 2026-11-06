"""C-8.4: a session can look up an unread retirement after its queue notice expires.

The lookup reads the original archive and retirement ledger, adds no records,
recreates no notice and pins no job. All state roots here are disposable.
"""
from __future__ import annotations

import json
import fcntl
from datetime import UTC, datetime, timedelta

import pytest

from subfleet import cli, retention
from subfleet import retention_notices as rnotices
from subfleet.store import Store

DAY = 86400


def test_outcomes_does_not_create_sqlite_sidecars(retired):
    root, store, headers = retired
    store.close()
    before = {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
              for path in root.rglob("*") if path.is_file()}
    assert not (root / "state.sqlite3-wal").exists()
    assert not (root / "state.sqlite3-shm").exists()
    report = rnotices.archived_outcomes(root, "s-one")
    assert report["errors"] == []
    assert [row["first_lines"] for row in report["outcomes"]] == [[headers["gone-one"]]]
    after = {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
             for path in root.rglob("*") if path.is_file()}
    assert after == before


def test_outcomes_with_a_live_wal_writer_creates_no_files(retired):
    root, store, headers = retired
    assert (root / "state.sqlite3-wal").exists() and (root / "state.sqlite3-shm").exists()
    before = {str(path.relative_to(root)) for path in root.rglob("*")}
    events = store.list_events()
    with (root / "daemon.lock").open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = rnotices.archived_outcomes(root, "s-one")
        assert report["errors"] == []
        assert [row["first_lines"] for row in report["outcomes"]] == [[headers["gone-one"]]]
        assert report["read_only"] is True and report["read_only_scope"] == "records"
        assert report["state_root_files"] == "sqlite-coordination"
    assert {str(path.relative_to(root)) for path in root.rglob("*")} == before | {"daemon.lock"}
    assert store.list_events() == events


def test_outcomes_writer_opens_while_private_snapshot_is_read(retired, monkeypatch):
    root, store, headers = retired
    store.close()
    real_query = Store.query
    writers = []

    def query_with_starting_writer(reader, sql, params=()):
        if reader.path.parent != root and "FROM events WHERE kind=?" in sql:
            writer = Store(root / "state.sqlite3")
            writers.append(writer)
            with writer.transaction("test.writer-started") as tx:
                tx.execute("UPDATE events SET data_json=json_set(data_json,'$.session_id','changed') "
                           "WHERE kind=?", (rnotices.RETIRED_EVENT,))
        return real_query(reader, sql, params)

    monkeypatch.setattr(Store, "query", query_with_starting_writer)
    try:
        report = rnotices.archived_outcomes(root, "s-one")
        assert len(writers) == 1
        assert report["errors"] == [] and report["state_root_files"] == "unchanged"
        assert [row["first_lines"] for row in report["outcomes"]] == [[headers["gone-one"]]]
        assert rnotices.archived_outcomes(root, "s-one")["outcomes"] == []
    finally:
        for writer in writers:
            writer.close()


def test_outcomes_fallback_may_create_only_sqlite_coordination_files(retired, monkeypatch):
    from subfleet import retention_snapshot
    root, store, headers = retired
    real_run = retention_snapshot.subprocess.run
    before = {}

    def close_writer_before_fallback(*args, **kwargs):
        result = real_run(*args, **kwargs)
        assert result.returncode == 1
        store.close()
        assert not (root / "state.sqlite3-wal").exists()
        assert not (root / "state.sqlite3-shm").exists()
        before.update({str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
                       for path in root.rglob("*") if path.is_file()})
        return result

    monkeypatch.setattr(retention_snapshot.subprocess, "run", close_writer_before_fallback)
    report = rnotices.archived_outcomes(root, "s-one")
    assert report["errors"] == []
    assert [row["first_lines"] for row in report["outcomes"]] == [[headers["gone-one"]]]
    assert report["read_only"] is True and report["read_only_scope"] == "records"
    assert report["state_root_files"] == "sqlite-coordination"
    after = {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
             for path in root.rglob("*") if path.is_file()}
    assert after.keys() - before.keys() <= {"state.sqlite3-wal", "state.sqlite3-shm"}
    assert all(after[name] == value for name, value in before.items())
    assert (root / "state.sqlite3-wal").exists() and (root / "state.sqlite3-shm").exists()


def test_outcomes_never_allocates_temporary_files_in_the_state_root(retired, monkeypatch):
    from subfleet import retention_snapshot
    root, store, headers = retired
    store.close()
    monkeypatch.setenv("TMPDIR", str(root))

    def refuse_temporary_directory(*args, **kwargs):
        raise AssertionError("lookup must not allocate scratch inside its state root")

    monkeypatch.setattr(retention_snapshot.tempfile, "TemporaryDirectory", refuse_temporary_directory)
    report = rnotices.archived_outcomes(root, "s-one")
    assert report["errors"] == [] and report["state_root_files"] == "sqlite-coordination"
    assert [row["first_lines"] for row in report["outcomes"]] == [[headers["gone-one"]]]


@pytest.fixture
def retired(tmp_path):
    root = tmp_path / "state"
    (root / "jobs").mkdir(parents=True)
    with Store(root / "state.sqlite3") as store:
        headers = {}
        for job_id, session in (("gone-one", "s-one"), ("gone-two", "s-two")):
            store.add_job(job_id=job_id, request_id=job_id, payload_digest="d", kind="dispatch",
                          workdir=str(root), prompt_path="/prompt", sandbox="read-only", state="succeeded",
                          created_at="2026-10-01T00:00:00Z", finished_at="2026-10-01T00:01:00Z")
            (root / "jobs" / job_id).mkdir()
            headers[job_id] = f"{job_id}: succeeded; rc=0; deliverable=/tmp/{job_id}.md; out=/tmp/out.md"
            notice = store.add_notice(job_id, headers[job_id] + "\nattempt a1: ok", session)
            store.update_notice(notice, created_at=rnotices.stamp(datetime.now(UTC) - timedelta(days=8)))
        result = retention.maintenance(store, root, max_jobs=0, max_bytes=0, notice_keep_s=7 * DAY,
                                       holders=lambda watches, **_: {})
        assert set(result["pruned"]) == set(headers), result
        records = [json.loads(row["data_json"]) for row in store.query(
            "SELECT data_json FROM events WHERE kind=? ORDER BY event_id", (rnotices.RETIRED_EVENT,))]
        expiry = max(rnotices.written_at(record["created_at"]) for record in records) + timedelta(days=8)
        assert len(rnotices.expire_outcomes(store, 7 * DAY, now=expiry)) == 2
        assert store.query("SELECT * FROM service_notices") == []
        yield root, store, headers


def test_archived_outcome_survives_queue_expiry_without_recreating_or_pinning(retired):
    root, store, headers = retired
    before_events = store.list_events()
    before_files = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
                    if path.is_file() and not path.name.startswith("state.sqlite3")}
    for _ in range(2):
        report = rnotices.archived_outcomes(root, "s-one")
        assert report["read_only"] is True and report["session_id"] == "s-one" and report["errors"] == []
        (outcome,) = report["outcomes"]
        assert outcome["job_id"] == "gone-one" and outcome["notice_keep_days"] == 7
        assert outcome["first_lines"] == [headers["gone-one"]]
        lines = outcome["text"].splitlines()
        assert lines[0] == headers["gone-one"]
        assert f"retention retired this job at {outcome['retired_at']} with its notice unread" in lines[1]
        assert "retention.notice_keep_days (7 days)" in lines[1]
        assert "subfleet retention restore gone-one --to DIR" in lines[1]
        assert headers["gone-two"] not in outcome["text"]
    assert store.list_events() == before_events
    assert store.query("SELECT * FROM notices") == store.query("SELECT * FROM service_notices") == []
    assert retention._pin_reasons(store, set(), None, notice_keep_s=0) == {}
    assert before_files == {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
                            if path.is_file() and not path.name.startswith("state.sqlite3")}


def test_outcomes_cli_has_json_and_readable_offline_output(retired, monkeypatch, capsys):
    root, store, headers = retired
    monkeypatch.setenv("SUBFLEET_HOME", str(root))
    assert cli.main(["retention", "outcomes", "--session", "s-two", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["read_only"] and report["errors"] == []
    assert [row["first_lines"] for row in report["outcomes"]] == [[headers["gone-two"]]]
    assert cli.main(["retention", "outcomes", "--session", "s-two"]) == 0
    readable = capsys.readouterr().out
    assert readable.startswith(headers["gone-two"] + "\n")
    assert "retention retired this job" in readable and "(7 days)" in readable
    assert cli.main(["retention", "outcomes", "--session", "no-such-session", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["outcomes"] == []
    assert store.query("SELECT * FROM service_notices") == []


def test_outcomes_lookup_never_creates_a_missing_state_root(tmp_path, monkeypatch, capsys):
    root = tmp_path / "missing"
    monkeypatch.setenv("SUBFLEET_HOME", str(root))
    assert cli.main(["retention", "outcomes", "--session", "s-one"]) == 1
    assert "retention outcomes:" in capsys.readouterr().err
    assert not root.exists()


def test_outcomes_report_an_unavailable_archive_without_recreating_it(retired, monkeypatch, capsys):
    root, store, _ = retired
    rows = root / "archive" / "gone-one" / "rows.json"
    rows.unlink()
    monkeypatch.setenv("SUBFLEET_HOME", str(root))
    assert cli.main(["retention", "outcomes", "--session", "s-one", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["outcomes"] == [] and [item["job_id"] for item in report["errors"]] == ["gone-one"]
    assert not rows.exists() and store.query("SELECT * FROM service_notices") == []


def test_outcomes_report_malformed_archived_notices(retired):
    root, store, _ = retired
    rows = root / "archive" / "gone-one" / "rows.json"
    saved = json.loads(rows.read_text())
    saved["rows"]["notices"] = ["not a notice row"]
    rows.write_text(json.dumps(saved))
    report = rnotices.archived_outcomes(root, "s-one")
    assert report["outcomes"] == []
    assert report["errors"] == [{"job_id": "gone-one", "error": "archived notices are malformed"}]
    assert store.query("SELECT * FROM service_notices") == []


def test_outcomes_resolve_an_archive_under_the_current_state_root(retired, tmp_path):
    root, store, headers = retired
    # Events can retain a former root after a user's move; no old-root file is read.
    with store.transaction("test.old-root") as tx:
        for row in tx.execute("SELECT event_id,data_json FROM events WHERE kind=?", (rnotices.RETIRED_EVENT,)):
            data = json.loads(row["data_json"])
            data["archive"] = str(tmp_path / "old-state" / "archive" / data["archive"].rsplit("/", 1)[1])
            tx.execute("UPDATE events SET data_json=? WHERE event_id=?", (json.dumps(data), row["event_id"]))
    report = rnotices.archived_outcomes(root, "s-one")
    assert report["errors"] == [] and report["outcomes"][0]["first_lines"] == [headers["gone-one"]]
    assert report["outcomes"][0]["archive"] == str(root / "archive" / "gone-one")
    assert not (tmp_path / "old-state").exists()


def test_outcomes_never_read_an_archive_linked_outside_the_state_root(retired, tmp_path, monkeypatch):
    from subfleet import retention_fs
    root, _, _ = retired
    archive = root / "archive" / "gone-one"
    outside = tmp_path / "outside"
    archive.rename(outside)
    archive.symlink_to(outside, target_is_directory=True)

    def refuse(*args, **kwargs):
        raise AssertionError("an outside archive must not be opened")
    monkeypatch.setattr(retention_fs, "read_regular", refuse)
    report = rnotices.archived_outcomes(root, "s-one")
    assert report["outcomes"] == []
    assert report["errors"] == [{"job_id": "gone-one",
                                  "error": "archive is outside the state root's archive folder"}]


def test_outcomes_never_open_a_database_linked_outside_the_state_root(tmp_path, monkeypatch):
    from subfleet import store
    root = tmp_path / "state"
    root.mkdir()
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"must not be opened")
    (root / "state.sqlite3").symlink_to(outside)

    def refuse(*args, **kwargs):
        raise AssertionError("an outside database must not be opened")
    monkeypatch.setattr(store, "Store", refuse)
    with pytest.raises(ValueError, match="state.sqlite3 is outside the state root"):
        rnotices.archived_outcomes(root, "s-one")
