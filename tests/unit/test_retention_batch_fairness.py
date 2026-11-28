"""Retention's batch is fair (C-8.4; review r1 of #286, P3-4).

A pass takes at most `batch` jobs into flight. Before this, a job that parked
without progress kept its slot for ever, and once parked jobs filled every slot
no eligible job started again. These tests drive the real pass, and where they
can the real archive builder, on real repositories.

The trigger: a real archive parks every slice and gets no further when a file
in its quarantined tree changes between slices and reading that file outlasts
the slice. The builder parks only after new work (review of a9a6cbf4, N3), and
re-reading the changed file is new work, so `last_work` never reads 0; what
stops moving is how far the walk gets (`Retirement.last_reached`).
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_fs as rfs
from tests.unit.retention_world import Clock, World, git, snapshot, trust_temporary_directories
from tests.unit.test_retention_regenerable import ignore, install, write


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    monkeypatch.setattr(retention.procs, "worktree_cwd_pids", lambda folder: set())
    w = World(tmp_path)
    yield w
    w.close()


def run(w: World, **kwargs):
    kwargs.setdefault("max_jobs", 0)
    kwargs.setdefault("max_bytes", 0)
    kwargs.setdefault("holders", lambda watches, **_: {})
    return retention.maintenance(w.store, w.root, **kwargs)


HOT = 64 * 1024


def hot_job(w: World, job_id: str, created: str):
    """A job with a `HOT`-byte file early in walk order, read slowly."""
    wt = w.job(job_id, created=created)
    (wt / "0-hot.bin").write_bytes(os.urandom(HOT))
    for n in range(3):
        (wt / f"z{n}.txt").write_text(f"cold {n}\n")
    return wt


def slow_hot_reads(monkeypatch, clock: Clock, seconds: float) -> None:
    original = rfs.read_hashes

    def slow(fd, size, fmt, check=None):
        if size == HOT:
            clock.advance(seconds)
        return original(fd, size, fmt, check)

    monkeypatch.setattr(rfs, "read_hashes", slow)


def rewrite_hot(w: World, wt, job_id: str) -> None:
    """The writer: the same size, new bytes, wherever the tree is now."""
    for path in (w.root / "retention" / job_id / "worktree" / "0-hot.bin", wt / "0-hot.bin"):
        if path.exists():
            path.write_bytes(os.urandom(HOT))
            return


# --- the trigger, with the real builder ----------------------------------------------------

def test_a_real_archive_parks_without_progress_while_a_file_changes_between_slices(world, monkeypatch):
    """Review r1 of #286, P3-4, with no stub: the hot file's read outlasts the
    slice and the file changes between slices. Each slice reads it again, writes
    a progress record for its new version, and parks at the next entry. With
    `batch=1` the job behind it must still start, and the pass puts the hot
    job back and defers it, keeping its archive progress."""
    w = world
    wt = hot_job(w, "job-hot", "2026-09-01T00:00:00Z")
    w.job("job-new", created="2026-09-01T00:01:00Z")
    clock = Clock()
    slow_hot_reads(monkeypatch, clock, 100)                 # longer than the 60 s slice
    reached = []
    original_archive = rarch.Retirement.archive

    def archive(self, slice_end):
        try:
            return original_archive(self, slice_end)
        finally:
            if self.job_id == "job-hot":
                reached.append((self.last_work, getattr(self, "last_reached", None)))

    monkeypatch.setattr(rarch.Retirement, "archive", archive)
    state = retention.RetentionState()
    results = []
    for _ in range(8):
        results.append(run(w, state=state, clock=clock, slice_s=60, batch=1))
        rewrite_hot(w, wt, "job-hot")
    started_new = next((n for n, r in enumerate(results) if "job-new" in r["awaiting_check2"]), None)
    assert started_new is not None and started_new <= 1, [
        (r["awaiting_check2"], r["in_flight"], r["deferred"]) for r in results
    ]
    assert results[started_new + 1]["pruned"] == ["job-new"]
    # Every hot slice did new work: the old `last_work` test never saw a stall.
    assert reached and all(work > 0 for work, _ in reached), reached
    stalled = [r["deferred"].get("job-hot", "") for r in results]
    assert any(reason.startswith("stalled") for reason in stalled), stalled
    # Put back where it was, its archive kept as an idle cache for the next try.
    assert (wt / "0-hot.bin").exists() and w.store.get_job("job-hot") is not None
    journal = rarch.load_journal(w.root, "job-hot")
    assert journal["state"] == "idle" and (w.root / "retention" / "job-hot" / "archive").is_dir()
    # Once the writer stops and the backoff passes, it retires.
    clock.advance(rarch.DEFER_ERROR_MAX_S + 1)
    for _ in range(6):
        last = run(w, state=state, clock=clock, slice_s=60, batch=1)
        if last["pruned"]:
            break
    assert last["pruned"] == ["job-hot"], last


def test_a_stalled_job_backs_off_as_for_an_error(world, monkeypatch):
    """The stall rule alone, real builder, no job waiting: the hot job parks
    once to set how far it got, then gets no further in `STALL_SLICES` slices
    in a row, and the pass puts it back with its archive kept. The deferral is the error
    backoff's: 6 h, then twice that the next time it stalls."""
    w = world
    wt = hot_job(w, "job-hot", "2026-09-01T00:00:00Z")
    clock = Clock()
    slow_hot_reads(monkeypatch, clock, 100)
    state = retention.RetentionState()
    deferrals = []
    for _ in range(2):
        for n in range(1 + retention.STALL_SLICES):
            result = run(w, state=state, clock=clock, slice_s=60)
            rewrite_hot(w, wt, "job-hot")
            if n < retention.STALL_SLICES:
                assert result["in_flight"] == ["job-hot"], result
        assert result["deferred"]["job-hot"].startswith("stalled: 3 slices in a row"), result
        assert result["in_flight"] == [] and result["more"] is False, result
        until, reason = state.deferred["job-hot"]
        deferrals.append(until - clock())
        journal = rarch.load_journal(w.root, "job-hot")
        assert journal["state"] == "idle" and (w.root / "retention" / "job-hot" / "archive").is_dir()
        assert (wt / "0-hot.bin").exists()
        clock.advance(until - clock() + 1)
    assert deferrals == [rarch.DEFER_ERROR_S, 2 * rarch.DEFER_ERROR_S], deferrals


