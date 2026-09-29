"""The one-off `decisions` prune: what it keeps, what it proves, what it refuses.

Every store here is a synthetic one built in a temp directory; nothing in this
file reads `~/.subfleet`. Each test names the clause it proves (C-20.5), or the
brief decision it pins where the contract has no clause for the behaviour — it
has none for a free-space guard, a gzip row backup, or a `--vacuum` rebuild,
which belong to this one-off pass rather than to the daemon.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import functools
import gzip
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import types
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import prune_decisions
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.offline import Offline
from subfleet.prune_decisions import (PruneRefused, PruneReportUnsaved, PruneStopped,
                                      PruneVerificationError, prune)
from subfleet.store import SCHEMA_VERSION, SchemaVersionError, Store

TERMINAL = "20260920-100000-terminal"
MIXED = "20260920-110000-mixed"
LIVE = "20260920-120000-waiting"


def seed_job(store: Store, root: Path, job_id: str, *, state: str = "succeeded") -> None:
    store.add_job(job_id=job_id, request_id=job_id, payload_digest="digest", kind="dispatch",
                  workdir=str(root), prompt_path="/prompt", sandbox="read-only", state=state)


def seed_decision(store: Store, job_id: str, note: str, *, attempt_id: str | None = None,
                  evaluated_at: str | None = None, size: int = 0) -> int:
    """One decision row. `evaluated_at` is set directly when a test needs an order.

    `Store.add_decision` stamps `utc_now()` at second precision, so rows written
    in one test share an instant and the readers tie-break on `decision_id`; a
    test that needs the two orderings to disagree says so with `evaluated_at`.
    """
    payload = {"policy_hash": "policy-sha", "note": note, "padding": "x" * size}
    decision_id = store.add_decision(job_id, payload, attempt_id)
    if evaluated_at is not None:
        store.connection.execute("UPDATE decisions SET evaluated_at=? WHERE decision_id=?",
                                 (evaluated_at, decision_id))
    return decision_id


@pytest.fixture
def seeded(tmp_path):
    """A store with a pruned-out terminal job, a job whose orderings disagree, and a live one."""
    with Store(tmp_path / "state.sqlite3") as store:
        store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/home/one", "home"),
                            "/home/one", LaneOwner.V2, False))
        for job_id, state in ((TERMINAL, "succeeded"), (MIXED, "failed"), (LIVE, "waiting")):
            seed_job(store, tmp_path, job_id, state=state)
        # Rows in every other table, so the invariance proof has something to prove.
        store.add_attempt(attempt_id=f"{TERMINAL}/a1", job_id=TERMINAL, seq=1, lane_id="codex-1",
                          model_requested="gpt-6-astra", state="succeeded")
        store.add_artifact(f"{TERMINAL}/a1", "deliverable", "/out.md", "sha", 12)
        store.add_notice(TERMINAL, "finished", "sess-1", state="acknowledged")
        store.acquire_lease("lane:codex-1:slot:1", f"{TERMINAL}/a1")
        store.add_action(action_id="reset-1", kind="reset-credit", op_key="codex:one:credit",
                         subject="codex:one", state="confirmed")
        store.connection.execute(
            "INSERT INTO service_notices (session_id, text, state, created_at)"
            " VALUES (?,?,?,?)", ("sess-1", "operator message", "surfaced", "2026-09-20T10:00:00Z"))

        for index in range(5):
            seed_decision(store, TERMINAL, f"waiting {index}")
        seed_decision(store, TERMINAL, "reserved", attempt_id=f"{TERMINAL}/a1")
        # MIXED's newest row by id is not its newest by clock, so the daemon's
        # reader and the offline reader select different rows.
        seed_decision(store, MIXED, "old id, new clock", evaluated_at="2026-09-20T11:30:00Z")
        seed_decision(store, MIXED, "middle", evaluated_at="2026-09-20T11:10:00Z")
        seed_decision(store, MIXED, "new id, old clock", evaluated_at="2026-09-20T11:20:00Z")
        for index in range(3):
            seed_decision(store, LIVE, f"still waiting {index}")
        yield store, tmp_path


def rows(root: Path, table: str, order: str) -> list[dict]:
    connection = sqlite3.connect((root / "state.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}')]
    finally:
        connection.close()


def snapshot(root: Path) -> dict[str, list[dict]]:
    """Every table as rows, for a mutation check that does not trust one connection.

    `total_changes` counts one connection's own writes, and the prune opens its
    own; what a test wants to know is whether the database moved.
    """
    connection = sqlite3.connect((root / "state.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        names = [row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name")]
        return {name: sorted((json.dumps(dict(row), sort_keys=True)
                              for row in connection.execute(f'SELECT * FROM "{name}"')))
                for name in names}
    finally:
        connection.close()


def decision_ids(root: Path) -> list[int]:
    return [row["decision_id"] for row in rows(root, "decisions", "decision_id")]


def applied(root: Path, **kwargs):
    return prune(root, apply=True, confirm=True, write_report=False, **kwargs)


def closed_store(root: Path) -> Path:
    """A seeded store with nothing holding it open and no `-wal`/`-shm` beside it.

    SQLite creates both sidecars whenever a WAL database is opened, read-only
    included, so a fixture that keeps a connection open cannot tell what the
    pass itself put in the state root.
    """
    with Store(root / "state.sqlite3") as store:
        seed_job(store, root, TERMINAL)
        for index in range(4):
            seed_decision(store, TERMINAL, f"waiting {index}")
    for sidecar in ("state.sqlite3-wal", "state.sqlite3-shm"):
        (root / sidecar).unlink(missing_ok=True)
    return root / "state.sqlite3"


def mutating(root: Path, mutate):
    """A `_verify` that runs `mutate` on a second connection first, then verifies.

    The before-fingerprints were taken at the top of the pass and the last batch
    has committed, so a write here is exactly the drift each detector exists to
    catch. The pass is between transactions at that point and the store is in
    WAL, so a second connection can write.
    """
    original = prune_decisions._verify

    def probe(conn, report, **kwargs):
        other = sqlite3.connect(root / "state.sqlite3", timeout=5)
        try:
            mutate(other)
            other.commit()
        finally:
            other.close()
        return original(conn, report, **kwargs)

    return probe


class Answers(list):
    """What `Connection.execute` hands back, for a canned pragma answer."""

    def fetchall(self):
        return list(self)


class Answering:
    """A connection that answers one statement itself and delegates every other.

    `PRAGMA integrity_check` and `PRAGMA foreign_key_check` cannot be made to
    fail on a healthy file without corrupting it, so those two detectors are
    driven with the answer SQLite gives for a file that is not healthy.
    """

    def __init__(self, conn, statement, answers):
        self._conn, self._statement, self._answers = conn, statement, Answers(answers)

    def execute(self, sql, *args):
        return self._answers if sql == self._statement else self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def drifting_readers(monkeypatch) -> None:
    """A proof that fails after the deletes have committed.

    The second reading of the readers — the one taken after the delete — comes
    back changed, so `_verify` raises with every batch already committed and
    the backups taken: the state in which the report matters most.
    """
    original = prune_decisions._reader_bytes
    calls: list[int] = []

    def drifting(conn, jobs):
        served = original(conn, jobs)
        calls.append(len(calls))
        if len(calls) > 1:
            return {job: dict(value, why="other bytes") for job, value in served.items()}
        return served

    monkeypatch.setattr(prune_decisions, "_reader_bytes", drifting)


def answering(statement: str, answers: list[tuple]):
    """A `_verify` that sees `statement` answered with `answers`."""
    original = prune_decisions._verify

    def probe(conn, report, **kwargs):
        return original(Answering(conn, statement, answers), report, **kwargs)

    return probe


# --- the keep set --------------------------------------------------------------

def test_the_keep_set_is_exactly_what_a_reader_or_an_attempt_holds(seeded):
    """C-11.5: a decision is kept when an attempt owns it or a reader selects it."""
    store, root = seeded
    report = prune(root, write_report=False)
    planned = report.decisions
    kept = {"attempt": 1, "why": 2, "show_job": 2,
            "latest-attemptless": 2, "latest-attemptless-by-time": 2, "live-job": 3}
    assert planned["kept_by"] == kept
    # TERMINAL keeps its attempt row and its last attempt-less row; MIXED keeps
    # the two rows its two readers disagree about; LIVE keeps all three.
    assert planned["total"] == 12
    assert planned["keep"] == 7
    assert planned["delete"] == 5
    assert planned["jobs"] == 3
    assert planned["jobs_affected"] == 2
    assert planned["live_jobs"] == 1


def test_a_job_that_is_not_terminal_keeps_its_whole_history(seeded):
    """C-4.1, C-8.4: a live job's rows are the daemon's, and the prune leaves them."""
    store, root = seeded
    before = [row for row in rows(root, "decisions", "decision_id") if row["job_id"] == LIVE]
    applied(root, batch_size=2)
    after = [row for row in rows(root, "decisions", "decision_id") if row["job_id"] == LIVE]
    assert after == before
    assert len(after) == 3


def test_the_two_readers_return_identical_bytes_for_every_affected_job(seeded):
    """C-11.5, C-17.5: `why` and offline `runs show` serve the same decision after the pass."""
    store, root = seeded
    daemon_query = prune_decisions.READER_QUERIES["why"]
    before = {job: (store.one(daemon_query, (job,))["decision_json"],
                    json.dumps(Offline(root).show_job(job)["decision"], sort_keys=True))
              for job in (TERMINAL, MIXED, LIVE)}
    report = applied(root, batch_size=2)
    after = {job: (store.one(daemon_query, (job,))["decision_json"],
                   json.dumps(Offline(root).show_job(job)["decision"], sort_keys=True))
             for job in (TERMINAL, MIXED, LIVE)}
    assert after == before
    assert report.verification["readers_identical"] == report.verification["readers_checked"] == 2


def test_the_daemon_and_offline_readers_keep_the_rows_they_each_select(seeded):
    """C-11.5: the two reader orderings are asked separately, never assumed equal."""
    store, root = seeded
    by_id = store.one(prune_decisions.READER_QUERIES["why"], (MIXED,))["decision_id"]
    by_clock = store.one(prune_decisions.READER_QUERIES["show_job"], (MIXED,))["decision_id"]
    assert by_id != by_clock
    applied(root, batch_size=2)
    assert {row["decision_id"] for row in rows(root, "decisions", "decision_id")} >= {by_id, by_clock}


# --- the dry run ---------------------------------------------------------------

def test_the_dry_run_is_the_default_and_writes_no_file_of_its_own_but_the_report(tmp_path):
    """C-3.4: the dry run opens the store read-only, so it writes no row and no file of its own.

    The store is built and closed first: the pass's own connection makes
    SQLite's `-wal` and `-shm` appear beside a WAL database, and those are the
    only new names a dry run may leave.
    """
    root = tmp_path / "root"
    closed_store(root)
    assert sorted(path.name for path in root.iterdir()) == ["state.sqlite3"]
    report = prune(root, write_report=False)
    assert report.dry_run is True
    assert report.batches == 0 and report.deleted == 0
    assert {path.name for path in root.iterdir()} - {"state.sqlite3"} <= {
        "state.sqlite3-wal", "state.sqlite3-shm"}
    assert not (root / "backups").exists()
    before = snapshot(root)
    files = sorted(path.name for path in root.iterdir())
    saved = prune(root)
    assert Path(saved.path).name.startswith("decisions-prune-report-")
    assert sorted(path.name for path in root.iterdir()) == sorted(files + [Path(saved.path).name])
    assert snapshot(root) == before


def test_the_dry_run_counts_what_a_real_pass_deletes(seeded):
    """Brief decision 4: the dry run's numbers are the pass's, not an estimate of it."""
    store, root = seeded
    dry = prune(root, write_report=False)
    real = applied(root, batch_size=2)
    assert real.decisions == dry.decisions
    assert real.deleted == dry.decisions["delete"]
    assert len(decision_ids(root)) == dry.decisions["keep"]


def test_apply_without_the_confirmation_is_refused(seeded):
    """Brief decision 4: writing takes --apply and --i-understand-this-deletes-decisions."""
    store, root = seeded
    before = snapshot(root)
    with pytest.raises(PruneRefused) as refusal:
        prune(root, apply=True, write_report=False)
    assert refusal.value.code == 7
    assert "--i-understand-this-deletes-decisions" in refusal.value.fix
    assert snapshot(root) == before
    assert prune_decisions.main(["--state-root", str(root), "--apply", "--no-report"]) == 7


# --- refusals ------------------------------------------------------------------

def test_a_daemon_holding_the_lock_refuses_the_write_pass(seeded):
    """C-3.4: the daemon is the store's writer, and two writers is the one thing to avoid."""
    store, root = seeded
    handle = os.open(root / "daemon.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(PruneRefused) as refusal:
            applied(root)
        assert refusal.value.code == 7
        assert "daemon.lock" in str(refusal.value)
        assert refusal.value.fix.startswith("subfleet daemon stop")
        assert "KeepAlive" in refusal.value.fix          # launchd starts it again
        prune(root, write_report=False)                  # a dry run never takes that path
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)
    assert len(decision_ids(root)) == 12


