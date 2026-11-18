"""C-11.10 (D-TASKHOLD1): the task hold's invariants, for every schedule drawn.

Each example builds an in-process daemon on a virtual clock (`fresh_daemon`), runs
a random schedule of holds, releases, job submissions (detached jobs of several
tasks, jobs pinned to a lane, and conversation turns), lane holds, lanes disabled
and enabled again (so C-11.8's pin grace runs), attempts that start or end, ends
`transient` through the daemon's own finalizer (so C-4.5's retry backoff runs),
daemon restarts, and clock steps, with one admission pass after every step, and
checks what each pass did against a model of the holds kept apart from the
daemon's own reading of them:

- T1: no attempt is created for a detached job of task T while a hold on T is in force;
- T2 (differential): for jobs of every other task admission decides as it would with
  no hold: the same schedule with the held task's jobs and its holds left out places
  every other job on the same pass, lane and model, and leaves every other waiting
  job with the same state, reason and deadline, whatever caps the pool has;
- T3: once a hold ends or is released, every job it kept waiting is looked at on the
  next pass, and no job is cancelled, failed or lost because of a hold;
- T4: a hold never changes, signals or re-places an attempt already live (the signal
  count is read before the hold or release command, not after it);
- T5 (differential): turns, and every job when no detached job of the held task
  exists, are placed exactly as with no hold;
- T6 (clock neutrality): a job's pin grace ends where it would with the held
  intervals cut out of time: its episode's end is the model's, a job fails for its
  pin no earlier than that, and a retry a hold kept never starts before its backoff
  ends and is due again at that end, not later;
- T7 (no demand while held): no job a hold in force covers is reset-credit demand
  (`_reset_demand`), read right after each step's command, before the pass has looked
  at any job again (review r1's P2-3 window), and again after the pass.

Fix round 2 (review r2 of #260, F1 and F3): a hold may land with no pass right after it
(`hold-quiet`), as when the daemon is stopped, paced or still recovering, and the broad schedules
start with a job of `build` whose clock runs (queued, a pin grace, a retry's backoff, or reset-credit
demand) and a hold on `build`, so they reach held jobs. Each property asserts that its examples
reached the held states it is there to check (`reaching`): a held job taken, a paused grace, a kept
backoff, a job that was reset-credit demand when its hold landed, and the reset timer's pending
transaction run after a hold landed (`test_t7_the_reset_timer_spends_nothing_for_a_held_job`).

Fix round 5 adds a separate generated interleaving: a new hold, replacement or release
commits after the pass copies its job and before its door writes. T6 is checked against
the unheld gap, and each ending event must name the row's end exactly once.

Bounded: 25 examples per property (`TASK_HOLD_EXAMPLES` resizes them), derandomized,
no example database, so a plain run is repeatable and leaves no `.hypothesis`.
`TASK_HOLD_SHARD=1` allows `--hypothesis-seed` to select independent smaller runs
whose example counts add to the requested total, without repeating a derandomized prefix.
`TASK_HOLD_MUTANT=<name>` runs them against one of `tests/task_hold_mutants.py`'s mutants,
and `HYPOTHESIS_NO_SHRINK=1` stops at the first failing example.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections import Counter

import pytest
from hypothesis import HealthCheck, Phase, event, example, given, note, settings, strategies as st

from subfleet import task_holds
from subfleet.contracts import TRANSIENT_RETRY_DELAY_S, Exit
from tests.disk_floor_model import stamp
from tests.task_hold_mutants import MUTANTS
from tests.task_hold_support import (attempts, configure, finalize_transient, finish, fresh_daemon, hold, job,
                                     legacy_retry, release, restart, submit, turn)

EXAMPLES = int(os.environ.get("TASK_HOLD_EXAMPLES", "25"))
SETTINGS = settings(max_examples=EXAMPLES, deadline=None, derandomize=not bool(os.environ.get("TASK_HOLD_SHARD")),
                    database=None,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large,
                                           HealthCheck.function_scoped_fixture])
if os.environ.get("HYPOTHESIS_NO_SHRINK"):
    # Each example builds one or two daemons, so shrinking a failure can take minutes:
    # this reports the first failure as found (the mutant runs in the report use it).
    SETTINGS = settings(SETTINGS, phases=[Phase.explicit, Phase.reuse, Phase.generate])


@SETTINGS
@given(finalized_at=st.sampled_from((0, 1, 40, 100)))
def test_legacy_retries_keep_the_finalizer_deadline(finalized_at):
    with fresh_daemon() as (service, harness, clock, signals):
        build = legacy_retry(service, harness, clock, finalized_at)
        service = restart(service, harness)
        clock[0] = finalized_at + 10
        hold(service, "build", stamp(500))
        clock[0] = finalized_at + 11
        release(service, "build")
        due = finalized_at + TRANSIENT_RETRY_DELAY_S
        for instant in (finalized_at + 11, due - 1, due, due + 1):
            clock[0] = instant
            service._admit()
            count = len(attempts(service, build))
            if instant < due:
                assert count == 1, "R4-legacy: early retry"
            if instant == due + 1:
                assert count == 2, "R4-legacy: late retry"


@pytest.fixture(autouse=True)
def mutant(monkeypatch):
    """`TASK_HOLD_MUTANT=<name>` runs these properties against one mutant (`tests/task_hold_mutants.py`)."""
    name = os.environ.get("TASK_HOLD_MUTANT")
    if name:
        MUTANTS[name](monkeypatch)


#: How often the examples of the property running now reached each held state (`reach`).
REACH: Counter = Counter()
#: Whether a mutant is applied: it may change what is reached, and only an invariant kills it.
MUTATED = [bool(os.environ.get("TASK_HOLD_MUTANT"))]


def reach(state: str) -> None:
    REACH[state] += 1
    event(f"reached: {state}")


def reaching(prop, states) -> None:
    """Run the property `prop`, then assert that its examples reached each of `states` (F3: at r2,
    the broad property took a held job in 2 of 25 examples and never paused a grace or kept a
    backoff, so its T6 and T7 assertions effectively never fired)."""
    REACH.clear()
    prop()
    print(f"reach {prop.__name__}: {dict(sorted(REACH.items()))}")      # shown with -rP
    if not MUTATED[0]:
        missing = [state for state in states if not REACH[state]]
        assert not missing, ("reach: no example reached", missing, dict(REACH))
HELD_TASKS = ("build", "review")
TASKS = ("build", "review", "sweep", None)
LIVE = ("reserved", "starting", "running", "finalizing")
CAPS = st.sampled_from([None, {"max_active_attempts": 1}, {"max_active_attempts": 2},
                        {"max_in_flight_per_lane": 1}, {"max_active_attempts": 3, "max_in_flight_per_lane": 1}])

GRACES = st.sampled_from((60, 600))
STEP = st.one_of(
    st.tuples(st.just("submit"), st.sampled_from(TASKS)),
    st.tuples(st.just("turn"), st.sampled_from(TASKS)),
    st.tuples(st.just("hold"), st.sampled_from(HELD_TASKS), st.sampled_from((30, 120, 600, 7200))),
    st.tuples(st.just("release"), st.sampled_from(HELD_TASKS)),
    st.tuples(st.just("tick"), st.sampled_from((1, 10, 61, 600))),
    st.tuples(st.just("finish"), st.integers(0, 7), st.booleans()),
    st.tuples(st.just("run"), st.integers(0, 7)),
    st.tuples(st.just("lane"), st.integers(1, 2), st.booleans()),
    # Fix round 1: the clocks a hold must leave alone, and a restart in the middle.
    st.tuples(st.just("pin"), st.sampled_from(TASKS), st.integers(1, 2)),
    st.tuples(st.just("enable"), st.integers(1, 2), st.booleans()),
    st.tuples(st.just("transient"), st.integers(0, 7)),
    st.tuples(st.just("restart")),
    # Fix round 2 (F1): a hold with no admission pass right after it.
    st.tuples(st.just("hold-quiet"), st.sampled_from(HELD_TASKS), st.sampled_from((30, 120, 600, 7200))),
)
#: Steps after which no admission pass runs: a quiet hold, and `idle`, the no-op a differential's
#: other run puts in a quiet hold's place so the n-th pass of each run follows the same step.
QUIET = ("hold-quiet", "idle")


def grace_daemon(caps, grace, *, lanes: int = 2):
    """`fresh_daemon` with C-11.8's grace at `grace` seconds, kept across restarts."""
    @contextlib.contextmanager
    def made():
        with fresh_daemon(caps=caps, lanes=lanes) as (service, harness, clock, signals):
            configure(service, harness, lambda daemon: daemon.policy["admission"].update(pin_grace_s=grace))
            yield service, harness, clock, signals
    return made()


