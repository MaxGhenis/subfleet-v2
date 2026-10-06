"""C-8.4: `dependencies`, the places a job needs besides its folder.

Invariants these state and check (the fake daemon's tests in
`tests/fake/test_retention_dependencies.py` run them end to end):

- D1 (git storage covers git). For any layout of repositories, linked checkouts and
  separate git directories, the git directory and the common directory git itself
  uses for a folder are among `git_storage(folder)` (differential with `git
  rev-parse`, generated layouts).
- D2 (the pins are exactly the specification). A finished job's allocated tree is
  kept as `worktree-in-use` exactly when another job that has not ended has a
  recorded place (row workdir, worktree, review root or `-o` path; recorded folder
  or write target; `depends`; a legacy job's spelled folder) that is the tree or
  inside it, or another job holds `out:<path>` (a pending export) for a path there,
  comparing folded spellings; `dependents` agrees inside a transaction
  (differential with a direct oracle, generated stores).
- D3 (spelling does not matter). Re-spelling the tree, or any recorded place, in
  another case or Unicode normal form changes no pin.
- D4 (monotone). Adding a job that has not ended never releases a pin; ending one
  never adds a pin.
- D5 (fences agree with `folders.retiring`). `fences` reads in one statement what
  `folders.retiring` reads place by place.
- D6 (the reads stay indexed). The pin queries read a job's events by job id
  (`events_job`) and the pending exports by a range of the leases' key; neither
  scans `events`.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unicodedata
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import dependencies, folders, retention
from subfleet.store import Store
from tests.unit.retention_world import git

TERMINAL = ("succeeded", "failed", "cancelled", "lost")
STATES = ("queued", "running", "waiting", *TERMINAL)


def repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--quiet", "-b", branch)
    (path / "f.txt").write_text(path.name)
    git(path, "add", ".")
    git(path, "commit", "--quiet", "-m", path.name)
    return path


def git_says(path: Path) -> list[str]:
    """The git directory and common directory git uses for `path`, or [] outside a repository."""
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-dir",
                             "--git-common-dir"], capture_output=True, text=True,
                            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    if result.returncode:
        return []
    return [folders.canonical(line) for line in result.stdout.splitlines() if line]


# --- D1: git storage --------------------------------------------------------------------

def test_d1_a_linked_checkout_outside_names_the_repository_it_lives_in(tmp_path):
    """The review's P1 layout: a checkout made outside a tree from a repository in it.
    Its folder, and a folder inside it, name the repository's admin directory and
    common directory, inside the tree; a folder outside any checkout names none."""
    nested = repo(tmp_path / "tree" / "vendor" / "lib")
    outside = tmp_path / "outside"
    git(nested, "worktree", "add", "--quiet", "--detach", str(outside), "HEAD")
    (outside / "pkg").mkdir()
    common = folders.canonical(nested / ".git")
    admin = folders.canonical(nested / ".git" / "worktrees" / "outside")
    for path in (outside, outside / "pkg"):
        assert dependencies.git_storage(str(path))[:2] == [admin, common]
        assert set(git_says(path)) <= set(dependencies.git_storage(str(path)))
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    assert dependencies.git_storage(str(neutral)) == []


def test_d1_storage_a_gitfile_names_counts_when_it_is_gone(tmp_path):
    """A `.git` file names its admin directory whether or not it is there: retention
    may have the tree that holds it in quarantine. With the admin directory gone (no
    `commondir` to read), the repository is the one two levels above `worktrees/`."""
    nested = repo(tmp_path / "tree" / "lib")
    outside = tmp_path / "outside"
    git(nested, "worktree", "add", "--quiet", "--detach", str(outside), "HEAD")
    moved = tmp_path / "quarantine"
    (tmp_path / "tree").rename(moved)
    found = dependencies.git_storage(str(outside))
    assert str(tmp_path / "tree" / "lib" / ".git" / "worktrees" / "outside") in found
    assert str(tmp_path / "tree" / "lib" / ".git") in found


def test_d1_a_separate_git_dir_and_a_relative_gitfile(tmp_path):
    """`git init --separate-git-dir` writes an absolute `gitdir:`; a submodule writes a
    relative one, read relative to the folder holding the `.git` file."""
    work = tmp_path / "work"
    store = tmp_path / "elsewhere" / "store.git"
    store.parent.mkdir()
    git(tmp_path, "init", "--quiet", "--separate-git-dir", str(store), str(work))
    assert dependencies.git_storage(str(work))[0] == folders.canonical(store)
    assert set(git_says(work)) <= set(dependencies.git_storage(str(work)))
    rel = tmp_path / "rel"
    rel.mkdir()
    (rel / ".git").write_text("gitdir: ../elsewhere/store.git\n")
    assert dependencies.git_storage(str(rel))[0] == folders.canonical(store)
    assert set(git_says(rel)) <= set(dependencies.git_storage(str(rel)))


def test_d1_alternates_are_followed(tmp_path):
    """A clone that borrows objects (`--shared`, or an `alternates` line relative to
    the objects directory, followed again from there) needs the object directories
    it borrows from."""
    source = repo(tmp_path / "tree" / "source")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "--quiet", "--shared", str(source), str(clone))
    found = dependencies.git_storage(str(clone))
    assert folders.canonical(source / ".git" / "objects") in found
    middle = repo(tmp_path / "middle")
    alternates = middle / ".git" / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text(f"# borrowed\n{os.path.relpath(clone / '.git' / 'objects', middle / '.git' / 'objects')}\n")
    found = dependencies.git_storage(str(middle))
    assert folders.canonical(clone / ".git" / "objects") in found
    assert folders.canonical(source / ".git" / "objects") in found          # followed again


def test_d1_a_fifo_where_a_gitfile_is_neither_blocks_nor_counts(tmp_path):
    """A `.git` that is a FIFO is not read (it would block in open()), nor counted."""
    folder = tmp_path / "f"
    folder.mkdir()
    os.mkfifo(folder / ".git")
    assert dependencies.git_storage(str(folder)) == []


LAYOUT = st.lists(st.tuples(st.sampled_from(["repo", "linked", "dir"]), st.integers(0, 6),
                            st.sampled_from(["a", "b", "c"])), min_size=1, max_size=5)


@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(LAYOUT, st.integers(0, 50))
def test_d1_git_storage_includes_what_git_uses_for_any_layout(steps, pick):
    """D1, differential: build repositories, linked checkouts (of an earlier
    repository, anywhere, possibly inside another checkout) and plain folders at
    generated places; for every folder made, git's own git directory and common
    directory are among `git_storage`'s."""
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
        root = Path(directory).resolve()
        made: list[Path] = [root]
        repos: list[Path] = []
        for n, (what, where, name) in enumerate(steps):
            base = made[where % len(made)]
            path = base / f"{name}{n}"
            if what == "repo":
                repos.append(repo(path, branch=f"b{n}"))
            elif what == "linked" and repos:
                git(repos[n % len(repos)], "worktree", "add", "--quiet", "--detach", str(path), "HEAD")
            else:
                path.mkdir(parents=True)
            made.append(path)
        for path in made[1:]:
            sub = path / "sub"
            sub.mkdir(exist_ok=True)
            for each in (path, sub):
                assert set(git_says(each)) <= set(dependencies.git_storage(str(each))), each


