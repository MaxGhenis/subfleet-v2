"""The delete path over real git repositories (C-8.7, d2660): what it deletes,
what it keeps, and what each gate leaves alone.

The ruling's bar: each job's record and accepted output stay, and every commit
the tree alone holds is kept in a bundle or under a ref in its repository; a
tree whose commits can't be kept stays whole. Invariants 1 to 4 and 6 are
checked here on built trees (the Hypothesis versions are in
`test_retention_delete_properties`, the kills in `test_retention_delete_resume`).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from subfleet import folders, retention
from subfleet import retention_archive as rarch
from subfleet import retention_delete as rd
from subfleet import retention_git as rgit
from subfleet import retention_marks as marks
from subfleet import retention_survey as rsurvey
from tests.unit.retention_delete_world import (
    OLD, STEP, allowed, changes, commit, delete_path, everything, nested_job, other_repo, outside,
    reachable, request, restore_modes, results, rich_job, rows, run, still_kept,
)
from tests.unit.retention_world import World, git, snapshot, trust_temporary_directories


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    yield w
    w.close()
    restore_modes(tmp_path)


def leases(w: World) -> list[tuple[str, str]]:
    return [(r["lease_key"], r["holder"]) for r in w.store.query("SELECT lease_key,holder FROM leases ORDER BY 1")]


# --- a deletion -----------------------------------------------------------------------

def test_a_rich_tree_is_deleted_with_every_commit_kept_and_every_record_left(world):
    """The ruling's bar on a tree holding every kind of root (invariants 2, 3
    and 6; mutants 5 and 14): commits only a reflog, a per-worktree ref, a
    bisect ref, a stash or a tag names are kept; the job's rows, folder and
    output stay; the marker and one event say what was done."""
    w = world
    job = rich_job(w, "job-rich")
    tree, roots = job["tree"], job["roots"]
    before = reachable(roots)
    jobdir_before = snapshot(w.root / "jobs" / "job-rich")
    view_before = outside(w, tree)
    row_before = dict(w.store.get_job("job-rich"))
    path = delete_path(w)
    record = run(path, request("job-rich"))
    line = results(record)["job-rich"]
    assert record["status"] == "done" and line["disposition"] == "deleted", line
    assert line["class"].startswith("nested-linked-worktree")
    assert not tree.exists() and not os.path.lexists(tree)
    # Invariant 3: outside the tree only the allowed places changed.
    diff = changes(view_before, outside(w, tree))
    ok = allowed(w, "job-rich", [w.repo / ".git", job["other"] / ".git"], job["admins"])
    assert {p for kind in diff.values() for p in kind if not ok(p)} == set(), diff
    assert all(not a.exists() for a in job["admins"])                       # the registrations went with the tree
    # Invariant 2: every commit is kept.
    still_kept(w, "job-rich", roots, before)
    # The job's record and output stay; its folder gains only the kept commits and the marker.
    assert dict(w.store.get_job("job-rich")) == row_before
    jobdir_after = snapshot(w.root / "jobs" / "job-rich")
    extra = {k for k in jobdir_after if k not in jobdir_before}
    assert {k for k in extra if not (k.startswith("kept-commits") or k == "tree-deleted.json")} == set()
    assert {k: v for k, v in jobdir_after.items() if k in jobdir_before} == jobdir_before
    marker = json.loads((w.root / "jobs" / "job-rich" / "tree-deleted.json").read_text())
    assert marker["ruling"] == "d-test:2026-11-09:ruled" and marker["job_id"] == "job-rich"
    assert marker["logical_bytes"] > 0 and marker["allocated_bytes"] >= marker["logical_bytes"] // 2
    assert marker["free_before"] > 0 and marker["free_after"] > 0 and marker["started_at"] <= marker["ended_at"]
    assert {r["repository"] for r in marker["refs"]} == {os.path.realpath(w.repo / ".git"),
                                                         os.path.realpath(job["other"] / ".git")}
    for ref in marker["refs"]:
        assert ref["ref"].startswith("refs/subfleet-kept/job-rich/")
        assert git(ref["repository"], "rev-parse", ref["ref"]) == ref["commit"]
    assert len(marker["bundles"]) == 2 and all(b["bytes"] > 0 and len(b["sha256"]) == 64 for b in marker["bundles"])
    assert sorted(len(b["prerequisites_from"]) for b in marker["bundles"]) == [0, 0]
    events = [e for e in w.store.list_events("job-rich") if e["kind"] == "retention.tree_deleted"]
    assert len(events) == 1 and json.loads(events[0]["data_json"]) == marker
    # Nothing is left in flight.
    assert leases(w) == [] and path.journals == set()
    assert os.listdir(w.root / "retention-delete") == [rd.RECORD]
    assert record["totals"]["deleted"] == 1 and record["totals"]["refs"] == len(marker["refs"])
    assert record["totals"]["bundles"] == 2 and record["totals"]["logical_bytes"] == marker["logical_bytes"]
    # Invariant 6: the normal pass keeps the marked job for good, with no error about its tree.
    first = retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0, holders=lambda watches, **_: {})
    assert "job-rich" not in first["pruned"] and w.store.get_job("job-rich") is not None
    assert (w.root / "jobs" / "job-rich" / "stdout").exists() and not (w.root / "archive" / "job-rich").exists()
    assert not (w.root / "retention" / "job-rich").exists()
    # The survey reports it deleted, from its marker, even within budget, and
    # never as a candidate; `kept_detail` lists kept trees that exist, and this one is gone.
    report = rsurvey.survey(w.root, sizes=True, holders=False)
    assert report["kept"]["jobs_by_reason"] == {marks.DELETED: 1}, report["kept"]
    assert all(r["job_id"] != "job-rich" for r in report["candidates"] + report["kept_detail"])
    info = rsurvey.job_info(dict(w.store.get_job("job-rich")), w.root, kept=marks.DELETED)
    assert info["pin"] == marks.DELETED and info["tree_deleted"]["job_id"] == "job-rich"
    # The same request again continues its record: the job stays deleted and nothing more is done.
    same = run(delete_path(w), request("job-rich"))
    assert same["run"] == 2 and same["deleted_this_run"] == 0 and results(same)["job-rich"]["disposition"] == "deleted"
    # A new request keeps it at the `finished` gate, and the old record is kept beside the new one.
    again = run(delete_path(w), request("job-rich", min_free_gb=1))
    assert results(again)["job-rich"]["gate"] == "finished" and "already deleted" in results(again)["job-rich"]["reason"]
    assert again["run"] == 1 and len([n for n in os.listdir(w.root / "retention-delete")
                                      if n.startswith(".request-")]) == 1
    events = [e for e in w.store.list_events("job-rich") if e["kind"] == "retention.tree_deleted"]
    assert len(events) == 1


def test_a_remote_less_history_tree_is_deleted_and_its_commits_kept_by_refs(world):
    """`remote-less-history`: no network remote holds the baseline; the tree's
    unpushed commits are kept under refs in the source repository."""
    w = world
    git(w.repo, "remote", "remove", "origin")
    commit(w.repo, "history.bin", "h" * 4000)
    tree = w.job("job-rlh", created=OLD)
    mine = commit(tree, "mine.txt", "only here\n")
    before = reachable({".": {"kind": "linked", "dir": tree, "common": w.repo / ".git"}})
    record = run(delete_path(w), request("job-rlh"), remote_less_history_bytes=1)
    line = results(record)["job-rlh"]
    assert line["disposition"] == "deleted" and line["class"].startswith("remote-less-history"), line
    have = set(git(w.repo, "rev-list", "--all").split())
    assert before["."] <= have and mine in have
    assert not w.admin("job-rlh").exists() and not tree.exists()
    # With the policy's real limit it is an ordinary job: the class gate keeps it.
    tree2 = w.job("job-rlh-small", created=OLD)
    line2 = results(run(delete_path(w), request("job-rlh-small"), **STEP))["job-rlh-small"]
    assert line2["gate"] == "class" and tree2.exists()


# --- a dry run --------------------------------------------------------------------------

def test_a_dry_run_changes_nothing_anywhere_and_says_what_an_apply_would_do(world):
    """Invariant 4 (mutant 8): no ref, bundle, journal, event, lease or file,
    and the store's rows unchanged; its verdicts are an apply's."""
    w = world
    job = rich_job(w, "job-dry")
    nested_job(w, "job-young", created=None)
    w.job("job-plain", created=OLD)
    before, rows_before = everything(w), rows(w)
    path = delete_path(w)
    record = run(path, request("job-dry", "job-young", "job-plain", "job-none", min_age_days=1), mode="dry-run")
    assert everything(w) == before and rows(w) == rows_before
    assert not (w.root / "retention-delete").exists()
    lines = results(record)
    assert lines["job-dry"]["disposition"] == "would-delete"
    assert {r["rel"] for r in lines["job-dry"]["roots"]} == {".git", "deep/nested/.git", "inner/.git",
                                                            "inner-wt/.git", "clone/.git"}
    assert len(lines["job-dry"]["keep"]["refs"]) == 2 and len(lines["job-dry"]["keep"]["bundles"]) == 2
    assert (lines["job-young"]["gate"], lines["job-plain"]["gate"], lines["job-none"]["gate"]) == \
        ("age", "class", "finished")
    assert record["totals"]["would-delete"] == 1 and record["totals"]["kept"] == 3
    assert path.status()["apply"] is None
    # The same request applied does what the dry run said.
    applied = results(run(path, request("job-dry", "job-young", "job-plain", "job-none", min_age_days=1)))
    assert {k: v["disposition"] for k, v in applied.items()} == \
        {"job-dry": "deleted", "job-young": "kept", "job-plain": "kept", "job-none": "kept"}
    assert not job["tree"].exists()


# --- the gates ----------------------------------------------------------------------------

def test_gates_one_to_four_keep_their_jobs_whole(world):
    """Invariant 1 for gates 1 to 4 (mutants 1 and 10): each job names its
    gate, its tree is unchanged entry by entry, and the leases are left free."""
    w = world
    trees = {}
    trees["g-young"] = nested_job(w, "g-young", created=None)
    trees["g-exempt"] = nested_job(w, "g-exempt", created=None)
    trees["g-turn"] = nested_job(w, "g-turn", kind="turn")
    trees["g-running"] = nested_job(w, "g-running", state="running")
    trees["g-attempt"] = nested_job(w, "g-attempt")
    w.attempt("g-attempt", state="running")
    trees["g-retiring"] = nested_job(w, "g-retiring")
    journal = rarch.journal_path(w.root, "g-retiring")
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"schema": rarch.SCHEMA, "job_id": "g-retiring", "state": "archiving"}))
    trees["g-marked"] = nested_job(w, "g-marked")
    (w.root / "jobs" / "g-marked" / marks.MARKER).write_text(json.dumps({"job_id": "g-marked"}))
    trees["g-inplace"] = nested_job(w, "g-inplace")
    w.store.update_job("g-inplace", in_place=1)
    w.job("g-readonly", worktree=False, created=OLD)
    trees["g-elsewhere"] = nested_job(w, "g-elsewhere")
    outside_tree = w.base / "elsewhere" / "g-elsewhere"
    outside_tree.parent.mkdir()
    os.rename(trees["g-elsewhere"], outside_tree)
    git(w.repo, "worktree", "repair", str(outside_tree))
    w.store.update_job("g-elsewhere", worktree=str(outside_tree))
    trees["g-elsewhere"] = outside_tree
    trees["g-link"] = nested_job(w, "g-link")
    real = w.base / "elsewhere" / "g-link"
    os.rename(trees["g-link"], real)
    os.symlink(real, w.root / "worktrees" / "g-link")
    trees["g-link"] = real
    before = {job: snapshot(tree) for job, tree in trees.items()}
    jobs = ["g-young", "g-exempt", "g-turn", "g-running", "g-attempt", "g-retiring", "g-marked", "g-inplace",
            "g-readonly", "g-elsewhere", "g-link", "g-missing"]
    path = delete_path(w)
    record = run(path, request(*jobs, min_age_days=14, age_exempt=["g-exempt"]))
    lines = results(record)
    gates = {job: lines[job]["gate"] for job in jobs}
    assert gates == {"g-young": "age", "g-exempt": None, "g-turn": "finished", "g-running": "finished",
                     "g-attempt": "finished", "g-retiring": "finished", "g-marked": "finished",
                     "g-inplace": "owned-path", "g-readonly": "owned-path", "g-elsewhere": "owned-path",
                     "g-link": "owned-path", "g-missing": "finished"}, {j: (lines[j]["gate"], lines[j]["reason"])
                                                                         for j in jobs}
    assert lines["g-exempt"]["disposition"] == "deleted" and not trees["g-exempt"].exists()
    for job, tree in trees.items():
        if job != "g-exempt":
            assert snapshot(tree) == before[job], job
            assert lines[job]["disposition"] == "kept", job
    assert leases(w) == [] and path.journals == set()
    assert sorted(os.listdir(w.root / "retention-delete")) == [rd.RECORD]


def test_gates_five_to_twelve_keep_their_jobs_whole(world, monkeypatch):
    """Invariant 1 for gates 5 to 12, a lease held elsewhere and a root whose
    commits can't be kept (mutants 3 and 13)."""
    w = world
    excluded = other_repo(w, "excluded")
    trees = {}
    trees["g-plain"] = w.job("g-plain", created=OLD)
    trees["g-excluded"] = nested_job(w, "g-excluded", repo=excluded)
    trees["g-pinned"] = nested_job(w, "g-pinned")
    trees["g-held"] = nested_job(w, "g-held")
    trees["g-cwd"] = nested_job(w, "g-cwd")
    trees["g-locked"] = nested_job(w, "g-locked")
    git(w.base / "home" / "other-g-locked", "worktree", "lock", "--reason", "someone's", str(trees["g-locked"] / "nested"))
    trees["g-registers-out"] = nested_job(w, "g-registers-out")
    inner = trees["g-registers-out"] / "inner"
    git(w.base, "init", "--quiet", "-b", "main", str(inner))
    commit(inner, "x", "x\n")
    git(inner, "worktree", "add", "--quiet", "--detach", str(w.base / "out-of-tree"), "HEAD")
    trees["g-symlink-root"] = nested_job(w, "g-symlink-root")
    (trees["g-symlink-root"] / "odd").mkdir()
    os.symlink(str(w.repo / ".git"), trees["g-symlink-root"] / "odd" / ".git")
    trees["g-leased"] = nested_job(w, "g-leased")
    with w.store.transaction("test.lease") as conn:
        conn.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES('retire:g-leased','someone','x')")
    trees["g-deletable"] = nested_job(w, "g-deletable")
    trees["g-unlisted"] = nested_job(w, "g-unlisted")
    before = {job: snapshot(tree) for job, tree in trees.items()}
    jobs = [j for j in trees if j != "g-unlisted"]

    def holders(watches, cancel=None):
        return {job: ["pid 99: a file open in the tree"] for job in watches if job == "g-held"}

    def cwd_pids(path):
        return frozenset({4242}) if "g-cwd" in path else frozenset()
    # The free-space gate first, in a dry run: every job that reaches it stops there.
    dry = results(run(delete_path(w), request(*jobs, min_free_gb=10 ** 9, exclude_repositories=[str(excluded)]),
                      mode="dry-run"))
    assert dry["g-deletable"]["gate"] == "free-space" and dry["g-deletable"]["disposition"] == "kept"
    path = delete_path(w, holders=holders, cwd_pids=cwd_pids)
    reads = Reads(monkeypatch)
    reads.active = True
    record = run(path, request(*jobs, exclude_repositories=[str(excluded)]), explicit={"g-pinned"})
    reads.active = False
    # A nested worktree naming the excluded repository puts its job out with nothing there read.
    assert reads.below(excluded) == [] and reads.named(excluded) == []
    lines = results(record)
    gates = {job: (lines[job]["gate"], lines[job]["disposition"]) for job in jobs}
    assert gates == {"g-plain": ("class", "kept"), "g-excluded": ("exclude-nested", "kept"),
                     "g-pinned": ("pins", "kept"), "g-held": ("holders", "kept"), "g-cwd": ("holders", "kept"),
                     "g-locked": ("locks", "kept"), "g-registers-out": ("outside", "kept"),
                     "g-symlink-root": ("keep", "failed"), "g-leased": ("leases", "skipped"),
                     "g-deletable": (None, "deleted")}, {j: lines[j]["reason"] for j in jobs}
    assert "someone's" in lines["g-locked"]["reason"] and "4242" in lines["g-cwd"]["reason"]
    assert "out-of-tree" in lines["g-registers-out"]["reason"]
    for job, tree in trees.items():
        if job != "g-deletable":
            assert snapshot(tree) == before[job], job
    assert not trees["g-deletable"].exists()
    assert leases(w) == [("retire:g-leased", "someone")] and path.journals == set()
    assert git(excluded, "for-each-ref", "refs/subfleet-kept") == ""


