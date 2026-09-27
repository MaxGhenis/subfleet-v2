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
