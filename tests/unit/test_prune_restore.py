"""Restore invariants for the one-off prune's published operator recipe.

After the daemon and other store users stop, restoring must recover every
table's count and content from the backup, regardless of existing WAL/SHM
files or shell metacharacters in paths. All displaced files stay together and
byte-identical, and the backup remains unchanged. The documentation and error
message must publish the same executable recipe. Every database is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from hypothesis import given, settings, strategies as st

from subfleet.prune_decisions import _restore_fix


STORE_FILES = ("state.sqlite3", "state.sqlite3-wal", "state.sqlite3-shm")


def _fingerprints(database: Path) -> dict[str, tuple[int, str]]:
    """An independent multiset digest that includes decisions and events."""
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        result = {}
        for name in names:
            quoted = name.replace('"', '""')
            rows = sorted(json.dumps(row, ensure_ascii=True, sort_keys=True,
                                     default=lambda blob: {"blob": blob.hex()}).encode()
                          for row in connection.execute(f'SELECT * FROM "{quoted}"'))
            result[name] = (len(rows), hashlib.sha256(b"\n".join(rows)).hexdigest())
        return result
    finally:
        connection.close()


def _run_recipe(state_root: Path, backup: Path) -> subprocess.CompletedProcess[str]:
    """Execute the shell block operators receive, with only temporary paths."""
    recipe = _restore_fix(str(backup))
    assert recipe.count("```sh\n") == 1
    script = recipe.split("```sh\n", 1)[1].split("```", 1)[0]
    return subprocess.run(
        ["/bin/sh", "-c", script],
        env={**os.environ, "state_root": str(state_root)},
        cwd=state_root.parent, text=True, capture_output=True, check=True, timeout=30)


def _backup(database: Path, rows: list[tuple[str, bytes]]) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            "CREATE TABLE decisions (id INTEGER PRIMARY KEY, payload TEXT, token BLOB);"
            "CREATE TABLE events (id INTEGER PRIMARY KEY, message TEXT);"
            "CREATE TABLE settings (name TEXT, value TEXT);")
        connection.executemany("INSERT INTO decisions (payload, token) VALUES (?, ?)", rows)
        connection.execute("INSERT INTO events VALUES (1, 'before pruning')")
        connection.execute("INSERT INTO settings VALUES ('encoding', 'UTF-8')")
        connection.commit()
    finally:
        connection.close()


def test_documented_restore_matches_error_recipe():
    """The unsafe one-file restoration cannot return in the migration guide."""
    document = (Path(__file__).resolve().parents[2] / "docs" / "migration.md").read_text()
    assert document.count("<!-- prune-restore:start -->") == 1
    assert document.count("<!-- prune-restore:end -->") == 1
    documented = document.split("<!-- prune-restore:start -->", 1)[1].split(
        "<!-- prune-restore:end -->", 1)[0].strip()
    assert documented == _restore_fix("<backup copy>")
    for name in STORE_FILES:
        assert name in documented
    assert "PRAGMA integrity_check" in documented
    assert "lsof" in documented
    assert "launchctl unload" in documented


def test_restore_recipe_clears_hot_wal_with_unsafe_negative_control(tmp_path):
    """A valid hot WAL can silently replace backup content after unsafe restore."""
    crashed = tmp_path / "crashed"
    crashed.mkdir()
    backup = tmp_path / "backup.sqlite3"
    subprocess.run([sys.executable, "-c", r'''
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
connection.execute("PRAGMA wal_autocheckpoint=0")
connection.executescript(
    "CREATE TABLE decisions (id INTEGER PRIMARY KEY, payload TEXT);"
    "CREATE TABLE events (id INTEGER PRIMARY KEY, message TEXT);"
    "CREATE TABLE settings (name TEXT, value TEXT);")
connection.executemany("INSERT INTO decisions VALUES (?, ?)",
                       [(i, "before " + "x" * 200) for i in range(3000)])
connection.execute("INSERT INTO events VALUES (1, 'before pruning')")
connection.execute("INSERT INTO settings VALUES ('version', 'before')")
connection.commit()
assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
connection.execute("VACUUM INTO ?", (sys.argv[2],))
connection.execute("DELETE FROM decisions WHERE id < 1000")
connection.execute("UPDATE decisions SET payload='after pruning' WHERE id=2999")
connection.execute("INSERT INTO events VALUES (2, 'pruned')")
connection.execute("UPDATE settings SET value='after'")
connection.commit()
os._exit(0)
''', str(crashed / "state.sqlite3"), str(backup)], check=True, timeout=30)
    assert all((crashed / name).is_file() for name in STORE_FILES)
    assert (crashed / "state.sqlite3-wal").stat().st_size > 32
    expected = _fingerprints(backup)
    backup_bytes = backup.read_bytes()

    # Clone before any connection can recover, checkpoint, or remove the log.
    unsafe = tmp_path / "unsafe"
    safe = tmp_path / "safe"
    shutil.copytree(crashed, unsafe)
    shutil.copytree(crashed, safe)
    originals = {name: (safe / name).read_bytes() for name in STORE_FILES}

    (unsafe / "state.sqlite3").rename(unsafe / "old.sqlite3")
    shutil.copyfile(backup, unsafe / "state.sqlite3")
    connection = sqlite3.connect(unsafe / "state.sqlite3")
    try:
        # Integrity alone misses this failure: the replayed database is valid.
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        connection.close()
    unsafe_fingerprints = _fingerprints(unsafe / "state.sqlite3")
    assert unsafe_fingerprints["decisions"][0] == 2000
    assert unsafe_fingerprints["events"][0] == 2
    assert unsafe_fingerprints["settings"][0] == expected["settings"][0]
    assert unsafe_fingerprints["settings"] != expected["settings"]
    assert unsafe_fingerprints != expected

    completed = _run_recipe(safe, backup)
    assert "ok" in completed.stdout.splitlines()
    assert _fingerprints(safe / "state.sqlite3") == expected
    displaced, = safe.glob("state-before-restore.*")
    assert {path.name: path.read_bytes() for path in displaced.iterdir()} == originals
    assert backup.read_bytes() == backup_bytes


@settings(max_examples=30, deadline=None)
@given(
    rows=st.lists(st.tuples(st.text(max_size=50), st.binary(max_size=30)), max_size=25),
    suffix=st.text(alphabet="ab ' \"$;`()[]!&", max_size=16),
    wal=st.one_of(st.none(), st.binary(min_size=1, max_size=80)),
    shm=st.one_of(st.none(), st.binary(min_size=1, max_size=80)),
)
def test_restore_recipe_preserves_backup_and_displaced_files(rows, suffix, wal, shm):
    """Restore content and preserve evidence for every sidecar/path combination."""
    # A fresh directory per generated example avoids function-fixture state
    # leaking between Hypothesis examples, including shrinking attempts.
    with tempfile.TemporaryDirectory(prefix="subfleet-restore-") as directory:
        root = Path(directory)
        state_root = root / ("state ' $ ; " + suffix)
        state_root.mkdir()
        backup = root / ("backup ' $ ; " + suffix + ".sqlite3")
        _backup(backup, rows)
        _backup(state_root / "state.sqlite3", [("displaced", b"old")])
        for name, data in (("state.sqlite3-wal", wal), ("state.sqlite3-shm", shm)):
            if data is not None:
                (state_root / name).write_bytes(data)
        expected = _fingerprints(backup)
        original_backup = backup.read_bytes()
        originals = {path.name: path.read_bytes() for path in state_root.iterdir()}

        completed = _run_recipe(state_root, backup)

        assert "ok" in completed.stdout.splitlines()
        assert _fingerprints(state_root / "state.sqlite3") == expected
        displaced, = state_root.glob("state-before-restore.*")
        assert {path.name: path.read_bytes() for path in displaced.iterdir()} == originals
        assert backup.read_bytes() == original_backup
        assert set(root.iterdir()) == {state_root, backup}
