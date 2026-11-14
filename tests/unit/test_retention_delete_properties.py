"""Properties of the delete path over generated requests and trees (C-8.7, d2660, invariants 1 to 5).

Hypothesis builds one to three jobs. A tree may hold nested linked worktrees
of other repositories at depth 0 to 2 (some with a commit only the reflog
names, some of the excluded repository, the first one locked), standalone
repositories inside at depth 0 to 2 (some with a stash), symbolic links
pointing inside, outside, nowhere and into the excluded repository, and
folders and files with odd modes (read-only, unreadable, executable). Some
jobs are listed and some young. The first test runs a dry run, then an apply,
and checks invariants 1 to 4; the second kills the daemon at a generated step
of the apply, restarts it, and checks invariant 5 with 1 to 3. Both use an
oracle of the gates written from the specification: a listed job is deleted
exactly when it is old, holds a nested linked worktree, none of the excluded
repository, no lock, and nothing unreadable.

Every example builds real repositories, so the default is a few examples,
derandomized, without shrinking; `RETENTION_DELETE_PROP_EXAMPLES` raises the
count and `RETENTION_DELETE_PROP_SHRINK=1` turns shrinking on.
"""
from __future__ import annotations

import os
from collections import Counter

from hypothesis import HealthCheck, Phase, example, given, settings
from hypothesis import strategies as st

from subfleet import retention_delete as rd
from subfleet import retention_marks as marks
from tests.unit.retention_delete_world import (
    OLD, STEP, _plain, allowed, changes, commit, delete_path, everything, finish, other_repo, reachable, remove,
    request, results, rows, still_kept,
)
from tests.unit.retention_world import World, git, snapshot, trust_temporary_directories

EXAMPLES = int(os.environ.get("RETENTION_DELETE_PROP_EXAMPLES", "4"))
PHASES = tuple(Phase) if os.environ.get("RETENTION_DELETE_PROP_SHRINK") == "1" else \
    (Phase.explicit, Phase.generate)
SETTINGS = dict(max_examples=EXAMPLES, deadline=None, derandomize=True, database=None, phases=PHASES,
                suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow,
                                       HealthCheck.data_too_large])

job_spec = st.fixed_dictionaries({
    # (depth, a commit only the reflog names, of the excluded repository)
    "nested": st.lists(st.tuples(st.integers(0, 2), st.booleans(), st.booleans()), max_size=2),
    # (depth, a stash)
    "inner": st.lists(st.tuples(st.integers(0, 2), st.booleans()), max_size=2),
    "links": st.lists(st.sampled_from(["inside", "outside", "dangling", "excluded"]), max_size=3),
    "modes": st.lists(st.sampled_from(["ro-dir", "ro-file", "unreadable-dir", "exec-file"]), max_size=2),
    "listed": st.booleans(),
    "young": st.booleans(),
    "lock": st.booleans(),
})
jobs_spec = st.lists(job_spec, min_size=1, max_size=3)


def spec(**fields) -> dict:
    return {"nested": [], "inner": [], "links": [], "modes": [], "listed": True, "young": False, "lock": False,
            **fields}


#: A deep tree with every kind of root, links everywhere and odd modes.
DEEP = spec(nested=[(2, True, False), (0, False, False)], inner=[(1, True), (2, False)],
            links=["inside", "outside", "excluded", "dangling"], modes=["ro-dir", "ro-file", "exec-file"])
#: One job for each way a tree stays whole, and one that goes.
MIXED = [spec(nested=[(0, False, True)]), spec(nested=[(1, False, False)], modes=["unreadable-dir"]),
         spec(nested=[(0, False, False)], lock=True), spec(nested=[(0, False, False)], young=True),
         spec(inner=[(0, True)]), spec(nested=[(1, True, False)], listed=False),
         spec(nested=[(1, True, False)], inner=[(0, False)], links=["excluded"])]