# --- scripted slices: the review's stub, and jobs that never finish ---------------------------

class Slices:
    """Scripted archive slices. `plans[job](n)` names what the job's n-th slice
    does: "progress" parks one step further than its last, "stall" parks with
    new work but no further (the real trigger above), "idle" parks with no work
    (review r1's stub), and "done" (and any job without a plan) runs the real
    builder. A parked slice costs `cost` seconds of the clock. Starts are
    recorded in `starts`, slices in `log`."""

    def __init__(self, monkeypatch, clock: Clock, plans: dict, cost: float = 60.0):
        self.plans, self.clock, self.cost = plans, clock, cost
        self.log: list[str] = []
        self.starts: list[str] = []
        self.count: dict[str, int] = {}
        self.step: dict[str, int] = {}
        archive, start = rarch.Retirement.archive, retention._Pass._start
        slices = self

        def scripted(retirement, slice_end):
            job_id = retirement.job_id
            n = slices.count[job_id] = slices.count.get(job_id, -1) + 1
            slices.log.append(job_id)
            plan = slices.plans.get(job_id)
            kind = plan(n) if plan else "done"
            if kind == "done":
                return archive(retirement, slice_end)
            slices.clock.advance(slices.cost)
            retirement.last_found = 0
            if kind == "progress":
                slices.step[job_id] = slices.step.get(job_id, 0) + 1
                retirement.last_work, retirement.last_reached = 1, slices.step[job_id]
            elif kind == "stall":
                retirement.last_work, retirement.last_reached = 1, 5
            else:
                retirement.last_work, retirement.last_reached = 0, 0
            return "parked"

        def recorded(self_, job, protected):
            slices.starts.append(job["job_id"])
            return start(self_, job, protected)

        monkeypatch.setattr(rarch.Retirement, "archive", scripted)
        monkeypatch.setattr(retention._Pass, "_start", recorded)


def always(kind):
    return lambda n: kind


def test_review_r1_stub_a_job_parking_without_work_no_longer_holds_the_only_slot(world, monkeypatch):
    """Review r1 of #286, P3-4, as the review ran it: `Retirement.archive`
    parks `job-a` without doing work every slice, `batch=1`. Before, over 4
    passes `job-b-new` never started. Now `job-a` yields at the second pass,
    `job-b-new` archives there, and a later pass puts `job-a`, restarted, back as
    stalled."""
    w = world
    w.job("job-a", worktree=False, created="2026-09-01T00:00:00Z")
    w.job("job-b-new", worktree=False, created="2026-09-01T00:01:00Z")
    clock = Clock()
    slices = Slices(monkeypatch, clock, {"job-a": always("idle")})
    state = retention.RetentionState()
    results = [run(w, state=state, clock=clock, slice_s=60, batch=1) for _ in range(6)]
    assert results[0]["in_flight"] == ["job-a"] and results[0]["more"]
    assert results[1]["awaiting_check2"] == ["job-b-new"], results[1]
    assert results[1]["deferred"]["job-a"] == "yielded: 1 slice since it started, for job-b-new"
    assert results[1]["more"], results[1]           # the yielded job is work waiting, not an hour away
    assert slices.starts == ["job-a", "job-b-new", "job-a"], slices.starts
    assert results[2]["pruned"] == ["job-b-new"], results[2]
    # Restarted at pass 3, it parks without work at passes 3, 4 and 5.
    assert results[4]["deferred"]["job-a"].startswith("stalled: 3 slices in a row"), results[4]
    assert not results[4]["more"] and not results[5]["more"] and not results[5]["progressed"]
    assert w.store.get_job("job-a") is not None and not (w.root / "retention" / "job-a" / "job").exists()


