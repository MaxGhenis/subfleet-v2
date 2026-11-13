"""D-STORE-FREE1: a compacted store preserves C-3.2 and all stored content.

All databases and child processes belong to the test's scratch directory. The
independent fingerprints below intentionally do not call the pass's verifier.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import compact_store
from subfleet.store import SCHEMA_VERSION, Store

_REAL_OPEN_HOLDERS = compact_store._open_holders


@pytest.fixture(autouse=True)
def _fast_holder_inspection(request, monkeypatch, tmp_path):
    """Keep independent SQLite proofs separate from slow kernel process scans."""
    if request.node.get_closest_marker("real_lsof") is None:
        monkeypatch.setattr(compact_store, "_open_holders", lambda _database: None)
    yield
    shutil.rmtree(tmp_path, ignore_errors=True)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _cell(value) -> bytes:
    if value is None:
        return b"null"
    if isinstance(value, bytes):
        return b"blob:" + value.hex().encode()
    if isinstance(value, str):
        return b"text:" + value.encode("utf-8")
    if isinstance(value, int):
        return b"int:" + str(value).encode()
    return b"real:" + struct.pack(">d", value)


def _contents(database: Path) -> dict[str, tuple[int, str]]:
    """Digest every value in primary-key order, with storage-class boundaries."""
    conn = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")]
        result = {}
        for table in tables:
            info = conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
            primary = [column[1] for column in sorted(info, key=lambda column: column[5])
                       if column[5]]
            order = primary or [column[1] for column in info]
            digest, count = hashlib.sha256(), 0
            for row in conn.execute(f"SELECT * FROM {_quote(table)} ORDER BY "
                                    + ",".join(_quote(column) for column in order)):
                count += 1
                for value in row:
                    cell = _cell(value)
                    digest.update(len(cell).to_bytes(8, "big"))
                    digest.update(cell)
                digest.update(b"\xffrow\xff")
            result[table] = (count, digest.hexdigest())
        return result
    finally:
        conn.close()


def _tree(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def _seed(root: Path, *, decisions: int = 48, delete_mod: int = 2) -> Path:
    """Retained events plus large deleted decision/job/attempt histories."""
    root.mkdir(parents=True, exist_ok=True)
    database = root / "state.sqlite3"
    with Store(database) as store:
        conn = store.connection
        conn.execute("BEGIN")
        conn.execute("INSERT INTO lanes (lane_id,provider,account_key,credential_ref,"
                     "credential_kind,created_at,updated_at) VALUES "
                     "('lane','codex','account','/synthetic','home','t','t')")
        conn.execute("INSERT INTO readings (lane_id,scope,window,utilization,label,source,observed_at) "
                     "VALUES ('lane','test','five_hour',0.375,'provider','synthetic','t')")
        for branch in ("a", "z"):
            conn.execute("INSERT INTO job_owned_branches (remote_key,branch_key,branch,remote,"
                         "family_job_id,job_id,claimed_at) VALUES ('remote',?,?,"
                         "'synthetic','family','job','t')", (branch, branch))
        for n in range(decisions):
            job = f"job-{n:04}"
            conn.execute("INSERT INTO jobs (job_id,request_id,payload_digest,kind,state,"
                         "workdir,prompt_path,sandbox,created_at) "
                         "VALUES (?,?,?,'dispatch','succeeded',?,'/prompt','read-only','t')",
                         (job, job, "digest", str(root)))
            conn.execute("INSERT INTO attempts (attempt_id,job_id,seq,lane_id,model_requested,"
                         "state,reserved_at) VALUES (?,?,1,'lane','test','succeeded','t')",
                         (job + "/a1", job))
            conn.execute("INSERT INTO decisions (job_id,attempt_id,evaluated_at,policy_hash,decision_json)"
                         " VALUES (?,?,'t','policy',?)", (job, job + "/a1", "d" * 16384))
            conn.execute("INSERT INTO events (ts,kind,job_id,data_json) VALUES "
                         "('t',?,?,?)", ("probe.state" if n % 3 == 0 else "audit.test", job,
                                        json.dumps({"holder": str(n), "padding": "e" * 1024})))
        for n in range(decisions):
            if n % delete_mod == 0:
                job = f"job-{n:04}"
                conn.execute("DELETE FROM decisions WHERE job_id=?", (job,))
                conn.execute("DELETE FROM attempts WHERE job_id=?", (job,))
                conn.execute("DELETE FROM jobs WHERE job_id=?", (job,))
        conn.execute("INSERT INTO service_notices (session_id,text,state,created_at)"
                     " VALUES ('synthetic',?,'pending','t')", ("nul\x00unicode \u2603",))
        conn.execute("COMMIT")
    return database


def _pragma(database: Path, pragma: str):
    conn = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        return conn.execute(f"PRAGMA {pragma}").fetchall()
    finally:
        conn.close()


def _indexes(database: Path) -> dict[str, list[tuple]]:
    """Force every named index to answer a complete scan of its table."""
    conn = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        indexes = conn.execute("SELECT name,tbl_name,sql FROM sqlite_schema "
                               "WHERE type='index' ORDER BY name").fetchall()
        result = {}
        for name, table, sql in indexes:
            # A partial index is usable only with its predicate. Its DDL uses
            # one WHERE and no quoted occurrence of that word in this schema.
            where = " WHERE " + sql.split("WHERE", 1)[1] if sql and "WHERE" in sql else ""
            rows = conn.execute(f"SELECT * FROM {_quote(table)} INDEXED BY {_quote(name)}"
                                + where).fetchall()
            result[name] = sorted(rows, key=repr)
        return result
    finally:
        conn.close()


def test_compact_preserves_every_table_and_index_and_enables_incremental(tmp_path):
    """Content/Shrinks: every table, index and audit row survives the pass."""
    database = _seed(tmp_path)
    before, indexes, old_size = _contents(database), _indexes(database), database.stat().st_size
    report = compact_store.compact(tmp_path, apply=True)
    assert _contents(database) == before
    assert _indexes(database) == indexes
    assert _pragma(database, "integrity_check") == [("ok",)]
    assert _pragma(database, "auto_vacuum") == [(2,)]
    assert database.stat().st_size < old_size
    assert _pragma(database, "freelist_count") == [(0,)]
    assert database.stat().st_size == _pragma(database, "page_count")[0][0] * report.page_size
    assert sorted(path.name for path in tmp_path.iterdir()) == ["state.sqlite3"]


def test_dry_run_reports_measurements_in_required_order_without_any_write(tmp_path, capsys):
    database = _seed(tmp_path)
    before = _tree(tmp_path)
    assert compact_store.main(["--state-root", str(tmp_path)]) == 0
    output = capsys.readouterr().out.lower()
    labels = ["page count", "freelist count", "live bytes", "return bytes", "needed bytes", "free bytes"]
    offsets = [output.index(label) for label in labels]
    assert offsets == sorted(offsets)
    assert _tree(tmp_path) == before
    report = compact_store.compact(tmp_path)
    page_count = _pragma(database, "page_count")[0][0]
    free_pages = _pragma(database, "freelist_count")[0][0]
    assert report.page_count == page_count
    assert report.freelist_count == free_pages
    assert report.live_bytes == (page_count - free_pages) * report.page_size
    assert report.reclaimable_bytes == free_pages * report.page_size
    assert report.needed_bytes > report.live_bytes
    assert report.free_bytes >= report.needed_bytes
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("apply", [False, True])
def test_daemon_lock_refusal_changes_no_byte(tmp_path, apply):
    """Refusal: the daemon's held single-instance lock excludes the pass."""
    _seed(tmp_path)
    with (tmp_path / "daemon.lock").open("w+b") as lock:
        lock.write(b"synthetic daemon\n")
        lock.flush()
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = _tree(tmp_path)
        with pytest.raises(compact_store.CompactRefused, match="daemon|lock"):
            compact_store.compact(tmp_path, apply=apply)
        assert _tree(tmp_path) == before


