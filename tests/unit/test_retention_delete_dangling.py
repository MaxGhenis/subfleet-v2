"""Inside stores retain commits that no ref or reflog names."""
from __future__ import annotations

import pytest

from subfleet import retention_delete as rd, retention_git as rgit
from tests.unit.retention_delete_world import commit, delete_path, nested_job, request, results, run
from tests.unit.retention_world import World, git


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(rgit, "temp_roots", lambda: {"/nonexistent-d2660-dangling-temp"})
    w = World(tmp_path)
    yield w
    w.close()


@pytest.mark.parametrize("late", [False, True])
def test_a_truly_dangling_inside_commit_is_kept_or_the_tree_stays_whole(world, late):
    w = world
    jid = "nonexistent-dangling-candidate"
    tree = nested_job(w, jid)
    inner = tree / "inside"
    git(w.base, "init", "--quiet", "-b", "main", str(inner))
    commit(inner, "tracked.txt", "ordinary history\n")
    tree_id = git(inner, "rev-parse", "HEAD^{tree}")
    orphan = {}

    def make_orphan(repository):
        orphan["oid"] = git(repository, "commit-tree", tree_id, "-m", "only an unreferenced object holds this")
        assert orphan["oid"] not in git(repository, "rev-list", "--all", "--reflog").splitlines()

    if not late:
        make_orphan(inner)

    def at_boundary(job, point):
        if late and job == jid and point == "removing-entry" and not orphan:
            moved = w.root / rd.marks.DELETE_DIR / jid / rd.TOMBSTONE / "inside"
            make_orphan(moved)

    line = results(run(delete_path(w, on_step=at_boundary), request(jid)))[jid]
    if late:
        assert line["disposition"] == "failed" and line["gate"] == "keep", line
        assert tree.is_dir() and git(inner, "cat-file", "-t", orphan["oid"]) == "commit"
        assert not rd.marks.marked(w.root, jid)
    else:
        assert line["disposition"] == "deleted", line
        bundle = rd.marks.marker(w.root, jid)["bundles"][0]["path"]
        restored = w.base / "restored-empty"
        git(w.base, "init", "--quiet", "--bare", str(restored))
        git(restored, "fetch", "--quiet", bundle, "+refs/kept/*:refs/kept/*")
        assert orphan["oid"] in git(restored, "rev-list", "--all").splitlines()
