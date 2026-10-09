"""C-6.9, C-6.10, C-11.4: a probe's verdict is the lane's, and its vehicle keeps its turn.

Incident, 2026-10-03 (defect D-1): a `hard` review sat `waiting` on `capacity`
from 18:36Z to 19:40Z and never started, while hard reviews created after it
were admitted. Each time admission chose a Codex lane for it the lane was
unmeasured, so the job carried the lane's admission probe (C-11.4). Four times
the probe was still running at its 60 s deadline, was stopped there, and was
read as `unknown` or `transient`. Each time only that job was put on a clock,
nothing was recorded against the lane, and the pass went on: the next job in
line probed the same lanes at once and was admitted on an `ok`. Probes run
inside the one detached pass, so the job's next look came a whole pass later
(10 to 21 minutes), and it then drew one more probe with the same chance of
missing. Nothing bounded how often that could repeat.

A probe says nothing about the job that carries it: the prompt is fixed and it
runs in a private directory on the lane's credential. So the job waiting on one
is first in line for the next, and later jobs that want that probe wait for it.

The invariants, each checked below on scripted outcomes and, in the property at
the end, on generated ones:

I1 (the turn). A job carries an admission probe of a model on a lane only when
   no job ahead of it in the pass's order waits on a probe of that model and
   could run on that lane.
I2 (no passing on a probe). So among jobs that ask for the same model on the
   same lanes, starts follow the pass's order whatever the probes answer: the
   number of later jobs that start ahead of a job waiting on a probe is zero.
I3 (the clock). After an inconclusive probe its vehicle is due no later than
   `PROBE_RETRY_S` after the probe was reserved, and never before that either.
I4 (it ends). A job that no longer waits on a probe holds nobody, and a job
   held for another's turn is looked at on the first pass that finds nobody
   ahead of it: once probes answer, every job starts.
I5 (the hold is narrow). A probe of another model, or on a lane the waiting
   job is not pinned to, is nobody's to wait for, and a job that needs no probe
   is never held. A job whose chosen lane is another's turn goes to a lane of the
   same model whose probe is its own, if one would take it.
I6 (rotation, the bound). After a probe of a model on a lane says nothing, the
   job's next probe of that model goes to a lane of that model it has not had
   such an answer on this round. So every lane that would take a job is probed
   for it within one round, and a later job held behind it waits at most that
   long for the lane it could use, however often one lane's probes say nothing.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import daemon as daemon_module, procs, scheduler
from subfleet.adapters import registry
from subfleet.contracts import (PROBE_RETRY_S, ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner,
                                Outcome, OutcomeClass, Reading, ReadingLabel)
from subfleet.daemon import Daemon, after, utcnow
from tests.claude_code import claude_code_active
from tests.fake.conftest import Harness
from tests.fake.test_probe_recovery import exit_receipts, reserved_probe
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)
from tests.fake_adapter import FakeAdapter

OK = Outcome(OutcomeClass.OK, "admitted", {"rc": 0, "signal": None})
#: What the incident's probes were read as once their deadline had stopped them.
CUT = Outcome(OutcomeClass.UNKNOWN, "Codex exited without a verified deliverable",
              {"rc": 0, "signal": None, "admission": "no successful deliverable", "stopped": "deadline"})
SLOW = Outcome(OutcomeClass.TRANSIENT, "Temporary Codex transport or capacity failure",
               {"rc": 0, "signal": None, "stopped": "deadline",
                "admission": "failed to refresh available models: request timed out"})
MODELS = {"gpt-6-astra": "astra", "gpt-5.6-terra": "terra", "claude-opus-5-5": "opus"}


def codex_lane(root: Path, identity: str) -> Lane:
    home = root / ("home-" + identity)
    home.mkdir(exist_ok=True)
    return Lane(identity, "codex", "codex:" + identity, Credential("codex", str(home), "home"), str(home),
                LaneOwner.V2, False)


def submit(service, harness, **changes):
    """A `hard` job: on an unmeasured lane it needs the lane's probe first (C-11.4)."""
    return service.dispatch("submit", harness.submit_args(**{"tier": "hard", **changes}))["job_id"]


