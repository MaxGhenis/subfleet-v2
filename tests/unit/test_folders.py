"""C-6.5, C-24.5: a folder's holders as lease rows (`subfleet/folders.py`).

Turns share a folder with one row each; a detached writer holds it alone. The rows
are found by a range on the lease key and an exact check, so a folder whose name
extends another's (`/a/b:c` beside `/a/b`) is never mistaken for it.
"""

from __future__ import annotations

import sqlite3

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import folders

# Real paths: any character but NUL, including the separators the keys use.
SEGMENT = st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00/"),
                  min_size=1, max_size=6)
FOLDER = st.lists(SEGMENT, min_size=1, max_size=3).map(lambda parts: "/" + "/".join(parts))
JOB = st.from_regex(r"20260929-[0-9]{6}-[a-z0-9-]{1,8}", fullmatch=True)


def table(rows):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE leases (lease_key TEXT PRIMARY KEY, holder TEXT NOT NULL)")
    db.executemany("INSERT OR IGNORE INTO leases VALUES (?,?)", rows)
    return lambda sql, params: db.execute(sql, params).fetchall()


@settings(max_examples=300, deadline=None)
@given(holds=st.lists(st.tuples(FOLDER, JOB, st.booleans()), max_size=12), probe=FOLDER)
def test_turn_holds_names_exactly_the_rows_on_that_folder(holds, probe):
    """For any folders (colons, semicolons and prefixes of one another included), the
    rows found on `probe` are exactly the turn rows whose folder is `probe`, of the kinds
    asked for, and `turn_folders` is exactly the set of folders held."""
    rows = [(folders.turn_key(folder, job, writable=writable), job) for folder, job, writable in holds]
    rows.append((folders.exclusive_key(probe), "detached"))          # never a turn's row
    read = table(rows)
    stored = {key: holder for key, holder in rows}
    want = {(key, holder) for key, holder in stored.items()
            if (parsed := folders.parse(key)) and parsed[1] == probe}
    assert set(folders.turn_holds(read, probe)) == want
    writers = {(key, holder) for key, holder in want if key.startswith(folders.TURN)}
    assert set(folders.turn_holds(read, probe, (folders.TURN,))) == writers
    assert folders.turn_folders(read) == {folders.parse(key)[1] for key in stored if folders.parse(key)}


@given(folder=FOLDER, job=JOB, writable=st.booleans())
def test_a_turn_key_round_trips(folder, job, writable):
    prefix = folders.TURN if writable else folders.READER
    assert folders.parse(folders.turn_key(folder, job, writable=writable)) == (prefix, folder, job)


def test_a_job_id_with_a_colon_is_refused_and_other_keys_are_not_turns():
    with pytest.raises(ValueError):
        folders.turn_key("/a", "job:1", writable=True)
    for key in ("worktree:/a", "conversation:x", "native:claude:s", "worktree-turn:", "worktree-read:/a:"):
        assert folders.parse(key) is None


def case_insensitive(directory) -> bool:
    probe = directory / "CaseProbe"
    probe.mkdir()
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.rmdir()


@pytest.fixture
def insensitive(tmp_path):
    """A directory on a case-insensitive volume (APFS's default), or the test is skipped."""
    if not case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive volume, as APFS is by default")
    return tmp_path


INNER = ("Scratch-Folder", "Ünïcode Inner")


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(flips=st.lists(st.booleans(), min_size=len("".join(INNER)), max_size=len("".join(INNER))),
       decomposed=st.booleans(), via=st.sampled_from(["path", "symlink", "dots", "tilde"]))
def test_one_folder_has_one_spelling_whatever_case_it_is_typed_in(insensitive, flips, decomposed, via,
                                                                    monkeypatch):
    """Review of 5e9f2fbd (P3-4): a folder outside git kept the case it was typed in, so
    `~/Scratch` and `~/scratch` were two lease keys and two `target`s for one folder,
    and two conversations there were never marked as sharing it. Every spelling the
    volume accepts (any case, either Unicode normalization, through a symlink, `..` or
    `~`) gives one string: the folder as its directories list it."""
    import os
    import unicodedata
    real = insensitive.joinpath(*INNER)
    real.mkdir(parents=True, exist_ok=True)
    want = os.path.join(folders.canonical(insensitive), *INNER)
    assert os.path.samefile(want, real)
    letters = iter(flips)
    typed = [("".join(c.swapcase() if next(letters) else c for c in name)) for name in INNER]
    if decomposed:
        typed = [unicodedata.normalize("NFD", name) for name in typed]
    if via == "symlink":
        link = insensitive / "Alias"
        if not link.is_symlink():
            link.symlink_to(insensitive / INNER[0], target_is_directory=True)
        spelled = str(insensitive / "aLIAS" / typed[1])
    elif via == "dots":
        spelled = str(insensitive / typed[0] / ".." / typed[0] / "." / typed[1])
    elif via == "tilde":
        monkeypatch.setenv("HOME", str(insensitive))
        spelled = "~/" + "/".join(typed)
    else:
        spelled = str(insensitive.joinpath(*typed))
    assert os.path.isdir(os.path.expanduser(spelled))
    assert folders.canonical(spelled) == want
    assert folders.canonical(want) == want                       # already one spelling: unchanged


def test_a_name_that_cannot_be_listed_or_is_missing_stays_as_typed(insensitive):
    """What cannot be resolved is kept, with everything after it: a missing tail (a
    scratch folder deleted since), and the names under a directory that cannot be read."""
    import os
    (insensitive / "Present").mkdir()
    base = folders.canonical(insensitive)
    assert folders.canonical(insensitive / "present" / "Gone" / "deeper") == os.path.join(base, "Present", "Gone",
                                                                                          "deeper")
    locked = insensitive / "Locked"
    (locked / "Inner").mkdir(parents=True)
    locked.chmod(0o111)                         # searchable, not listable
    try:
        assert folders.canonical(insensitive / "LOCKED" / "inner") == os.path.join(base, "Locked", "inner")
    finally:
        locked.chmod(0o755)
