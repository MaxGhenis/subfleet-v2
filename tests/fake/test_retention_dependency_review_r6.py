"""R6-F1: a worktrees/<id> name does not imply a shared Git repository."""
import pytest

from subfleet import dependencies, folders
from tests.fake.test_retention_dependency_review_r2 import world, reader, maintain
from tests.fake.test_retention_dependency_review_r5 import source
from tests.unit.retention_world import git
from tests.unit.test_retention_dependency_units import repo


@pytest.mark.parametrize("ending", [None, b"\n", b"\r\n"], ids=["absent", "lf", "crlf"])
@pytest.mark.parametrize("named,state", [
    pytest.param(False, "running", id="directory-live-control"),
    pytest.param(True, "running", id="named-live", marks=pytest.mark.xfail(
        strict=True, reason="R6-F1: empty common target guesses a parent and omits Git's own objects")),
    pytest.param(False, "succeeded", id="directory-exporting-control"),
    pytest.param(True, "succeeded", id="named-exporting-control"),
])
def test_empty_common_target_keeps_the_own_store_git_reads(world, named, state, ending):
    tree = world.job("owner")
    owner, commit = source(world, tree)
    outside = repo(world.base / "outside")
    admin = outside / ".git"
    if named:
        admin = world.base / "container" / "worktrees" / "slot"
        admin.parent.mkdir(parents=True)
        (outside / ".git").rename(admin)
        (outside / ".git").write_text(f"gitdir: {admin}\n")
    # Keep the borrower's existing refs, index and reflogs fully readable too.
    git(owner, "fetch", "--quiet", str(outside), "HEAD")
    (admin / "objects").rename(world.base / "unused-objects")
    (admin / "objects").symlink_to(owner / ".git" / "objects", target_is_directory=True)
    if ending is not None:
        (admin / "commondir").write_bytes(ending)

    # No mutation or incomplete read: this stable repository works in real Git.
    assert git(outside, "fsck", "--no-dangling") == ""
    assert git(outside, "show", commit + ":owner-only.txt") == "R5 owner-only storage"
    recorded = dependencies.record(str(outside))
    assert not recorded.get("incomplete"), recorded
    reader(world, recorded, state=state)
    if state == "succeeded":
        world.store.acquire_lease("out:/outside/result", "borrower")
    result = maintain(world)
    if state == "running":
        assert "owner" not in result["pruned"] and tree.is_dir(), (recorded, result)
        assert folders.canonical(owner / ".git" / "objects") in recorded["git"]
        assert git(outside, "show", commit + ":owner-only.txt") == "R5 owner-only storage"
    else:
        assert "owner" in result["pruned"] and not tree.exists(), result


@pytest.mark.parametrize("layout", ["gitfile", "commondir"])
@pytest.mark.parametrize("padded", [
    pytest.param(False, id="short-control"),
    pytest.param(True, id="padded", marks=pytest.mark.xfail(
        strict=True, reason="R6-F2: pointer read cap drops usable storage without incomplete evidence")),
])
def test_crlf_padded_pointer_keeps_the_storage_real_git_reads(world, layout, padded):
    tree = world.job("owner")
    owner, commit = source(world, tree)
    outside = repo(world.base / "outside")
    git(owner, "fetch", "--quiet", str(outside), "HEAD")
    if layout == "gitfile":
        (outside / ".git").rename(world.base / "unused.git")
        pointer = outside / ".git"
        prefix = b"gitdir: "
    else:
        pointer = outside / ".git" / "commondir"
        prefix = b""
    pointer.write_bytes(prefix + str(owner / ".git").encode()
                       + b"\r\n" * (35000 if padded else 1))
    assert git(outside, "fsck", "--no-dangling") == ""
    assert git(outside, "show", commit + ":owner-only.txt") == "R5 owner-only storage"
    recorded = dependencies.record(str(outside))
    reader(world, recorded)
    result = maintain(world)
    assert "owner" not in result["pruned"] and tree.is_dir(), (recorded, result)
    assert (folders.canonical(owner / ".git" / "objects") in recorded.get("git", [])
            or recorded.get("incomplete")), recorded
    assert git(outside, "show", commit + ":owner-only.txt") == "R5 owner-only storage"
