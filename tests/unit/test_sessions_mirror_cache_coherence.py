"""C-23.28: known mirror writes invalidate both cooperating inventories."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from subfleet.sessions import mirror
from tests.unit.test_sessions_mirror_hot_progress import (
    ONE, engine, entry, read, rewrite, warm, world,
)


def freeze_directory_stat(monkeypatch, directory):
    """Model directory timestamps too coarse to identify our own rename."""
    original = os.stat
    fixed = original(directory)

    def frozen(path, *args, **kwargs):
        if not isinstance(path, int) and Path(path) == directory:
            return fixed
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", frozen)


def test_inline_hot_invalidates_full_cache_even_when_directory_stat_is_unchanged(world, monkeypatch):
    """C-23.28: a hot write is known evidence of change, even without mtime
    movement; otherwise the stale full copy can undo the user's archive.
    """
    running = engine(world)
    warm(running)
    source, target = entry(world), entry(world, 1)
    freeze_directory_stat(monkeypatch, target.parent)
    original_checkpoint = running._checkpoint
    clock = mirror.time.monotonic
    offset = [0.0]
    monkeypatch.setattr(mirror.time, "monotonic", lambda: clock() + offset[0])
    changed = []

    def checkpoint(current, stage=None):
        if stage == "copying entries" and not changed:
            # The full pass already retained both old copies. Its inline hot
            # service sees the user's new value and rewrites the second copy.
            rewrite(source, isArchived=True)
            changed.append(True)
            offset[0] += 3
        return original_checkpoint(current, stage)

    monkeypatch.setattr(running, "_checkpoint", checkpoint)
    result = running.run_once()
    assert result.state == "ok", result.error
    assert changed
    assert read(source)["isArchived"] and read(target)["isArchived"]
    assert read(running.flags_path)[ONE]["isArchived"], "stale full cache regressed the hot merge base"
    assert running._hot_services >= 1


def test_full_copy_invalidates_hot_cache_even_when_directory_stat_is_unchanged(world, monkeypatch):
    """C-23.28: invalidation also flows from a full pass to its hot inventory."""
    running = engine(world)
    warm(running)
    source, target = entry(world), entry(world, 1)
    worker = running._hot_worker = running._fork_hot()
    freeze_directory_stat(monkeypatch, target.parent)
    rewrite(source, isArchived=True)
    current = mirror.Pass("test")
    assert running._place(source, target, ONE, read(source), "updated", current)
    files, _fresh = worker._scan(target.parent, mirror.Pass("test", kind="hot"), sweep=False)
    if files is None:
        files = worker._files(target.parent)
    assert files[target.name]["isArchived"], "hot inventory retained the full pass's old copy"


def test_slow_directory_listing_services_hot_before_listing_another_entry(world, monkeypatch):
    """C-23.28: a slow scandir iterator cannot postpone hot service until all
    names are collected; GIL contention can delay readdir as well as stat.
    """
    running = engine(world)
    warm(running)
    source, target = entry(world), entry(world, 1)
    running._last_sweep = None
    monkeypatch.setattr(mirror, "LISTING_CHECKPOINT_ENTRIES", 1, raising=False)
    original_scandir, clock = mirror.os.scandir, mirror.time.monotonic
    offset = [0.0]
    monkeypatch.setattr(mirror.time, "monotonic", lambda: clock() + offset[0])
    wrapped, changed, observed = [], [], []

    class SlowListing:
        def __init__(self, listing):
            self.listing = listing

        def __enter__(self):
            self.listing.__enter__()
            return self

        def __exit__(self, *args):
            return self.listing.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            if changed and not observed:
                assert read(target)["isArchived"], "directory listing starved due flag sync"
                assert running.sidecar()["pass"]["state"] == "running"
                observed.append(True)
            item = next(self.listing)
            if item.name.startswith("local_") and not changed:
                rewrite(source, isArchived=True)
                changed.append(True)
                offset[0] += 3
            return item

    def scandir(path):
        listing = original_scandir(path)
        if Path(path) == source.parent and not wrapped:
            wrapped.append(True)
            return SlowListing(listing)
        return listing

    monkeypatch.setattr(mirror.os, "scandir", scandir)
    result = running.run_once()
    assert result.state == "ok", result.error
    assert observed and read(target)["isArchived"]
    assert read(running.flags_path)[ONE]["isArchived"]


def test_cancelled_listing_keeps_a_known_write_invalidated(world, monkeypatch):
    """C-23.28: cancellation cannot turn a stale dirty listing into a complete
    cache hit when directory timestamps did not record the known write.
    """
    running = engine(world)
    warm(running)
    source, target = entry(world), entry(world, 1)
    worker = running._hot_worker = running._fork_hot()
    freeze_directory_stat(monkeypatch, target.parent)
    rewrite(source, isArchived=True)
    assert running._place(source, target, ONE, read(source), "updated", mirror.Pass("test"))
    worker.cancel = threading.Event()
    checkpoint = worker._checkpoint

    def cancel_in_listing(current, stage=None):
        worker.cancel.set()
        return checkpoint(current, stage)

    monkeypatch.setattr(worker, "_checkpoint", cancel_in_listing)
    with pytest.raises(mirror._Cancelled):
        worker._scan(target.parent, mirror.Pass("test", kind="hot"), sweep=False)
    worker.cancel.clear()
    monkeypatch.setattr(worker, "_checkpoint", checkpoint)
    files, _fresh = worker._scan(target.parent, mirror.Pass("test", kind="hot"), sweep=False)
    if files is None:
        files = worker._files(target.parent)
    assert files[target.name]["isArchived"], "cancelled listing erased known-write invalidation"
