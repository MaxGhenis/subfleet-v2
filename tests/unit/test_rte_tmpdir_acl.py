"""D-TMPDIR-ACL1: an entry whose extended ACL denies delete kept an attempt
TMPDIR for ever (C-12.10).

`chmod +a "everyone deny delete"` refuses unlink to the file's owner too.
Removal fixed directory modes but not ACLs, so each sweep renamed the folder
to its tombstone, failed at the first unlink and put it back. Now an unlink or
rmdir inside the tombstone refused with EACCES or EPERM has the ACL that
refuses it (the entry's own, or its directory's `delete_child`) removed,
through a descriptor opened without following a link, and is retried once.

Invariants (each has a test here; the property-based ones draw trees of random
depth, modes 0000 to 0777 on files and directories, symlinks inside and
outside, some entries with deny-delete ACLs):

- 2, all or nothing inside, nothing outside: `_remove` returns True with the
  allocation gone, or False with it back at its name; no file outside changes
  (mode, ACL, flags, content, name), even one a symlink inside points at whose
  ACL denies delete.
- 3, no retry loop: a tree whose only obstacles are deny-delete ACLs on this
  user's entries goes in one pass.
- 5, idempotence: a second sweep over an allocation already removed is a
  no-op and records nothing new.

APFS clones (`cp -cR`) do not carry ACLs, so every fixture sets its own. Each
restores what it set in teardown (`rte_acl.restored`).
"""
from __future__ import annotations

import errno
import logging
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import attempt_tmp
from subfleet import retention_fs as rfs
from tests.unit.rte_acl import (
    DENY_DELETE, DENY_DELETE_CHILD, DENY_WRITEATTR, add_acl, has_acl, outside_state, removes_cleanly,
    restore, restored,  # noqa: F401 - restored: the fixture
)

