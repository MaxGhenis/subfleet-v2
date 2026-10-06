"""C-6.5, C-24.5: a folder's holders as lease rows (`subfleet/folders.py`).

Turns share a folder with one row each; a detached writer holds it alone. The rows
are found by a range on the lease key and an exact check, so a folder whose name
extends another's (`/a/b:c` beside `/a/b`) is never mistaken for it.
"""

from __future__ import annotations

import os
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


@st.composite
def near(draw, probe):
    """A folder `probe` may be confused with: itself, one inside it, one whose name
    extends it (`/a/bc`, `/a/b:c`, `/a/b;`), the folder above it, or any other."""
    return draw(st.one_of(st.just(probe), SEGMENT.map(lambda s: f"{probe}/{s}"),
                          st.lists(SEGMENT, min_size=2, max_size=3).map(lambda parts: probe + "/" + "/".join(parts)),
                          SEGMENT.map(lambda s: probe + s), st.just(probe.rsplit("/", 1)[0] or "/"), FOLDER))


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data())
def test_turn_holds_inside_names_exactly_the_rows_on_that_folder_or_below_it(data):
    """With `inside`, the rows found on `probe` are exactly the turn rows whose folder
    is `probe` or a folder below it (`folder == probe or folder.startswith(probe + "/")`,
    review of 599af189, P3-1), each once, of the kinds asked for; without it, the
    exact answer is unchanged."""
    probe = data.draw(FOLDER, label="probe")
    holds = data.draw(st.lists(st.tuples(near(probe), JOB, st.booleans()), max_size=12), label="holds")
    rows = [(folders.turn_key(folder, job, writable=writable), job) for folder, job, writable in holds]
    rows.append((folders.exclusive_key(probe), "detached"))
    read = table(rows)
    stored = {key: holder for key, holder in rows}
    below = {(key, holder) for key, holder in stored.items()
             if (parsed := folders.parse(key)) and (parsed[1] == probe or parsed[1].startswith(probe + "/"))}
    found = folders.turn_holds(read, probe, inside=True)
    assert set(found) == below and len(found) == len(below)
    writers = {(key, holder) for key, holder in below if key.startswith(folders.TURN)}
    assert set(folders.turn_holds(read, probe, (folders.TURN,), inside=True)) == writers
    exact = {(key, holder) for key, holder in below if folders.parse(key)[1] == probe}
    assert set(folders.turn_holds(read, probe)) == exact


@given(a=FOLDER, b=FOLDER, c=FOLDER)
def test_within_is_a_partial_order_on_folders(a, b, c):
    """`within` is the user's rule (`folder == top or folder.startswith(top + "/")`)
    and, on folders spelled without a trailing `/`, reflexive, antisymmetric and
    transitive; every folder is within `/`."""
    assert folders.within(a, b) == (a == b or a.startswith(b + "/"))
    assert folders.within(a, a) and folders.within(a, "/")
    if folders.within(a, b) and folders.within(b, a):
        assert a == b
    if folders.within(a, b) and folders.within(b, c):
        assert folders.within(a, c)
    assert folders.within(a + "/x", a) and not folders.within(a + "x", a) and not folders.within(a + ":x", a)


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(folder=FOLDER, data=st.data())
def test_above_is_exactly_the_other_folders_a_folder_is_within(folder, data):
    """`above` (C-8.4) agrees with `within`, built another way: the folders above a
    folder spelled one way are its leading parts up to each `/` but the first, and
    `/`, nearest first, one per name in it; each is a folder it is `within`, and of
    the folders it may be confused with (one inside it, one whose name extends it,
    any other), none is above it unless `within` says so."""
    found = folders.above(folder)
    built = sorted({folder[:at] for at, char in enumerate(folder) if char == "/" and at} | {"/"}, key=len, reverse=True)
    assert found == built and len(found) == folder.count("/")
    assert all(folders.within(folder, each) and each != folder for each in found)
    assert folders.above("/") == []
    other = data.draw(near(folder), label="other")
    assert (other in found) == (other != folder and folders.within(folder, other))


