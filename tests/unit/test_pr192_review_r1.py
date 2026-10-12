"""PR #192 review probes; every input is a disposable store."""

import contextlib
import errno
import gzip
import json
import sqlite3

import pytest

from subfleet import prune_decisions as pruning
from subfleet.store import Store


def seed(root):
    with Store(root / "state.sqlite3") as store:
        store.add_job(job_id="terminal", request_id="terminal", payload_digest="digest",
                      kind="dispatch", workdir=str(root), prompt_path="/synthetic/prompt",
                      sandbox="read-only", state="succeeded")
        for i in range(4):
            store.add_decision("terminal", {"policy_hash": "hash", "note": str(i)})


def snapshot(root):
    conn = sqlite3.connect(root / "state.sqlite3")
    try:
        return {name: conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
                for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


@pytest.mark.parametrize("fault", ["count", "digest", "gzip", "full-volume"])
def test_bad_row_backup_prevents_any_delete(tmp_path, monkeypatch, fault):
    seed(tmp_path)
    before = snapshot(tmp_path)
    original = pruning._read_rows

    def damaged(path):
        if fault == "full-volume":
            raise OSError(errno.ENOSPC, "review: backup volume full")
        if fault == "gzip":
            path.write_bytes(b"not a gzip backup")
            return original(path)
        count, digest = original(path)
        return (count - 1, digest) if fault == "count" else (count, "0" * 64)

    monkeypatch.setattr(pruning, "_read_rows", damaged)
    with pytest.raises(pruning.PruneError) as stopped:
        pruning.prune(tmp_path, apply=True, confirm=True)
    assert stopped.value.report.deleted == stopped.value.report.batches == 0
    assert snapshot(tmp_path) == before


def test_bad_whole_backup_prevents_any_delete(tmp_path, monkeypatch):
    seed(tmp_path)
    before = snapshot(tmp_path)
    connect = pruning.sqlite3.connect

    class DamagedBackup:
        def __init__(self, real):
            self.real = real

        def execute(self, sql):
            assert sql == "PRAGMA integrity_check"
            return type("Cursor", (), {"fetchall": lambda self: [("review: corrupt backup",)]})()

        def close(self):
            self.real.close()

    def damaged_connect(database, *args, **kwargs):
        conn = connect(database, *args, **kwargs)
        return DamagedBackup(conn) if "/backups/state-" in str(database) else conn

    monkeypatch.setattr(pruning.sqlite3, "connect", damaged_connect)
    with pytest.raises(pruning.PruneVerificationError, match="backup copy.*fails integrity_check") as stopped:
        pruning.prune(tmp_path, apply=True, confirm=True)
    assert stopped.value.report.deleted == stopped.value.report.batches == 0
    assert snapshot(tmp_path) == before


def test_all_backups_verified_before_first_delete(tmp_path, monkeypatch):
    seed(tmp_path)
    begin = Store.transaction
    verified = []
    original_copy = pruning._copy_store
    original_rows = pruning._backup_rows

    def copy(*args):
        record = original_copy(*args)
        assert record["integrity_check"] == "ok"
        verified.append("copy")
        return record

    def rows(*args):
        record = original_rows(*args)
        assert record["verified"] is True
        with gzip.open(record["path"], "rt") as stream:
            assert len([json.loads(line) for line in stream]) == 3
        verified.append("rows")
        return record

    def transaction(self, kind, **kwargs):
        if kind == pruning.PRUNE_KIND:
            assert verified == ["copy", "rows"]
        return begin(self, kind, **kwargs)

    monkeypatch.setattr(pruning, "_copy_store", copy)
    monkeypatch.setattr(pruning, "_backup_rows", rows)
    monkeypatch.setattr(Store, "transaction", transaction)
    assert pruning.prune(tmp_path, apply=True, confirm=True).deleted == 3


def test_well_formed_whole_backup_with_changed_reader_bytes_is_refused(tmp_path, monkeypatch):
    seed(tmp_path)
    before = snapshot(tmp_path)
    connect = pruning.sqlite3.connect
    changed = []

    def damaged_connect(database, *args, **kwargs):
        if "/backups/state-" in str(database):
            from pathlib import Path
            from urllib.parse import unquote, urlsplit

            path = Path(unquote(urlsplit(str(database)).path))
            data = path.read_bytes()
            assert data.count(b'"note":"3"') == 1
            path.write_bytes(data.replace(b'"note":"3"', b'"note":"X"'))
            changed.append(path)
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(pruning.sqlite3, "connect", damaged_connect)
    with pytest.raises(pruning.PruneError):
        report = pruning.prune(tmp_path, apply=True, confirm=True)
        conn = connect(changed[0])
        try:
            print("changed-backup:", report.deleted, report.backups["copy"],
                  conn.execute("SELECT decision_json FROM decisions ORDER BY decision_id DESC LIMIT 1").fetchone(),
                  "proof-ok:", report.verification["ok"])
        finally:
            conn.close()
    assert changed
    assert snapshot(tmp_path) == before


def test_schema_7_missing_table_is_refused_without_prebackup_repair(tmp_path):
    seed(tmp_path)
    conn = sqlite3.connect(tmp_path / "state.sqlite3")
    try:
        conn.execute("DROP TABLE service_notices")
        conn.commit()
    finally:
        conn.close()
    before = snapshot(tmp_path)
    with pytest.raises(pruning.PruneError):
        report = pruning.prune(tmp_path, apply=True, confirm=True)
        print("schema-repair:", "service_notices" in before,
              "service_notices" in snapshot(tmp_path), "deleted:", report.deleted,
              "tables-identical:", report.verification["tables_identical"])
    assert snapshot(tmp_path) == before


def test_after_vacuum_changed_prune_audit_payload_is_rejected(tmp_path, monkeypatch):
    seed(tmp_path)
    verify = pruning._reverify

    def change_audit(conn, report, **kwargs):
        conn.execute("UPDATE events SET data_json=? WHERE event_id>?",
                     ('{"deleted":0}', kwargs["events_max"]))
        return verify(conn, report, **kwargs)

    monkeypatch.setattr(pruning, "_reverify", change_audit)
    with pytest.raises(pruning.PruneVerificationError):
        report = pruning.prune(tmp_path, apply=True, confirm=True, vacuum=True)
        print("changed-audit:", [row for row in snapshot(tmp_path)["events"]
                                if row[2] == pruning.PRUNE_KIND],
              "after-vacuum-ok:", report.verification["after_vacuum"]["ok"])


def test_whole_backup_matches_every_planning_fingerprint_before_delete(tmp_path, monkeypatch):
    """The verified copy is content-equal to the planning snapshot before DELETE."""
    seed(tmp_path)
    before = snapshot(tmp_path)
    original_copy = pruning._copy_store
    begin = Store.transaction
    verified = []

    def copy(conn, destination, expected):
        record = original_copy(conn, destination, expected)
        assert set(record["fingerprints"]) == set(before)
        assert record["fingerprints"] == expected
        assert record["fingerprints"]["decisions"]["rows"] == 4
        assert record["verified"] is True
        verified.append(record)
        return record

    def transaction(self, kind, **kwargs):
        if kind == pruning.PRUNE_KIND:
            assert len(verified) == 1
        return begin(self, kind, **kwargs)

    monkeypatch.setattr(pruning, "_copy_store", copy)
    monkeypatch.setattr(Store, "transaction", transaction)
    report = pruning.prune(tmp_path, apply=True, confirm=True)
    expected = {name: value for name, value in report.fingerprints["before"].items()
                if name != "decisions-kept"}
    assert report.backups["copy"]["fingerprints"] == expected
    assert report.deleted == 3


@pytest.mark.parametrize("table,fault", [
    ("decisions", "content"), ("events", "content"),
    ("service_notices", "content"), ("extra_evidence", "content"),
    ("extra_evidence", "count"), ("extra_evidence", "missing"),
    ("unexpected_evidence", "added"),
])
def test_whole_backup_detects_all_table_content_and_inventory_drift(
        tmp_path, monkeypatch, table, fault):
    """The whole-copy proof covers retained rows, audit rows and extra tables."""
    seed(tmp_path)
    with sqlite3.connect(tmp_path / "state.sqlite3") as conn:
        conn.execute("CREATE TABLE extra_evidence (payload BLOB)")
        conn.execute("INSERT INTO extra_evidence VALUES (X'00FF')")
        conn.execute("INSERT INTO service_notices(session_id,text,state,created_at) "
                     "VALUES ('synthetic','original','surfaced','2026-10-01T00:00:00Z')")
    before = snapshot(tmp_path)
    connect = pruning.sqlite3.connect
    begin = Store.transaction
    deletes = []

    def transaction(self, kind, **kwargs):
        if kind == pruning.PRUNE_KIND:
            deletes.append(kind)
        return begin(self, kind, **kwargs)

    def damaged_connect(database, *args, **kwargs):
        if "/backups/state-" in str(database):
            from urllib.parse import unquote, urlsplit

            with contextlib.closing(connect(unquote(urlsplit(str(database)).path))) as backup:
                if fault == "missing":
                    backup.execute(f'DROP TABLE "{table}"')
                elif fault == "added":
                    backup.execute(f'CREATE TABLE "{table}" (payload TEXT)')
                elif fault == "count":
                    backup.execute(f'DELETE FROM "{table}"')
                elif table == "decisions":
                    backup.execute("UPDATE decisions SET decision_json=' {\"note\":\"3\"} '")
                elif table == "events":
                    backup.execute("UPDATE events SET data_json=' {\"changed\":true} '")
                elif table == "service_notices":
                    backup.execute("UPDATE service_notices SET text='altered'")
                else:
                    backup.execute("UPDATE extra_evidence SET payload=X'FF00'")
                backup.commit()
                assert backup.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(Store, "transaction", transaction)
    monkeypatch.setattr(pruning.sqlite3, "connect", damaged_connect)
    with pytest.raises(pruning.PruneVerificationError, match="differs from the planning snapshot") as stopped:
        pruning.prune(tmp_path, apply=True, confirm=True)
    assert table in str(stopped.value)
    assert stopped.value.report.deleted == stopped.value.report.batches == 0
    assert deletes == []
    assert snapshot(tmp_path) == before


def test_whole_backup_is_compared_to_planning_snapshot_not_later_source(tmp_path, monkeypatch):
    """Even a copy matching a later source mutation must fail before DELETE."""
    seed(tmp_path)
    original = pruning._copy_store

    def drift(conn, destination, expected):
        conn.execute("UPDATE decisions SET decision_json=? WHERE decision_id=4",
                     ('{"policy_hash":"hash","note":"X"}',))
        return original(conn, destination, expected)

    monkeypatch.setattr(pruning, "_copy_store", drift)
    with pytest.raises(pruning.PruneVerificationError, match="snapshot.*decisions") as stopped:
        pruning.prune(tmp_path, apply=True, confirm=True)
    assert stopped.value.report.deleted == stopped.value.report.batches == 0
    assert len(snapshot(tmp_path)["decisions"]) == 4


def test_apply_plans_in_one_snapshot_without_repairing_indexes(tmp_path, monkeypatch):
    """No source DDL precedes the original baseline or whole-store backup."""
    seed(tmp_path)
    with sqlite3.connect(tmp_path / "state.sqlite3") as conn:
        index = conn.execute("SELECT name FROM sqlite_master WHERE type='index' "
                             "AND sql IS NOT NULL ORDER BY name LIMIT 1").fetchone()[0]
        conn.execute(f'DROP INDEX "{index}"')
    original_plan = pruning.plan
    original_fingerprints = pruning._fingerprints
    baseline_seen = []

    def plan(conn):
        assert conn.in_transaction
        return original_plan(conn)

    def fingerprints(conn):
        if not baseline_seen:
            assert conn.in_transaction
        baseline_seen.append(True)
        return original_fingerprints(conn)

    monkeypatch.setattr(pruning, "plan", plan)
    monkeypatch.setattr(pruning, "_fingerprints", fingerprints)
    report = pruning.prune(tmp_path, apply=True, confirm=True)
    for database in (tmp_path / "state.sqlite3", report.backups["copy"]["path"]):
        with sqlite3.connect(database) as conn:
            assert conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (index,)).fetchone() is None


