"""Regression cases from archive offload review round 1 (C-8.8)."""
from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from subfleet import retention_archive as rarch
from subfleet import retention_fs as rfs
from subfleet import retention_offload as rof
from tests.unit.test_retention_offload import (
    Crash,
    Env,
    PLAIN,
    RICH,
    archives,
    crash_at,
    env,
    install,
    offload,
    outcomes,
    quick_flushes,
    sha,
    tree_state,
    volumes,
)


def _outside(env: Env, name: str) -> Path:
    stub = rof.read_stub(env.root, name)
    assert stub is not None
    return env.volume.mount / stub["path"]


def _interrupted(env: Env, name: str, monkeypatch, step: str) -> Path:
    monkeypatch.setattr(rof, "HOOK", crash_at(step, 1))
    with pytest.raises(Crash):
        offload(env)
    monkeypatch.setattr(rof, "HOOK", None)
    return _outside(env, name)


def _inode(path: Path) -> tuple[int, int]:
    found = path.stat()
    return found.st_dev, found.st_ino


def test_copying_recall_never_drops_the_only_good_bundle(archives, env, monkeypatch):
    name = install(archives.build(RICH), env)
    found = rof.plan(env.root, name)
    outside = _interrupted(env, name, monkeypatch, "copied")
    inside_bundle, outside_bundle = env.archive(name) / found.bundle, outside / found.bundle
    good = sha(inside_bundle)
    assert good is not None and sha(outside_bundle) == good
    assert rof.read_stub(env.root, name)["bundle_sha256"] is None
    inside_bundle.write_bytes(b"damaged internal bundle")

    report = rof.recall(env.root, name, drop_external=True)

    assert outcomes(report) == {name: "failed"}
    assert sha(outside_bundle) == good
    assert rof.read_stub(env.root, name) is not None


def test_shared_target_keeps_bundles_from_two_state_roots(archives, env):
    name = install(archives.build(RICH), env, "shared-name")
    second_root = env.base / "second-state"
    (second_root / "archive").mkdir(parents=True)
    second = Env(env.base, second_root, env.target, env.volume)
    other = dict(RICH, job=[("a", b"a different archived job\n")])
    install(archives.build(other), second, name)
    first_bundle = rof.plan(env.root, name).bundle
    second_bundle = rof.plan(second.root, name).bundle
    first_sha = sha(env.archive(name) / first_bundle)
    second_sha = sha(second.archive(name) / second_bundle)
    assert first_sha != second_sha

    assert outcomes(offload(env)) == {name: "offloaded"}
    first_outside = _outside(env, name)
    assert outcomes(offload(second)) == {name: "offloaded"}
    second_outside = _outside(second, name)

    assert first_outside != second_outside
    assert sha(first_outside / first_bundle) == first_sha
    assert sha(second_outside / second_bundle) == second_sha
    assert rarch.check_archive(env.root, name)["ok"]
    assert rarch.check_archive(second.root, name)["ok"]
    assert outcomes(rof.recall(env.root, name, drop_external=True)) == {name: "recalled"}
    assert sha(second_outside / second_bundle) == second_sha
    assert rarch.check_archive(second.root, name)["ok"]


@pytest.mark.parametrize("step", ["copied", "verified"])
def test_resumed_matching_external_entries_become_private(archives, env, monkeypatch, step):
    name = install(archives.build(RICH), env)
    outside = _interrupted(env, name, monkeypatch, step)
    paths = [outside, outside / "files", env.target / rof.FOLDER]
    files = [path for path in outside.rglob("*") if path.is_file()]
    for path in paths:
        path.chmod(0o755)
    for path in files:
        path.chmod(0o644)

    assert outcomes(offload(env)) == {name: "offloaded"}

    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in paths)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)


@pytest.mark.parametrize("step", ["copied", "verified"])
def test_resumed_matching_external_files_are_fullsynced(archives, env, monkeypatch, step):
    name = install(archives.build(RICH), env)
    outside = _interrupted(env, name, monkeypatch, step)
    expected = {_inode(path) for path in outside.rglob("*") if path.is_file()}
    flushed: set[tuple[int, int]] = set()

    def record(fd: int) -> None:
        found = os.fstat(fd)
        flushed.add((found.st_dev, found.st_ino))

    monkeypatch.setattr(rfs, "fullsync", record)
    assert outcomes(offload(env)) == {name: "offloaded"}
    assert expected <= flushed


