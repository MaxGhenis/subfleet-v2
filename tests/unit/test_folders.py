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


def test_present_says_whether_every_name_of_a_folder_is_there(insensitive):
    """`present` (C-8.4, review of 8a112986, finding 1) is `spelling`'s first value,
    and None as its second only when the kernel looked up every name: a folder
    typed in another case, or through a directory that can be searched but not
    listed, is there and spelled as stored. A missing name is kept as typed by both,
    which `spelling` calls the one spelling (nothing else names anything), and
    `present` says it is not there, naming the first name it could not look up, so
    admission reserves no row on it; so for a name under a file, and for one under
    a directory that cannot be searched, which `spelling` doubts too."""
    import errno
    (insensitive / "Present" / "Inner").mkdir(parents=True)
    (insensitive / "Present" / "a-file").write_text("")
    base = folders.canonical(insensitive)
    assert folders.present(insensitive / "present" / "INNER") == (os.path.join(base, "Present", "Inner"), None)
    for typed, kind, first in [(("present", "Gone", "deeper"), FileNotFoundError, ("Present", "Gone")),
                               (("PRESENT", "A-FILE", "x"), NotADirectoryError, ("Present", "a-file", "x"))]:
        spelled, missing = folders.present(insensitive.joinpath(*typed))
        assert spelled == folders.spelling(insensitive.joinpath(*typed))[0]
        assert isinstance(missing, kind) and missing.filename == os.path.join(base, *first), missing
    locked, sealed = insensitive / "Locked", insensitive / "Sealed"
    (locked / "Inner").mkdir(parents=True)
    (sealed / "Inner").mkdir(parents=True)
    locked.chmod(0o111)                         # searchable, not listable
    sealed.chmod(0o000)                         # neither
    try:
        assert folders.present(insensitive / "LOCKED" / "inner") == (os.path.join(base, "Locked", "Inner"), None)
        spelled, missing = folders.present(insensitive / "SEALED" / "inner")
        assert spelled == os.path.join(base, "Sealed", "inner") and missing.errno == errno.EACCES, missing
        assert folders.spelling(insensitive / "SEALED" / "inner")[1]
    finally:
        locked.chmod(0o755)
        sealed.chmod(0o755)


def test_present_without_getattrlist_is_whether_the_real_path_exists(tmp_path, monkeypatch):
    """Where the kernel spells nothing (no getattrlist, ENOSYS) `spelling` keeps the
    real path and says so, and `present` takes a folder as there when its real path
    exists: such a system compares names as given, so no other spelling of it can
    come back. A real path that does not exist is not there."""
    import errno

    def no_getattrlist(path):
        raise OSError(errno.ENOSYS, "this system has no getattrlist", path)

    (tmp_path / "Here").mkdir()
    monkeypatch.setattr(folders, "_kernel_path", no_getattrlist)
    here, gone = os.path.realpath(tmp_path / "Here"), os.path.realpath(tmp_path / "Gone")
    assert folders.present(tmp_path / "Here") == (here, None)
    assert folders.spelling(tmp_path / "Here") == (here, f"{here}: this system has no getattrlist")
    spelled, missing = folders.present(tmp_path / "Gone")
    assert spelled == gone and isinstance(missing, FileNotFoundError) and missing.filename == gone


TREE_NAMES = ("Alpha", "Ünï", "straße")


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(depth=st.integers(0, len(TREE_NAMES)), extra=st.lists(st.sampled_from(["Gone", "x", "ÉTÉ"]), max_size=2),
       flips=st.lists(st.booleans(), min_size=20, max_size=20), decomposed=st.booleans())
def test_present_is_the_one_spelling_of_a_folder_exactly_when_it_is_there(insensitive, depth, extra, flips,
                                                                          decomposed):
    """For any spelling of a path on a case-insensitive volume (each name in any
    case, NFC or NFD) whose first `depth` names exist and whose others do not:
    `present` gives `spelling`'s string; says the folder is there exactly when it
    is (no missing names); and when it is, gives the one spelling of the existing
    folder, which is its own answer again. When it is not, the names after the
    existing part are kept as typed, so another spelling of the same path gives
    another string: the reason no row is keyed on it."""
    import unicodedata
    existing = insensitive.joinpath(*TREE_NAMES)
    existing.mkdir(parents=True, exist_ok=True)
    letters = iter(flips * 4)
    names = [*TREE_NAMES[:depth], *extra]
    typed = ["".join(c.swapcase() if next(letters) else c for c in name) for name in names]
    if decomposed:
        typed = [unicodedata.normalize("NFD", name) for name in typed]
    path = insensitive.joinpath(*typed)
    spelled, missing = folders.present(path)
    assert spelled == folders.spelling(path)[0]
    there = not extra
    assert (missing is None) == there == os.path.isdir(path), (path, missing)
    if there:
        assert spelled == os.path.join(folders.canonical(insensitive), *TREE_NAMES[:depth]) or depth == 0
        assert folders.present(spelled) == (spelled, None)
    else:
        assert spelled.endswith(os.path.join(*typed[depth:]))       # kept as typed


