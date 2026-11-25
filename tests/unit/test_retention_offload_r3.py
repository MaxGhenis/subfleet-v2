"""Regression cases from archive offload review round 3 (C-8.8).

Recall needs nothing external when every planned internal entry is there and
verifies, whatever the stub's state (`--drop-external` still tries to remove
the external copy, and keeps it when the volume is gone): a crash at
`removing`, with the volume gone afterwards, must still be recallable. Apart
from that, only a missing or wrong internal entry makes recall open the
external folder, and once removal may have begun it does so before it touches
`files/` or the bundle (`test_a_damaged_ownership_marker_fails_naming_the_file`
in round 2's module, and the guards below).
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from subfleet import retention_archive as rarch
from subfleet import retention_offload as rof
from tests.unit.test_retention_offload import (
    EXT_UUID,
    Crash,
    RICH,
    archives,
    crash_at,
    env,
    install,
    offload,
    outcomes,
    quick_flushes,
    tree_state,
    volumes,
)
from tests.unit.test_retention_offload_r2 import _interrupted, _outside

pytestmark = pytest.mark.retention_catch_up

NOT_MOUNTED = f"volume 'Backup ☃' ({EXT_UUID}), which is not mounted"


def _without_stub(tree: dict | None) -> dict:
    tree = dict(tree or {})
    tree.pop(rof.STUB, None)
    return tree


# --- P2: a complete internal copy is recalled without the volume -----------------------------------

def test_recall_after_a_crash_at_removing_with_the_volume_gone(archives, env, volumes, monkeypatch):
    name = install(archives.build(RICH), env)
    before = tree_state(env.archive(name))
    outside = _interrupted(env, name, monkeypatch, "removing")
    stub = rof.read_stub(env.root, name)
    assert stub["state"] == "verified" and stub["removing"] is True
    assert rof.external_authoritative(stub)
    assert _without_stub(tree_state(env.archive(name))) == before      # nothing internal removed yet
    external_before = tree_state(outside)
    volumes.unmount(EXT_UUID)

    report = rof.recall(env.root, name)

    assert outcomes(report) == {name: "recalled"}, report
    assert report["results"][0]["external"] == "kept"
    assert report["results"][0]["copied_back"] == 0
    assert rof.read_stub(env.root, name) is None
    assert _without_stub(tree_state(env.archive(name))) == before
    assert tree_state(outside) == external_before
    assert rarch.check_archive(env.root, name)["ok"]


def test_an_offloaded_archive_whose_recall_put_everything_back_is_recalled_without_the_volume(
        archives, env, volumes, monkeypatch):
    """`offloaded` too: a recall killed after its last copy, before it marked
    the stub `recalled`, leaves a complete internal copy; the rerun needs
    nothing external, and `--drop-external` keeps the unreachable copy."""
    name = install(archives.build(RICH), env)
    before = tree_state(env.archive(name))
    assert outcomes(offload(env)) == {name: "offloaded"}
    outside = _outside(env, name)
    monkeypatch.setattr(rof, "HOOK", crash_at("recall-bundle", 1))
    with pytest.raises(Crash):
        rof.recall(env.root, name)
    monkeypatch.setattr(rof, "HOOK", None)
    stub = rof.read_stub(env.root, name)
    assert stub["state"] == "offloaded" and not stub.get("recalled") and rof.external_authoritative(stub)
    assert _without_stub(tree_state(env.archive(name))) == before
    external_before = tree_state(outside)
    volumes.unmount(EXT_UUID)

    report = rof.recall(env.root, name, drop_external=True)

    assert outcomes(report) == {name: "recalled"}, report
    assert report["results"][0]["external"].startswith("kept: ") and NOT_MOUNTED in report["results"][0]["external"]
    assert rof.read_stub(env.root, name) is None
    assert _without_stub(tree_state(env.archive(name))) == before
    assert tree_state(outside) == external_before
    assert rarch.check_archive(env.root, name)["ok"]
    # Mounted again, a later offload to that target takes the kept copy over
    # through its ledger, and `--drop-external` then removes it (C-8.8).
    volumes.mount(env.volume.mount)
    assert outcomes(offload(env)) == {name: "offloaded"}
    assert rarch.check_archive(env.root, name)["ok"]
    report = rof.recall(env.root, name, drop_external=True)
    assert outcomes(report) == {name: "recalled"} and report["results"][0]["external"] == "dropped", report
    assert not outside.exists()
    assert _without_stub(tree_state(env.archive(name))) == before


# --- guards: an incomplete internal copy still needs the volume, and nothing internal changes first ---

def _unchanged_failure(env, name: str, volumes, *, why: str) -> None:
    outside = _outside(env, name)
    internal_before, external_before = tree_state(env.archive(name)), tree_state(outside)
    files_before = (env.archive(name) / "files").exists()
    stub_before = rof.stub_path(env.root, name).read_bytes()
    volumes.unmount(EXT_UUID)
    for drop in (False, True):
        report = rof.recall(env.root, name, drop_external=drop)
        assert outcomes(report) == {name: "failed"}, (why, report)
        assert NOT_MOUNTED in report["results"][0]["error"], why
        assert tree_state(env.archive(name)) == internal_before, why
        assert (env.archive(name) / "files").exists() is files_before, why
        assert rof.stub_path(env.root, name).read_bytes() == stub_before, why
        assert tree_state(outside) == external_before, why


def test_an_offloaded_recall_with_the_volume_gone_changes_nothing(archives, env, volumes):
    name = install(archives.build(RICH), env)
    assert outcomes(offload(env)) == {name: "offloaded"}
    assert not (env.archive(name) / "files").exists()
    _unchanged_failure(env, name, volumes, why="offloaded")
    assert not (env.archive(name) / "files").exists()                  # no empty files/ left behind


def test_a_half_removed_internal_copy_still_needs_the_volume(archives, env, volumes, monkeypatch):
    """A crash after the first internal removal: an entry is missing, so
    recall needs the volume, and fails naming it, changing nothing. The
    missing entry is the first one recall reaches, so this holds the outcome,
    not the order; the next test holds the order."""
    name = install(archives.build(RICH), env)
    _interrupted(env, name, monkeypatch, "remove-file")
    stub = rof.read_stub(env.root, name)
    assert stub["state"] == "verified" and stub["removing"] is True
    _unchanged_failure(env, name, volumes, why="remove-file")


def test_a_missing_entry_is_found_before_any_internal_change(archives, env, volumes, monkeypatch):
    """A crash at `removing`, then the last stored file goes and the first
    one's mode changes. Recall must find the gap by reading and fail on the
    volume before it gives the first file its recorded mode back: without
    the check up front it reaches the first file, restores its mode, and
    only then finds the volume gone."""
    name = install(archives.build(RICH), env)
    _interrupted(env, name, monkeypatch, "removing")
    stores = sorted(rof.plan(env.root, name).stores)
    assert len(stores) >= 2, stores
    folder = env.archive(name) / "files"
    first, last = folder / stores[0], folder / stores[-1]
    folder_mode = stat.S_IMODE(folder.stat().st_mode)
    os.chmod(folder, folder_mode | 0o700)
    last.unlink()
    os.chmod(folder, folder_mode)
    os.chmod(first, stat.S_IMODE(first.stat().st_mode) ^ 0o040)
    _unchanged_failure(env, name, volumes, why="missing last file, first file's mode changed")


def test_a_changed_internal_file_still_needs_the_volume(archives, env, volumes, monkeypatch):
    """A crash at `removing`, then one internal stored file changes: it fails
    to verify, so the internal copy is not complete."""
    name = install(archives.build(RICH), env)
    _interrupted(env, name, monkeypatch, "removing")
    found = rof.plan(env.root, name)
    store = sorted(found.stores)[0]
    path = env.archive(name) / "files" / store
    mode = path.stat().st_mode
    os.chmod(path, mode | 0o200)
    data = bytearray(path.read_bytes())
    data[-1:] = bytes([data[-1] ^ 0xFF]) if data else b"x"
    path.write_bytes(bytes(data))
    os.chmod(path, mode)
    _unchanged_failure(env, name, volumes, why="changed file")


def test_a_stub_without_a_bundle_checksum_still_needs_the_volume(archives, env, volumes, monkeypatch):
    """An authoritative stub that records no bundle checksum (an earlier
    build's) counts as failing: its internal bundle cannot be verified."""
    name = install(archives.build(RICH), env)
    _interrupted(env, name, monkeypatch, "removing")
    path = rof.stub_path(env.root, name)
    stub = json.loads(path.read_text())
    assert stub["bundle"] and stub["bundle_sha256"]
    stub["bundle_sha256"] = None
    path.write_text(json.dumps(stub))
    _unchanged_failure(env, name, volumes, why="no bundle checksum")