def build(w: World, specs: list[dict]) -> dict:
    """The world's jobs from `specs`: each job's tree, its roots, its admin
    directories and repositories, which jobs the request lists, and the
    excluded repository."""
    excluded = other_repo(w, "excluded")
    (w.base / "sentinel").write_text("outside every tree\n")
    built: dict[str, dict] = {}
    for i, job in enumerate(specs):
        job_id = f"p-{i}"
        tree = w.job(job_id, created=None if job["young"] else OLD)
        roots = {".": {"kind": "linked", "dir": tree, "common": w.repo / ".git"}}
        admins, commons = [w.admin(job_id)], [w.repo / ".git"]
        for j, (depth, reflog_only, from_excluded) in enumerate(job["nested"]):
            rel = "/".join(["d"] * depth + [f"nested{j}"])
            repo = excluded if from_excluded else other_repo(w, f"other-{job_id}-{j}")
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            git(repo, "worktree", "add", "--quiet", "--detach", str(tree / rel), "HEAD")
            keep = commit(tree / rel, f"n{j}.txt", f"{job_id} {j}\n")
            if reflog_only:
                commit(tree / rel, f"gone{j}.txt", "reset away\n")
                git(tree / rel, "reset", "--quiet", "--hard", keep)
            if job["lock"] and j == 0:
                git(repo, "worktree", "lock", str(tree / rel))
            roots[rel] = {"kind": "linked", "dir": tree / rel, "common": repo / ".git"}
            if not from_excluded:
                admins.append(repo / ".git" / "worktrees" / f"nested{j}")
                commons.append(repo / ".git")
        for j, (depth, stash) in enumerate(job["inner"]):
            rel = "/".join(["i"] * depth + [f"inner{j}"])
            inner = tree / rel
            git(w.base, "init", "--quiet", "-b", "main", str(inner))
            commit(inner, "x.txt", f"{job_id} inner {j}\n")
            if stash:
                (inner / "x.txt").write_text("stashed\n")
                git(inner, "stash", "--quiet")
            roots[rel] = {"kind": "inside", "dir": inner, "common": inner / ".git", "held": False}
        for k, link in enumerate(job["links"]):
            target = {"inside": "README.md", "outside": str(w.base / "sentinel"), "dangling": "missing/thing",
                      "excluded": str(excluded / "README.md")}[link]
            os.symlink(target, tree / f"link{k}")
        for k, mode in enumerate(job["modes"]):
            if mode == "ro-dir":
                (tree / f"ro{k}").mkdir()
                (tree / f"ro{k}" / "f").write_text("in a read-only folder\n")
                os.chmod(tree / f"ro{k}" / "f", 0o444)
                os.chmod(tree / f"ro{k}", 0o555)
            elif mode == "unreadable-dir":
                (tree / f"no{k}").mkdir()
                (tree / f"no{k}" / "f").write_text("unreadable\n")
                os.chmod(tree / f"no{k}", 0o000)
            elif mode == "ro-file":
                (tree / f"ro{k}.txt").write_text("read-only\n")
                os.chmod(tree / f"ro{k}.txt", 0o400)
            else:
                (tree / f"x{k}.sh").write_text("#!/bin/sh\n")
                os.chmod(tree / f"x{k}.sh", 0o700)
        built[job_id] = {"tree": tree, "roots": roots, "admins": admins, "commons": commons, "spec": job}
    listed = [job for job, b in built.items() if b["spec"]["listed"]] or [next(iter(built))]
    for job in listed:
        built[job]["spec"] = {**built[job]["spec"], "listed": True}
    return {"jobs": built, "listed": listed, "excluded": excluded}


def deletable(job: dict) -> bool:
    """The oracle: what the gates should say of a listed job, from its spec alone."""
    return job["listed"] and bool(job["nested"]) and not job["young"] and not job["lock"] \
        and not any(from_excluded for _, _, from_excluded in job["nested"]) \
        and "unreadable-dir" not in job["modes"]


def trees_of(w: World, built: dict) -> list[str]:
    return [os.path.relpath(b["tree"], w.base) for b in built["jobs"].values()]


def outside_all(w: World, trees: list[str]) -> dict:
    """Invariant 3's view: everything but the trees, the store's files and the
    check repositories the test makes."""
    return _plain({k: v for k, v in everything(w).items()
                   if k != "checks" and not k.startswith("checks/")
                   and not any(k == t or k.startswith(t + "/") for t in trees)})


def check_outcome(w: World, built: dict, record: dict, before: dict, whole_views: dict,
                  outside_before: dict, interrupted: str | None = None) -> set[str]:
    """Invariants 1 to 3 after an apply, against the oracle; the jobs deleted."""
    lines = results(record)
    deleted = {job for job, line in lines.items() if line["disposition"] == "deleted"}
    for job, b in built["jobs"].items():
        expected = deletable(b["spec"])
        if job == interrupted and job not in deleted:
            assert lines[job]["disposition"] == "interrupted", lines[job]
        elif expected:
            assert job in deleted, (job, lines.get(job), b["spec"])
        else:
            assert job not in deleted, (job, lines.get(job), b["spec"])
        if job in deleted:
            assert not os.path.lexists(b["tree"]) and not any(a.exists() for a in b["admins"])
            still_kept(w, job, b["roots"], before[job])                     # invariant 2
            assert marks.marked(w.root, job)
        else:
            assert snapshot(b["tree"]) == whole_views[job], job              # invariant 1
            assert not marks.marked(w.root, job)
            assert not (w.root / "jobs" / job / "kept-commits").exists()
    # Invariant 3: outside the trees only the deleted jobs' allowed places, and
    # the request's own folder, changed.
    oks = [allowed(w, job, built["jobs"][job]["commons"], built["jobs"][job]["admins"]) for job in deleted]
    base = os.path.relpath(w.root / "retention-delete", w.base)
    diff = changes(outside_before, outside_all(w, trees_of(w, built)))
    stray = {p for kind in diff.values() for p in kind
             if not (p == base or p.startswith(base + "/")) and not any(ok(p) for ok in oks)}
    assert stray == set(), {kind: sorted(paths)[:20] for kind, paths in diff.items()}
    assert git(built["excluded"], "for-each-ref", "refs/subfleet-kept") == ""
    events = [e for e in w.store.list_events() if e["kind"] == rd.EVENT]
    assert sorted(e["job_id"] for e in events) == sorted(deleted)
    assert w.store.query("SELECT COUNT(*) AS n FROM leases")[0]["n"] == 0
    return deleted


