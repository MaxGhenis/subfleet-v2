"""C-11.10 (D-TASKHOLD1): the task hold's invariants, for every schedule drawn.

Each example builds an in-process daemon on a virtual clock (`fresh_daemon`), runs
a random schedule of holds, releases, job submissions (detached jobs of several
tasks, and conversation turns), lane holds, attempts that start or end, and clock
steps, with one admission pass after every step, and checks what each pass did
against a model of the holds kept apart from the daemon's own reading of them:

- T1: no attempt is created for a detached job of task T while a hold on T is in force;
- T2 (differential): for jobs of every other task admission decides as it would with
  no hold: the same schedule with the held task's jobs and its holds left out places
  every other job on the same pass, lane and model, whatever caps the pool has;
- T3: once a hold ends or is released, every job it kept waiting is looked at on the
  next pass, and no job is cancelled, failed or lost because of a hold;
- T4: a hold never changes, signals or re-places an attempt already live;
- T5 (differential): turns, and every job when no detached job of the held task
  exists, are placed exactly as with no hold.

Bounded: 25 examples per property (`TASK_HOLD_EXAMPLES` resizes them), derandomized,
no example database, so a plain run is repeatable and leaves no `.hypothesis`.
`TASK_HOLD_MUTANT=<name>` runs them against one of `tests/task_hold_mutants.py`'s mutants,
and `HYPOTHESIS_NO_SHRINK=1` stops at the first failing example.
"""

from __future__ import annotations

import os
from collections import Counter

import pytest
from hypothesis import HealthCheck, Phase, given, note, settings, strategies as st

from subfleet import task_holds
from tests.disk_floor_model import stamp
from tests.task_hold_mutants import MUTANTS
from tests.task_hold_support import finish, fresh_daemon, hold, release, submit, turn

EXAMPLES = int(os.environ.get("TASK_HOLD_EXAMPLES", "25"))
SETTINGS = settings(max_examples=EXAMPLES, deadline=None, derandomize=True, database=None,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large,
                                           HealthCheck.function_scoped_fixture])
if os.environ.get("HYPOTHESIS_NO_SHRINK"):
    # Each example builds one or two daemons, so shrinking a failure can take minutes:
    # this reports the first failure as found (the mutant runs in the report use it).
    SETTINGS = settings(SETTINGS, phases=[Phase.explicit, Phase.reuse, Phase.generate])


@pytest.fixture(autouse=True)
def mutant(monkeypatch):
    """`TASK_HOLD_MUTANT=<name>` runs these properties against one mutant (`tests/task_hold_mutants.py`)."""
    name = os.environ.get("TASK_HOLD_MUTANT")
    if name:
        MUTANTS[name](monkeypatch)
HELD_TASKS = ("build", "review")
TASKS = ("build", "review", "sweep", None)
LIVE = ("reserved", "starting", "running", "finalizing")
CAPS = st.sampled_from([None, {"max_active_attempts": 1}, {"max_active_attempts": 2},
                        {"max_in_flight_per_lane": 1}, {"max_active_attempts": 3, "max_in_flight_per_lane": 1}])

STEP = st.one_of(
    st.tuples(st.just("submit"), st.sampled_from(TASKS)),
    st.tuples(st.just("turn"), st.sampled_from(TASKS)),
    st.tuples(st.just("hold"), st.sampled_from(HELD_TASKS), st.sampled_from((30, 120, 600, 7200))),
    st.tuples(st.just("release"), st.sampled_from(HELD_TASKS)),
    st.tuples(st.just("tick"), st.sampled_from((1, 10, 61, 600))),
    st.tuples(st.just("finish"), st.integers(0, 7), st.booleans()),
    st.tuples(st.just("run"), st.integers(0, 7)),
    st.tuples(st.just("lane"), st.integers(1, 2), st.booleans()),
)


