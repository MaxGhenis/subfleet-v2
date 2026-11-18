"""Real fixture regressions for final preservation and publication boundaries.

No command-guard probes, live stores, home overrides or control signals run.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from subfleet import retention_delete as rd, retention_git as rgit
from tests.unit.retention_delete_world import commit, delete_path, nested_job, request, results, run
from tests.unit.retention_world import World, git


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(rgit, "temp_roots", lambda: {"/nonexistent-d2660-final-preservation-temp"})
    w = World(tmp_path)
    yield w
    w.close()


def inside_repo(w, jid):
    tree = nested_job(w, jid)
    inner = tree / "inside"
    git(w.base, "init", "--quiet", "-b", "main", str(inner))
    commit(inner, "tracked.txt", "inside history\n")
    return tree, inner


@pytest.mark.parametrize("fail_verification", [False, True])
def test_old_bundle_destination_files_survive_success_and_rollback(world, monkeypatch, fail_verification):
    w = world
    jid = "nonexistent-accepted-bundle-paths"
    tree, inner = inside_repo(w, jid)
    folder = w.root / "jobs" / jid / "kept-commits"
    folder.mkdir()
    # All possible original root numbers contain synthetic accepted exports.
    originals = {folder / name: ("accepted output " + name).encode()
                 for n in range(4) for name in (f"{n}.bundle", f".{n}.bundle.tmp")}
    for path, data in originals.items():
        path.write_bytes(data)
    path = delete_path(w)
    if fail_verification:
        monkeypatch.setattr(path, "_verify_bundle", lambda *a: (_ for _ in ()).throw(rgit.GitError("fixture verify failure")))
    line = results(run(path, request(jid)))[jid]
    assert {path: path.read_bytes() for path in originals} == originals
    if fail_verification:
        assert line["disposition"] == "failed" and tree.is_dir(), line
        assert set(folder.iterdir()) == set(originals)
    else:
        assert line["disposition"] == "deleted" and not tree.exists(), line
        bundle = Path(rd.marks.marker(w.root, jid)["bundles"][0]["path"])
        assert bundle.parent.parent == folder and bundle not in originals
        assert git(w.repo, "bundle", "list-heads", str(bundle))


@pytest.mark.parametrize("point", ["kept", "removing-entry", "removed"])
def test_changed_preservation_refs_do_not_erase_the_last_commit_record(world, point):
    w = world
    jid = "nonexistent-changed-preservation-ref"
    tree = nested_job(w, jid)
    oid = commit(tree, "unique.txt", "only this detached worktree names the commit\n")
    changed = {}

    def retarget(job, at):
        if job != jid or at != point or changed:
            return
        journal = json.loads((w.root / rd.marks.DELETE_DIR / jid / rd.marks.JOURNAL).read_text())
        ref = next(r for r in journal["kept"]["refs"] if r["repository"] == str(w.repo / ".git") and r["commit"] == oid)
        git(w.repo, "update-ref", ref["ref"], w.head())
        changed["ref"] = ref["ref"]

    line = results(run(delete_path(w, on_step=retarget), request(jid)))[jid]
    assert changed
    if point == "removed":
        assert line["disposition"] == "deleted", line
        assert oid in git(w.repo, "rev-list", "--all").splitlines()
        assert w.admin(jid).is_dir()
    else:
        assert line["gate"] == "keep" and line["disposition"] == "failed", line
        assert tree.is_dir() and git(tree, "rev-parse", "HEAD") == oid
        assert not rd.marks.marked(w.root, jid)


def test_nonempty_local_grafts_keep_the_candidate_whole(world):
    w = world
    jid = "nonexistent-local-grafts"
    tree, inner = inside_repo(w, jid)
    tree_id = git(inner, "rev-parse", "HEAD^{tree}")
    first = git(inner, "commit-tree", tree_id, "-m", "first unrelated root")
    second = git(inner, "commit-tree", tree_id, "-m", "second unrelated root")
    grafts = inner / ".git" / "info" / "grafts"
    grafts.write_text(f"{second} {first}\n")
    line = results(run(delete_path(w), request(jid)))[jid]
    assert line["disposition"] != "deleted" and "grafts" in line["reason"], line
    assert tree.is_dir() and git(inner, "cat-file", "-t", first) == "commit"


def test_bundle_restore_must_reach_every_census_commit(world, monkeypatch):
    w = world
    jid = "nonexistent-incomplete-tip-reduction"
    tree, inner = inside_repo(w, jid)
    main = git(inner, "rev-parse", "HEAD")
    oid = git(inner, "commit-tree", git(inner, "rev-parse", "HEAD^{tree}"), "-m", "unreferenced independent root")
    original = rd.independent

    def incomplete(common, commits, cancel=None):
        return [main] if common == inner / ".git" else original(common, commits, cancel)

    monkeypatch.setattr(rd, "independent", incomplete)
    line = results(run(delete_path(w), request(jid)))[jid]
    assert line["disposition"] == "failed" and tree.is_dir(), line
    assert "every saved census commit" in line["reason"]
    assert git(inner, "cat-file", "-t", oid) == "commit"


@pytest.mark.parametrize("excluded", [False, True])
def test_a_preexisting_bundle_folder_symlink_never_redirects_publication(world, excluded):
    w = world
    jid = "nonexistent-symlink-bundle-folder"
    tree, inner = inside_repo(w, jid)
    target = w.base / "unrelated-output-folder"
    target.mkdir()
    (target / "accepted.txt").write_text("accepted output\n")
    (w.root / "jobs" / jid / "kept-commits").symlink_to(target, target_is_directory=True)
    req = request(jid, exclude_repositories=[str(target)] if excluded else [])
    line = results(run(delete_path(w), req))[jid]
    assert line["disposition"] == "failed" and tree.is_dir(), line
    assert set(p.name for p in target.iterdir()) == {"accepted.txt"}
    assert (target / "accepted.txt").read_text() == "accepted output\n"