# --- what an excluded repository is spared -----------------------------------------------

class Reads:
    """Every path Python stats, opens or lists, as (kind, path), and every
    command run (git's arguments, its git dir and folder), while active.
    Paths are kept as spelled; `below` and `named` compare them folded and
    resolved, after the run."""

    def __init__(self, monkeypatch):
        self.paths: list[tuple[str, str]] = []
        self.commands: list[list[str]] = []
        self.active = False
        real_stat, real_lstat = os.stat, os.lstat

        def spy_stat(path, *args, **kwargs):
            self._add("stat", path, kwargs.get("dir_fd"))
            return real_stat(path, *args, **kwargs)

        def spy_lstat(path, *args, **kwargs):
            self._add("stat", path, kwargs.get("dir_fd"))
            return real_lstat(path, *args, **kwargs)
        monkeypatch.setattr(os, "stat", spy_stat)
        monkeypatch.setattr(os, "lstat", spy_lstat)
        real_run = rgit.run

        def spy_run(args, *a, **kw):
            if self.active:
                self.commands.append([*map(str, args), str(kw.get("git_dir")), str(kw.get("cwd"))])
            return real_run(args, *a, **kw)
        monkeypatch.setattr(rgit, "run", spy_run)
        sys.addaudithook(self.audit)          # a hook stays for the process: `active` turns it off

    def _add(self, kind, path, dir_fd=None):
        if self.active and isinstance(path, (str, bytes, os.PathLike)) and dir_fd is None:
            self.paths.append((kind, os.fsdecode(path)))

    def audit(self, event, args):
        if not self.active or not args:
            return
        if event in ("open", "os.listdir", "os.scandir") and isinstance(args[0], (str, bytes, os.PathLike)):
            self.paths.append(("open" if event == "open" else "list", os.fsdecode(args[0])))
        elif event == "subprocess.Popen":
            self.commands.append([str(a) for a in (args[1] or [])] + [str(args[2])])

    @staticmethod
    def _key(path: str) -> str:
        return folders.fold(os.path.realpath(path)) if path.startswith("/") else ""

    def below(self, root: Path, *, kinds=("stat", "open", "list")) -> list[str]:
        """Recorded paths strictly below `root`, and commands naming `root` or below."""
        assert not self.active
        key = folders.fold(os.path.realpath(root))
        hits = [p for kind, p in self.paths if kind in kinds and self._key(p).startswith(key + "/")]
        hits += [" ".join(c) for c in self.commands
                 if any(self._key(part) == key or self._key(part).startswith(key + "/") for part in c)]
        return hits

    def named(self, path: Path, kinds=("open", "list")) -> list[str]:
        assert not self.active
        key = folders.fold(os.path.realpath(path))
        return [p for kind, p in self.paths if kind in kinds and self._key(p) == key]