class Run:
    """One schedule on one daemon, recording what each pass placed and checking T1, T3 and T4."""

    def __init__(self, service, harness, clock, signals):
        self.service, self.harness, self.clock, self.signals = service, harness, clock, signals
        self.holds: dict[str, int] = {}          # the model: task -> end (virtual seconds)
        self.by_request: dict[str, str] = {}     # job id -> request id (the schedule's own name)
        self.passes: list[Counter] = []
        self.turns = 0

    def in_force(self) -> set[str]:
        return {task for task, end in self.holds.items() if end > self.clock[0]}

    def jobs(self) -> dict[str, dict]:
        return {row["job_id"]: dict(row) for row in self.service.store.query("SELECT * FROM jobs")}

    def live(self) -> list[dict]:
        marks = ",".join("?" * len(LIVE))
        return [dict(row) for row in self.service.store.query(
            f"SELECT a.*, j.kind FROM attempts a JOIN jobs j USING(job_id) WHERE a.state IN ({marks}) "
            "ORDER BY a.rowid", LIVE)]

    def step(self, index: int, step: tuple, *, check: bool = True) -> None:
        service, harness, clock = self.service, self.harness, self.clock
        kind = step[0]
        live_before = {row["attempt_id"]: row for row in self.live()}
        held_before = self.in_force()
        ended_by_schedule: set[str] = set()
        if kind == "submit":
            name = f"job-{index}"
            self.by_request[submit(service, harness, step[1], name)] = name
        elif kind == "turn":
            self.turns += 1
            self.by_request[turn(service, harness, index, task=step[1])] = f"turn-{index}"
        elif kind == "hold":
            hold(service, step[1], stamp(clock[0] + step[2]))
            self.holds[step[1]] = clock[0] + step[2]
        elif kind == "release":
            release(service, step[1])
            self.holds.pop(step[1], None)
        elif kind == "tick":
            clock[0] += step[1]
        elif kind == "finish":
            detached = [row for row in live_before.values() if row["kind"] != "turn"]
            if detached:
                attempt = detached[step[1] % len(detached)]
                # A transient end only below the job's last attempt: a job out of attempts fails
                # for its own reason, which is no hold's, and T3 would read it as one.
                finish(service, attempt, retry=step[2] and attempt["seq"] < 2)
                ended_by_schedule.add(attempt["attempt_id"])
        elif kind == "run":
            reserved = [row for row in live_before.values() if row["state"] == "reserved"]
            if reserved:
                attempt = reserved[step[1] % len(reserved)]
                with service.store.transaction("test.started", job_id=attempt["job_id"]) as tx:
                    tx.execute("UPDATE attempts SET state='running',started_at=? WHERE attempt_id=?",
                               (stamp(clock[0]), attempt["attempt_id"]))
                ended_by_schedule.add(attempt["attempt_id"])
        elif kind == "lane":
            lane = f"codex-{step[1]}"
            if step[2]:
                service.dispatch("lanes", {"action": "hold", "lane_id": lane, "until": stamp(clock[0] + 900)})
            else:
                service.dispatch("lanes", {"action": "release", "lane_id": lane})
        before = {row["attempt_id"] for row in self.service.store.query("SELECT attempt_id FROM attempts")}
        signals_before = len(self.signals)
        service._admit()
        made = [dict(row) for row in service.store.query("SELECT a.*, j.kind, j.task FROM attempts a "
                                                          "JOIN jobs j USING(job_id) ORDER BY a.rowid")
                if row["attempt_id"] not in before]
        self.passes.append(Counter((self.by_request.get(row["job_id"], row["job_id"]), row["lane_id"],
                                    row["model_requested"]) for row in made))
        if not check:
            return
        held = self.in_force()
        note(f"step {index} {step}: held {sorted(held)}, made {[(r['task'], r['kind']) for r in made]}")
        # T1: nothing of a held task starts; turns are never held.
        for row in made:
            assert not (row["kind"] != "turn" and row["task"] in held), ("T1", row)
        # T4: an attempt live before the step is exactly as it was, unless the schedule itself moved it.
        live_after = {row["attempt_id"]: row for row in self.live()}
        for attempt_id, row in live_before.items():
            if attempt_id in ended_by_schedule:
                continue
            assert live_after.get(attempt_id) == row, ("T4", attempt_id, row, live_after.get(attempt_id))
        assert len(self.signals) == signals_before, ("T4 signal", self.signals)
        # T3: a job of a task no longer held is looked at on this pass; none ends for a hold.
        jobs = self.jobs()
        for job_id, job in jobs.items():
            assert job["state"] not in ("failed", "cancelled", "lost"), ("T3 ended", job)
            if job["wait_reason"] == "task-held":
                assert job["kind"] != "turn" and job["task"] in held, ("T3 still held", job, held)
                assert job["next_check_at"] <= stamp(self.holds[job["task"]]), ("T3 clock", job)
            recorded = service._holds.get(job_id)
            if recorded and recorded.get("reason") == "task-held":
                assert job["task"] in held, ("T3 hold record", job_id, recorded)
        for task in held_before - held:
            # Every job the hold kept is now queued, placed, or waiting for something else.
            for job in jobs.values():
                if job["task"] == task and job["kind"] != "turn" and job["state"] in ("queued", "waiting"):
                    assert job["wait_reason"] != "task-held", ("T3 woken", job)

    def run(self, steps, *, check: bool = True) -> None:
        for index, step in enumerate(steps):
            self.step(index, step, check=check)