def moment(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


class Probes:
    """`Daemon._execute_probe`, scripted: each probe takes the next outcome, then `ok`.

    An outcome may come with how long its probe ran, `(outcome, seconds)`; the
    default is the whole minute, as a probe its deadline stopped runs. Nothing
    waits: the probe's reservation is moved that far into the past before it
    returns. `line` is who waited on a probe each time one was carried (I1).
    """

    def __init__(self, service, monkeypatch, outcomes=(), bad=()):
        self.service, self.outcomes, self.bad = service, list(outcomes), set(bad)
        self.vehicles: list[tuple[str, str, str]] = []
        self.line: list[list] = []
        self.reserved: dict[str, str] = {}
        monkeypatch.setattr(service, "_execute_probe", self)

    def __call__(self, job, lane, model, holder):
        # A lane in `bad` is a slow one: every probe there runs to its deadline.
        outcome = CUT if lane.lane_id in self.bad else self.outcomes.pop(0) if self.outcomes else OK
        outcome, ran_s = outcome if isinstance(outcome, tuple) else (outcome, 60)
        self.vehicles.append((job["job_id"], lane.lane_id, MODELS[model["id"]]))
        self.line.append(list(getattr(self.service, "_probe_line", ())))   # none before the fix
        record = self.service._probe_record(holder)
        record["created_at"] = self.reserved[job["job_id"]] = after(-ran_s)
        self.service._save_probe(record)
        return outcome

    def jobs(self):
        return [job_id for job_id, _, _ in self.vehicles]


def started(service) -> list[str]:
    """Job ids in the order their attempts were reserved."""
    return [row["job_id"] for row in service.store.query("SELECT job_id FROM attempts ORDER BY rowid")]


def make_due(service, *job_ids):
    for job_id in job_ids or [row["job_id"] for row in service.store.list_jobs()]:
        if service.store.get_job(job_id)["state"] == "waiting":
            service.store.update_job(job_id, next_check_at=utcnow())


# --- the incident ---------------------------------------------------------------------------------

def test_d1_a_job_whose_probe_said_nothing_is_not_passed_by_the_jobs_behind_it(routing_state, monkeypatch):
    """I1, I2. The incident: the oldest job's probe is cut at its deadline. Before, the two behind it
    probed the same lane in the same pass and started, and the oldest waited a minute and a pass."""
    service, harness = routing_state
    oldest, second, third = (submit(service, harness) for _ in range(3))
    probes = Probes(service, monkeypatch, [CUT])
    service._admit()
    # One probe ran, for the oldest job. Nothing behind it took the next one.
    assert probes.jobs() == [oldest]
    assert started(service) == []
    assert service._holds[oldest] == {"reason": "probe-pending", "lane": "codex-1", "model": "astra",
                                      "next_check_at": service.store.get_job(oldest)["next_check_at"]}
    for later in (second, third):
        hold = service._holds[later]
        assert (hold["reason"], hold["behind"]) == ("probe-pending", oldest)
        assert (hold["lane"], hold["model"]) == ("codex-1", "astra")
        assert service.store.get_job(later)["wait_reason"] == "capacity"
    # The probe ran its whole minute, so its vehicle is due again at once (I3),
    # and the next pass's first probe is its own; the others follow in order, on
    # that pass, without waiting out their clocks (I4).
    assert service.store.get_job(oldest)["next_check_at"] <= utcnow()
    service._admit()
    assert probes.jobs() == [oldest, oldest, second, third]
    assert started(service) == [oldest, second, third]
    assert not service.store.query("SELECT 1 FROM leases WHERE holder LIKE 'probe:%'")


def test_however_many_probes_say_nothing_the_first_ok_starts_the_oldest(routing_state, monkeypatch):
    """I2: the incident's job drew four such probes while later jobs started on theirs."""
    service, harness = routing_state
    oldest, second = submit(service, harness), submit(service, harness)
    probes = Probes(service, monkeypatch, [CUT, SLOW, SLOW, CUT])
    for _ in range(4):
        service._admit()
        assert started(service) == [] and set(probes.jobs()) == {oldest}
        assert service._holds[second]["behind"] == oldest
    service._admit()
    assert probes.jobs() == [oldest] * 5 + [second]
    assert started(service) == [oldest, second]
    # C-6.10: a probe that ends the same way adds no second decision row.
    assert len(service.store.list_decisions(oldest)) == 2            # the wait's, then the attempt's


# --- I3: the clock ------------------------------------------------------------------------------

@pytest.mark.parametrize("ran_s, left_s", [(60, 0), (75, 0), (7, 53), (0, 60)])
def test_the_retry_clock_runs_from_the_probes_reservation(routing_state, monkeypatch, ran_s, left_s):
    """I3: a probe that used its whole minute leaves its vehicle due at once; one that failed in
    seven seconds waits out the other fifty-three, so a broken CLI is never probed in a loop."""
    service, harness = routing_state
    job_id = submit(service, harness)
    probes = Probes(service, monkeypatch, [(CUT, ran_s)])
    before = utcnow()
    service._admit()
    due = moment(service.store.get_job(job_id)["next_check_at"])
    assert 0 <= (due - moment(before)).total_seconds() - left_s <= 2          # whole-second stamps
    assert (due - moment(probes.reserved[job_id])).total_seconds() >= min(ran_s, PROBE_RETRY_S)
    event = service.store.query("SELECT lane_id,data_json FROM events WHERE kind='job.probe_waiting' "
                                "AND data_json<>'{}'")[-1]
    assert event["lane_id"] == "codex-1"
    assert json.loads(event["data_json"]) == {"model": "astra", "class": "unknown",
                                              "next_check_at": service.store.get_job(job_id)["next_check_at"]}


def test_a_vehicle_whose_clock_runs_keeps_its_place(routing_state, monkeypatch):
    """I1, I3: a probe that failed in a second leaves its vehicle on a clock for the rest of the
    minute; the job behind it is looked at meanwhile and still waits for it, rather than probing the
    lane the moment its own clock comes due."""
    service, harness = routing_state
    oldest, later = submit(service, harness), submit(service, harness)
    probes = Probes(service, monkeypatch, [(CUT, 1)])
    service._admit()
    assert service.store.get_job(oldest)["next_check_at"] > after(30)
    for _ in range(3):
        make_due(service, later)
        service._admit()
        assert service._holds[later]["behind"] == oldest
    assert probes.jobs() == [oldest] and started(service) == []
    make_due(service, oldest)
    service._admit()
    assert started(service) == [oldest, later]


def test_a_wait_on_a_probe_is_not_brought_forward_by_a_release(routing_state, monkeypatch):
    """C-6.10, unchanged: freed capacity never re-probes the provider before the clock."""
    service, harness = routing_state
    job_id = submit(service, harness)
    probes = Probes(service, monkeypatch, [(CUT, 1)])
    service._admit()
    assert service.store.acquire_lease("out:/somewhere", "another-job")
    service._admit()
    service.store.release_leases("another-job")
    service._admit()
    assert probes.jobs() == [job_id] and started(service) == []


# --- I5: the hold is narrow ---------------------------------------------------------------------

def test_a_job_waits_only_for_a_probe_the_waiter_could_use(routing_state, monkeypatch):
    """I5: a waiter pinned to one lane holds no probe of another lane, and a probe of one model holds
    no probe of another (incident, 2026-09-29: a job pinned to a lane that could not take it held 38
    younger jobs while another lane was open)."""
    service, harness = routing_state
    service.store.put_lane(codex_lane(service.root, "codex-2"))
    pinned = submit(service, harness, pinned_lane="codex-1")
    elsewhere = submit(service, harness, pinned_lane="codex-2")
    other_model = submit(service, harness, pinned_model="terra", pinned_lane="codex-1")
    same = submit(service, harness, exclusions=["codex-2"])
    probes = Probes(service, monkeypatch, [CUT])
    service._admit()
    assert probes.vehicles == [(pinned, "codex-1", "astra"), (elsewhere, "codex-2", "astra"),
                               (other_model, "codex-1", "terra")]
    assert started(service) == [elsewhere, other_model]
    assert service._holds[same]["behind"] == pinned
    service._admit()
    assert started(service) == [elsewhere, other_model, pinned, same]


def test_a_job_that_needs_no_probe_is_never_held_for_one(routing_state, monkeypatch):
    """I5: the hold is on the probe. A job whose first attempt is its own probe (C-11.4) starts."""
    service, harness = routing_state
    waiting = submit(service, harness)
    cheap = submit(service, harness, tier="standard")
    probes = Probes(service, monkeypatch, [(CUT, 1)])
    service._admit()
    assert probes.jobs() == [waiting] and started(service) == [cheap]


def test_a_measured_lane_takes_the_waiter_without_a_probe(routing_state, monkeypatch):
    """I4: what the waiter waits for is a look at the lane; a fresh reading is one."""
    service, harness = routing_state
    oldest, second = submit(service, harness), submit(service, harness)
    probes = Probes(service, monkeypatch, [CUT])
    service._admit()
    service.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))
    make_due(service)
    service._admit()
    assert probes.jobs() == [oldest] and started(service) == [oldest, second]