def test_recall_restores_modes_of_matching_internal_files(archives, env):
    name = install(archives.build(RICH), env)
    found = rof.plan(env.root, name)
    assert outcomes(offload(env, keep_internal=True)) == {name: "verified"}
    stored = env.archive(name) / "files" / next(iter(found.stores))
    bundle = env.archive(name) / found.bundle
    original_modes = {path: stat.S_IMODE(path.stat().st_mode) for path in (stored, bundle)}
    for path in original_modes:
        path.chmod(0o666)

    assert outcomes(rof.recall(env.root, name)) == {name: "recalled"}
    assert {path: stat.S_IMODE(path.stat().st_mode) for path in original_modes} == original_modes


def test_recall_fullsyncs_matching_internal_files_before_dropping_external(archives, env, monkeypatch):
    name = install(archives.build(RICH), env)
    found = rof.plan(env.root, name)
    assert outcomes(offload(env, keep_internal=True)) == {name: "verified"}
    paths = [env.archive(name) / "files" / store for store in found.stores]
    paths.append(env.archive(name) / found.bundle)
    expected = {_inode(path) for path in paths}
    flushed: set[tuple[int, int]] = set()

    def record(fd: int) -> None:
        found = os.fstat(fd)
        flushed.add((found.st_dev, found.st_ino))

    monkeypatch.setattr(rfs, "fullsync", record)
    assert outcomes(rof.recall(env.root, name, drop_external=True)) == {name: "recalled"}
    assert expected <= flushed


@pytest.mark.parametrize("name,retention_name", [("job", "job"), ("job.2", "job"), ("job.2", "job.2")])
def test_recall_refuses_all_unsettled_archive_names(archives, env, name, retention_name):
    install(archives.build(PLAIN), env, name)
    assert outcomes(offload(env)) == {name: "offloaded"}
    outside = _outside(env, name)
    (env.root / "retention" / retention_name).mkdir(parents=True)
    internal_before, external_before = tree_state(env.archive(name)), tree_state(outside)

    report = rof.recall(env.root, name, drop_external=True)

    assert outcomes(report) == {name: "skipped"}
    assert "retention folder" in report["results"][0]["reason"]
    assert tree_state(env.archive(name)) == internal_before
    assert tree_state(outside) == external_before


def test_recall_cannot_write_through_a_replaced_archive_symlink(archives, env):
    name = install(archives.build(RICH), env)
    assert outcomes(offload(env)) == {name: "offloaded"}
    elsewhere = env.base / "unrelated-directory"
    env.archive(name).rename(elsewhere)
    env.archive(name).symlink_to(elsewhere, target_is_directory=True)
    before = tree_state(elsewhere)

    report = rof.recall(env.root, name, drop_external=True)

    assert outcomes(report) == {name: "failed"}
    assert tree_state(elsewhere) == before


def test_offload_rechecks_archive_directory_after_planning(archives, env, monkeypatch):
    name = install(archives.build(RICH), env)
    elsewhere = env.base / "unrelated-directory"
    original = rof.ArchiveLock.__enter__
    saved: dict[str, object] = {}

    def replaced(lock):
        held = original(lock)
        if lock.name == name:
            env.archive(name).rename(elsewhere)
            env.archive(name).symlink_to(elsewhere, target_is_directory=True)
            saved["before"] = tree_state(elsewhere)
        return held

    monkeypatch.setattr(rof.ArchiveLock, "__enter__", replaced)
    report = offload(env)

    assert outcomes(report) == {name: "failed"}
    assert tree_state(elsewhere) == saved["before"]


def test_offload_cannot_remove_through_archive_replaced_after_verification(archives, env, monkeypatch):
    name = install(archives.build(RICH), env)
    elsewhere = env.base / "unrelated-after-verification"
    saved = {}

    def replace(step):
        if step == "removing":
            env.archive(name).rename(elsewhere)
            env.archive(name).symlink_to(elsewhere, target_is_directory=True)
            saved["before"] = tree_state(elsewhere)

    monkeypatch.setattr(rof, "HOOK", replace)
    report = offload(env)

    assert outcomes(report) == {name: "failed"}
    assert tree_state(elsewhere) == saved["before"]