def test_jobs_that_never_finish_take_turns_in_the_order_they_began_waiting(world, monkeypatch):
    """`job-a` parks one step further every slice and never finishes, so the
    stall rule never puts it back; only the yield frees its slot. Waiting jobs
    start in the order they began waiting: `job-a`, having yielded, queues
    behind `job-c` and `job-d`, which were waiting then, and ahead of
    `job-n`, which began waiting after."""
    w = world
    for n, job_id in enumerate(["job-a", "job-c", "job-d"]):
        w.job(job_id, worktree=False, created=f"2026-09-01T00:0{n}:00Z")
    clock = Clock()
    slices = Slices(monkeypatch, clock, {"job-a": always("progress")})
    state = retention.RetentionState()
    results = [run(w, state=state, clock=clock, slice_s=60, batch=1) for _ in range(5)]
    assert slices.starts == ["job-a", "job-c", "job-d", "job-a"], slices.starts
    assert [r["pruned"] for r in results] == [[], [], ["job-c"], ["job-d"], []]
    # Alone, it keeps its slot: nothing waits, so nothing asks it to yield.
    assert results[3]["in_flight"] == results[4]["in_flight"] == ["job-a"]
    assert all(not r["deferred"] for r in results[2:]), results
    w.job("job-n", worktree=False, created="2026-09-02T00:00:00Z")
    results = [run(w, state=state, clock=clock, slice_s=60, batch=1) for _ in range(2)]
    assert slices.starts[4:] == ["job-n", "job-a"], slices.starts
    assert results[0]["awaiting_check2"] == results[1]["pruned"] == ["job-n"]
    assert results[1]["in_flight"] == ["job-a"]
    assert state.queue == {}


def test_parked_jobs_hold_at_most_batch_minus_one_slots_while_a_job_waits(world, monkeypatch):
    """Four jobs that never finish and `batch=2`: never more than two in flight
    at the end of a pass, every pass with a job waiting starts one, and each
    job keeps getting turns."""
    w = world
    jobs = [f"job-{n}" for n in range(4)]
    for n, job_id in enumerate(jobs):
        w.job(job_id, worktree=False, created=f"2026-09-01T00:0{n}:00Z")
    clock = Clock()
    slices = Slices(monkeypatch, clock, {job_id: always("progress") for job_id in jobs})
    state = retention.RetentionState()
    for n in range(16):
        before = len(slices.starts)
        result = run(w, state=state, clock=clock, slice_s=60, batch=2)
        holders = slot_holders(w)
        assert len(holders) <= 2 and holders == sorted(result["in_flight"]), (n, holders, result)
        if n:
            assert len(slices.starts) > before, (n, slices.starts)
    assert all(slices.starts.count(job_id) >= 3 for job_id in jobs), slices.starts


@pytest.mark.parametrize("needs_discovery", [False, True], ids=["starts", "discovery-defers"])
def test_a_pass_past_its_deadline_still_starts_one_job_beside_parked_ones(world, monkeypatch,
                                                                      needs_discovery):
    """Before, a pass whose recovery, pin reads or size measuring used up the
    deadline started nothing while any job was in flight, so a slow
    pre-selection on every pass kept a waiting job out for ever. One start a
    pass is always attempted; the deadline holds back the rest."""
    w = world
    for n, job_id in enumerate(["job-a", "job-b", "job-c"]):
        w.job(job_id, worktree=False, created=f"2026-09-01T00:0{n}:00Z")
    clock = Clock(start=time.monotonic() + 1e6)
    slices = Slices(monkeypatch, clock, {"job-a": always("progress")})
    state = retention.RetentionState()
    run(w, state=state, clock=clock, slice_s=60, batch=3, deadline=clock() + 1, measure_s=0)
    assert slices.starts == ["job-a", "job-b", "job-c"]
    tree = w.job("job-d", worktree=needs_discovery, created="2026-09-02T00:00:00Z")
    if needs_discovery:
        tree.rename(w.base / "gone-worktree")
        w.store.connection.execute("UPDATE jobs SET workdir=? WHERE job_id=?", (str(w.base / "gone-lane"), "job-d"))
    w.job("job-e", worktree=False, created="2026-09-02T00:01:00Z")
    measure = retention._Pass._measure

    def slow_measure(self, *args):
        measure(self, *args)
        clock.advance(3600)                 # pre-selection outlasts the deadline

    monkeypatch.setattr(retention._Pass, "_measure", slow_measure)
    result = run(w, state=state, clock=clock, slice_s=60, batch=3, deadline=clock() + 1, measure_s=0)
    assert slices.starts[3:] == ["job-d"], slices.starts
    assert result["pruned"] == ["job-b", "job-c"] and result["more"], result
    if needs_discovery:
        assert result["deferred"]["job-d"] == "repository discovery deadline", result
        assert result["in_flight"] == ["job-a"] and result["awaiting_check2"] == [], result
    else:
        assert result["awaiting_check2"] == ["job-d"], result
        assert sorted(result["in_flight"]) == ["job-a", "job-d"], result
    assert set(state.queue) == {"job-e"}


