"""C-6.3, C-26.9: admission places what it placed before the check and the turn pass.

The review of d04b8b3 found no unsafe reservation in the new admission, but two
ways it could hold a job back forever where e053b2c's placed it at once:

- Staggered readings on sixty Claude lanes, refreshed on a 120 s cycle, kept the
  fleet-wide horizon one to three seconds away. A Codex job's reservation came
  three seconds after its evaluation, so every check was past that horizon and
  refused it (`old`), three tries a pass, for thirty passes: no reservation,
  though every evaluation chose the same Codex lane. The check now reads each
  lane's own clock, and only for the lanes the decision looks at.
- Turns took `lane:<id>:slot:0` first on every pass, the one lease a detached
  job's admission probe takes, so an older writable job stayed `probe-pending`
  for as long as turns kept coming. Turns now number slots of their own.

These run the daemon's own admission in-process, on a simulated clock where one
matters, with the reviewer's schedules.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import capacity, scheduler
from subfleet import daemon as daemon_module
from subfleet.contracts import (ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, Reading,
                                ReadingLabel)
from subfleet.daemon import Daemon
from tests.fake.test_admission_latency import fleet_daemon, submit, submit_turn
from tests.routing_strategies import event


class Clock(datetime):
    """The daemon's wall clock, moved by the test."""
    at = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.at


def simulated(service, patch, start: datetime) -> None:
    Clock.at = start
    patch.setattr(daemon_module, "datetime", Clock)
    patch.setattr(service.timers, "now", lambda: Clock.at)
    patch.setattr(service, "_desktop_identity", lambda: capacity.DesktopIdentity("unverified"))
    patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None))


def stamp(at: datetime) -> str:
    return capacity._iso(at)


# --- P1: a lane's clock is read only where the decision looks --------------------------------------

def test_c6_3_readings_ageing_out_on_sixty_unrelated_lanes_never_hold_a_job_back(tmp_path):
    """The review's schedule, as it ran on d04b8b3: sixty Claude lanes whose readings are
    staggered two seconds apart and refreshed once 120 s old (the TTL), published at the
    time of refresh; two Codex jobs pinned to codex-1, Astra, which no Claude lane could
    ever take; three seconds between each evaluation and its reservation. At every check
    the fleet's earliest horizon has passed. The older job is reserved on the first pass,
    no Claude lane is ever judged again, and nothing is evaluated a second time."""
    start = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        simulated(service, patch, start)
        observed, reset = {}, stamp(start + timedelta(days=1))
        for index in range(60):
            lane_id = f"claude-{index}"
            service.store.put_lane(Lane(lane_id, "claude", f"claude:fixture-{index}",
                                        Credential("claude", f"fixture-{index}", "keychain-token"), None,
                                        LaneOwner.V2, False))
            observed[lane_id] = start - timedelta(seconds=2 * index)
            service.store.add_reading(Reading(lane_id, "account", "seven_day", .2, reset, ReadingLabel.PROVIDER,
                                              "fixture", stamp(observed[lane_id])))
        for name in ("older", "younger"):
            service.store.add_job(job_id=name, request_id=name, payload_digest=name, kind="dispatch",
                                  pinned_model="astra", pinned_lane="codex-1", workdir=str(harness.workdir),
                                  prompt_path=str(harness.root / "prompt.md"), sandbox="read-only")
        pick, stands = service._pick, service._route_stands
        checks, refreshed = [], [0]

        def sensor_then_pick(job, **options):
            assert not service.store._holds_writer()
            for lane_id, when in list(observed.items()):          # due sensors publish, at the time they do
                if (Clock.at - when).total_seconds() >= 120:
                    observed[lane_id] = Clock.at
                    refreshed[0] += 1
                    service.store.add_reading(Reading(lane_id, "account", "seven_day", .2, reset,
                                                      ReadingLabel.PROVIDER, "fixture", stamp(Clock.at)))
            decision = pick(job, **options)
            if job["job_id"] == "older":                           # every evaluation of it chooses the same
                assert (decision.chosen_lane, decision.chosen_model) == ("codex-1", "astra")
            Clock.at += timedelta(seconds=3)                       # evaluation and the wait for the store lock
            return decision

        def check(basis, decision):
            if basis["job"]["job_id"] == "older":
                fleet = capacity.decision_horizon(basis["view"], reading_ttl_s=120)
                checks.append(fleet is not None and Clock.at >= fleet)
            return stands(basis, decision)
        patch.setattr(service, "_pick", sensor_then_pick)
        patch.setattr(service, "_route_stands", check)
        for _ in range(30):
            service._admit()
            if service.store.list_attempts("older"):
                break
        assert [row["lane_id"] for row in service.store.list_attempts("older")] == ["codex-1"]
        assert checks == [True]                    # the fleet's horizon had passed: d04b8b3 refused this
        counts = service._route_evaluations
        assert counts["old"] == 0 and counts["again"] == 0 and counts["deferred"] == 0
        assert counts["rejudged"] == 0             # no Claude lane was ever looked at
        # The younger job competes for codex-1. With no cap (C-6.4, the default since
        # 2026-09-27) it is placed beside the older one; under a cap it waits for a
        # slot. Either way it never waits on a clock.
        assert service._holds.get("younger", {}).get("reason") != "route-moved"