def test_a_tree_whose_gitfile_names_an_excluded_repository_is_not_read_beyond_it(world, monkeypatch):
    """Invariant 7 (mutant 2): with the job's workdir elsewhere, the tree's
    top-level gitfile is the only thing in the tree read, nothing in the
    excluded repository is read or run, and the tree is unchanged; with the
    workdir in the excluded repository, not even the gitfile is read. The
    exclusion is spelled in another case, and the gitfile's path relative."""
    w = world
    tree = nested_job(w, "job-micro")
    gitfile = tree / ".git"
    gitfile.write_text("gitdir: " + os.path.relpath(w.admin("job-micro"), tree) + "\n")
    w.store.update_job("job-micro", workdir=str(w.base / "home"))
    tree2 = nested_job(w, "job-micro-workdir")
    before = {t: snapshot(t) for t in (tree, tree2)}
    excluded = str(w.repo).upper() if os.path.exists(str(w.repo).upper()) else str(w.repo)
    reads = Reads(monkeypatch)
    path = delete_path(w)
    reads.active = True
    record = run(path, request("job-micro", "job-micro-workdir", exclude_repositories=[excluded]))
    reads.active = False
    lines = results(record)
    assert (lines["job-micro"]["gate"], lines["job-micro-workdir"]["gate"]) == ("exclude-top", "exclude-top")
    assert "excluded repository" in lines["job-micro"]["reason"]
    # Nothing in the excluded repository is statted, opened, listed or run;
    # its own entry is only statted, once per job, to resolve its spelling.
    assert reads.below(w.repo) == [] and reads.named(w.repo) == []
    inside = {folders.fold(os.path.realpath(p)) for p in reads.below(tree)}
    assert inside <= {folders.fold(os.path.realpath(gitfile))}, inside
    assert reads.below(tree2) == []
    for t, view in before.items():
        assert snapshot(t) == view


