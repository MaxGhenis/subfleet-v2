"""R4: parent aliases and old-journal persistence in retention dependency pins."""
from contextlib import contextmanager
import os

import pytest

from subfleet import dependencies
from tests.fake.test_retention_dependency_review_r2 import world, reader, maintain
from tests.unit.retention_world import git
from tests.unit.test_retention_dependency_units import repo


@pytest.mark.xfail(strict=True, reason="R4-F1: initial objects directory aliases are neither recorded nor revalidated")
@pytest.mark.parametrize("state", ["running", "succeeded"], ids=["live", "exporting"])
@pytest.mark.parametrize("entry", ["same-inode", "absent"])
def test_objects_parent_swap_keeps_every_allocated_tree(world, monkeypatch, state, entry):
    trees = [world.job("owner-a"), world.job("owner-b")]
    world.store.update_job("owner-b", worktree=None)
    source = repo(trees[0] / "source")
    (source / "owner-only.txt").write_text("R4 objects parent dependency\n")
    git(source, "add", "owner-only.txt")
    git(source, "commit", "--quiet", "-m", "unique owner commit")
    commit = git(source, "rev-parse", "HEAD")
    outside = repo(world.base / "outside")
    assert git(outside, "rev-parse", "HEAD") != commit
    objects = outside / ".git" / "objects"
    old = world.base / "old-objects"
    objects.rename(old)
    objects.symlink_to(old, target_is_directory=True)
    alternate = objects / "info" / "alternates"
    target = source / ".git" / "objects"
    if entry == "same-inode":
        alternate.write_bytes(b"# first\n# second\n")
        os.link(alternate, target / "info" / "alternates")
        before = alternate.stat()
    original = dependencies.open_regular
    changes = []

    @contextmanager
    def opening(path, *args, **kwargs):
        if entry == "absent" and str(path) == str(alternate):
            try:
                with original(path, *args, **kwargs) as stream:
                    yield stream
            except FileNotFoundError:
                assert not changes
                changes.append(True)
                objects.unlink()
                objects.symlink_to(target, target_is_directory=True)
                raise
            return
        with original(path, *args, **kwargs) as stream:
            if str(path) != str(alternate):
                yield stream
                return

            class Changing:
                def readline(self, size=-1):
                    value = stream.readline(size)
                    if not changes:
                        changes.append(True)
                        objects.unlink()
                        objects.symlink_to(target, target_is_directory=True)
                    return value

                def __getattr__(self, name):
                    return getattr(stream, name)

            yield Changing()

    with monkeypatch.context() as patch:
        patch.setattr(dependencies, "open_regular", opening)
        recorded = dependencies.record(str(outside))
    assert changes
    if entry == "same-inode":
        after = alternate.stat()
        for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
            assert getattr(before, field) == getattr(after, field)
    else:
        assert not alternate.exists()
    assert git(outside, "show", commit + ":owner-only.txt") == "R4 objects parent dependency"
    reader(world, recorded, state=state)
    if state == "succeeded":
        world.store.acquire_lease("out:/outside/result", "borrower")
    result = maintain(world)
    assert not result["pruned"], (recorded, result)
    assert all(tree.is_dir() for tree in trees)
    assert recorded.get("incomplete"), recorded
    assert all("dependency-incomplete" in result["pin_reasons"][owner]
               for owner in ("owner-a", "owner-b"))