class Run:
    """One schedule on one daemon, recording what each pass placed and checking T1, T3, T4, T6 and T7."""

    def __init__(self, service, harness, clock, signals, *, grace: int | None = None):
        self.service, self.harness, self.clock, self.signals = service, harness, clock, signals
        self.grace = grace
        self.holds: dict[str, int] = {}          # the model: task -> end (virtual seconds)
        self.spans: dict[str, list[list[int]]] = {}   # task -> each stretch it was held, [start, end]
        self.by_request: dict[str, str] = {}     # job id -> request id (the schedule's own name)
        self.passes: list[Counter] = []
        self.waits: list[dict] = []              # per pass: request -> (state, reason, deadline), every job
        self.retries: dict[str, dict] = {}       # job id -> its transient attempt's retry deadline
        self.restarted: set[str] = set()         # jobs a restart found with an attempt live
        self.turns = 0
        self.seen_event = 0                      # the last event `reached` has read

    def in_force(self) -> set[str]:
        return {task for task, end in self.holds.items() if end > self.clock[0]}

    def held_between(self, task, start: int, end: int) -> int:
        """Seconds of [start, end] that a hold on `task` was in force, by the model."""
        return sum(max(0, min(stop, end) - max(begin, start)) for begin, stop in self.spans.get(task, ()))

    def grace_ends(self, task, since: int) -> int:
        """T6: when a grace of `self.grace` begun at `since` runs out, the held stretches cut out."""
        ends = since + self.grace
        while (moved := since + self.grace + self.held_between(task, since, ends)) != ends:
            ends = moved
        return ends

    def held_demand(self) -> list[tuple[str, str]]:
        """T7: the reset-credit demand (`_reset_demand`, the timer's own read) that a hold in
        force by the model covers: (request, task) for each such job. Empty when T7 holds."""
        held, jobs = self.in_force(), self.jobs()
        return [(self.by_request.get(item["job_id"], item["job_id"]), job["task"])
                for item in self.service._reset_demand()
                if (job := jobs.get(item["job_id"])) and job["kind"] != "turn" and job["task"] in held]

    def jobs(self) -> dict[str, dict]:
        return {row["job_id"]: dict(row) for row in self.service.store.query("SELECT * FROM jobs")}

    def demand_of(self, task: str) -> set[str]:
        """The reset-credit demand (`_reset_demand`) that is a detached job of `task`."""
        jobs = self.jobs()
        return {item["job_id"] for item in self.service._reset_demand()
                if (job := jobs.get(item["job_id"])) and job["kind"] != "turn" and job["task"] == task}

    def reached(self) -> None:
        """F3: the held states this step's command and pass reached, from the events they wrote."""
        for row in self.service.store.query("SELECT event_id, data_json FROM events WHERE kind='job.task_held' "
                                            "AND event_id>? ORDER BY event_id", (self.seen_event,)):
            data = json.loads(row["data_json"] or "{}")
            if data.get("taken"):
                reach("taken")
                if isinstance(data.get("pin_paused_s"), (int, float)):
                    reach("paused")
                if data.get("kept"):
                    reach("kept")
        last = self.service.store.one("SELECT max(event_id) AS n FROM events")["n"]
        self.seen_event = max(self.seen_event, last or 0)

    def live(self) -> list[dict]:
        marks = ",".join("?" * len(LIVE))
        return [dict(row) for row in self.service.store.query(
            f"SELECT a.*, j.kind FROM attempts a JOIN jobs j USING(job_id) WHERE a.state IN ({marks}) "
            "ORDER BY a.rowid", LIVE)]

    def step(self, index: int, step: tuple, *, check: bool = True) -> None:
        harness, clock = self.harness, self.clock
        kind = step[0]
        # T4: the signals before the command itself, so a hold or release that signals is seen.
        signals_before = len(self.signals)
        live_before = {row["attempt_id"]: row for row in self.live()}
        held_before = self.in_force()
        tasks_before = {job_id: job["wait_reason"] for job_id, job in self.jobs().items()}
        ended_by_schedule: set[str] = set()
        now = clock[0]
        if kind == "submit":
            name = f"job-{index}"
            self.by_request[submit(self.service, harness, step[1], name)] = name
        elif kind == "pin":
            name = f"pin-{index}"
            self.by_request[submit(self.service, harness, step[1], name, pinned_lane=f"codex-{step[2]}")] = name
        elif kind == "turn":
            self.turns += 1
            self.by_request[turn(self.service, harness, index, task=step[1])] = f"turn-{index}"
        elif kind in ("hold", "hold-quiet"):
            if self.demand_of(step[1]):
                reach("demand-window")      # a job of the task is reset-credit demand as its hold lands
            hold(self.service, step[1], stamp(now + step[2]))
            if step[1] in held_before:
                self.spans[step[1]][-1][1] = now + step[2]      # a replacement: one stretch, a new end
            else:
                self.spans.setdefault(step[1], []).append([now, now + step[2]])
            self.holds[step[1]] = now + step[2]
        elif kind == "release":
            release(self.service, step[1])
            if step[1] in held_before:
                self.spans[step[1]][-1][1] = now
            self.holds.pop(step[1], None)
        elif kind == "tick":
            clock[0] += step[1]
        elif kind in ("finish", "transient"):
            detached = [row for row in live_before.values() if row["kind"] != "turn"]
            if kind == "transient":
                # The real finalizer: a retry only below the job's last attempt (as `finish`).
                detached = [row for row in detached if row["seq"] < 2]
            if detached:
                attempt = detached[step[1] % len(detached)]
                if kind == "finish":
                    # A transient end only below the job's last attempt: a job out of attempts fails
                    # for its own reason, which is no hold's, and T3 would read it as one.
                    finish(self.service, attempt, retry=step[2] and attempt["seq"] < 2)
                else:
                    row = finalize_transient(self.service, attempt)
                    if (row["state"], row["wait_reason"]) == ("waiting", "capacity") and row["next_check_at"]:
                        self.retries[attempt["job_id"]] = {
                            "deadline": row["next_check_at"], "attempts": attempt["seq"],
                            "after": self.service.store.one("SELECT max(event_id) AS n FROM events")["n"]}
                ended_by_schedule.add(attempt["attempt_id"])
        elif kind == "run":
            reserved = [row for row in live_before.values() if row["state"] == "reserved"]
            if reserved:
                attempt = reserved[step[1] % len(reserved)]
                with self.service.store.transaction("test.started", job_id=attempt["job_id"]) as tx:
                    tx.execute("UPDATE attempts SET state='running',started_at=? WHERE attempt_id=?",
                               (stamp(now), attempt["attempt_id"]))
                ended_by_schedule.add(attempt["attempt_id"])
        elif kind == "lane":
            lane = f"codex-{step[1]}"
            if step[2]:
                self.service.dispatch("lanes", {"action": "hold", "lane_id": lane, "until": stamp(now + 900)})
            else:
                self.service.dispatch("lanes", {"action": "release", "lane_id": lane})
        elif kind == "enable":
            self.service.store.update_lane(f"codex-{step[1]}", enabled=int(step[2]))
        elif kind == "restart":
            # The restart is the schedule's own act: what its recovery does to an attempt that
            # was live, and to that attempt's job, is not the hold's.
            ended_by_schedule.update(live_before)
            self.restarted.update(row["job_id"] for row in live_before.values())
            self.service = restart(self.service, harness)
        if kind not in ("hold", "hold-quiet", "release"):
            # Only a hold or release is read from before its command (review r1); any other
            # step's own signals (a finalizer's, a restart's, a lane's) are the schedule's.
            signals_before = len(self.signals)
        # T7: read in both runs of a differential alike (it writes nothing), checked below.
        demand_before = self.held_demand()
        service = self.service
        before = {row["attempt_id"] for row in service.store.query("SELECT attempt_id FROM attempts")}
        if kind not in QUIET:
            service._admit()
        demand_after = self.held_demand()
        self.reached()
        made = [dict(row) for row in service.store.query("SELECT a.*, j.kind, j.task FROM attempts a "
                                                          "JOIN jobs j USING(job_id) ORDER BY a.rowid")
                if row["attempt_id"] not in before]
        self.passes.append(Counter((self.by_request.get(row["job_id"], row["job_id"]), row["lane_id"],
                                    row["model_requested"]) for row in made))
        jobs = self.jobs()
        self.waits.append({self.by_request.get(job_id, job_id): (job["task"], job["kind"], job["state"],
                                                                  job["wait_reason"], job["next_check_at"])
                           for job_id, job in jobs.items()})
        if not check:
            return
        now = clock[0]
        held = self.in_force()
        note(f"step {index} {step} at {now}: held {sorted(held)}, made {[(r['task'], r['kind']) for r in made]}")
        # T1: nothing of a held task starts; turns are never held.
        for row in made:
            assert not (row["kind"] != "turn" and row["task"] in held), ("T1", row)
        # T7: a held job is no reset-credit demand, before the pass looked at it again and after.
        assert demand_before == [], ("T7 before the pass", step, demand_before)
        assert demand_after == [], ("T7 after the pass", step, demand_after)
        # T4: an attempt live before the step is exactly as it was, unless the schedule itself moved it.
        live_after = {row["attempt_id"]: row for row in self.live()}
        for attempt_id, row in live_before.items():
            if attempt_id in ended_by_schedule:
                continue
            assert live_after.get(attempt_id) == row, ("T4", attempt_id, row, live_after.get(attempt_id))
        assert len(self.signals) == signals_before, ("T4 signal", step, self.signals[signals_before:])
        # T3: a job of a task no longer held is looked at on this pass; none ends for a hold.
        for job_id, job in jobs.items():
            if job["state"] in ("failed", "cancelled", "lost") and job_id not in self.restarted:
                # T6: the one end a schedule here may bring is C-11.8's, once the grace has run
                # out with the held stretches cut out of it, and never earlier.
                refused = service.store.one("SELECT data_json FROM events WHERE job_id=? AND kind='job.pin_refused' "
                                            "ORDER BY event_id DESC LIMIT 1", (job_id,))
                assert job["state"] == "failed" and job["rc"] == int(Exit.NO_LANE) and refused, ("T3 ended", job)
                since = seconds(json.loads(refused["data_json"])["since"])
                assert self.grace is not None and self.grace_ends(job["task"], since) <= seconds(job["finished_at"]), \
                    ("T6 grace: failed early", job, since, self.spans.get(job["task"]))
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
        # T6, the pin grace: an episode's end, while no hold keeps its job, is the model's.
        for job_id, episode in service._pin_episodes.items():
            job = jobs.get(job_id)
            if (job is None or not episode.get("open") or not episode.get("fail_at") or self.grace is None
                    or job["state"] not in ("queued", "waiting") or job["wait_reason"] == "task-held"
                    or "paused_s" in episode):
                continue
            task = job["task"] if job["kind"] != "turn" else None
            assert episode["fail_at"] == stamp(self.grace_ends(task, seconds(episode["since"]))), \
                ("T6 grace", job_id, episode, self.spans.get(task))
        # T6, the retry backoff: a retry a hold kept starts no earlier than its deadline, and the
        # hold's end leaves it due at that deadline, not later.
        for job_id, retry in self.retries.items():
            job = jobs[job_id]
            pinned = service.store.one("SELECT 1 FROM events WHERE job_id=? AND kind LIKE 'job.pin_%' "
                                       "AND event_id>?", (job_id, retry["after"]))
            was_held = service.store.one("SELECT 1 FROM events WHERE job_id=? AND kind='job.task_held' "
                                         "AND event_id>?", (job_id, retry["after"]))
            if pinned or not was_held or job_id in self.restarted:
                continue
            started = [row for row in made if row["job_id"] == job_id and row["seq"] > retry["attempts"]]
            assert not (started and stamp(now) < retry["deadline"]), ("T6 retry: started early", job_id, retry)
            if (tasks_before.get(job_id) == "task-held" and job["wait_reason"] != "task-held"
                    and not started and stamp(now) < retry["deadline"] and job["state"] == "waiting"):
                assert (job["wait_reason"], job["next_check_at"]) == ("capacity", retry["deadline"]), \
                    ("T6 retry: the hold's end moved its deadline", job_id, job, retry)
            # Fix round 2 (F2): a hold keeps the retry's deadline only while it is ahead. Each end
            # gives back that deadline if both the take and the end came before it, else no clock:
            # never a capacity recheck's, which capacity freeing up would bring forward unheld.
            for ended in service.store.query(
                    "SELECT event_id, ts, data_json FROM events WHERE job_id=? AND kind='job.task_hold_ended' "
                    "AND event_id>? ORDER BY event_id", (job_id, retry.get("ends_seen", retry["after"]))):
                retry["ends_seen"] = ended["event_id"]
                data = json.loads(ended["data_json"])
                if "pin_fail_at" in data:
                    continue
                taken = service.store.one(
                    "SELECT ts FROM events WHERE job_id=? AND kind='job.task_held' AND event_id>? AND event_id<? "
                    "AND json_extract(data_json,'$.taken') ORDER BY event_id DESC LIMIT 1",
                    (job_id, retry["after"], ended["event_id"]))
                if taken is None:
                    continue
                ahead = seconds(taken["ts"]) < seconds(retry["deadline"]) and \
                    seconds(ended["ts"]) < seconds(retry["deadline"])
                assert data.get("next_check_at") == (retry["deadline"] if ahead else None), \
                    ("T6 retry: the hold's end gave back a clock that is no retry deadline", job_id, data, retry)

    def run(self, steps, *, check: bool = True) -> None:
        for index, step in enumerate(steps):
            self.step(index, step, check=check)