def test_c6_3_a_reservation_records_the_evidence_an_evaluation_there_would(tmp_path):
    """Review of d04b8b3 (P3): codex-1's reading is past its reset (it measures nothing)
    and 119 s old when the route is evaluated; two seconds later, at the reservation, an
    evaluation labels it `stale-provider`. The recorded decision said `provider`: the check
    carried the early evaluation's evidence. It records what an evaluation at its own
    clock records, the reading's label and age included."""
    start = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        simulated(service, patch, start)
        service.store.update_lane("codex-2", enabled=0)
        service.store.update_lane("codex-3", enabled=0)
        service.store.add_reading(Reading("codex-1", "account", "seven_day", .2, stamp(start - timedelta(seconds=1)),
                                          ReadingLabel.PROVIDER, "fixture", stamp(start - timedelta(seconds=119))))
        job_id = service.dispatch("submit", harness.submit_args(pinned_model="astra"))["job_id"]
        pick = service._pick

        def pick_then_wait(job, **options):
            decision = pick(job, **options)
            assert [row["label"] for row in decision.evaluations[0]["readings"]] == ["provider"]
            Clock.at += timedelta(seconds=2)
            return decision
        patch.setattr(service, "_pick", pick_then_wait)
        service._admit()
        attempts = service.store.list_attempts(job_id)
        assert [row["lane_id"] for row in attempts] == ["codex-1"]
        recorded = json.loads(service.store.one("SELECT decision_json FROM decisions WHERE attempt_id=?",
                                                (attempts[0]["attempt_id"],))["decision_json"])
        evaluation = recorded["evaluations"][0]
        assert [row["label"] for row in evaluation["readings"]] == ["stale-provider"]
        assert [row["age_s"] for row in evaluation["readings"]] == [121.0]
        assert evaluation["evaluated_at"] == stamp(start + timedelta(seconds=2))
        full = scheduler.evaluate(service.policy, service._capacity_view(), {
            **service.store.get_job(job_id), "exclusions": (), "policy_hash": service.policy_digest})
        assert [row["label"] for row in full.evaluations[0]["readings"]] == ["stale-provider"]


# --- P1: a turn never holds the lease a detached job's probe needs ---------------------------------

def probing(service, patch, probes: list, during=None):
    """A successful admission probe that records itself, and runs `during` while its lease is held."""
    from subfleet.contracts import Outcome, OutcomeClass

    def probe(job, lane, model, holder):
        held = service.store.one("SELECT lease_key FROM leases WHERE holder=?", (holder,))
        probes.append((job["job_id"], lane.lane_id, held["lease_key"]))
        if during is not None:
            during()
        return Outcome(OutcomeClass.OK, "admitted", {"rc": 0, "signal": None})
    patch.setattr(service, "_execute_probe", probe)


def one_unmeasured_lane(service, harness, patch):
    """codex-1 alone, no reading: a writable detached job needs its probe first (C-11.4).
    The workdir is a committed repository on a feature branch, as a writable job's must be."""
    from tests.unit.test_salvage import git
    for argv in (("init", "-b", "task/liveness"), ("config", "user.name", "Test User"),
                 ("config", "user.email", "test@example.invalid")):
        git(harness.workdir, *argv)
    (harness.workdir / "tracked.txt").write_text("baseline\n")
    git(harness.workdir, "add", ".")
    git(harness.workdir, "commit", "-m", "baseline")
    service.store.update_lane("codex-2", enabled=0)
    service.store.update_lane("codex-3", enabled=0)
    patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None))


def complete(service, job_id):
    live = [row for row in service.store.list_attempts(job_id) if row["state"] == "reserved"][0]
    with service.store.transaction("test.complete") as tx:
        tx.execute("UPDATE attempts SET state='succeeded' WHERE attempt_id=?", (live["attempt_id"],))
        tx.execute("UPDATE jobs SET state='succeeded' WHERE job_id=?", (job_id,))
        tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (live["attempt_id"], job_id))


def slot_of(service, job_id):
    attempt = service.store.list_attempts(job_id)[0]["attempt_id"]
    return service.store.one("SELECT lease_key FROM leases WHERE holder=? AND lease_key LIKE 'lane:%'",
                             (attempt,))["lease_key"]


def test_c26_9_a_stream_of_turns_never_keeps_an_older_writable_job_from_its_probe(tmp_path):
    """The review's schedule: an `easy` writable detached job on codex-1, unmeasured, needs
    an admission probe, whose lease is `lane:codex-1:slot:0`; a default-tier turn arrives,
    and each next one is queued before the last one ends, ten in all. On d04b8b3 the turn
    pass, which runs first, gave every turn `slot:0`, the job's probe could never take it,
    and it stayed `probe-pending` through all ten (e053b2c's one pass put the `easy` job
    before the turn, and placed both). Turns now take slots of their own: the job is probed
    and placed on the first pass, beside the first turn, and every turn is placed."""
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        one_unmeasured_lane(service, harness, patch)
        probes = []
        probing(service, patch, probes)
        detached = submit(service, harness, pinned_model="astra", tier="easy", sandbox="workspace-write",
                          in_place=True)
        turn = submit_turn(service, harness, 0)
        for n in range(10):
            service._admit()
            assert [row["lane_id"] for row in service.store.list_attempts(detached)] == ["codex-1"]
            assert [row["lane_id"] for row in service.store.list_attempts(turn)] == ["codex-1"]
            assert slot_of(service, turn) == "lane:codex-1:slot:turn-0"
            successor = submit_turn(service, harness, n + 1) if n < 9 else None
            complete(service, turn)
            turn = successor
        assert probes == [(detached, "codex-1", "lane:codex-1:slot:0")]      # one probe, on the first pass
        assert slot_of(service, detached) == "lane:codex-1:slot:1"          # `slot:0` is the probe's alone