# --- runs ------------------------------------------------------------------------------------

def test_limit_ends_the_run_and_the_same_request_continues_it(world):
    """`--limit` (mutant 11): the run ends after N deletions; the same request
    applied again keeps what was deleted and goes on; a new request starts afresh."""
    w = world
    for job in ("l-1", "l-2", "l-3"):
        nested_job(w, job)
    path = delete_path(w)
    req = request("l-1", "l-2", "l-3")
    first = run(path, req, limit=1)
    assert first["status"] == "done-limit" and first["deleted_this_run"] == 1 and first["totals"]["pending"] == 2
    assert [l["job_id"] for l in first["results"]] == ["l-1"]
    second = run(path, req, limit=1)
    assert second["run"] == 2 and second["status"] == "done-limit"
    assert [l["job_id"] for l in second["results"]] == ["l-1", "l-2"]
    third = run(path, req)
    assert third["status"] == "done" and third["totals"]["deleted"] == 3 and third["run"] == 3
    assert [r["status"] for r in third["runs"]] == ["done-limit", "done-limit", "done"]
    record = json.loads((w.root / "retention-delete" / rd.RECORD).read_text())
    assert record["status"] == "done" and len(record["runs"]) == 3
    # Restarted, the daemon reads the record back and has nothing to do.
    again = delete_path(w)
    assert again.status()["apply"]["status"] == "done" and not again.pending()


