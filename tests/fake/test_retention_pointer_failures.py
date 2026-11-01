"""Pointer read failures fail retirement closed; genuinely missing paths do not."""
import errno

import pytest

from subfleet import dependencies
from subfleet.sessions.transcripts import TooLarge
from tests.fake.test_retention_dependency_review_r2 import world, reader, maintain


@pytest.mark.parametrize("defect", ["dangling", "oversized", "permission", "oserror", "value"])
def test_unreadable_common_pointer_keeps_allocated_trees(world, monkeypatch, defect):
    tree = world.job("owner")
    admin = world.base / "outside" / ".git"
    admin.mkdir(parents=True)
    pointer = admin / "commondir"
    if defect == "dangling":
        pointer.symlink_to(world.base / "gone")
    elif defect == "oversized":
        pointer.write_bytes(b"\r\n" * (dependencies._FILE_LIMIT // 2 + 1))
        with pytest.raises(TooLarge):
            dependencies.read_regular(pointer, limit=dependencies._FILE_LIMIT)
    else:
        pointer.write_bytes(b"../common\n")
        real = dependencies.read_regular
        error = {"permission": PermissionError(errno.EACCES, "fixture denied"),
                 "oserror": OSError(errno.EIO, "fixture interrupted"),
                 "value": ValueError("fixture invalid read")}[defect]

        def reading(path, **kwargs):
            if str(path) == str(pointer):
                raise error
            return real(path, **kwargs)

        monkeypatch.setattr(dependencies, "read_regular", reading)
    recorded = dependencies.record(str(admin.parent))
    assert any(error["file"] == str(pointer) for error in recorded.get("incomplete", [])), recorded
    reader(world, recorded)
    result = maintain(world)
    assert not result["pruned"] and tree.is_dir(), result
    assert str(pointer) in result["pin_reasons"]["owner"]


def test_stale_gitfile_with_vanished_target_still_retires(world):
    tree = world.job("owner")
    outside = world.base / "outside"
    outside.mkdir()
    vanished = world.base / "vanished"
    vanished.mkdir()
    (outside / ".git").write_text(f"gitdir: {vanished}\n")
    vanished.rmdir()
    recorded = dependencies.record(str(outside))
    assert not recorded.get("incomplete"), recorded
    reader(world, recorded)
    result = maintain(world)
    assert "owner" in result["pruned"] and not tree.exists(), result


def test_pointer_beneath_a_file_is_missing_evidence(tmp_path):
    parent = tmp_path / "file"
    parent.write_bytes(b"not a directory")
    aliases = dependencies._Aliases()
    assert aliases.text(str(parent / "commondir")) is None
    incomplete = []
    aliases.check(incomplete)
    assert incomplete == []