@pytest.mark.parametrize("state", ["running", "succeeded"], ids=["live", "exporting"])
@pytest.mark.parametrize("alias", ["dotgit-link", "dotgit-directory", "gitfile", "gitdir-target", "gitdir-parent", "commondir", "common-target", "common-parent", "alternate", "alternate-parent", "checkout-parent"])
@pytest.mark.parametrize("action", ["retarget", "remove"])
def test_changed_repository_spelling_globally_pins_allocations(world, monkeypatch, state, alias, action):
    trees = [world.job("owner-a"), world.job("owner-b")]
    world.store.update_job("owner-b", worktree=None)
    source = repo(trees[0] / "source")
    (source / "unique.txt").write_text("R4 retained dependency\n")
    git(source, "add", "unique.txt")
    git(source, "commit", "--quiet", "-m", "unique dependency")
    commit = git(source, "rev-parse", "HEAD")
    old = repo(world.base / "old")
    oldgit, newgit = old / ".git", source / ".git"
    outside = world.base / "outside"
    named = outside / ".git"
    pointer = False
    target = newgit
    filename = oldgit / "objects" / "info" / "alternates"
    filename.write_bytes(b"# old\n# stable\n")
    if alias == "checkout-parent":
        outside.symlink_to(old, target_is_directory=True)
        named, target = outside, source
    elif alias in ("alternate", "alternate-parent", "dotgit-directory"):
        repo(outside)
        if alias == "dotgit-directory":
            filename = named / "objects" / "info" / "alternates"
            filename.write_bytes(b"# old\n# stable\n")
        else:
            named = world.base / "object-alias"
            named.symlink_to(oldgit / "objects" if alias == "alternate" else oldgit, target_is_directory=True)
            target = newgit / "objects" if alias == "alternate" else newgit
            entry = named if alias == "alternate" else named / "objects"
            (outside / ".git" / "objects" / "info" / "alternates").write_text(str(entry) + "\n")
    else:
        outside.mkdir()
        if alias == "dotgit-link":
            named.symlink_to(oldgit, target_is_directory=True)
        elif alias == "gitfile":
            named.write_text(f"gitdir: {oldgit}\n")
            pointer = True
        elif alias in ("gitdir-target", "gitdir-parent"):
            link = world.base / "git-alias"
            link.symlink_to(oldgit if alias == "gitdir-target" else old, target_is_directory=True)
            named.write_text(f"gitdir: {link if alias == 'gitdir-target' else link / '.git'}\n")
            named = link
            target = newgit if alias == "gitdir-target" else source
        else:
            admin = repo(world.base / "admin") / ".git"
            named.write_text(f"gitdir: {admin}\n")
            named = admin / "commondir"
            if alias == "commondir":
                named.write_text(str(oldgit))
                pointer = True
            else:
                link = world.base / "common-alias"
                link.symlink_to(oldgit if alias == "common-target" else old, target_is_directory=True)
                named.write_text(str(link if alias == "common-target" else link / ".git"))
                named = link
                target = newgit if alias == "common-target" else source
    original = dependencies.open_regular
    changes = []

    @contextmanager
    def opening(path, *args, **kwargs):
        assert not world.store.connection.in_transaction
        with original(path, *args, **kwargs) as stream:
            if str(path) != str(filename):
                yield stream
                return

            class Changing:
                def readline(self, size=-1):
                    line = stream.readline(size)
                    if not changes:
                        changes.append(True)
                        if pointer and action == "retarget":
                            named.write_text(f"gitdir: {target}\n" if alias == "gitfile" else str(target))
                        else:
                            if alias == "dotgit-directory":
                                named.rename(named.with_name("old-git"))
                            else:
                                named.unlink()
                            if action == "retarget":
                                named.symlink_to(target, target_is_directory=True)
                    return line

                def __getattr__(self, name):
                    return getattr(stream, name)

            yield Changing()

    with monkeypatch.context() as patch:
        patch.setattr(dependencies, "open_regular", opening)
        recorded = dependencies.record(str(outside))
    assert changes
    if action == "retarget":
        assert git(outside, "show", commit + ":unique.txt") == "R4 retained dependency"
    reader(world, recorded, state=state)
    if state == "succeeded":
        world.store.acquire_lease("out:/outside/result", "borrower")
    result = maintain(world)
    assert not result["pruned"] and all(tree.is_dir() for tree in trees), (recorded, result)
    assert recorded.get("incomplete"), recorded
    assert all("dependency-incomplete" in result["pin_reasons"][owner]
               for owner in ("owner-a", "owner-b"))


@pytest.mark.parametrize("failure", ["first", "recheck", "both"])
def test_repeated_alias_failures_cannot_be_erased_by_deduplication(tmp_path, monkeypatch, failure):
    path = str(tmp_path / "alias")
    tmp_path.joinpath("alias").mkdir()
    original = dependencies.os.stat
    calls = []

    def failing(named, *args, **kwargs):
        if str(named) == path:
            calls.append(True)
            if (failure in ("first", "both") and len(calls) == 1) or (failure in ("recheck", "both") and len(calls) == 1001):
                raise PermissionError("R4 alias observation denied")
        return original(named, *args, **kwargs)

    monkeypatch.setattr(dependencies.os, "stat", failing)
    aliases = dependencies._Aliases()
    for _ in range(1000):
        aliases.resolve(path)
    errors = []
    aliases.check(errors)
    assert len(aliases.paths) == 1
    assert len(errors) == 1 and errors[0]["file"] == path, errors
    assert "R4 alias observation denied" in errors[0]["reason"]


@pytest.mark.parametrize("state", [
    pytest.param("running", marks=pytest.mark.xfail(
        strict=True, reason="R4-F1: initial objects directory target is not recorded"), id="live"),
    pytest.param("succeeded", id="exporting-control"),
])
def test_stable_objects_alias_keeps_the_tree_git_reads(world, state):
    tree = world.job("owner")
    source = repo(tree / "source")
    (source / "unique.txt").write_text("R4 stable objects alias\n")
    git(source, "add", "unique.txt")
    git(source, "commit", "--quiet", "-m", "unique source")
    commit = git(source, "rev-parse", "HEAD")
    outside = repo(world.base / "outside")
    objects = outside / ".git" / "objects"
    objects.rename(world.base / "old-objects")
    objects.symlink_to(source / ".git" / "objects", target_is_directory=True)
    recorded = dependencies.record(str(outside))
    assert not recorded.get("incomplete"), recorded
    assert git(outside, "show", commit + ":unique.txt") == "R4 stable objects alias"
    reader(world, recorded, state=state)
    if state == "succeeded":
        world.store.acquire_lease("out:/outside/result", "borrower")
    result = maintain(world)
    if state == "succeeded":
        # With complete discovery, a terminal exporter keeps its output paths.
        assert "owner" in result["pruned"] and not tree.exists(), result
    else:
        assert "owner" not in result["pruned"] and tree.is_dir(), (recorded, result)
