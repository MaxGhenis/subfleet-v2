"""C-11.10 (D-RO-ADMIT1): sandbox holds compose with task holds at admission.

The generated model is independent of the hold reader: it keeps end times in
virtual seconds and computes membership from the submitted job's stored task,
sandbox and kind. Real daemon reservation transactions create the attempts.
Only workspace preparation is stubbed, to give each job a harmless independent
fixture directory; no Git worktree, guardian or provider is started.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from hypothesis import example, given, settings, strategies as st

from subfleet import protocol, task_holds
from tests.disk_floor_model import stamp
from tests.fake.test_admission_latency import measure
from tests.task_hold_support import attempts, finish, fresh_daemon, hold, job, submit, turn


SETTINGS = settings(max_examples=25, deadline=None, database=None, derandomize=True)
TASKS = ("build", "review", "sweep", None)
SANDBOXES = ("read-only", "workspace-write", "")
KINDS = ("dispatch", "turn")
JOB = st.tuples(st.sampled_from(TASKS), st.sampled_from(SANDBOXES), st.sampled_from(KINDS))
JOBS = st.lists(JOB, min_size=1, max_size=8)
ENDS = st.integers(min_value=1, max_value=120)
TASK_HOLDS = st.dictionaries(st.sampled_from(TASKS[:-1]), ENDS, max_size=3)
SANDBOX_HOLD = st.one_of(st.none(), ENDS)
CAPS = st.sampled_from([
    {"max_active_attempts": 1, "max_in_flight_per_lane": 1},
    {"max_active_attempts": 2, "max_in_flight_per_lane": 1},
    {"max_active_attempts": 4, "max_in_flight_per_lane": 2},
])
ROOM = {"max_active_attempts": 20, "max_in_flight_per_lane": 20, "max_in_flight_unmeasured": 20}


def sandbox_hold(service, end: int, note: str = "disk brake", **extra) -> dict:
    return service.dispatch("tasks", {"action": "hold", "sandbox": "workspace-write",
                                      "until": stamp(end), "note": note, **extra})


def sandbox_release(service, **extra) -> dict:
    return service.dispatch("tasks", {"action": "release", "sandbox": "workspace-write", **extra})


def listing(service) -> list[dict]:
    return service.dispatch("tasks", {"action": "list"})["task_holds"]


def model_end(spec, task_ends, sandbox_end, now):
    """Latest active end, with the turn/no-task exception and conservative empty sandbox."""
    task, sandbox, kind = spec
    if kind == "turn" or not task:
        return None
    ends = [task_ends.get(task, 0)]
    if (sandbox or "workspace-write") == "workspace-write":
        ends.append(sandbox_end or 0)
    return max((end for end in ends if end > now), default=None)


def sandbox_covers(spec, sandbox_end, now):
    task, sandbox, kind = spec
    return bool(task and kind != "turn" and (sandbox or "workspace-write") == "workspace-write"
                and sandbox_end is not None and sandbox_end > now)


@contextmanager
def fleet(caps=None):
    with fresh_daemon(caps=caps or ROOM) as (service, harness, clock, signals):
        # Writable jobs require measured capacity (C-11.4), unlike the existing
        # read-only task-hold fixture. Keep that capacity fresh for every clock
        # in these tests, so no admission probe is needed or started.
        service.policy["caps"]["reading_ttl_s"] = 3600
        for lane in service.store.list_lanes():
            measure(service, lane.lane_id)

        def refuse_probe_guardian(*args, **kwargs):
            raise AssertionError("sandbox-hold fixtures must never spawn a probe guardian")

        service._spawn_guardian = refuse_probe_guardian
        # All admission decisions, capacity accounting, leases and attempts are
        # real. Workspace preparation alone returns each job's fixture directory.
        service._workspace = lambda row: (row["workdir"], None, None, [])
        # The current schema rejects empty sandboxes. Generated legacy rows
        # still reach the real hold readers and reservation checks unchanged;
        # only this fixture's inert launch spec supplies an enum for them.
        # Production launch semantics are deliberately unchanged.
        spec = service._spec
        service._spec = lambda row, **overrides: spec(
            {**row, "sandbox": row["sandbox"] or "workspace-write"}, **overrides)
        yield service, harness, clock, signals


def jobs_from(service, harness, specs):
    """Submit normally, then model persisted legacy values (including empty sandbox).

    Writable admission gets a separate directory for each job, so directory
    exclusivity cannot accidentally create or conceal a capacity difference.
    """
    jobs = {}
    for index, spec in enumerate(specs):
        if spec is None:
            continue
        task, sandbox, kind = spec
        name = f"generated-{index}"
        ident = (turn(service, harness, index + 1, task=task) if kind == "turn"
                 else submit(service, harness, task, name))
        workdir = harness.root / f"work-{index}"
        workdir.mkdir()
        with service.store.transaction("test.sandbox_job", job_id=ident) as tx:
            # Today's schema CHECK accepts only the two named sandboxes. The
            # brief also requires conservative handling of a legacy empty row;
            # model that old input without weakening the production schema.
            if sandbox == "":
                tx.execute("PRAGMA ignore_check_constraints=ON")
            try:
                tx.execute("UPDATE jobs SET sandbox=?,workdir=? WHERE job_id=?", (sandbox, str(workdir), ident))
            finally:
                if sandbox == "":
                    tx.execute("PRAGMA ignore_check_constraints=OFF")
        jobs[name] = ident
    return jobs


def install_holds(service, task_ends, sandbox_end):
    for task, end in task_ends.items():
        hold(service, task, stamp(end), note=f"task brake {task}")
    if sandbox_end is not None:
        sandbox_hold(service, sandbox_end)


def held_for(service, ident, now):
    with service.store.transaction("test.read_hold") as tx:
        return task_holds.job_held(tx, ident, stamp(now))


def placement_image(service, jobs):
    names = {ident: name for name, ident in jobs.items()}
    return [(names[row["job_id"]], row["seq"], row["lane_id"], row["model_requested"])
            for row in attempts(service)]


@SETTINGS
@given(specs=JOBS, task_ends=TASK_HOLDS, sandbox_end=SANDBOX_HOLD,
       now=st.integers(min_value=0, max_value=121), caps=CAPS)
@example(specs=[("review", "workspace-write", "dispatch"), ("review", "", "dispatch"),
                ("build", "read-only", "dispatch"), ("build", "workspace-write", "turn"),
                (None, "workspace-write", "dispatch")],
         task_ends={}, sandbox_end=100, now=0,
         caps={"max_active_attempts": 4, "max_in_flight_per_lane": 2})
def test_s1_held_jobs_never_create_an_attempt(specs, task_ends, sandbox_end, now, caps):
    """Invariant 1: held means unplaced, across generated tasks, rows, turns and holds."""
    with fleet(caps) as (service, harness, clock, signals):
        jobs = jobs_from(service, harness, specs)
        install_holds(service, task_ends, sandbox_end)
        clock[0] = now
        service._admit()
        for index, spec in enumerate(specs):
            ident = jobs[f"generated-{index}"]
            if model_end(spec, task_ends, sandbox_end, now) is not None:
                assert attempts(service, ident) == [], "S1: a held job created an attempt"
                assert job(service, ident)["wait_reason"] == "task-held"


@SETTINGS
@given(specs=JOBS, task_ends=TASK_HOLDS, sandbox_end=ENDS,
       now=st.integers(min_value=0, max_value=121), caps=CAPS)
@example(specs=[("review", "workspace-write", "dispatch"), ("build", "read-only", "dispatch"),
                ("sweep", "read-only", "dispatch"), ("build", "workspace-write", "turn")],
         task_ends={"sweep": 80}, sandbox_end=100, now=0,
         caps={"max_active_attempts": 1, "max_in_flight_per_lane": 1})
def test_s2_unheld_placements_match_without_the_sandbox_hold(specs, task_ends, sandbox_end, now, caps):
    """Invariant 2: remove covered demand in the no-sandbox-hold reference run.

    The same unheld population sees identical capacity, task holds and submission
    order. Keeping covered demand in the reference would spend capacity that the
    sandbox hold intentionally reserves for the rest of the queue.
    """
    baseline = [None if sandbox_covers(spec, sandbox_end, now) else spec for spec in specs]
    with fleet(caps) as (service, harness, clock, signals):
        jobs = jobs_from(service, harness, specs)
        install_holds(service, task_ends, sandbox_end)
        clock[0] = now
        service._admit()
        held_image = placement_image(service, jobs)
        unheld_states = {name: tuple(job(service, ident)[key] for key in
                                    ("state", "wait_reason", "next_check_at"))
                         for name, ident in jobs.items() if baseline[int(name.split("-")[-1])] is not None}
    with fleet(caps) as (service, harness, clock, signals):
        jobs = jobs_from(service, harness, baseline)
        install_holds(service, task_ends, None)
        clock[0] = now
        service._admit()
        assert placement_image(service, jobs) == held_image, "S2: unheld placements changed"
        assert {name: tuple(job(service, ident)[key] for key in
                            ("state", "wait_reason", "next_check_at"))
                for name, ident in jobs.items()} == unheld_states, "S2: unheld waits changed"


@SETTINGS
@given(specs=JOBS, task_ends=TASK_HOLDS, sandbox_end=ENDS,
       delta=st.sampled_from((-1, 0, 1)))
def test_s4_hold_membership_obeys_the_clock(specs, task_ends, sandbox_end, delta):
    """Invariant 4: the instant before, at and after an end is decided by the clock."""
    now = sandbox_end + delta
    with fleet() as (service, harness, clock, signals):
        jobs = jobs_from(service, harness, specs)
        install_holds(service, task_ends, sandbox_end)
        clock[0] = now
        for index, spec in enumerate(specs):
            current = held_for(service, jobs[f"generated-{index}"], now)
            end = model_end(spec, task_ends, sandbox_end, now)
            assert (None if current is None else current["until"]) == (None if end is None else stamp(end)), \
                "S4: hold membership disagrees with its end"
        service._admit()
        for index, spec in enumerate(specs):
            placed = attempts(service, jobs[f"generated-{index}"])
            assert bool(placed) == (model_end(spec, task_ends, sandbox_end, now) is None), \
                "S4: an ended hold changed admission"


@SETTINGS
@given(specs=JOBS, task_ends=TASK_HOLDS, sandbox_end=SANDBOX_HOLD,
       now=st.integers(min_value=0, max_value=121))
@example(specs=[("review", "workspace-write", "dispatch"), ("build", "read-only", "dispatch"),
                ("review", "", "dispatch"), ("review", "workspace-write", "turn"),
                (None, "workspace-write", "dispatch")],
         task_ends={"review": 120, "build": 80}, sandbox_end=100, now=90)
def test_s6_task_and_sandbox_holds_compose_to_the_later_end(specs, task_ends, sandbox_end, now):
    """Invariant 6: the disjunction of active task/sandbox holds uses its latest end."""
    with fleet() as (service, harness, clock, signals):
        jobs = jobs_from(service, harness, specs)
        install_holds(service, task_ends, sandbox_end)
        clock[0] = now
        for index, spec in enumerate(specs):
            current = held_for(service, jobs[f"generated-{index}"], now)
            end = model_end(spec, task_ends, sandbox_end, now)
            assert (current is not None) == (end is not None), "S6: hold composition is not a disjunction"
            if current is not None:
                assert current["until"] == stamp(end), "S6: composition did not use the later end"
        service._admit()
        for index, spec in enumerate(specs):
            ident = jobs[f"generated-{index}"]
            end = model_end(spec, task_ends, sandbox_end, now)
            assert bool(attempts(service, ident)) == (end is None), "S6: composed admission changed"
            if end is not None:
                assert job(service, ident)["next_check_at"] == stamp(end)


def test_sandbox_hold_reads_the_job_row_instead_of_the_task_default():
    """Mutant 1: explicit read-only builds run; explicit writable reviews wait."""
    with fleet() as (service, harness, clock, signals):
        specs = [("build", "read-only", "dispatch"), ("review", "workspace-write", "dispatch")]
        jobs = jobs_from(service, harness, specs)
        sandbox_hold(service, 100)
        service._admit()
        assert len(attempts(service, jobs["generated-0"])) == 1, "row sandbox: explicit read-only build held"
        assert attempts(service, jobs["generated-1"]) == [], "row sandbox: writable review placed"


def test_empty_sandbox_is_conservatively_held():
    """Mutant 2: legacy empty sandboxes are workspace-write at this door."""
    with fleet() as (service, harness, clock, signals):
        jobs = jobs_from(service, harness, [("review", "", "dispatch")])
        sandbox_hold(service, 100)
        service._admit()
        assert attempts(service, jobs["generated-0"]) == [], "empty sandbox: job escaped the hold"


def test_s5_turns_and_jobs_without_a_task_are_never_held():
    """Invariant 5 / mutant 3: neither hold can stop turns or jobs without tasks."""
    with fleet() as (service, harness, clock, signals):
        specs = [("build", "workspace-write", "turn"), ("review", "", "turn"),
                 (None, "workspace-write", "dispatch"), (None, "", "dispatch")]
        jobs = jobs_from(service, harness, specs)
        install_holds(service, {"build": 100, "review": 100}, 100)
        service._admit()
        assert all(len(attempts(service, ident)) == 1 for ident in jobs.values()), "S5: exempt job was held"


def test_expired_sandbox_hold_stops_nothing_without_being_reaped():
    """Mutant 4: the row remains, while the clock ends its effect."""
    with fleet() as (service, harness, clock, signals):
        jobs = jobs_from(service, harness, [("review", "workspace-write", "dispatch")])
        sandbox_hold(service, 100)
        service._admit()
        assert attempts(service, jobs["generated-0"]) == []
        clock[0] = 100
        service._admit()
        assert len(attempts(service, jobs["generated-0"])) == 1, "expired sandbox: job still held"
        assert service.store.one("SELECT 1 FROM leases WHERE lease_key='sandbox-hold:workspace-write'")


def test_s3_running_attempt_job_and_lane_are_unchanged():
    """Invariant 3: hold, replacement and release change no running state or lane."""
    with fleet() as (service, harness, clock, signals):
        jobs = jobs_from(service, harness, [("review", "workspace-write", "dispatch")])
        ident = jobs["generated-0"]
        service._admit()
        assert len(attempts(service, ident)) == 1, "S3: the fixture failed to create a running attempt"
        with service.store.transaction("test.running") as tx:
            tx.execute("UPDATE attempts SET state='running',started_at=? WHERE job_id=?", (stamp(0), ident))
        before_attempts, before_job = attempts(service, ident), job(service, ident)
        before_lanes = service.store.query("SELECT * FROM lanes ORDER BY lane_id")
        sandbox_hold(service, 100)
        clock[0] = 10
        sandbox_hold(service, 200, note="replacement brake")
        service._admit()
        sandbox_release(service)
        service._admit()
        assert attempts(service, ident) == before_attempts, "S3: running attempt changed"
        assert job(service, ident) == before_job, "S3: running job changed"
        assert service.store.query("SELECT * FROM lanes ORDER BY lane_id") == before_lanes, "S3: lane changed"
        assert signals == [], "S3: running process was signalled"


def test_s7_hold_list_replace_release_round_trip():
    """Invariant 7: replacement keeps acquired_at while in force; release removes only its row."""
    with fleet() as (service, harness, clock, signals):
        sandbox_hold(service, 100, note="first brake")
        assert listing(service) == [{"sandbox": "workspace-write", "until": stamp(100),
                                     "note": "first brake", "since": stamp(0)}]
        clock[0] = 10
        sandbox_hold(service, 200, note="second brake")
        assert listing(service) == [{"sandbox": "workspace-write", "until": stamp(200),
                                     "note": "second brake", "since": stamp(0)}]
        row = service.store.one("SELECT * FROM leases WHERE lease_key='sandbox-hold:workspace-write'")
        assert (row["acquired_at"], row["expires_at"], row["holder"]) == \
            (stamp(0), stamp(200), "operator: second brake")
        sandbox_release(service)
        assert listing(service) == []
        sandbox_hold(service, 100, note="expired interval")
        clock[0] = 100
        sandbox_hold(service, 200, note="new interval")
        assert listing(service)[0]["since"] == stamp(100)


def test_sandbox_release_keeps_an_independent_task_hold():
    """Mutant 5: releasing the sandbox hold cannot delete or weaken a task hold."""
    with fleet() as (service, harness, clock, signals):
        jobs = jobs_from(service, harness, [("review", "workspace-write", "dispatch")])
        hold(service, "review", stamp(200), note="keep the review task")
        sandbox_hold(service, 100)
        task_before = service.store.one(task_holds.ROW_SQL, (task_holds.lease_key("review"),))
        sandbox_release(service)
        assert service.store.one(task_holds.ROW_SQL, (task_holds.lease_key("review"),)) == task_before, \
            "sandbox release: task hold was changed"
        service._admit()
        assert attempts(service, jobs["generated-0"]) == [], "sandbox release: task-held job escaped"
        assert job(service, jobs["generated-0"])["next_check_at"] == stamp(200)


def test_a_sandbox_hold_committed_after_the_pass_read_stops_reservation():
    """The final reserving transaction reads the sandbox row on its own clock."""
    with fleet() as (service, harness, clock, signals):
        jobs = jobs_from(service, harness, [("review", "workspace-write", "dispatch")])
        ident = jobs["generated-0"]
        route, landed = service._prepare_route, []

        def hold_then_route(row, *args, **kwargs):
            if row["job_id"] == ident and not landed:
                epoch = service._task_hold_epoch
                landed.append(sandbox_hold(service, 100))
                service._task_hold_epoch = epoch
            return route(row, *args, **kwargs)

        service._prepare_route = hold_then_route
        service._admit()
        assert landed and attempts(service, ident) == [], "S1: reservation ignored a newly committed sandbox hold"
        assert job(service, ident)["next_check_at"] == stamp(100)


@pytest.mark.parametrize("released", ("task", "sandbox"))
def test_releasing_either_hold_keeps_the_other_in_force(released):
    with fleet() as (service, harness, clock, signals):
        jobs = jobs_from(service, harness, [("review", "workspace-write", "dispatch")])
        ident = jobs["generated-0"]
        install_holds(service, {"review": 200}, 100)
        service._admit()
        assert job(service, ident)["next_check_at"] == stamp(200)
        if released == "task":
            service.dispatch("tasks", {"action": "release", "task": "review"})
            remaining = 100
        else:
            sandbox_release(service)
            remaining = 200
        assert attempts(service, ident) == []
        service._admit()
        assert job(service, ident)["next_check_at"] == stamp(remaining)
        clock[0] = remaining
        service._admit()
        assert len(attempts(service, ident)) == 1


def test_sandbox_hold_pauses_and_restores_the_existing_pin_episode():
    """C-11.8's episode and the task-held events are shared with task holds."""
    with fleet() as (service, harness, clock, signals):
        service.policy["admission"]["pin_grace_s"] = 60
        service.store.update_lane("codex-1", enabled=0)
        ident = submit(service, harness, "review", "pinned-review", pinned_lane="codex-1")
        with service.store.transaction("test.writable_pin", job_id=ident) as tx:
            tx.execute("UPDATE jobs SET sandbox='workspace-write' WHERE job_id=?", (ident,))
        service._admit()
        clock[0] = 10
        sandbox_hold(service, 1000)
        clock[0] = 500
        sandbox_release(service)
        service._admit()
        assert service._pin_episodes[ident]["fail_at"] == stamp(550), "sandbox hold spent the paused pin grace"
        events = service.store.query("SELECT kind FROM events WHERE job_id=? ORDER BY event_id", (ident,))
        assert "job.task_held" in [event["kind"] for event in events]
        assert "job.task_hold_ended" in [event["kind"] for event in events]