# --- I6: rotation, and the bound it gives -------------------------------------------------------

def test_after_a_probe_said_nothing_the_next_goes_to_another_lane(routing_state, monkeypatch):
    """I6: the second stuck job of the incident probed codex-2 twice running, as the first did."""
    service, harness = routing_state
    service.store.put_lane(codex_lane(service.root, "codex-2"))
    job_id = submit(service, harness)
    probes = Probes(service, monkeypatch, [CUT, CUT, CUT])
    for _ in range(3):
        service._admit()
    # codex-1 ranks first (C-11.3: both unmeasured, nothing in flight, lane id);
    # then the other lane; then, both tried, a new round from the top.
    assert [lane for _, lane, _ in probes.vehicles] == ["codex-1", "codex-2", "codex-1"]
    service._admit()
    assert [lane for _, lane, _ in probes.vehicles] == ["codex-1", "codex-2", "codex-1", "codex-2"]
    assert [row["lane_id"] for row in service.store.list_attempts(job_id)] == ["codex-2"]


def test_a_later_job_held_behind_a_job_stuck_on_a_slow_lane_waits_one_round(routing_state, monkeypatch):
    """I6: the oldest job could use any lane and the slow one ranks first; the later job is pinned
    to the other. Without rotation the oldest probed the slow lane every pass and held the later job
    behind it for as long as that lane stayed slow."""
    service, harness = routing_state
    service.store.put_lane(codex_lane(service.root, "codex-2"))
    oldest = submit(service, harness)
    later = submit(service, harness, pinned_lane="codex-2")
    probes = Probes(service, monkeypatch, bad={"codex-1"})
    service._admit()
    assert probes.vehicles == [(oldest, "codex-1", "astra")]
    assert service._holds[later]["behind"] == oldest
    service._admit()
    assert probes.vehicles[1:] == [(oldest, "codex-2", "astra"), (later, "codex-2", "astra")]
    assert started(service) == [oldest, later]


def test_a_younger_job_probes_a_lane_no_older_job_can_use(routing_state, monkeypatch):
    """I5: the older job is pinned to the slow lane, so the younger one's turn is the other lane's
    probe; held behind the pinned job it waited as long as that lane stayed slow."""
    service, harness = routing_state
    service.store.put_lane(codex_lane(service.root, "codex-2"))
    pinned = submit(service, harness, pinned_lane="codex-1")
    free = submit(service, harness)
    probes = Probes(service, monkeypatch, bad={"codex-1"})
    service._admit()
    assert probes.vehicles == [(pinned, "codex-1", "astra"), (free, "codex-2", "astra")]
    assert started(service) == [free]
    for _ in range(3):
        make_due(service)
        service._admit()
    assert started(service) == [free] and {lane for _, lane, _ in probes.vehicles[2:]} == {"codex-1"}


def test_rotation_never_takes_a_model_the_chain_promotes_to(routing_state, monkeypatch):
    """I6: with every lane of its model tried, a job starts a new round on that model; it does not
    spend a costlier one because probes said nothing."""
    from tests.fake.test_routing_end_to_end import claude_lane
    service, harness = routing_state
    service.store.put_lane(claude_lane("claude-2"))
    service.policy["tiers"].append("highest")
    service.policy["chains"]["research"] = ["haiku", "sonnet", "opus", "astra", "opus"]
    job_id = submit(service, harness, pinned_model=None, task="research")
    probes = Probes(service, monkeypatch, [CUT])
    service._admit()
    service._admit()
    assert [(lane, model) for _, lane, model in probes.vehicles] == [("codex-1", "astra"), ("codex-1", "astra")]
    assert [row["model_requested"] for row in service.store.list_attempts(job_id)] == ["gpt-6-astra"]


def test_a_route_evaluated_again_at_the_reservation_keeps_the_lane_rotation_chose(routing_state, monkeypatch):
    """C-6.3: a commit that moves the route sends the reservation to evaluate it again; that
    evaluation leaves out the lanes rotation left out, so it lands on the lane just probed and does
    not wait for a probe of the lane the probe said nothing on."""
    service, harness = routing_state
    service.store.put_lane(codex_lane(service.root, "codex-2"))
    job_id = submit(service, harness)
    probes = Probes(service, monkeypatch, [CUT])
    service._admit()
    real, moved = service._route_stands, []

    def stands(basis, decision):
        if not moved:
            moved.append(basis.get("rotation"))
            return "moved", 0, None
        return real(basis, decision)
    monkeypatch.setattr(service, "_route_stands", stands)
    service._admit()
    assert moved == [("codex-1",)]
    assert [lane for _, lane, _ in probes.vehicles] == ["codex-1", "codex-2"]
    assert [row["lane_id"] for row in service.store.list_attempts(job_id)] == ["codex-2"]


