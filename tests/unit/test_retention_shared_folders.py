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
from tests.unit.retention_world import Clock, World, git, snapshot


def run(w):
    return retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0,
                                 holders=lambda watches, **_: {}, clock=Clock())


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


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


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_turn_between_selection_and_transaction_stops_retirement(world, monkeypatch, writable):
    wt = world.job("job")
    before = snapshot(wt)
    start = retention._Pass._start

    def race(driver, job, protected):
        assert not driver.store._holds_writer()
        assert world.store.acquire_lease(folders.turn_key(folders.canonical(wt), "late", writable=writable), "late")
        return start(driver, job, protected)

    monkeypatch.setattr(retention._Pass, "_start", race)
    # Isolate the atomic turn_holds guard from the pin census.
    monkeypatch.setattr(folders, "turn_folders", lambda read: set())
    result = run(world)
    assert result["pruned"] == [] and "job" in result["protected"], result
    assert snapshot(wt) == before and world.admin("job").is_dir()
    assert world.store.get_job("job")
    assert {r["holder"] for r in world.store.list_leases()} == {"late"}


@pytest.mark.parametrize("writable", [True, False], ids=["TURN", "READER"])
def test_archive_commit_rechecks_turn_rows_and_rolls_back(world, monkeypatch, writable):
    """An out-of-band row after archive verification must still stop row deletion."""
    wt = world.job("job")
    before = snapshot(wt)
    check = rarch.Retirement.final_check

    def late(retirement):
        check(retirement)
        world.store.acquire_lease(folders.turn_key(str(wt), "late", writable=writable), "late")

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
            assert daemon.store.one("SELECT holder FROM leases WHERE lease_key=?",
                                    (folders.exclusive_key(folders.canonical(wt)),)), "canonical retirement fence missing"
            daemon._admit_turns()
            observed.append(turn)
            assert not _live(daemon, turn), daemon._holds
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


phase = st.sampled_from(["select", "begin", "archive", "verify", "delete"])
operation = st.tuples(phase, st.sampled_from(["start", "end"]), st.booleans())


@settings(max_examples=40, deadline=None, derandomize=True)
@example([("select", "start", True)])
@example([("select", "start", False)])
@example([("begin", "start", True), ("archive", "end", True), ("delete", "start", False)])
@example([("begin", "start", False)])
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