def test_a_start_refused_at_selection_queues_behind_the_others(world, monkeypatch):
    """A start refused at selection (a turn on the tree, a lease, a pin that
    holds only this pass) counts as the job's turn: it leaves the queue and,
    still eligible next pass, queues behind the jobs that were waiting, so a job
    refused every pass never keeps the head of the queue."""
    w = world
    for n, job_id in enumerate(["job-x", "job-b", "job-c"]):
        w.job(job_id, worktree=False, created=f"2026-09-01T00:0{n}:00Z")
    clock = Clock()
    attempts = []
    start = retention._Pass._start

    def refusing(self, job, protected):
        attempts.append(job["job_id"])
        if job["job_id"] == "job-x":
            protected.add("job-x")              # as `_start` does for "turn-folder"
            return None
        return start(self, job, protected)

    monkeypatch.setattr(retention._Pass, "_start", refusing)
    state = retention.RetentionState()
    results = [run(w, state=state, clock=clock, batch=1) for _ in range(4)]
    assert attempts == ["job-x", "job-b", "job-c", "job-x"], attempts
    assert [r["pruned"] for r in results] == [[], [], ["job-b"], ["job-c"]]


def test_a_job_that_yields_queues_ahead_of_one_that_begins_waiting_after(world, monkeypatch):
    """`job-a` yields for `job-b` and takes its ticket then. `job-x`, older but
    deferred until after the yield, begins waiting a pass later, so it queues
    behind `job-a`: age orders only the jobs that begin waiting in the same
    pass."""
    w = world
    for n, job_id in enumerate(["job-x", "job-a", "job-b"]):
        w.job(job_id, worktree=False, created=f"2026-09-01T00:0{n}:00Z")
    clock = Clock()
    slices = Slices(monkeypatch, clock, {"job-a": always("progress")})
    state = retention.RetentionState()
    state.defer("job-x", 10_000, "held by the test", clock())
    results = [run(w, state=state, clock=clock, slice_s=60, batch=1) for _ in range(2)]
    assert results[1]["deferred"]["job-a"] == "yielded: 1 slice since it started, for job-b", results[1]
    assert results[1]["awaiting_check2"] == ["job-b"], results[1]
    clock.advance(10_000)
    results = [run(w, state=state, clock=clock, slice_s=60, batch=1) for _ in range(2)]
    assert slices.starts == ["job-a", "job-b", "job-a", "job-x"], slices.starts
    assert results[0]["pruned"] == ["job-b"], results[0]
    assert results[1]["awaiting_check2"] == ["job-x"], results[1]
    assert results[1]["deferred"]["job-a"].startswith("yielded"), results[1]


def test_a_job_cancelled_before_its_first_slice_keeps_its_slot_for_one(world, monkeypatch):
    """A pass cancelled after it started `job-a` and before its slice leaves a
    full batch of jobs that have had no slice since they started (with
    `batch=1`, just `job-a`). The next pass gives `job-a` its slice rather than
    asking it to yield, so a start always buys a slice; the pass after that, it
    yields to `job-b`."""
    w = world
    w.job("job-a", worktree=False, created="2026-09-01T00:00:00Z")
    w.job("job-b", worktree=False, created="2026-09-01T00:01:00Z")
    clock = Clock()
    slices = Slices(monkeypatch, clock, {"job-a": always("progress")})
    cancel = threading.Event()
    armed = [True]
    holder_listing = retention._Pass._holder_listing

    def cancelled_after_check1(self, retirements):
        passed = holder_listing(self, retirements)
        job_a = retirements.get("job-a")
        if job_a is not None and job_a.journal.get("check1") and armed:
            armed.clear()
            cancel.set()
        return passed

    monkeypatch.setattr(retention._Pass, "_holder_listing", cancelled_after_check1)
    state = retention.RetentionState()
    first = run(w, state=state, clock=clock, slice_s=60, batch=1, cancel=cancel)
    assert first["interrupted"] == "cancelled" and slices.starts == ["job-a"] and slices.log == [], first
    assert rarch.load_journal(w.root, "job-a")["state"] == "quarantined"
    assert state.holds["job-a"].slices == 0
    cancel.clear()
    second = run(w, state=state, clock=clock, slice_s=60, batch=1, cancel=cancel)
    assert "job-a" not in second["deferred"] and second["in_flight"] == ["job-a"] and second["more"], second
    assert slices.log == ["job-a"] and slices.starts == ["job-a"], (slices.log, slices.starts)
    third = run(w, state=state, clock=clock, slice_s=60, batch=1, cancel=cancel)
    assert third["deferred"]["job-a"] == "yielded: 1 slice since it started, for job-b", third
    assert third["awaiting_check2"] == ["job-b"], third


def slot_holders(w: World) -> list[str]:
    """In-flight journals that are neither archived nor rollback pending."""
    return sorted(job_id for job_id, j in rarch.journals(w.root).items()
                  if isinstance(j, dict) and j["state"] in rarch.IN_FLIGHT
                  and j["state"] != "archived" and not j.get("rollback_pending"))


