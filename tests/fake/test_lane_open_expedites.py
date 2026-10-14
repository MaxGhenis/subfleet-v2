"""C-6.10, C-6.16: a lane that opens is capacity that came free.

Incident, 2026-10-14 (read from the live store). The hub's lane ramp holds the
Codex lanes and releases one at a time. It released codex-1 at 02:08:48Z and held
it again at 02:09:32Z. In between, admission placed 15 jobs on codex-1, each one
whose backed-off capacity clock (C-6.10) was due, one after another, each behind
its own admission probe. The live policy's one priority caller (C-6.16) had a job
waiting for the same lanes: last looked at 02:08:38Z on a 30 s clock, it was not
looked at again until 02:09:36Z, after the lane was held. A released lease brought
every backed-off wait forward; a lane opening did not, so "priority first" held
only among the jobs whose clocks happened to be due.

The rule now (C-6.10):

- a lane that has opened since the last pass began (an operator's `lanes
  release`, `lanes release --limits`, a reset or a reset credit that released its
  closure, its closure's `until_at` passing, a lane enabled or enrolled) brings
  every backed-off capacity wait forward, so the pass's order (C-6.9, C-6.16)
  decides who gets it;
- a pass that began before the lane opened reserves no attempt on it and runs no
  probe there; and once it finds the lane open before a look, it looks at no
  further job, so the next pass, which follows at once, offers the lane in order;
- it stops so at most every other pass, so a run of openings never leaves the end
  of the order unlooked.

The example tests replay the incident for each way a lane opens, and the cases a
long pass makes possible. The property tests check `LaneGates.opened` against a
model of what "open" means, and that after a lane opens, the next pass looks at
every waiting job the lane could serve, in priority-then-FIFO order.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, note, settings
from hypothesis import strategies as st

from subfleet import capacity, protocol
from subfleet import daemon as daemon_module
from subfleet.adapters import registry
from subfleet.adapters.base import AdapterError
from subfleet.contracts import (ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, Outcome,
                                OutcomeClass, Reading, ReadingLabel)
from subfleet.daemon import Daemon, LaneGates, after, utcnow
from tests.claude_code import claude_code_active
from tests.fake.conftest import Harness
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)
from tests.fake_adapter import FakeAdapter

#: C-6.16: the policy's priority caller in these cases (canonical: stripped, lower case).
PRIORITY = "priority-caller"
#: Bounded: a plain run is 25 examples (derandomized when `CI` is set); `HYPOTHESIS_MAX_EXAMPLES` resizes it.
EXAMPLES = int(os.environ.get("HYPOTHESIS_MAX_EXAMPLES") or 25)
PROPERTY = settings(max_examples=EXAMPLES, derandomize=bool(os.environ.get("CI")), database=None, deadline=None,
                    print_blob=True, suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much,
                                                           HealthCheck.function_scoped_fixture])


def codex_lane(lane_id: str) -> Lane:
    return Lane(lane_id, "codex", f"codex:{lane_id}@example.invalid", Credential("codex", f"/fake/{lane_id}", "home"),
                f"/fake/{lane_id}", LaneOwner.V2, False)


def measured(service, lane_id: str) -> None:
    service.store.add_reading(Reading(lane_id, "account", "seven_day", .2, after(86400), ReadingLabel.PROVIDER,
                                      "fixture", utcnow()))


def hold(service, lane_id: str, hours: float = 6) -> None:
    """`lanes hold <lane> --until <+6 h>`, as the hub's ramp sends it."""
    service._hold_lane(protocol.LanesArgs(action="hold", lane_id=lane_id, until=after(hours * 3600)))


def release(service, lane_id: str) -> None:
    """`lanes release <lane>`."""
    service._hold_lane(protocol.LanesArgs(action="release", lane_id=lane_id))


def limit(service, lane_id: str, clock=ClockSource.REPORTED, scope: str = "account") -> None:
    service.store.add_closure(Closure(lane_id, scope, after(3600), ClosureReason.PROVIDER_LIMIT, clock, "fixture"))


def usage_reset(service, lane_id: str) -> None:
    """What a usage read that sees the window reset does (`timers.py`, `closure.reset`)."""
    with service.store.transaction("closure.reset") as tx:
        tx.execute("UPDATE closures SET released_at=? WHERE lane_id=? AND scope='account' AND "
                   "reason='provider-limit' AND released_at IS NULL", (utcnow(), lane_id))