def test_c26_9_a_turn_of_the_same_tier_never_keeps_a_writable_job_from_its_probe(tmp_path):
    """The same with a `standard` writable job: e053b2c's one pass put a turn before a
    detached job of its own tier, so there too each turn took `slot:0` first, and the job
    waited for a gap between turns. It is placed on the first pass now."""
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        one_unmeasured_lane(service, harness, patch)
        probes = []
        probing(service, patch, probes)
        detached = submit(service, harness, pinned_model="astra", tier="standard", sandbox="workspace-write",
                          in_place=True)
        turn = submit_turn(service, harness, 0)
        service._admit()
        assert [row["lane_id"] for row in service.store.list_attempts(detached)] == ["codex-1"]
        assert [job for job, _, _ in probes] == [detached]
        assert slot_of(service, turn) == "lane:codex-1:slot:turn-0"
        assert slot_of(service, detached) == "lane:codex-1:slot:1"


def test_c11_4_detached_work_never_keeps_an_older_writable_job_from_its_probe(tmp_path):
    """The uncap plan's review: with no per-lane cap a busy lane nearly always had an
    attempt on `slot:0`, the one lease an admission probe takes, so an older writable
    job waiting to probe lost it to each later job that needed no probe. Detached
    attempts number from `slot:1` now: a read-only job runs on codex-1, a writable job
    behind it is probed at once, beside it, and a third job runs beside both."""
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        one_unmeasured_lane(service, harness, patch)
        probes = []
        probing(service, patch, probes)
        first = submit(service, harness, pinned_model="astra", tier="easy")
        service._admit()
        assert slot_of(service, first) == "lane:codex-1:slot:1"
        writable = submit(service, harness, pinned_model="astra", tier="easy", sandbox="workspace-write",
                          in_place=True)
        later = submit(service, harness, pinned_model="astra", tier="easy")
        service._admit()
        assert probes == [(writable, "codex-1", "lane:codex-1:slot:0")]      # probed while `first` runs
        assert slot_of(service, writable) == "lane:codex-1:slot:2"
        assert slot_of(service, later) == "lane:codex-1:slot:3"
        assert not service.store.one("SELECT 1 FROM leases WHERE lease_key='lane:codex-1:slot:0'")


def test_c26_9_a_turn_held_off_a_lane_by_a_probe_is_looked_at_when_the_probe_ends(tmp_path):
    """The turn pass runs beside the detached pass, so it can meet a lane a detached job's
    admission probe holds: the probe keeps every job off the lane while it runs (C-11.4),
    a turn too, and the turn waits (`no-slot`) on a backed-off clock. Probe leases were not
    counted as freed capacity, so the turn waited out that clock after the probe had ended;
    under e053b2c's one pass the probe had always ended before the turn was looked at. The
    turn pass now counts the admission probes' leases, and looks again at once."""
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        one_unmeasured_lane(service, harness, patch)
        probes, turn = [], []

        def turn_pass_meanwhile():
            turn.append(submit_turn(service, harness, 1))
            service._admit_turns()                       # the turn worker's pass, while the probe runs
            assert not service.store.list_attempts(turn[0])
            assert service._holds[turn[0]]["reason"] == "no-slot"
            # Its next look is well in the future: only freed capacity brings it forward.
            service.store.update_job(turn[0], next_check_at=daemon_module.after(600))
        probing(service, patch, probes, during=turn_pass_meanwhile)
        detached = submit(service, harness, pinned_model="astra", tier="easy", sandbox="workspace-write",
                          in_place=True)
        service._admit()
        assert [row["lane_id"] for row in service.store.list_attempts(detached)] == ["codex-1"]
        assert not service.store.list_attempts(turn[0])  # the detached pass does not look at turns
        service._admit_turns()
        assert [row["lane_id"] for row in service.store.list_attempts(turn[0])] == ["codex-1"]
        assert slot_of(service, turn[0]) == "lane:codex-1:slot:turn-0"


@pytest.mark.parametrize("probe_began", ["before the pass", "during the pass"])
def test_c26_9_a_turn_held_by_a_probe_is_looked_at_when_it_ends_whenever_it_began(tmp_path, probe_began):
    """Review of 5d14f98: the turn pass counted the admission probes' leases it read at its
    start. A probe that began after that, while the pass evaluated a turn pinned to its
    lane, held the turn `no-slot`, and its end freed nothing the pass had seen: the turn
    waited out its backed-off clock. A turn held by a probe now counts that probe's lease
    as seen, whenever it began."""
    holder = "probe:0123456789abcdef01234567"          # the shape `_prepare_route` gives an admission probe
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        one_unmeasured_lane(service, harness, patch)
        turn = submit_turn(service, harness, 1)
        if probe_began == "before the pass":
            assert service.store.acquire_lease("lane:codex-1:slot:0", holder)
        real = service._pick

        def pick(job, **options):
            decision = real(job, **options)
            if probe_began == "during the pass" and job["job_id"] == turn:
                assert service.store.acquire_lease("lane:codex-1:slot:0", holder)
            return decision
        patch.setattr(service, "_pick", pick)
        service._admit_turns()
        assert not service.store.list_attempts(turn) and service._holds[turn]["reason"] == "no-slot"
        service.store.release_leases(holder)                              # the probe ends
        service.store.update_job(turn, next_check_at=daemon_module.after(600))   # only freed capacity helps now
        patch.setattr(service, "_pick", real)
        service._admit_turns()
        assert [row["lane_id"] for row in service.store.list_attempts(turn)] == ["codex-1"]