def schedule(draw_steps):
    return st.lists(STEP, min_size=1, max_size=draw_steps)


@SETTINGS
@given(steps=schedule(18), caps=CAPS)
def test_t1_t3_t4_over_random_schedules(steps, caps):
    with fresh_daemon(caps=caps) as daemon:
        Run(*daemon).run(steps)


def without(steps, task):
    """The schedule with no hold or release of `task` and no detached job of it.

    Steps keep their places (a removed step becomes a one-second-free no-op tick of
    zero), so the n-th pass of each run follows the same step of the schedule."""
    kept = []
    for step in steps:
        if step[0] in ("hold", "release") and step[1] == task or step[0] == "submit" and step[1] == task:
            kept.append(("tick", 0))
        else:
            kept.append(step)
    return kept


@SETTINGS
@given(steps=schedule(16), caps=CAPS, until=st.sampled_from((600, 7200, 10 ** 6)))
def test_t2_other_tasks_are_placed_as_with_no_hold(steps, caps, until):
    """The held task's jobs are inert while it is held: every other job's placements match
    a run in which the task and its hold never existed, pass for pass, until the hold ends."""
    steps = [("hold", "build", until), *[step for step in steps
                                        if not (step[0] in ("hold", "release") and step[1] == "build")]]
    # The hold ends at its clock; compare the passes before that instant only.
    with fresh_daemon(caps=caps) as daemon:
        held = Run(*daemon)
        held.run(steps)
        ends_at = []
        elapsed = 0
        for index, step in enumerate(steps):
            if step[0] == "tick":
                elapsed += step[1]
            if elapsed >= until:
                ends_at.append(index)
                break
    with fresh_daemon(caps=caps) as daemon:
        plain = Run(*daemon)
        plain.run(without(steps, "build"), check=False)
    horizon = ends_at[0] if ends_at else len(steps)
    for index in range(horizon):
        others = Counter({key: n for key, n in held.passes[index].items()})
        assert others == plain.passes[index], ("T2", index, steps[index], held.passes[index], plain.passes[index])


@SETTINGS
@given(steps=schedule(16), caps=CAPS)
def test_t5_turns_and_everything_else_ignore_a_hold_with_nothing_to_hold(steps, caps):
    """Holds on `build` with no detached `build` job: every pass places the same jobs, turns of
    task `build` among them, as the same schedule with the holds left out."""
    steps = [step for step in steps if not (step[0] == "submit" and step[1] == "build")]
    steps = [("hold", "build", 7200), *steps]
    with fresh_daemon(caps=caps) as daemon:
        held = Run(*daemon)
        held.run(steps)
    with fresh_daemon(caps=caps) as daemon:
        plain = Run(*daemon)
        plain.run([("tick", 0) if step[0] in ("hold", "release") and step[1] == "build" else step
                   for step in steps], check=False)
    assert held.passes == plain.passes, ("T5", held.passes, plain.passes)


@SETTINGS
@given(task=st.sampled_from(HELD_TASKS), until=st.integers(1, 10 ** 6), now=st.integers(0, 2 * 10 ** 6))
def test_a_hold_is_in_force_exactly_before_its_end(task, until, now):
    """C-11.10's clock, on the pure reader: no reaper, only the instant."""
    row = {"lease_key": task_holds.lease_key(task), "holder": "operator: n", "acquired_at": stamp(0),
           "expires_at": stamp(until)}
    assert (task in task_holds.in_force([row], stamp(now))) == (now < until)