def test_sandbox_hold_survives_lease_reapers_and_is_outside_task_range():
    """Mutant 6: attempt/job/probe/retention/resume cleanup preserves the operator row."""
    with fleet() as (service, harness, clock, signals):
        sandbox_hold(service, 100)
        review = submit(service, harness, "review", "reaper-review")
        service._admit()
        [attempt] = attempts(service, review)
        finish(service, attempt)
        for holder in (attempt["attempt_id"], review, "probe:timer:x", "retention:x", "resume:x"):
            service.store.release_leases(holder)
        service._recover_probes()
        assert listing(service) == [{"sandbox": "workspace-write", "until": stamp(100),
                                     "note": "disk brake", "since": stamp(0)}], "reaper deleted sandbox hold"
        low, high = task_holds.ROWS_PARAMS
        assert not low <= "sandbox-hold:workspace-write" < high, "sandbox key entered the task-hold range"


def test_a_task_named_like_the_sandbox_key_cannot_replace_the_sandbox_hold():
    with fleet() as (service, harness, clock, signals):
        task = "sandbox-hold:workspace-write"
        with service.store.transaction("test.colliding_task_name") as tx:
            tx.execute("INSERT INTO leases(lease_key,holder,acquired_at,expires_at) VALUES(?,?,?,?)",
                       (task_holds.lease_key(task), "operator: task brake", stamp(0), stamp(120)))
        sandbox_hold(service, 100)
        rows = service.store.query("SELECT lease_key,holder,acquired_at,expires_at FROM leases ORDER BY lease_key")
        active = task_holds.in_force(rows, stamp(0))
        assert len(active) == 2, "task and sandbox hold index keys collided"
        assert len(task_holds.listing(active)) == 2
        assert task_holds.hold_for({"kind": "dispatch", "task": task, "sandbox": "read-only"}, active)["until"] \
            == stamp(120)
        assert task_holds.hold_for({"kind": "dispatch", "task": "review", "sandbox": "workspace-write"},
                                   active)["until"] == stamp(100)
        assert task_holds.hold_for({"kind": "dispatch", "task": "review", "sandbox": "read-only"}, active) is None


def test_sandbox_conditional_holds_use_the_same_note_ownership_rule():
    with fleet() as (service, harness, clock, signals):
        sandbox_hold(service, 100, note="disk: first", if_note_prefix="disk:")
        sandbox_hold(service, 200, note="disk: second", if_note_prefix="disk:")
        before = service.store.query("SELECT * FROM leases ORDER BY lease_key")
        with pytest.raises(protocol.ProtocolError) as error:
            sandbox_release(service, if_note_prefix="other:")
        assert error.value.reason == "task-hold-condition"
        assert service.store.query("SELECT * FROM leases ORDER BY lease_key") == before
        sandbox_release(service, if_note_prefix="disk:")
        assert listing(service) == []