@pytest.mark.parametrize("old", [False, True], ids=["this admission", "e053b2c's"])
def test_c6_3_a_fleet_cap_that_flips_between_evaluation_and_check_never_holds_a_job_back(tmp_path, old):
    """Review of 5d14f98, its schedule at resonance: the detached fleet is one short of its
    cap, and a timer probe's lease (which counts toward it) is held while each evaluation
    reads its snapshot and gone by each check. The check refused a cap that ended between
    the two, ROUTE_TRIES times a pass: the job was never placed, while e053b2c's
    evaluation at the reservation placed it at once. The check now judges every lane again
    when a cap begins or ends, and places it on the first try, as e053b2c did; with the
    flip the other way round (free at the evaluation, full at the check) it waits
    `fleet-full`, as e053b2c's did, and is never `route-moved`."""
    timer = "probe:timer:liveness"
    for phase in ("full at evaluation", "full at check"):
        with fleet_daemon(tmp_path / phase.replace(" ", "-")) as (service, harness, patch):
            service.policy["caps"].update(max_active_attempts=2, max_in_flight_per_lane=1, reading_ttl_s=3600)
            patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None))
            from tests.fake.test_admission_latency import measure
            for lane_id in CODEX:
                measure(service, lane_id)
            occupier = submit(service, harness, pinned_model="astra", pinned_lane="codex-1")
            service._admit()
            assert [row["lane_id"] for row in service.store.list_attempts(occupier)] == ["codex-1"]
            job_id = submit(service, harness, pinned_model="astra")
            real = service._pick

            def flipping(job, _real=real, _full_first=(phase == "full at evaluation"), **options):
                if _full_first:
                    assert service.store.acquire_lease("lane:codex-3:slot:0", timer)
                decision = _real(job, **options)
                if _full_first:
                    service.store.release_leases(timer)
                else:
                    assert service.store.acquire_lease("lane:codex-3:slot:0", timer)
                return decision
            patch.setattr(service, "_pick", flipping)
            if old:
                as_before(service, patch)
            service._admit()
            if phase == "full at evaluation":
                assert [row["lane_id"] for row in service.store.list_attempts(job_id)] == ["codex-2"]
            else:
                assert not service.store.list_attempts(job_id)
                assert service._holds[job_id]["reason"] == "fleet-full"
                service.store.release_leases(timer)
            assert service._route_evaluations["deferred"] == 0 and service._route_evaluations["again"] == 0


def test_c26_9_both_passes_racing_keep_every_cap_detached_fifo_and_place_everything(tmp_path):
    """The review's concurrency probe: 40 detached jobs and 24 turns on three measured
    Codex lanes, four admission calls at once per wave (two `_admit`, two turn passes),
    a random pause between each evaluation and its reservation, then every attempt ends.
    No job has two attempts and no slot lease two holders; neither pool passes its fleet
    or per-lane cap; each kind is placed in submission order; and everything is placed
    within 20 waves."""
    import random
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from tests.fake.test_admission_latency import measure
    with fleet_daemon(tmp_path / "fleet") as (service, harness, patch):
        service.policy["caps"].update(max_active_attempts=4, max_in_flight_per_lane=2, max_in_flight_unmeasured=1,
                                      reading_ttl_s=3600)
        service.policy["conversations"].update(max_active_turns=2, turn_slots_per_lane=1)
        for lane_id in CODEX:
            measure(service, lane_id)
        patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None))
        detached = [submit(service, harness, pinned_model="astra") for _ in range(40)]
        turns = [submit_turn(service, harness, n) for n in range(24)]
        picked, pauses, lock = service._pick, random.Random(927), threading.Lock()

        def interleaved(job, **options):
            decision = picked(job, **options)
            with lock:
                pause = pauses.uniform(0, .003)
            time.sleep(pause)
            return decision
        patch.setattr(service, "_pick", interleaved)
        admitted = {"detached": [], "turn": []}
        for wave in range(20):
            if len(admitted["detached"]) == len(detached) and len(admitted["turn"]) == len(turns):
                break
            with ThreadPoolExecutor(max_workers=4) as workers:
                for future in [workers.submit(fn) for fn in (service._admit, service._admit_turns,
                                                            service._admit, service._admit_turns)]:
                    future.result(timeout=120)
            active = service.store.query("SELECT a.*,j.kind FROM attempts a JOIN jobs j USING(job_id) "
                                         "WHERE a.state IN ('reserved','starting','running','finalizing') "
                                         "ORDER BY a.rowid")
            assert active, wave
            assert len({row["job_id"] for row in active}) == len(active)
            slots = service.store.query("SELECT * FROM leases WHERE lease_key LIKE 'lane:%:slot:%'")
            assert len(slots) == len(active) == len({row["lease_key"] for row in slots})
            for turn, fleet_cap, lane_cap, label in ((False, 4, 2, "detached"), (True, 2, 1, "turn")):
                pool = [row for row in active if (row["kind"] == "turn") == turn]
                assert len(pool) <= fleet_cap
                for lane_id in CODEX:
                    assert sum(row["lane_id"] == lane_id for row in pool) <= lane_cap
                admitted[label].extend(row["job_id"] for row in pool)
            assert admitted["detached"] == detached[:len(admitted["detached"])]       # C-6.9 FIFO
            with service.store.transaction("test.complete") as tx:
                for attempt in active:
                    tx.execute("UPDATE attempts SET state='succeeded' WHERE attempt_id=?", (attempt["attempt_id"],))
                    tx.execute("UPDATE jobs SET state='succeeded' WHERE job_id=?", (attempt["job_id"],))
                    tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (attempt["attempt_id"], attempt["job_id"]))
        assert admitted["detached"] == detached and admitted["turn"] == turns