# --- D2 to D4: the pins --------------------------------------------------------------

ROOT = "/s"
TREES = ("t1", "t2", "Té")


def spelled(path: str, how: int) -> str:
    """`path` in another case or Unicode form, as a volume that folds them finds alike."""
    return [path, path.upper(), unicodedata.normalize("NFD", path), path.swapcase()][how % 4]


#: A place: a tree (or none), where in it, and how it is spelled.
PLACE = st.tuples(st.sampled_from([*TREES, "t1x", "out"]), st.sampled_from(["", "/a", "/a/b"]), st.integers(0, 3))


def place(spec) -> str:
    tree, below, how = spec
    base = f"{ROOT}/worktrees/{tree}" if tree != "out" else "/elsewhere"
    return spelled(base + below, how)


JOB = st.fixed_dictionaries({
    "state": st.sampled_from(STATES),
    "workdir": PLACE, "worktree": st.none() | PLACE, "review_root": st.none() | PLACE, "out_path": st.none() | PLACE,
    "recorded": st.sampled_from(["new", "legacy", "spelled"]),
    "folder": st.none() | PLACE, "depends_review": st.none() | PLACE, "depends_out": st.none() | PLACE,
    "git": st.lists(PLACE, max_size=2), "exporting": st.booleans(),
})
OWNERS = st.lists(st.tuples(st.sampled_from(TREES), st.booleans()), max_size=3, unique_by=lambda o: o[0])


