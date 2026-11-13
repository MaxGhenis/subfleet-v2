"""Root Git storage and registration-owned raw-object retention regressions.

All operations are on temporary fixture repositories. These are functional
delete-path cases, not executable command-guard probes or live cleanup.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from subfleet import retention_delete as rd
from tests.unit.retention_delete_world import delete_path, nested_job, request, restore_modes, results, run
from tests.unit.retention_world import World, git, snapshot, trust_temporary_directories


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    yield w
    w.close()
    restore_modes(tmp_path)


def test_a_bare_store_at_the_candidate_root_keeps_its_unique_orphan(world):
    w = world
    job = "nonexistent-root-bare-store"
    tree = nested_job(w, job)
    git(w.base, "init", "--quiet", "--bare", str(tree))
    empty = git(w.base, f"--git-dir={tree}", "mktree")
    orphan = git(w.base, f"--git-dir={tree}", "commit-tree", empty, "-m", "unique root-only orphan")
    assert any(root.rel == "." and root.kind == "gitdir" for root in rd.scan_tree(tree).roots)

    line = results(run(delete_path(w), request(job)))[job]

    assert line["disposition"] == "deleted", line
    assert not tree.exists()
    marker = json.loads((w.root / "jobs" / job / "tree-deleted.json").read_text())
    bundle = next(item for item in marker["bundles"] if item["repository"] == str(tree))
    restored = w.base / "restored-root-store.git"
    git(w.base, "init", "--quiet", "--bare", str(restored))
    git(w.base, f"--git-dir={restored}", "bundle", "verify", bundle["path"])
    git(w.base, f"--git-dir={restored}", "fetch", "--quiet", bundle["path"], "+refs/*:refs/restored/*")
    assert orphan in git(w.base, f"--git-dir={restored}", "rev-list", "--all").split()


def prepare_borrowed_commit(w: World, admin: Path) -> tuple[Path, str]:
    source = w.base / "nonexistent-borrowed-commit-source.git"
    git(w.base, "init", "--quiet", "--bare", str(source))
    empty = git(w.base, f"--git-dir={source}", "mktree")
    oid = git(w.base, f"--git-dir={source}", "commit-tree", empty, "-m", "unique borrowed commit")
    objects = admin / "borrowed-objects"
    for value in (empty, oid):
        target = objects / value[:2] / value[2:]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / "objects" / value[:2] / value[2:], target)
    assert rd.scan_tree(objects).roots == []
    return objects, oid


@pytest.mark.parametrize("late,transitive", [(False, False), (False, True), (True, False), (True, True)])
def test_raw_objects_borrowed_from_a_removable_admin_are_preserved(world, late, transitive):
    w = world
    job = "nonexistent-admin-raw-object-borrower"
    tree = nested_job(w, job)
    admin = w.admin(job)
    common = w.repo / ".git"
    info = common / "objects" / "info"
    info.mkdir(exist_ok=True)
    state = {}

    def add_borrower():
        objects, oid = prepare_borrowed_commit(w, admin)
        if transitive:
            middle = w.base / "intermediate-objects"
            (middle / "info").mkdir(parents=True)
            (middle / "info" / "alternates").write_text(str(objects) + "\n")
            target = middle
        else:
            target = objects
        (info / "alternates").write_text(str(target) + "\n")
        # Keep the object named by the surviving repository. Initial cases
        # also make it the outer registration's HEAD, matching the loss case.
        git(w.repo, "update-ref", "refs/heads/nonexistent-borrowed-history", oid)
        if not late:
            (admin / "HEAD").write_text(oid + "\n")
        state.update(objects=objects, oid=oid)

    if not late:
        add_borrower()
    before = snapshot(tree)

    def after_tree_removal(job_id, point):
        if late and job_id == job and point == "removed" and not state:
            add_borrower()

    line = results(run(delete_path(w, on_step=after_tree_removal), request(job)))[job]

    assert admin.is_dir() and state["objects"].is_dir()
    assert git(w.repo, "cat-file", "-t", state["oid"]) == "commit"
    if late:
        assert line["disposition"] == "deleted" and not tree.exists(), line
        assert any(item["admin"] == str(admin) and "borrows objects" in item["why"]
                   for item in line["admins"]["kept"]), line
    else:
        assert line["disposition"] == "failed" and line["gate"] == "keep", line
        assert "borrows objects" in line["reason"]
        assert snapshot(tree) == before


def test_an_unrelated_alternate_does_not_pin_a_registration(world):
    w = world
    job = "nonexistent-unrelated-alternate"
    tree = nested_job(w, job)
    safe = w.base / "unrelated-objects"
    safe.mkdir()
    info = w.repo / ".git" / "objects" / "info"
    info.mkdir(exist_ok=True)
    (info / "alternates").write_text(str(safe) + "\n")

    line = results(run(delete_path(w), request(job)))[job]

    assert line["disposition"] == "deleted", line
    assert not tree.exists() and not w.admin(job).exists() and safe.is_dir()


def test_a_nonlinked_outside_repository_is_persisted_for_late_borrower_checks(world):
    w = world
    job = "nonexistent-nonlinked-late-borrower"
    tree = nested_job(w, job)
    admin = w.admin(job)
    external = w.base / "nonlinked-external.git"
    git(w.base, "init", "--quiet", "--bare", str(external))
    checkout = tree / "nonlinked-external"
    checkout.mkdir()
    (checkout / ".git").write_text(f"gitdir: {external}\n")
    state = {}

    def after_tree_removal(job_id, point):
        if job_id == job and point == "removed" and not state:
            objects, oid = prepare_borrowed_commit(w, admin)
            (external / "objects" / "info" / "alternates").write_text(str(objects) + "\n")
            git(w.base, f"--git-dir={external}", "update-ref", "refs/heads/nonexistent-borrower", oid)
            state["oid"] = oid

    line = results(run(delete_path(w, on_step=after_tree_removal), request(job)))[job]

    assert line["disposition"] == "deleted" and not tree.exists(), line
    assert admin.is_dir()
    assert git(w.base, f"--git-dir={external}", "cat-file", "-t", state["oid"]) == "commit"
    assert any(item["admin"] == str(admin) and str(external) in item["why"]
               for item in line["admins"]["kept"]), line
