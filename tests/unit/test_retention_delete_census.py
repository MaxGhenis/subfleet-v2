"""Regressions for the independently reproduced d2660 snapshot/restart gaps.

These are real functional operations on newly created fixture trees only.
Command-guard probes are not executed and no live store is read.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import retention_delete as rd, retention_git as rgit
from tests.unit.retention_delete_world import commit, delete_path, nested_job, request, results, run
from tests.unit.retention_world import World, git


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(rgit, "temp_roots", lambda: {"/nonexistent-d2660-fixture-temp"})
    w = World(tmp_path)
    yield w
    w.close()


@pytest.mark.parametrize("point", ["kept", "checked", "removing", "removing-entry"])
def test_a_repository_added_after_the_keep_snapshot_is_not_deleted(world, point):
    w = world
    job_id = "nonexistent-late-repository"
    tree = nested_job(w, job_id)
    added = {}

    def add_repository(job, at):
        if job == job_id and at == point and not added:
            late = tree / "late" if tree.exists() else w.root / rd.marks.DELETE_DIR / job_id / "tree" / "late"
            git(w.base, "init", "--quiet", "-b", "main", str(late))
            added["oid"] = commit(late, "unique.txt", "this commit has no other home\n")

    record = run(delete_path(w, on_step=add_repository), request(job_id))
    line = results(record)[job_id]
    assert line["disposition"] == "failed" and line["gate"] == "keep", line
    assert tree.is_dir() and git(tree / "late", "rev-parse", "HEAD") == added["oid"]
    assert not rd.marks.marked(w.root, job_id)
    assert not [e for e in w.store.list_events(job_id) if e["kind"] == rd.EVENT]


@pytest.mark.parametrize("point", ["removing", "removing-entry"])
def test_a_replaced_tombstone_is_never_removed(world, point):
    w = world
    job_id = "nonexistent-replaced-tombstone"
    nested_job(w, job_id)
    tombstone = w.root / rd.marks.DELETE_DIR / job_id / "tree"
    preserved = w.base / "preserved-original"
    seen = {}

    def replace(job, at):
        if job == job_id and at == point and not seen:
            seen["original"] = tombstone.stat().st_ino
            tombstone.rename(preserved)
            tombstone.mkdir()
            (tombstone / "unrelated.txt").write_text("unrelated replacement data\n")
            seen["replacement"] = tombstone.stat().st_ino

    path = delete_path(w, on_step=replace)
    line = results(run(path, request(job_id)))[job_id]
    assert seen["original"] != seen["replacement"]
    assert line["disposition"] == "failed", line
    assert (tombstone / "unrelated.txt").read_text() == "unrelated replacement data\n"
    assert preserved.is_dir() and not rd.marks.marked(w.root, job_id)
    # A restart must also leave the unrelated replacement untouched.
    again = delete_path(w)
    again.step(remote_less_history_bytes=10 ** 12)
    assert (tombstone / "unrelated.txt").read_text() == "unrelated replacement data\n"
    assert not rd.marks.marked(w.root, job_id)


def test_a_marker_and_event_keep_identical_facts_after_event_commit_crash(world):
    w = world
    job_id = "nonexistent-marker-restart"
    nested_job(w, job_id)
    now = [datetime(2026, 11, 12, tzinfo=UTC)]
    path = delete_path(w, now=lambda: now[0])
    saved = path._saved

    class Interrupted(BaseException):
        pass

    def before_marked(journal, state, **changes):
        if state == "marked":
            raise Interrupted()
        return saved(journal, state, **changes)

    path._saved = before_marked
    with pytest.raises(Interrupted):
        run(path, request(job_id))
    marker = rd.marks.marker(w.root, job_id)
    now[0] += timedelta(seconds=60)
    again = delete_path(w, now=lambda: now[0])
    again.step(remote_less_history_bytes=10 ** 12)
    events = [e for e in w.store.list_events(job_id) if e["kind"] == rd.EVENT]
    assert len(events) == 1
    assert rd.marks.marker(w.root, job_id) == marker == json.loads(events[0]["data_json"])


def test_a_failed_journal_write_does_not_advance_in_memory_state(world, monkeypatch):
    w = world
    path = delete_path(w)
    job_id = "nonexistent-journal-write"
    journal = {"job_id": job_id, "state": "checked"}

    def unavailable(*args, **kwargs):
        raise OSError("fixture write unavailable")

    monkeypatch.setattr(rd.rfs, "write_atomic", unavailable)
    with pytest.raises(OSError):
        path._saved(journal, rd.DESTRUCTIVE)
    assert journal == {"job_id": job_id, "state": "checked"}


@given(point=st.sampled_from(["kept", "checked", "removing", "removing-entry"]), depth=st.integers(min_value=0, max_value=2))
@settings(max_examples=6, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_late_repository_conservation_property(tmp_path, monkeypatch, point, depth):
    monkeypatch.setattr(rgit, "temp_roots", lambda: {"/nonexistent-d2660-fixture-temp"})
    case = tmp_path / f"case-{len(os.listdir(tmp_path))}"
    case.mkdir()
    w = World(case)
    try:
        job_id = "nonexistent-late-property"
        tree = nested_job(w, job_id)
        created = {}
        relative = "/".join(["deep"] * depth + ["late"])

        def add(job, at):
            if job != job_id or at != point or created:
                return
            root = tree if tree.exists() else w.root / rd.marks.DELETE_DIR / job_id / "tree"
            late = root / relative
            late.parent.mkdir(parents=True, exist_ok=True)
            git(w.base, "init", "--quiet", "-b", "main", str(late))
            created["oid"] = commit(late, "unique.txt", "a late commit must survive\n")

        line = results(run(delete_path(w, on_step=add), request(job_id)))[job_id]
        assert line["disposition"] == "failed" and tree.is_dir(), line
        assert git(tree / relative, "rev-parse", "HEAD") == created["oid"]
        assert not rd.marks.marked(w.root, job_id)
    finally:
        w.close()
