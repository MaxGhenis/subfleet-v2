"""Legacy dependency safety over disposable fixtures, without command probes."""
from __future__ import annotations

import pytest

from subfleet import dependencies, retention_delete as rd, retention_git as rgit
from subfleet.retention_delete_guard import GitReadGuard
from tests.unit.retention_delete_world import delete_path, nested_job, request, results, run
from tests.unit.retention_world import World, git


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(rgit, "temp_roots", lambda: {"/nonexistent-d2660-legacy-temp"})
    w = World(tmp_path)
    yield w
    w.close()


def test_an_unrelated_ordinary_legacy_job_does_not_block_cleanup(world):
    w = world
    jid = "nonexistent-legacy-candidate"
    tree = nested_job(w, jid)
    w.job("nonexistent-unrelated-legacy", worktree=False, state="running")
    line = results(run(delete_path(w), request(jid)))[jid]
    assert line["disposition"] == "deleted", line
    assert not tree.exists()


@pytest.mark.parametrize("field", ["workdir", "review_root", "out_path"])
def test_legacy_dependencies_inside_the_candidate_still_pin_it(world, field):
    w = world
    jid = "nonexistent-needed-legacy-candidate"
    tree = nested_job(w, jid)
    w.job("nonexistent-dependent-legacy", worktree=False, state="running")
    w.store.update_job("nonexistent-dependent-legacy", **{field: str(tree / "needed")})
    line = results(run(delete_path(w), request(jid), mode="dry-run"))[jid]
    assert line["gate"] == "pins" and tree.is_dir(), line


def test_legacy_discovery_checks_an_ancestor_checkout_and_preserves_its_storage(world):
    w = world
    jid = "nonexistent-ancestor-storage-candidate"
    tree = nested_job(w, jid)
    inner = tree / "inside"
    git(w.base, "init", "--quiet", "-b", "main", str(inner))
    outside = w.base / "legacy-checkout"
    outside.mkdir()
    (outside / ".git").write_text(f"gitdir: {inner / '.git'}\n")
    deep = outside / "nested" / "folder"
    deep.mkdir(parents=True)
    w.job("nonexistent-ancestor-legacy", worktree=False, state="running")
    w.store.update_job("nonexistent-ancestor-legacy", workdir=str(deep))
    line = results(run(delete_path(w), request(jid), mode="dry-run"))[jid]
    assert line["gate"] == "pins" and tree.is_dir(), line


def test_legacy_inferred_parent_objects_are_checked_before_discovery(world, monkeypatch):
    w = world
    excluded = w.base / "excluded-repository"
    excluded.mkdir()
    common = w.base / "inferred"
    admin = common / "worktrees" / "nonexistent-old-registration"
    admin.mkdir(parents=True)
    (common / "objects").symlink_to(excluded, target_is_directory=True)
    checkout = w.base / "legacy-checkout"
    checkout.mkdir()
    (checkout / ".git").write_text(f"gitdir: {admin}\n")
    row = {"job_id": "nonexistent-legacy-inferred", "submitted": "{}", "workdir": str(checkout),
           "review_root": None, "out_path": None}
    guard = GitReadGuard(rd.Excluder([str(excluded)]))

    def forbidden(*args):
        raise AssertionError("unproved legacy traversal ran")

    monkeypatch.setattr(dependencies, "legacy", forbidden)
    with pytest.raises(rgit.GitError):
        guard.legacy(lambda sql, params: [row])


def test_legacy_nonabsolute_paths_fail_closed_before_discovery(world, monkeypatch):
    row = {"job_id": "nonexistent-relative-legacy", "submitted": "{}", "workdir": "relative-checkout",
           "review_root": None, "out_path": None}
    guard = GitReadGuard(rd.Excluder([]))
    monkeypatch.setattr(dependencies, "legacy", lambda read: pytest.fail("unproved discovery ran"))
    with pytest.raises(rgit.GitError, match="nonabsolute"):
        guard.legacy(lambda sql, params: [row])