def test_rotation_never_promotes_at_the_reservation(routing_state, monkeypatch):
    """I6, review of #154 (P2, the same code here): the lane rotation chose closed between its probe and
    the reservation; with the rotated-from lane still left out, the reservation's check walked on to
    Astra. It looks again instead, and the next round goes back to claude-1."""
    from tests.fake.test_routing_end_to_end import claude_lane
    service, harness = routing_state
    for identity in ("claude-1", "claude-2"):
        service.store.put_lane(claude_lane(identity))
    service.store.add_reading(Reading("codex-1", "account", "seven_day", .1, after(3600),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))
    service.policy["tiers"].append("highest")
    service.policy["chains"]["research"] = ["haiku", "sonnet", "opus", "opus", "astra"]
    job_id = submit(service, harness, pinned_model=None, task="research")
    probes = Probes(service, monkeypatch, [CUT, OK])
    service._admit()
    assert [lane for _, lane, _ in probes.vehicles] == ["claude-1"]
    prepare = service._prepare_route

    def then_close(*args):
        result = prepare(*args)
        if result[0] is not None:
            service.store.add_closure(Closure("claude-2", "claude-opus-5-5", after(3600),
                                              ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "race"))
        return result
    monkeypatch.setattr(service, "_prepare_route", then_close)
    service._admit()
    assert [lane for _, lane, _ in probes.vehicles] == ["claude-1", "claude-2"]
    assert service.store.list_attempts(job_id) == []
    assert (service._holds[job_id]["reason"], service._holds[job_id]["model"]) == ("probe-pending", "opus")
    make_due(service)
    service._admit()
    assert [lane for _, lane, _ in probes.vehicles] == ["claude-1", "claude-2", "claude-1"]
    assert [(row["lane_id"], row["model_requested"]) for row in service.store.list_attempts(job_id)] == [
        ("claude-1", "claude-opus-5-5")]
    assert service.store.get_job(job_id)["exclusions"] == "[]"


def test_a_lane_a_job_excludes_is_not_its_probe_turn(routing_state, monkeypatch):
    """I5, review of #153 (P1-1): the older job could use any lane but the one it excludes, so the
    probe of that lane is the later job's turn; it held it while every codex-1 probe ran out of time."""
    service, harness = routing_state
    service.store.put_lane(codex_lane(service.root, "codex-2"))
    older = submit(service, harness, exclusions=["codex-2"])
    later = submit(service, harness, pinned_lane="codex-2")
    probes = Probes(service, monkeypatch, bad={"codex-1"})
    service._admit()
    assert probes.vehicles == [(older, "codex-1", "astra"), (later, "codex-2", "astra")]
    assert started(service) == [later]


def test_a_retry_exclusion_is_left_out_of_the_turn_too(routing_state, monkeypatch):
    """I5, review of #153 (P1-1): the same, the exclusion being the older job's own `limited` attempt
    on codex-2 (C-4.5)."""
    service, harness = routing_state
    service.store.put_lane(codex_lane(service.root, "codex-2"))
    older = submit(service, harness)
    with service.store.transaction("fixture.attempt") as tx:
        tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,outcome_class,"
                   "evidence_json,reserved_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (older + "/a1", older, 1, "codex-2", "gpt-6-astra", "failed", "limited", "{}", utcnow()))
    later = submit(service, harness, pinned_lane="codex-2")
    probes = Probes(service, monkeypatch, bad={"codex-1"})
    for _ in range(2):
        make_due(service)
        service._admit()
    assert later in started(service)
    assert all(lane == "codex-1" for job_id, lane, _ in probes.vehicles if job_id == older)


def test_a_lane_whose_probe_cannot_be_reserved_is_passed_over_in_the_same_look(routing_state, monkeypatch):
    """I5, review of #153 (P1-2): a timer took codex-1's `slot:0` between the older job's evaluation and
    its reservation, every pass. The older job never probed, so nothing rotated, yet it held codex-2's
    turn from the later job pinned there. Now the look goes on to codex-2, and both start."""
    service, harness = routing_state
    service.store.put_lane(codex_lane(service.root, "codex-2"))
    older = submit(service, harness)
    later = submit(service, harness, pinned_lane="codex-2")
    probes = Probes(service, monkeypatch)
    route, timers = service._route, []

    def timer_wins(job, **options):
        decision = route(job, **options)
        if job["job_id"] == older and decision.chosen_lane == "codex-1":
            timers.append(service.timers._reserve(service.store.get_lane("codex-1"), "read"))
        return decision
    monkeypatch.setattr(service, "_route", timer_wins)
    try:
        for _ in range(2):
            for holder in timers:
                service.timers._release(holder)
            timers.clear()
            make_due(service)
            service._admit()
    finally:
        for holder in timers:
            service.timers._release(holder)
    assert probes.vehicles[0] == (older, "codex-2", "astra")
    assert set(started(service)) == {older, later}


def test_a_lock_wait_before_the_reservation_spends_neither_clock(routing_state, monkeypatch):
    """I3, review of #153 (P2-4): 45 s waiting for the store's writer lock before the probe's
    reservation left a retry clock 15 s after it, and a deadline 15 s after it too."""
    service, harness = routing_state
    job_id = submit(service, harness)
    clock = {"now": datetime.now(timezone.utc).replace(microsecond=0)}

    def stamp(value):
        return value.isoformat(timespec="seconds").replace("+00:00", "Z")
    monkeypatch.setattr(daemon_module, "utcnow", lambda: stamp(clock["now"]))
    monkeypatch.setattr(daemon_module, "after", lambda seconds: stamp(clock["now"] + timedelta(seconds=seconds)))
    transaction, deadlines = service.store.transaction, []

    @contextmanager
    def after_a_lock_wait(kind="state.changed", **kwargs):
        if kind == "probe.reserved":
            clock["now"] += timedelta(seconds=45)
        with transaction(kind, **kwargs) as tx:
            yield tx
    monkeypatch.setattr(service.store, "transaction", after_a_lock_wait)

    def probe(job, lane, model, holder):
        record = service._probe_record(holder)
        deadlines.append((record["created_at"], record["deadline_at"]))
        return Outcome(OutcomeClass.UNKNOWN, "fast spawn failure")
    monkeypatch.setattr(service, "_execute_probe", probe)
    service._admit()
    reserved = clock["now"]
    assert deadlines == [(stamp(reserved), stamp(reserved + timedelta(seconds=60)))]
    assert service.store.get_job(job_id)["next_check_at"] == stamp(reserved + timedelta(seconds=PROBE_RETRY_S))