def test_external_bundle_link_after_hash_prevents_internal_removal(archives, env, monkeypatch):
    name = install(archives.build(RICH), env)
    found = rof.plan(env.root, name)
    original = rof._digest_at
    linked = {"done": False}
    elsewhere = env.base / "outside-target"
    elsewhere.mkdir()
    (elsewhere / found.bundle).write_bytes((env.archive(name) / found.bundle).read_bytes())

    def link_after_hash(dir_fd, leaf, size):
        digest = original(dir_fd, leaf, size)
        if leaf == found.bundle and not linked["done"]:
            stub = rof.read_stub(env.root, name)
            if stub is not None:
                outside = env.volume.mount / stub["path"]
                if outside.exists() and (os.fstat(dir_fd).st_dev, os.fstat(dir_fd).st_ino) == _inode(outside):
                    linked["done"] = True
                    (outside / leaf).unlink()
                    (outside / leaf).symlink_to(elsewhere / leaf)
        return digest

    monkeypatch.setattr(rof, "_digest_at", link_after_hash)
    report = offload(env)

    assert linked["done"]
    assert outcomes(report) == {name: "failed"}
    for store, (_, expected) in found.stores.items():
        assert sha(env.archive(name) / "files" / store) == expected
    assert sha(env.archive(name) / found.bundle) == sha(elsewhere / found.bundle)


def test_interrupted_repair_keeps_resolver_on_verified_internal_copy(archives, env, monkeypatch):
    name = install(archives.build(RICH), env)
    found = rof.plan(env.root, name)
    outside = _interrupted(env, name, monkeypatch, "removing")
    store = next(store for store, (size, _) in found.stores.items() if size)
    (outside / "files" / store).write_bytes(b"damaged external file")
    original = rof._copy_into

    def interrupted(src, leaf, dst, dst_name, mode, **kwargs):
        original(src, leaf, dst, dst_name, mode, **kwargs)
        fd = os.open(dst_name, os.O_WRONLY | os.O_TRUNC, dir_fd=dst)
        try:
            os.write(fd, b"partial repaired file")
        finally:
            os.close(fd)
        raise Crash("during repair")

    monkeypatch.setattr(rof, "_copy_into", interrupted)
    with pytest.raises(Crash):
        offload(env)

    assert not rof.external_authoritative(rof.read_stub(env.root, name))
    assert rarch.check_archive(env.root, name)["ok"]


def test_repair_refuses_to_switch_to_incomplete_internal_copy(archives, env, monkeypatch):
    name = install(archives.build(RICH), env)
    found = rof.plan(env.root, name)
    outside = _interrupted(env, name, monkeypatch, "remove-file")
    store = next(store for store, (size, _) in found.stores.items()
                 if size and (env.archive(name) / "files" / store).exists())
    (outside / "files" / store).write_bytes(b"damaged external file")
    before = tree_state(outside)

    report = offload(env)

    assert outcomes(report) == {name: "failed"}
    assert tree_state(outside) == before
    assert rof.external_authoritative(rof.read_stub(env.root, name))


@pytest.mark.parametrize("matching", [False, True])
def test_unowned_target_bundle_is_never_adopted_or_replaced(archives, env, matching):
    name = install(archives.build(RICH), env)
    found = rof.plan(env.root, name)
    where, refused = rof.target(env.root, env.target, allow_unencrypted=False)
    assert where is not None and not refused
    outside = env.volume.mount / where.path_of(name)
    (outside / "files").mkdir(parents=True)
    bundle = outside / found.bundle
    bundle.write_bytes((env.archive(name) / found.bundle).read_bytes() if matching else b"foreign bundle")
    before = tree_state(outside)

    report = offload(env)

    assert outcomes(report) == {name: "failed"}
    assert tree_state(outside) == before
    assert rarch.check_archive(env.root, name)["ok"]


@pytest.mark.parametrize("matching", [False, True])
def test_replaced_owned_bundle_is_never_adopted_or_overwritten(archives, env, monkeypatch, matching):
    name = install(archives.build(RICH), env)
    found = rof.plan(env.root, name)
    outside = _interrupted(env, name, monkeypatch, "copied")
    bundle = outside / found.bundle
    original = bundle.read_bytes()
    good = sha(env.archive(name) / found.bundle)
    bundle.unlink()
    bundle.write_bytes(original if matching else b"foreign replacement")
    before = tree_state(outside)

    report = offload(env)

    assert outcomes(report) == {name: "failed"}
    assert tree_state(outside) == before
    assert sha(env.archive(name) / found.bundle) == good
    assert rarch.check_archive(env.root, name)["ok"]