def test_a_yield_whose_rollback_fails_keeps_its_slot(world, monkeypatch):
    """A yield whose rollback raises before it records itself (a journal
    write that fails, say) leaves `job-a` in flight. The pass counts no yield,
    starts nothing in its place and gives `job-a` its slice, so the batch
    still holds one job; the next pass yields it."""
    w = world
    w.job("job-a", worktree=False, created="2026-09-01T00:00:00Z")
    w.job("job-b", worktree=False, created="2026-09-01T00:01:00Z")
    clock = Clock()
    slices = Slices(monkeypatch, clock, {"job-a": always("progress")})
    rollback = rarch.Retirement.rollback
    failing = [False]

    def flaky(self, reason, **kwargs):
        if failing[0] and reason.startswith("yielded"):
            raise OSError(28, "No space left on device")
        return rollback(self, reason, **kwargs)

    monkeypatch.setattr(rarch.Retirement, "rollback", flaky)
    state = retention.RetentionState()
    run(w, state=state, clock=clock, slice_s=60, batch=1)
    failing[0] = True
    second = run(w, state=state, clock=clock, slice_s=60, batch=1)
    assert any("rollback failed: OSError" in e["error"] for e in second["errors"]), second
    assert slices.starts == ["job-a"] and slices.log == ["job-a", "job-a"], (slices.starts, slices.log)
    assert second["in_flight"] == ["job-a"] and "job-a" not in second["deferred"], second
    assert slot_holders(w) == ["job-a"]
    failing[0] = False
    third = run(w, state=state, clock=clock, slice_s=60, batch=1)
    assert third["deferred"]["job-a"] == "yielded: 2 slices since it started, for job-b", third
    assert third["awaiting_check2"] == ["job-b"] and slot_holders(w) == [], third


def test_a_yield_a_live_turn_holds_up_frees_the_slot_as_recovery_does(world, monkeypatch):
    """A live turn in the quarantine folder holds the yield's rollback up
    (`kept`). Recovery has always left a job with a rollback pending out of
    the batch, so the yield frees the slot at once: `job-b` starts, and
    `job-a` waits in quarantine, never sliced, until the turn ends and
    recovery puts it back."""
    w = world
    w.job("job-a", worktree=False, created="2026-09-01T00:00:00Z")
    w.job("job-b", worktree=False, created="2026-09-01T00:01:00Z")
    clock = Clock()
    slices = Slices(monkeypatch, clock, {"job-a": always("progress")})
    fence = rarch.Retirement._fence_quarantine
    turn = [False]

    def turn_in_quarantine(self):
        if turn[0] and self.job_id == "job-a":
            return "turn-folder: a turn in job-a's quarantine"
        return fence(self)

    monkeypatch.setattr(rarch.Retirement, "_fence_quarantine", turn_in_quarantine)
    state = retention.RetentionState()
    run(w, state=state, clock=clock, slice_s=60, batch=1)
    turn[0] = True
    second = run(w, state=state, clock=clock, slice_s=60, batch=1)
    assert second["deferred"]["job-a"] == "yielded: 1 slice since it started, for job-b", second
    assert second["awaiting_check2"] == ["job-b"] and slices.starts == ["job-a", "job-b"], second
    journal = rarch.load_journal(w.root, "job-a")
    assert journal["state"] == "quarantined" and journal["rollback_pending"]["reason"].startswith("yielded")
    assert slot_holders(w) == []
    third = run(w, state=state, clock=clock, slice_s=60, batch=1)
    assert third["pruned"] == ["job-b"], third
    assert third["deferred"]["job-a"].startswith("turn-folder") and slices.log.count("job-a") == 1, third
    turn[0] = False
    fourth = run(w, state=state, clock=clock, slice_s=60, batch=1)
    # Recovery puts it back (no deferral left), and the same pass starts it again.
    assert slices.starts == ["job-a", "job-b", "job-a"] and slot_holders(w) == ["job-a"], fourth
    assert not fourth["errors"] and fourth["in_flight"] == ["job-a"], fourth


def test_repeated_failed_yields_preserve_slice_stamp_and_stall_count(world, monkeypatch):
    """A failed rollback journal write leaves the same hold active (review r1, P2-2)."""
    w = world
    w.job("job-a", worktree=False, created="2026-09-01T00:00:00Z")
    w.job("job-b", worktree=False, created="2026-09-01T00:01:00Z")
    clock, state = Clock(), retention.RetentionState()
    slices = Slices(monkeypatch, clock, {"job-a": lambda n: "progress" if n == 0 else "idle"})
    real_write = rfs.write_atomic
    attempted = []
    fail_stalls = True

    def write_with_failed_rollback(path, data, **kwargs):
        if path == rarch.journal_path(w.root, "job-a"):
            pending = json.loads(data).get("rollback_pending")
            if pending is not None:
                reason = pending["reason"]
                if reason.startswith("yielded") or (reason.startswith("stalled") and fail_stalls):
                    attempted.append((reason, state.sliced.get("job-a")))
                    raise OSError(28, "No space left on device")
        return real_write(path, data, **kwargs)

    monkeypatch.setattr(rfs, "write_atomic", write_with_failed_rollback)
    first = run(w, state=state, clock=clock, batch=1, measure_s=0)
    assert first["in_flight"] == ["job-a"]
    for prior_slices in range(1, 5):
        prior_stamp = state.sliced["job-a"]
        next_stamp = clock()
        attempted.clear()
        result = run(w, state=state, clock=clock, batch=1, measure_s=0)
        assert result["errors"] and all("rollback failed: OSError" in e["error"] for e in result["errors"]), result
        assert not result["progressed"] and result["deferred"] == {}, result
        assert slices.starts == slot_holders(w) == result["in_flight"] == ["job-a"], result
        unit = "slice" if prior_slices == 1 else "slices"
        expected = [(f"yielded: {prior_slices} {unit} since it started, for job-b", prior_stamp)]
        if prior_slices >= 3:
            expected.append((f"stalled: {prior_slices} slices in a row got no further than step 1", next_stamp))
        assert attempted == expected
        hold = state.holds["job-a"]
        assert (hold.slices, hold.reached, hold.stalls) == (prior_slices + 1, 1, prior_slices)
        assert state.sliced["job-a"] == next_stamp

    fail_stalls = False
    result = run(w, state=state, clock=clock, batch=1, measure_s=0)
    assert result["deferred"]["job-a"] == "stalled: 5 slices in a row got no further than step 1", result
    assert result["in_flight"] == slot_holders(w) == [], result
    assert "job-a" not in state.holds and "job-a" not in state.sliced
    assert run(w, state=state, clock=clock, batch=1, measure_s=0)["awaiting_check2"] == ["job-b"]