@given(text=st.text(max_size=12))
def test_fold_is_blind_to_case_and_normal_form_and_keeps_separators(text):
    """`fold` (C-8.4): one string for a name in either case of ASCII letters and in
    either normal form, and a `/` for each `/`, none added or lost, so `within`
    between folded spellings is `within` up to case and normal form."""
    import unicodedata
    assert folders.fold(text) == folders.fold(unicodedata.normalize("NFC", text)) \
        == folders.fold(unicodedata.normalize("NFD", text))
    ascii_text = "".join(c for c in text if c.isascii())
    assert folders.fold(ascii_text) == folders.fold(ascii_text.swapcase()) == ascii_text.lower()
    assert folders.fold(text).count("/") == text.count("/")


FOLD_SEGMENT = st.text(st.sampled_from("aAbBjJoO:-.ßẞéÉ́"), min_size=1, max_size=4).filter(
    lambda name: name not in (".", ".."))
FOLD_FOLDER = st.lists(FOLD_SEGMENT, min_size=1, max_size=3).map(lambda parts: "/" + "/".join(parts))


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data())
def test_folded_retiring_names_exactly_retentions_fences_a_folder_folds_into(data):
    """`retiring(..., folded=True)` (C-8.4) against a scan of every row: the
    `worktree:` keys held by retention (`retention:<job>`) whose folder the probe is
    `within` once both are `fold`ed, nearest first; never a detached writer's, an
    attempt's or a gate round's, nor a turn's row. It names every fence the exact
    `retiring` names, and, for a probe spelled as the fences are, nothing more."""
    import unicodedata
    probe = data.draw(FOLD_FOLDER, label="probe")
    swap = lambda text: "".join(c.swapcase() if data.draw(st.booleans()) else c for c in text)   # noqa: E731
    spots = st.one_of(st.sampled_from([probe, *folders.above(probe)]), FOLD_FOLDER,
                      st.sampled_from([probe, *folders.above(probe)]).map(swap),
                      st.sampled_from([probe, *folders.above(probe)]).map(lambda s: unicodedata.normalize("NFC", s)))
    fences = data.draw(st.lists(st.tuples(spots, HOLDER), max_size=10), label="fences")
    turns = data.draw(st.lists(st.tuples(spots, JOB, st.booleans()), max_size=4), label="turns")
    rows = [(folders.exclusive_key(spot), holder) for spot, holder in fences]
    rows += [(folders.turn_key(spot, job, writable=writable), job) for spot, job, writable in turns]
    read = table(rows)
    stored = dict(read("SELECT lease_key, holder FROM leases", ()))
    want = [key for key, holder in stored.items()
            if key.startswith("worktree:") and holder.startswith("retention:")
            and folders.within(folders.fold(probe), folders.fold(key[len("worktree:"):]))]
    found = folders.retiring(read, probe, folded=True)
    assert sorted(found) == sorted(want) and len(found) == len(set(found))
    assert [key.count("/") for key in found] == sorted((key.count("/") for key in found), reverse=True)
    exact = folders.retiring(read, probe)
    assert set(exact) <= set(found)
    if all(folders.fold(key[len("worktree:"):]) != folders.fold(other) or key[len("worktree:"):] == other
           for key in stored for other in (probe, *folders.above(probe))):
        assert set(found) == set(exact)


def test_folded_retiring_reads_no_file_system(monkeypatch):
    """C-8.4: the folded check runs inside the reserving transaction too: string
    operations on the keys it reads, no folder spelled or looked up."""
    import types

    def unexpected(*args, **kwargs):
        raise AssertionError("no filesystem work in the admitting transaction")

    for name in ("canonical", "spelling", "present", "_spelled", "_kernel_path"):
        monkeypatch.setattr(folders, name, unexpected)
    monkeypatch.setattr(folders, "os", types.SimpleNamespace(path=types.SimpleNamespace(dirname=os.path.dirname)))
    read = table([("worktree:/s/Job", "retention:j"), ("worktree:/s/jOB/x", "detached"), ("worktree:/", "retention:k"),
                  (folders.turn_key("/s/Job/x", "t", writable=True), "t")])
    assert folders.retiring(read, "/s/jOB/x/y", folded=True) == ["worktree:/s/Job", "worktree:/"]
    assert folders.retiring(read, "/s/jOB/x/y") == ["worktree:/"]


def test_under_git_finds_a_git_entry_at_or_above_a_folder(tmp_path):
    """`under_git`: a `.git` directory or file (a linked worktree's) on the folder or
    on any folder above it; none beside it or below it."""
    (tmp_path / "repo" / "sub" / "deeper").mkdir(parents=True)
    (tmp_path / "repo" / ".git").mkdir()
    (tmp_path / "linked" / "x").mkdir(parents=True)
    (tmp_path / "linked" / ".git").write_text("gitdir: /elsewhere\n")
    (tmp_path / "plain" / "y").mkdir(parents=True)
    (tmp_path / "plain" / "y" / "z" / ".git").mkdir(parents=True)
    base = os.path.realpath(tmp_path)
    if folders.under_git(base):
        pytest.skip("the test directory is itself inside a git checkout")
    assert folders.under_git(os.path.join(base, "repo", "sub", "deeper"))
    assert folders.under_git(os.path.join(base, "repo"))
    assert folders.under_git(os.path.join(base, "linked", "x"))
    assert not folders.under_git(os.path.join(base, "plain", "y"))
    assert not folders.under_git(os.path.join(base, "repo-beside"))