def clock_passes(service, lane_id: str) -> None:
    """The closure's `until_at` passes: as if its hour had gone by."""
    with service.store.transaction("fixture.clock_passed") as tx:
        tx.execute("UPDATE closures SET until_at=? WHERE lane_id=? AND released_at IS NULL",
                   ("2026-01-01T00:00:00Z", lane_id))


def enroll(service, lane_id: str) -> None:
    service.store.put_lane(codex_lane(lane_id))
    measured(service, lane_id)


#: How a lane is shut, and how it opens: each returns the lane that opens.
OPENINGS = {
    "lanes release": (lambda s: hold(s, "codex-1"), lambda s: release(s, "codex-1") or "codex-1"),
    "lanes release --limits": (
        lambda s: limit(s, "codex-1", ClockSource.GUESSED),
        lambda s: s._release_lane_limits(protocol.LanesArgs(action="release-limits", lane_id="codex-1",
                                                            note="fixture: window reset seen")) and "codex-1"),
    "usage reset": (lambda s: limit(s, "codex-1"), lambda s: usage_reset(s, "codex-1") or "codex-1"),
    "reset credit": (lambda s: limit(s, "codex-1"),
                     lambda s: s.timers.actions._release("codex-1", utcnow(), action_id="fixture") or "codex-1"),
    "until_at passed": (lambda s: limit(s, "codex-1"), lambda s: clock_passes(s, "codex-1") or "codex-1"),
    "lane enabled": (lambda s: s.store.update_lane("codex-1", enabled=0),
                     lambda s: s.store.update_lane("codex-1", enabled=1) or "codex-1"),
    "lane enrolled": (lambda s: s.store.update_lane("codex-1", enabled=0), lambda s: enroll(s, "codex-2") or "codex-2"),
}


def submit(service, harness, name: str, **changes) -> str:
    return service.dispatch("submit", harness.submit_args(request_id=name, **changes))["job_id"]


def reservations(service) -> list[tuple[str, str]]:
    """(job, lane) of every attempt reserved, in the order reserved."""
    return [(row["job_id"], row["lane_id"]) for row in service.store.query(
        "SELECT job_id,lane_id FROM attempts ORDER BY rowid")]


def back_off(service, job_id: str, seconds: float = 20) -> None:
    """C-6.10: the job's wait has backed off: its next look is `seconds` away (20 s at the release, in the
    incident) and its record says so, as after ten looks that reached one verdict."""
    service._capacity_waits[job_id] = {**service._capacity_waits[job_id], "rechecks": 9}
    service.store.update_job(job_id, next_check_at=after(seconds))


def due(service, job_id: str) -> None:
    service.store.update_job(job_id, next_check_at=utcnow())


def ramp(service, lane_id: str, starts: int = 1) -> None:
    """The hub's ramp (`lane_ramp8.sh`): once `starts` attempts are reserved on the lane, it is held again."""
    def boundary(name, job_id, attempt_id):
        if name == "reserved" and sum(lane == lane_id for _, lane in reservations(service)) >= starts:
            hold(service, lane_id)
    service.crash_hook = boundary


def looks(service, during: dict | None = None) -> list[str]:
    """Record each look (C-6.10: it starts at the job's workspace); `during` maps a job id to what happens
    while that job is being looked at, after its look has begun."""
    seen: list[str] = []
    real = service._workspace

    def workspace(job):
        seen.append(job["job_id"])
        action = (during or {}).pop(job["job_id"], None)
        if action:
            action()
        return real(job)
    service._workspace = workspace
    return seen


@pytest.fixture
def fleet(routing_state):  # noqa: F811
    """codex-1, measured, and C-6.16's one priority caller; every other job's caller is not live (C-6.9)."""
    service, harness = routing_state
    measured(service, "codex-1")
    service.policy["admission"]["priority_callers"] = [PRIORITY]
    return service, harness


def queue(service, harness, shut, **changes) -> tuple[str, list[str]]:
    """The incident's queue: three older jobs, then the priority caller's, all waiting for the shut lane.
    After one pass, the priority job's wait has backed off and the others' clocks are due."""
    shut(service)
    older = [submit(service, harness, f"older-{n}", **changes) for n in range(3)]
    chosen = submit(service, harness, "chosen", caller_session=PRIORITY.upper(), **changes)
    service._admit()
    assert reservations(service) == []
    assert all(service.store.get_job(job)["state"] == "waiting" for job in [*older, chosen])
    back_off(service, chosen)
    for job in older:
        due(service, job)
    return chosen, older


