"""D-TMPDIR-ACL1, change 4: retention's own deleter against a deny-delete ACL.

`rfs.Reclaim` deletes only entries a verified manifest lists unchanged and
sets everything else aside under `conflicts/<label>/`. A file whose ACL
denies delete (`chmod +a "everyone deny delete"`, the live case's shape)
refuses its unlink to its owner. These tests settle what Reclaim does with
one today:

- the unlink is refused (EACCES), and so is the set-aside rename of the file
  itself: on macOS a rename needs delete on its source, so the file stays
  where it is ("in place");
- its directory then fails `rmdir` (not empty) and is renamed aside whole: a
  directory without the ACL can be moved, its contents with it;
- `Reclaim.run` returns True: the tree is gone from its place, and the file
  and its ACL sit in `retention-conflicts/<job>/`, reported as kept, with a
  `retention.conflict` event.

So Reclaim finishes, and the brief's condition for clearing ACLs in Reclaim
("if Reclaim can't finish") is not met: Reclaim is unchanged. The archive's
copy of such a file carries no ACL (a clone does not copy it), so clearing
one before an unlink would lose nothing the archive keeps.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from subfleet import retention
from subfleet import retention_fs as rfs
from tests.unit.retention_world import World, trust_temporary_directories
from tests.unit.rte_acl import add_acl, has_acl, restore, restored  # noqa: F401 - restored: the fixture

pytestmark = pytest.mark.skipif(not rfs.ACL_AVAILABLE, reason="needs libSystem's ACL calls (macOS)")


def entries_of(root: Path) -> dict[str, dict]:
    """A manifest's entries for `root` as it stands: every entry's signature."""
    out = {}
    for directory, dirnames, filenames in os.walk(root):
        for name in [".", *dirnames, *filenames]:
            full = os.path.normpath(os.path.join(directory, name))
            rel = os.path.relpath(full, root)
            rel = "" if rel == "." else rel
            out[rel] = {"p": rel, "sig": rfs.signature(os.lstat(full))}
    return out


def test_reclaim_sets_a_deny_delete_file_aside_with_its_directory_and_finishes(restored):
    tree = restored / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "top.txt").write_text("top")
    (tree / "sub" / "other.txt").write_text("other")
    (tree / "sub" / "locked.txt").write_text("locked")
    (tree / "locked-top.txt").write_text("locked top")
    for path in (tree / "sub" / "locked.txt", tree / "locked-top.txt"):
        add_acl(path)                        # before the signatures: an ACL moves the ctime
    entries = entries_of(tree)
    conflicts = restored / "retention-conflicts" / "job-r"
    reclaim = rfs.Reclaim(entries, conflicts, "worktree")

    assert reclaim.run(tree) is True
    assert not os.path.lexists(tree)
    assert reclaim.deleted == 2, "only the listed, unchanged files without the ACL are unlinked"
    # Each locked file's own set-aside rename was refused: it stayed in place.
    own = {(k["path"], k["reason"], k["where"]) for k in reclaim.kept if k["path"].endswith("locked.txt")
           or k["path"] == "locked-top.txt"}
    assert own == {("sub/locked.txt", "unlink-failed", "in place"), ("locked-top.txt", "unlink-failed", "in place")}
    assert {e["error"] for e in reclaim.errors} == {"Permission denied"}
    assert {e["path"] for e in reclaim.errors} == {"sub/locked.txt", "locked-top.txt"}
    # The directories went aside whole, the locked files and their ACLs with them.
    moved = {k["path"]: k for k in reclaim.kept if k["where"] != "in place"}
    assert moved["sub"]["reason"] == "not-empty" and moved[""]["reason"] == "remainder"
    sub_aside = conflicts / moved["sub"]["where"].replace("//", "/")
    root_aside = conflicts / moved[""]["where"].replace("//", "/")
    assert (sub_aside / "locked.txt").read_text() == "locked" and has_acl(sub_aside / "locked.txt")
    assert (root_aside / "locked-top.txt").read_text() == "locked top" and has_acl(root_aside / "locked-top.txt")
    assert sorted(p.name for p in root_aside.iterdir()) == ["locked-top.txt"]
    assert sorted(p.name for p in sub_aside.iterdir()) == ["locked.txt"]


def test_a_rename_of_a_file_whose_acl_denies_delete_is_refused(restored):
    """Why the set-aside stays in place: macOS checks delete on a rename's source."""
    (restored / "aside").mkdir()
    locked = restored / "locked.txt"
    locked.write_text("x")
    add_acl(locked)
    with pytest.raises(PermissionError) as refused:
        os.rename(locked, restored / "aside" / "locked.txt")
    assert refused.value.errno == errno.EACCES
    holder = restored / "holder"
    holder.mkdir()
    os.rename(locked.parent / "aside", holder / "aside")      # a directory without the ACL moves
    assert locked.exists() and has_acl(locked)


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    yield w
    w.close()
    restore(tmp_path)                        # the ACLs go even when the test failed


def test_a_retired_job_s_deny_delete_file_ends_in_retention_conflicts_and_the_pass_finishes(world):
    """End to end: the job is retired and reclaimed; the file and its ACL are
    in `retention-conflicts/<job>/`, reported as kept; the archive's copy
    of it carries no ACL."""
    w = world
    wt = w.job("job-acl")
    (wt / "notes").mkdir()
    locked = wt / "notes" / "locked.txt"
    locked.write_bytes(b"kept by an ACL")
    add_acl(locked)
    result = retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0, holders=lambda watches, **_: {},
                                   measure_s=0)
    assert result["pruned"] == ["job-acl"], result
    assert not os.path.lexists(wt)
    conflicts = w.root / "retention-conflicts" / "job-acl"
    aside = [Path(d) / n for d, _, files in os.walk(conflicts) for n in files if n == "locked.txt"]
    assert len(aside) == 1 and aside[0].read_bytes() == b"kept by an ACL" and has_acl(aside[0]), aside
    events = [json.loads(e["data_json"] or "{}") for e in w.store.list_events("job-acl")
              if e["kind"] == "retention.conflict"]
    kept = [k for data in events for k in data.get("kept", [])]
    assert any(k["path"] == "notes/locked.txt" and k["tree"] == "worktree" for k in kept), events
    archive = w.root / "archive"
    stored = [Path(d) / n for d, _, files in os.walk(archive) for n in files]
    assert stored and not any(has_acl(p) for p in stored), "the archive's clones carry no ACL"
