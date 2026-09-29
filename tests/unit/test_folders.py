"""C-6.5, C-24.5: a folder's holders as lease rows (`subfleet/folders.py`).

Turns share a folder with one row each; a detached writer holds it alone. The rows
are found by a range on the lease key and an exact check, so a folder whose name
extends another's (`/a/b:c` beside `/a/b`) is never mistaken for it.
"""

from __future__ import annotations

import sqlite3

import pytest
from hypothesis import given, settings, strategies as st

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