def prepare(tmp_path_factory, monkeypatch, specs: list[dict]):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path_factory.mktemp("prop"))
    built = build(w, specs)
    req = request(*built["listed"], min_age_days=1, exclude_repositories=[str(built["excluded"])])
    before = {job: reachable(b["roots"]) for job, b in built["jobs"].items()}
    whole_views = {job: snapshot(b["tree"]) for job, b in built["jobs"].items()}
    return w, built, req, before, whole_views


def run_mode(w: World, req: rd.Request, mode: str) -> dict:
    path = delete_path(w)
    path.start(req, mode=mode)
    return finish(path, mode=mode)


@settings(**SETTINGS)
@given(specs=jobs_spec)
@example(specs=[DEEP])
@example(specs=MIXED)
def test_a_dry_run_then_an_apply_keep_invariants_one_to_four(tmp_path_factory, monkeypatch, specs):
    w, built, req, before, whole_views = prepare(tmp_path_factory, monkeypatch, specs)
    try:
        all_before, rows_before = everything(w), rows(w)
        dry = results(run_mode(w, req, "dry-run"))
        # Invariant 4: a dry run changes nothing anywhere, not even a folder of its own.
        assert everything(w) == all_before and rows(w) == rows_before
        assert not (w.root / "retention-delete").exists()
        outside_before = outside_all(w, trees_of(w, built))
        deleted = check_outcome(w, built, run_mode(w, req, "apply"), before, whole_views, outside_before)
        # The dry run said what the apply did.
        assert {job for job, line in dry.items() if line["disposition"] == "would-delete"} == deleted
    finally:
        w.close()
        remove(w.base)


class Killed(BaseException):
    """The daemon, killed: not an `Exception`, so the path never catches it."""


#: The hook points, each with the journal state it stands in: before
#: `DESTRUCTIVE` a restart puts the tree back, from it on the restart finishes.
POINTS = {"gating": "gating", "keeping": "keeping", "kept": "kept", "renaming": "renaming",
          "renamed-unsaved": "renaming", "renamed": "renamed", "checked": "checked", "removing": "removing",
          "removing-entry": "removing", "removed": "removed", "admins": "admins", "marking": "marking",
          "marker-written": "marking", "marked": "marked"}


@settings(**SETTINGS)
@given(specs=jobs_spec, point=st.sampled_from(sorted(POINTS)), occurrence=st.integers(1, 4))
@example(specs=[DEEP], point="removing-entry", occurrence=4)
@example(specs=[DEEP, spec(nested=[(1, False, False)])], point="renamed-unsaved", occurrence=1)
def test_a_kill_at_a_generated_step_keeps_invariant_five(tmp_path_factory, monkeypatch, specs, point, occurrence):
    w, built, req, before, whole_views = prepare(tmp_path_factory, monkeypatch, specs)
    try:
        outside_before = outside_all(w, trees_of(w, built))
        seen: Counter = Counter()
        killed: list[str] = []

        def on_step(job_id: str, at: str) -> None:
            seen[(job_id, at)] += 1
            if not killed and at == point and seen[(job_id, at)] == occurrence:
                killed.append(job_id)
                raise Killed(f"{at} #{occurrence}")
        path = delete_path(w, on_step=on_step)
        path.start(req, mode="apply")
        try:
            for _ in range(200):
                path.step(**STEP)
                if path.status()["apply"]["status"] not in ("running", "stopping"):
                    break
        except Killed:
            pass
        again = delete_path(w)                          # the restarted daemon
        record = finish(again)
        lines = results(record)
        job = killed[0] if killed else None
        if job is not None:
            # Invariant 5: whole with no destructive step taken, or gone with its commits kept.
            if rd.STATES.index(POINTS[point]) < rd.STATES.index(rd.DESTRUCTIVE):
                assert lines[job]["disposition"] == "interrupted", lines[job]
            else:
                assert lines[job]["disposition"] == "deleted" and lines[job].get("resumed") is True, lines[job]
        check_outcome(w, built, record, before, whole_views, outside_before, interrupted=job)
        assert again.journals == set()
    finally:
        w.close()
        remove(w.base)