def test_the_pass_holds_the_lock_for_its_whole_length(seeded, monkeypatch):
    """C-3.4: a check released before the work leaves a window for a second writer.

    Asked at three points, so a lock let go anywhere in between shows as a
    False: before the first DELETE (`_backup_rows`), after the last one
    (`_verify`), and after the rebuild (`_reverify`).
    """
    store, root = seeded
    held: list[bool] = []

    def probing(original):
        def probe(*args, **kwargs):
            handle = os.open(root / "daemon.lock", os.O_RDWR)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(handle)
            return original(*args, **kwargs)
        return probe

    for name in ("_backup_rows", "_verify", "_reverify"):
        monkeypatch.setattr(prune_decisions, name, probing(getattr(prune_decisions, name)))
    applied(root, batch_size=2, vacuum=True)
    assert held == [True, True, True]


def test_a_volume_without_room_for_the_backups_is_refused_with_the_numbers(seeded, monkeypatch):
    """Brief decision 6: the backups come first, so the pass refuses before it deletes."""
    store, root = seeded
    before = snapshot(root)
    monkeypatch.setattr(prune_decisions.shutil, "disk_usage",
                        lambda path: types.SimpleNamespace(total=1 << 30, used=1 << 30, free=1024))
    with pytest.raises(PruneRefused) as refusal:
        applied(root)
    assert refusal.value.code == 7
    assert "1.02 KB free" in str(refusal.value) and "needs" in str(refusal.value)
    assert "1.25" in str(refusal.value)                 # the formula, in the message
    assert snapshot(root) == before
    assert not (root / "backups").exists()
    # The dry run records the same verdict rather than refusing: reporting is its job.
    report = prune(root, write_report=False)
    assert report.space["sufficient"] is False


