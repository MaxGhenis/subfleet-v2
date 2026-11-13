"""A partial fixture deletion consumes a canary until recovery finishes."""
from __future__ import annotations

import errno

import pytest

from subfleet import retention_delete as rd, retention_git as rgit
from tests.unit.retention_delete_world import delete_path, nested_job, request, STEP
from tests.unit.retention_world import World, Clock


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(rgit, "temp_roots", lambda: {"/nonexistent-d2660-canary-journal-temp"})
    w = World(tmp_path)
    yield w
    w.close()


def test_a_limited_apply_waits_for_failed_partial_deletion(world, monkeypatch):
    w = world
    first, second = "nonexistent-canary-first", "nonexistent-canary-second"
    nested_job(w, first)
    second_tree = nested_job(w, second)
    state = {"removing": False, "faulted": False}

    def entered(job, point):
        if job == first and point == "removing":
            state["removing"] = True

    unlink = rd.os.unlink

    def fail_after_unlink(name, *args, **kwargs):
        result = unlink(name, *args, **kwargs)
        if state["removing"] and not state["faulted"] and kwargs.get("dir_fd") is not None:
            state["faulted"] = True
            raise OSError(errno.EIO, "fixture interruption after the first actual unlink")
        return result

    monkeypatch.setattr(rd.os, "unlink", fail_after_unlink)
    clock = Clock()
    path = delete_path(w, on_step=entered, clock=clock)
    path.start(request(first, second), mode="apply", limit=1)
    path.step(**STEP)
    assert state["faulted"] and first in path.journals
    assert path.apply["results"][first]["disposition"] == "failed"
    report = path.step(**STEP)
    assert report["waiting_for_journals"] == [first]
    assert path.apply["results"][second] is None
    assert second_tree.is_dir() and not rd.marks.marked(w.root, second)
    clock.advance(rd.RETRY_S + 1)
    path.step(**STEP)
    assert path.apply["status"] == "done-limit" and path.apply["deleted_this_run"] == 1
    assert rd.marks.marked(w.root, first) and not rd.marks.marked(w.root, second)
    assert second_tree.is_dir()
