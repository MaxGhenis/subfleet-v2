"""Round-3 backup verification inputs; every database is disposable."""

import contextlib
import gzip
import json
from pathlib import Path
import sqlite3
import struct
from urllib.parse import unquote, urlsplit

import pytest

from subfleet import prune_decisions as pruning
from subfleet.store import Store


def seed(root):
    with Store(root / "state.sqlite3") as store:
        store.add_job(job_id="terminal", request_id="terminal", payload_digest="digest",
                      kind="dispatch", workdir=str(root), prompt_path="/synthetic/prompt",
                      sandbox="read-only", state="succeeded")
        for index in range(4):
            store.add_decision("terminal", {"policy_hash": "hash", "note": str(index)})


def read_cell(value):
    # Independent comparison of Python SQLite reads, including signed zero.
    if isinstance(value, float):
        return (float, struct.pack(">d", value))
    return (type(value), value)


def signature(database, connect, surrogate=False):
    with contextlib.closing(connect(database)) as conn:
        if surrogate:
            conn.text_factory = lambda raw: raw.decode("utf-8", "surrogatepass")
        tables = [name for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        result = {}
        for name in tables:
            cursor = conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid')
            columns = [entry[0] for entry in cursor.description]
            result[name] = [tuple(zip(columns, map(read_cell, row))) for row in cursor]
        return result


CASES = [
    pytest.param('payload', (1,), 'UPDATE review_probe SET payload=?', (1.0,),
                 False, id="integer-v-real"),
    pytest.param('payload', ("1",), 'UPDATE review_probe SET payload=?', (1,),
                 False, id="text-v-integer"),
    pytest.param('payload', (-0.0,), 'UPDATE review_probe SET payload=?', (0.0,),
                 False, id="negative-v-positive-zero"),
    pytest.param('payload', (2**53 + 1,), 'UPDATE review_probe SET payload=?', (2**53 + 2,),
                 False, id="integers-beyond-double-precision"),
    pytest.param('"INTEGER"', ("1",), 'UPDATE review_probe SET "INTEGER"=?', (1,),
                 False, id="column-named-storage-tag"),
    pytest.param('payload', ("a\0b",), 'UPDATE review_probe SET payload=?', ("a\0c",),
                 False, id="text-nul-suffix"),
    pytest.param('payload', None,
                 "UPDATE review_probe SET payload=CAST(X'EDA081' AS TEXT)", (),
                 True, id="lone-surrogate"),
    pytest.param('payload', None,
                 "UPDATE review_probe SET payload=CAST(X'F0908080' AS TEXT)", (),
                 True, id="surrogate-pair-v-non-bmp"),
    pytest.param('payload', ("\u00e9",), 'UPDATE review_probe SET payload=?', ("e\u0301",),
                 False, id="nfc-v-nfd"),
    pytest.param('"left", "right"', ("alpha", "beta"),
                 'UPDATE review_probe SET "left"=?, "right"=?', ("beta", "alpha"),
                 False, id="two-column-values-swap"),
    pytest.param('"left", "right"', ("alpha", "beta"),
                 'ALTER TABLE review_probe RENAME COLUMN "left" TO "temporary";'
                 'ALTER TABLE review_probe RENAME COLUMN "right" TO "left";'
                 'ALTER TABLE review_probe RENAME COLUMN "temporary" TO "right"', (),
                 False, id="two-column-names-swap"),
    pytest.param('payload', ("blob:00ff",), 'UPDATE review_probe SET payload=?', (b"\0\xff",),
                 False, id="text-to-blob-round2"),
    pytest.param('payload', (b"\0\xff",), 'UPDATE review_probe SET payload=?', ("blob:00ff",),
                 False, id="blob-to-text-round2"),
]


@pytest.mark.parametrize("columns,values,sql,params,surrogate", CASES)
def test_distinct_backup_row_is_refused_before_any_delete(
        tmp_path, monkeypatch, request, columns, values, sql, params, surrogate):
    seed(tmp_path)
    connect = sqlite3.connect
    with contextlib.closing(connect(tmp_path / "state.sqlite3")) as conn:
        conn.execute(f"CREATE TABLE review_probe ({columns})")
        if values is None:
            bits = "EDA080EDB080" if "pair" in request.node.callspec.id else "EDA080"
            conn.execute(f"INSERT INTO review_probe VALUES (CAST(X'{bits}' AS TEXT))")
        else:
            conn.execute(f"INSERT INTO review_probe VALUES ({','.join('?' for _ in values)})",
                         values)
        conn.commit()
    before = signature(tmp_path / "state.sqlite3", connect, surrogate)
    altered = []
    live_sql = []

    def damage(database, *args, **kwargs):
        if "/backups/state-" in str(database):
            path = Path(unquote(urlsplit(str(database)).path))
            with contextlib.closing(connect(path)) as backup:
                if len(sql.split(";")) > 1:
                    backup.executescript(sql)
                else:
                    backup.execute(sql, params)
                backup.commit()
                assert backup.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            copied = signature(path, connect, surrogate)
            assert {name: len(rows) for name, rows in copied.items()} == {
                name: len(rows) for name, rows in before.items()}
            assert copied["review_probe"] != before["review_probe"]
            altered.append(path)
        conn = connect(database, *args, **kwargs)
        if surrogate:
            conn.text_factory = lambda raw: raw.decode("utf-8", "surrogatepass")
        if "/backups/" not in str(database) and "state.sqlite3" in str(database):
            conn.set_trace_callback(live_sql.append)
        return conn

    monkeypatch.setattr(pruning.sqlite3, "connect", damage)
    with pytest.raises(pruning.PruneVerificationError, match="snapshot.*review_probe") as stopped:
        pruning.prune(tmp_path, apply=True, confirm=True)
    assert len(altered) == 1  # The actual whole-copy verification was reached.
    assert stopped.value.report.deleted == stopped.value.report.batches == 0
    assert not any(sql.lstrip().upper().startswith("DELETE") for sql in live_sql)
    assert signature(tmp_path / "state.sqlite3", connect, surrogate) == before


@pytest.mark.parametrize("affinity", ["", "INTEGER", "REAL", "NUMERIC", "TEXT", "BLOB"])
def test_faithful_vacuum_copy_preserves_read_types(tmp_path, affinity):
    values = [None, 1, 1.0, 1.25, -0.0, 2**53 + 1, "1", "01", "nul\0text", "\u00e9",
              b"\0\xff", float("inf"), float("-inf")]
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "copy.sqlite3"
    with contextlib.closing(sqlite3.connect(source)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(f"CREATE TABLE review_probe (payload {affinity})")
        conn.executemany("INSERT INTO review_probe VALUES (?)", [(value,) for value in values])
        conn.commit()
        before_types = [tuple(row) for row in conn.execute(
            "SELECT typeof(payload) FROM review_probe ORDER BY rowid")]
        expected = {"review_probe": pruning._digest(conn.execute("SELECT * FROM review_probe"))}
        record = pruning._copy_store(conn, destination, expected)
    with contextlib.closing(sqlite3.connect(destination)) as backup:
        after_types = backup.execute(
            "SELECT typeof(payload) FROM review_probe ORDER BY rowid").fetchall()
    assert before_types == after_types
    assert signature(source, sqlite3.connect) == signature(destination, sqlite3.connect)
    assert record["verified"] is True


def test_equal_row_with_reversed_column_order_verifies(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "copy.sqlite3"
    connect = sqlite3.connect
    with contextlib.closing(connect(source)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE review_probe ("INTEGER", "TEXT")')
        conn.execute("INSERT INTO review_probe VALUES (?, ?)", (1, "1"))
        conn.commit()
        expected = {"review_probe": pruning._digest(conn.execute("SELECT * FROM review_probe"))}

        def reorder(database, *args, **kwargs):
            with contextlib.closing(connect(destination)) as copy:
                copy.executescript('ALTER TABLE review_probe RENAME TO old;'
                                   'CREATE TABLE review_probe ("TEXT", "INTEGER");'
                                   'INSERT INTO review_probe SELECT "TEXT", "INTEGER" FROM old;'
                                   'DROP TABLE old;')
            return connect(database, *args, **kwargs)

        monkeypatch.setattr(pruning.sqlite3, "connect", reorder)
        record = pruning._copy_store(conn, destination, expected)
        assert record["verified"] is True


def test_different_text_factory_can_refuse_physically_faithful_copy(tmp_path):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "copy.sqlite3"
    with contextlib.closing(sqlite3.connect(source)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE review_probe (payload TEXT)")
        conn.execute("INSERT INTO review_probe VALUES ('body')")
        conn.commit()
        # Metadata still decodes normally, but the payload is read as bytes.
        conn.text_factory = lambda raw: raw if raw == b"body" else raw.decode("utf-8")
        expected = {"review_probe": pruning._digest(conn.execute("SELECT * FROM review_probe"))}
        with pytest.raises(pruning.PruneVerificationError, match="snapshot.*review_probe"):
            pruning._copy_store(conn, destination, expected)
    assert signature(source, sqlite3.connect) == signature(destination, sqlite3.connect)


def test_default_text_factory_cannot_read_sqlite_lone_surrogate(tmp_path):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "copy.sqlite3"
    with contextlib.closing(sqlite3.connect(source)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE review_probe (payload)")
        conn.execute("INSERT INTO review_probe VALUES (CAST(X'EDA080' AS TEXT))")
        conn.commit()
        conn.text_factory = lambda raw: raw.decode("utf-8", "surrogatepass")
        expected = {"review_probe": pruning._digest(conn.execute("SELECT * FROM review_probe"))}
        with pytest.raises(sqlite3.OperationalError, match="decode"):
            pruning._copy_store(conn, destination, expected)
    assert signature(source, sqlite3.connect, True) == signature(destination, sqlite3.connect, True)


def test_plain_confirmed_apply_completes_backups_and_prunes(tmp_path, monkeypatch):
    seed(tmp_path)
    phase = ["planning"]
    visits = {}
    digest = pruning._row_digest

    def witnessed(row):
        visits[phase[0]] = visits.get(phase[0], 0) + 1
        return digest(row)

    monkeypatch.setattr(pruning, "_row_digest", witnessed)
    for name, stage in [("_copy_store", "backup"), ("_verify", "after_delete"),
                        ("_reverify", "after_vacuum")]:
        original = getattr(pruning, name)

        def witnessed_stage(*args, _original=original, _stage=stage, **kwargs):
            phase[0] = _stage
            return _original(*args, **kwargs)

        monkeypatch.setattr(pruning, name, witnessed_stage)
    report = pruning.prune(tmp_path, apply=True, confirm=True, vacuum=True)
    assert report.deleted == 3
    assert report.backups["copy"]["verified"] is True
    assert report.backups["rows"]["verified"] is True
    assert report.verification["after_vacuum"]["ok"] is True
    assert all(visits.get(stage, 0) > 0 for stage in
               ["planning", "backup", "after_delete", "after_vacuum"])
    with gzip.open(report.backups["rows"]["path"], "rt") as stream:
        assert [json.loads(line)["decision_id"] for line in stream] == [1, 2, 3]