HOLDER = st.one_of(JOB.map(lambda job: "retention:" + job), JOB, JOB.map(lambda job: f"{job}/a1"),
                   JOB.map(lambda job: "gate-round:" + job), st.just("retention"), st.just("retentions:x"))


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data())
def test_retiring_names_exactly_retentions_fences_on_a_folder_and_above_it(data):
    """`retiring` (C-8.4, C-6.5) against a scan of every row: the `worktree:` keys
    whose folder `probe` is `within` and whose holder is retention's
    (`retention:<job>`), nearest first. Not a detached writer's, an attempt's or a
    gate round's `worktree:` above it, not a fence beside or below it, and never a
    turn's row, whatever their folders' names."""
    probe = data.draw(FOLDER, label="probe")
    spots = st.one_of(st.sampled_from([probe, *folders.above(probe)]), near(probe))
    fences = data.draw(st.lists(st.tuples(spots, HOLDER), max_size=10), label="fences")
    turns = data.draw(st.lists(st.tuples(spots, JOB, st.booleans()), max_size=4), label="turns")
    rows = [(folders.exclusive_key(spot), holder) for spot, holder in fences]
    rows += [(folders.turn_key(spot, job, writable=writable), job) for spot, job, writable in turns]
    read = table(rows)
    stored = dict(read("SELECT lease_key, holder FROM leases", ()))
    want = [key for key, holder in stored.items()
            if key.startswith("worktree:") and holder.startswith("retention:")
            and folders.within(probe, key[len("worktree:"):])]
    assert folders.retiring(read, probe) == sorted(want, key=len, reverse=True)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(tree=FOLDER, data=st.data(), job=JOB, writable=st.booleans())
def test_a_fence_holds_exactly_the_turns_whose_row_would_keep_its_tree(tree, data, job, writable):
    """The two sides of C-8.4 agree. Retention's fence on a tree holds a turn at
    admission (`retiring`) exactly when that turn's row, had it been there first,
    would keep the tree at selection and at the commit (`turn_holds(..., inside=True)`)
    and in the census (`within`): a turn on the tree or in a folder inside it, never
    one beside or above it."""
    turn = data.draw(near(tree), label="turn")
    fenced = bool(folders.retiring(table([(folders.exclusive_key(tree), "retention:job")]), turn))
    kept = bool(folders.turn_holds(table([(folders.turn_key(turn, job, writable=writable), job)]), tree, inside=True))
    assert fenced == kept == folders.within(turn, tree)


def test_retiring_reads_no_file_system(monkeypatch):
    """C-8.4: admission calls `retiring` inside its reserving transaction, where no
    folder is spelled or looked up: the folder comes spelled (`canonical`, at submit),
    and its parents are string operations. `folders` sees an `os` with nothing but
    `path.dirname` (pytest's own `os` is untouched)."""
    import types

    def unexpected(*args, **kwargs):
        raise AssertionError("no filesystem work in the admitting transaction")

    for name in ("canonical", "spelling", "_kernel_path"):
        monkeypatch.setattr(folders, name, unexpected)
    monkeypatch.setattr(folders, "os", types.SimpleNamespace(path=types.SimpleNamespace(dirname=os.path.dirname)))
    read = table([("worktree:/w", "retention:j"), ("worktree:/w/a", "detached"), ("worktree:/", "retention:k")])
    assert folders.retiring(read, "/w/a/b") == ["worktree:/w", "worktree:/"]


def test_inside_reads_each_rows_folder_from_its_key():
    """A key in `<folder>/`'s range whose last ':' falls inside `<folder>` names
    another folder (`/a`, by a job `1/b/x`). `turn_key` never writes one, since a job
    id holds no '/' (C-1.1); a row written otherwise is still not taken as inside."""
    read = table([("worktree-turn:/a:1/b/x", "x"), (folders.turn_key("/a:1/b/c", "j", writable=True), "j")])
    assert folders.turn_holds(read, "/a:1/b", inside=True) == [(folders.turn_key("/a:1/b/c", "j", writable=True), "j")]


def test_inside_a_root_folder_is_every_folder_once():
    read = table([(folders.turn_key("/", "j1", writable=True), "j1"),
                  (folders.turn_key("/a", "j2", writable=False), "j2")])
    found = folders.turn_holds(read, "/", inside=True)            # `/`'s own rows are in both ranges
    assert len(found) == 2 and set(found) == {(folders.turn_key("/", "j1", writable=True), "j1"),
                                              (folders.turn_key("/a", "j2", writable=False), "j2")}


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


INNER = ("Above-It", "Scratch-Folder", "Ünïcode Inner")


@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(flips=st.lists(st.booleans(), min_size=len("".join(INNER)), max_size=len("".join(INNER))),
       decomposed=st.booleans(), via=st.sampled_from(["path", "symlink", "dots", "tilde"]),
       unlistable=st.sets(st.sampled_from(range(len(INNER)))))