# --- the incident, for each way a lane opens ------------------------------------------------------

@pytest.mark.parametrize("opening", sorted(OPENINGS))
def test_c6_16_a_lane_that_opens_goes_first_to_the_priority_job_whose_clock_runs(fleet, opening):
    """C-6.10, C-6.16, the 2026-10-14 incident: the lane opens for one start (the ramp holds it again after
    it) and the priority caller's job, 20 s from its backed-off recheck, takes it. Before, the pass looked
    only at the jobs whose clocks were due, and the oldest of them took the lane."""
    service, harness = fleet
    shut, open_ = OPENINGS[opening]
    chosen, older = queue(service, harness, shut)
    lane = open_(service)
    ramp(service, lane)
    seen = looks(service)
    service._admit()
    assert reservations(service) == [(chosen, lane)]
    assert seen[0] == chosen                                   # looked at first, its clock 20 s out
    assert all(service.store.get_job(job)["state"] == "waiting" for job in older)


def test_c6_10_a_release_that_leaves_the_lane_shut_brings_nothing_forward(fleet):
    """C-6.10, C-9.6 (D-HOLD1): `lanes release` lifts the hold alone; a limit on the lane stays in force, so
    no lane opened and no backed-off wait is looked at early."""
    service, harness = fleet
    chosen, older = queue(service, harness, lambda s: (hold(s, "codex-1"), limit(s, "codex-1")))
    for job in older:
        service.store.update_job(job, next_check_at=after(600))
    release(service, "codex-1")
    seen = looks(service)
    service._admit()
    assert seen == [] and reservations(service) == []


# --- a pass that began before the lane opened -----------------------------------------------------

def test_c6_10_a_lane_that_opens_during_a_look_goes_to_no_job_of_that_pass(fleet):
    """C-6.10: the release lands while the pass looks at the oldest due job (preparing its workspace). Its
    route then finds codex-1 open, and before, it took the lane ahead of the priority job the pass had
    passed over on its clock. Now its reservation is refused (`lane-opened`), it keeps its place, the pass
    looks at no further job, and the next pass gives the lane to the priority job first."""
    service, harness = fleet
    chosen, older = queue(service, harness, lambda s: hold(s, "codex-1"))
    last_looks = {job: service._holds[job] for job in older}
    seen = looks(service, during={older[0]: lambda: release(service, "codex-1")})
    service._admit()
    assert reservations(service) == []
    assert seen == [older[0]]                                  # the pass stopped: no further look
    assert service._holds[older[0]] == {"reason": "lane-opened", "opened": ["codex-1"]}
    for job in older[1:]:
        # C-6.11: a pass that does not look at a job repeats its last hold in full.
        assert {key: value for key, value in service._holds[job].items() if key != "next_check_at"} == \
            {key: value for key, value in last_looks[job].items() if key != "next_check_at"}
    assert service.store.get_job(older[0])["state"] == "waiting"
    ramp(service, "codex-1")
    seen.clear()
    service._admit()
    assert reservations(service) == [(chosen, "codex-1")]
    assert seen[0] == chosen


def test_c6_10_a_lane_that_opens_between_looks_stops_the_pass_before_its_next_look(fleet):
    """C-6.10: the release lands after one look has placed its job elsewhere (a trivial job, first of the
    background jobs, on codex-2, open all along) and before the next look begins. The pass looks at no
    further job, so no later job takes codex-1 ahead of the priority job, which the next pass places
    first."""
    service, harness = fleet
    service.store.put_lane(codex_lane("codex-2"))
    measured(service, "codex-2")
    chosen, older = queue(service, harness, lambda s: hold(s, "codex-1"), pinned_lane="codex-1")
    elsewhere = submit(service, harness, "elsewhere", tier="trivial", pinned_lane="codex-2")

    def boundary(name, job_id, attempt_id):
        if name == "reserved" and job_id == elsewhere:
            release(service, "codex-1")                       # after that job's reservation, mid-pass
    service.crash_hook = boundary
    seen = looks(service)
    service._admit()
    assert reservations(service) == [(elsewhere, "codex-2")]
    assert seen == [elsewhere]                                 # the older jobs were due, and not looked at
    assert all(service.store.get_job(job)["state"] == "waiting" for job in [chosen, *older])
    ramp(service, "codex-1")
    seen.clear()
    service._admit()
    assert reservations(service)[1:] == [(chosen, "codex-1")]
    assert seen[0] == chosen