def test_drop_external_keeps_unclaimed_file_at_an_archived_store_name(archives, env):
    name = install(archives.build(RICH), env)
    found = rof.plan(env.root, name)
    assert outcomes(offload(env, keep_internal=True)) == {name: "verified"}
    outside = _outside(env, name)
    store = next(iter(found.stores))
    marker = outside / rof.OWNER
    owner = json.loads(marker.read_text())
    del owner["entries"][f"files/{store}"]
    marker.write_text(json.dumps(owner))
    foreign = outside / "files" / store
    foreign.unlink()
    foreign.write_bytes(b"foreign file at a formerly archived path")
    before = tree_state(outside)

    report = rof.recall(env.root, name, drop_external=True)

    assert tree_state(outside) == before
    assert outcomes(report) == {name: "recalled"}
    assert report["results"][0]["external"].startswith("kept")


def test_contents_refuses_external_ownership_mismatch(archives, env, monkeypatch, capsys):
    name = install(archives.build(RICH), env)
    assert outcomes(offload(env)) == {name: "offloaded"}
    outside = _outside(env, name)
    marker = outside / rof.OWNER
    owner = json.loads(marker.read_text())
    owner["source"] = "1" * 64
    marker.write_text(json.dumps(owner))
    before = tree_state(outside)

    with pytest.raises(rof.Failed, match="belongs to another archive or source"):
        rof.contents(env.root, name)

    from subfleet import cli
    monkeypatch.setenv("SUBFLEET_HOME", str(env.root))
    assert cli.main(["retention", "restore", name, "--check", "--json"]) == 1
    output = capsys.readouterr()
    assert "belongs to another archive or source" in output.out + output.err
    assert tree_state(outside) == before


def test_recall_from_copied_state_keeps_original_sources_external_copy(archives, env):
    name = install(archives.build(RICH), env)
    assert outcomes(offload(env)) == {name: "offloaded"}
    outside = _outside(env, name)
    before = tree_state(outside)
    clone = env.base / "cloned-state"
    shutil.copytree(env.root, clone, symlinks=True)

    report = rof.recall(clone, name, drop_external=True)

    assert outcomes(report) == {name: "recalled"}
    assert report["results"][0]["external"].startswith("kept")
    assert tree_state(outside) == before
    assert rarch.check_archive(env.root, name)["ok"]
    assert rarch.check_archive(clone, name)["ok"]


@pytest.mark.parametrize("kind", ["stored", "bundle"])
def test_recall_reuses_verified_temporary_when_external_source_is_damaged(archives, env, monkeypatch, kind):
    name = install(archives.build(RICH), env)
    found = rof.plan(env.root, name)
    assert outcomes(offload(env)) == {name: "offloaded"}
    outside = _outside(env, name)
    if kind == "bundle":
        leaf = found.bundle
        source = outside / leaf
        destination = env.archive(name) / leaf
    else:
        leaf = next(store for store, (size, _) in found.stores.items() if size)
        source = outside / "files" / leaf
        destination = env.archive(name) / "files" / leaf
    temporary = destination.with_name(rof._RECALL + leaf)
    good = sha(source)
    original = rof._copy_into

    def interrupted(src, name_, dst, dst_name, mode, **kwargs):
        original(src, name_, dst, dst_name, mode, **kwargs)
        if dst_name == rof._RECALL + leaf:
            raise Crash("after complete recall copy, before promotion")

    monkeypatch.setattr(rof, "_copy_into", interrupted)
    with pytest.raises(Crash):
        rof.recall(env.root, name, drop_external=True)
    assert good is not None and sha(temporary) == good
    assert not destination.exists()
    source.write_bytes(b"damaged external recall source")
    monkeypatch.setattr(rof, "_copy_into", original)

    report = rof.recall(env.root, name, drop_external=True)

    assert outcomes(report) == {name: "recalled"}
    assert sha(destination) == good
    assert not outside.exists()
    assert rarch.check_archive(env.root, name)["ok"]