def rotated_to_codex_2(service, harness, monkeypatch, then):
    """A job whose probe on codex-1 said nothing; on the next pass rotation probes codex-2, which
    answers, and `then` runs between that probe and the reservation."""
    service.store.put_lane(codex_lane(service.root, "codex-2"))
    job_id = submit(service, harness)
    probes = Probes(service, monkeypatch, [CUT, OK])
    service._admit()
    prepare = service._prepare_route

    def prepared(*args):
        result = prepare(*args)
        if result[0] is not None:
            then()
        return result
    monkeypatch.setattr(service, "_prepare_route", prepared)
    service._admit()
    monkeypatch.setattr(service, "_prepare_route", prepare)
    assert [lane for _, lane, _ in probes.vehicles] == ["codex-1", "codex-2"]
    return job_id, probes


def test_a_lane_rotation_left_out_is_never_refused_the_job(routing_state, monkeypatch):
    """C-11.4, review of #154: codex-2 closes before the reservation, and codex-1, left out by
    rotation, is the one lane that would take the job. It is held, not refused, and the next round
    probes codex-1 and starts there."""
    service, harness = routing_state
    job_id, probes = rotated_to_codex_2(service, harness, monkeypatch, lambda: service.store.add_closure(
        Closure("codex-2", "account", after(3600), ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "race")))
    assert service._holds[job_id]["reason"] == "probe-pending" and not service.store.list_attempts(job_id)
    make_due(service)
    service._admit()
    assert [lane for _, lane, _ in probes.vehicles] == ["codex-1", "codex-2", "codex-1"]
    assert [row["lane_id"] for row in service.store.list_attempts(job_id)] == ["codex-1"]


def test_a_full_fleet_is_reported_as_one_even_after_rotation(routing_state, monkeypatch):
    """C-6.11, review of #154 (P3-1): with rotation's lane left out the fleet was full anyway; the job
    read `probe-pending` for that look rather than `fleet-full`."""
    service, harness = routing_state
    service.policy["caps"]["max_active_attempts"] = 1

    def fill():
        assert service.store.acquire_lease("lane:codex-1:slot:0", "probe:other")
    job_id, _ = rotated_to_codex_2(service, harness, monkeypatch, fill)
    assert service._holds[job_id]["reason"] == "fleet-full" and not service.store.list_attempts(job_id)


def test_a_check_that_walks_back_to_the_jobs_own_model_is_reserved(routing_state, monkeypatch):
    """C-11.4, review of #153 r2 (P3-A): chain [opus, astra]; claude-1 is measured but closed to Opus,
    so rotation runs on Astra (codex-1 says nothing, codex-2 answers). The Opus closure is lifted
    before the reservation, whose check then chooses claude-1/opus: the job's own first model, which
    is no promotion. It is reserved there, not held as if promoted."""
    from tests.fake.test_routing_end_to_end import claude_lane
    service, harness = routing_state
    service.store.put_lane(codex_lane(service.root, "codex-2"))
    service.store.put_lane(claude_lane("claude-1"))
    service.store.add_reading(Reading("claude-1", "account", "seven_day", .1, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))
    service.store.add_closure(Closure("claude-1", "claude-opus-5-5", after(3600), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))
    service.policy["tiers"].append("highest")
    service.policy["chains"]["research"] = ["haiku", "sonnet", "opus", "opus", "astra"]
    job_id = submit(service, harness, pinned_model=None, task="research")
    probes = Probes(service, monkeypatch, [CUT, OK])
    service._admit()
    prepare = service._prepare_route

    def then_reopen(*args):
        result = prepare(*args)
        if result[0] is not None:
            with service.store.transaction("fixture.release") as tx:
                tx.execute("UPDATE closures SET released_at=? WHERE lane_id='claude-1' AND released_at IS NULL",
                           (utcnow(),))
        return result
    monkeypatch.setattr(service, "_prepare_route", then_reopen)
    make_due(service)
    service._admit()
    assert [lane for _, lane, _ in probes.vehicles] == ["codex-1", "codex-2"]
    assert [(row["lane_id"], row["model_requested"]) for row in service.store.list_attempts(job_id)] == [
        ("claude-1", "claude-opus-5-5")]


def test_a_lane_rotation_left_out_is_never_a_standing_refusal(routing_state, monkeypatch):
    """C-11.8, review of #153 r2 (P3-B): codex-1 said nothing and rotation probed codex-2. Before the
    reservation codex-2 is disabled and codex-1 closes for an hour. Rotation's `excluded` on codex-1 is
    not the job's own exclusion, so the job is not refused for good: its closure ends by itself."""
    service, harness = routing_state
    service.store.put_lane(codex_lane(service.root, "codex-2"))
    job_id = submit(service, harness)
    probes = Probes(service, monkeypatch, [CUT, OK])
    service._admit()
    prepare = service._prepare_route

    def then_turn(*args):
        result = prepare(*args)
        if result[0] is not None:
            service.store.add_closure(Closure("codex-1", "account", after(3600), ClosureReason.PROVIDER_LIMIT,
                                              ClockSource.REPORTED, "race"))
            service.store.update_lane("codex-2", enabled=0)
        return result
    monkeypatch.setattr(service, "_prepare_route", then_turn)
    make_due(service)
    service._admit()
    assert [lane for _, lane, _ in probes.vehicles] == ["codex-1", "codex-2"]
    assert not service._holds[job_id].get("for_good")
    view = service._pin_view(service._desktop_identity())
    assert scheduler.unadmittable(service.policy, view, service.store.get_job(job_id)) is None