def build(store: Store, owners, jobs) -> dict[str, set[str]]:
    """Fill `store`: finished owners of trees (recorded or unrecorded worktrees) and
    the generated jobs; returns, for the oracle, each job's places and whether it
    counts (has not ended), and each pending export's paths."""
    places: dict[str, tuple[bool, set[str], set[str]]] = {}
    for tree, recorded in owners:
        store.add_job(job_id=tree, request_id="r-" + tree, payload_digest="d", kind="dispatch", workdir="/src",
                      worktree=f"{ROOT}/worktrees/{tree}" if recorded else None, prompt_path="/p",
                      sandbox="workspace-write", state="succeeded")
    for n, job in enumerate(jobs):
        job_id = f"j{n}"
        row = {"workdir": place(job["workdir"]), **{k: place(job[k]) if job[k] else None
                                                    for k in ("worktree", "review_root", "out_path")}}
        store.add_job(job_id=job_id, request_id="r-" + job_id, payload_digest="d", kind="dispatch",
                      prompt_path="/p", sandbox="read-only", state=job["state"], **row)
        submitted: dict = {}
        if job["recorded"] == "new":
            submitted = {**({"folder": place(job["folder"])} if job["folder"] else {}),
                         "depends": {**({"review_root": place(job["depends_review"])} if job["depends_review"] else {}),
                                     **({"out_path": place(job["depends_out"])} if job["depends_out"] else {}),
                                     **({"git": [place(g) for g in job["git"]]} if job["git"] else {})}}
        with store.transaction("job.submitted", job_id=job_id, data=submitted or None) as tx:
            tx.execute("UPDATE jobs SET name=? WHERE job_id=?", ("n", job_id))      # the audit event is the record
        spelled_folder = place(job["folder"]) if job["recorded"] == "spelled" and job["folder"] else None
        if spelled_folder:
            store.add_event("job.folder_spelled", job_id=job_id, data={"folder": spelled_folder})
        mine = {p for p in row.values() if p}
        if job["recorded"] == "new":
            mine |= {p for p in (submitted.get("folder"), *submitted["depends"].values()) if isinstance(p, str)}
            mine |= set(submitted["depends"].get("git") or ())
        if spelled_folder:
            mine.add(spelled_folder)
        exports = set()
        if job["exporting"] and row["out_path"] and store.acquire_lease(f"out:{row['out_path']}", job_id):
            exports = {row["out_path"], *([submitted["depends"]["out_path"]]
                                          if job["recorded"] == "new" and job["depends_out"] else [])}
        places[job_id] = (job["state"] not in TERMINAL, mine, exports)
    return places


def folded_within(path: str, tree: str) -> bool:
    a, b = dependencies.key(path), dependencies.key(tree)
    return a == b or a.startswith(b + "/")


def oracle(places, tree: str, exclude: str) -> bool:
    return any(job_id != exclude and ((live and any(folded_within(p, tree) for p in mine))
                                     or any(folded_within(p, tree) for p in exports))
               for job_id, (live, mine, exports) in places.items())


