"""Complete linked-admin censuses on temporary stores (d2660).

These are fixture delete-path operations, never executable command-guard
probes. No live store, external repository, network, or home override is used.
"""
from __future__ import annotations

import errno

import pytest

from subfleet import retention_git as rgit
from tests.unit.retention_delete_world import (
    delete_path, nested_job, request, restore_modes, results, run,
)
from tests.unit.retention_world import World, git, snapshot, trust_temporary_directories


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    yield w
    w.close()
    restore_modes(tmp_path)


@pytest.mark.parametrize("pseudo", ["MERGE_HEAD", "FETCH_HEAD"])
def test_outside_admin_keeps_every_uppercase_pseudo_ref_entry(world, pseudo):
    w = world
    job = "nonexistent-outside-pseudo-census"
    tree = nested_job(w, job)
    tree_id = git(w.repo, "rev-parse", "HEAD^{tree}")
    first = git(w.repo, "commit-tree", tree_id, "-m", "first orphan pseudo entry")
    second = git(w.repo, "commit-tree", tree_id, "-m", "uppercase second orphan pseudo entry")
    suffix = "\tnot-for-merge\tfixture branch" if pseudo == "FETCH_HEAD" else ""
    (w.admin(job) / pseudo).write_text(first + suffix + "\n" + second.upper() + suffix + "\n")

    line = results(run(delete_path(w), request(job)))[job]

    assert line["disposition"] == "deleted", line
    assert not tree.exists() and not w.admin(job).exists()
    assert {first, second} <= set(git(w.repo, "rev-list", "--all").split())


@pytest.mark.parametrize("metadata", ["rebase-apply/nonexistent-commit-record", "sequencer/nonexistent-commit-record"])
def test_oversized_admin_metadata_refuses_deletion_without_changing_ordinary_callers(world, metadata):
    w = world
    job = "nonexistent-oversized-admin-census"
    tree = nested_job(w, job)
    record = w.admin(job) / metadata
    record.parent.mkdir(parents=True, exist_ok=True)
    # Sparse fixture: exceed the reader's bound without allocating 256 MiB.
    with record.open("wb") as stream:
        stream.seek(256 * 1024 * 1024)
        stream.write(b"\n")
    before = snapshot(tree)

    line = results(run(delete_path(w), request(job)))[job]

    assert line["disposition"] != "deleted", line
    assert snapshot(tree) == before and w.admin(job).exists()
    # The stricter failure belongs to the delete context; archive callers keep
    # the existing bounded census behavior outside that context.
    assert git(tree, "rev-parse", "HEAD") in rgit.admin_objects(w.admin(job), "sha1")["commit"]


@pytest.mark.parametrize("failure", ["open", "read"])
def test_admin_metadata_io_failure_refuses_deletion(world, monkeypatch, failure):
    w = world
    job = "nonexistent-unreadable-admin-census"
    tree = nested_job(w, job)
    name = "nonexistent-unreadable-commit-record"
    (w.admin(job) / name).write_text(git(tree, "rev-parse", "HEAD") + "\n")
    before = snapshot(tree)
    original_open, original_read = rgit.os.open, rgit.os.read
    failing_handles: set[int] = set()

    def open_metadata(path, flags, *args, **kwargs):
        target = path == name and kwargs.get("dir_fd") is not None
        if target and failure == "open":
            raise OSError(errno.EACCES, "fixture metadata open failure", name)
        handle = original_open(path, flags, *args, **kwargs)
        if target:
            failing_handles.add(handle)
        return handle

    def read_metadata(handle, size):
        if handle in failing_handles:
            failing_handles.remove(handle)
            raise OSError(errno.EIO, "fixture metadata read failure")
        return original_read(handle, size)

    monkeypatch.setattr(rgit.os, "open", open_metadata)
    monkeypatch.setattr(rgit.os, "read", read_metadata)
    line = results(run(delete_path(w), request(job)))[job]

    assert line["disposition"] != "deleted", line
    assert snapshot(tree) == before and w.admin(job).exists()
    if failure == "open":
        assert git(tree, "rev-parse", "HEAD") in rgit.admin_objects(w.admin(job), "sha1")["commit"]
