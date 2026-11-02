"""Retention's batch is fair, for every mix of jobs (C-8.4; review r1 of #286, P3-4).

The real pass (`retention.maintenance`, the real store, journals, quarantine
and rollback) runs over generated jobs whose archive slices are scripted
(`Slices`): some finish at once, some after a few slices, and some never, by
parking one step further each slice, by parking with work but no further, or
by parking with no work. Generated deadlines give a pass room for one slice or
several (a pass always begins: one whose deadline has passed returns at once), and generated gaps between passes let deferrals run out.

Say a job *waits* in a pass when, as the pass begins, it is in the store, not
in flight and not deferred (every pool here is over its budget and nothing is
pinned, so it is eligible), and the pass does not attempt to start it. The
properties:

1. At the end of every pass at most `batch` jobs are in flight.
2. A pass in which a job yielded its slot attempts a start.
3. Bounded waiting: a run of consecutive passes in which one job waits is at
   most 2(q + 1) passes long, where q is the number of jobs queued ahead of it
   at the end of the run's first pass; so never longer than 2N passes for N
   jobs. (Each pass with a job waiting attempts a start from the head of the
   queue, except a pass after one that started jobs too late to slice them all:
   such a pass slices one, which can then yield. A job that begins waiting
   later, or yields, queues behind every job waiting then.)

Before this change, property 3 failed within a few passes for any job behind a
job that never finishes once the batch was full.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from hypothesis import HealthCheck, Phase, given, settings, strategies as st

from subfleet import retention
from subfleet import retention_archive as rarch
from subfleet import retention_git as rgit
from tests.unit.retention_world import Clock, World
from tests.unit.test_retention_batch_fairness import Slices

EXAMPLES = int(os.environ.get("RETENTION_FAIRNESS_EXAMPLES", "60"))

PLAN = st.one_of(
    st.just(("done", 0)),
    st.tuples(st.just("finish"), st.integers(1, 4)),     # parks k slices, one step further each, then done
    st.just(("progress", 0)),                            # never finishes, always gets further
    st.just(("stall", 0)),                               # new work every slice, never further
    st.just(("idle", 0)),                                # no work at all (review r1's stub)
)


def plan_of(kind: str, k: int):
    if kind == "finish":
        return lambda n: "progress" if n % (k + 1) < k else "done"
    return lambda n: kind


class _Patch:
    """`monkeypatch` for one Hypothesis example (the fixture is per test)."""

    def __init__(self):
        self.undo: list[tuple[object, str, object]] = []

    def setattr(self, target, name, value):
        self.undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def close(self):
        for target, name, value in reversed(self.undo):
            setattr(target, name, value)


@settings(max_examples=EXAMPLES, deadline=None, phases=(Phase.explicit, Phase.reuse, Phase.generate, Phase.shrink),
          suppress_health_check=[HealthCheck.too_slow])
@given(batch=st.integers(1, 3), plans=st.lists(PLAN, min_size=2, max_size=5),
       rooms=st.lists(st.integers(0, 3), min_size=24, max_size=24),
       gaps=st.lists(st.sampled_from([0, 0, 0, 7 * 3600]), min_size=24, max_size=24))
def test_every_waiting_job_starts_within_a_bounded_number_of_passes(batch, plans, rooms, gaps):
    patch = _Patch()
    patch.setattr(rgit, "temp_roots", lambda: {"/nonexistent-temporary-root"})
    with tempfile.TemporaryDirectory() as tmp:
        w = World(Path(tmp))
        try:
            check(w, patch, batch, plans, rooms, gaps)
        finally:
            w.close()
            patch.close()


def check(w: World, patch: _Patch, batch: int, plans: list, rooms: list[int], gaps: list[int]) -> None:
    jobs = [f"job-{n}" for n in range(len(plans))]
    for n, job_id in enumerate(jobs):
        w.job(job_id, worktree=False, created=f"2026-09-01T00:{n:02}:00Z")
    # `deadline` is also checked against the real monotonic clock: run far ahead of it.
    clock = Clock(start=time.monotonic() + 1e6)
    cost = 60.0
    slices = Slices(patch, clock, {job_id: plan_of(*plan) for job_id, plan in zip(jobs, plans)}, cost=cost)
    state = retention.RetentionState()
    run_length = dict.fromkeys(jobs, 0)
    bound: dict[str, int] = {}
    passes = 2 * len(jobs) + 12
    for n in range(passes):
        clock.advance(gaps[n % len(gaps)])
        now = clock()
        flying = {job_id for job_id, j in rarch.journals(w.root).items()
                  if isinstance(j, dict) and j["state"] in rarch.IN_FLIGHT and not j.get("rollback_pending")}
        candidates = [job_id for job_id in jobs if w.store.get_job(job_id) is not None and job_id not in flying
                      and not (job_id in state.deferred and now < state.deferred[job_id][0])]
        before = len(slices.starts)
        result = retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0, holders=lambda watches, **_: {},
                                       state=state, clock=clock, slice_s=cost, batch=batch,
                                       deadline=clock() + 1 + rooms[n % len(rooms)] * cost)
        assert not result["errors"], result["errors"]
        started = set(slices.starts[before:])
        after = [job_id for job_id, j in rarch.journals(w.root).items()
                 if isinstance(j, dict) and j["state"] in rarch.IN_FLIGHT and not j.get("rollback_pending")]
        # 1. The batch bound.
        assert len(after) <= batch, (n, after, result)
        # 2. A yield makes room for a start in the same pass.
        if any(reason.startswith("yielded") for reason in result["deferred"].values()):
            assert started, (n, result)
        # 3. Bounded waiting.
        for job_id in jobs:
            if job_id in candidates and job_id not in started:
                if run_length[job_id] == 0:
                    queue = getattr(state, "queue", None)     # a build without tickets: the 2N bound
                    ahead = len(jobs) - 1 if queue is None else sum(1 for t in queue.values() if t < queue[job_id])
                    bound[job_id] = 2 * (ahead + 1)
                run_length[job_id] += 1
                assert run_length[job_id] <= bound[job_id] <= 2 * len(jobs), \
                    (job_id, n, run_length[job_id], bound[job_id], slices.starts, result["deferred"])
            else:
                run_length[job_id] = 0