def fresh_store() -> tuple[Store, tempfile.TemporaryDirectory]:
    directory = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
    return Store(Path(directory.name) / "state.sqlite3"), directory


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(OWNERS, st.lists(JOB, max_size=6), st.integers(0, 3))
def test_d2_d3_worktree_in_use_is_exactly_the_specification(owners, jobs, how):
    """D2 and D3: `_pin_reasons` (pass and `only`) keeps an owner's tree as
    `worktree-in-use` exactly when the oracle says another job needs a place in it,
    for recorded and unrecorded trees; `dependents`, read inside a transaction on
    the tree spelled any way, names a job exactly then."""
    store, directory = fresh_store()
    try:
        places = build(store, owners, jobs)
        reasons = retention._pin_reasons(store, set(), None, root=Path(ROOT))
        for tree, _ in owners:
            path = f"{ROOT}/worktrees/{tree}"
            expected = oracle(places, path, tree)
            assert (reasons.get(tree) == "worktree-in-use") == expected, (tree, reasons, places)
            only = retention._pin_reasons(store, set(), None, root=Path(ROOT), only=tree)
            assert (only.get(tree) == "worktree-in-use") == expected
            with store.transaction("test.read") as tx:
                found = dependencies.dependents(lambda sql, params: tx.execute(sql, params).fetchall(),
                                                spelled(path, how), exclude=tree)
            assert bool(found) == expected, (tree, found, places)
            assert set(found) == {job_id for job_id in places if job_id != tree and oracle({job_id: places[job_id]},
                                                                                         path, tree)}
    finally:
        store.close()
        directory.cleanup()


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(OWNERS.filter(bool), st.lists(JOB, max_size=5), JOB, st.data())
def test_d4_a_job_that_has_not_ended_never_releases_a_pin_and_ending_one_never_adds_one(owners, jobs, extra, data):
    """D4: the pin set grows when a job that has not ended is added, and shrinks (or
    stays) when one ends (its export written: no `out:` lease left)."""
    extra = {**extra, "state": data.draw(st.sampled_from(("queued", "running", "waiting")))}
    store, directory = fresh_store()
    try:
        build(store, owners, jobs)
        before = {k for k, v in retention._pin_reasons(store, set(), None, root=Path(ROOT)).items()
                  if v == "worktree-in-use"}
        build_one(store, extra, "extra")
        after = {k for k, v in retention._pin_reasons(store, set(), None, root=Path(ROOT)).items()
                 if v == "worktree-in-use"}
        assert before <= after
        store.update_job("extra", state="succeeded")
        store.release_leases("extra")
        ended = {k for k, v in retention._pin_reasons(store, set(), None, root=Path(ROOT)).items()
                 if v == "worktree-in-use"}
        assert ended <= after and ended == before
    finally:
        store.close()
        directory.cleanup()


def build_one(store: Store, job, job_id: str) -> None:
    row = {"workdir": place(job["workdir"]), **{k: place(job[k]) if job[k] else None
                                                for k in ("worktree", "review_root", "out_path")}}
    store.add_job(job_id=job_id, request_id="r-" + job_id, payload_digest="d", kind="dispatch", prompt_path="/p",
                  sandbox="read-only", state=job["state"], **row)
    depends = {**({"git": [place(g) for g in job["git"]]} if job["git"] else {})}
    with store.transaction("job.submitted", job_id=job_id, data={"depends": depends}) as tx:
        tx.execute("UPDATE jobs SET name=? WHERE job_id=?", ("n", job_id))
    if job["exporting"] and row["out_path"]:
        store.acquire_lease(f"out:{row['out_path']}", job_id)


def test_d2_a_pending_export_pins_and_a_written_one_does_not(tmp_path):
    """An ended job still holding `out:<path>` keeps a tree its path is in; once the
    lease goes (its export written, `Daemon._export`), nothing of it does."""
    store = Store(tmp_path / "s.sqlite3")
    try:
        store.add_job(job_id="tree", request_id="t", payload_digest="d", kind="dispatch", workdir="/src",
                      worktree="/s/worktrees/tree", prompt_path="/p", sandbox="workspace-write", state="succeeded")
        store.add_job(job_id="writer", request_id="w", payload_digest="d", kind="dispatch", workdir="/x",
                      out_path="/s/worktrees/TREE/out.md", prompt_path="/p", sandbox="read-only", state="succeeded")
        assert "tree" not in retention._pin_reasons(store, set(), None)
        store.acquire_lease("out:/s/worktrees/TREE/out.md", "writer")
        assert retention._pin_reasons(store, set(), None)["tree"] == "worktree-in-use"
        store.release_leases("writer")
        assert "tree" not in retention._pin_reasons(store, set(), None)
    finally:
        store.close()


