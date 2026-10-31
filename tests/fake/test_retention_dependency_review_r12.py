"""C-8.4, R12-F1: a rejected absolute spelling can still name readable storage."""
import errno
import inspect
import json
import os
from pathlib import Path
import unicodedata

import pytest

from subfleet import dependencies, folders
from tests.fake.test_retention_dependency_review_r2 import world, reader, maintain
from tests.fake.test_retention_dependency_review_r5 import source
from tests.fake.test_retention_dependency_review_r11 import baseline_reader
from tests.unit.test_retention_location_inputs import git


def check_normalization_length(world, monkeypatch, route, module, record_property):
    tree = world.job("owner").resolve()
    original, commit = source(world, tree)
    # NFC is short enough for every discovery syscall. The stored NFD names
    # make canonical's absolute result exceed Darwin's 1024-byte PATH_MAX.
    component_bytes = len(os.fsencode("é" * 35 + "0")) + 1
    count = max(1, (860 - len(os.fsencode(tree)) - 20) // component_bytes)
    names = ["é" * 35 + str(n) for n in range(count)]
    descriptor = os.open(tree, os.O_RDONLY)
    try:
        for name in names:
            physical = unicodedata.normalize("NFD", name)
            os.mkdir(physical, dir_fd=descriptor)
            next_descriptor = os.open(name, os.O_RDONLY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except FileNotFoundError:
        pytest.skip("requires a volume accepting Unicode normalization aliases")
    finally:
        os.close(descriptor)
    checkout = world.base / "outside"
    if route == "alternates":
        moved = tree.joinpath(*names, "source")
        original.rename(moved)
        target = moved / ".git" / "objects"
        assert git("init", "-q", "-b", "main", checkout).returncode == 0
        (checkout / ".git" / "refs" / "heads" / "main").write_text(commit + "\n")
        (checkout / ".git" / "objects" / "info" / "alternates").write_text(str(target) + "\n")
    else:
        assert route == "scan"
        assert git("clone", "-q", "--no-hardlinks", original, checkout).returncode == 0
        assert git("-C", checkout, "gc", "--prune=now").returncode == 0
        target = tree.joinpath(*names, "packs")
        pack = checkout / ".git" / "objects" / "pack"
        pack.rename(target)
        pack.symlink_to(target)
    canonical = folders.canonical
    stale = canonical(target)
    assert len(os.fsencode(target)) < 1024
    with pytest.raises(OSError) as failure:
        os.stat(stale)
    assert failure.value.errno == errno.ENAMETOOLONG
    initial_identity = target.stat()
    before = git("-C", checkout, "show", commit + ":owner-only.txt")
    health = git("-C", checkout, "fsck", "--no-dangling")
    assert before.returncode == health.returncode == 0, (before.stderr, health.stderr)
    changed = []

    def spelling(path):
        value = canonical(path)
        caller = inspect.currentframe().f_back.f_code.co_name
        wanted = "<genexpr>" if route == "alternates" else "scan"
        if os.fspath(path) == str(target) and caller == wanted and not changed:
            descriptor = os.open(tree, os.O_RDONLY)
            try:
                for name in names:
                    physical = unicodedata.normalize("NFD", name)
                    os.rename(physical, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
                    next_descriptor = os.open(name, os.O_RDONLY, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = next_descriptor
            finally:
                os.close(descriptor)
            changed.append(True)
        return value

    with monkeypatch.context() as local:
        local.setattr(folders, "canonical", spelling)
        recorded = module.record(str(checkout))
    assert changed
    assert not recorded.get("incomplete"), recorded
    assert os.path.samestat(initial_identity, target.stat())
    assert not dependencies._spelled_location(stale)
    with pytest.raises(OSError) as failure:
        os.stat(stale)
    assert failure.value.errno == errno.ENAMETOOLONG
    middle = git("-C", checkout, "show", commit + ":owner-only.txt")
    middle_health = git("-C", checkout, "fsck", "--no-dangling")
    assert middle.returncode == middle_health.returncode == 0
    assert middle.stdout == before.stdout
    reader(world, recorded)
    result = maintain(world)
    after = git("-C", checkout, "show", commit + ":owner-only.txt")
    observation = dict(route=route, raw=str(target), canonical=stale,
        raw_bytes=len(os.fsencode(target)), canonical_bytes=len(os.fsencode(stale)),
        stat_errno=errno.ENAMETOOLONG, recorded=recorded, result=result,
        fsck=health.returncode, renamed_fsck=middle_health.returncode,
        before=before.returncode, middle=middle.returncode, after=after.returncode)
    record_property("R12-F1", json.dumps(observation, sort_keys=True))
    assert "owner" not in result["pruned"] and tree.is_dir(), observation
    assert after.returncode == 0 and after.stdout == before.stdout, observation


@pytest.mark.parametrize("route", ["alternates", "scan"])
def test_baseline_keeps_readable_normalization_length(world, monkeypatch, route, record_property):
    check_normalization_length(world, monkeypatch, route, baseline_reader(), record_property)


@pytest.mark.xfail(strict=True, reason="R12-F1: canonical ENAMETOOLONG drops storage readable by its discovered spelling")
@pytest.mark.parametrize("route", ["alternates", "scan"])
def test_filter_keeps_readable_normalization_length(world, monkeypatch, route, record_property):
    check_normalization_length(world, monkeypatch, route, dependencies, record_property)