def seconds(text: str) -> int:
    """A store instant as the model's virtual seconds."""
    from tests.disk_floor_model import BASE
    return int((task_holds.instant(text) - BASE).total_seconds())


def schedule(draw_steps):
    return st.lists(STEP, min_size=1, max_size=draw_steps)


#: Fix round 2 (F3): how a broad schedule starts, with a job of `build` whose clock runs when
#: the first hold on `build` lands, so the schedules reach held jobs (r2: a hold took a job in 2
#: of 25 examples, and none paused a grace, kept a backoff, or met a job that was demand).
PRELUDES = {
    "queued": [("submit", "build")],
    "pinned": [("enable", 1, False), ("pin", "build", 1)],          # C-11.8's grace runs from here
    "backoff": [("submit", "build"), ("transient", 0)],             # C-4.5's backoff runs from here
    "demand": [("lane", 1, True), ("lane", 2, True), ("submit", "build")],   # C-23.16's demand
}
FIRST_HOLD = st.tuples(st.sampled_from(("hold", "hold-quiet")), st.just("build"),
                       st.sampled_from((30, 90, 120, 600, 7200)))
BROAD_REACH = ("taken", "paused", "kept", "demand-window")


@SETTINGS
# Review r2's two steps, which kill `grace-runs-while-held` here and in the T6-one-job property.
@example(prelude="pinned", first=("hold", "build", 90), steps=[("tick", 120)], caps=None, grace=60)
# F1: the same hold with no pass after it, and a release no pass saw either.
@example(prelude="pinned", first=("hold-quiet", "build", 90), steps=[("tick", 120)], caps=None, grace=60)
@example(prelude="queued", first=("hold-quiet", "build", 120), steps=[("tick", 61), ("tick", 61)], caps=None,
         grace=60)
