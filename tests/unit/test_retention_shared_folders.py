"""I5 on the archive driver: shared turn rows, atomic fences and interleavings.

All filesystem work is in real temporary Git repositories; no provider or
live daemon is launched. Readers and writers are checked independently.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import example, given, settings, strategies as st

from subfleet import folders, retention
from subfleet import retention_archive as rarch
from subfleet.store import Store
from tests.unit.retention_world import Clock, World, git, snapshot


def run(w):
    return retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0,
                                 holders=lambda watches, **_: {}, clock=Clock())


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


@pytest.fixture
def begun(monkeypatch):
    """The jobs whose retirement began (`Retirement.begin`). The selecting
    transaction keeps a job before its tree is quarantined; the commit's own
    checks also keep it, but only by rolling back a tree that was moved from
    under the live turn meanwhile. So a test of the selection layer asserts
    this stays empty, not just that the tree survived (8a112986's commit check
    otherwise hides the selection check's removal)."""
    started = []
    real = rarch.Retirement.begin

    def begin(retirement, job, pool):
        started.append(retirement.job_id)
        return real(retirement, job, pool)

    monkeypatch.setattr(rarch.Retirement, "begin", begin)
    return started


def nested_repository(wt: Path) -> str:
    """The folder a conversation opened in `<wt>/vendor/lib`, its own repository,
    keys its rows on: that repository's top level, canonical, inside `wt`."""
    from subfleet.salvage import git_toplevel
    nested = wt / "vendor" / "lib"
    nested.mkdir(parents=True)
    git(nested, "init", "--quiet", "-b", "main")
    (nested / "f.txt").write_text("nested\n")
    git(nested, "add", ".")
    git(nested, "commit", "--quiet", "-m", "nested")
    folder = folders.canonical(git_toplevel(str(nested)))
    assert folder != folders.canonical(wt) and folders.within(folder, folders.canonical(wt))
    return folder


def turn_folder(wt: Path, where: str) -> str:
    return folders.canonical(wt) if where == "tree" else nested_repository(wt)


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_live_turn_folder_protects_job_through_full_pass(world, writable):
    wt = world.job("job")
    before = snapshot(wt)
    key = folders.turn_key(folders.canonical(wt), "live-turn", writable=writable)
    assert world.store.acquire_lease(key, "live-turn")
    reasons = retention._pin_reasons(world.store, set(), None)
    result = run(world)
    assert result["pruned"] == [] and "job" in result["protected"], result
    assert reasons["job"] == "turn-folder"
    assert world.store.get_job("job") and snapshot(wt) == before
    assert world.admin("job").is_dir()
    assert not (world.root / "retention" / "job").exists()
    world.store.release_leases("live-turn")
    result = run(world)
    assert result["pruned"] == ["job"], result
    assert not wt.exists() and not world.admin("job").exists()


@pytest.mark.parametrize("where", ["tree", "nested"])
@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_turn_between_selection_and_transaction_stops_retirement(world, monkeypatch, begun, writable, where):
    """`nested`: the selecting transaction also reads rows on a folder inside
    the tree (review of 599af189, P3-1); the census is blinded below. The
    retirement never begins, so the tree is never moved from under the turn."""
    wt = world.job("job")
    folder = turn_folder(wt, where)
    before = snapshot(wt)
    start = retention._Pass._start

    def race(driver, job, protected):
        assert not driver.store._holds_writer()
        assert world.store.acquire_lease(folders.turn_key(folder, "late", writable=writable), "late")
        return start(driver, job, protected)

    monkeypatch.setattr(retention._Pass, "_start", race)
    # Isolate the atomic turn_holds guard from the pin census.
    monkeypatch.setattr(folders, "turn_folders", lambda read: set())
    result = run(world)
    assert result["pruned"] == [] and "job" in result["protected"], result
    assert snapshot(wt) == before and world.admin("job").is_dir()
    assert world.store.get_job("job")
    assert {r["holder"] for r in world.store.list_leases()} == {"late"}
    assert begun == [], "the selecting transaction let the retirement begin under a live turn"


@pytest.mark.parametrize("where", ["tree", "nested"])
@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_archive_commit_rechecks_turn_rows_and_rolls_back(world, monkeypatch, writable, where):
    """An out-of-band row after archive verification must still stop row
    deletion, also on a folder inside the tree (the pins, P3-1)."""
    wt = world.job("job")
    folder = str(wt) if where == "tree" else nested_repository(wt)
    before = snapshot(wt)
    check = rarch.Retirement.final_check

    def late(retirement):
        check(retirement)
        world.store.acquire_lease(folders.turn_key(folder, "late", writable=writable), "late")

    monkeypatch.setattr(rarch.Retirement, "final_check", late)
    result = run(world)
    assert result["pruned"] == [] and "job" in result["protected"], result
    assert world.store.get_job("job") and snapshot(wt) == before
    assert world.admin("job").is_dir()


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_retirement_fence_blocks_actual_turn_reservation(tmp_path, writable):
    """Admission and retirement agree on APFS case aliases of a recorded folder.

    Start admission after retention has acquired its fence but before it moves
    the worktree. No live turn row exists at selection, so only the canonical
    fence, not a turn pin, can prevent this reservation.
    """
    from tests.fake.test_admission_latency import fleet_daemon, measure
    from tests.fake.test_admission_liveness import CODEX, _checkout, _live
    from tests.fake.test_turn_wait_reasons import message_in, SETTINGS

    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        wt = daemon.root / "worktrees" / "Retired"
        git(harness.workdir, "worktree", "add", "--quiet", "--detach", str(wt), "HEAD")
        alias = wt.with_name("rETIRED")
        if not alias.exists() or not alias.samefile(wt):
            pytest.skip("case-insensitive filesystem required")
        daemon.store.add_job(job_id="retired", request_id="retired", payload_digest="d", kind="dispatch",
                             workdir=str(harness.workdir), worktree=str(alias), prompt_path="/prompt",
                             sandbox="workspace-write", state="succeeded", workdir_head=git(wt, "rev-parse", "HEAD"))
        directory = daemon.root / "jobs" / "retired"
        directory.mkdir(parents=True)
        (directory / "stdout").write_text("retired output")
        options = {**SETTINGS, "permission": "accept-edits" if writable else "read-only"}
        _, _, turn = message_in(daemon, harness, "During retirement", workspace=wt, settings=options)
        patch.setattr(daemon, "_workspace", lambda job: (job["workdir"], None, None, []))
        observed = []

        def begin(retirement, job, pool):
            daemon._admit_turns()
            observed.append(turn)
            assert not _live(daemon, turn), "turn reserved while retirement held its folder"
            assert daemon.store.one("SELECT holder FROM leases WHERE lease_key=?",
                                    (folders.exclusive_key(folders.canonical(wt)),)), "canonical retirement fence missing"
            assert folders.turn_holds(daemon.store.query, folders.canonical(wt)) == []
            assert daemon._holds[turn]["reason"] == "lease-held"
            raise rarch.Defer("test finished at fence", 1)

        patch.setattr(rarch.Retirement, "begin", begin)
        result = retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                       holders=lambda watches, **_: {})
        assert observed == [turn], result
        daemon.store.update_job(turn, next_check_at=None)
        daemon._admit_turns()
        assert _live(daemon, turn), daemon._holds


