"""Registration-owned stores stay whole until all their history can be kept."""
from __future__ import annotations

import pytest

from subfleet import retention_delete as rd, retention_git as rgit
from tests.unit.retention_delete_world import delete_path, nested_job, request, results, run
from tests.unit.retention_world import World, git


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(rgit, "temp_roots", lambda: {"/nonexistent-d2660-admin-storage-temp"})
    w = World(tmp_path)
    yield w
    w.close()


@pytest.mark.parametrize("referenced,late", [(False, False), (True, False), (False, True)])
def test_a_registration_owned_nested_store_is_not_discarded(world, referenced, late):
    w = world
    jid = "nonexistent-admin-storage-candidate"
    tree = nested_job(w, jid)
    admin = w.admin(jid)
    store = admin / "modules" / "vendor"
    created = {}

    def make_store():
        git(w.base, "init", "--quiet", "--bare", str(store))
        tree_id = git(store, "mktree")
        created["oid"] = git(store, "commit-tree", tree_id, "-m", "nested registration history")
        git(store, "update-ref", "refs/heads/main", created["oid"])
        git(store, "symbolic-ref", "HEAD", "refs/heads/main")
        if referenced:
            checkout = tree / "vendor"
            checkout.mkdir()
            (checkout / ".git").write_text(f"gitdir: {store}\n")

    if not late:
        make_store()

    def after_tree_removal(job, point):
        if late and job == jid and point == "removed" and not created:
            make_store()

    line = results(run(delete_path(w, on_step=after_tree_removal), request(jid)))[jid]
    assert store.is_dir() and git(store, "rev-parse", "HEAD") == created["oid"]
    if late:
        assert line["disposition"] == "deleted" and not tree.exists(), line
        assert any(item["admin"] == str(admin) and "nested Git storage" in item["why"]
                   for item in line["admins"]["kept"])
        assert rd.marks.marked(w.root, jid)
    else:
        assert line["gate"] == "keep" and line["disposition"] == "failed", line
        assert tree.is_dir() and not rd.marks.marked(w.root, jid)