# T6's backoff and T7's demand, each met by a hold.
@example(prelude="backoff", first=("hold", "build", 30), steps=[("tick", 61)], caps=None, grace=600)
@example(prelude="demand", first=("hold-quiet", "build", 600), steps=[("tick", 1)], caps=None, grace=600)
@given(prelude=st.sampled_from(sorted(PRELUDES)), first=FIRST_HOLD, steps=schedule(18), caps=CAPS, grace=GRACES)
def broad_schedules(prelude, first, steps, caps, grace):
    with grace_daemon(caps, grace) as daemon:
        Run(*daemon, grace=grace).run([*PRELUDES[prelude], first, *steps])


def test_t1_t3_t4_t6_over_random_schedules():
    reaching(broad_schedules, BROAD_REACH)


def without(steps, task):
    """The schedule with no hold or release of `task` and no detached job of it.

    Steps keep their places (a removed step becomes a one-second-free no-op tick of
    zero), so the n-th pass of each run follows the same step of the schedule."""
    kept = []
    for step in steps:
        if step[0] in ("hold", "hold-quiet", "release", "submit", "pin") and step[1] == task:
            kept.append(("idle",) if step[0] in QUIET else ("tick", 0))
        else:
            kept.append(step)
    return kept


def others(waits: dict, task: str) -> dict:
    """A pass's waiting states and deadlines, but for the detached jobs of `task`."""
    return {name: row for name, row in waits.items() if not (row[0] == task and row[1] != "turn")}


