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

import os
import time

import pytest

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_fs as rfs
from tests.unit.retention_world import Clock, World, trust_temporary_directories


@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
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
    """A job whose first file in walk order is `HOT` bytes, read slowly."""
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
    `batch=1` the job behind it must still start, and the hot job is put back
    and deferred, its archive progress kept."""
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
    started_new = next((n for n, r in enumerate(results) if "job-new" in r["pruned"]), None)
    assert started_new is not None and started_new <= 2, [(r["pruned"], r["in_flight"], r["deferred"])
                                                           for r in results]
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
    in a row and is put back, its archive kept. The deferral is the error
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
    `job-b-new` retires there, and `job-a`, restarted, is put back as stalled."""
    w = world
    w.job("job-a", worktree=False, created="2026-09-01T00:00:00Z")
    w.job("job-b-new", worktree=False, created="2026-09-01T00:01:00Z")
    clock = Clock()
    slices = Slices(monkeypatch, clock, {"job-a": always("idle")})
    state = retention.RetentionState()
    results = [run(w, state=state, clock=clock, slice_s=60, batch=1) for _ in range(6)]
    assert results[0]["in_flight"] == ["job-a"] and results[0]["more"]
    assert results[1]["pruned"] == ["job-b-new"], results[1]
    assert results[1]["deferred"]["job-a"] == "yielded: 1 slice since it started, for job-b-new"
    assert results[1]["more"], results[1]           # the yielded job is work waiting, not an hour away
    assert slices.starts == ["job-a", "job-b-new", "job-a"], slices.starts
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
    assert [r["pruned"] for r in results] == [[], ["job-c"], ["job-d"], [], []]
    # Alone, it keeps its slot: nothing waits, so nothing asks it to yield.
    assert results[3]["in_flight"] == results[4]["in_flight"] == ["job-a"]
    assert all(not r["deferred"] for r in results[2:]), results
    w.job("job-n", worktree=False, created="2026-09-02T00:00:00Z")
    results = [run(w, state=state, clock=clock, slice_s=60, batch=1) for _ in range(2)]
    assert slices.starts[4:] == ["job-n", "job-a"], slices.starts
    assert results[0]["pruned"] == ["job-n"] and results[1]["in_flight"] == ["job-a"]
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
        flying = [job_id for job_id, j in rarch.journals(w.root).items() if j["state"] in rarch.IN_FLIGHT]
        assert len(flying) <= 2 and sorted(flying) == sorted(result["in_flight"]), (n, flying, result)
        if n:
            assert len(slices.starts) > before, (n, slices.starts)
    assert all(slices.starts.count(job_id) >= 3 for job_id in jobs), slices.starts


def test_a_pass_past_its_deadline_still_starts_one_job_beside_parked_ones(world, monkeypatch):
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
    w.job("job-d", worktree=False, created="2026-09-02T00:00:00Z")
    w.job("job-e", worktree=False, created="2026-09-02T00:01:00Z")
    measure = retention._Pass._measure

    def slow_measure(self, *args):
        measure(self, *args)
        clock.advance(3600)                 # pre-selection outlasts the deadline

    monkeypatch.setattr(retention._Pass, "_measure", slow_measure)
    result = run(w, state=state, clock=clock, slice_s=60, batch=3, deadline=clock() + 1, measure_s=0)
    assert slices.starts[3:] == ["job-d"], slices.starts
    assert result["pruned"] == ["job-d"] and result["in_flight"] == ["job-a"] and result["more"], result