def test_probe_reach_excludes_exactly_what_evaluate_excludes(routing_state):
    """C-6.9, differential (review of #153 r2): the lanes a job's probe turn covers are exactly the
    lanes `evaluate` does not refuse as `excluded`, by every name a lane answers to."""
    from tests.fake.test_routing_end_to_end import claude_lane
    service, harness = routing_state
    for identity in ("codex-2", "codex-3"):
        service.store.put_lane(codex_lane(service.root, identity))
    service.store.put_lane(claude_lane("claude-1", account="max@example.org"))
    service.timers.metadata["codex-2"] = {"email": "two@example.org"}
    service.timers.metadata["codex-3"] = {"email": "shared@example.org"}
    roster = service._pin_roster()
    names = sorted({name for lane in roster for name in scheduler._identities(lane)} | {"nobody"})
    view = service._capacity_view()
    base = {"job_id": "j", "kind": "dispatch", "pinned_model": "astra", "tier": "hard", "sandbox": "read-only"}

    @settings(max_examples=300, deadline=None, suppress_health_check=list(HealthCheck), database=None)
    @given(st.lists(st.sampled_from(names), max_size=4, unique=True),
           st.sampled_from([None, "codex-1", "codex-2", "codex-3"]))
    def check(exclusions, pin):
        job = {**base, "exclusions": exclusions, "pinned_lane": pin}
        evaluation = scheduler.evaluate(service.policy, view, job).evaluations[0]
        usable = set(evaluation["candidates"]) | {row["lane_id"] for row in evaluation["rejections"]
                                                  if "excluded" not in row["reasons"]}
        lanes = scheduler.demand_lanes(roster, job, service.policy)
        assert scheduler.probe_reach(service.policy, roster, "astra", lanes, exclusions) == usable
    check()


# --- I4: it ends --------------------------------------------------------------------------------

def test_a_job_that_stops_waiting_on_a_probe_holds_nobody(routing_state, monkeypatch):
    """I4: the line is started again by every pass, from the jobs that wait on a probe then."""
    service, harness = routing_state
    oldest, second, third = (submit(service, harness) for _ in range(3))
    probes = Probes(service, monkeypatch, [(CUT, 1)])
    service._admit()
    assert service._holds[second]["behind"] == oldest and service._holds[third]["behind"] == oldest
    service.dispatch("kill", {"job_id": oldest})
    service._admit()                       # nobody is ahead of them now: looked at on this pass
    assert probes.jobs() == [oldest, second, third] and started(service) == [second, third]


def test_a_limited_probe_closes_the_lane_and_frees_the_line(routing_state, monkeypatch):
    """I4: a waiter whose lane closed waits for capacity, not for a probe, and holds nobody; when
    the lane opens again the two go in their order."""
    service, harness = routing_state
    oldest = submit(service, harness, pinned_lane="codex-1")
    later = submit(service, harness, pinned_lane="codex-1")
    limited = Outcome(OutcomeClass.LIMITED, "limited", {"rc": 1}, closure=Closure(
        "codex-1", "gpt-6-astra", after(3600), ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "fixture"))
    probes = Probes(service, monkeypatch, [(CUT, 1), limited])
    service._admit()
    assert service._holds[later]["behind"] == oldest
    make_due(service)
    service._admit()
    assert probes.jobs() == [oldest, oldest] and service._probe_line == []
    assert all(service._holds[job_id]["reason"].startswith("closed:") for job_id in (oldest, later))
    with service.store.transaction("fixture.closure_lifted") as tx:
        tx.execute("DELETE FROM closures WHERE lane_id='codex-1'")
    make_due(service)
    service._admit()
    assert probes.jobs() == [oldest, oldest, oldest, later] and started(service) == [oldest, later]


def test_a_quarantined_probe_leaves_its_job_to_an_operator_and_holds_nobody(routing_state, monkeypatch):
    """I4: `uncertain` is a person's to end (C-6.11); its lane is held by the probe's lease."""
    service, harness = routing_state
    service.store.put_lane(codex_lane(service.root, "codex-2"))
    oldest, second = submit(service, harness), submit(service, harness)
    quarantined = Outcome(OutcomeClass.UNKNOWN, "probe containment is quarantined", {"probe_quarantined": True})
    probes = Probes(service, monkeypatch, [quarantined])
    service._admit()
    assert service.store.get_job(oldest)["wait_reason"] == "uncertain"
    assert probes.jobs() == [oldest, second] and started(service) == [second]


# --- C-6.11: what `why` says --------------------------------------------------------------------

def test_why_names_the_probe_and_whose_turn_it_is(routing_state, monkeypatch):
    service, harness = routing_state
    oldest, second = submit(service, harness), submit(service, harness)
    Probes(service, monkeypatch, [(CUT, 1)])
    service._admit()
    text = service.dispatch("why", {"job_id": second})["text"]
    assert f"codex-1 must be probed for astra before the job may start on it, and {oldest}, an older job" in text
    assert "its lane is being probed" in service.dispatch("why", {"job_id": oldest})["text"]


# --- C-11.4: a probe its deadline stopped says so ---------------------------------------------

def test_a_probe_stopped_at_its_deadline_says_so(routing_state, monkeypatch):
    """The incident's probes read `unknown` or `transient` with rc 0: nothing recorded that the daemon
    had stopped them, and it took their durations (all 60 s) to see it."""
    service, harness = routing_state
    record = reserved_probe(service, submit(service, harness))
    record["deadline_at"] = after(-1)
    monkeypatch.setattr(service, "_contain_probe", lambda value: True)
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *a, **k: "alive")
    assert service._await_probe(record) == (True, None)
    assert record["stopped"] == "deadline"


def test_a_probe_that_ended_by_itself_is_not_called_stopped(routing_state, monkeypatch):
    service, harness = routing_state
    record = reserved_probe(service, submit(service, harness))
    record["deadline_at"] = after(-1)                               # the receipt is read first
    (Path(record["directory"]) / "exit.json").write_text(json.dumps({"rc": 0, "child_pid": 900002}))
    monkeypatch.setattr(service, "_contain_probe", lambda value: True)
    assert service._await_probe(record)[0] is True
    assert "stopped" not in record