@SETTINGS
@given(steps=schedule(16), caps=CAPS, until=st.sampled_from((600, 7200, 10 ** 6)), grace=GRACES)
def test_t2_other_tasks_are_placed_as_with_no_hold(steps, caps, until, grace):
    """The held task's jobs are inert while it is held: every other job's placements, and
    every other waiting job's state, reason and deadline, match a run in which the task
    and its hold never existed, pass for pass, until the hold ends."""
    steps = [("hold", "build", until), *[step for step in steps
                                        if not (step[0] in ("hold", "hold-quiet", "release") and step[1] == "build")]]
    # The hold ends at its clock; compare the passes before that instant only.
    with grace_daemon(caps, grace) as daemon:
        held = Run(*daemon, grace=grace)
        held.run(steps)
        ends_at = []
        elapsed = 0
        for index, step in enumerate(steps):
            if step[0] == "tick":
                elapsed += step[1]
            if elapsed >= until:
                ends_at.append(index)
                break
    with grace_daemon(caps, grace) as daemon:
        plain = Run(*daemon, grace=grace)
        plain.run(without(steps, "build"), check=False)
    horizon = ends_at[0] if ends_at else len(steps)
    for index in range(horizon):
        placed = Counter({key: n for key, n in held.passes[index].items()})
        assert placed == plain.passes[index], ("T2", index, steps[index], held.passes[index], plain.passes[index])
        assert others(held.waits[index], "build") == plain.waits[index], \
            ("T2 waits", index, steps[index], others(held.waits[index], "build"), plain.waits[index])


@SETTINGS
@given(steps=schedule(16), caps=CAPS, grace=GRACES)
def test_t5_turns_and_everything_else_ignore_a_hold_with_nothing_to_hold(steps, caps, grace):
    """Holds on `build` with no detached `build` job: every pass places the same jobs, turns of
    task `build` among them, and leaves every job waiting as, the same schedule with the holds
    left out."""
    steps = [step for step in steps if not (step[0] in ("submit", "pin") and step[1] == "build")]
    steps = [("hold", "build", 7200), *steps]
    with grace_daemon(caps, grace) as daemon:
        held = Run(*daemon, grace=grace)
        held.run(steps)
    with grace_daemon(caps, grace) as daemon:
        plain = Run(*daemon, grace=grace)
        plain.run([(("idle",) if step[0] in QUIET else ("tick", 0))
                   if step[0] in ("hold", "hold-quiet", "release") and step[1] == "build" else step
                   for step in steps], check=False)
    assert held.passes == plain.passes, ("T5", held.passes, plain.passes)
    assert held.waits == plain.waits, ("T5 waits", held.waits, plain.waits)


@SETTINGS
@given(task=st.sampled_from(HELD_TASKS), until=st.integers(1, 10 ** 6), now=st.integers(0, 2 * 10 ** 6))
def test_a_hold_is_in_force_exactly_before_its_end(task, until, now):
    """C-11.10's clock, on the pure reader: no reaper, only the instant."""
    row = {"lease_key": task_holds.lease_key(task), "holder": "operator: n", "acquired_at": stamp(0),
           "expires_at": stamp(until)}
    assert (task in task_holds.in_force([row], stamp(now))) == (now < until)


# --- T6, one job: its clock already running, then random holds ------------------------------------

CLOCK_STEP = st.one_of(
    st.tuples(st.just("tick"), st.sampled_from((1, 5, 20, 45, 120))),
    st.tuples(st.just("hold"), st.sampled_from((10, 30, 90, 300))),
    st.tuples(st.just("release")),
    st.tuples(st.just("restart")),
    # Fix round 2 (F1): a hold with no pass after it, and time with no pass (the daemon stopped,
    # paced by C-5.10, or still in startup recovery); (F2) the blocker of `backoff-blocked` ends.
    st.tuples(st.just("hold-quiet"), st.sampled_from((10, 30, 90, 300))),
    st.tuples(st.just("pause"), st.sampled_from((20, 100, 400))),
    st.tuples(st.just("finish")),
)
#: The steps after which no pass runs.
CLOCK_QUIET = ("hold-quiet", "pause")
ONE_JOB_REACH = ("taken", "paused", "kept", "recheck-held", "kept-late")
#: Review r2's F2 input as clock steps: the retry is due at 60 on the job's one lane, whose one
#: slot a review holds, so the job's C-6.10 recheck backs off to 121 by t=92.
BLOCKED_TO_92 = [("tick", 60), ("tick", 1), ("tick", 2), ("tick", 4), ("tick", 8), ("tick", 16), ("tick", 1)]
#: Fix round 3 (review r3 of #260, R3-2): how long after the provider's `transient` exit at t=0
#: the finalizer runs, for kind `backoff` (load, a restart): the retry is due 60 s after that.
LATE = st.sampled_from((0, 0, 1, 40, 100))


