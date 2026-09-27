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

from subfleet import capacity, scheduler
from subfleet import daemon as daemon_module
from subfleet.contracts import Credential, Lane, LaneOwner, Reading, ReadingLabel
from tests.fake.test_admission_latency import fleet_daemon


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
        # The younger job competes for codex-1, unmeasured and so one slot: it waits for a slot, not a clock.
        assert service._holds["younger"]["reason"] != "route-moved"


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
    from tests.fake.test_admission_latency import submit, submit_turn
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
        assert slot_of(service, detached) == "lane:codex-1:slot:0"


def test_c26_9_a_turn_of_the_same_tier_never_keeps_a_writable_job_from_its_probe(tmp_path):
    """The same with a `standard` writable job: e053b2c's one pass put a turn before a
    detached job of its own tier, so there too each turn took `slot:0` first, and the job
    waited for a gap between turns. It is placed on the first pass now."""
    from tests.fake.test_admission_latency import submit, submit_turn
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
        assert slot_of(service, detached) == "lane:codex-1:slot:0"


def test_c26_9_a_turn_held_off_a_lane_by_a_probe_is_looked_at_when_the_probe_ends(tmp_path):
    """The turn pass runs beside the detached pass, so it can meet a lane a detached job's
    admission probe holds: the probe keeps every job off the lane while it runs (C-11.4),
    a turn too, and the turn waits (`no-slot`) on a backed-off clock. Probe leases were not
    counted as freed capacity, so the turn waited out that clock after the probe had ended;
    under e053b2c's one pass the probe had always ended before the turn was looked at. The
    turn pass now counts the admission probes' leases, and looks again at once."""
    from tests.fake.test_admission_latency import submit, submit_turn
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