def test_c6_10_no_probe_runs_on_a_lane_that_opened_after_the_pass_began(fleet):
    """C-6.10, C-11.4: in the incident every job placed on codex-1 first carried an admission probe there
    (about 6 s each). A job whose look began before the release does not probe the lane: the probe would
    hold the lane from every job, and the pass, for up to a minute. The next pass's first probe is the
    priority job's."""
    service, harness = fleet
    chosen, older = queue(service, harness, lambda s: hold(s, "codex-1"))
    probed: list[str] = []
    service._needs_probe = lambda decision, job: bool(decision.chosen_lane)

    def execute_probe(job, lane, model, holder):
        probed.append(job["job_id"])
        return Outcome(OutcomeClass.OK, "fixture probe")
    service._execute_probe = execute_probe
    service._identity_binds = lambda outcome: True
    seen = looks(service, during={older[0]: lambda: release(service, "codex-1")})
    service._admit()
    assert probed == [] and reservations(service) == []
    assert seen == [older[0]]
    ramp(service, "codex-1")
    service._admit()
    assert probed[:1] == [chosen]
    assert reservations(service) == [(chosen, "codex-1")]


def test_c6_10_a_pass_after_one_that_stopped_runs_to_its_end_and_still_places_nobody_on_a_later_lane(fleet):
    """C-6.10 (liveness): a pass stops for an opening at most every other pass, so a run of openings each
    sooner than a pass takes never leaves the end of the order unlooked. The pass after a stopped one looks
    at every job due, even after another lane opens during it, and still gives that lane to none of them:
    the pass after it does, oldest first."""
    service, harness = fleet
    service.store.put_lane(codex_lane("codex-2"))
    measured(service, "codex-2")
    hold(service, "codex-2")
    chosen, older = queue(service, harness, lambda s: hold(s, "codex-1"))
    seen = looks(service, during={older[0]: lambda: release(service, "codex-1")})
    service._admit()                                           # stops at older[0]
    assert seen == [older[0]] and service._pass_stopped["detached"]
    ramp(service, "codex-1")
    seen.clear()
    looks_during = {older[1]: lambda: release(service, "codex-2")}
    seen = looks(service, during=looks_during)
    service._admit()                                           # does not stop
    assert reservations(service) == [(chosen, "codex-1")]
    assert seen == [chosen, *older]                            # every due job looked at
    assert service._holds[older[1]] == {"reason": "lane-opened", "opened": ["codex-2"]}
    assert service._holds[older[2]] == {"reason": "lane-opened", "opened": ["codex-2"]}
    assert not service._pass_stopped["detached"]
    seen.clear()
    service._admit()
    assert reservations(service)[1:] == [(job, "codex-2") for job in older]
    assert seen == older


def test_c6_11_why_says_a_lane_opened(fleet):
    """C-6.11: `why` names the lane and says the job keeps its place for the next pass."""
    service, harness = fleet
    chosen, older = queue(service, harness, lambda s: hold(s, "codex-1"))
    looks(service, during={older[0]: lambda: release(service, "codex-1")})
    service._admit()
    text = service.dispatch("why", {"job_id": older[0]})["text"]
    assert "codex-1 opened after the last admission pass began" in text
    assert "keeps its place" in text


# --- LaneGates.opened ------------------------------------------------------------------------------

def gates(closed=(), usable=("a",)) -> LaneGates:
    return LaneGates(frozenset(closed), frozenset(usable))


@pytest.mark.parametrize("before,now,opened", [
    (gates([("a", "account")]), gates(), {"a"}),                                          # a closure ended
    (gates([("a", "m1")]), gates(), {"a"}),                                               # one model's ended
    (gates([("a", "account"), ("a", "m1")]), gates([("a", "account")]), set()),          # still shut
    (gates([("a", "account")]), gates([("a", "account")]), set()),                       # a hold, a limit left
    (gates(usable=()), gates(), {"a"}),                                                   # enabled or enrolled
    (gates(usable=()), gates([("a", "account")]), set()),                                 # enabled, still closed
    (gates([("a", "m1")]), gates([("a", "m2")]), {"a"}),                                  # m1 opened, m2 shut
    (gates([("a", "account")]), gates(usable=()), set()),                                 # ended, but disabled
    (gates(), gates([("a", "m1")]), set()),                                               # it closed
])
def test_c6_10_lane_gates_opened(before, now, opened):
    assert before.opened(now) == opened