def test_c26_9_with_no_turn_cap_one_pass_places_every_turn(tmp_path):
    """The shipped policy sets no turn cap: five turns on three measured Codex lanes are
    all placed by one admission call, so two lanes each run two turns, and no turn is held
    `fleet-full` or `slot-kept` (2026-09-27: a turn waited 12 minutes behind two others)."""
    from tests.fake.test_admission_latency import measure
    with fleet_daemon(tmp_path / "fleet") as (service, harness, patch):
        assert service.policy["conversations"]["max_active_turns"] is None
        assert service.policy["conversations"]["turn_slots_per_lane"] is None
        for lane_id in CODEX:
            measure(service, lane_id)
        patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None))
        turns = [submit_turn(service, harness, n) for n in range(5)]
        service._admit()
        held = {job_id: service._holds.get(job_id) for job_id in turns if not service.store.list_attempts(job_id)}
        assert not held
        lanes = [service.store.list_attempts(job_id)[0]["lane_id"] for job_id in turns]
        assert max(lanes.count(lane_id) for lane_id in CODEX) >= 2


@pytest.mark.parametrize("newer_pin", [None, "codex-2"])
def test_c26_9_with_no_turn_cap_no_turn_waits_behind_another(tmp_path, newer_pin):
    """C-6.9 keeps an older job's place only where a later one could take the slot it waits
    for. With no turn cap there is none: an older turn waiting on its closed lane holds a
    later turn neither `behind-older-job` nor `slot-kept`. With a cap set, FIFO holds."""
    from tests.fake.test_admission_latency import commit, measure
    # Either cap alone turns FIFO on (review of 1b38d641: each half of the rule untested).
    for caps, placed in (({}, True), ({"max_active_turns": 3, "turn_slots_per_lane": 1}, newer_pin is not None),
                         ({"turn_slots_per_lane": 1}, newer_pin is not None),
                         ({"max_active_turns": 3}, newer_pin is not None)):
        with fleet_daemon(tmp_path / ("fleet-" + "-".join(sorted(caps)))) as (service, harness, patch):
            service.policy["conversations"].update(caps)
            for lane_id in CODEX:
                measure(service, lane_id)
            patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None))
            commit(service, "close", "codex-1", 1)
            older = submit_turn(service, harness, 0, pinned_lane="codex-1")
            newer = submit_turn(service, harness, 1, **({"pinned_lane": newer_pin} if newer_pin else {}))
            for _ in range(2):
                service._admit()
            assert service._holds[older]["reason"].startswith("closed")
            assert bool(service.store.list_attempts(newer)) is placed, (caps, service._holds.get(newer))
            if not placed:
                assert service._holds[newer]["reason"] == "behind-older-job"


def _turn_in(service, harness, n, *, workdir, sandbox="workspace-write", model="astra"):
    """A conversation turn's job in `workdir`, as the dispatcher submits it."""
    from subfleet import protocol
    prompt = harness.root / f"turn-{n}.md"
    prompt.write_text("turn")
    args = protocol.SubmitArgs(request_id=f"turn:message-{n}:0", kind="turn", workdir=str(workdir),
                               prompt_path=str(prompt), sandbox=sandbox, pinned_model=model,
                               name=f"turn-conversation-{n}", in_place=True, independent=True,
                               no_preamble=True, max_attempts=1, allow_tmp=True)
    turn = {"conversation_id": f"conversation-{n}", "message_id": f"message-{n}", "provider": "codex",
            "digest": f"digest-{n}"}
    return service.submit(args, turn=turn)["job_id"]


def _checkout(harness):
    """Make the harness's workdir a git checkout with one commit, so writable turns lease it."""
    from tests.unit.test_salvage import git
    for argv in (("init", "-b", "task/x"), ("config", "user.name", "T"), ("config", "user.email", "t@example.invalid")):
        git(harness.workdir, *argv)
    (harness.workdir / "f.txt").write_text("x\n")
    git(harness.workdir, "add", ".")
    git(harness.workdir, "commit", "-m", "base")


def _end(service, job_id):
    """The job's attempts end and release their leases, as a finished turn's do."""
    with service.store.transaction("test.end") as tx:
        for (attempt_id,) in tx.execute("SELECT attempt_id FROM attempts WHERE job_id=?", (job_id,)).fetchall():
            tx.execute("UPDATE attempts SET state='succeeded' WHERE attempt_id=?", (attempt_id,))
            tx.execute("DELETE FROM leases WHERE holder=?", (attempt_id,))
        tx.execute("UPDATE jobs SET state='succeeded' WHERE job_id=?", (job_id,))
        tx.execute("DELETE FROM leases WHERE holder=?", (job_id,))


def test_c26_9_with_no_turn_cap_a_turn_never_waits_behind_another_checkout_lease(tmp_path):
    """Two writable turns in one checkout take turns on its lease; with no turn cap, a
    read-only turn of a third conversation elsewhere is placed while one of them waits."""
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        _checkout(harness)
        patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None))
        elsewhere = harness.root / "elsewhere"
        elsewhere.mkdir()
        first = _turn_in(service, harness, 0, workdir=harness.workdir)
        service._admit_turns()
        assert service.store.list_attempts(first)
        second = _turn_in(service, harness, 1, workdir=harness.workdir)
        third = _turn_in(service, harness, 2, workdir=elsewhere, sandbox="read-only")
        for _ in range(3):
            service._admit_turns()
        assert not service.store.list_attempts(second)
        assert service._holds[second]["reason"] == "lease-held"
        assert service.store.list_attempts(third), service._holds.get(third)


def _clock(service, job_id, when):
    service.store.update_job(job_id, next_check_at=when)