def test_one_folder_has_one_spelling_whatever_case_it_is_typed_in(insensitive, flips, decomposed, via, unlistable,
                                                                    monkeypatch):
    """Review of 5e9f2fbd (P3-4): a folder outside git kept the case it was typed in, so
    `~/Scratch` and `~/scratch` were two lease keys and two `target`s for one folder,
    and two conversations there were never marked as sharing it. Every spelling the
    volume accepts (any case, either Unicode normalization, through a symlink, `..` or
    `~`) gives one string: the folder as its directories store it. Review of b0033e5d
    (P2): so does every mode of the directories on the way that can be searched, the
    folder's included; any of them at 0111 (searchable, not listable) kept the names
    below it as typed when the spelling was read from listings."""
    import os
    import unicodedata
    real = insensitive.joinpath(*INNER)
    real.mkdir(parents=True, exist_ok=True)
    link = insensitive / "Alias"
    if not link.is_symlink():
        link.symlink_to(insensitive.joinpath(*INNER[:2]), target_is_directory=True)
    want = os.path.join(folders.canonical(insensitive), *INNER)
    assert os.path.samefile(want, real)
    letters = iter(flips)
    typed = [("".join(c.swapcase() if next(letters) else c for c in name)) for name in INNER]
    if decomposed:
        typed = [unicodedata.normalize("NFD", name) for name in typed]
    if via == "symlink":
        spelled = str(insensitive / "aLIAS" / typed[2])
    elif via == "dots":
        spelled = str(insensitive / typed[0] / typed[1] / ".." / typed[1] / "." / typed[2])
    elif via == "tilde":
        monkeypatch.setenv("HOME", str(insensitive))
        spelled = "~/" + "/".join(typed)
    else:
        spelled = str(insensitive.joinpath(*typed))
    locked = [insensitive.joinpath(*INNER[:depth + 1]) for depth in sorted(unlistable)]
    for directory in locked:
        directory.chmod(0o111)
    try:
        assert all(not os.access(directory, os.R_OK) for directory in locked)
        assert os.path.isdir(os.path.expanduser(spelled))
        assert folders.canonical(spelled) == want
        assert folders.canonical(want) == want                   # already one spelling: unchanged
    finally:
        for directory in reversed(locked):
            directory.chmod(0o755)


def test_a_missing_name_stays_as_typed_and_one_that_cannot_be_looked_up_is_in_doubt(insensitive):
    """What does not exist is kept as typed, with everything after it, and that is its
    one spelling (no other names anything): a missing tail (a scratch folder deleted
    since), or a name under a file. A name under a directory that can be searched but
    not listed is spelled as stored (review of b0033e5d, P2: it was kept as typed). A
    name under a directory that cannot be searched is kept as typed, and `spelling`
    says it could not be looked up, so C-26.10 refuses rather than compares it."""
    import os
    (insensitive / "Present").mkdir()
    (insensitive / "Present" / "a-file").write_text("")
    base = folders.canonical(insensitive)
    assert folders.spelling(insensitive / "present" / "Gone" / "deeper") == (
        os.path.join(base, "Present", "Gone", "deeper"), None)
    assert folders.spelling(insensitive / "PRESENT" / "A-FILE" / "x") == (os.path.join(base, "Present", "a-file", "x"),
                                                                          None)
    locked = insensitive / "Locked"
    (locked / "Inner").mkdir(parents=True)
    sealed = insensitive / "Sealed"
    (sealed / "Inner").mkdir(parents=True)
    locked.chmod(0o111)                         # searchable, not listable
    sealed.chmod(0o000)                         # neither
    try:
        assert folders.spelling(insensitive / "LOCKED" / "inner") == (os.path.join(base, "Locked", "Inner"), None)
        assert folders.spelling(insensitive / "LOCKED" / "gone") == (os.path.join(base, "Locked", "gone"), None)
        spelled, doubt = folders.spelling(insensitive / "SEALED" / "inner" / "deeper")
        assert spelled == folders.canonical(insensitive / "sealed" / "inner" / "deeper") == os.path.join(
            base, "Sealed", "inner", "deeper")
        assert doubt == f"{os.path.join(base, 'Sealed', 'inner')}: Permission denied"
    finally:
        locked.chmod(0o755)
        sealed.chmod(0o755)


def test_a_mount_point_and_a_firmlinked_folder_are_spelled_as_the_system_shows_them():
    """The kernel's path, not each directory's name for itself: `/` is `/` (its
    ATTR_CMN_NAME is the volume's name), and a folder reached through the Data
    volume's firmlink (`/System/Volumes/Data/Users/…`) is the one under `/Users`."""
    import os
    assert folders.spelling("/") == ("/", None)
    home = folders.canonical(os.path.expanduser("~"))
    data = "/System/Volumes/Data" + home
    if not home.startswith("/Users/") or not os.path.isdir(data) or not os.path.samefile(data, home):
        pytest.skip("needs a home under the Data volume's /Users firmlink, as macOS has")
    assert folders.spelling(data) == (home, None)