def test_one_volume_is_charged_for_the_backups_and_the_rebuild_together(seeded):
    """Brief decision 6: `--vacuum` writes a second whole database on the state root's volume."""
    store, root = seeded
    with_vacuum = prune(root, write_report=False, vacuum=True)
    size = with_vacuum.space["database_bytes"]
    assert with_vacuum.space["same_volume"] is True
    assert with_vacuum.space["rebuild_bytes"] == max(
        size - with_vacuum.decisions["delete_bytes"], 0) > 0
    assert with_vacuum.space["volumes"][str(root)]["needed"] == (
        int(size * prune_decisions.NEEDED_FACTOR) + with_vacuum.space["rebuild_bytes"]
        + prune_decisions.NEEDED_MARGIN)
    assert "the rebuild" in with_vacuum.space["formula"]
    # Without the rebuild the same volume needs only the two backups and the margin.
    without = prune(root, write_report=False)
    assert without.space["rebuild_bytes"] == 0
    assert without.space["volumes"][str(root)]["needed"] == (
        int(size * prune_decisions.NEEDED_FACTOR) + prune_decisions.NEEDED_MARGIN)
    assert prune_decisions.NEEDED_MARGIN_TEXT in without.space["formula"]


def test_a_backup_dir_on_another_volume_charges_each_volume_for_its_half(seeded, monkeypatch):
    """Brief decision 6: with --backup-dir elsewhere each volume is measured for what it takes."""
    store, root = seeded
    elsewhere = root.parent / "other-volume" / "backups"
    mounts = {str(prune_decisions._existing(root)): 1,
              str(prune_decisions._existing(elsewhere)): 2}
    monkeypatch.setattr(prune_decisions, "_volume", lambda path: mounts[str(path)])
    frees = {str(root): 8 << 30, str(root.parent): 1024}
    monkeypatch.setattr(prune_decisions.shutil, "disk_usage",
                        lambda path: types.SimpleNamespace(total=1 << 40, used=0,
                                                           free=frees[str(path)]))
    report = prune(root, write_report=False, backup_dir=elsewhere, vacuum=True)
    size = report.space["database_bytes"]
    assert report.space["same_volume"] is False
    assert report.space["volumes"][str(root.parent)]["needed"] == int(
        size * prune_decisions.NEEDED_FACTOR)
    assert report.space["volumes"][str(root)]["needed"] == (
        report.space["rebuild_bytes"] + prune_decisions.NEEDED_MARGIN)
    assert report.space["sufficient"] is False
    before = snapshot(root)
    with pytest.raises(PruneRefused) as refusal:
        applied(root, backup_dir=elsewhere, vacuum=True)
    assert refusal.value.code == 7
    assert str(root.parent) in str(refusal.value)        # the volume without the room
    assert "1.02 KB free" in str(refusal.value)
    assert snapshot(root) == before


def test_a_backup_that_is_already_there_is_refused_rather_than_overwritten(seeded):
    """Brief decision 6: the evidence of an earlier pass is never written over."""
    store, root = seeded
    stamp = "20260920T100000Z"
    (root / "backups").mkdir(mode=0o700)
    (root / "backups" / f"state-{stamp}.sqlite3").write_bytes(b"an earlier copy")
    before = snapshot(root)
    with pytest.raises(PruneRefused) as refusal:
        applied(root, now="2026-09-20T10:00:00Z")
    assert refusal.value.code == 7
    assert "already exists" in str(refusal.value)
    assert (root / "backups" / f"state-{stamp}.sqlite3").read_bytes() == b"an earlier copy"
    assert snapshot(root) == before