@pytest.mark.parametrize("receipt_after_stop", [False, True])
@pytest.mark.parametrize("guardian_alive", ["alive", "dead", "unknown"])
def test_a_recovered_probe_says_it_was_stopped_only_if_it_was(routing_state, monkeypatch, receipt_after_stop,
                                                              guardian_alive):
    """Review of #154: recovery completed a probe it stopped at the deadline without `stopped`, with or
    without the receipt the stopped guardian wrote during containment (P2); and a probe whose guardian
    was already gone when recovery came after the deadline was stopped by nobody (P3-2)."""
    service, harness = routing_state
    record = reserved_probe(service, submit(service, harness))
    record["deadline_at"] = after(-1)
    service._save_probe(record)

    def census(value):
        if receipt_after_stop:
            exit_receipts(value)
        return procs.Containment()
    monkeypatch.setattr(service, "_probe_census", census)
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *a, **k: guardian_alive)
    monkeypatch.setattr(daemon_module.procs, "same_process", lambda *a, **k: guardian_alive == "alive")
    monkeypatch.setattr(daemon_module.procs, "signal_group", lambda *a, **k: pytest.fail("signalled"))
    service._recover_probes()
    completed = json.loads(service.store.query("SELECT data_json FROM events WHERE kind='probe.completed' "
                                               "AND data_json<>'{}'")[-1]["data_json"])
    # A look that failed ("unknown") is no evidence the guardian is gone (review of #154 r3, P3-A).
    assert completed["evidence"].get("stopped") == (None if guardian_alive == "dead" else "deadline")


def test_a_probe_whose_adapter_raised_after_its_deadline_says_so(routing_state, monkeypatch):
    """Review of #154 (P2, the same code here): an adapter that raised reading a stopped probe's stream."""
    from types import SimpleNamespace
    service, harness = routing_state
    job_id = submit(service, harness)
    record = reserved_probe(service, job_id, state="reserved")
    record["deadline_at"] = after(-1)
    service._save_probe(record)
    monkeypatch.setattr(daemon_module.procs, "pipe_above_stdio", lambda: (800, 801))
    close, write = daemon_module.os.close, daemon_module.os.write
    monkeypatch.setattr(daemon_module.os, "close", lambda fd: None if fd in (800, 801) else close(fd))
    monkeypatch.setattr(daemon_module.os, "write", lambda fd, value: None if fd == 801 else write(fd, value))
    monkeypatch.setattr(daemon_module.subprocess, "Popen",
                        lambda *a, **k: SimpleNamespace(pid=900001, poll=lambda: None))
    monkeypatch.setattr(daemon_module.procs, "same_process", lambda *a, **k: True)     # running at its deadline
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *a, **k: "alive")
    monkeypatch.setattr(service, "_probe_census", lambda value: exit_receipts(value) or procs.Containment())

    def classify(*args):
        raise OSError("stream unavailable after the deadline stop")
    monkeypatch.setattr(FakeAdapter, "classify", classify)
    service._probe_candidate(service.store.get_job(job_id),
                             SimpleNamespace(chosen_lane="codex-1", chosen_model="astra"), record["holder"])
    completed = json.loads(service.store.query("SELECT data_json FROM events WHERE kind='probe.completed' "
                                               "AND data_json<>'{}'")[-1]["data_json"])
    assert completed["evidence"]["stopped"] == "deadline"


@pytest.mark.parametrize("receipt", [{"rc": 0, "signal": None, "wall_s": 60.0, "child_pid": 900002}, None])
def test_the_deadline_is_in_the_probes_evidence(routing_state, monkeypatch, receipt):
    """C-11.4: `probe.completed` carries `stopped: deadline`, with or without an exit receipt."""
    from types import SimpleNamespace
    service, harness = routing_state
    job_id = submit(service, harness)
    record = reserved_probe(service, job_id, state="reserved")
    monkeypatch.setattr(daemon_module.procs, "pipe_above_stdio", lambda: (800, 801))
    close, write = daemon_module.os.close, daemon_module.os.write
    monkeypatch.setattr(daemon_module.os, "close", lambda fd: None if fd in (800, 801) else close(fd))
    monkeypatch.setattr(daemon_module.os, "write", lambda fd, value: None if fd == 801 else write(fd, value))
    monkeypatch.setattr(daemon_module.subprocess, "Popen", lambda command, **kwargs: SimpleNamespace(pid=900001))

    def awaited(value, child):
        value.update(state="contained", stopped="deadline")
        service._save_probe(value)
        return True, receipt
    monkeypatch.setattr(service, "_await_probe", awaited)
    outcome = service._execute_probe(service.store.get_job(job_id), service.store.get_lane("codex-1"),
                                     service.policy["models"]["astra"], record["holder"])
    assert outcome.evidence["stopped"] == "deadline"
    assert outcome.cls == (OutcomeClass.OK if receipt else OutcomeClass.UNKNOWN)


# --- the property: generated jobs, probe outcomes and clocks -------------------------------------

@contextmanager
def fleet():
    """A daemon over two unmeasured Codex lanes, as `routing_state` builds one, for one example."""
    with tempfile.TemporaryDirectory(prefix="probe-turn-") as tmp, pytest.MonkeyPatch.context() as patch:
        root = Path(tmp).resolve() / "state"
        root.mkdir()
        harness = Harness(root)
        patch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
        patch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
        patch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
        patch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
        claude_code_active(patch, Path(tmp) / "claude")
        service = Daemon(root)
        try:
            service.store.put_lane(codex_lane(root, "codex-2"))
            yield service, harness, patch
        finally:
            service.close()


#: What a probe can answer that says nothing about its lane, and how long it ran.
SAYS_NOTHING = st.tuples(st.sampled_from([CUT, SLOW, Outcome(OutcomeClass.UNKNOWN, "probe unavailable: OSError"),
                                          Outcome(OutcomeClass.CONTENT_FILTER, "filtered", {"rc": 1})]),
                         st.sampled_from([0, 1, 7, 30, 59, 60, 61, 90]))
JOBS = st.lists(st.tuples(st.sampled_from(["astra", "terra"]), st.sampled_from([None, "codex-1", "codex-2"])),
                min_size=2, max_size=6)
#: Before each pass: every waiting job made due, only those whose own clock says so, or one killed.
STEPS = st.lists(st.tuples(st.sampled_from(["due", "clock", "clock", "kill"]), st.integers(0, 5)),
                 min_size=1, max_size=8)


def could_use(job: tuple, lane_id: str, model: str) -> bool:
    """Whether a job that waits on a probe of its model could use this one (I1)."""
    return job[0] == model and job[1] in (None, lane_id)


