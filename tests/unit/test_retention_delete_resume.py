"""Invariant 5: a daemon killed at any step of a deletion, then restarted, ends
in one of two states: the tree whole with no destructive step taken, or the
tree gone with its commits kept (C-8.7, d2660; mutant 7).

`on_step` raises `Killed`, which is not an `Exception`, so nothing in the path
catches it, as nothing runs after a real kill; a new `DeletePath` over the same
store and state root stands for the restarted daemon. Every point the journal
passes is tried, with each repeat of `keeping` (each ref recorded before it is
made, the bundle's folder, its paths), the step between the rename and its
journal write, and entries mid-removal. A kill inside the bundle's
verification, a second kill while the restart finishes, and a kill while the
restart rolls back are tried too.
"""
from __future__ import annotations

import os
from collections import Counter

import pytest

from subfleet import retention_delete as rd
from subfleet import retention_git as rgit
from subfleet import retention_marks as marks
from tests.unit.retention_delete_world import (
    OLD, STEP, commit, delete_path, finish, other_repo, reachable, request, restore_modes, results, run, still_kept,
)
from tests.unit.retention_world import World, git, snapshot, trust_temporary_directories


class Killed(BaseException):
    """The daemon, killed: not an `Exception`, so the path never catches it."""


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    yield w
    w.close()
    restore_modes(tmp_path)


def medium_job(w: World, job_id: str) -> dict:
    """The tree's own registration with an unpushed commit, a nested linked
    worktree of another repository with one, and a standalone repository inside."""
    tree = w.job(job_id, created=OLD)
    commit(tree, "mine.txt", "mine\n")
    other = other_repo(w, f"other-{job_id}")
    git(other, "worktree", "add", "--quiet", "--detach", str(tree / "nested"), "HEAD")
    commit(tree / "nested", "nested.txt", "nested\n")
    inner = tree / "inner"
    git(w.base, "init", "--quiet", "-b", "main", str(inner))
    commit(inner, "inner.txt", "inner\n")
    roots = {".": {"kind": "linked", "dir": tree, "common": w.repo / ".git"},
             "nested": {"kind": "linked", "dir": tree / "nested", "common": other / ".git"},
             "inner": {"kind": "inside", "dir": inner, "common": inner / ".git", "held": False}}
    return {"tree": tree, "roots": roots, "other": other,
            "admins": [w.admin(job_id), other / ".git" / "worktrees" / "nested"]}


#: (point, which time it is reached) for the medium job: `keeping` is reached
#: once for its journal write, once for each of the two refs recorded before
#: it is made, once for the bundle's folder and once for the bundle's paths.
POINTS = [("gating", 1), ("keeping", 1), ("keeping", 2), ("keeping", 3), ("keeping", 4), ("keeping", 5),
          ("kept", 1), ("renaming", 1), ("renamed-unsaved", 1), ("renamed", 1), ("checked", 1),
          ("removing", 1), ("removing-entry", 1), ("removing-entry", 25), ("removed", 1), ("admins", 1),
          ("marking", 1), ("marker-written", 1), ("marked", 1)]
WHOLE = {p for p in POINTS if p[0] in ("gating", "keeping", "kept", "renaming", "renamed-unsaved", "renamed",
                                       "checked")}


def killer(job: str, point: str, occurrence: int):
    seen: Counter = Counter()

    def on_step(job_id: str, at: str) -> None:
        if job_id == job:
            seen[at] += 1
            if at == point and seen[at] == occurrence:
                raise Killed(f"{at} #{occurrence}")
    return on_step


def kill_then_restart(w: World, path: rd.DeletePath, jobs: tuple[str, ...]) -> rd.DeletePath:
    path.start(request(*jobs), mode="apply")
    with pytest.raises(Killed):
        for _ in range(50):
            path.step(**STEP)
    again = delete_path(w)
    assert again.journals == {jobs[0]} and again.pending()
    return again


def whole(w: World, job: dict, view: dict) -> None:
    assert snapshot(job["tree"]) == view
    for common in (w.repo / ".git", job["other"] / ".git"):
        assert git(common, "for-each-ref", "refs/subfleet-kept") == ""
        assert not os.path.lexists(common / "refs" / "subfleet-kept")      # not even an empty namespace
    assert all(admin.exists() for admin in job["admins"])
    assert not (w.root / "jobs" / "job-kill" / "kept-commits").exists()
    assert not marks.marked(w.root, "job-kill")
    assert not (w.root / "retention-delete" / "job-kill").exists()