def test_d2_legacy_places_count_only_while_their_job_has_not_ended(tmp_path):
    """What `legacy` read for a job queued before `depends` was recorded counts like
    `depends` while that job has not ended, and not after."""
    store = Store(tmp_path / "s.sqlite3")
    try:
        store.add_job(job_id="tree", request_id="t", payload_digest="d", kind="dispatch", workdir="/src",
                      worktree="/s/worktrees/tree", prompt_path="/p", sandbox="workspace-write", state="succeeded")
        store.add_job(job_id="old", request_id="o", payload_digest="d", kind="dispatch", workdir="/outside",
                      prompt_path="/p", sandbox="read-only", state="queued")
        places = {"old": ["/s/worktrees/tree/lib/.git"]}
        assert "tree" not in retention._pin_reasons(store, set(), None)
        assert retention._pin_reasons(store, set(), None, legacy=places)["tree"] == "worktree-in-use"
        store.update_job("old", state="cancelled")
        assert "tree" not in retention._pin_reasons(store, set(), None, legacy=places)
    finally:
        store.close()


def test_d2_legacy_reads_only_jobs_queued_before_depends_was_recorded(tmp_path):
    """`legacy` reads the places of a job that has not ended and whose submit recorded
    no `depends`: the git storage of its outside checkout. A newer job, and an ended
    one, are not read."""
    nested = repo(tmp_path / "tree" / "lib")
    outside = tmp_path / "outside"
    git(nested, "worktree", "add", "--quiet", "--detach", str(outside), "HEAD")
    store = Store(tmp_path / "s.sqlite3")
    try:
        for job_id, state, data in (("old", "queued", {}), ("new", "queued", {"depends": {}}),
                                    ("ended", "failed", {})):
            store.add_job(job_id=job_id, request_id=job_id, payload_digest="d", kind="dispatch",
                          workdir=str(outside), prompt_path="/p", sandbox="read-only", state=state)
            with store.transaction("job.submitted", job_id=job_id, data=data or None) as tx:
                tx.execute("UPDATE jobs SET name='n' WHERE job_id=?", (job_id,))
        found = dependencies.legacy(store.query)
        assert set(found) == {"old"}
        assert folders.canonical(nested / ".git") in found["old"]
    finally:
        store.close()


# --- D5: fences ------------------------------------------------------------------------

FOLDERS = st.sampled_from(["/a", "/a/b", "/a/b/c", "/a/bc", "/a/b:c", "/x", "/x/y"])


@settings(max_examples=200, deadline=None)
@given(st.dictionaries(FOLDERS, st.sampled_from(["retention:r1", "retention:r2", "job-1", "gate-round:g"]),
                       max_size=5),
       st.lists(st.tuples(st.sampled_from([None, "review-root", "output", "git-storage"]), FOLDERS), max_size=4))
def test_d5_fences_reads_what_retiring_reads_place_by_place(held, places):
    """D5, differential: for any leases and places, `fences` answers what
    `folders.retiring` answers for each place, in order, in one statement."""
    store, directory = fresh_store()
    try:
        for folder, holder in held.items():
            store.acquire_lease(folders.exclusive_key(folder), holder)
        statements = []

        def read(sql, params):
            statements.append(sql)
            return store.query(sql, params)

        found = dependencies.fences(read, places)
        expected = [(role, path, keys) for role, path in places if (keys := folders.retiring(store.query, path))]
        assert found == expected
        assert len(statements) <= 1
    finally:
        store.close()
        directory.cleanup()


# --- D6: the reads stay indexed ---------------------------------------------------------

def test_d6_the_pin_reads_use_the_job_index_and_a_lease_range(tmp_path):
    """D6: SQLite plans `live`'s reads by job id on `events` (`events_job`) and by a
    range of `leases`' key, never a scan of `events`."""
    store = Store(tmp_path / "s.sqlite3")
    try:
        for sql in (dependencies._LIVE, dependencies._EXPORTING):
            plan = " | ".join(row["detail"] for row in store.query("EXPLAIN QUERY PLAN " + sql))
            assert "events_job" in plan and "SCAN e" not in plan and "SCAN events" not in plan, plan
        plan = " | ".join(row["detail"] for row in store.query("EXPLAIN QUERY PLAN " + dependencies._EXPORTING))
        assert "SEARCH leases" in plan, plan
        plan = " | ".join(row["detail"] for row in store.query("EXPLAIN QUERY PLAN " + dependencies._SPELLED, ("j",)))
        assert "events_job" in plan, plan
    finally:
        store.close()