@settings(max_examples=60, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(jobs=JOBS, outcomes=st.lists(st.one_of(st.just(OK), SAYS_NOTHING), max_size=14), steps=STEPS,
       bad=st.sets(st.sampled_from(["codex-1", "codex-2"]), max_size=1))
def test_no_sequence_of_probe_outcomes_lets_a_later_job_take_a_waiters_probe(jobs, outcomes, steps, bad):
    """I1 to I6 over generated jobs (two models, pinned to either lane or to none), probe outcomes that
    say nothing (cut at the deadline, transient, unknown, a content filter; of any length), clocks, and
    a lane whose every probe runs to its deadline until the end."""
    with fleet() as (service, harness, patch):
        ids = [submit(service, harness, pinned_model=model, pinned_lane=lane) for model, lane in jobs]
        wants = dict(zip(ids, jobs))
        order = {job_id: index for index, job_id in enumerate(ids)}
        probes = Probes(service, patch, outcomes, bad=bad)
        killed: set[str] = set()

        def check_pass(first: int):
            for (vehicle, lane_id, model), line in zip(probes.vehicles[first:], probes.line[first:]):
                # I1, from the line the daemon kept: nobody in it could use this probe.
                assert not [waiter for waiter, wanted, lanes in line
                            if wanted == model and (lanes is None or lane_id in lanes)]
                assert all(order[waiter] < order[vehicle] for waiter, _, _ in line)
            waiting_on_probe = {job_id: hold for job_id, hold in service._holds.items()
                                if hold["reason"] == "probe-pending"}
            for vehicle, lane_id, model in probes.vehicles[first:]:
                # I1, from the holds the pass published: no job ahead of a vehicle
                # still waits on a probe the vehicle carried.
                assert not [job_id for job_id in waiting_on_probe if order[job_id] < order[vehicle]
                            and could_use(wants[job_id], lane_id, model) and job_id != vehicle]
            begun = started(service)
            assert len(begun) == len(set(begun))                        # one attempt a job
            for index, later in enumerate(begun):
                # I2: nothing that started is ahead of an older job of the same
                # demand that still waits on a probe.
                assert not [job_id for job_id in waiting_on_probe if order[job_id] < order[later]
                            and wants[job_id] == wants[later]]
            for job_id, hold in waiting_on_probe.items():
                job = service.store.get_job(job_id)
                assert job["state"] == "waiting" and job["wait_reason"] == "capacity" and job["next_check_at"]
                if hold.get("behind"):
                    # Held for a turn: by an older job that waits on the same probe.
                    assert order[hold["behind"]] < order[job_id]
                    assert could_use(wants[hold["behind"]], hold["lane"], hold["model"])
                    assert service._holds[hold["behind"]]["reason"] == "probe-pending"
                elif job_id in probes.jobs()[first:]:
                    # I3: due PROBE_RETRY_S after its probe was reserved, or now if that has passed.
                    reserved = moment(probes.reserved[job_id])
                    waited = (moment(job["next_check_at"]) - reserved).total_seconds()
                    ran = (moment(utcnow()) - reserved).total_seconds()
                    assert abs(waited - max(PROBE_RETRY_S, ran)) <= 2, (waited, ran)
            assert not service.store.query("SELECT 1 FROM leases WHERE holder LIKE 'probe:%'")

        for action, index in steps:
            if action == "due":
                make_due(service)
            elif action == "kill" and index < len(ids) and ids[index] not in killed | set(started(service)):
                service.dispatch("kill", {"job_id": ids[index]})
                killed.add(ids[index])
            first = len(probes.vehicles)
            service._admit()
            check_pass(first)
        # I2, over the whole run: among jobs of one demand, starts follow the order.
        begun = started(service)
        for demand in set(jobs):
            same = [job_id for job_id in begun if wants[job_id] == demand]
            assert same == sorted(same, key=order.get)
            unstarted = [job_id for job_id in ids if wants[job_id] == demand and job_id not in begun
                         and job_id not in killed]
            assert all(order[job_id] > order[done] for job_id in unstarted for done in same)
        # I6: once the other probes answer, every job that could run on a lane
        # that is not slow starts while the slow lane stays slow, within two passes
        # a job (a probe there that says nothing, then one elsewhere).
        probes.outcomes.clear()
        for _ in range(2 * len(ids) + 2):
            make_due(service)
            first = len(probes.vehicles)
            service._admit()
            check_pass(first)
        stuck = {job_id for job_id in ids if wants[job_id][1] in bad}
        assert set(started(service)) == set(ids) - killed - stuck
        # I4: once every probe answers, the rest start too.
        probes.bad.clear()
        for _ in range(len(ids) + 1):
            make_due(service)
            first = len(probes.vehicles)
            service._admit()
            check_pass(first)
        assert set(started(service)) == set(ids) - killed


# --- the rule itself ----------------------------------------------------------------------------

WAITERS = st.lists(st.tuples(st.sampled_from(["astra", "terra", "opus"]),
                             st.one_of(st.none(), st.frozensets(st.sampled_from(["a", "b", "c"]), max_size=2))),
                   max_size=6)


@given(waiters=WAITERS, lane=st.sampled_from(["a", "b", "c"]), model=st.sampled_from(["astra", "terra", "opus"]))
def test_the_turn_is_the_first_waiter_that_could_use_the_probe(waiters, lane, model):
    line = [(f"job-{index}", wanted, lanes) for index, (wanted, lanes) in enumerate(waiters)]
    turn = scheduler.probe_turn(line, lane, model)
    usable = [job_id for job_id, wanted, lanes in line if wanted == model and (lanes is None or lane in lanes)]
    assert turn == (usable[0] if usable else None)
    if turn is not None:
        # Whoever is ahead of the one with the turn had no use for the probe, and
        # taking the one with the turn out of the line passes it to the next.
        rest = [row for row in line if row[0] != turn]
        assert scheduler.probe_turn(rest, lane, model) == (usable[1] if len(usable) > 1 else None)
    # A line with nobody in it, or only waiters on other models, holds nobody.
    assert scheduler.probe_turn([], lane, model) is None
    assert scheduler.probe_turn([row for row in line if row[1] != model], lane, model) is None