def test_a_failure_on_one_job_fails_it_whole_and_the_run_goes_on(world, monkeypatch):
    """Mutant 6: a bundle that fails `git bundle verify` keeps the tree whole,
    takes back the refs and bundle that job made, and the next job is deleted."""
    w = world
    bad = nested_job(w, "f-bad")
    inner = bad / "inner"
    git(w.base, "init", "--quiet", "-b", "main", str(inner))
    commit(inner, "x", "x\n")
    nested_job(w, "f-good")
    before = snapshot(bad)
    real = rd.DeletePath._verify_bundle

    def verify(self, path, expected, fmt, scratch):
        if "f-bad" in str(path):
            raise rgit.GitError("bundle verify failed (test)")
        return real(self, path, expected, fmt, scratch)
    monkeypatch.setattr(rd.DeletePath, "_verify_bundle", verify)
    path = delete_path(w)
    lines = results(run(path, request("f-bad", "f-good")))
    assert lines["f-bad"]["disposition"] == "failed" and "bundle verify failed" in lines["f-bad"]["reason"]
    assert lines["f-good"]["disposition"] == "deleted"
    assert snapshot(bad) == before
    assert git(w.repo, "for-each-ref", "refs/subfleet-kept/f-bad") == ""
    assert git(w.base / "home" / "other-f-bad", "for-each-ref", "refs/subfleet-kept") == ""
    assert not (w.root / "jobs" / "f-bad" / "kept-commits").exists()
    assert not marks.marked(w.root, "f-bad") and leases(w) == [] and path.journals == set()