def test_a_waiter_that_becomes_protected_loses_its_ticket(world, monkeypatch):
    """After becoming eligible again, a waiter queues behind existing tickets (review r1, P2-3)."""
    w = world
    for n, job_id in enumerate(["job-a", "job-b", "job-c"]):
        w.job(job_id, worktree=False, created=f"2026-09-01T00:0{n}:00Z")
    clock, state = Clock(), retention.RetentionState()
    slices = Slices(monkeypatch, clock, {"job-a": always("progress")})
    budget = dict(batch=1, max_jobs=1, max_bytes=10**12, state=state, clock=clock)
    first = run(w, **budget)
    assert first["in_flight"] == ["job-a"] and set(state.queue) == {"job-b"}, first
    old_ticket = state.queue["job-b"]
    second = run(w, referenced_job_ids=["job-b"], **budget)
    assert second["awaiting_check2"] == ["job-c"] and "job-b" in second["protected"], second
    assert "job-b" not in state.queue and state.queue["job-a"] > old_ticket

    # Both live jobs are eligible again under the tighter pool budget. The
    # previously protected job cannot jump ahead of the job that just yielded.
    third = run(w, **{**budget, "max_jobs": 0})
    assert slices.starts == ["job-a", "job-c", "job-a"], third
    assert state.queue["job-b"] > old_ticket
    fourth = run(w, **{**budget, "max_jobs": 0})
    assert fourth["awaiting_check2"] == ["job-b"], fourth


def test_cancellation_after_a_yield_keeps_every_byte_and_the_waiters_turn(world, monkeypatch):
    """A cancelled pass can yield, then leave the queued start to the next pass (review r1, P2-3)."""
    w = world
    a_tree = w.job("job-a", created="2026-09-01T00:00:00Z")
    (a_tree / "untracked").write_bytes(b"bytes that only this tree holds\x00")
    w.job("job-b", worktree=False, created="2026-09-01T00:01:00Z")
    a_job, b_job = (w.root / "jobs" / job_id for job_id in ("job-a", "job-b"))
    trees = {path: snapshot(path) for path in (a_tree, a_job, b_job)}
    clock, state, cancel = Clock(), retention.RetentionState(), threading.Event()
    slices = Slices(monkeypatch, clock, {"job-a": always("progress")})
    rollback = rarch.Retirement.rollback

    def cancel_after_rollback(self, reason, **kwargs):
        result = rollback(self, reason, **kwargs)
        if reason.startswith("yielded"):
            cancel.set()
        return result

    monkeypatch.setattr(rarch.Retirement, "rollback", cancel_after_rollback)
    options = dict(state=state, clock=clock, batch=1, cancel=cancel)
    assert run(w, **options)["in_flight"] == ["job-a"]
    result = run(w, **options)
    assert result["interrupted"] == "cancelled" and slices.starts == ["job-a"], result
    assert result["listings"] == 0 and result["pruned"] == [], result
    assert all(w.store.get_job(job_id) is not None for job_id in ("job-a", "job-b"))
    assert {path: snapshot(path) for path in trees} == trees
    assert state.queue["job-b"] < state.queue["job-a"]

    cancel.clear()
    next_pass = run(w, **options)
    assert slices.starts == ["job-a", "job-b"] and next_pass["awaiting_check2"] == ["job-b"], next_pass


def test_a_yield_does_not_increase_the_backoff_of_the_next_error(world, monkeypatch):
    """Yielding a cached archive leaves its error count unchanged (review r1, P3-4)."""
    w = world
    hot_job(w, "job-a", "2026-09-01T00:00:00Z")
    w.job("job-b", worktree=False, created="2026-09-01T00:01:00Z")
    clock, state = Clock(), retention.RetentionState()
    slow_hot_reads(monkeypatch, clock, 100)
    options = dict(state=state, clock=clock, slice_s=60, batch=1)
    assert run(w, **options)["in_flight"] == ["job-a"]
    yielded = run(w, **options)
    assert yielded["deferred"].get("job-a", "").startswith("yielded"), yielded
    assert rarch.load_journal(w.root, "job-a")["state"] == "idle"
    archive = rarch.Retirement.archive

    def fail_after_yield(self, slice_end):
        if self.job_id == "job-a":
            raise OSError("archive error after yield")
        return archive(self, slice_end)

    monkeypatch.setattr(rarch.Retirement, "archive", fail_after_yield)
    failed = run(w, **options)
    assert failed["deferred"]["job-a"] == "error: OSError: archive error after yield", failed
    assert state.deferred["job-a"][0] - clock() == rarch.DEFER_ERROR_S
    assert rarch.load_journal(w.root, "job-a")["failures"] == 1