def gone(w: World, job: dict, before: dict) -> None:
    assert not job["tree"].exists() and not any(admin.exists() for admin in job["admins"])
    still_kept(w, "job-kill", job["roots"], before)
    events = [e for e in w.store.list_events("job-kill") if e["kind"] == "retention.tree_deleted"]
    assert len(events) == 1 and marks.marked(w.root, "job-kill")
    assert not (w.root / "retention-delete" / "job-kill").exists()


def leases(w: World) -> list:
    return [tuple(r) for r in w.store.query("SELECT lease_key,holder FROM leases")]


@pytest.mark.parametrize("point,occurrence", POINTS, ids=[f"{p}-{n}" for p, n in POINTS])
def test_a_kill_at_each_step_ends_whole_or_gone_with_the_commits_kept(world, point, occurrence):
    w = world
    job = medium_job(w, "job-kill")
    w.job("job-next", created=OLD)
    before, view = reachable(job["roots"]), snapshot(job["tree"])
    again = kill_then_restart(w, delete_path(w, on_step=killer("job-kill", point, occurrence)),
                              ("job-kill", "job-next"))
    record = finish(again)
    lines = results(record)
    assert lines["job-next"]["gate"] == "class"                 # the run went on after the restart
    assert leases(w) == [] and again.journals == set()
    if (point, occurrence) in WHOLE:
        whole(w, job, view)
        assert lines["job-kill"]["disposition"] == "interrupted" and record["status"] == "done"
        # Not tried again in that run; the same request, applied again, deletes it.
        retry = results(run(again, request("job-kill", "job-next")))
        assert retry["job-kill"]["disposition"] == "deleted"
    else:
        assert lines["job-kill"]["disposition"] == "deleted" and lines["job-kill"]["resumed"] is True
        assert record["deleted_this_run"] == 1
    gone(w, job, before)


def test_a_kill_inside_the_bundle_check_ends_with_the_tree_whole(world, monkeypatch):
    """Killed after the bundle is written and before it is verified: the
    restart takes away the bundle, its folder and the refs."""
    w = world
    job = medium_job(w, "job-kill")
    view = snapshot(job["tree"])

    def verify(*args, **kwargs):
        raise Killed("verify")
    monkeypatch.setattr(rgit, "verify_bundle", verify)
    path = delete_path(w)
    path.start(request("job-kill"), mode="apply")
    with pytest.raises(Killed):
        path.step(**STEP)
    monkeypatch.undo()
    trust_temporary_directories(monkeypatch)
    again = delete_path(w)
    report = again.step(**STEP)
    assert report["resumed"][0]["rolled_back"] == "keeping"
    whole(w, job, view)
    assert leases(w) == []


def test_a_second_kill_while_the_restart_finishes_is_finished_by_the_next(world):
    w = world
    job = medium_job(w, "job-kill")
    before = reachable(job["roots"])
    again = kill_then_restart(w, delete_path(w, on_step=killer("job-kill", "removing-entry", 10)), ("job-kill",))
    again.on_step = killer("job-kill", "removing-entry", 5)
    with pytest.raises(Killed):
        again.step(**STEP)
    third = delete_path(w)
    record = finish(third)
    assert results(record)["job-kill"]["disposition"] == "deleted"
    gone(w, job, before)
    assert leases(w) == []


def test_a_kill_while_the_restart_rolls_back_is_rolled_back_by_the_next(world, monkeypatch):
    """The rollback is safe to run again: killed while it deletes the refs it
    made, the next restart finishes putting everything back."""
    w = world
    job = medium_job(w, "job-kill")
    view = snapshot(job["tree"])
    again = kill_then_restart(w, delete_path(w, on_step=killer("job-kill", "checked", 1)), ("job-kill",))
    real = rgit.run

    def run_git(args, *a, **kw):
        if list(args[:2]) == ["update-ref", "--no-deref"] and "-d" in args:
            raise Killed("rollback")
        return real(args, *a, **kw)
    monkeypatch.setattr(rgit, "run", run_git)
    with pytest.raises(Killed):
        again.step(**STEP)
    monkeypatch.setattr(rgit, "run", real)
    third = delete_path(w)
    assert third.journals == {"job-kill"}
    report = third.step(**STEP)
    assert report["resumed"][0]["rolled_back"] in rd.STATES
    whole(w, job, view)
    assert leases(w) == []