@SETTINGS
@example(kind="pinned", grace=60, late=0, steps=[("hold", 90), ("tick", 120)])                     # review r2's
@example(kind="pinned", grace=60, late=0, steps=[("hold-quiet", 90), ("tick", 120)])               # F1: expiry
@example(kind="pinned", grace=60, late=0, steps=[("hold-quiet", 300), ("pause", 100), ("release",)])   # F1: release
@example(kind="all-lanes", grace=60, late=0, steps=[("hold-quiet", 90), ("pause", 100), ("restart",)])  # F1: restart
@example(kind="backoff", grace=60, late=0, steps=[("hold", 30), ("tick", 20), ("tick", 45)])       # a kept backoff
@example(kind="backoff-blocked", grace=60, late=0, steps=[*BLOCKED_TO_92, ("hold", 1), ("tick", 1), ("finish",)])
@example(kind="backoff-blocked", grace=60, late=0, steps=[*BLOCKED_TO_92, ("hold", 300), ("tick", 1), ("release",),
                                                          ("finish",)])
# Review r3's R3-2: finalized at 100, held at 110, released at 111; the retry is due at 160.
@example(kind="backoff", grace=60, late=100, steps=[("tick", 10), ("hold", 10), ("tick", 1), ("release",),
                                                     ("tick", 49)])
@given(kind=st.sampled_from(("pinned", "all-lanes", "backoff", "backoff-blocked")), grace=GRACES, late=LATE,
       steps=st.lists(CLOCK_STEP, min_size=1, max_size=14))
def one_job_schedules(kind, grace, late, steps):
    """T6 where the schedules above seldom reach: one build whose clock runs from t=0, a
    C-11.8 grace on its disabled pinned lane (`pinned`) or with every lane disabled after a
    `transient` end (`all-lanes`), or C-4.5's backoff after a `transient` end, due at
    `TRANSIENT_RETRY_DELAY_S` (`backoff`), on the job's one lane while a review holds its one
    slot until a `finish` step (`backoff-blocked`, review r2's F2). Then random holds on `build`
    (a hold in force is replaced, later or sooner), releases, daemon restarts and clock steps, a
    pass after each but a quiet hold or a pause, against a model of the stretches the task was held:

    - a grace runs out exactly where the held time cut out of it puts it: the job fails (rc 3,
      C-11.8) on the first pass that finds that instant passed and no hold in force, never
      before, and until then, whenever no hold keeps it, its episode ends there;
    - a backoff's retry starts on the first pass at or after its deadline with no hold in
      force, never before (the deadline is an instant: held time does not move it); a blocked
      one, on the first such pass once its blocker has ended too, as unheld, where a freed slot
      brings a capacity wait forward (C-6.10): a hold keeps no capacity recheck as a deadline
      (a restart, which forgets what freed up, is a plain pass in this kind); in kind `backoff`
      the finalizer runs `late` seconds after the provider's exit (fix round 3, R3-2), and the
      deadline is 60 s after the finalizer, not after the exit;
    - nothing else ends the job (T3)."""
    blocked = kind == "backoff-blocked"
    with grace_daemon({"max_in_flight_per_lane": 1} if blocked else None, grace, lanes=1 if blocked else 2) \
            as (service, harness, clock, signals):
        build = submit(service, harness, "build", "build-1", **({"pinned_lane": "codex-1"} if kind == "pinned" else {}))
        if kind == "pinned":
            service.store.update_lane("codex-1", enabled=0)
        else:
            service._admit()
            [first] = attempts(service, build)
            if kind == "backoff":
                finalize_transient(service, dict(first), clock=clock, finalized_at=late)
            else:
                finalize_transient(service, dict(first))
            if kind == "all-lanes":
                for lane in ("codex-1", "codex-2"):
                    service.store.update_lane(lane, enabled=0)
        blocking = None
        if blocked:
            # A priority caller's review (C-6.16), so the build's own wait keeps no slot from it (C-6.9).
            configure(service, harness, lambda daemon: daemon.policy["admission"].update(priority_callers=["blocker"]))
            blocker = submit(service, harness, "review", "review-1", pinned_lane="codex-1", caller_session="blocker")
        service._admit()                            # t=0 (`late`): the grace, or the backoff, runs from here
        deadline = (late if kind == "backoff" else 0) + TRANSIENT_RETRY_DELAY_S
        if blocked:
            [blocking] = attempts(service, blocker)
        spans: list[list[int]] = []                 # each stretch `build` was held, [start, end]
        until: int | None = None                    # the end of the hold in force, by the model
        expected: int | None = None                 # the pass the job fails at, or its retry starts at
        finished: int | None = None                 # when the blocker ended
        seen = [service.store.one("SELECT max(event_id) AS n FROM events")["n"] or 0]

        def held_at(at: int) -> bool:
            return until is not None and until > at

        def grace_end() -> int:
            ends = grace
            while (moved := grace + sum(max(0, min(stop, ends) - begin) for begin, stop in spans)) != ends:
                ends = moved
            return ends

        def count_reached() -> None:
            for row in service.store.query("SELECT event_id, data_json FROM events WHERE kind='job.task_held' "
                                           "AND event_id>? ORDER BY event_id", (seen[0],)):
                data = json.loads(row["data_json"] or "{}")
                if data.get("taken"):
                    reach("taken")
                    if isinstance(data.get("pin_paused_s"), (int, float)):
                        reach("paused")
                    if data.get("kept"):
                        reach("kept")
                        if kind == "backoff" and late:
                            reach("kept-late")      # R3-2: the deadline of a late finalizer
            seen[0] = service.store.one("SELECT max(event_id) AS n FROM events")["n"] or 0
        for step in steps:
            now = clock[0]
            if step[0] in ("tick", "pause"):
                clock[0] += step[1]
            elif step[0] in ("hold", "hold-quiet"):
                row = service.store.get_job(build)
                if (kind.startswith("backoff") and now >= deadline
                        and (row["state"], row["wait_reason"]) == ("waiting", "capacity")
                        and row["next_check_at"] and row["next_check_at"] > stamp(now)):
                    reach("recheck-held")           # F2: a hold meets a capacity recheck, past the deadline
                if held_at(now):
                    spans[-1][1] = now + step[1]            # a replacement: one stretch, a new end
                else:
                    spans.append([now, now + step[1]])
                until = now + step[1]
                hold(service, "build", stamp(until))
            elif step[0] == "release":
                if held_at(now):
                    spans[-1][1] = now
                until = None
                release(service, "build")
            elif step[0] == "finish":
                if blocking is not None and finished is None:
                    finish(service, blocking)
                    finished = now
            elif not blocked:
                service = restart(service, harness)
            count_reached()
            if step[0] in CLOCK_QUIET:
                row = service.store.get_job(build)
                assert row["state"] not in ("failed", "cancelled", "lost"), ("T3 ended with no pass", dict(row))
                continue
            service._admit()
            count_reached()
            at = clock[0]
            if blocked:
                due = max(TRANSIENT_RETRY_DELAY_S, finished) if finished is not None else None
            else:
                due = deadline if kind == "backoff" else grace_end()
            if expected is None and due is not None and at >= due and not held_at(at):
                expected = at
            row = service.store.get_job(build)
            note(f"{step} at {at}: held {spans}, due {due}, row {(row['state'], row['wait_reason'], row['next_check_at'])}")
            if kind.startswith("backoff"):
                retried = [attempt["reserved_at"] for attempt in attempts(service, build) if attempt["seq"] > 1]
                assert retried == ([stamp(expected)] if expected is not None else []), \
                    ("T6 retry", at, retried, expected, spans, finished)
                assert row["state"] not in ("failed", "cancelled", "lost"), ("T3 ended", dict(row))
                continue
            if expected is not None:
                assert (row["state"], row["rc"], row["finished_at"]) == ("failed", int(Exit.NO_LANE), stamp(expected)), \
                    ("T6 grace: not failed when it ran out", at, expected, spans, dict(row))
                return
            assert row["state"] in ("queued", "waiting"), ("T6 grace: failed early", at, due, spans, dict(row))
            episode = service._pin_episodes.get(build) or {}
            if row["wait_reason"] != "task-held" and "paused_s" not in episode:
                assert episode.get("fail_at") == stamp(due), ("T6 grace: the episode's end", at, episode, spans)


