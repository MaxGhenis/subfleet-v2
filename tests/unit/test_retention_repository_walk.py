"""The repository census is complete, bounded, and never deletion evidence on its own."""
from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import attempt_tmp, retention
from subfleet import retention_archive as rarch
from subfleet import retention_git as rgit
from tests.unit.retention_world import Clock, World, git, snapshot, trust_temporary_directories


def _old_known_repositories(jobs, root):
    """The pre-change census, kept independent of the optimized walk."""
    found = {}
    jobs = list(jobs)
    for job in jobs:
        try:
            tree = rarch.owned_worktree(job, root)
        except ValueError:
            continue
        if tree is not None and os.path.lexists(tree):
            try:
                reg, _ = rgit.registration(tree)
            except OSError:
                continue
            if reg is not None:
                found.setdefault(str(reg.common), reg.common)
    for workdir in sorted({job["workdir"] for job in jobs if job.get("workdir")}):
        if os.path.isdir(workdir):
            common = rgit.common_dir(Path(workdir))
            if common is not None:
                found.setdefault(str(common), common)
    return list(found.values())


@pytest.fixture(scope="module")
def census_rows(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("repository-census")
    root = tmp_path / "state"
    (root / "worktrees").mkdir(parents=True)
    repos = [tmp_path / name for name in ("repository-a", "repository-b")]
    trees = []
    for index, repo in enumerate(repos):
        git(tmp_path, "init", "--quiet", str(repo))
        git(repo, "commit", "--quiet", "--allow-empty", "-m", "base")
        (repo / "sub").mkdir()
        tree = root / "worktrees" / f"allocated-{index}"
        git(repo, "worktree", "add", "--quiet", "--detach", str(tree), "HEAD")
        (tree / "sub").mkdir()
        trees.append(tree)
    per_worktree = tmp_path / "repository-worktree-config"
    git(tmp_path, "init", "--quiet", str(per_worktree))
    git(per_worktree, "commit", "--quiet", "--allow-empty", "-m", "base")
    git(per_worktree, "config", "extensions.worktreeConfig", "true")
    linked = tmp_path / "linked-worktree-config"
    git(per_worktree, "worktree", "add", "--quiet", "--detach", str(linked), "HEAD")
    empty = tmp_path / "non-repository"
    empty.mkdir()
    alias = tmp_path / "repository-alias"
    alias.symlink_to(repos[0], target_is_directory=True)
    workdirs = [repos[0], repos[0] / "sub", repos[1], trees[0], trees[1] / "sub",
                alias, empty, tmp_path / "missing"]
    rows = [dict(job_id=f"readonly-{index}", sandbox="read-only", workdir=str(workdir))
            for index, workdir in enumerate(workdirs)]
    rows += [dict(job_id=f"allocated-{index}", sandbox="workspace-write", worktree=str(tree),
                  workdir=str(repos[1 - index])) for index, tree in enumerate(trees)]
    rows += [dict(job_id="gone-tree", sandbox="workspace-write", worktree=str(root / "worktrees" / "gone"),
                  workdir=str(repos[0])),
             dict(job_id="invalid-tree", sandbox="workspace-write", worktree=str(tmp_path / "outside"),
                  workdir=str(repos[1])),
             dict(job_id="in-place", sandbox="workspace-write", in_place=True, workdir=str(repos[0])),
             dict(job_id="no-workdir", sandbox="read-only", workdir=None),
             dict(job_id="readonly-linked", sandbox="read-only", workdir=str(linked))]
    return root, rows


@settings(max_examples=25, deadline=None, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(st.lists(st.integers(min_value=0, max_value=13), max_size=35))
def test_complete_repository_walk_matches_old_census_and_is_deterministic(census_rows, indices):
    root, templates = census_rows
    rows = [templates[index] for index in indices]
    expected = _old_known_repositories(rows, root)
    actual = rarch.known_repositories(iter(rows), root)
    assert set(actual) == set(expected)
    assert rarch.known_repositories(iter(rows), root) == actual
    assert set(rarch.known_repositories(reversed(rows), root)) == set(actual)
    assert len(actual) == len(set(actual))


@pytest.mark.parametrize("shape", ["unrecorded", "unrecorded-with-head", "missing-unrecorded",
                                  "missing-unrecorded-with-head", "symlink-tree", "dotdot-tree", "symlink-root"])
def test_registration_shortcuts_preserve_unusual_allocation_semantics(census_rows, tmp_path, shape):
    root, templates = census_rows
    source = templates[8].copy()
    # The repository can only enter the census through its registration.
    source["workdir"] = str(tmp_path / "missing-workdir")
    if shape.startswith("unrecorded"):
        source["worktree"] = None
        if shape.endswith("with-head"):
            source["workdir_head"] = "a" * 40
    elif shape.startswith("missing-unrecorded"):
        source["job_id"] = "absent-unrecorded"
        source["worktree"] = None
        if shape.endswith("with-head"):
            source["workdir_head"] = "a" * 40
    elif shape == "symlink-tree":
        alias = root / "worktrees" / "alias"
        alias.symlink_to(source["worktree"], target_is_directory=True)
        source["worktree"] = str(alias)
    elif shape == "dotdot-tree":
        (root / "worktrees" / "intermediate").mkdir()
        source["worktree"] = str(root / "worktrees" / "intermediate" / ".." / "allocated-0")
    elif shape == "symlink-root":
        alias = tmp_path / "state-alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    assert set(rarch.known_repositories([source], root)) == set(_old_known_repositories([source], root))


def test_allocation_created_during_census_invalidates_absent_snapshot(tmp_path, monkeypatch):
    root = tmp_path / "state"
    allocated = root / "worktrees"
    allocated.mkdir(parents=True)
    tree = allocated / "appeared"
    row = dict(job_id="appeared", sandbox="workspace-write", worktree=str(tree))
    common = tmp_path / "repository.git"
    registrations = []

    def registration(path):
        registrations.append(path)
        return rgit.Registration(tmp_path / "admin", common, b""), None

    def rows():
        yield row
        tree.mkdir()                       # after its absent entry was skipped
        yield dict(job_id="later-row", sandbox="read-only")

    monkeypatch.setattr(rgit, "registration", registration)
    actual = rarch.known_repositories(rows(), root)
    assert actual == [common]
    assert registrations == [tree]
    assert actual == _old_known_repositories([row], root)


def test_unreadable_allocation_listing_falls_back_to_original_per_path_checks(tmp_path, monkeypatch):
    root = tmp_path / "state"
    tree = root / "worktrees" / "present"
    tree.mkdir(parents=True)
    common = tmp_path / "repository.git"
    row = dict(job_id="present", sandbox="workspace-write", worktree=str(tree))
    original_listdir = os.listdir
    failed = []

    def listdir(path):
        if isinstance(path, (str, os.PathLike)) and Path(path) == root / "worktrees":
            failed.append(path)
            raise PermissionError("synthetic unreadable listing")
        return original_listdir(path)

    monkeypatch.setattr(rarch.os, "listdir", listdir)
    monkeypatch.setattr(rgit, "registration", lambda path: (rgit.Registration(tmp_path / "admin", common, b""), None))
    assert rarch.known_repositories([row], root) == _old_known_repositories([row], root) == [common]
    assert len(failed) == 1


def test_absent_allocation_snapshot_avoids_per_job_filesystem_probes(tmp_path, monkeypatch):
    root = tmp_path / "state"
    (root / "worktrees").mkdir(parents=True)
    rows = [dict(job_id=f"absent-{index}", sandbox="workspace-write",
                 worktree=str(root / "worktrees" / f"absent-{index}")) for index in range(100)]
    candidates = {row["worktree"] for row in rows}
    original_lexists = os.path.lexists
    probes = []

    def lexists(path):
        if os.fspath(path) in candidates:
            probes.append(path)
        return original_lexists(path)

    def owned(job, root):
        pytest.fail("an unchanged absent snapshot must avoid resolving individual allocations")

    monkeypatch.setattr(rarch.os.path, "lexists", lexists)
    monkeypatch.setattr(rarch, "owned_worktree", owned)
    assert rarch.known_repositories(rows, root) == []
    assert probes == []


@pytest.mark.parametrize("actual_name,listed_name", [("case-job", "CASE-JOB"), ("é-job", "e\u0301-job")])
def test_allocation_snapshot_aliases_keep_original_per_path_checks(tmp_path, monkeypatch,
                                                                  actual_name, listed_name):
    """A directory entry's spelling cannot prove a case/Unicode alias absent."""
    root = tmp_path / "state"
    allocated = root / "worktrees"
    tree = allocated / actual_name
    tree.mkdir(parents=True)
    row = dict(job_id=actual_name, sandbox="workspace-write", worktree=str(tree))
    common = tmp_path / "repository.git"
    original_listdir = os.listdir
    owned_calls = []

    def listdir(path):
        if isinstance(path, (str, os.PathLike)) and Path(path) == allocated:
            return [listed_name]
        return original_listdir(path)

    def owned(job, state_root):
        owned_calls.append(job["job_id"])
        return tree

    monkeypatch.setattr(rarch.os, "listdir", listdir)
    monkeypatch.setattr(rarch, "owned_worktree", owned)
    monkeypatch.setattr(rgit, "registration", lambda path: (rgit.Registration(tmp_path / "admin", common, b""), None))
    assert rarch.known_repositories([row], root) == [common]
    assert owned_calls == [actual_name]
    assert _old_known_repositories([row], root) == [common]


def test_case_insensitive_allocation_lookup_matches_old_census(tmp_path, monkeypatch):
    root = tmp_path / "state"
    tree = root / "worktrees" / "case-job"
    tree.mkdir(parents=True)
    alias = tree.with_name("CASE-JOB")
    if not os.path.lexists(alias):
        pytest.skip("fixture filesystem is case sensitive")
    row = dict(job_id="case-job", sandbox="workspace-write", worktree=str(alias))
    common = tmp_path / "repository.git"
    monkeypatch.setattr(rgit, "registration", lambda path: (rgit.Registration(tmp_path / "admin", common, b""), None))
    assert rarch.known_repositories([row], root) == _old_known_repositories([row], root) == [common]


def test_known_repositories_deadline_during_registration(tmp_path, monkeypatch):
    """Metadata-only registration reads in the first loop need checkpoints too."""
    clock = Clock(0)
    tree = tmp_path / "tree"
    tree.mkdir()
    calls = []

    def registration(path):
        calls.append(path)
        clock.advance(1)
        return rgit.Registration(tmp_path / "admin", tmp_path / "common", b""), None

    monkeypatch.setattr(rarch, "owned_worktree", lambda job, root: tree)
    monkeypatch.setattr(rgit, "registration", registration)
    rows = [dict(job_id=f"job-{index}", sandbox="workspace-write", worktree=str(tree)) for index in range(40)]
    with pytest.raises(rarch.Defer) as caught:
        rarch.known_repositories(rows, tmp_path, deadline=3, clock=clock)
    assert caught.value.reason == "repository discovery deadline"
    assert caught.value.seconds == rarch.DEFER_CHANGED_S
    assert len(calls) == 3 and clock() == 3


def test_known_repositories_deadline_during_workdirs(tmp_path, monkeypatch):
    """An operation crossing the deadline cannot publish a partial census."""
    clock = Clock(0)
    calls = []
    rows = []
    for index in range(40):
        path = tmp_path / f"workdir-{index:02}"
        path.mkdir()
        rows.append(dict(job_id=f"job-{index}", sandbox="read-only", workdir=str(path)))

    def common(path, **kwargs):
        calls.append(path)
        clock.advance(1)
        return path / ".git"

    monkeypatch.setattr(rgit, "fast_common_dir", common)
    with pytest.raises(rarch.Defer, match="repository discovery deadline"):
        rarch.known_repositories(rows, tmp_path, deadline=3, clock=clock)
    assert len(calls) == 3 and clock() == 3


def test_context_does_not_publish_incomplete_repository_census(tmp_path, monkeypatch):
    clock = Clock(0)
    rows = []
    for index in range(3):
        path = tmp_path / f"workdir-{index}"
        path.mkdir()
        rows.append(dict(job_id=f"job-{index}", sandbox="read-only", workdir=str(path)))
    calls = []

    def common(path, **kwargs):
        calls.append(path)
        clock.advance(1)
        return path / ".git"

    monkeypatch.setattr(rgit, "fast_common_dir", common)
    ctx = rarch.Context(tmp_path, SimpleNamespace(list_jobs=lambda: rows), clock=clock, deadline=2)
    with pytest.raises(rarch.Defer, match="repository discovery deadline"):
        ctx.known_repositories()
    assert ctx.repositories is None
    assert len(calls) == 2
    ctx.deadline = 20
    assert set(ctx.known_repositories()) == {Path(row["workdir"]) / ".git" for row in rows}
    assert len(calls) == 5
    assert ctx.known_repositories() is ctx.repositories
    assert len(calls) == 5


def test_repository_census_checks_cancellation_in_first_loop(tmp_path, monkeypatch):
    cancel = threading.Event()
    calls = []
    tree = tmp_path / "tree"
    tree.mkdir()

    def owned(job, root):
        calls.append(job["job_id"])
        cancel.set()
        return None

    monkeypatch.setattr(rarch, "owned_worktree", owned)
    with pytest.raises(rarch.Interrupted, match="cancelled"):
        rarch.known_repositories([dict(job_id=str(i), sandbox="workspace-write", worktree=str(tree))
                                 for i in range(40)], tmp_path, cancel=cancel)
    assert len(calls) == 1


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    # These tests exercise only synthetic trees and do not enumerate host processes.
    monkeypatch.setattr(retention.procs, "worktree_cwd_pids", lambda folder: set())
    value = World(tmp_path)
    yield value
    value.close()


def _gone_job(w, job_id, *, salvage=False):
    tree = w.job(job_id)
    (tree / "private.txt").write_text("unpublished work\n")
    git(tree, "add", "private.txt")
    git(tree, "commit", "--quiet", "-m", "private commit")
    private = git(tree, "rev-parse", "HEAD")
    if salvage:
        w.attempt(job_id)
        ref = "refs/subfleet-salvage/repository-walk-a1"
        git(w.repo, "update-ref", ref, private)
        w.store.add_artifact(f"{job_id}/a1", "salvage", ref, rarch.salvage_digest(private), 0)
    shutil.rmtree(tree)
    w.store.connection.execute("UPDATE jobs SET workdir=? WHERE job_id=?", (str(w.base / "gone-lane"), job_id))
    return tree


def _maintenance(w, **kwargs):
    return retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0,
                                 holders=lambda watches, **_: {}, **kwargs)


def test_discovery_deadline_defers_without_partial_retirement_and_backs_off(world, monkeypatch):
    w = world
    tree = _gone_job(w, "job-gone")
    w.job("job-evidence", worktree=False)
    jobdir_before = snapshot(w.root / "jobs" / "job-gone")
    admin_before = snapshot(w.admin("job-gone"))
    clock, state = Clock(0), retention.RetentionState()
    calls = []

    def common(path, **kwargs):
        calls.append(path)
        clock.advance(10)
        return w.repo / ".git"

    monkeypatch.setattr(rgit, "fast_common_dir", common)
    first = _maintenance(w, state=state, clock=clock, deadline=1, referenced_job_ids=["job-evidence"])
    assert first["pruned"] == [] and first["errors"] == [], first
    assert first["deferred"]["job-gone"].startswith("repository discovery deadline"), first
    assert w.store.get_job("job-gone") is not None
    assert not tree.exists()
    assert snapshot(w.admin("job-gone")) == admin_before
    assert snapshot(w.root / "jobs" / "job-gone") == jobdir_before
    assert rarch.journals(w.root) == {}
    assert w.store.query("SELECT * FROM leases WHERE holder=?", ("retention:job-gone",)) == []
    assert not (w.root / "archive" / "job-gone").exists()
    assert not (w.root / "retention" / "job-gone").exists()
    assert len(calls) == 1
    second = _maintenance(w, state=state, clock=clock, deadline=clock() + 1,
                          referenced_job_ids=["job-evidence"])
    assert second["pruned"] == [] and second["errors"] == [], second
    assert second["deferred"]["job-gone"] == "repository discovery deadline"
    assert len(calls) == 1, "the next pass must honor the existing deferral before scanning again"


@pytest.mark.parametrize("salvage", [False, True], ids=["private-head", "salvage-ref"])
def test_missing_repository_from_census_keeps_rows_registration_and_salvage(world, salvage):
    """An undiscovered repository's private HEAD/ref must never be retired unfound."""
    w = world
    _gone_job(w, "job-unfound", salvage=salvage)
    admin_before = snapshot(w.admin("job-unfound"))
    result = _maintenance(w)
    assert result["pruned"] == [] and result["errors"] == [], result
    expected = "salvage not archivable" if salvage else "repository not found"
    assert result["deferred"]["job-unfound"].startswith(expected), result
    assert w.store.get_job("job-unfound") is not None
    assert snapshot(w.admin("job-unfound")) == admin_before
    if salvage:
        assert git(w.repo, "show-ref", "--verify", "refs/subfleet-salvage/repository-walk-a1")
    assert rarch.journals(w.root) == {}
    assert w.store.query("SELECT * FROM leases WHERE holder=?", ("retention:job-unfound",)) == []
    assert not (w.root / "archive" / "job-unfound").exists()


def test_salvage_listing_stops_at_the_deadline_and_keeps_no_partial_answer(tmp_path, monkeypatch):
    """A job with salvage rows asks every census repository: the same bound as the census."""
    clock = Clock(0)
    ctx = rarch.Context(tmp_path, SimpleNamespace(), clock=clock, deadline=5)
    calls = []

    def listing(common, prefix, cancel=None):
        calls.append(common)
        clock.advance(10)
        return {}

    monkeypatch.setattr(rgit, "no_refs_under", lambda common, prefix: False)
    monkeypatch.setattr(rgit, "refs_under", listing)
    assert ctx.salvage_refs(tmp_path / "one") == {}
    with pytest.raises(rarch.Defer, match="repository discovery deadline"):
        ctx.salvage_refs(tmp_path / "two")
    assert calls == [tmp_path / "one"]
    assert str(tmp_path / "two") not in ctx.salvage_listings


def _repositories_with_refs(base, holdings):
    commons = []
    for index, refs in enumerate(holdings):
        repo = base / f"repository-{index}"
        git(base, "init", "--quiet", str(repo))
        git(repo, "commit", "--quiet", "--allow-empty", "-m", "base")
        head = git(repo, "rev-parse", "HEAD")
        for ref in refs:
            git(repo, "update-ref", ref, head)
        commons.append(Path(os.path.realpath(repo / ".git")))
    return commons


def _search_every_repository(commons, wanted):
    """The search before the proof: list every repository."""
    for common in commons:
        listing = rgit.refs_under(common, "refs/subfleet-salvage/")
        if all(ref in listing and rarch.salvage_digest(listing[ref]) == digest for ref, digest in wanted.items()):
            return common
    return None


REF_NAMES = ("refs/subfleet-salvage/job-a1", "refs/subfleet-salvage/job-a2", "refs/subfleet-salvage/job-a10")


def _salvage_source(commons, wanted, listed):
    def salvage_refs(common):
        listed.append(common)
        return rgit.refs_under(common, "refs/subfleet-salvage/")

    return rarch.source_of_gone_tree(dict(workdir=None), Path("/nonexistent-ret-walk/tree"), lambda: commons,
                                     salvage_refs, wanted)


def test_salvage_search_lists_only_the_repository_that_might_hold_the_refs(tmp_path):
    commons = _repositories_with_refs(tmp_path, [(), (REF_NAMES[1],), (REF_NAMES[0],), ()])
    head = git(commons[2].parent, "rev-parse", "HEAD")
    listed = []
    reg, common, lost = _salvage_source(commons, {REF_NAMES[0]: rarch.salvage_digest(head)}, listed)
    assert (reg, common, lost) == (None, commons[2], None)
    assert listed == [commons[2]]


@settings(max_examples=25, deadline=None, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(st.lists(st.sets(st.sampled_from(REF_NAMES), max_size=3), min_size=1, max_size=4),
       st.sets(st.sampled_from(REF_NAMES), min_size=1), st.booleans(), st.booleans())
def test_salvage_search_finds_what_listing_every_repository_finds(tmp_path_factory, holdings, names, matches, packed):
    """Same repository as the unfiltered search, whatever refs sit where and however stored."""
    base = tmp_path_factory.mktemp("salvage-search")
    commons = _repositories_with_refs(base, [sorted(refs) for refs in holdings])
    if packed:
        for common in commons:
            git(common.parent, "pack-refs", "--all", "--prune")
    digest = rarch.salvage_digest(git(commons[0].parent, "rev-parse", "HEAD"))
    wanted = {ref: digest if matches else "0" * len(digest) for ref in sorted(names)}
    _, common, _ = _salvage_source(commons, wanted, [])
    assert common == _search_every_repository(commons, wanted)


def test_salvage_listing_of_a_repository_with_refs_is_gits(tmp_path):
    repo = tmp_path / "repo"
    git(tmp_path, "init", "--quiet", str(repo))
    git(repo, "commit", "--quiet", "--allow-empty", "-m", "base")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/subfleet-salvage/job-a1", head)
    ctx = rarch.Context(tmp_path, SimpleNamespace())
    assert ctx.salvage_refs(repo / ".git") == rgit.refs_under(repo / ".git", "refs/subfleet-salvage/")
    assert list(ctx.salvage_refs(repo / ".git")) == ["refs/subfleet-salvage/job-a1"]


def test_one_jobs_probe_rows_use_the_job_index_and_match_the_kind_scan(world):
    """Retention reads this under the store lock once a start and once a commit."""
    w = world
    w.job("job-probed", worktree=False)
    w.job("job-other", worktree=False)
    for job_id, holder, tmpdir in (("job-probed", "probe-a", {"path": "/a"}), ("job-probed", "probe-a", {"path": "/b"}),
                                   ("job-probed", "probe-b", {"path": "/c", "removed_at": "now"}),
                                   ("job-other", "probe-c", {"path": "/d"})):
        w.store.add_event("probe.state", job_id=job_id, data={"holder": holder, "tmpdir": tmpdir})
    seen = []

    class Recording:
        def query(self, sql, params=()):
            seen.append((sql, params))
            return w.store.query(sql, params)

    rows = attempt_tmp.probe_tmpdir_rows(Recording(), job_id="job-probed")
    sql, params = seen[0]
    plan = " ".join(str(tuple(row)) for row in w.store.connection.execute("EXPLAIN QUERY PLAN " + sql, params))
    assert "events_job" in plan and "events_kind" not in plan, plan
    assert sql.count("+kind") == 1
    as_before = w.store.query(sql.replace("+kind", "kind"), params)
    assert [dict(row) for row in rows] == [dict(row) for row in as_before]
    assert [row["holder"] for row in rows] == ["probe-a"] and "/b" in rows[0]["tmpdir"]
    every = attempt_tmp.probe_tmpdir_rows(w.store)
    assert sorted(row["holder"] for row in every) == ["probe-a", "probe-c"]