@pytest.mark.parametrize("older_clock", ["future", "due"])
@pytest.mark.parametrize("caps,newer_model", [
    ({}, "astra"),                                                   # no cap: no C-6.9 hold among turns
    ({"max_active_turns": 3, "turn_slots_per_lane": 1}, "terra"),   # capped, but the two do not compete
    ({"max_active_turns": 50, "turn_slots_per_lane": 50}, "terra"),
])
def test_c26_9_a_lease_freed_mid_pass_goes_to_the_turn_that_waited_for_it(tmp_path, caps, newer_model, older_clock):
    """FIFO on a lease (review of 1b38d641). An older turn waits for a checkout another
    turn holds; that turn ends after the pass has passed the older one, and a newer turn
    wanting the same checkout is looked at next. The newer turn waits, queued behind the
    older one, which takes the checkout on the next pass. Without the queue the newer turn
    took it: with no turn cap nothing else holds one turn behind another, and a turn of
    another model never competed (C-6.9) even with caps set. The older turn is passed on
    its clock (`future`: queued from its recorded hold) or looked at (`due`: queued when
    it is held again); each path is forced, not left to where a second boundary falls."""
    from subfleet.daemon import after, utcnow
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        service.policy["conversations"].update(caps)
        _checkout(harness)
        patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None))
        first = _turn_in(service, harness, 0, workdir=harness.workdir)
        service._admit_turns()
        assert service.store.list_attempts(first)
        older = _turn_in(service, harness, 1, workdir=harness.workdir)
        service._admit_turns()
        assert service._holds[older]["reason"] == "lease-held"
        newer = _turn_in(service, harness, 2, workdir=harness.workdir, model=newer_model)
        _clock(service, older, after(3600) if older_clock == "future" else utcnow())
        real = service._workspace

        def first_ends_now(job):
            if job["job_id"] == newer:
                _end(service, first)
            return real(job)
        patch.setattr(service, "_workspace", first_ends_now)
        service._admit_turns()
        hold = service._holds[newer]
        assert not service.store.list_attempts(newer), hold
        assert hold["reason"] == "lease-held" and hold["leases"] == [] and hold["queued_behind"] == [older]
        assert [key.split(":", 1)[0] for key in hold["queued"]] == ["worktree"]
        patch.setattr(service, "_workspace", real)
        service._admit_turns()
        assert service.store.list_attempts(older), service._holds.get(older)
        assert not service.store.list_attempts(newer)
        _end(service, older)
        service._admit_turns()
        assert service.store.list_attempts(newer), service._holds.get(newer)


def test_c26_9_turns_waiting_on_one_lease_take_it_oldest_first(tmp_path):
    """Three turns wait for one checkout: a lease freed mid-pass is queued for the oldest,
    both later turns name the oldest, and the checkout then passes to them in age order."""
    from subfleet.daemon import after
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        _checkout(harness)
        patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None))
        first = _turn_in(service, harness, 0, workdir=harness.workdir)
        service._admit_turns()
        oldest = _turn_in(service, harness, 1, workdir=harness.workdir)
        middle = _turn_in(service, harness, 2, workdir=harness.workdir)
        service._admit_turns()
        assert service._holds[oldest]["reason"] == service._holds[middle]["reason"] == "lease-held"
        newest = _turn_in(service, harness, 3, workdir=harness.workdir)
        for job_id in (oldest, middle):
            _clock(service, job_id, after(3600))
        real = service._workspace

        def first_ends_now(job):
            if job["job_id"] == newest:
                _end(service, first)
            return real(job)
        patch.setattr(service, "_workspace", first_ends_now)
        service._admit_turns()
        assert service._holds[newest]["queued_behind"] == [oldest]
        patch.setattr(service, "_workspace", real)
        order = []
        for _ in range(3):
            service._admit_turns()
            live = [job_id for job_id in (oldest, middle, newest) if service.store.query(
                "SELECT 1 FROM attempts WHERE job_id=? AND state IN ('reserved','starting','running')", (job_id,))]
            assert len(live) <= 1
            order.extend(job_id for job_id in live if job_id not in order)
            for job_id in live:
                _end(service, job_id)
        assert order == [oldest, middle, newest]


def test_c26_9_a_turn_on_its_clock_keeps_its_place_for_a_lease_it_was_queued_for(tmp_path):
    """A turn whose last hold was queued-only (the lease free, kept for an older turn that
    has since gone) keeps its place while it waits on its clock: a newer turn wanting the
    lease is queued behind it, not placed. The clocked-skip branch reads `queued` as well
    as `leases` (review of b74e4aa5: a branch reading only `leases` passed every test).
    No release happens in the pass, so the waiting turn is not hurried: the hold it
    recorded is set up directly, as a pass would have recorded it."""
    from subfleet.daemon import after
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        _checkout(harness)
        patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None))
        waiting = _turn_in(service, harness, 1, workdir=harness.workdir)
        key = f"worktree:{service._write_target(service._job(waiting), harness.workdir)}"
        hold = {"reason": "lease-held", "leases": [], "queued": [key], "queued_behind": ["gone"]}
        service._capacity_wait(waiting, "lease-held:" + key, hold)
        with service.store.transaction("fixture.wait") as tx:
            tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=? WHERE job_id=?",
                       (after(3600), waiting))
        service._admit_turns()                                   # records the lease snapshot: nothing is freed next
        newer = _turn_in(service, harness, 2, workdir=harness.workdir)
        service._admit_turns()
        assert not service.store.list_attempts(newer), service._holds.get(newer)
        assert service._holds[newer]["queued_behind"] == [waiting]


# --- the property: what e053b2c's admission places, this one places, pass for pass ----------------

