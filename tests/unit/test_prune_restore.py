"""Restore invariants for the one-off prune's published operator recipe.

After the daemon and other store users stop, restoring must recover every
table's count and content from the backup, regardless of existing WAL/SHM
files or shell metacharacters in paths. All displaced files stay together and
byte-identical, and the backup remains unchanged. The documentation and error
message must publish the same executable recipe. Its shell block stays whole for
every path: the fence is longer than any backtick run the path puts inside it,
so no Markdown reader ends the block early, and the harness reads the block as
such a reader does (D-PRR1). Every database is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from hypothesis import example, given, settings, strategies as st

from subfleet import prune_decisions as pruning
from subfleet.prune_decisions import _restore_fix
from tests.markdown_fences import fenced_blocks, shell_block


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


def _recipe_environment(state_root: Path) -> dict[str, str]:
    """The recipe's whole environment, built from nothing (D-PRR1).

    pytest prints each failing frame's arguments, `subprocess.run`'s `kwargs`
    among them, so a copy of `os.environ` put inherited values in a report.
    """
    environment = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
                   "TMPDIR": os.environ.get("TMPDIR") or tempfile.gettempdir(),
                   "state_root": str(state_root)}
    environment.update((name, os.environ[name]) for name in ("LANG", "LC_ALL")
                       if name in os.environ)
    return environment


def _run_recipe(state_root: Path, backup: Path) -> subprocess.CompletedProcess[str]:
    """Execute the shell block operators receive, read as a Markdown reader reads it."""
    script = shell_block(_restore_fix(str(backup)))
    # A truncated extraction fails here, by name, not as a shell syntax error.
    assert script == pruning.RESTORE_SCRIPT.format(copy_path=shlex.quote(str(backup))) + "\n"
    return subprocess.run(
        ["/bin/sh", "-c", script], env=_recipe_environment(state_root),
        cwd=state_root.parent, text=True, capture_output=True, check=True, timeout=120)


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


def test_restore_recipe_clears_hot_wal_over_a_compacted_copy_with_malformed_control(tmp_path):
    """The copy a pass takes is compacted by `VACUUM INTO`, so an old log's pages do not fit it."""
    crashed = tmp_path / "crashed"
    crashed.mkdir()
    backup = tmp_path / "backup.sqlite3"
    subprocess.run([sys.executable, "-c", r'''
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1], isolation_level=None)
assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
connection.execute("PRAGMA wal_autocheckpoint=0")
connection.executescript(
    "CREATE TABLE decisions (id INTEGER PRIMARY KEY, payload TEXT);"
    "CREATE TABLE events (id INTEGER PRIMARY KEY, message TEXT);")
connection.execute("BEGIN")
connection.executemany("INSERT INTO decisions VALUES (?, ?)",
                       [(i, "before " + "x" * 2000) for i in range(3000)])
connection.execute("COMMIT")
# Free pages in the live file, which VACUUM INTO leaves out of the copy.
connection.execute("DELETE FROM decisions WHERE id % 3 = 0")
assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
connection.execute("VACUUM INTO ?", (sys.argv[2],))
connection.execute("BEGIN")
connection.execute("DELETE FROM decisions WHERE id < 1500")
connection.execute("INSERT INTO events VALUES (1, 'pruned')")
connection.execute("COMMIT")
os._exit(0)
''', str(crashed / "state.sqlite3"), str(backup)], check=True, timeout=60)
    assert (crashed / "state.sqlite3-wal").stat().st_size > 32
    expected = _fingerprints(backup)
    assert expected["decisions"][0] == 2000
    backup_bytes = backup.read_bytes()

    unsafe = tmp_path / "unsafe"
    safe = tmp_path / "safe"
    shutil.copytree(crashed, unsafe)
    shutil.copytree(crashed, safe)
    originals = {name: (safe / name).read_bytes() for name in STORE_FILES}

    (unsafe / "state.sqlite3").rename(unsafe / "old.sqlite3")
    shutil.copyfile(backup, unsafe / "state.sqlite3")
    connection = sqlite3.connect(unsafe / "state.sqlite3")
    try:
        assert connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
        with pytest.raises(sqlite3.DatabaseError, match="malformed"):
            connection.execute("SELECT COUNT(*) FROM decisions").fetchone()
    finally:
        connection.close()

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
# Seed 18's shrunk failure: the old harness cut the block at this run.
@example(rows=[], suffix="```", wal=None, shm=None)
@example(rows=[("x", b"\x00")], suffix="````", wal=b"wal", shm=b"shm")
@example(rows=[], suffix="a```$;'b````\"(!&", wal=None, shm=b"shm")
# A run on a line of its own, which ends a three-backtick fence for CommonMark.
@example(rows=[], suffix="\n```\n", wal=b"wal", shm=None)
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


#: Backup paths whose backticks ended the old three-backtick block for some
#: reader: a mid-line run (seed 18's shape) ends it for a reader that stops at
#: the next triple backtick, and a run on a line of its own ends it for CommonMark.
FENCED_PATHS = [
    "/backups/state ```.sqlite3",
    "/backups/state ````.sqlite3",
    "/backups/state\n```\necho the fence closed\n.sqlite3",
    "/backups/state\n````\n.sqlite3",
]


