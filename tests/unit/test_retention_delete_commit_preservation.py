"""Regressions for locally complete commit preservation (d2660, invariant 2).

Only temporary fixture stores and local Git repositories are used. Network
URLs are nonexistent identifiers; no network operation is performed. These
are functional delete-path tests, never destructive-shaped guard probes.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
import pytest

from subfleet import retention_git as rgit
from tests.unit.retention_delete_world import (
    commit, delete_path, nested_job, request, restore_modes, results, run,
)
from tests.unit.retention_world import World, git, snapshot, trust_temporary_directories


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    yield w
    w.close()
    restore_modes(tmp_path)


def inside_repo(w: World, tree: Path, *, fmt: str = "sha1") -> tuple[Path, str]:
    inside = tree / "private-inner"
    git(w.base, "init", "--quiet", "-b", "main", f"--object-format={fmt}", str(inside))
    return inside, commit(inside, "only.txt", "private fixture history\n")


def restored_commits(w: World, job: str, inside: Path) -> set[str]:
    marker = json.loads((w.root / "jobs" / job / "tree-deleted.json").read_text())
    bundles = [b for b in marker["bundles"] if b["repository"] == str(inside / ".git")]
    assert len(bundles) == 1, marker
    bundle = bundles[0]
    assert bundle["prerequisites_from"] == []
    restored = w.base / "restored.git"
    git(w.base, "init", "--quiet", "--bare", f"--object-format={marker_format(bundle)}", str(restored))
    # This has no alternates and no remote history: both verification and
    # restoration must succeed with only the retained bundle.
    git(restored, "bundle", "verify", bundle["path"])
    git(restored, "fetch", "--quiet", bundle["path"], "+refs/*:refs/restored/*")
    return set(git(restored, "rev-list", "--all").split())


def marker_format(bundle: dict) -> str:
    return "sha1" if len(next(iter(bundle["heads"].values()))) == 40 else "sha256"


@pytest.mark.parametrize("descendant", [False, True])
def test_claimed_remote_history_is_kept_in_a_self_contained_bundle(world, descendant):
    w = world
    job = "nonexistent-review-claimed-remote"
    tree = nested_job(w, job)
    inside, base = inside_repo(w, tree)
    git(inside, "remote", "add", "origin", "https://nonexistent-review.invalid/not-a-commit-backup.git")
    git(inside, "update-ref", "refs/remotes/origin/main", base)
    expected = {base}
    if descendant:
        expected.add(commit(inside, "child.txt", "private descendant\n"))
    assert not expected.intersection(git(w.remote, "rev-list", "--all").split())
    record = run(delete_path(w), request(job))
    assert results(record)[job]["disposition"] == "deleted"
    assert not tree.exists()
    assert expected <= restored_commits(w, job, inside)


@pytest.mark.parametrize("pseudo", ["MERGE_HEAD", "FETCH_HEAD"])
@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
def test_every_pseudo_ref_entry_survives_in_the_bundle(world, pseudo, fmt):
    w = world
    job = "nonexistent-review-pseudo-entries"
    tree = nested_job(w, job)
    inside, base = inside_repo(w, tree, fmt=fmt)
    tree_id = git(inside, "rev-parse", "HEAD^{tree}")
    first = git(inside, "commit-tree", tree_id, "-m", "only in first pseudo entry")
    second = git(inside, "commit-tree", tree_id, "-m", "only in second pseudo entry")
    suffix = "\tnot-for-merge\tfixture branch" if pseudo == "FETCH_HEAD" else ""
    (inside / ".git" / pseudo).write_text(first + suffix + "\n" + second.upper() + suffix + "\n")
    record = run(delete_path(w), request(job))
    assert results(record)[job]["disposition"] == "deleted"
    assert {base, first, second} <= restored_commits(w, job, inside)


def test_a_shallow_repository_with_missing_ancestry_stays_whole(world):
    w = world
    job = "nonexistent-review-missing-ancestry"
    tree = nested_job(w, job)
    inside, base = inside_repo(w, tree)
    tip = commit(inside, "child.txt", "shallow descendant\n")
    (inside / ".git" / "shallow").write_text(tip + "\n")
    # Functional fault injection entirely inside the fixture repository.
    (inside / ".git" / "objects" / base[:2] / base[2:]).unlink()
    before = snapshot(tree)
    record = run(delete_path(w), request(job))
    assert results(record)[job]["disposition"] != "deleted"
    assert snapshot(tree) == before


def test_a_bundle_that_depends_on_the_source_is_refused_before_removal(world, monkeypatch):
    w = world
    job = "nonexistent-review-bundle-prerequisite"
    tree = nested_job(w, job)
    inside, base = inside_repo(w, tree)
    commit(inside, "child.txt", "descendant needing its parent\n")
    before = snapshot(tree)
    original = rgit.create_bundle

    def prerequisite_bundle(git_dir, path, refs, held, **kwargs):
        # Simulate a builder regression: omit a locally present prerequisite.
        # Verification must detect it without borrowing the source's objects.
        rgit.create_ref(git_dir, "refs/nonexistent-review-prerequisite/base", base)
        original(git_dir, path, refs, ["--glob=refs/nonexistent-review-prerequisite/*"], **kwargs)

    monkeypatch.setattr(rgit, "create_bundle", prerequisite_bundle)
    record = run(delete_path(w), request(job))
    assert results(record)[job]["disposition"] != "deleted"
    assert snapshot(tree) == before


@settings(max_examples=4, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(st.lists(st.binary(min_size=1, max_size=8), min_size=1, max_size=5, unique=True))
def test_arbitrary_multi_entry_pseudo_refs_preserve_every_commit(tmp_path, monkeypatch, messages):
    trust_temporary_directories(monkeypatch)
    with tempfile.TemporaryDirectory(prefix="pseudo-property-", dir=tmp_path) as directory:
        w = World(Path(directory))
        try:
            job = "nonexistent-review-pseudo-property"
            tree = nested_job(w, job)
            inside, base = inside_repo(w, tree)
            tree_id = git(inside, "rev-parse", "HEAD^{tree}")
            expected = {base}
            entries = []
            for message in messages:
                oid = git(inside, "commit-tree", tree_id, "-m", message.hex())
                entries.append(oid)
                expected.add(oid)
            (inside / ".git" / "MERGE_HEAD").write_text("\n".join(entries) + "\n")
            record = run(delete_path(w), request(job))
            assert results(record)[job]["disposition"] == "deleted"
            assert expected <= restored_commits(w, job, inside)
        finally:
            w.close()
            restore_modes(w.base)