@pytest.mark.parametrize("fault", ["bytes", "deleted-count"])
def test_after_vacuum_compares_prune_audit_bytes_and_records_failure(
        tmp_path, monkeypatch, fault):
    """Changed audit bytes fail the full-table proof even at unchanged counts."""
    seed(tmp_path)
    original = pruning._reverify

    def change(conn, report, **kwargs):
        original_payload = conn.execute("SELECT data_json FROM events WHERE event_id>?",
                                        (kwargs["events_max"],)).fetchone()[0]
        payload = " " + original_payload if fault == "bytes" else '{"deleted":0}'
        conn.execute("UPDATE events SET data_json=? WHERE event_id>?",
                     (payload, kwargs["events_max"]))
        return original(conn, report, **kwargs)

    monkeypatch.setattr(pruning, "_reverify", change)
    with pytest.raises(pruning.PruneVerificationError, match="events changed after the delete") as stopped:
        pruning.prune(tmp_path, apply=True, confirm=True, vacuum=True)
    report = stopped.value.report
    assert report.verification["after_vacuum"]["events_identical"] is False
    assert report.verification["after_vacuum"]["ok"] is False
    assert report.fingerprints["after"]["events-all"]["rows"] == report.fingerprints["after_vacuum"]["events-all"]["rows"]
    assert report.backups["copy"]["path"] in stopped.value.fix
    with open(report.path) as saved:
        assert json.load(saved)["verification"]["after_vacuum"]["ok"] is False