def test_t6_one_jobs_clock_under_random_holds():
    reaching(one_job_schedules, ONE_JOB_REACH)


# --- T7, the reset timer: its pending transaction after a hold lands ----------------------------

RESET_STEP = st.sampled_from([("admit",), ("hold",), ("release",), ("cycle",), ("cycle-hold-late",)])
RESET_REACH = ("demand-window", "pending")


@SETTINGS
@example(steps=[("cycle-hold-late",)])              # the hold lands between the last demand read and the spend
@example(steps=[("hold",), ("cycle",)])             # r1's P2-3: the hold lands, the timer runs before any pass
@given(steps=st.lists(RESET_STEP, min_size=1, max_size=8))
def reset_schedules(steps):
    """T7 through the reset timer itself (C-23.16), on `test_task_hold_reset_demand`'s fleet: six
    Codex lanes at their limit with a gift banked on each, the fake reset-credit endpoints, and a
    build waiting on capacity, which is demand. Random holds and releases of `build`, admission
    passes, and reset cycles, some with the hold landing inside the cycle, after the timer's last
    read of demand and before its pending transaction (`cycle-hold-late`). Whenever a hold on
    `build` is in force, the build is no demand, and a cycle spends no credit, writes no reset
    action and confirms no reset; `cycle-hold-late` reaches the pending transaction with a hold
    in force. A credit spent while no hold is in force is the timer's to spend."""
    from tests.fake.test_task_hold_reset_demand import hold_build, reset_fleet
    with reset_fleet() as (service, http, build):
        held = False
        for index, step in enumerate(steps):
            spent_before = (list(http.consumed()), len(service.store.query("SELECT * FROM actions")),
                            len(service.store.query("SELECT * FROM events WHERE kind='reset-credit.confirmed'")))
            result = None
            if step[0] == "admit":
                service._admit()
            elif step[0] == "hold":
                if build in {item["job_id"] for item in service._reset_demand()}:
                    reach("demand-window")
                hold_build(service)
                held = True
            elif step[0] == "release":
                service.dispatch("tasks", {"action": "release", "task": "build"})
                held = False
            elif step[0] == "cycle":
                result = service.timers.reset_credits_cycle()
            else:
                reader, reads = service.timers.demand, []

                def demand():
                    found = reader()
                    reads.append(found)
                    if len(reads) == 2:             # the timer's last read, just before its pending transaction
                        if build in {item["job_id"] for item in found}:
                            reach("pending")
                        hold_build(service)
                    return found
                service.timers.demand = demand
                try:
                    result = service.timers.reset_credits_cycle()
                finally:
                    service.timers.demand = reader
                if len(reads) < 2:
                    hold_build(service)             # the cycle ended before it: the hold lands after it
                held = True
            note(f"step {index} {step}: held {held}, result {result}")
            if held:
                assert build not in {item["job_id"] for item in service._reset_demand()}, \
                    ("T7: a held build was reset-credit demand", step)
                if result is not None:
                    spent_after = (list(http.consumed()), len(service.store.query("SELECT * FROM actions")),
                                   len(service.store.query("SELECT * FROM events WHERE kind='reset-credit.confirmed'")))
                    assert spent_after == spent_before and result.get("status") != "confirmed", \
                        ("T7-pending: the timer spent a credit for a held build", step, result)