phase = st.sampled_from(["select", "begin", "lock", "archive", "verify", "delete"])
operation = st.tuples(phase, st.sampled_from(["start", "end"]), st.booleans())


@settings(max_examples=40, deadline=None, derandomize=True)
@example([("select", "start", True)])
@example([("select", "start", False)])
@example([("begin", "start", True), ("archive", "end", True), ("delete", "start", False)])
@example([("begin", "start", False)])
@example([("lock", "start", True), ("lock", "start", False)])
@given(st.lists(operation, min_size=0, max_size=25))
def test_interleavings_never_delete_a_folder_named_by_a_turn(schedule):
    """Generate starts/ends at each real archive-driver boundary.

    The independent turn actor reserves its SQL row only while the folder
    exists and the exclusive fence is absent, atomically. The actual daemon's
    reservation is tested above. Before both quarantine and verified reclaim,
    the oracle reads the real lease table and forbids a move/delete with any
    live writer or reader naming the original folder.
    """
    with tempfile.TemporaryDirectory(prefix="retention-i5-") as temporary, pytest.MonkeyPatch.context() as patch:
        w = World(Path(temporary))
        try:
            wt = w.job("job")
            folder = folders.canonical(wt)
            executed = set()

            def step(at):
                if at in executed:
                    return
                executed.add(at)
                for when, action, writable in schedule:
                    if when != at:
                        continue
                    holder = "writer" if writable else "reader"
                    if action == "end":
                        w.store.release_leases(holder)
                    elif wt.is_dir():
                        with w.store.transaction() as conn:
                            fence = conn.execute("SELECT holder FROM leases WHERE lease_key=?",
                                                 (folders.exclusive_key(folder),)).fetchone()
                            if not fence:
                                conn.execute("INSERT OR IGNORE INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                                             (folders.turn_key(folder, holder, writable=writable), holder, "now"))

            def oracle():
                assert folders.turn_holds(w.store.query, folder) == [], schedule

            def wrap(cls, method, at, check=False):
                real = getattr(cls, method)
                def wrapped(self, *args, **kwargs):
                    step(at)
                    if check:
                        oracle()
                    return real(self, *args, **kwargs)
                patch.setattr(cls, method, wrapped)

            wrap(retention._Pass, "_start", "begin")
            wrap(rarch.Retirement, "lock", "lock")
            wrap(rarch.Retirement, "archive", "archive")
            wrap(rarch.Retirement, "final_check", "verify")
            wrap(rarch.Retirement, "quarantine", "quarantine", check=True)
            wrap(rarch.Retirement, "reclaim", "delete", check=True)
            step("select")
            result = run(w)
            live = folders.turn_holds(w.store.query, folder)
            if live:
                assert wt.is_dir() and w.store.get_job("job"), (schedule, result)
                assert "job" in result["protected"], (schedule, result)
            elif "job" in result["pruned"]:
                assert not wt.exists() and not w.admin("job").exists()
        finally:
            w.close()


def test_archive_journal_and_fence_use_the_same_folder_spelling(world):
    wt = world.job("MixedCase")
    alias = wt.with_name("mIXEDcASE")
    if not alias.exists() or not alias.samefile(wt):
        pytest.skip("case-insensitive filesystem required")
    world.store.update_job("MixedCase", worktree=str(alias))
    result = run(world)
    assert result["pruned"] == ["MixedCase"], result
    assert not wt.exists() and not world.admin("MixedCase").exists()


@pytest.mark.parametrize("recorded", ["alias", "unrecorded"])
@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_the_commit_recheck_reads_rows_on_the_journals_spelling(world, monkeypatch, writable, recorded):
    """A row inside the tree after verification (admission fences only a turn's own
    folder) stops the commit also when `jobs.worktree` is spelled in another case or
    was never recorded. The pins compare the recorded spelling, so the commit also
    reads the rows on the journal's canonical one (review of 31048e67, F1: both
    were pruned, the tree deleted under the live row)."""
    wt = world.job("Job")
    folder = nested_repository(wt)
    if recorded == "alias":
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
        world.store.acquire_lease(folders.turn_key(folder, "late", writable=writable), "late")

    monkeypatch.setattr(rarch.Retirement, "final_check", late)
    result = run(world)
    assert result["pruned"] == [] and "Job" in result["protected"], result
    assert world.store.get_job("Job") and snapshot(wt) == before and world.admin("Job").is_dir()
    world.store.release_leases("late")
    monkeypatch.setattr(rarch.Retirement, "final_check", check)
    assert "Job" in run(world)["pruned"] and not wt.exists()


@pytest.mark.parametrize("where", ["tree", "nested"])
def test_turn_pin_recheck_does_no_filesystem_work_in_transaction(world, monkeypatch, where):
    wt = world.job("job")
    folder = str(wt) if where == "tree" else nested_repository(wt)
    world.store.acquire_lease(folders.turn_key(folder, "live", writable=False), "live")

    def unexpected(*args, **kwargs):
        pytest.fail("folder spelling must be established outside retention transactions")

    monkeypatch.setattr(folders, "canonical", unexpected)
    monkeypatch.setattr(folders, "_kernel_path", unexpected)
    with world.store.transaction():
        assert retention._pin_reasons(world.store, set(), None, only="job")["job"] == "turn-folder"


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_a_differently_spelt_recording_is_kept_by_the_selection_check(world, begun, writable):
    """The census compares recorded spellings, so only the selecting
    transaction's canonical turn_holds keeps a job whose jobs.worktree is spelt
    in another case while a turn row (canonical, as admission keys it) is live.
    It keeps it before the retirement begins (the commit's check would keep it
    too, but only after quarantining the tree)."""
    wt = world.job("Job")
    alias = wt.with_name(wt.name.swapcase())
    if not alias.exists() or not alias.samefile(wt):
        pytest.skip("case-insensitive filesystem required")
    world.store.update_job("Job", worktree=str(alias))
    before = snapshot(wt)
    world.store.acquire_lease(folders.turn_key(folders.canonical(alias), "live", writable=writable), "live")
    result = run(world)
    assert result["pruned"] == [] and "Job" in result["protected"], result
    assert snapshot(wt) == before and world.store.get_job("Job") and world.admin("Job").is_dir()
    assert begun == [], "the selecting transaction let the retirement begin under a live turn"


def test_a_restart_mid_retirement_keeps_the_canonical_fence(world):
    """A pass stopped while archiving leaves its fence: admission keeps turns
    and readers out of the quarantined folder until a later pass finishes."""
    import threading
    wt = world.job("job")
    folder = folders.canonical(wt)
    cancel = threading.Event()
    archive = rarch.Retirement.archive

    def stop(self, *args, **kwargs):
        cancel.set()
        return archive(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(rarch.Retirement, "archive", stop)
        try:
            retention.maintenance(world.store, world.root, max_jobs=0, max_bytes=0,
                                  holders=lambda watches, **_: {}, clock=Clock(), cancel=cancel)
        except rarch.Interrupted:
            pass
    fence = world.store.one("SELECT holder FROM leases WHERE lease_key=?", (folders.exclusive_key(folder),))
    assert fence is not None and fence["holder"] == "retention:job"
    assert not wt.exists() and rarch.load_journal(world.root, "job") is not None
    result = run(world)
    assert result["pruned"] == ["job"] or "job" in result.get("reclaimed", []), result
    assert world.store.one("SELECT 1 FROM leases WHERE holder='retention:job'") is None


@pytest.mark.parametrize("turn_state", ["running", "lost"])
def test_a_turn_in_a_repository_nested_in_the_job_worktree_keeps_it(world, turn_state):
    """I5 for a folder inside the job's worktree (review of 599af189, P3-1): a
    conversation opened in a repository nested there keys its row on that
    repository's top level. While its job runs, #76's worktree-in-use pin keeps
    the host; a quarantined turn's job is `lost` and keeps its TURN row (daemon
    `_quarantine`), and then only the row says the folder is in use. Failed on
    ceb1f15f for `lost`: the job was pruned and its tree deleted."""
    wt = world.job("job")
    folder = nested_repository(wt)
    world.store.add_job(job_id="turnjob", request_id="req-turnjob", payload_digest="d", kind="turn",
                        workdir=folder, worktree=folder, in_place=1, prompt_path="/prompt",
                        sandbox="workspace-write", state=turn_state)
    world.attempt("turnjob", state="running" if turn_state == "running" else "quarantined")
    world.store.acquire_lease(folders.turn_key(folder, "turnjob", writable=True), "turnjob")
    before = snapshot(wt)
    assert retention._pin_reasons(world.store, set(), None)["job"] == (
        "worktree-in-use" if turn_state == "running" else "turn-folder")
    result = run(world)
    assert "job" not in result["pruned"] and "job" in result["protected"], result
    assert snapshot(wt) == before and world.store.get_job("job") and world.admin("job").is_dir()
    if turn_state == "lost":
        world.store.release_leases("turnjob")         # the quarantine is resolved: the row goes
        result = run(world)
        assert "job" in result["pruned"], result
        assert not wt.exists() and not world.admin("job").exists()


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_the_census_sees_a_turn_in_a_folder_inside_the_worktree(world, monkeypatch, writable):
    """P3-1, the pins alone: the selecting transaction's check is blinded, so
    only `_pin_reasons` can keep the job (each layer is tested alone, as in
    test_retention_worktrees.py)."""
    wt = world.job("job")
    folder = nested_repository(wt)
    world.store.acquire_lease(folders.turn_key(folder, "live", writable=writable), "live")
    before = snapshot(wt)
    real = folders.turn_holds
    monkeypatch.setattr(folders, "turn_holds", lambda read, folder, kinds=folders.SHARED, **_: [])
    assert retention._pin_reasons(world.store, set(), None)["job"] == "turn-folder"
    result = run(world)
    assert result["pruned"] == [] and "job" in result["protected"], result
    assert snapshot(wt) == before and world.store.get_job("job")
    monkeypatch.setattr(folders, "turn_holds", real)
    world.store.release_leases("live")
    assert run(world)["pruned"] == ["job"]


@pytest.mark.parametrize("beside", ["2", "-copy", ":x", "parent"])
def test_a_turn_beside_the_worktree_keeps_nothing(world, beside):
    """`inside` is a folder below the tree, not one whose name extends it
    (`<wt>2`, `<wt>-copy`, `<wt>:x`) and not one above it (the folder holding
    every tree): such a row keeps nothing, in the census or at selection."""
    wt = world.job("job")
    folder = folders.canonical(wt)
    other = str(Path(folder).parent) if beside == "parent" else folder + beside
    world.store.acquire_lease(folders.turn_key(other, "live", writable=True), "live")
    assert "job" not in retention._pin_reasons(world.store, set(), None)
    assert folders.turn_holds(world.store.query, folder, inside=True) == []
    assert run(world)["pruned"] == ["job"]
    assert not wt.exists()


def in_place(world, job_id: str, folder: str, *, kind: str = "turn", state: str = "succeeded") -> None:
    """A job recorded in place in `folder`, as admission records a writable turn
    (`jobs.worktree` is its write target, daemon.py `_admit`)."""
    world.store.add_job(job_id=job_id, request_id="req-" + job_id, payload_digest="d", kind=kind,
                        workdir=folder, worktree=folder, in_place=1, prompt_path="/prompt",
                        sandbox="workspace-write", state=state, created_at="2026-01-01T00:00:00.000+00:00",
                        finished_at=None if state == "running" else "2026-01-01T00:01:00.000+00:00")
    directory = world.root / "jobs" / job_id
    directory.mkdir()
    (directory / "stdout").write_text("output of " + job_id)


@pytest.mark.parametrize("where", ["folder", "inside"])
@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_a_live_turn_keeps_no_in_place_job_in_its_folder(world, writable, where):
    """P3-2 (review of 599af189): retention never removes an in-place job's
    folder, so a live turn in a shared folder keeps none of the older jobs
    recorded there, turns or detached writers: they retire with their rows and
    job directories while the folder stays as it is. Failed on ceb1f15f: all
    four were kept as `turn-folder` until the conversation ended."""
    scratch = world.base / "home" / "scratch"
    scratch.mkdir()
    (scratch / "notes.txt").write_text("a person's notes\n")
    folder = folders.canonical(scratch)
    live = folder
    if where == "inside":
        (scratch / "sub").mkdir()
        live = folders.canonical(scratch / "sub")
    for n in range(3):
        in_place(world, f"turn-{n}", folder)
    in_place(world, "detached", folder, kind="dispatch")
    in_place(world, "live", live, state="running")
    world.attempt("live", state="running")
    world.store.acquire_lease(folders.turn_key(live, "live", writable=writable), "live")
    before = snapshot(scratch)
    reasons = retention._pin_reasons(world.store, set(), None)
    assert not {job: why for job, why in reasons.items() if why == "turn-folder"}, reasons
    result = retention.maintenance(world.store, world.root, max_jobs=0, max_bytes=0, turn_max_jobs=0,
                                   turn_max_bytes=0, turn_keep_s=0, holders=lambda watches, **_: {}, clock=Clock())
    assert sorted(result["pruned"]) == ["detached", "turn-0", "turn-1", "turn-2"], result
    assert "live" in result["protected"] and world.store.get_job("live")
    assert snapshot(scratch) == before


@pytest.mark.parametrize("sandbox", ["read-only", "workspace-write"])
def test_a_turn_row_keeps_no_job_without_its_own_tree(world, sandbox):
    """P3-2's rule is `owned` (_pin_reasons): a writable tree retention
    allocated. A read-only job recorded with a worktree retires as well."""
    wt = world.job("job")
    world.store.update_job("job", sandbox=sandbox, in_place=0 if sandbox == "read-only" else 1)
    world.store.acquire_lease(folders.turn_key(folders.canonical(wt), "live", writable=True), "live")
    before = snapshot(wt)
    assert "job" not in retention._pin_reasons(world.store, set(), None)
    assert run(world)["pruned"] == ["job"]
    assert snapshot(wt) == before                    # not its tree: retention leaves it


# --- the pin as a property --------------------------------------------------------

NAME = st.text(alphabet="ab:;-0./", min_size=1, max_size=4).filter(lambda name: name not in (".", ".."))
TREE = st.lists(NAME.filter(lambda name: "/" not in name), min_size=1, max_size=2).map(
    lambda parts: "/t/" + "/".join(parts))


@st.composite
def census(draw):
    """Jobs with and without their own tree, and turn rows on their trees, on
    folders inside, beside, above and elsewhere, every spelling the key's
    separators (':', '/') and their neighbours (';', '0', '-') can confuse."""
    jobs = []
    for n in range(draw(st.integers(1, 5))):
        tree = draw(st.one_of(TREE, st.none()))
        jobs.append(dict(job_id=f"job-{n}", worktree=tree, in_place=draw(st.booleans()),
                         sandbox=draw(st.sampled_from(["workspace-write", "read-only"]))))
    trees = [job["worktree"] for job in jobs if job["worktree"]] or ["/t/a"]
    near = st.sampled_from(trees).flatmap(lambda tree: st.one_of(
        st.just(tree), NAME.map(lambda name: f"{tree}/{name}"), NAME.map(lambda name: tree + name),
        st.just(tree.rsplit("/", 1)[0] or "/"), TREE))
    rows = draw(st.lists(st.tuples(near, st.booleans()), max_size=6))
    return jobs, rows


@settings(max_examples=150, deadline=None, derandomize=True)
@given(census())
def test_the_turn_folder_pin_is_exactly_an_owned_tree_a_turn_is_in(case):
    """For every job and turn row: `turn-folder` keeps a job iff it has its own
    allocated tree (writable, not in place: P3-2) and some row's folder is that
    tree or inside it (P3-1), checked against `folder == wt or
    folder.startswith(wt + "/")`, and the per-job reading (the selecting and
    committing transactions' `only=`) agrees with the census."""
    jobs, rows = case
    with tempfile.TemporaryDirectory(prefix="retention-pin-") as temporary:
        store = Store(Path(temporary) / "state.sqlite3")
        try:
            for job in jobs:
                store.add_job(request_id="req-" + job["job_id"], payload_digest="d", kind="dispatch",
                              workdir="/t", prompt_path="/prompt", state="succeeded", **job)
            for n, (folder, writable) in enumerate(rows):
                store.acquire_lease(folders.turn_key(folder, f"turn-{n}", writable=writable), f"turn-{n}")
            census_reasons = retention._pin_reasons(store, set(), None)
            for job in jobs:
                tree = job["worktree"]
                want = bool(tree and job["sandbox"] == "workspace-write" and not job["in_place"]
                            and any(folder == tree or folder.startswith(tree + "/") for folder, _ in rows))
                assert (census_reasons.get(job["job_id"]) == "turn-folder") == want, (job, rows, census_reasons)
                alone = retention._pin_reasons(store, set(), None, only=job["job_id"])
                assert alone.get(job["job_id"]) == census_reasons.get(job["job_id"]), (job, rows)
        finally:
            store.close()