CODEX = ("codex-1", "codex-2", "codex-3")
TIMER = "probe:timer:liveness"
LIVENESS = settings(max_examples=40, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture,
                                           HealthCheck.data_too_large])


def as_before(service, patch) -> None:
    """e053b2c's admission, from this code: one pass over every queued job in `ordered_jobs`
    order (a turn before the detached jobs of its own tier only), and the route evaluated
    again, whole, inside the reserving transaction, at the reservation's clock, as e053b2c
    did whenever a commit had landed since its early evaluation. Turn slots are numbered
    apart from detached ones, and detached ones from 1, as here: with one numbering
    e053b2c let turns of a writable job's own tier keep it from its probe (the test
    above), and detached attempts on `slot:0` did the same to a writable job behind them
    (review of the uncap plan), holds this code removes, not a bar to hold it to."""
    evaluate = Daemon._pick.__get__(service)

    def numbered_apart(tx, lane_id, turn):
        prefix = f"lane:{lane_id}:slot:" + ("turn-" if turn else "")
        slot = 0 if turn else 1
        while tx.execute("SELECT 1 FROM leases WHERE lease_key=?", (f"{prefix}{slot}",)).fetchone():
            slot += 1
        return f"{prefix}{slot}"
    patch.setattr(service, "_route_stands",
                  lambda basis, decision: (None, 0, evaluate(basis["job"], desktop=basis["desktop"])))
    patch.setattr(service, "_in_pass", lambda job, kind: True)
    patch.setattr(service, "_slot_lease", numbered_apart)
    patch.setattr(service, "_admit", lambda: service._admit_kind("detached"))


def placements(service) -> dict[str, tuple[str, str]]:
    """Every job admission has placed, by request id: the lane and model of its attempt."""
    return {row["request_id"]: (row["lane_id"], row["model_requested"]) for row in service.store.query(
        "SELECT j.request_id, a.lane_id, a.model_requested FROM attempts a JOIN jobs j USING(job_id)")}