def test_a_holder_seen_after_the_rename_puts_the_tree_back(world):
    """Mutant 4: the holder scan runs again on the renamed tree; a holder
    there puts the tree back whole and takes back the refs."""
    w = world
    tree = nested_job(w, "h-late")
    before = snapshot(tree)

    def holders(watches, cancel=None):
        watch = watches.get("h-late")
        if watch and any(p.endswith("/" + rd.TOMBSTONE) and "/retention-delete/" in p for p in watch.prefixes):
            return {"h-late": ["pid 7: opened a file after the rename"]}
        return {}
    path = delete_path(w, holders=holders)
    line = results(run(path, request("h-late")))["h-late"]
    assert (line["gate"], line["disposition"]) == ("holders", "kept") and "after the rename" in line["reason"]
    assert snapshot(tree) == before and git(w.repo, "for-each-ref", "refs/subfleet-kept") == ""
    assert leases(w) == [] and path.journals == set()


def test_an_admin_directory_that_names_another_place_stays(world):
    """Mutant 12: a registration whose `gitdir` was pointed at another tree
    after the deletion began is not removed, and the marker says why."""
    w = world
    tree = nested_job(w, "a-moved")
    admin = w.base / "home" / "other-a-moved" / ".git" / "worktrees" / "nested"
    elsewhere = w.base / "elsewhere-tree"

    def on_step(job_id, point):
        if point == "removed":
            elsewhere.mkdir()
            (admin / "gitdir").write_text(str(elsewhere / ".git") + "\n")
    path = delete_path(w, on_step=on_step)
    line = results(run(path, request("a-moved")))["a-moved"]
    assert line["disposition"] == "deleted" and not tree.exists()
    assert admin.exists() and not w.admin("a-moved").exists()
    marker = marks.marker(w.root, "a-moved")
    assert [k["admin"] for k in marker["admins_kept"]] == [str(admin)] or \
        [os.path.realpath(k["admin"]) for k in marker["admins_kept"]] == [os.path.realpath(admin)]
    assert "not a place in the removed tree" in marker["admins_kept"][0]["why"]