def test_t7_the_reset_timer_spends_nothing_for_a_held_job():
    reaching(reset_schedules, RESET_REACH)


# --- the properties against the mutants (opt-in) ------------------------------------------------

PROPERTIES = {
    "T1/T3/T4/T6/T7": test_t1_t3_t4_t6_over_random_schedules,
    "T2": test_t2_other_tasks_are_placed_as_with_no_hold,
    "T5": test_t5_turns_and_everything_else_ignore_a_hold_with_nothing_to_hold,
    "T6-one-job": test_t6_one_jobs_clock_under_random_holds,
    "T7-reset": test_t7_the_reset_timer_spends_nothing_for_a_held_job,
    "clock": test_a_hold_is_in_force_exactly_before_its_end,
}
#: `TASK_HOLD_MUTANT_MATRIX=all` (or a comma list of mutant names) runs every property against
#: each mutant, and `TASK_HOLD_MUTANT_REPORT=<file>` gets one JSON line per cell: killed (`K`,
#: with the counterexample's first line) or survived (`S`). Opt-in: a surviving cell runs every
#: example, and a cell may survive by design (each mutant is caught by its named scenario in
#: `test_task_hold_mutants.py`; the matrix says which properties catch it too).
MATRIX = [name for name in (MUTANTS if os.environ.get("TASK_HOLD_MUTANT_MATRIX") == "all"
                            else os.environ.get("TASK_HOLD_MUTANT_MATRIX", "").split(",")) if name]


@pytest.mark.skipif(not MATRIX, reason="opt-in: TASK_HOLD_MUTANT_MATRIX=all or a comma list of mutants")
@pytest.mark.parametrize("prop", sorted(PROPERTIES))
@pytest.mark.parametrize("name", MATRIX or ["none"])
def test_the_properties_against_a_mutant(name, prop, monkeypatch):
    MUTANTS[name](monkeypatch)
    MUTATED[0] = True                         # only an invariant kills a mutant, never a reach count
    try:
        PROPERTIES[prop]()
    except Exception as exc:                  # noqa: BLE001 -- any failure is the mutant's kill
        cell = {"mutant": name, "property": prop, "result": "K",
                "first": f"{type(exc).__name__}: {str(exc).splitlines()[0][:300] if str(exc) else ''}"}
    else:
        cell = {"mutant": name, "property": prop, "result": "S"}
    finally:
        MUTATED[0] = bool(os.environ.get("TASK_HOLD_MUTANT"))
    report = os.environ.get("TASK_HOLD_MUTANT_REPORT")
    if report:
        with open(report, "a", encoding="utf-8") as out:
            out.write(json.dumps(cell) + "\n")


@st.composite
def midpass_hold_commits(draw):
    """A command commits after the pass copies its job and before either door write."""
    operation = draw(st.sampled_from(("new", "replacement", "release")))
    first_end = draw(st.integers(15, 25))
    commit_at = draw(st.integers(first_end + 1, first_end + 30))
    expires_before_door = draw(st.booleans())
    return operation, first_end, commit_at, expires_before_door


@SETTINGS
@example(command=("new", 25, 40, False))
@example(command=("new", 25, 55, False))
@example(command=("new", 25, 40, True))
@example(command=("replacement", 25, 40, True))
@example(command=("release", 25, 40, False))
@given(command=midpass_hold_commits())
def test_t6_time_between_holds_counts_once_when_a_hold_commits_midpass(command):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from tests.fake.test_task_hold_clocks import events, fails_exactly_at, stuck

    operation, old_end, commit_at, expires_before_door = command
    with fresh_daemon() as (service, harness, clock, signals), pytest.MonkeyPatch.context() as patch:
        build = stuck(service, harness, clock, "pinned")
        clock[0] = 10
        first_end = old_end if operation == "new" else 1000
        hold(service, "build", stamp(first_end))
        copied, resume = (threading.Barrier(2, timeout=20) for _ in range(2))
        original = service._task_holds_now
        once = [False]

        def read(*args, **kwargs):
            if not once[0]:
                once[0] = True
                copied.wait()
                resume.wait()
            return original(*args, **kwargs)

        patch.setattr(service, "_task_holds_now", read)
        clock[0] = commit_at - 2
        with ThreadPoolExecutor(max_workers=1) as pool:
            passing = pool.submit(service._admit_kind, "detached")
            copied.wait()
            clock[0] = commit_at
            if operation == "release":
                release(service, "build")
                ended_at, left = commit_at, 50
            else:
                second_end = commit_at + 2 if expires_before_door else 2000
                hold(service, "build", stamp(second_end))
                # Independent T6 model: only the unheld gap old_end..commit counts.
                left = 50 - (commit_at - old_end) if operation == "new" else 50
                ended_at = second_end if expires_before_door else 100
            clock[0] = commit_at + 6
            resume.wait()
            passing.result(timeout=25)
        patch.setattr(service, "_task_holds_now", original)
        clock[0] = 100
        if operation != "release":
            release(service, "build")
        expected = ended_at + left
        row = job(service, build)
        assert (row["wait_reason"], row["next_check_at"]) == ("capacity", stamp(expected)), (
            "T6-midpass: time between holds counted twice", command, dict(row))
        endings = events(service, build, "job.task_hold_ended")
        ends = [event["ended_at"] for event in endings]
        assert ends == ([stamp(old_end), stamp(ended_at)] if operation == "new" else [stamp(ended_at)]), (
            "T6-midpass: a hold ending was applied more than once or at a copied clock", command, endings)
        # Some expiries leave a deadline before the next command at 100; revisit
        # it directly without making the model depend on when a pass notices it.
        fails_exactly_at(service, clock, build, expected)