def test_a_job_waiting_for_check2_uses_no_batch_slot(world, monkeypatch):
    w = world
    w.job("job-a", worktree=False, created="2026-09-01T00:00:00Z")
    w.job("job-b", worktree=False, created="2026-09-01T00:01:00Z")
    clock, state = Clock(), retention.RetentionState()
    slices = Slices(monkeypatch, clock, {})
    options = dict(batch=1, state=state, clock=clock)
    first = run(w, **options)
    assert first["awaiting_check2"] == ["job-a"] and first["pruned"] == [], first
    second = run(w, **options)
    assert slices.starts == ["job-a", "job-b"] and second["deferred"] == {}, second
    assert second["pruned"] == ["job-a"] and second["awaiting_check2"] == ["job-b"], second
    assert second["listings"] == 1 and set(second["listing"]["first"]) == {"job-b"}, second
    assert set(second["listing"]["second"]) == {"job-a"}, second


BIG = 48 * 1024


def record_reached(monkeypatch, job_id: str) -> list[tuple[str, int, int]]:
    """Each archive call of `job_id`: its outcome, `last_work` and `last_reached`."""
    calls: list[tuple[str, int, int]] = []
    archive = rarch.Retirement.archive

    def recorded(self, slice_end):
        outcome = "raised"
        try:
            outcome = archive(self, slice_end)
            return outcome
        finally:
            if self.job_id == job_id:
                calls.append((outcome, self.last_work, self.last_reached))

    monkeypatch.setattr(rarch.Retirement, "archive", recorded)
    return calls


def strictly_further(calls: list[tuple[str, int, int]]) -> bool:
    parked = [reached for outcome, _, reached in calls if outcome == "parked"]
    return all(a < b for a, b in zip(parked, parked[1:]))


def test_slow_read_backs_are_progress_not_a_stall(world, monkeypatch):
    """Of a tree that does not change, every slice gets further, in each phase:
    six files whose every read outlasts the slice park once a file while the
    builder archives them and once a file while it reads their copies back. None of
    those slices is a stall, and the job retires."""
    w = world
    wt = w.job("job-big")
    for n in range(6):
        (wt / f"big-{n}.bin").write_bytes(os.urandom(BIG))
    clock = Clock()
    original = rfs.read_hashes
    reads = []

    def slow(fd, size, fmt, check=None):
        if size == BIG:
            reads.append(size)
            clock.advance(100)
        return original(fd, size, fmt, check)

    monkeypatch.setattr(rfs, "read_hashes", slow)
    calls = record_reached(monkeypatch, "job-big")
    state = retention.RetentionState()
    for _ in range(30):
        result = run(w, state=state, clock=clock, slice_s=60)
        assert not result["deferred"], result
        if result["pruned"]:
            break
    assert result["pruned"] == ["job-big"], result
    assert len(reads) >= 12, reads                  # archived and read back, each in its own slice
    # Strictly further every parked slice, across the walk, Git step and read-back.
    assert len([c for c in calls if c[0] == "parked"]) >= 11 and strictly_further(calls), calls


def test_a_slice_that_only_reads_copies_back_gets_further(world, monkeypatch):
    """A job with no worktree whose last two files each outlast the slice: the
    slice that reads the last one parks where the read-back begins, and the
    next slice only reads copies back. A copy read back counts as progress, so
    that slice gets further too."""
    w = world
    w.job("job-copies", worktree=False,
          files={"stdout": b"out\n", "z1.bin": os.urandom(BIG), "z2.bin": os.urandom(BIG)})
    clock = Clock()
    original = rfs.read_hashes

    def slow(fd, size, fmt, check=None):
        if size == BIG:
            clock.advance(100)
        return original(fd, size, fmt, check)

    monkeypatch.setattr(rfs, "read_hashes", slow)
    calls = record_reached(monkeypatch, "job-copies")
    state = retention.RetentionState()
    for _ in range(10):
        result = run(w, state=state, clock=clock, slice_s=60)
        assert not result["deferred"], result
        if result["pruned"]:
            break
    assert result["pruned"] == ["job-copies"], result
    assert len([c for c in calls if c[0] == "parked"]) >= 3 and strictly_further(calls), calls