@contextlib.contextmanager
def _holder(database: Path, *, transaction: bool = False):
    child = subprocess.Popen(
        [sys.executable, "-c", "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
         + ("c.execute('BEGIN EXCLUSIVE'); " if transaction else "")
         + "print('ready',flush=True); sys.stdin.readline(); c.close()", str(database)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        yield child
    finally:
        child.communicate("done\n", timeout=10)
        assert child.returncode == 0, child.stderr


@pytest.mark.real_lsof
@pytest.mark.parametrize("transaction", [False, True])
def test_any_other_holder_refusal_changes_no_byte(tmp_path, transaction):
    """Refusal includes an idle open connection without a SQLite transaction."""
    database = _seed(tmp_path)
    with _holder(database, transaction=transaction):
        before = _tree(tmp_path)
        with pytest.raises(compact_store.CompactRefused, match="open|holder|process|busy|lock"):
            compact_store.compact(tmp_path, apply=True)
        assert _tree(tmp_path) == before


@pytest.mark.parametrize("version", [SCHEMA_VERSION - 1, SCHEMA_VERSION + 1])
@pytest.mark.parametrize("apply", [False, True])
def test_schema_skew_refusal_changes_no_byte(tmp_path, version, apply):
    """Refusal checks both older and newer versions without opening Store."""
    database = _seed(tmp_path)
    with contextlib.closing(sqlite3.connect(database)) as conn, conn:
        conn.execute("UPDATE schema_version SET version=?", (version,))
    before = _tree(tmp_path)
    with pytest.raises(compact_store.CompactRefused, match="schema"):
        compact_store.compact(tmp_path, apply=apply)
    assert _tree(tmp_path) == before


def test_schema_structure_skew_refusal_changes_no_byte(tmp_path):
    database = _seed(tmp_path)
    with contextlib.closing(sqlite3.connect(database)) as conn, conn:
        conn.execute("DROP INDEX events_kind")
    before = _tree(tmp_path)
    with pytest.raises(compact_store.CompactRefused, match="schema|index"):
        compact_store.compact(tmp_path, apply=True)
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("change", ["check", "expression-index"])
def test_schema_expression_skew_refuses_without_any_write(tmp_path, change):
    """Version and column metadata alone do not prove the schema matches."""
    database = _seed(tmp_path)
    with contextlib.closing(sqlite3.connect(database)) as conn, conn:
        if change == "check":
            conn.execute("PRAGMA writable_schema=ON")
            conn.execute("UPDATE sqlite_schema SET sql=replace(sql,?,?) WHERE name='jobs'",
                         ("'utility','default'", "'utility','default','different'"))
        else:
            conn.execute("DROP INDEX events_probe_holder")
            conn.execute("CREATE INDEX events_probe_holder "
                         "ON events(json_extract(data_json,'$.different'),event_id DESC) "
                         "WHERE kind='probe.state' AND json_valid(data_json)")
    before = _tree(tmp_path)
    with pytest.raises(compact_store.CompactRefused, match="schema|index|check"):
        compact_store.compact(tmp_path, apply=True)
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("ddl", [
    "CREATE TABLE extra (id INTEGER PRIMARY KEY, value TEXT)",
    "CREATE VIEW extra AS SELECT job_id FROM jobs",
    "CREATE TRIGGER extra AFTER INSERT ON leases BEGIN SELECT 1; END",
])
@pytest.mark.parametrize("apply", [False, True])
def test_extra_schema_object_refuses_without_any_write(tmp_path, ddl, apply):
    database = _seed(tmp_path)
    with contextlib.closing(sqlite3.connect(database)) as conn, conn:
        conn.execute(ddl)
    before = _tree(tmp_path)
    with pytest.raises(compact_store.CompactRefused, match="schema|object"):
        compact_store.compact(tmp_path, apply=apply)
    assert _tree(tmp_path) == before


def test_current_schema_reached_by_migration_can_be_compacted(tmp_path):
    """ALTER TABLE adds columns in a different physical order than new stores."""
    from test_store_migration_2 import _schema_before_migration

    database = tmp_path / "state.sqlite3"
    with contextlib.closing(sqlite3.connect(database)) as conn, conn:
        _schema_before_migration(conn, SCHEMA_VERSION)
    with Store(database):
        pass
    before = _contents(database)
    compact_store.compact(tmp_path, apply=True)
    assert _contents(database) == before
    assert _pragma(database, "auto_vacuum") == [(2,)]


@pytest.mark.parametrize("apply", [False, True])
def test_low_space_refusal_changes_no_byte(tmp_path, monkeypatch, apply):
    """Refusal: the need includes live pages, margin and the old WAL."""
    _seed(tmp_path)
    before = _tree(tmp_path)
    monkeypatch.setattr(compact_store.shutil, "disk_usage", lambda _path: shutil._ntuple_diskusage(1, 1, 0))
    with pytest.raises(compact_store.CompactRefused, match="space|free|need"):
        compact_store.compact(tmp_path, apply=apply)
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("failure", ["missing", "timeout", "stderr", "exit"])
def test_holder_inspection_failure_refuses_without_changes(tmp_path, monkeypatch, failure):
    """Ownership must be established; failed inspection cannot mean idle."""
    database = _seed(tmp_path)
    before = _tree(tmp_path)

    def failed(*args, **kwargs):
        if failure == "missing":
            raise FileNotFoundError("test-owned missing lsof")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("lsof", 180)
        return subprocess.CompletedProcess([], 2 if failure == "exit" else 1,
                                           stdout="", stderr="inspection warning" if failure == "stderr" else "")

    monkeypatch.setattr(compact_store.subprocess, "run", failed)
    with pytest.raises(compact_store.CompactRefused, match="inspect|ownership|holder"):
        _REAL_OPEN_HOLDERS(database)
    assert _tree(tmp_path) == before


def test_named_original_is_retained_and_never_overwritten(tmp_path):
    database = _seed(tmp_path)
    before = _contents(database)
    kept = tmp_path / "original.sqlite3"
    compact_store.compact(tmp_path, apply=True, keep_original=kept)
    assert _contents(kept) == before == _contents(database)
    assert _pragma(kept, "auto_vacuum") == [(0,)]
    assert _pragma(database, "auto_vacuum") == [(2,)]
    tree = _tree(tmp_path)
    with pytest.raises(compact_store.CompactRefused, match="exist|original|overwrite"):
        compact_store.compact(tmp_path, apply=True, keep_original=kept)
    assert _tree(tmp_path) == tree


def test_temp_database_is_fsynced_before_rename(tmp_path, monkeypatch):
    """Atomic: a rename cannot publish a new file that was never fsynced."""
    database = _seed(tmp_path)
    fsynced: set[tuple[int, int]] = set()
    fsync, replace = os.fsync, os.replace
    renamed = []

    def sync(fd):
        stat = os.fstat(fd)
        fsynced.add((stat.st_dev, stat.st_ino))
        return fsync(fd)

    def publish(source, destination, *args, **kwargs):
        if Path(destination) == database:
            stat = Path(source).stat()
            assert (stat.st_dev, stat.st_ino) in fsynced, "published before fsync"
            renamed.append(True)
        return replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(compact_store.os, "fsync", sync)
    monkeypatch.setattr(compact_store.os, "replace", publish)
    compact_store.compact(tmp_path, apply=True)
    assert renamed


@pytest.mark.parametrize("stage", compact_store.SWAP_STAGES)
def test_every_swap_crash_leaves_complete_readable_content(tmp_path, stage):
    """Atomic: abruptly exit at each publication boundary, without cleanup."""
    database = _seed(tmp_path)
    before = _contents(database)
    code = ("import os,sys; from pathlib import Path; from subfleet.compact_store import compact; "
            "import subfleet.compact_store as m; m._open_holders=lambda database: None; "
            "compact(Path(sys.argv[1]),apply=True,"
            "fault=lambda stage: os._exit(73) if stage==sys.argv[2] else None)")
    code = code.replace("apply=True,", "apply=True,keep_original=Path(sys.argv[1])/'original.sqlite3',")
    child = subprocess.run([sys.executable, "-c", code, str(tmp_path), stage],
                           capture_output=True, text=True, timeout=60)
    assert child.returncode == 73, child.stderr
    assert database.is_file()
    assert _contents(database) == before
    assert _pragma(database, "integrity_check") == [("ok",)]
    assert _pragma(database, "auto_vacuum") in ([(0,)], [(2,)])
    # A later successful operator pass removes abandoned outputs rather than
    # requiring an install kit to guess which hidden file is authoritative.
    compact_store.compact(tmp_path, apply=True)
    assert _contents(database) == before
    assert not list(tmp_path.glob(".state-compact-*"))


@pytest.mark.real_lsof
@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
def test_arbitrary_open_file_descriptor_refuses_without_changes(tmp_path, suffix):
    """Refusal covers non-SQLite holders and either sidecar, too."""
    database = _seed(tmp_path)
    target = database.with_name(database.name + suffix)
    if suffix:
        target.write_bytes(b"test-owned sidecar holder\n")
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; f=open(sys.argv[1],'rb'); "
         "print('ready',flush=True); sys.stdin.readline(); f.close()", str(target)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        before = _tree(tmp_path)
        with pytest.raises(compact_store.CompactRefused, match="open|holder|process"):
            compact_store.compact(tmp_path, apply=True)
        assert _tree(tmp_path) == before
    finally:
        _, stderr = child.communicate("done\n", timeout=10)
        assert child.returncode == 0, stderr


def _hot_wal(database: Path, statement: str, parameters: tuple = ()) -> None:
    """Leave a fully committed WAL, as a terminated synthetic writer would."""
    code = ("import os,sqlite3,sys,json; c=sqlite3.connect(sys.argv[1]); "
            "c.execute(sys.argv[2],json.loads(sys.argv[3])); c.commit(); os._exit(0)")
    subprocess.run([sys.executable, "-c", code, str(database), statement, json.dumps(parameters)],
                   check=True, capture_output=True, text=True, timeout=30)
    assert database.with_name(database.name + "-wal").stat().st_size > 32


@pytest.mark.parametrize("apply", [False, True])
def test_schema_skew_only_in_wal_refuses_without_touching_sidecars(tmp_path, apply):
    """Refusal reads the latest committed schema, without SQLite SHM writes."""
    database = _seed(tmp_path)
    _hot_wal(database, "UPDATE schema_version SET version=?", (SCHEMA_VERSION + 1,))
    before = _tree(tmp_path)
    with pytest.raises(compact_store.CompactRefused, match="schema"):
        compact_store.compact(tmp_path, apply=apply)
    assert _tree(tmp_path) == before


def test_low_space_with_hot_wal_changes_no_byte(tmp_path, monkeypatch):
    database = _seed(tmp_path)
    _hot_wal(database, "INSERT INTO service_notices (session_id,text,state,created_at) "
             "VALUES ('hot','committed WAL','pending','t')")
    before = _tree(tmp_path)
    monkeypatch.setattr(compact_store.shutil, "disk_usage", lambda _path: shutil._ntuple_diskusage(1, 1, 0))
    with pytest.raises(compact_store.CompactRefused, match="space|free|need"):
        compact_store.compact(tmp_path, apply=True)
    assert _tree(tmp_path) == before


def test_committed_wal_is_folded_before_publication(tmp_path):
    database = _seed(tmp_path)
    _hot_wal(database, "INSERT INTO service_notices (session_id,text,state,created_at) "
             "VALUES ('hot','committed WAL','pending','t')")
    compact_store.compact(tmp_path, apply=True)
    with contextlib.closing(sqlite3.connect(database.as_uri() + "?immutable=1", uri=True)) as conn:
        assert conn.execute("SELECT text FROM service_notices WHERE session_id='hot'").fetchall() == [
            ("committed WAL",)]
    assert _pragma(database, "integrity_check") == [("ok",)]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["state.sqlite3"]


def test_digest_mismatch_refuses_swap_even_when_counts_match(tmp_path, monkeypatch):
    """Content: valid SQLite with one changed value must fail verification."""
    database = _seed(tmp_path)
    before = _contents(database)
    original = compact_store._fingerprints
    calls = 0

    def unequal(conn):
        nonlocal calls
        result = original(conn)
        calls += 1
        if calls > 1:
            result["events"] = dict(result["events"], sha256="0" * 64)
        return result

    monkeypatch.setattr(compact_store, "_fingerprints", unequal)
    with pytest.raises(compact_store.CompactVerificationError, match="digest|content|fingerprint|differ"):
        compact_store.compact(tmp_path, apply=True)
    assert _contents(database) == before
    assert _pragma(database, "auto_vacuum") == [(0,)]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["state.sqlite3"]


def test_valid_output_with_changed_value_is_never_published(tmp_path):
    """A valid, same-count copy still fails when a single audit value differs."""
    database = _seed(tmp_path)
    before = _contents(database)

    def change_copy(stage):
        if stage == "built":
            [temporary] = tmp_path.glob(".state-compact-*.sqlite3")
            with contextlib.closing(sqlite3.connect(temporary)) as conn, conn:
                conn.execute("UPDATE events SET ts='changed-copy' WHERE event_id="
                             "(SELECT MIN(event_id) FROM events)")
                assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]

    with pytest.raises(compact_store.CompactVerificationError, match="digest|content|differ"):
        compact_store.compact(tmp_path, apply=True, fault=change_copy)
    assert _contents(database) == before
    assert _pragma(database, "auto_vacuum") == [(0,)]


def test_valid_output_with_missing_index_is_never_published(tmp_path):
    """Content includes index answers, which table digests cannot establish."""
    database = _seed(tmp_path)
    before, indexes = _contents(database), _indexes(database)

    def change_copy(stage):
        if stage == "built":
            [temporary] = tmp_path.glob(".state-compact-*.sqlite3")
            with contextlib.closing(sqlite3.connect(temporary)) as conn, conn:
                conn.execute("DROP INDEX events_kind")
                assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]

    with pytest.raises(compact_store.CompactVerificationError, match="schema|index"):
        compact_store.compact(tmp_path, apply=True, fault=change_copy)
    assert _contents(database) == before
    assert _indexes(database) == indexes
    assert _pragma(database, "auto_vacuum") == [(0,)]