def test_a_schema_newer_than_this_build_is_refused(seeded, capsys):
    """C-3.5: the store is newer than this build, so the pass exits 1 naming both versions."""
    store, root = seeded
    store.connection.execute("INSERT INTO schema_version VALUES (?,?)",
                             (99, "2026-09-20T10:00:00Z"))
    with pytest.raises(SchemaVersionError):
        prune(root, write_report=False)
    assert prune_decisions.main(["--state-root", str(root), "--no-report"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (f"subfleet: prune decisions: store schema 99 is newer than supported schema "
            f"{SCHEMA_VERSION}") in captured.err
    assert "  fix: " in captured.err
    assert len(decision_ids(root)) == 12


def test_a_schema_behind_this_build_is_refused_before_a_write_can_migrate_it(seeded):
    """C-3.1: a write-open migrates an older store, writing rows outside this pass's proof.

    `Store.__init__` runs the numbered migrations and inserts `schema_version`
    and `events` rows before `_pass` takes its copy or its fingerprints, so the
    copy offered as the restore path would already be a migrated store and the
    proof would call the store untouched.
    """
    store, root = seeded
    store.connection.execute("DELETE FROM schema_version")
    store.connection.execute("INSERT INTO schema_version VALUES (?,?)",
                             (SCHEMA_VERSION - 1, "2026-09-20T10:00:00Z"))
    before = snapshot(root)
    with pytest.raises(PruneRefused) as refusal:
        applied(root)
    assert refusal.value.code == 7
    assert f"schema {SCHEMA_VERSION - 1} and this build at {SCHEMA_VERSION}" in str(refusal.value)
    assert refusal.value.fix.startswith("subfleet daemon start")
    assert snapshot(root) == before
    # The dry run records the mismatch and still plans: it opens the store
    # read-only, which `Store` never migrates.
    report = prune(root, write_report=False)
    assert report.schema == {"store": SCHEMA_VERSION - 1, "build": SCHEMA_VERSION}
    assert report.decisions["delete"] == 5
    assert snapshot(root) == before


def test_a_missing_store_is_refused(tmp_path):
    """Brief decision 1: the pass names what it could not find instead of creating it."""
    with pytest.raises(PruneRefused) as refusal:
        prune(tmp_path, write_report=False)
    assert refusal.value.code == 7
    assert not (tmp_path / "state.sqlite3").exists()


# --- the proofs ----------------------------------------------------------------

def test_every_other_table_is_byte_identical_after_the_pass(seeded):
    """C-3.2: jobs, attempts, artifacts, notices, leases and the rest are untouched."""
    store, root = seeded
    before = snapshot(root)
    report = applied(root, batch_size=2)
    after = snapshot(root)
    for table in before:
        if table in ("decisions", "events"):
            continue
        assert after[table] == before[table], table
    assert report.verification["tables_identical"] is True
    # Enumerated from the schema, not named here, so a new table is covered.
    assert set(report.verification["tables_checked"]) == set(before) - {"decisions", "events"}
    assert "service_notices" in report.verification["tables_checked"]


def test_events_gain_one_row_per_batch_and_rewrite_no_history(seeded):
    """C-3.2: the delete is a state change, so each batch inserts exactly one events row."""
    store, root = seeded
    before = rows(root, "events", "event_id")
    report = applied(root, batch_size=2)
    after = rows(root, "events", "event_id")
    assert after[:len(before)] == before
    added = after[len(before):]
    assert report.batches == 3                           # 5 rows, two at a time
    assert len(added) == report.batches
    assert {row["kind"] for row in added} == {"decisions.pruned"}
    assert [json.loads(row["data_json"])["deleted"] for row in added] == [2, 2, 1]
    assert report.verification["events_append_only"] is True
    assert report.verification["events_added"] == {"decisions.pruned": 3}


def test_the_deleted_rows_round_trip_out_of_the_gzip_backup(seeded):
    """Brief decision 6: every deleted row is recoverable, and the digest names the file."""
    store, root = seeded
    before = {row["decision_id"]: row for row in rows(root, "decisions", "decision_id")}
    report = applied(root, batch_size=2)
    backup = Path(report.backups["rows"]["path"])
    assert backup.parent == root / "backups"
    assert oct(backup.stat().st_mode)[-3:] == "600"
    assert oct((root / "backups").stat().st_mode)[-3:] == "700"
    recovered = [json.loads(line) for line in gzip.open(backup, "rb")]
    assert len(recovered) == report.backups["rows"]["rows"] == report.deleted
    survivors = set(decision_ids(root))
    for row in recovered:
        assert row == before[row["decision_id"]]
        assert row["decision_id"] not in survivors
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    assert digest == report.backups["rows"]["sha256"]
    assert report.backups["rows"]["verified"] is True


def test_the_whole_store_is_copied_and_checked_before_the_first_delete(seeded):
    """Brief decision 6, docs/migration.md: `VACUUM INTO` a copy, then check it."""
    store, root = seeded
    before = decision_ids(root)
    report = applied(root, batch_size=2)
    copy = Path(report.backups["copy"]["path"])
    assert copy.parent == root / "backups"
    assert oct(copy.stat().st_mode)[-3:] == "600"
    assert report.backups["copy"]["integrity_check"] == "ok"
    connection = sqlite3.connect(copy.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        copied = [row[0] for row in connection.execute(
            "SELECT decision_id FROM decisions ORDER BY decision_id")]
    finally:
        connection.close()
    assert copied == before                              # the copy predates the delete
    assert len(decision_ids(root)) < len(before)


def test_the_store_passes_integrity_and_foreign_key_checks(seeded):
    """C-3.1: `decisions` is a leaf table, and the pass proves it rather than reasoning it."""
    store, root = seeded
    report = applied(root, batch_size=2)
    assert report.verification["integrity_check"] == "ok"
    assert report.verification["foreign_key_check"] == "ok"
    assert report.verification["ok"] is True
    connection = sqlite3.connect((root / "state.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        assert [row[0] for row in connection.execute("PRAGMA integrity_check")] == ["ok"]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_a_verification_failure_reports_the_restore_path_and_saves_the_report(seeded,
                                                                             monkeypatch):
    """Brief decision 8: a proof that does not hold exits non-zero and names the copy."""
    store, root = seeded
    drifting_readers(monkeypatch)
    with pytest.raises(PruneVerificationError) as failure:
        prune(root, apply=True, confirm=True, batch_size=2)
    assert failure.value.code == 1
    fix = failure.value.fix
    # All three files, because SQLite replays a log left beside the database.
    assert "`state.sqlite3`, `state.sqlite3-wal` and `state.sqlite3-shm`" in fix
    assert failure.value.report.backups["copy"]["path"] in fix
    assert "PRAGMA integrity_check" in fix
    assert failure.value.report.verification["ok"] is False
    assert failure.value.report.errors
    saved = json.loads(Path(failure.value.report.path).read_text())
    assert saved["verification"]["ok"] is False
    assert saved["backups"]["copy"]["path"].endswith(".sqlite3")


# --- each detector in its failing direction ------------------------------------

def test_a_change_to_another_table_fails_the_proof(seeded, monkeypatch):
    """C-3.2: every table but `decisions` and `events` must be digest-identical after."""
    store, root = seeded
    monkeypatch.setattr(prune_decisions, "_verify", mutating(root, lambda other: other.execute(
        "UPDATE jobs SET state='lost' WHERE job_id=?", (TERMINAL,))))
    with pytest.raises(PruneVerificationError) as failure:
        applied(root, batch_size=2)
    assert "jobs changed" in str(failure.value)
    assert failure.value.report.verification["tables_identical"] is False
    assert failure.value.report.verification["ok"] is False


def test_a_rewritten_events_row_fails_the_proof(seeded, monkeypatch):
    """C-3.2: `events` is the append-only audit spine, so a row already there cannot change."""
    store, root = seeded
    monkeypatch.setattr(prune_decisions, "_verify", mutating(root, lambda other: other.execute(
        "UPDATE events SET kind='tampered' WHERE event_id=(SELECT MIN(event_id) FROM events)")))
    with pytest.raises(PruneVerificationError) as failure:
        applied(root, batch_size=2)
    assert "events rewrote history" in str(failure.value)
    assert failure.value.report.verification["events_append_only"] is False


def test_an_extra_events_row_fails_the_proof(seeded, monkeypatch):
    """C-3.2: one transaction, one events row — so a batch may add exactly one."""
    store, root = seeded
    monkeypatch.setattr(prune_decisions, "_verify", mutating(root, lambda other: other.execute(
        "INSERT INTO events(ts,kind,data_json) VALUES"
        " ('2026-09-20T10:00:00Z','decisions.pruned','{}')")))
    with pytest.raises(PruneVerificationError) as failure:
        applied(root, batch_size=2)
    assert "events gained {'decisions.pruned': 4}, not 3 decisions.pruned rows" in str(failure.value)


def test_an_edited_surviving_row_fails_the_proof(seeded, monkeypatch):
    """C-11.5: the rows the plan kept must still be those rows, byte for byte."""
    store, root = seeded
    monkeypatch.setattr(prune_decisions, "_verify", mutating(root, lambda other: other.execute(
        "UPDATE decisions SET policy_hash='other'"
        " WHERE decision_id=(SELECT MIN(decision_id) FROM decisions)")))
    with pytest.raises(PruneVerificationError) as failure:
        applied(root, batch_size=2)
    assert "the kept decisions changed" in str(failure.value)
    assert failure.value.report.verification["decisions_identical"] is False


def test_a_job_that_loses_its_last_decision_fails_the_proof(seeded, monkeypatch):
    """C-11.5: every job that had a decision still has one, so the distinct count cannot move."""
    store, root = seeded
    monkeypatch.setattr(prune_decisions, "_verify", mutating(root, lambda other: other.execute(
        "DELETE FROM decisions WHERE job_id=?", (LIVE,))))
    with pytest.raises(PruneVerificationError) as failure:
        applied(root, batch_size=2)
    assert "2 jobs have decisions, not 3" in str(failure.value)


def test_an_integrity_check_that_is_not_ok_fails_the_proof(seeded, monkeypatch):
    """C-3.1: the store's own integrity check is part of the proof, not a formality."""
    store, root = seeded
    monkeypatch.setattr(prune_decisions, "_verify",
                        answering("PRAGMA integrity_check", [("row 7 missing from index",)]))
    with pytest.raises(PruneVerificationError) as failure:
        applied(root, batch_size=2)
    assert "integrity_check: row 7 missing from index" in str(failure.value)
    assert failure.value.report.verification["integrity_check"] == "row 7 missing from index"
    assert failure.value.report.verification["ok"] is False


def test_a_foreign_key_check_that_reports_rows_fails_the_proof(seeded, monkeypatch):
    """C-3.1: `decisions.job_id` references `jobs`, and the proof asks rather than assumes."""
    store, root = seeded
    monkeypatch.setattr(prune_decisions, "_verify",
                        answering("PRAGMA foreign_key_check", [("decisions", 9, "jobs", 0)]))
    with pytest.raises(PruneVerificationError) as failure:
        applied(root, batch_size=2)
    assert "foreign_key_check reported 1 rows" in str(failure.value)
    assert failure.value.report.verification["foreign_key_check"] == "1 rows"


# --- idempotence and space -----------------------------------------------------

def test_a_second_apply_changes_nothing_and_writes_no_backup(seeded):
    """Brief decision 8: the keep set is everything left, so the pass has nothing to do."""
    store, root = seeded
    applied(root, batch_size=2)
    before = snapshot(root)
    backups = sorted(path.name for path in (root / "backups").iterdir())
    second = applied(root, batch_size=2, vacuum=True)
    assert second.decisions["delete"] == 0
    assert second.batches == 0 and second.deleted == 0
    # --vacuum belongs on the pass that deletes and took the backups.
    assert second.verification == {"nothing_to_prune": True, "vacuum_skipped": True}
    assert snapshot(root) == before
    assert sorted(path.name for path in (root / "backups").iterdir()) == backups


def big_store(root: Path) -> None:
    """Enough decision_json that the pages it holds are visible in the file size."""
    with Store(root / "state.sqlite3") as store:
        seed_job(store, root, TERMINAL)
        for index in range(24):
            seed_decision(store, TERMINAL, f"waiting {index}", size=64 * 1024)


def test_vacuum_returns_the_freed_pages_to_the_filesystem(tmp_path):
    """Brief: without --vacuum the freelist holds the space; with it the file shrinks."""
    root = tmp_path / "root"
    big_store(root)
    without = applied(root, batch_size=8)
    assert without.database_bytes["freelist_after"] > 0
    assert without.database_bytes["after"] >= without.database_bytes["before"]
    assert without.database_bytes["page_count_after"] == \
        without.database_bytes["page_count_before"]

    second = tmp_path / "vacuumed"
    big_store(second)
    with_vacuum = applied(second, batch_size=8, vacuum=True)
    assert with_vacuum.verification["integrity_check_after_vacuum"] == "ok"
    assert with_vacuum.database_bytes["freelist_after"] == 0
    assert with_vacuum.database_bytes["after"] < with_vacuum.database_bytes["before"] // 2
    assert with_vacuum.database_bytes["page_count_after"] < \
        with_vacuum.database_bytes["page_count_before"]


def test_the_backup_directory_can_live_on_another_path(seeded, tmp_path):
    """Brief: --backup-dir puts the copy where there is room for it."""
    store, root = seeded
    elsewhere = tmp_path / "elsewhere" / "backups"
    report = applied(root, batch_size=2, backup_dir=elsewhere)
    assert Path(report.backups["copy"]["path"]).parent == elsewhere
    assert Path(report.backups["rows"]["path"]).parent == elsewhere
    assert not (root / "backups").exists()


# --- the report and the entry point --------------------------------------------

def test_the_report_records_the_plan_the_backups_and_the_proofs(seeded):
    """Brief: the written report is the pass's deliverable."""
    store, root = seeded
    report = prune(root, apply=True, confirm=True, batch_size=2)
    saved = json.loads(Path(report.path).read_text())
    assert Path(report.path).name.startswith("decisions-prune-report-")
    assert saved["dry_run"] is False
    assert saved["decisions"]["delete"] == 5
    assert saved["backups"]["rows"]["rows"] == 5
    assert saved["verification"]["ok"] is True
    assert saved["fingerprints"]["before"]["jobs"] == saved["fingerprints"]["after"]["jobs"]
    assert saved["errors"] == []


def test_main_plans_by_default_and_prints_json(seeded, capsys):
    """C-17.4: the contract goes to stdout; the default writes nothing."""
    store, root = seeded
    before = snapshot(root)
    assert prune_decisions.main(["--state-root", str(root), "--json", "--no-report"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["decisions"]["delete"] == 5
    assert snapshot(root) == before


def test_main_applies_and_prints_a_human_report(seeded, capsys):
    """Brief: the operator's view names the backups and what was proved."""
    store, root = seeded
    assert prune_decisions.main([
        "--state-root", str(root), "--apply", "--i-understand-this-deletes-decisions",
        "--no-report"]) == 0
    printed = capsys.readouterr().out
    assert "decisions-pruned-" in printed and "state-" in printed
    assert "5 rows" in printed
    assert len(decision_ids(root)) == 7


def test_a_dry_run_reads_one_snapshot_while_a_daemon_keeps_writing(seeded, monkeypatch):
    """C-3.4, C-11.5 the 2026-09-21 dry run: a row written mid-plan for a waiting job no longer fails the check."""
    store, root = seeded
    store.close()
    real_plan = prune_decisions.plan
    writer = sqlite3.connect(str(root / "state.sqlite3"), timeout=5)

    def plan_then_the_daemon_writes(conn):
        planned = real_plan(conn)
        # What the live daemon did: a waiting job's newest decision, after the plan was read.
        writer.execute("INSERT INTO decisions(job_id,attempt_id,evaluated_at,policy_hash,decision_json) "
                       "VALUES(?,NULL,'2099-01-01T00:00:00Z','h','{}')", (LIVE,))
        writer.commit()
        return planned
    monkeypatch.setattr(prune_decisions, "plan", plan_then_the_daemon_writes)
    try:
        report = prune_decisions.prune(root, write_report=False)
    finally:
        writer.close()
    assert report.dry_run and report.deleted == 0
    assert report.decisions["total"] == len(decision_ids(root)) - 1     # the plan's instant, not the row after it
    assert rows(root, "decisions", "decision_id")[-1]["evaluated_at"] == "2099-01-01T00:00:00Z"


# --- a report that cannot be saved ---------------------------------------------
#
# Invariants, for every outcome of the pass and every way saving its report can
# go: the exit code, the error and the fix are the ones the same pass gives with
# a working save (a finished pass whose report is lost exits 1, not 0); the
# report is on disk or printed in full, and is the report a working save writes
# but for the save's own error; a copy the pass took is on disk and named in any
# fix; and the store ends the same either way.

NO_SPACE = "OSError: [Errno 28] No space left on device"


def unsavable(monkeypatch, make=lambda: OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))):
    """Publishing the report fails the way a full volume makes it fail."""
    def publish(path, data):
        raise make()

    monkeypatch.setattr(prune_decisions, "atomic_publish", publish)


def printed_report(err: str) -> dict:
    """The report `_fail` prints in full when it is not on disk."""
    _, marker, body = err.partition("; here it is in full:\n")
    assert marker, err
    return json.loads(body)


def interrupted_at(monkeypatch, batch: int) -> None:
    """A Ctrl-C as delete batch `batch` begins, the earlier ones committed."""
    begun = []

    class Interrupted(Store):
        def transaction(self, kind="state.changed", **kwargs):
            if kind == prune_decisions.PRUNE_KIND:
                begun.append(kind)
                if len(begun) == batch:
                    raise KeyboardInterrupt
            return super().transaction(kind, **kwargs)

    monkeypatch.setattr(prune_decisions, "Store", Interrupted)


def test_a_report_that_cannot_be_saved_keeps_the_failed_proof_and_its_restore(seeded,
                                                                            monkeypatch):
    """Brief decision 8: after committed deletes, a full volume at the report hides neither the error nor the copy."""
    store, root = seeded
    drifting_readers(monkeypatch)
    unsavable(monkeypatch)
    with pytest.raises(PruneVerificationError) as failure:
        prune(root, apply=True, confirm=True, batch_size=2)
    report = failure.value.report
    assert "a reader returns different bytes" in str(failure.value)
    assert report.deleted == 5 and report.batches == 3
    assert report.path is None and report.save_error == NO_SPACE
    assert report.errors[-1] == f"report not saved: {NO_SPACE}"
    copy = report.backups["copy"]["path"]
    assert Path(copy).is_file() and copy in failure.value.fix
    assert "`state.sqlite3`, `state.sqlite3-wal` and `state.sqlite3-shm`" in failure.value.fix
    assert not list(root.glob("decisions-prune-report-*"))


def test_main_prints_the_failed_proof_its_restore_and_the_whole_unsaved_report(seeded,
                                                                             monkeypatch,
                                                                             capsys):
    """C-17.4: the operator gets the error, the fix naming the copy, and every recovery detail."""
    store, root = seeded
    drifting_readers(monkeypatch)
    unsavable(monkeypatch)
    assert prune_decisions.main(["--state-root", str(root), "--apply",
                                 "--i-understand-this-deletes-decisions"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(
        "subfleet: prune decisions: the store is not what the plan promised: "
        "a reader returns different bytes")
    head = captured.err.split("\n  report: ")[0]
    assert f"\n  report: not saved (saving it failed: {NO_SPACE}); here it is in full:\n" \
        in captured.err
    report = printed_report(captured.err)
    assert report["backups"]["copy"]["path"] in head
    assert Path(report["backups"]["copy"]["path"]).is_file()
    assert Path(report["backups"]["rows"]["path"]).is_file()
    assert report["deleted"] == 5 and report["verification"]["ok"] is False
    # What the restore recipe checks the restored store against.
    assert report["decisions"]["total"] == 12
    assert report["fingerprints"]["before"]["events"]["rows"] > 0
    assert "look in" not in captured.err
    assert len(decision_ids(root)) == 7


def test_a_finished_pass_whose_report_cannot_be_saved_raises_with_the_report(seeded,
                                                                           monkeypatch):
    """Brief: the report is part of what a pass delivers, so losing it is not success."""
    store, root = seeded
    unsavable(monkeypatch)
    with pytest.raises(PruneReportUnsaved) as unsaved:
        prune(root, apply=True, confirm=True, batch_size=2)
    report = unsaved.value.report
    assert unsaved.value.code == 1
    assert isinstance(unsaved.value.__cause__, OSError)
    assert report.deleted == 5 and report.verification["ok"] is True
    assert str(unsaved.value) == (
        "prune decisions: the pass deleted 5 rows in 3 committed batches and every proof "
        f"held, but its report could not be saved: {NO_SPACE}")
    assert report.backups["copy"]["path"] in unsaved.value.fix


def test_main_prints_a_finished_pass_whose_report_cannot_be_saved(seeded, monkeypatch, capsys):
    """C-17.4: stdout still carries the report a finished pass prints; stderr says it is not on disk."""
    store, root = seeded
    unsavable(monkeypatch)
    assert prune_decisions.main(["--state-root", str(root), "--apply",
                                 "--i-understand-this-deletes-decisions", "--json"]) == 1
    captured = capsys.readouterr()
    shown = json.loads(captured.out)
    assert shown["deleted"] == 5 and shown["verification"]["ok"] is True
    assert printed_report(captured.err) == shown
    assert "every proof held, but its report could not be saved" in captured.err
    assert len(decision_ids(root)) == 7


def test_a_dry_run_whose_report_cannot_be_saved_still_prints_its_plan(seeded, monkeypatch,
                                                                      capsys):
    """Brief decision 4: the dry run writes nothing, and a report it cannot save loses no plan."""
    store, root = seeded
    before = snapshot(root)
    unsavable(monkeypatch, lambda: PermissionError(errno.EACCES, "Permission denied"))
    assert prune_decisions.main(["--state-root", str(root)]) == 1
    captured = capsys.readouterr()
    assert "delete           5 rows over" in captured.out
    assert "error            report not saved: PermissionError" in captured.out
    assert ("subfleet: prune decisions: the dry run finished, but its report could not be "
            "saved: PermissionError: [Errno 13] Permission denied") in captured.err
    assert "the dry run deleted nothing" in captured.err
    assert snapshot(root) == before


def test_a_stop_mid_delete_keeps_its_error_when_the_report_cannot_be_saved(seeded,
                                                                         monkeypatch):
    """Brief decision 8: a Ctrl-C in the delete loop, then another at the save, still names the copy."""
    store, root = seeded
    interrupted_at(monkeypatch, batch=2)
    unsavable(monkeypatch, KeyboardInterrupt)
    with pytest.raises(PruneStopped) as stopped:
        prune(root, apply=True, confirm=True, batch_size=2)
    assert str(stopped.value).startswith("prune decisions: the pass stopped on KeyboardInterrupt")
    assert str(stopped.value).endswith("after 2 rows in 1 committed batches")
    assert stopped.value.report.save_error == "KeyboardInterrupt: "
    assert stopped.value.report.backups["copy"]["path"] in stopped.value.fix
    assert len(decision_ids(root)) == 10


def test_a_failed_pass_under_no_report_prints_its_report(seeded, monkeypatch, capsys):
    """Brief: --no-report prints the report rather than saving it, a failed pass's included."""
    store, root = seeded
    drifting_readers(monkeypatch)
    assert prune_decisions.main(["--state-root", str(root), "--apply",
                                 "--i-understand-this-deletes-decisions", "--no-report"]) == 1
    err = capsys.readouterr().err
    assert "\n  report: not saved (--no-report); here it is in full:\n" in err
    report = printed_report(err)
    assert report["backups"]["copy"]["path"] in err.split("\n  report: ")[0]
    assert report["errors"] and not any(error.startswith("report not saved")
                                        for error in report["errors"])


@pytest.mark.parametrize("drift", [False, True])
def test_an_error_closing_the_store_replaces_neither_the_outcome_nor_the_report(
        seeded, monkeypatch, drift):
    """C-3.4: closing and unlocking come after the last write, and the lock is released anyway."""
    store, root = seeded
    closing = Store.close

    def close_then_fail(self):
        closing(self)
        if self is not store:                           # the pass's own, not the fixture's
            raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Store, "close", close_then_fail)
    if drift:
        drifting_readers(monkeypatch)
        with pytest.raises(PruneVerificationError) as failure:
            prune(root, apply=True, confirm=True, batch_size=2)
        report = failure.value.report
    else:
        report = prune(root, apply=True, confirm=True, batch_size=2)
        assert report.verification["ok"] is True
    assert report.errors[-1] == "OperationalError: disk I/O error"
    assert json.loads(Path(report.path).read_text())["errors"] == report.errors
    handle = os.open(root / "daemon.lock", os.O_RDWR)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(handle)


def test_an_error_before_the_plan_says_nothing_was_deleted(seeded, monkeypatch, capsys):
    """C-17.4: an error opening the store is not pointed at a report that does not exist."""
    store, root = seeded

    def unopenable(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(prune_decisions, "Store", unopenable)
    assert prune_decisions.main(["--state-root", str(root), "--apply",
                                 "--i-understand-this-deletes-decisions"]) == 1
    err = capsys.readouterr().err
    assert "subfleet: prune decisions: OperationalError: unable to open database file" in err
    assert "the pass stopped before it planned, so it deleted nothing and has no report" in err
    assert len(decision_ids(root)) == 12
    assert not list(root.glob("decisions-prune-report-*"))


# The pass outcomes the property below drives: two that finish, and four that
# stop at different depths — before any backup, between the two backups, after
# some batches committed, and after every batch committed.
FINISHES = ("proved", "dry-run")
STOPS = ("refused-for-space", "disk-full-at-row-backup", "interrupted", "proof-fails")
CLOCK = "2026-09-29T12:00:00Z"


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """A closed store with rows to delete in three batches of three, copied per example."""
    root = tmp_path_factory.mktemp("template")
    with Store(root / "state.sqlite3") as store:
        for job_id, state in ((TERMINAL, "succeeded"), (MIXED, "failed"), (LIVE, "waiting")):
            seed_job(store, root, job_id, state=state)
        for index in range(7):
            seed_decision(store, TERMINAL, f"waiting {index}")
        for index in range(3):
            seed_decision(store, MIXED, f"mixed {index}")
        for index in range(2):
            seed_decision(store, LIVE, f"still waiting {index}")
    for sidecar in ("state.sqlite3-wal", "state.sqlite3-shm"):
        (root / sidecar).unlink(missing_ok=True)
    return root


def save_failure(kind: str, detail) -> BaseException:
    if kind == "oserror":
        return OSError(detail, os.strerror(detail))
    if kind == "interrupt":
        return KeyboardInterrupt()
    return RuntimeError(detail)


def run_main(root: Path, outcome: str, save: tuple, as_json: bool,
             batch: int) -> tuple[int, str, str]:
    """`main` over `root` with `outcome` and `save` injected, its paths made relative."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(prune_decisions, "utc_now", lambda: CLOCK)
        free = 1 if outcome == "refused-for-space" else 1 << 50
        patch.setattr(prune_decisions.shutil, "disk_usage",
                      lambda path: types.SimpleNamespace(total=1 << 50, used=0, free=free))
        patch.setattr(prune_decisions, "prune",
                      functools.partial(prune_decisions.prune, batch_size=3))
        if outcome == "proof-fails":
            drifting_readers(patch)
        elif outcome == "interrupted":
            interrupted_at(patch, batch)
        elif outcome == "disk-full-at-row-backup":
            def full(*args, **kwargs):
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            patch.setattr(prune_decisions, "_backup_rows", full)
        if save[0] not in ("saved", "no-report"):
            unsavable(patch, lambda: save_failure(*save))
        argv = ["--state-root", str(root)]
        if outcome != "dry-run":
            argv += ["--apply", "--i-understand-this-deletes-decisions"]
        if as_json:
            argv.append("--json")
        if save[0] == "no-report":
            argv.append("--no-report")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = prune_decisions.main(argv)
    return (code, out.getvalue().replace(str(root), "<root>"),
            err.getvalue().replace(str(root), "<root>"))


@settings(max_examples=30, deadline=None)
@given(outcome=st.sampled_from(FINISHES + STOPS),
       save=st.one_of(st.just(("saved", None)), st.just(("no-report", None)),
                      st.tuples(st.just("oserror"),
                                st.sampled_from([errno.ENOSPC, errno.EACCES, errno.EROFS,
                                                 errno.EIO, errno.EDQUOT])),
                      st.just(("interrupt", None)),
                      st.tuples(st.just("runtime"), st.text(max_size=12))),
       as_json=st.booleans(), batch=st.integers(1, 3))
def test_a_report_that_cannot_be_saved_changes_nothing_but_where_it_is(template, outcome, save,
                                                                     as_json, batch):
    """Brief decision 8, differential: the same pass with a working save is the oracle."""
    with tempfile.TemporaryDirectory(prefix="subfleet-prune-") as directory:
        control, subject = Path(directory).resolve() / "control", Path(directory).resolve() / "s"
        shutil.copytree(template, control)
        shutil.copytree(template, subject)
        c_code, c_out, c_err = run_main(control, outcome, ("saved", None), as_json, batch)
        s_code, s_out, s_err = run_main(subject, outcome, save, as_json, batch)

        saved, = control.glob("decisions-prune-report-*.json")
        oracle = json.loads(saved.read_text().replace(str(control), "<root>"))
        stopped = outcome in STOPS
        unsaved = save[0] not in ("saved", "no-report")
        failure = save_failure(*save) if unsaved else None
        save_error = f"{type(failure).__name__}: {failure}"
        errors = oracle["errors"] + ([f"report not saved: {save_error}"] if unsaved else [])

        # The exit code, the error and the fix are the pass's own; a pass that
        # finished and lost only its report says so instead of exiting 0.
        head = s_err.split("\n  report: ")[0]
        assert s_code == (1 if unsaved and not stopped else c_code)
        if stopped or not unsaved:
            assert head == c_err.split("\n  report: ")[0]
        else:
            assert c_err == "" and head.startswith("subfleet: prune decisions: the ")
            assert f"but its report could not be saved: {save_error}\n  fix: " in head
        # The report is on disk or printed in full, and is the oracle's but for the save's error.
        if save[0] == "saved":
            mine, = subject.glob("decisions-prune-report-*.json")
            assert json.loads(mine.read_text().replace(str(subject), "<root>")) == oracle
        else:
            assert not list(subject.glob("decisions-prune-report-*"))
            if stopped or unsaved:
                assert printed_report(s_err) == {**oracle, "errors": errors}
        # A finished pass prints what it prints with a working save, the report's line aside.
        if stopped:
            assert s_out == c_out == ""
        elif as_json:
            assert json.loads(s_out) == {**json.loads(c_out), "errors": errors}
        else:
            where = ("mode ", "report ", "error ")
            assert [line for line in s_out.splitlines() if not line.startswith(where)] == \
                [line for line in c_out.splitlines() if not line.startswith(where)]
            assert [line for line in s_out.splitlines() if line.startswith("error ")] == \
                [f"{'error':<16} {error}" for error in errors]
        # A copy the pass took is on disk, and any fix names it.
        copy = oracle["backups"].get("copy", {}).get("path")
        if copy:
            assert Path(copy.replace("<root>", str(subject))).is_file()
            assert copy in head or head == ""
        # And the store ends the same either way.
        assert rows(subject, "decisions", "decision_id") == rows(control, "decisions", "decision_id")