@pytest.mark.parametrize("path", FENCED_PATHS)
def test_restore_fix_fences_its_block_beyond_every_backtick_run_in_the_path(path):
    """C-11.5, D-PRR1: the fence is a run longer than any inside the block, and
    never shorter than three, so no backup path can close it early."""
    blocks = fenced_blocks(_restore_fix(path))
    assert [block.info for block in blocks] == ["sh"]
    block, = blocks
    longest = max(map(len, re.findall("`+", block.content)), default=0)
    assert len(block.fence) >= 3 and len(block.fence) > longest
    assert block.closing == block.fence
    assert f"    backup={shlex.quote(path)}\n" in block.content
    assert block.content.endswith("'PRAGMA integrity_check'\n)\n")


@pytest.mark.parametrize("path", FENCED_PATHS)
def test_run_recipe_hands_the_shell_the_whole_block(path, tmp_path, monkeypatch):
    """D-PRR1: the harness runs the block a Markdown reader sees, not the text up
    to the first triple backtick, which seed 18 cut inside the backup's path."""
    launched = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **options: launched.append(argv))
    _run_recipe(tmp_path / "state", Path(path))
    script = pruning.RESTORE_SCRIPT.format(copy_path=shlex.quote(path)) + "\n"
    assert launched == [["/bin/sh", "-c", script]]


def test_run_recipe_gives_the_shell_only_the_variables_it_needs(tmp_path, monkeypatch):
    """C-20.6, D-PRR1: the recipe's environment is PATH, the session's HOME,
    TMPDIR, LANG and LC_ALL when set, and `state_root`; never a copy of
    `os.environ`, whose values a failing run's report prints."""
    names = tmp_path / "names.json"
    tools = tmp_path / "tools"
    tools.mkdir()
    # A stand-in `sqlite3` records the names it was given, never their values.
    record = (f"import json, os; open({str(names)!r}, 'w').write(json.dumps(sorted(os.environ)));"
              " print('ok')")
    stand_in = tools / "sqlite3"
    stand_in.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(record)}\n")
    stand_in.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PRR1_CANARY", "inherited")
    monkeypatch.setenv("PRR1_CANARY_TOKEN", "SECRET7prr1-canary")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.delenv("LANG", raising=False)
    state_root = tmp_path / "state"
    state_root.mkdir()
    backup = tmp_path / "backup.sqlite3"
    _backup(backup, [])

    completed = _run_recipe(state_root, backup)

    assert completed.stdout.splitlines() == ["ok"]
    # What the shell and the interpreter set for themselves.
    own = {"PWD", "OLDPWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING", "__PYVENV_LAUNCHER__"}
    assert set(json.loads(names.read_text())) - own == {
        "PATH", "HOME", "TMPDIR", "LC_ALL", "state_root"}


#: Text a backup path can hold: backtick runs of every length to six, quotes,
#: `$`, every CommonMark line ending, and other characters, ASCII or not.
PATH_TEXT = st.lists(st.one_of(
    st.integers(0, 6).map(lambda count: "`" * count),
    st.sampled_from(["'", '"', "$", "\n", "\r", "\r\n", " ", "\t", ";", "/", "é", "日本", "🙂"]),
    st.text(max_size=3),
), max_size=12).map(lambda pieces: "/backups/" + "".join(pieces))


@settings(max_examples=200, deadline=None)
@given(path=PATH_TEXT)
@example(path="/backups/```")
@example(path="/backups/\n```\n")
@example(path="/backups/\r````\r\n``````")
def test_restore_fix_block_is_the_template_script_for_every_path(path):
    """C-11.5, D-PRR1 invariant 1: read by CommonMark's fence rule, the recipe's
    one block is exactly the template's script with `shlex.quote(path)` in it,
    under a fence longer than any run inside, and the prose around it is the
    prose every path gets."""
    recipe = _restore_fix(path)
    blocks = fenced_blocks(recipe)
    assert [(block.info, block.closing) for block in blocks] == [("sh", blocks[0].fence)]
    block, = blocks
    assert block.content == pruning.RESTORE_SCRIPT.format(copy_path=shlex.quote(path)) + "\n"
    assert len(block.fence) >= 3
    assert len(block.fence) > max(map(len, re.findall("`+", block.content)), default=0)
    plain = _restore_fix("/backups/state.sqlite3")
    plain_block, = fenced_blocks(plain)
    assert recipe[:block.start] == plain[:plain_block.start]
    assert recipe[block.end:] == plain[plain_block.end:]


@pytest.mark.parametrize(("text", "expected"), [
    # A longer closing run closes; a shorter one is content.
    ("````\naaa\n```\n``````\n", [("````", "``````", "aaa\n```\n")]),
    # Up to three spaces may indent a closing fence; four make it content.
    ("```\naaa\n   ```\n", [("```", "```", "aaa\n")]),
    ("```\naaa\n    ```\n", [("```", None, "aaa\n    ```\n")]),
    # A closing fence may be followed only by spaces or tabs.
    ("```\naaa\n``` x\n", [("```", None, "aaa\n``` x\n")]),
    # A backtick in the info string means no fence at all.
    ("``` `x`\naaa\n", []),
    # CR and CR LF end lines too.
    ("```\raaa\r\n```\r", [("```", "```", "aaa\r\n")]),
])
def test_the_fence_reader_follows_commonmark(text, expected):
    """D-PRR1: the harness's reader keeps CommonMark's fence rules."""
    assert [(block.fence, block.closing, block.content) for block in fenced_blocks(text)] == expected