def test_the_journal_lives_under_retention_delete_never_under_retention(world):
    """Mutant 15: `retention_archive.journals` reads every entry of
    `<state>/retention/` as a retirement; the delete path's journal is not one."""
    w = world
    nested_job(w, "j-where")
    seen = {}

    def on_step(job_id, point):
        if point == "kept":
            seen["journal"] = json.loads((w.root / "retention-delete" / job_id / "journal.json").read_text())
            seen["retirements"] = rarch.journals(w.root)
            seen["ids"] = marks.journal_ids(w.root)
            seen["leases"] = leases(w)
    line = results(run(delete_path(w, on_step=on_step), request("j-where")))["j-where"]
    assert line["disposition"] == "deleted"
    assert seen["journal"]["state"] == "kept" and seen["retirements"] == {} and seen["ids"] == {"j-where"}
    assert sorted(seen["leases"]) == sorted([("retire:j-where", "retention:j-where"),
                                             (seen["journal"]["keys"][1], "retention:j-where")])
    assert not (w.root / "retention").exists() or "j-where" not in os.listdir(w.root / "retention")


def test_the_normal_pass_never_takes_a_job_whose_deletion_is_in_flight(world):
    """Invariant 6 while a deletion is journalled (mutant 9): the normal pass
    keeps the job (`tree-delete in flight`), and does not release its leases."""
    w = world
    nested_job(w, "n-flight")
    w.job("n-normal", created=OLD)
    seen = {}

    def on_step(job_id, point):
        if point == "renamed":
            seen["pass"] = retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0,
                                                 holders=lambda watches, **_: {})
            seen["leases"] = leases(w)
    line = results(run(delete_path(w, on_step=on_step), request("n-flight")))["n-flight"]
    assert line["disposition"] == "deleted"
    assert "n-flight" not in seen["pass"]["pruned"] and "n-normal" in seen["pass"]["pruned"]
    assert ("retire:n-flight", "retention:n-flight") in seen["leases"]
    assert w.store.get_job("n-flight") is not None and (w.root / "jobs" / "n-flight" / "stdout").exists()


def test_a_store_failure_ends_the_run_and_a_later_step_resumes_the_journal(world, monkeypatch):
    """C-8.7: a store write failure ends the run (no further job is tried); the
    journal it leaves is finished or rolled back by a later step."""
    import sqlite3
    w = world
    t1 = nested_job(w, "s-1")
    t2 = nested_job(w, "s-2")
    before = snapshot(t1)
    path = delete_path(w)
    path.start(request("s-1", "s-2"), mode="apply")
    real = w.store.transaction
    state = {"failed": False}

    def failing(kind, **kw):
        if kind == "retention.delete_selected" and not state["failed"]:
            state["failed"] = True
            raise sqlite3.OperationalError("disk I/O error (test)")
        return real(kind, **kw)
    monkeypatch.setattr(w.store, "transaction", failing)
    report = path.step(**STEP)
    assert report.get("error") == "store"
    record = path.status()["apply"]
    assert record["status"] == "failed" and record["error"].startswith("store") and record["totals"]["pending"] == 2
    assert snapshot(t1) == before and t2.exists() and path.journals == set() and leases(w) == []