# --- record ------------------------------------------------------------------------------

def test_record_spells_each_place_once(tmp_path):
    """Submit's record: the review root and `-o` path in the one spelling the volume
    stores (`folders.canonical`), whatever case they were given in, and the git
    storage of the workdir and of the review root."""
    review = repo(tmp_path / "Review")
    work = repo(tmp_path / "work")
    typed = tmp_path / "REVIEW"
    if not typed.exists():
        pytest.skip("this volume tells case apart")
    found = dependencies.record(str(work), review_root=str(typed), out_path=str(typed / "Out.md"))
    assert found["review_root"] == folders.canonical(review)
    assert found["out_path"] == folders.canonical(review) + "/Out.md"
    assert found["git"] == [folders.canonical(work / ".git"), folders.canonical(review / ".git")]
    assert json.loads(json.dumps(found)) == found
    assert dependencies.needed({"depends": found}) == [
        ("review-root", found["review_root"]), ("output", found["out_path"]),
        *(("git-storage", each) for each in found["git"])]
    assert dependencies.needed({}) is None


# --- the transactions read the fence's spelling ---------------------------------------

@pytest.fixture
def world(tmp_path):
    from tests.unit.retention_world import World
    w = World(tmp_path)
    yield w
    w.close()


def run(world):
    from tests.unit.retention_world import Clock
    return retention.maintenance(world.store, world.root, max_jobs=0, max_bytes=0,
                                 holders=lambda watches, **_: {}, clock=Clock())


def respelled(world, job_id: str) -> None:
    """Record the job's worktree as an older daemon may have: through a symlinked
    alias of the state root (as `/var` is of `/private/var`), a spelling no fold
    makes canonical."""
    alias = world.base / "alias"
    if not alias.exists():
        alias.symlink_to(world.root)
    world.store.update_job(job_id, worktree=str(alias / "worktrees" / job_id))


def needing_git(world, job_id: str, place: str) -> None:
    """A queued job whose checkout's git storage, as submit recorded it, is `place`."""
    world.store.add_job(job_id=job_id, request_id="r-" + job_id, payload_digest="d", kind="dispatch",
                        workdir="/elsewhere", prompt_path="/p", sandbox="read-only", state="queued")
    with world.store.transaction("job.submitted", job_id=job_id, data={"depends": {"git": [place]}}) as tx:
        tx.execute("UPDATE jobs SET name='n' WHERE job_id=?", (job_id,))


def test_the_selecting_transaction_reads_dependents_on_the_fences_spelling(world):
    """The pass compares a job's recorded worktree as recorded (folded); a spelling
    through a symlink is not the canonical one the fence is on. The selecting
    transaction reads the dependents again on the fence's spelling, so a job that
    needs the tree keeps it there: nothing is moved and nothing is rolled back."""
    wt = world.job("job")
    respelled(world, "job")
    before = snapshot_of(wt)
    needing_git(world, "needs", folders.canonical(wt) + "/lib/.git")
    result = run(world)
    assert "job" in result["protected"] and "job" not in result["pruned"], result
    assert "job" not in result["deferred"] and result["pin_reasons"].get("job") is None, result
    assert snapshot_of(wt) == before


def test_the_commit_reads_dependents_on_the_journals_spelling(world, monkeypatch):
    """A job that needs the tree, submitted after selection, keeps it at the commit,
    which reads the dependents on the journal's (the fence's) spelling: the
    retirement is rolled back and the tree comes back as it was."""
    from subfleet import retention_archive as rarch
    wt = world.job("job")
    respelled(world, "job")
    before = snapshot_of(wt)
    real = rarch.Retirement.begin

    def begin(self, *args, **kwargs):
        needing_git(world, "needs", folders.canonical(wt) + "/lib/.git")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(rarch.Retirement, "begin", begin)
    result = run(world)
    assert "job" not in result["pruned"] and result["deferred"]["job"] == "pinned: worktree-in-use", result
    assert snapshot_of(wt) == before


def snapshot_of(path: Path):
    from tests.unit.retention_world import snapshot
    return snapshot(path)
