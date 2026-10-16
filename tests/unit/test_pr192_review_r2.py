"""Round-2 fault inputs for PR #192; use only disposable stores."""

import contextlib
import gzip
import json
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from urllib.parse import unquote, urlsplit

import pytest

from subfleet import prune_decisions as pruning
from subfleet.store import Store
from tests.table_order import ordered_select


def seed(root):
    with Store(root / "state.sqlite3") as store:
        store.add_job(job_id="terminal", request_id="terminal", payload_digest="digest",
                      kind="dispatch", workdir=str(root), prompt_path="/synthetic/prompt",
                      sandbox="read-only", state="succeeded")
        for i in range(4):
            store.add_decision("terminal", {"policy_hash": "hash", "note": str(i)})
    with contextlib.closing(sqlite3.connect(root / "state.sqlite3")) as conn:
        conn.execute("CREATE TABLE review_evidence (payload)")
        conn.execute("INSERT INTO review_evidence VALUES (X'00FF')")
        conn.execute("INSERT INTO service_notices(session_id,text,state,created_at) "
                     "VALUES ('synthetic','blob:00ff','surfaced','2026-10-01T00:00:00Z')")
        conn.commit()


def snapshot(database, connect=sqlite3.connect):
    with contextlib.closing(connect(database)) as conn:
        return {name: conn.execute(ordered_select(conn, name)).fetchall()
                for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.mark.parametrize("table,column,value", [
    ("service_notices", "text", "changed"),
    ("review_evidence", "payload", b"\xff\x00"),
    ("service_notices", "text", b"\x00\xff"),
    ("review_evidence", "payload", "blob:00ff"),
])
def test_one_changed_backup_row_at_identical_counts_is_refused(
        tmp_path, monkeypatch, table, column, value):
    seed(tmp_path)
    before = snapshot(tmp_path / "state.sqlite3")
    connect = pruning.sqlite3.connect
    altered = []

    def damage(database, *args, **kwargs):
        if "/backups/state-" in str(database):
            path = Path(unquote(urlsplit(str(database)).path))
            with contextlib.closing(connect(path)) as backup:
                backup.execute(f'UPDATE "{table}" SET "{column}"=?', (value,))
                backup.commit()
                assert backup.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            copied = snapshot(path, connect)
            assert {name: len(rows) for name, rows in copied.items()} == {
                name: len(rows) for name, rows in before.items()}
            assert copied[table] != before[table]
            altered.append(path)
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(pruning.sqlite3, "connect", damage)
    try:
        report = pruning.prune(tmp_path, apply=True, confirm=True)
    except pruning.PruneVerificationError as stopped:
        assert table in str(stopped)
        assert stopped.report.deleted == stopped.report.batches == 0
        assert snapshot(tmp_path / "state.sqlite3") == before
    else:
        copied = snapshot(altered[0])
        pytest.fail(f"prune deleted {report.deleted}; backup verified="
                    f"{report.backups['copy']['verified']}; {table} original="
                    f"{before[table]!r}, copied={copied[table]!r}")


def test_table_added_after_planning_is_refused_before_delete(tmp_path, monkeypatch):
    seed(tmp_path)
    before = snapshot(tmp_path / "state.sqlite3")
    original = pruning._copy_store

    def drift(conn, destination, expected):
        # A separate writer commits after the planning read transaction ended.
        with contextlib.closing(sqlite3.connect(tmp_path / "state.sqlite3")) as writer:
            writer.execute("CREATE TABLE review_late (payload TEXT)")
            writer.execute("INSERT INTO review_late VALUES ('after planning')")
            writer.commit()
        return original(conn, destination, expected)

    monkeypatch.setattr(pruning, "_copy_store", drift)
    with pytest.raises(pruning.PruneVerificationError, match="snapshot.*review_late") as stopped:
        pruning.prune(tmp_path, apply=True, confirm=True)
    assert stopped.value.report.deleted == stopped.value.report.batches == 0
    after = snapshot(tmp_path / "state.sqlite3")
    assert after.pop("review_late") == [("after planning",)]
    assert after == before


def test_vacuum_changed_audit_payload_is_refused(tmp_path, monkeypatch):
    seed(tmp_path)
    original = pruning._reverify

    def damage(conn, report, **kwargs):
        # Same JSON meaning, different bytes, count, kind and id unchanged.
        cursor = conn.execute("UPDATE events SET data_json=' ' || data_json "
                              "WHERE kind=? AND event_id>?",
                              (pruning.PRUNE_KIND, kwargs["events_max"]))
        assert cursor.rowcount == 1
        return original(conn, report, **kwargs)

    monkeypatch.setattr(pruning, "_reverify", damage)
    with pytest.raises(pruning.PruneVerificationError, match="events changed after the delete") as stopped:
        pruning.prune(tmp_path, apply=True, confirm=True, vacuum=True)
    report = stopped.value.report
    assert report.verification["after_vacuum"]["events_identical"] is False
    assert report.backups["copy"]["path"] in stopped.value.fix
    assert report.fingerprints["after"]["events-all"]["rows"] == report.fingerprints[
        "after_vacuum"]["events-all"]["rows"]


@pytest.mark.parametrize("stop", ["interrupt-after-copy", "kill-before-delete"])
def test_stop_between_backup_and_delete_preserves_original_and_copy(tmp_path, monkeypatch, stop):
    seed(tmp_path)
    before = snapshot(tmp_path / "state.sqlite3")
    if stop == "interrupt-after-copy":
        def interrupt(*args):
            raise KeyboardInterrupt("review: stopped after verified whole copy")

        monkeypatch.setattr(pruning, "_backup_rows", interrupt)
        with pytest.raises(pruning.PruneStopped, match="KeyboardInterrupt") as stopped:
            pruning.prune(tmp_path, apply=True, confirm=True)
        report = stopped.value.report
        assert report.deleted == report.batches == 0
        assert report.backups["copy"]["verified"] is True
        assert report.path and Path(report.path).is_file()
    else:
        # The subprocess terminates itself exactly as the first delete
        # transaction would begin; the parent always reaps it.
        source = '''
import os, signal, sys
from subfleet import prune_decisions as pruning
from subfleet.store import Store
original = Store.transaction
def crash(self, kind, **kwargs):
    if kind == pruning.PRUNE_KIND:
        os.kill(os.getpid(), signal.SIGKILL)
    return original(self, kind, **kwargs)
Store.transaction = crash
pruning.prune(sys.argv[1], apply=True, confirm=True)
'''
        child = subprocess.run([sys.executable, "-c", source, str(tmp_path)],
                               cwd=Path(__file__).resolve().parents[2],
                               capture_output=True, text=True, timeout=120)
        assert child.returncode == -signal.SIGKILL, (child.returncode, child.stderr)
        assert not list(tmp_path.glob("decisions-prune-report-*.json"))
        rows = list((tmp_path / "backups").glob("decisions-pruned-*.jsonl.gz"))
        assert len(rows) == 1
        with gzip.open(rows[0], "rt") as stream:
            backed_up = [json.loads(line) for line in stream]
        assert [row["decision_id"] for row in backed_up] == [1, 2, 3]
    assert snapshot(tmp_path / "state.sqlite3") == before
    copies = list((tmp_path / "backups").glob("state-*.sqlite3"))
    assert len(copies) == 1
    assert snapshot(copies[0]) == before
    # Kernel locks are released even when the process cannot run its cleanup.
    lock = pruning._hold_daemon_lock(tmp_path)
    pruning._release_daemon_lock(lock)


def test_later_archive_rows_have_no_prune_record(tmp_path):
    from subfleet.retention_archive import job_rows

    seed(tmp_path)
    report = pruning.prune(tmp_path, apply=True, confirm=True)
    with Store(tmp_path / "state.sqlite3", read_only=True) as store:
        rows = job_rows(store, "terminal")
        assert [row["decision_id"] for row in rows["decisions"]] == [4]
        assert "events" not in rows
        assert not any("prun" in key for key in rows)
        assert report.deleted == 3