def test_a_walk_begun_again_counts_from_the_tree_start(world, monkeypatch):
    """A `node_modules` package that holds a repository sends the slice's walk
    round again with that package archived (`NotRegenerable`). Twenty files
    precede it; six files whose every read outlasts the slice follow it. The
    slice that finds the repository counts the walk begun again from the
    tree's start, so later slices, which do not walk into it, still get
    further than it did: no stall, and the job retires."""
    w = world
    wt = w.job("job-nested")
    (wt / ".gitignore").write_text((wt / ".gitignore").read_text() + "node_modules/\n")
    for n in range(20):
        (wt / f"a{n:02}.txt").write_text(f"before {n}\n")
    (wt / "package.json").write_text('{"name": "web"}\n')
    pkg = wt / "node_modules" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text('{"name": "pkg"}\n')
    (pkg / "index.js").write_text("module.exports = 1;\n")
    git(pkg, "init", "--quiet")
    git(pkg, "add", "index.js")
    git(pkg, "commit", "--quiet", "-m", "only here")
    for n in range(6):
        (wt / f"z{n}.bin").write_bytes(os.urandom(BIG))
    clock = Clock()
    original = rfs.read_hashes

    def slow(fd, size, fmt, check=None):
        if size == BIG:
            clock.advance(100)
        return original(fd, size, fmt, check)

    monkeypatch.setattr(rfs, "read_hashes", slow)
    walks = []
    walk_init = rfs.RegenerableWalk.__init__

    def counting(self, root_fd, clear, denied=frozenset()):
        walks.append(set(denied))
        walk_init(self, root_fd, clear, denied)

    monkeypatch.setattr(rfs.RegenerableWalk, "__init__", counting)
    calls = record_reached(monkeypatch, "job-nested")
    state = retention.RetentionState()
    for _ in range(30):
        result = run(w, state=state, clock=clock, slice_s=60)
        assert not result["deferred"], result
        if result["pruned"]:
            break
    assert result["pruned"] == ["job-nested"], result
    assert walks[:2] == [set(), {"node_modules/pkg"}], walks      # the first slice walked round again
    assert strictly_further(calls), calls


SLOW = 40 * 1024


def test_a_repository_found_in_regenerable_output_is_progress(world, monkeypatch):
    """The walk verifies a venv's installed files by hash, one slow file a
    slice, then finds an editable clone (a `.git`) deeper in the same venv. It
    begins again with the venv archived, so each file it verified must now be
    stored, slice by slice, short of where the earlier slices got. The slice
    that finds the repository counts as progress and its position becomes the
    furthest, so the slices after it are no stall, and the job retires."""
    w = world
    wt = w.job("job-venv")
    ignore(wt, ".venv/")
    venv = wt / ".venv"
    write(venv / "pyvenv.cfg", "home = /usr/local/bin\nversion_info = 3.14\n")
    install(venv, "pkg", {f"pkg/d{n}.bin": os.urandom(SLOW) for n in range(5)})
    clone = venv / "lib" / "python3.14" / "site-packages" / "zz-editable"
    write(clone / "mod.py", "work that exists only here\n")
    git(clone, "init", "--quiet")
    git(clone, "add", "mod.py")
    git(clone, "commit", "--quiet", "-m", "only here")
    clock = Clock()
    original = rfs.read_hashes

    def slow(fd, size, fmt, check=None):
        if size == SLOW:
            clock.advance(100)
        return original(fd, size, fmt, check)

    monkeypatch.setattr(rfs, "read_hashes", slow)
    calls = record_reached(monkeypatch, "job-venv")
    state = retention.RetentionState()
    for _ in range(40):
        result = run(w, state=state, clock=clock, slice_s=60)
        assert not result["deferred"], (result["deferred"], calls)
        if result["pruned"]:
            break
    assert result["pruned"] == ["job-venv"], result
    # The shape happened: a slice after the finding got less far than an earlier one.
    assert not strictly_further(calls), calls


def test_three_consecutive_nested_repository_discoveries_do_not_stall(world, monkeypatch):
    """An unchanged tree gets through three slow discoveries (review r1, P2-1)."""
    w = world
    wt = w.job("job-nested")
    ignore(wt, "node_modules/")
    write(wt / "package.json", '{"name": "web"}\n')
    linked = wt / "0-linked"
    linked.mkdir()
    packages = [f"node_modules/pkg-{n}" for n in range(3)]
    for n, package in enumerate(packages):
        repo = wt / package
        repo.mkdir(parents=True)
        git(repo, "init", "--quiet", "--template=")
        write(repo / "package.json", '{"name": "nested"}\n')
        # All files in a repository are already in the walk's earlier prefix.
        for index, path in enumerate(sorted(p for p in repo.rglob("*") if p.is_file())):
            os.link(path, linked / f"{n}-{index}")

    clock, state = Clock(), retention.RetentionState()
    classify = rfs.RegenerableWalk.classify
    archive = rarch.Retirement.archive
    findings: list[list[str]] = []
    calls = []

    def slow_classify(self, *args):
        clock.advance(10)
        try:
            return classify(self, *args)
        except rfs.NotRegenerable as exc:
            findings[-1].append(exc.root)
            raise

    def record_archive(self, slice_end):
        findings.append([])
        outcome = archive(self, slice_end)
        calls.append((outcome, self.last_work))
        return outcome

    monkeypatch.setattr(rfs.RegenerableWalk, "classify", slow_classify)
    monkeypatch.setattr(rarch.Retirement, "archive", record_archive)
    for _ in range(60):
        result = run(w, state=state, clock=clock, slice_s=60, batch=1)
        assert not result["deferred"], (result, findings, calls)
        if result["pruned"]:
            break
    assert result["pruned"] == ["job-nested"], result
    found_in = [n for n, discovered in enumerate(findings) if discovered]
    assert [root for discovered in findings for root in discovered] == packages
    assert found_in[0] > 0 and found_in == list(range(found_in[0], found_in[0] + 3)), findings
    # A discovery is the only record in each of the three parked slices.
    assert [calls[n] for n in found_in] == [("parked", 1)] * 3, calls