@LIVENESS
@given(st.data())
def test_c6_3_c26_9_admission_places_what_e053b2c_placed_pass_for_pass(tmp_path_factory, data):
    """Over random schedules of readings (sensors on the Codex lanes and on up to 24 Claude
    lanes no job here could take, refreshed on their own cycles, so readings age out
    between an evaluation and its reservation), closures (some ending within a pass),
    timer probes that start or end between an evaluation and its check (so the detached
    fleet's cap begins or ends under it), conversation turns, detached
    jobs (writable ones needing their probe on an unmeasured lane, pins, every tier) and
    attempts ending, this admission and e053b2c's (`as_before`)
    run side by side from the same fleet, on one clock: every evaluation of a pass at the
    pass's instant, each job's reservation 0 to 3 s after it. After every pass both have
    placed the same jobs, on the same lanes and models: for every N, every job the old
    admission places within N passes this one places within N passes, and nothing else.
    On d04b8b3 this fails both ways the review found: a job refused at every check for
    another lane's clock, and a writable job kept from its probe by a turn; on 09f82e1,
    a job refused at every check for a cap that began or ended."""
    draw = data.draw
    root = tmp_path_factory.mktemp("liveness")
    start = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
    caps = {"max_active_attempts": draw(st.sampled_from([1, 2, 4]), label="fleet cap"),
            "max_in_flight_per_lane": draw(st.sampled_from([1, 2]), label="lane cap")}
    turn_cap = draw(st.sampled_from([1, 2]), label="turn cap")
    sensors = {}                                    # lane -> (cycle, phase, utilization)
    for lane_id in CODEX:
        if draw(st.sampled_from([True, False, False]), label=f"{lane_id} measured"):
            sensors[lane_id] = (draw(st.sampled_from([60, 120]), label=f"{lane_id} cycle"),
                                draw(st.integers(0, 119), label=f"{lane_id} phase"),
                                draw(st.sampled_from([.1, .5, .9]), label=f"{lane_id} use"))
    unrelated = draw(st.sampled_from([0, 8, 24]), label="unrelated Claude lanes")
    for n in range(unrelated):
        sensors[f"claude-{n}"] = (120, (n * 120) // max(unrelated, 1), .2)
    disabled = draw(st.lists(st.sampled_from(CODEX), max_size=2, unique=True), label="disabled")
    delays: dict[str, int] = {}
    flips: dict[str, bool] = {}
    clock = [start]
    published: dict[tuple[str, str], set] = {}
    with fleet_daemon(root / "new") as (new, new_harness, new_patch), \
            fleet_daemon(root / "old") as (old, old_harness, old_patch):
        new_patch.setattr(daemon_module, "datetime", Clock)
        Clock.at = start
        fleets = {"new": (new, new_harness, new_patch), "old": (old, old_harness, old_patch)}
        for name, (service, harness, patch) in fleets.items():
            service.policy["caps"].update(caps)
            service.policy.setdefault("conversations", {}).update(max_active_turns=turn_cap, turn_slots_per_lane=1)
            one_unmeasured_lane(service, harness, patch)          # a committed workdir; codex-2, codex-3 off ...
            for lane_id in CODEX:                                 # ... and back on unless drawn disabled
                service.store.update_lane(lane_id, enabled=int(lane_id not in disabled))
            for n in range(unrelated):
                service.store.put_lane(Lane(f"claude-{n}", "claude", f"claude:fixture-{n}",
                                            Credential("claude", f"fixture-{n}", "keychain-token"), None,
                                            LaneOwner.V2, False))
            patch.setattr(service.timers, "now", lambda: Clock.at)
            patch.setattr(service, "_desktop_identity", lambda: capacity.DesktopIdentity("unverified"))
            # Each job its own worktree, as admission cuts one (C-6.6), with no git here.
            patch.setattr(service, "_workspace", lambda job, _root=harness.root: (
                str(_root / "worktrees" / job["job_id"]) if job["sandbox"] == "workspace-write" else job["workdir"],
                None, None))
            probing(service, patch, [])
            real = service._pick

            def on_the_pass_clock(job, _real=real, _service=service, **options):
                Clock.at = clock[0]                               # every evaluation at the pass's instant
                decision = _real(job, **options)
                Clock.at = clock[0] + timedelta(seconds=delays.get(job["request_id"], 0))
                if flips.get(job["request_id"]):
                    # A timer probe starts or ends between this evaluation and its check, on
                    # a lane no job here runs on: its lease counts toward the detached
                    # fleet's cap (C-6.4), so the cap can begin or end under the check.
                    # Only after detached jobs' evaluations, which both admissions make in
                    # one order: turns come first here and among them there, and a lease
                    # left by one kind's look would reach the other's in another order.
                    if not _service.store.release_leases(TIMER):
                        assert _service.store.acquire_lease("lane:elsewhere:slot:0", TIMER)
                return decision
            patch.setattr(service, "_pick", on_the_pass_clock)
            if name == "old":
                as_before(service, patch)

        def publish(service, name):
            """Every sensor sample due by now, stamped when it was taken."""
            for lane_id, (cycle, phase, use) in sensors.items():
                taken = published.setdefault((name, lane_id), set())
                sample = start - timedelta(seconds=cycle - phase)
                while sample <= clock[0]:
                    if sample not in taken:
                        taken.add(sample)
                        service.store.add_reading(Reading(lane_id, "account", "seven_day", use,
                                                          stamp(start + timedelta(days=3)), ReadingLabel.PROVIDER,
                                                          "fixture", stamp(sample)))
                    sample += timedelta(seconds=cycle)

        jobs, turns = 0, 0
        passes = draw(st.integers(3, 7), label="passes")
        for number in range(passes):
            clock[0] += timedelta(seconds=draw(st.sampled_from([31, 47, 90]), label="step"))
            Clock.at = clock[0]
            submissions = []
            for _ in range(draw(st.integers(0, 2), label="detached jobs")):
                jobs += 1
                request = f"job-{jobs}"
                delays[request] = draw(st.sampled_from([0, 1, 3]), label="delay")
                flips[request] = draw(st.booleans(), label="a timer probe starts or ends after evaluating")
                writable = draw(st.sampled_from([True, True, False]), label="writable")
                submissions.append(("detached", request, {
                    "request_id": request, "pinned_model": draw(st.sampled_from(["astra", "terra"]), label="model"),
                    "tier": draw(st.sampled_from(["trivial", "easy", "standard", "hard"]), label="tier"),
                    "sandbox": "workspace-write" if writable else "read-only", "caller_session": f"session-{jobs}",
                    **({"pinned_lane": draw(st.sampled_from(CODEX), label="pin")}
                       if draw(st.integers(0, 3), label="pinned") == 0 else {})}))
            for _ in range(draw(st.integers(0, 2), label="turns")):
                turns += 1
                delays[f"turn:message-{turns}:0"] = draw(st.sampled_from([0, 1, 3]), label="delay")
                submissions.append(("turn", turns, None))
            closures = draw(st.lists(st.tuples(st.sampled_from(CODEX),
                                               st.sampled_from(["account", "gpt-6-astra", "gpt-5.6-terra"]),
                                               st.sampled_from([2, 40, 400])), max_size=1), label="closures")
            running = sorted(request for request, _ in placements(new).items()
                             if new.store.one("SELECT 1 FROM jobs WHERE request_id=? AND state='running'", (request,)))
            ends = sorted({running[index % len(running)] for index in
                           draw(st.lists(st.integers(0, 9), max_size=3), label="ends")} if running else set())
            for name, (service, harness, _) in fleets.items():
                publish(service, name)
                for kind, request, args in submissions:
                    if kind == "turn":
                        submit_turn(service, harness, request)
                    else:
                        service.dispatch("submit", harness.submit_args(**args))
                for lane_id, scope, seconds in closures:
                    service.store.put_closure(Closure(lane_id, scope, stamp(clock[0] + timedelta(seconds=seconds)),
                                                      ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "test"))
                for request in ends:
                    complete(service, service.store.get_job_by_request(request)["job_id"])
                for row in service.store.query("SELECT job_id FROM jobs WHERE state='waiting'"):
                    service.store.update_job(row["job_id"], next_check_at=stamp(clock[0]))   # every wait due
            for name, (service, _, _) in fleets.items():
                Clock.at = clock[0]
                service._admit()
                service.store.release_leases(TIMER)                   # the timer probe is over by the next pass
            assert placements(new) == placements(old), (number, placements(old), placements(new))
        placed = len(placements(new))
        event(f"placed: {min(placed, 6)}{'+' if placed >= 6 else ''}")
        probes = len(new.store.query("SELECT 1 FROM events WHERE kind='probe.completed'"))
        event(f"probes: {min(probes, 2)}{'+' if probes >= 2 else ''}")
        if probes and turns:
            event("probes and turns in one run")
        counts = new._route_evaluations
        event("lanes judged again at a check" if counts["rejudged"] else "no lane judged again")
        if any(flips.values()):
            event("a timer probe came or went between an evaluation and its check")
        # Timer probes come and go between evaluations and checks here, caps begin and end,
        # readings age out: none of it sends a route to be evaluated again or a job to wait.
        assert counts["again"] == 0 and counts["deferred"] == 0