@settings(max_examples=16, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(payloads=st.lists(st.one_of(st.none(), st.text(max_size=80), st.binary(max_size=80),
                                 st.integers(-(2 ** 63), 2 ** 63 - 1)), min_size=1, max_size=24),
       deletes=st.sets(st.integers(0, 63), min_size=16, max_size=48))
def test_generated_contents_and_delete_sets_compact_without_changing_rows(tmp_path, payloads, deletes):
    """Hypothesis: random values/deletes retain exact table digests and shrink."""
    root = Path(tempfile.mkdtemp(prefix="compact-property-", dir=tmp_path))
    try:
        database = _seed(root, decisions=64, delete_mod=3)
        with contextlib.closing(sqlite3.connect(database)) as conn, conn:
            for n, payload in enumerate(payloads):
                conn.execute("INSERT INTO leases (lease_key,holder,acquired_at,expires_at)"
                             " VALUES (?,?,'t',?)", (f"key-{n}", "holder", payload))
            for n in deletes:
                job = f"job-{n:04}"
                conn.execute("DELETE FROM decisions WHERE job_id=?", (job,))
                conn.execute("DELETE FROM attempts WHERE job_id=?", (job,))
                conn.execute("DELETE FROM jobs WHERE job_id=?", (job,))
        before, old_size = _contents(database), database.stat().st_size
        compact_store.compact(root, apply=True)
        assert _contents(database) == before
        assert _pragma(database, "integrity_check") == [("ok",)]
        assert _pragma(database, "auto_vacuum") == [(2,)]
        assert database.stat().st_size < old_size
    finally:
        shutil.rmtree(root)