LANES = ("a", "b")
SCOPES = ("account", "m1", "m2")
GATES = st.builds(lambda closed, usable: LaneGates(frozenset(closed), frozenset(usable)),
                  st.sets(st.tuples(st.sampled_from(LANES), st.sampled_from(SCOPES))), st.sets(st.sampled_from(LANES)))


@PROPERTY
@given(before=GATES, now=GATES, only=st.none() | st.sampled_from(LANES))
def test_c6_10_lane_gates_opened_is_open_for_some_model_now_and_not_before(before, now, only):
    """C-6.10, differential: a lane has opened when some model (one of those named, or one no closure
    names) can run on it now and could not before: the lane usable, and neither the account nor that
    model under a closure. Gates read for one lane answer for that lane alone."""
    def runs(state: LaneGates, lane: str, model: str) -> bool:
        return (lane in state.usable and (lane, "account") not in state.closed
                and (lane, model) not in state.closed)
    if only is not None:
        now = LaneGates(frozenset(gate for gate in now.closed if gate[0] == only), now.usable & {only})
    models = ("m1", "m2", "unnamed")
    expected = {lane for lane in LANES if (only is None or lane == only)
                and any(runs(now, lane, model) and not runs(before, lane, model) for model in models)}
    assert before.opened(now) == expected


# --- after a lane opens, the next pass looks at every waiting job it could serve, in order ---------

JOB = st.fixed_dictionaries({
    "model": st.sampled_from(("astra", "terra", "opus")),          # opus: no lane here serves it
    "tier": st.sampled_from((None, "trivial", "easy", "standard", "hard")),
    "priority": st.booleans(),
    "pin": st.sampled_from((None, None, "codex-1", "codex-2")),
    "backed_off": st.booleans(),
})
SHUT = {
    "hold": (lambda s: hold(s, "codex-1"), lambda s: release(s, "codex-1")),
    "limit, reset": (lambda s: limit(s, "codex-1"), lambda s: usage_reset(s, "codex-1")),
    "limit, reset credit": (lambda s: limit(s, "codex-1"),
                            lambda s: s.timers.actions._release("codex-1", utcnow(), action_id="fixture")),
    "limit, clock": (lambda s: limit(s, "codex-1"), lambda s: clock_passes(s, "codex-1")),
    "model limit, clock": (lambda s: limit(s, "codex-1", scope="gpt-6-astra"), lambda s: clock_passes(s, "codex-1")),
    "disabled": (lambda s: s.store.update_lane("codex-1", enabled=0),
                 lambda s: s.store.update_lane("codex-1", enabled=1)),
}


