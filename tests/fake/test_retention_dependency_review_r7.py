"""C-8.4: an inferred parent is a storage hint, not another Git repository."""
import shutil

import pytest

from subfleet import dependencies, folders
from tests.fake.test_retention_dependency_review_r2 import world, reader, maintain
from tests.unit.test_retention_dependency_units import repo
from tests.unit.test_retention_pointer_whitespace import git


@pytest.mark.parametrize("vanished", [False, True], ids=["valid-repository", "vanished-target"])
@pytest.mark.xfail(strict=True, reason="R7-F1: probing an unused inferred-parent pointer globally pins trees")
def test_unused_parent_pointer_must_not_globally_pin_trees(world, vanished):
    tree = world.job("owner")
    parent = world.base / "container"
    (parent / "worktrees").mkdir(parents=True)
    admin = parent / "worktrees" / "slot"
    checkout = world.base / "outside"
    initialized = git("init", "-q", "--separate-git-dir", admin, checkout)
    assert initialized.returncode == 0, initialized.stderr
    committed = git("-C", checkout, "-c", "user.name=Review", "-c",
                    "user.email=review@example.test", "commit", "--allow-empty", "-qm", "own")
    assert committed.returncode == 0, committed.stderr
    # Git uses admin's own store. This ordinary container is not a repository,
    # and its directory named commondir is unrelated to the checkout.
    (parent / "commondir").mkdir()
    if vanished:
        shutil.rmtree(admin)
    health = git("-C", checkout, "fsck", "--no-dangling")
    read = git("-C", checkout, "show", "HEAD")
    assert (health.returncode == 0) == (not vanished), health.stderr
    assert (read.returncode == 0) == (not vanished), read.stderr
    recorded = dependencies.record(str(checkout))
    reader(world, recorded)
    result = maintain(world)
    assert not recorded.get("incomplete"), (recorded, result)
    assert "owner" in result["pruned"] and not tree.exists(), result


def test_named_own_objects_swap_keeps_actual_storage(world, monkeypatch):
    from tests.fake.test_retention_dependency_review_r5 import source
    from tests.unit.retention_world import git as checked_git

    tree = world.job("owner")
    owner, commit = source(world, tree)
    outside = repo(world.base / "outside")
    checked_git(owner, "fetch", "--quiet", str(outside), "HEAD")
    admin = world.base / "container" / "worktrees" / "slot"
    admin.parent.mkdir(parents=True)
    (outside / ".git").rename(admin)
    (outside / ".git").write_text(f"gitdir: {admin}\n")
    old = world.base / "old-objects"
    objects = admin / "objects"
    objects.rename(old)
    objects.symlink_to(old, target_is_directory=True)
    actual = owner / ".git" / "objects"
    real = dependencies._alternates
    changed = []

    def alternates(path, **kwargs):
        if str(path) == str(objects) and not changed:
            changed.append(True)
            objects.unlink()
            objects.symlink_to(actual, target_is_directory=True)
        return real(path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(dependencies, "_alternates", alternates)
        recorded = dependencies.record(str(outside))
    assert changed
    assert checked_git(outside, "fsck", "--no-dangling") == ""
    assert checked_git(outside, "show", commit + ":owner-only.txt") == "R5 owner-only storage"
    reader(world, recorded)
    result = maintain(world)
    assert "owner" not in result["pruned"] and tree.is_dir(), (recorded, result)
    assert (recorded.get("incomplete")
            or folders.canonical(actual) in recorded.get("git", [])), recorded
