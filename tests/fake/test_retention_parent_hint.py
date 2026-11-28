"""The inferred parent contributes storage hints without pointer traversal."""
import os

from subfleet import dependencies, folders
from tests.fake.test_retention_dependency_review_r2 import world, reader, maintain
from tests.fake.test_retention_dependency_review_r5 import source
from tests.unit.test_retention_dependency_units import repo
from tests.unit.test_retention_pointer_whitespace import git


def named_repository(world):
    checkout = repo(world.base / "outside")
    parent = world.base / "container"
    admin = parent / "worktrees" / "slot"
    admin.parent.mkdir(parents=True)
    (checkout / ".git").rename(admin)
    (checkout / ".git").write_text(f"gitdir: {admin}\n")
    return checkout, parent


def test_inferred_parent_objects_keeps_its_resolved_storage(world):
    tree = world.job("owner")
    owner, _ = source(world, tree)
    checkout, parent = named_repository(world)
    objects = owner / ".git" / "objects"
    (parent / "objects").symlink_to(objects, target_is_directory=True)
    recorded = dependencies.record(str(checkout))
    assert not recorded.get("incomplete"), recorded
    reader(world, recorded)
    result = maintain(world)
    assert "owner" not in result["pruned"] and tree.is_dir(), (recorded, result)
    assert folders.canonical(objects) in recorded["git"], recorded


def test_unused_parent_oversized_alternates_still_globally_pins(world, record_property):
    tree = world.job("owner")
    checkout, parent = named_repository(world)
    alternates = parent / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True)
    alternates.write_bytes(b"x" * (dependencies._ALTERNATE_LINE_LIMIT + 1))
    health = git("-C", checkout, "fsck", "--no-dangling")
    read = git("-C", checkout, "show", "HEAD:f.txt")
    assert health.returncode == read.returncode == 0
    assert read.stdout == b"outside"
    recorded = dependencies.record(str(checkout))
    assert any(error["file"] == str(alternates) for error in recorded.get("incomplete", [])), recorded
    reader(world, recorded)
    result = maintain(world)
    assert "owner" not in result["pruned"] and tree.is_dir(), (recorded, result)
    assert "dependency-incomplete" in result["pin_reasons"]["owner"], result
    record_property("D-R7-1", dict(pointer=os.fspath(alternates), bytes=alternates.stat().st_size,
                                    recorded=recorded, maintenance=result))