class Example:
    """One daemon on its own state root: fake providers, no process, a fixture session registry."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-lane-open-"))
        self.patch = pytest.MonkeyPatch()
        root = self.tmp / "state"
        root.mkdir()
        self.harness = Harness(root)
        self.patch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
        self.patch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
        self.patch.setattr(capacity, "read_desktop_account", lambda path=None: None)
        self.patch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
        claude_code_active(self.patch, self.tmp / "claude")
        self.service = Daemon(root)
        self.service._launch = lambda *args: pytest.fail("nothing here launches a guardian")
        self.service._execute_probe = lambda job, lane, model, holder: Outcome(OutcomeClass.OK, "fixture probe")
        self.service._identity_binds = lambda outcome: True

    def close(self):
        try:
            self.service.close()
        finally:
            self.patch.undo()
            shutil.rmtree(self.tmp, ignore_errors=True)


def serves(job: dict, lane: str, models=("astra", "terra")) -> bool:
    """C-11.2: whether `lane` (a Codex lane) could run the job once it opens: one of the `models` it opens
    for, and no pin to another lane."""
    return job["pinned_model"] in models and job["pinned_lane"] in (None, lane)


@PROPERTY
@given(jobs=st.lists(JOB, min_size=1, max_size=6), shut=st.sampled_from(sorted(SHUT)),
       second=st.sampled_from(("none", "held", "open")), during=st.none() | st.integers(0, 5))
def test_c6_10_after_a_lane_opens_the_next_pass_looks_at_every_waiting_job_it_serves_in_order(
        jobs, shut, second, during):
    """C-6.10, C-6.9, C-6.16: a lane shut one way or another opens, between two passes or during a look
    of a pass. Then:

    - no pass that began before it opened reserves an attempt on it;
    - a pass that finds it open before a look looks at no further job;
    - the first pass that begins after it opened looks at every job left waiting that the lane could
      serve, whether its clock was due or backed off, and its looks are in priority-then-FIFO order:
      priority callers' jobs first, oldest first whatever their tier, then the rest by tier, then age."""
    example = Example()
    try:
        service, harness = example.service, example.harness
        service.policy["admission"]["priority_callers"] = [PRIORITY]
        measured(service, "codex-1")
        if second != "none":
            service.store.put_lane(codex_lane("codex-2"))
            measured(service, "codex-2")
            if second == "held":
                hold(service, "codex-2")
        close_lane, open_lane = SHUT[shut]
        # A closure of one model's scope shuts codex-1 for that model alone: it is open for Terra throughout.
        opens_for = ("astra",) if shut == "model limit, clock" else ("astra", "terra")
        close_lane(service)
        order: dict[str, int] = {}
        for n, spec in enumerate(jobs):
            try:
                job_id = submit(service, harness, f"job-{n}", pinned_model=spec["model"], tier=spec["tier"],
                                pinned_lane=spec["pin"], caller_session=PRIORITY if spec["priority"] else "other")
            except (protocol.ProtocolError, AdapterError) as exc:
                note(f"job {n} {spec} refused at submit: {exc}")
                continue
            order[job_id] = n
        note(f"shut={shut} second={second} during={during} submitted={list(order)}")
        service._admit()                                         # every job's first look
        rows = {row["job_id"]: row for row in service.store.query("SELECT * FROM jobs")}
        for job_id in order:
            if rows[job_id]["state"] == "waiting" and rows[job_id]["wait_reason"] == "capacity":
                if jobs[order[job_id]]["backed_off"] and job_id in service._capacity_waits:
                    service.store.update_job(job_id, next_check_at=after(600))
                else:
                    due(service, job_id)
        before = len(reservations(service))
        opened_at: list[int] = []
        seen = looks(service)

        def opens():
            opened_at.append(len(seen))
            open_lane(service)
        if during is not None:
            # During the look of the `during`-th job this pass looks at, if it looks at that many.
            real = service._workspace

            def workspace(job):
                result = real(job)
                if not opened_at and len(seen) == during + 1:
                    opens()
                return result
            service._workspace = workspace
        service._admit()                                         # the pass that began before the lane opened
        first = reservations(service)[before:]
        rows = {row["job_id"]: row for row in service.store.query("SELECT * FROM jobs")}
        shut_out = [(job, lane) for job, lane in first if lane == "codex-1" and serves(rows[job], lane, opens_for)]
        assert not shut_out, f"C-6.10: a pass that began before codex-1 opened placed {shut_out} on it"
        if opened_at:
            assert seen == seen[:opened_at[0]], \
                f"C-6.10: after codex-1 opened during the look of {seen[opened_at[0] - 1]}, the pass looked at {seen[opened_at[0]:]}"
        else:
            opens()
        rows = {row["job_id"]: row for row in service.store.query("SELECT * FROM jobs")}
        waiting = [job_id for job_id in order if rows[job_id]["state"] in ("queued", "waiting")]
        seen.clear()
        service._admit()                                         # the first pass that began after it opened
        # C-6.10's one exception: the wait after an inconclusive probe keeps its own clock.
        probe_waits = {job_id for job_id, wait in service._capacity_waits.items() if not wait["expedite"]}
        missed = [job_id for job_id in waiting if serves(rows[job_id], "codex-1", opens_for)
                  and job_id not in seen and job_id not in probe_waits]
        assert not missed, (f"C-6.10: the first pass after codex-1 opened did not look at {missed} "
                            f"({[(job, rows[job]['next_check_at'], service._holds.get(job)) for job in missed]})")
        tiers = service.policy["tiers"]

        def rank(job_id: str) -> tuple:
            row = rows[job_id]
            if row["caller_session"] == PRIORITY:
                return (0, 0, order[job_id])                     # C-6.16: FIFO across tiers
            return (1, tiers.index(row["tier"] or "standard"), order[job_id])
        assert seen == sorted(seen, key=rank), f"C-6.9, C-6.16: looks out of order: {seen}"
        assert not [job for job, hold_ in service._holds.items() if hold_.get("reason") == "lane-opened"]
    finally:
        example.close()