DAY = 86_400
PROPERTY = settings(max_examples=12, deadline=None, derandomize=True, database=None,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
pytestmark = pytest.mark.skipif(not rfs.ACL_AVAILABLE, reason="needs libSystem's ACL calls (macOS)")


def scratch_in(base: Path, log=None) -> attempt_tmp.AttemptTemp:
    root = base / "user-temp"
    root.mkdir(exist_ok=True)
    return attempt_tmp.AttemptTemp(base / "store", root=root, log=log)


def subject(attempt_id="job/a1"):
    return {"attempt_id": attempt_id, "state": "succeeded"}


class Lines(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def quiet_log(name):
    lines, log = Lines(), logging.getLogger(name)
    log.handlers[:] = [lines]
    log.propagate = False
    return lines, log


# --- the live case ---------------------------------------------------------------------------

def test_two_files_whose_acl_denies_delete_go_with_their_tmpdir_in_one_pass(restored):
    """The live case: a TMPDIR whose two empty files carry `group:everyone deny
    delete` failed removal on every sweep. It goes in one pass now (invariant 3),
    and is not put back."""
    scratch = scratch_in(restored)
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    (folder / "deep" / "er").mkdir(parents=True)
    for i in range(50):
        (folder / "deep" / f"payload-{i}").write_bytes(os.urandom(64))
    for path in (folder / "deep" / "er" / "locked-1", folder / "locked-2"):
        path.write_bytes(b"")
        add_acl(path, DENY_DELETE)
    with pytest.raises(PermissionError):
        os.unlink(folder / "locked-2")            # the obstacle is real: the owner is refused too
    assert scratch.remove(record, subject(), verified_empty=True) == "removed"
    assert scratch.presence(record) == "absent"
    assert not os.path.lexists(folder) and not os.path.lexists(scratch.tombstone_path(record))


@pytest.mark.parametrize("rule", [DENY_DELETE, DENY_DELETE_CHILD, f"{DENY_DELETE},delete_child"])
def test_a_directory_whose_acl_denies_delete_or_delete_child_goes_too(restored, rule):
    """A directory's own `delete` (its rmdir refused) and its `delete_child`
    (each child's unlink refused, the child itself carrying no ACL)."""
    scratch = scratch_in(restored)
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    (folder / "d" / "sub").mkdir(parents=True)
    (folder / "d" / "x").write_text("x")
    (folder / "d" / "sub" / "y").write_text("y")
    add_acl(folder / "d", rule)
    assert scratch._remove(record) is True
    assert scratch.presence(record) == "absent"


def test_the_tombstone_s_own_delete_child_acl_is_cleared_never_the_temp_root_s(restored):
    """The tombstone is the allocation: its `delete_child` ACL is removed. The
    temp root above it is not the allocation's and keeps its ACL; one there
    that refuses the tombstone's rmdir defers removal."""
    scratch = scratch_in(restored)
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    (folder / "x").write_text("x")
    add_acl(folder, DENY_DELETE_CHILD)
    assert scratch._remove(record) is True
    record = scratch.create("job/a2")
    add_acl(scratch.root, DENY_DELETE_CHILD)
    try:
        lines, log = quiet_log("tests.rte.root-acl")
        scratch.log = log
        assert scratch._remove(record) is False
        assert has_acl(scratch.root), "the temp root's ACL is not the allocation's"
        assert scratch.presence(record) == "present"
    finally:
        restore(scratch.root)


def test_a_symlink_whose_own_acl_denies_delete_goes_and_its_outside_target_keeps_its_acl(restored):
    """Mutant 3's witness: the link's own ACL is removed through `O_SYMLINK`,
    never by a call that follows it, so the outside file it names, whose ACL
    also denies delete, keeps its ACL, mode and bytes."""
    scratch = scratch_in(restored)
    outside = restored / "outside"
    outside.mkdir()
    target = outside / "target"
    target.write_text("outside")
    add_acl(target, DENY_DELETE)
    before = outside_state(outside)
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    os.symlink(target, folder / "link")
    os.symlink(outside, folder / "dirlink")
    add_acl(folder / "link", DENY_DELETE)
    add_acl(folder / "dirlink", DENY_DELETE)
    assert has_acl(folder / "link") and has_acl(target)
    assert scratch._remove(record) is True
    assert outside_state(outside) == before
    assert has_acl(target)


def test_a_file_no_open_admits_has_its_acl_cleared_by_name_first(restored):
    """A mode-0000 file can be opened by nothing: its mode is set to exactly
    0600 by name, never through a link, then it is opened, checked and its ACL
    removed through the descriptor."""
    scratch = scratch_in(restored)
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    locked = folder / "locked"
    locked.write_text("x")
    add_acl(locked, DENY_DELETE)
    os.chmod(locked, 0)
    assert scratch._remove(record) is True


def test_a_hard_linked_file_keeps_its_acl_and_removal_defers(restored):
    """An inode with another link may be named outside the allocation: its ACL
    is never removed (clearing it would change the other name's), so removal
    defers and puts the folder back."""
    scratch = scratch_in(restored)
    outside = restored / "outside"
    outside.mkdir()
    (outside / "shared").write_text("shared")
    add_acl(outside / "shared", DENY_DELETE)
    before = outside_state(outside)
    record = scratch.create("job/a1")
    os.link(outside / "shared", Path(record["path"]) / "shared")
    lines, log = quiet_log("tests.rte.hardlink")
    scratch.log = log
    assert scratch._remove(record) is False
    assert scratch.presence(record) == "present"
    assert outside_state(outside) == before
    assert lines.lines == ["TMPDIR removal deferred for job/a1: PermissionError(EACCES)"], lines.lines


def test_only_an_acl_that_refused_an_unlink_is_cleared(restored, monkeypatch):
    """Mutant 5's witness: ACLs are removed after a refusal, from the entry
    refused (or its directory), never from every entry before emptying. A file
    whose ACL refuses no unlink (deny writeattr) is unlinked with it."""
    scratch = scratch_in(restored)
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    (folder / "blocking").write_text("b")
    (folder / "harmless").write_text("h")
    add_acl(folder / "blocking", DENY_DELETE)
    add_acl(folder / "harmless", DENY_WRITEATTR)
    blocking_ino = os.lstat(folder / "blocking").st_ino
    cleared, real = [], rfs.clear_extended_acl

    def spy(fd, **kwargs):
        changed = real(fd, **kwargs)
        if changed:
            cleared.append(os.fstat(fd).st_ino)
        return changed

    monkeypatch.setattr(rfs, "clear_extended_acl", spy)
    assert scratch._remove(record) is True
    assert cleared == [blocking_ino], cleared


def test_an_acl_refusal_is_retried_once_after_its_acl_is_removed(restored, monkeypatch):
    """Mutant 4's witness: the retry follows the ACL's removal; a retry
    without it is refused again. And the call is retried once, not looped."""
    scratch = scratch_in(restored)
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    (folder / "locked").write_text("x")
    add_acl(folder / "locked", DENY_DELETE)
    calls, real = [], os.unlink

    def counted(path, *args, **kwargs):
        calls.append(os.fspath(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", counted)
    assert scratch._remove(record) is True
    assert calls.count("locked") == 2, calls


def test_a_refusal_no_acl_explains_still_defers_and_names_its_errno(restored, monkeypatch):
    """EPERM from an immutable flag (no ACL to remove): removal defers, the
    folder goes back to its name, and the line names the errno, never the
    refusal's words."""
    scratch = scratch_in(restored)
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    (folder / "pinned").write_text("x")
    os.chflags(folder / "pinned", stat.UF_IMMUTABLE)
    lines, log = quiet_log("tests.rte.uchg")
    scratch.log = log
    assert scratch.remove(record, subject(), verified_empty=True) is None
    assert scratch.presence(record) == "present"
    assert lines.lines == ["TMPDIR removal deferred for job/a1: PermissionError(EPERM)"], lines.lines


def acl_entries(path):
    listing = subprocess.run(["/bin/ls", "-led", str(path)], check=True, capture_output=True, text=True)
    return listing.stdout.splitlines()[1:]


@pytest.mark.parametrize("pinned", ["entry", "parent"])
def test_an_immutable_refusal_preserves_unrelated_acls(restored, pinned):
    """EPERM alone is no evidence that a deny-writeattr ACL blocks delete."""
    scratch = scratch_in(restored)
    record = scratch.create("job/a1")
    directory = Path(record["path"]) / "d"
    directory.mkdir()
    entry = directory / "x"
    entry.write_text("x")
    locked, harmless = (entry, directory) if pinned == "entry" else (directory, entry)
    add_acl(harmless, DENY_WRITEATTR)
    before = acl_entries(harmless)
    os.chflags(locked, stat.UF_IMMUTABLE)
    assert scratch._remove(record) is False
    assert acl_entries(harmless) == before


@pytest.mark.parametrize("on_parent", [False, True])
def test_clearing_delete_denials_preserves_other_permissions_and_entries(restored, on_parent):
    """A mixed deny ACE loses only its delete bit; unrelated ACEs stay ordered."""
    directory = restored / "d"
    directory.mkdir()
    entry = directory / "x"
    entry.write_text("x")
    target = directory if on_parent else entry
    permission = "delete_child" if on_parent else "delete"
    add_acl(target, f"everyone deny {permission},writeattr")
    add_acl(target, "everyone allow read")
    before = acl_entries(target)
    fd = os.open(directory, rfs.O_DIR)
    try:
        assert attempt_tmp._clear_blocking_acls(fd, "x", parent_inside=on_parent)
    finally:
        os.close(fd)
    after = acl_entries(target)
    assert len(after) == len(before) == 2
    assert "deny writeattr" in after[0] and permission not in after[0]
    assert after[1] == before[1]


def test_a_file_whose_acl_denies_read_and_delete_is_removed_in_one_pass(restored):
    scratch = scratch_in(restored)
    record = scratch.create("job/a1")
    entry = Path(record["path"]) / "locked"
    entry.write_text("synthetic payload")
    add_acl(entry, "everyone deny read,delete")
    with pytest.raises(PermissionError):
        os.open(entry, rfs.O_FILE)
    assert scratch._remove(record) is True
    assert scratch.presence(record) == "absent"


# --- sweep_unknown: the folder's name, never None ------------------------------------------------

def aged_sighting(scratch):
    """An unknown folder of this store (its marker, no record), sighted a day ago."""
    record = scratch.create("job/gone")
    folder = Path(record["path"])
    notes, removed = {}, []
    scratch.sweep_unknown(lambda p: False, notes.get, lambda r: notes.__setitem__(r["path"], r), removed.append,
                          now=0.0)
    assert str(folder) in notes and not removed
    return folder, notes, removed


def test_sweep_unknown_logs_the_folder_s_name_and_the_errno_never_none(restored):
    """Mutant 6's witness: a sighting records no attempt, so the line named
    "None". It names the folder now, and the errno."""
    lines, log = quiet_log("tests.rte.sweep")
    scratch = scratch_in(restored, log)
    folder, notes, removed = aged_sighting(scratch)
    (folder / "pinned").write_text("x")
    os.chflags(folder / "pinned", stat.UF_IMMUTABLE)
    scratch.sweep_unknown(lambda p: False, notes.get, lambda r: notes.__setitem__(r["path"], r), removed.append,
                          now=DAY + 1)
    assert not removed and folder.is_dir()
    assert lines.lines == [f"TMPDIR removal deferred for {folder.name}: PermissionError(EPERM)"], lines.lines


def test_sweep_unknown_removes_an_acl_locked_folder_and_a_second_sweep_records_nothing(restored):
    """Invariants 3 and 5 through the sweep the live folder is on now: the
    folder goes in one sweep; a second sweep is a no-op (no note, no removal,
    no line)."""
    lines, log = quiet_log("tests.rte.sweep2")
    scratch = scratch_in(restored, log)
    folder, notes, removed = aged_sighting(scratch)
    (folder / "locked").write_text("")
    add_acl(folder / "locked", DENY_DELETE)
    note = lambda r: notes.__setitem__(r["path"], r)  # noqa: E731
    scratch.sweep_unknown(lambda p: False, notes.get, note, removed.append, now=DAY + 1)
    assert [r["path"] for r in removed] == [str(folder)] and not folder.exists()
    snapshot = (dict(notes), list(removed), list(lines.lines))
    scratch.sweep_unknown(lambda p: False, notes.get, note, removed.append, now=DAY + 2)
    assert (dict(notes), list(removed), list(lines.lines)) == snapshot
    assert lines.lines == []


def test_a_second_removal_of_a_removed_allocation_is_a_no_op(restored):
    """Invariant 5 through `remove`: `absent`, and nothing logged."""
    lines, log = quiet_log("tests.rte.idem")
    scratch = scratch_in(restored, log)
    record = scratch.create("job/a1")
    (Path(record["path"]) / "locked").write_text("")
    add_acl(Path(record["path"]) / "locked", DENY_DELETE)
    assert scratch.remove(record, subject(), verified_empty=True) == "removed"
    assert scratch.remove(record, subject(), verified_empty=True) == "absent"
    assert lines.lines == []


# --- the property: generated trees ---------------------------------------------------------------

MODES = st.integers(0, 0o777)
FILE = st.tuples(st.just("f"), MODES, st.booleans())
LINK = st.tuples(st.just("l"), st.sampled_from(["inside", "outside-file", "outside-dir", "dangling"]), st.booleans())
TREES = st.recursive(
    st.one_of(FILE, LINK),
    lambda children: st.tuples(st.just("d"), MODES, st.sampled_from([None, DENY_DELETE, DENY_DELETE_CHILD]),
                               st.lists(children, max_size=3)),
    max_leaves=10)


def build(parent: Path, node, counter, outside: Path, folder: Path, later: list) -> None:
    """Create `node` under `parent`; ACLs and modes go into `later`, applied
    deepest first once every entry exists."""
    counter[0] += 1
    path = parent / f"e{counter[0]}"
    kind = node[0]
    if kind == "f":
        path.write_bytes(os.urandom(8))
        later.append((path, node[1], DENY_DELETE if node[2] else None))
    elif kind == "l":
        target = {"inside": folder, "outside-file": outside / "target", "outside-dir": outside,
                  "dangling": outside / "missing"}[node[1]]
        os.symlink(target, path)
        later.append((path, None, DENY_DELETE if node[2] else None))
    else:
        path.mkdir()
        for child in node[3]:
            build(path, child, counter, outside, folder, later)
        later.append((path, node[1], node[2]))


def generated(base: Path, children, pinned: bool):
    scratch = scratch_in(base, quiet_log(f"tests.rte.prop.{base.name}")[1])
    outside = base / "outside"
    outside.mkdir()
    (outside / "target").write_text("outside")
    add_acl(outside / "target", DENY_DELETE)
    record = scratch.create("job/a1")
    folder = Path(record["path"])
    later: list = []
    counter = [0]
    for child in children:
        build(folder, child, counter, outside, folder, later)
    files = [path for path, mode, _ in later if mode is not None and path.is_file() and not path.is_symlink()]
    pin = files[0] if pinned and files else None
    for path, mode, rule in later:                  # children before their directory
        if rule is not None:
            add_acl(path, rule)
        if path == pin:
            os.chflags(path, stat.UF_IMMUTABLE)     # no mode change reaches an immutable file
        elif mode is not None:
            os.chmod(path, mode, follow_symlinks=False)
    return scratch, record, folder, outside, pinned and bool(files)


@PROPERTY
@given(st.lists(TREES, max_size=4), st.booleans())
def test_removal_is_all_or_nothing_inside_and_changes_nothing_outside(tmp_path, children, pin):
    """Invariants 2 and 3 over generated trees: with only modes and deny-delete
    ACLs in the way, removal goes in one pass; with an immutable file it
    returns False and the allocation is back at its name; either way nothing
    outside changes, the ACL'd file the symlinks point at included."""
    base = Path(tempfile.mkdtemp(prefix="rte-", dir=tmp_path))
    try:
        scratch, record, folder, outside, pinned = generated(base, children, pin)
        before = outside_state(outside)
        removed = scratch._remove(record)
        assert outside_state(outside) == before
        if pinned:
            assert removed is False and scratch.presence(record) == "present"
        else:
            assert removed is True and scratch.presence(record) == "absent"
            assert sorted(p.name for p in scratch.root.iterdir()) == []
    finally:
        assert removes_cleanly(base)


@PROPERTY
@given(st.lists(TREES, max_size=4))
def test_walk_finishes_or_raises_tree_error_never_permission_error(tmp_path, children):
    """Invariant 1 over generated trees: `walk` finishes or raises
    `TreeError`, never a bare `PermissionError` (D-SURVEY-EACCES1)."""
    base = Path(tempfile.mkdtemp(prefix="rte-walk-", dir=tmp_path))
    try:
        _, _, folder, _, _ = generated(base, children, False)
        fd = os.open(folder, rfs.O_DIR)
        try:
            try:
                for _ in rfs.walk(fd):
                    pass
            except rfs.TreeError as exc:
                assert exc.reason in ("unreadable", "changed"), exc
            except PermissionError as exc:          # pragma: no cover - the defect
                pytest.fail(f"bare PermissionError from walk: {exc!r}")
        finally:
            os.close(fd)
    finally:
        assert removes_cleanly(base)


def test_restore_leaves_every_blocking_fixture_removable(tmp_path):
    """The fixture rule: a tree with deny-delete ACLs, an immutable file and
    modes without owner bits removes cleanly once `restore` ran."""
    base = tmp_path / "blocking"
    (base / "d" / "e").mkdir(parents=True)
    (base / "d" / "e" / "f").write_text("x")
    (base / "d" / "g").write_text("y")
    os.symlink("/nonexistent-rte-target", base / "d" / "link")
    add_acl(base / "d" / "g", DENY_DELETE)
    add_acl(base / "d" / "link", DENY_DELETE)
    add_acl(base / "d", DENY_DELETE_CHILD)
    os.chflags(base / "d" / "e" / "f", stat.UF_IMMUTABLE)
    os.chmod(base / "d" / "e", 0)
    os.chmod(base / "d", 0o500)
    with pytest.raises(OSError) as refused:
        os.unlink(base / "d" / "g")
    assert refused.value.errno in (errno.EACCES, errno.EPERM)
    assert removes_cleanly(base)
