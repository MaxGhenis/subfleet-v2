"""C-8.4: a `worktree:` lease on a folder inside a job's own worktree keeps the job.

A detached in-place writer in `<wt>/vendor/lib`, a repository nested in job J's
allocated worktree `<wt>`, holds `worktree:<wt>/vendor/lib` (C-6.5). When its
attempt is quarantined its job is `lost` and keeps that lease (daemon
`_quarantine`), so #76's `worktree-in-use` pin, which counts only jobs not yet
ended, no longer keeps J. The `worktree-lease` pin matched the worktree itself
only, and the selecting transaction checked only `worktree:<wt>`, so retention
archived J and deleted its tree under the writer's folder (review of 31048e67,
F3, probe C; the same on 8a112986 and on ceb1f15f).

The rule mirrors a turn row's (test_retention_shared_folders.py, review of
599af189, P3-1 and P3-2): a lease held by anything other than `retention:<J>` on
J's worktree or a folder inside it keeps J when J has its own allocated tree,
in the pin census (recorded spellings), the selecting transaction and the
commit (canonical spelling). Each layer is tested alone, the others blinded.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import folders, retention
from subfleet import retention_archive as rarch
from subfleet.store import Store
from tests.unit.retention_world import World, snapshot
from tests.unit.test_retention_shared_folders import NAME, TREE, nested_repository, run


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def writer(world, folder: str, *, state: str = "lost") -> None:
    """A detached in-place writer in `folder`, holding `worktree:<folder>` as
    admission gives it (daemon `_admit`). `lost`: its attempt was quarantined,
    and the job keeps the lease until the quarantine is resolved."""
    world.store.add_job(job_id="writer", request_id="req-writer", payload_digest="d", kind="dispatch",
                        workdir=folder, worktree=folder, in_place=1, prompt_path="/prompt",
                        sandbox="workspace-write", state=state)
    world.attempt("writer", state="quarantined" if state == "lost" else "running")
    assert world.store.acquire_lease(folders.exclusive_key(folder), "writer")


@pytest.mark.parametrize("writer_state", ["running", "lost"])
def test_a_writer_in_a_repository_nested_in_the_job_worktree_keeps_it(world, writer_state):
    """Probe C of the review of 31048e67 (F3): while the writer runs, #76's
    `worktree-in-use` pin keeps the host; once its attempt is quarantined only
    its lease says the folder is in use. Failed on 8a112986 for `lost`: the host
    had no pin reason and was pruned, its tree deleted after being archived."""
    wt = world.job("host")
    folder = nested_repository(wt)
    writer(world, folder, state=writer_state)
    before = snapshot(wt)
    reasons = retention._pin_reasons(world.store, set(), None)
    assert reasons["host"] == ("worktree-in-use" if writer_state == "running" else "worktree-lease"), reasons
    result = run(world)
    assert "host" not in result["pruned"] and "host" in result["protected"], result
    assert snapshot(wt) == before and world.store.get_job("host") and world.admin("host").is_dir()
    if writer_state == "lost":
        world.store.release_leases("writer")          # the quarantine is resolved: the lease goes
        result = run(world)
        assert result["pruned"] == ["host"], result   # the writer's quarantined attempt keeps it
        assert not wt.exists() and not world.admin("host").exists()


def test_the_census_sees_a_writer_in_a_folder_inside_the_worktree(world, monkeypatch):
    """The pins alone: the selecting and committing transactions' checks are
    blinded, so only `_pin_reasons` can keep the job."""
    wt = world.job("job")
    writer(world, nested_repository(wt))
    before = snapshot(wt)
    real = folders.exclusive_inside
    monkeypatch.setattr(folders, "exclusive_inside", lambda read, folder: [])
    assert retention._pin_reasons(world.store, set(), None)["job"] == "worktree-lease"
    result = run(world)
    assert result["pruned"] == [] and "job" in result["protected"], result
    assert snapshot(wt) == before and world.store.get_job("job")
    monkeypatch.setattr(folders, "exclusive_inside", real)
    world.store.release_leases("writer")
    assert run(world)["pruned"] == ["job"]


def test_a_writer_between_selection_and_transaction_stops_retirement(world, monkeypatch):
    """The selecting transaction alone: the census is blinded and the lease is
    taken after selection, so only the transaction's range over the folders
    inside the tree can refuse the job, before any of it is archived."""
    wt = world.job("job")
    folder = nested_repository(wt)
    before = snapshot(wt)
    start = retention._Pass._start

    def race(driver, job, protected):
        assert not driver.store._holds_writer()
        writer(world, folder)
        return start(driver, job, protected)

    monkeypatch.setattr(retention._Pass, "_start", race)
    monkeypatch.setattr(folders, "exclusive_folders", lambda read: [])
    result = run(world)
    assert result["pruned"] == [] and "job" in result["protected"], result
    assert "job" not in result["deferred"] and rarch.load_journal(world.root, "job") is None, result
    assert snapshot(wt) == before and world.admin("job").is_dir() and world.store.get_job("job")
    assert {r["holder"] for r in world.store.list_leases()} == {"writer"}


def test_a_recorded_alias_is_kept_by_the_selection_range(world):
    """The selecting transaction reads the leases on the canonical spelling: with
    `jobs.worktree` recorded in another case, the census (recorded spellings)
    misses a writer's lease inside the tree, and the selecting transaction
    alone refuses the job, before any of it is archived. Nothing is blinded
    (review of c3cf6a55, F2: a selection on the recorded spelling let the
    retirement begin, and only the commit rolled it back)."""
    wt = world.job("Job")
    alias = wt.with_name(wt.name.swapcase())
    if not alias.exists() or not alias.samefile(wt):
        pytest.skip("case-insensitive filesystem required")
    world.store.update_job("Job", worktree=str(alias))
    writer(world, nested_repository(wt))
    before = snapshot(wt)
    assert "Job" not in retention._pin_reasons(world.store, set(), None)
    result = run(world)
    assert result["pruned"] == [] and "Job" in result["protected"], result
    assert "Job" not in result["deferred"] and rarch.load_journal(world.root, "Job") is None, result
    assert snapshot(wt) == before and world.admin("Job").is_dir() and world.store.get_job("Job")
    world.store.release_leases("writer")
    assert run(world)["pruned"] == ["Job"] and not wt.exists()


@pytest.mark.parametrize("recorded", ["census", "alias", "unrecorded"])
def test_the_commit_recheck_sees_a_writer_inside_the_tree(world, monkeypatch, recorded):
    """The commit alone: the lease appears after verification (admission fences
    only a writer's own folder), so neither the census nor the selecting
    transaction saw it. `census`: the commit's canonical check is blinded and
    its pins (`ctx.pinned`, recorded spelling) find the lease. `alias` and
    `unrecorded`: `jobs.worktree` is spelt in another case, or was never
    recorded, so the pins miss it and only the canonical check on the journal's
    spelling stops the commit (as for turn rows, review of 31048e67, F1)."""
    wt = world.job("Job")
    folder = nested_repository(wt)
    real = folders.exclusive_inside
    if recorded == "census":
        monkeypatch.setattr(folders, "exclusive_inside", lambda read, folder: [])
    elif recorded == "alias":
        alias = wt.with_name(wt.name.swapcase())
        if not alias.exists() or not alias.samefile(wt):
            pytest.skip("case-insensitive filesystem required")
        world.store.update_job("Job", worktree=str(alias))
    else:
        world.store.update_job("Job", worktree=None)       # allocated, never recorded (C-6.12)
    before = snapshot(wt)
    check = rarch.Retirement.final_check

    def late(retirement):
        check(retirement)
        writer(world, folder)

    monkeypatch.setattr(rarch.Retirement, "final_check", late)
    result = run(world)
    assert result["pruned"] == [] and "Job" in result["protected"], result
    assert result["deferred"]["Job"] == "pinned: worktree-lease", result
    assert world.store.get_job("Job") and snapshot(wt) == before and world.admin("Job").is_dir()
    world.store.release_leases("writer")
    monkeypatch.setattr(folders, "exclusive_inside", real)
    monkeypatch.setattr(rarch.Retirement, "final_check", check)
    assert "Job" in run(world)["pruned"] and not wt.exists()


@pytest.mark.parametrize("beside", ["2", "-copy", ".x", ":x", ";", "parent", "root"])
def test_a_writer_beside_or_above_the_worktree_keeps_nothing(world, beside):
    """Inside is a folder below the tree, not one whose name extends it (`<wt>2`,
    `<wt>-copy`, `<wt>.x`, `<wt>:x`, `<wt>;`) and not one above it (the folder
    holding every tree, or `/`): such a lease keeps nothing, in the census or at
    selection."""
    wt = world.job("job")
    folder = folders.canonical(wt)
    other = {"parent": str(Path(folder).parent), "root": "/"}.get(beside, folder + beside)
    assert world.store.acquire_lease(folders.exclusive_key(other), "writer")
    assert "job" not in retention._pin_reasons(world.store, set(), None)
    assert folders.exclusive_inside(world.store.query, folder) == []
    assert run(world)["pruned"] == ["job"]
    assert not wt.exists()


@pytest.mark.parametrize("layer", ["census", "selection", "commit"])
@pytest.mark.parametrize("holder", ["retention:job", "retention:other"])
def test_only_another_holder_keeps_the_job(world, monkeypatch, holder, layer):
    """A lease held by the job's own retirement keeps it in no layer; any other
    holder keeps it, also another job's retirement. Retention holds only its
    tree's own key, so inside the tree this is a key it never writes, and a pass
    first releases every `retention:` lease with no journal (`_recover`): the
    lease is taken after that, in each layer alone, the census blinded for the
    transactions. The filter is the rule as stated, as for `worktree-lease` on
    the tree itself."""
    wt = world.job("job")
    folder = nested_repository(wt)
    own = holder == "retention:job"
    if layer == "census":
        assert world.store.acquire_lease(folders.exclusive_key(folder), holder)
        reasons = retention._pin_reasons(world.store, set(), None)
        assert reasons.get("job") == (None if own else "worktree-lease"), reasons
        return
    monkeypatch.setattr(folders, "exclusive_folders", lambda read: [])
    if layer == "selection":
        start = retention._Pass._start

        def race(driver, job, protected):
            assert world.store.acquire_lease(folders.exclusive_key(folder), holder)
            return start(driver, job, protected)

        monkeypatch.setattr(retention._Pass, "_start", race)
    else:
        check = rarch.Retirement.final_check

        def late(retirement):
            check(retirement)
            assert world.store.acquire_lease(folders.exclusive_key(folder), holder)

        monkeypatch.setattr(rarch.Retirement, "final_check", late)
    result = run(world)
    if own:
        assert result["pruned"] == ["job"] and not wt.exists(), result
        assert world.store.list_leases() == []        # the commit released all of its own
    else:
        assert result["pruned"] == [] and "job" in result["protected"] and wt.exists(), result
        assert result["deferred"].get("job") == (None if layer == "selection" else "pinned: worktree-lease")


@pytest.mark.parametrize("sandbox,in_place", [("read-only", 0), ("workspace-write", 1)])
def test_a_lease_inside_keeps_no_job_without_its_own_tree(world, sandbox, in_place):
    """As for turn rows (P3-2): a job with no tree of its own (read-only, or in
    place, whose folder retention never removes) is kept by a lease on its
    recorded worktree, as before, and not by one on a folder inside it."""
    wt = world.job("job")
    world.store.update_job("job", sandbox=sandbox, in_place=in_place)
    folder = nested_repository(wt)
    assert world.store.acquire_lease(folders.exclusive_key(folder), "writer")
    before = snapshot(wt)
    assert "job" not in retention._pin_reasons(world.store, set(), None)
    assert run(world)["pruned"] == ["job"]
    assert snapshot(wt) == before                    # not its tree: retention leaves it


@pytest.mark.parametrize("layer", ["census", "selection", "commit"])
def test_no_filesystem_work_inside_a_retention_transaction(world, monkeypatch, layer):
    """Folder spellings are established before the store lock is taken: whichever
    layer finds the lease (the pins in a transaction; the selecting
    transaction's range, the census blinded; the commit's canonical range, the
    pins blinded), no folder is spelt while the store's writer lock is held."""
    wt = world.job("job")
    folder = nested_repository(wt)
    spelling, kernel = folders.spelling, folders._kernel_path

    def guarded(real):
        def call(*args, **kwargs):
            if world.store._holds_writer():
                pytest.fail("folder spelling must be established outside retention transactions")
            return real(*args, **kwargs)
        return call

    monkeypatch.setattr(folders, "spelling", guarded(spelling))
    monkeypatch.setattr(folders, "_kernel_path", guarded(kernel))
    if layer == "census":
        assert world.store.acquire_lease(folders.exclusive_key(folder), "writer")
        with world.store.transaction():
            assert retention._pin_reasons(world.store, set(), None, only="job")["job"] == "worktree-lease"
        return
    monkeypatch.setattr(folders, "exclusive_folders", lambda read: [])
    if layer == "selection":
        start = retention._Pass._start

        def race(driver, job, protected):
            assert world.store.acquire_lease(folders.exclusive_key(folder), "writer")
            return start(driver, job, protected)

        monkeypatch.setattr(retention._Pass, "_start", race)
    else:
        check = rarch.Retirement.final_check

        def late(retirement):
            check(retirement)
            assert world.store.acquire_lease(folders.exclusive_key(folder), "writer")

        monkeypatch.setattr(rarch.Retirement, "final_check", late)
    result = run(world)
    assert result["pruned"] == [] and "job" in result["protected"] and result["errors"] == [], result
    assert result["deferred"].get("job") == (None if layer == "selection" else "pinned: worktree-lease"), result


# --- the pin as a property --------------------------------------------------------


@st.composite
def leases(draw):
    """Jobs with and without their own tree, and `worktree:` leases on their
    trees and on folders inside, beside, above and elsewhere (every spelling the
    separators '/' and ':' and their neighbours ';', '0', '-', '.' can confuse),
    held by a writer, the job's own retirement or another job's."""
    jobs = []
    for n in range(draw(st.integers(1, 5))):
        tree = draw(st.one_of(TREE, st.none()))
        jobs.append(dict(job_id=f"job-{n}", worktree=tree, in_place=draw(st.booleans()),
                         sandbox=draw(st.sampled_from(["workspace-write", "read-only"]))))
    trees = [job["worktree"] for job in jobs if job["worktree"]] or ["/t/a"]
    near = st.sampled_from(trees).flatmap(lambda tree: st.one_of(
        st.just(tree), NAME.map(lambda name: f"{tree}/{name}"), NAME.map(lambda name: tree + name),
        st.just(tree.rsplit("/", 1)[0] or "/"), TREE))
    holder = st.one_of(st.integers(0, 3).map(lambda n: f"writer-{n}"),
                       st.sampled_from([f"retention:{job['job_id']}" for job in jobs]))
    rows = draw(st.lists(st.tuples(near, holder), max_size=6, unique_by=lambda row: row[0]))
    return jobs, rows


@settings(max_examples=150, deadline=None, derandomize=True)
@given(leases())
def test_the_worktree_lease_pin_is_exactly_a_lease_on_or_in_the_tree(case):
    """For every job and `worktree:` lease: `worktree-lease` keeps a job iff some
    lease not held by its own retirement is on its recorded worktree (any job, as
    before) or, for a job with its own allocated tree (writable, not in place),
    on a folder inside it, checked against `folder == wt or
    folder.startswith(wt + "/")`. The per-job reading (`only=`, the selecting and
    committing transactions') agrees with the census, and so does the
    transactions' range (`exclusive_inside` and the tree's own key) for every job
    with its own tree (differential, recorded and canonical spellings equal)."""
    jobs, rows = case
    with tempfile.TemporaryDirectory(prefix="retention-pin-") as temporary:
        store = Store(Path(temporary) / "state.sqlite3")
        try:
            for job in jobs:
                store.add_job(request_id="req-" + job["job_id"], payload_digest="d", kind="dispatch",
                              workdir="/t", prompt_path="/prompt", state="succeeded", **job)
            for folder, holder in rows:
                assert store.acquire_lease(folders.exclusive_key(folder), holder)
            census = retention._pin_reasons(store, set(), None)
            for job in jobs:
                job_id, tree = job["job_id"], job["worktree"]
                owned = bool(tree and job["sandbox"] == "workspace-write" and not job["in_place"])
                others = [folder for folder, holder in rows if holder != f"retention:{job_id}"]
                want = bool(tree and any(folder == tree or (owned and folder.startswith(tree + "/"))
                                         for folder in others))
                assert (census.get(job_id) == "worktree-lease") == want, (job, rows, census)
                assert set(census.values()) <= {"worktree-lease"}, census
                alone = retention._pin_reasons(store, set(), None, only=job_id)
                assert alone.get(job_id) == census.get(job_id), (job, rows)
                if owned:
                    exact = store.one("SELECT holder FROM leases WHERE lease_key=?", (folders.exclusive_key(tree),))
                    selected = bool(exact and exact["holder"] != f"retention:{job_id}") or any(
                        holder != f"retention:{job_id}" for _, holder in folders.exclusive_inside(store.query, tree))
                    assert selected == want, (job, rows)
        finally:
            store.close()
