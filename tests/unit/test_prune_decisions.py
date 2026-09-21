"""The one-off `decisions` prune: what it keeps, what it proves, what it refuses.

Every store here is a synthetic one built in a temp directory; nothing in this
file reads `~/.subfleet`. Every test names the clause it proves (C-20.5).
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import sqlite3
import types
from pathlib import Path

import pytest

from subfleet import prune_decisions
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.offline import Offline
from subfleet.prune_decisions import PruneRefused, PruneVerificationError, prune
from subfleet.store import SchemaVersionError, Store

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

def test_the_dry_run_is_the_default_and_writes_nothing_but_its_report(seeded):
    """Brief: the default pass plans in full and mutates nothing."""
    store, root = seeded
    before = snapshot(root)
    files = sorted(path.name for path in root.iterdir())
    report = prune(root, write_report=False)
    assert report.dry_run is True
    assert report.batches == 0 and report.deleted == 0
    assert snapshot(root) == before
    assert sorted(path.name for path in root.iterdir()) == files
    assert not (root / "backups").exists()
    saved = prune(root)
    assert Path(saved.path).name.startswith("decisions-prune-report-")
    assert sorted(path.name for path in root.iterdir()) == sorted(files + [Path(saved.path).name])
    assert snapshot(root) == before


def test_the_dry_run_counts_what_a_real_pass_deletes(seeded):
    """Brief: the dry run's numbers are the pass's, not an estimate of it."""
    store, root = seeded
    dry = prune(root, write_report=False)
    real = applied(root, batch_size=2)
    assert real.decisions == dry.decisions
    assert real.deleted == dry.decisions["delete"]
    assert len(decision_ids(root)) == dry.decisions["keep"]


def test_apply_without_the_confirmation_is_refused(seeded):
    """Brief: writing takes --apply and --i-understand-this-deletes-decisions together."""
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


def test_the_pass_holds_the_lock_for_its_whole_length(seeded):
    """C-3.4: a check released before the work leaves a window for a second writer."""
    store, root = seeded
    held: list[bool] = []
    original = prune_decisions._backup_rows

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

    prune_decisions._backup_rows = probe
    try:
        applied(root, batch_size=2)
    finally:
        prune_decisions._backup_rows = original
    assert held == [True]


def test_a_volume_without_room_for_the_backups_is_refused_with_the_numbers(seeded, monkeypatch):
    """Brief: the backups come first, so the pass refuses before it deletes anything."""
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


def test_a_backup_that_is_already_there_is_refused_rather_than_overwritten(seeded):
    """Brief: the evidence of an earlier pass is never written over."""
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


def test_a_schema_newer_than_this_build_is_refused(seeded):
    """C-3.5: the pass opens the store through Store, so it inherits that refusal."""
    store, root = seeded
    store.connection.execute("INSERT INTO schema_version VALUES (?,?)",
                             (99, "2026-09-20T10:00:00Z"))
    with pytest.raises(SchemaVersionError):
        prune(root, write_report=False)
    assert len(decision_ids(root)) == 12


def test_a_missing_store_is_refused(tmp_path):
    """Brief: the pass names what it could not find instead of creating it."""
    with pytest.raises(PruneRefused) as refusal:
        prune(tmp_path, write_report=False)
    assert refusal.value.code == 7
    assert not (tmp_path / "state.sqlite3").exists()


# --- the proofs ----------------------------------------------------------------

def test_every_other_table_is_byte_identical_after_the_pass(seeded):
    """Brief: jobs, attempts, artifacts, notices, leases and the rest are untouched."""
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
    """Brief: every deleted row is recoverable, and the report's digest names the file."""
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
    """docs/migration.md: `VACUUM INTO` a copy, then `PRAGMA integrity_check` on it."""
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
    """Brief: a proof that does not hold exits non-zero and names the copy to restore from."""
    store, root = seeded
    original = prune_decisions._reader_bytes
    calls: list[int] = []

    def drifting(conn, jobs):
        """The second reading — the one taken after the delete — comes back changed."""
        served = original(conn, jobs)
        calls.append(len(calls))
        if len(calls) > 1:
            return {job: dict(value, why="other bytes") for job, value in served.items()}
        return served

    monkeypatch.setattr(prune_decisions, "_reader_bytes", drifting)
    with pytest.raises(PruneVerificationError) as failure:
        prune(root, apply=True, confirm=True, batch_size=2)
    assert failure.value.code == 1
    assert "restore from the copy" in failure.value.fix
    assert failure.value.report.verification["ok"] is False
    assert failure.value.report.errors
    saved = json.loads(Path(failure.value.report.path).read_text())
    assert saved["verification"]["ok"] is False
    assert saved["backups"]["copy"]["path"].endswith(".sqlite3")


# --- idempotence and space -----------------------------------------------------

def test_a_second_apply_changes_nothing_and_writes_no_backup(seeded):
    """Brief: the keep set is everything that is left, so the pass has nothing to do."""
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
