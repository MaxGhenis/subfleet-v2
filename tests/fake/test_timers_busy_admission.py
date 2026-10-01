"""C-18.3 through admission: a busy lane under a reset credit's override is measured by its read.

The 2026-09-30 codex-4 case with the daemon's own admission: a confirmed reset-credit
consume makes `_pick` hold out the lane's readings (C-23.17), so the lane is
"eligible but unmeasured" and capped at `max_in_flight_unmeasured` (1). Only a timer
read settles the override, and until this change no timer read reached a lane with
an attempt in flight.
"""

import json
from datetime import datetime, timedelta, timezone

from subfleet.contracts import Reading, ReadingLabel
from subfleet.daemon import utcnow
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401  (fixture)


def judged(decision, lane_id="codex-1"):
    """How the decision's walk judged `lane_id`: a rejection, or a candidate's details."""
    for evaluation in decision["evaluations"]:
        for row in evaluation["rejections"]:
            if row["lane_id"] == lane_id:
                return {key: row[key] for key in ("status", "measured", "reasons", "in_flight")}
        if lane_id in evaluation["candidate_details"]:
            row = evaluation["candidate_details"][lane_id]
            return {key: row.get(key) for key in ("status", "measured", "reasons", "in_flight")}
    return None


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Wham:
    """codex-1's account (`codex:fake`, `tests/fake/conftest.Harness`) has opened again."""

    def __init__(self):
        self.calls, self.leases_seen = [], []

    def probe_status(self, lane, env):
        self.calls.append(lane.lane_id)
        now = datetime.now(timezone.utc)
        return {"status": "ok", "limit_reached": False, "allowed": True, "account_key": "codex:fake",
                "checked_at": stamp(now), "readings": (
                    Reading(lane.lane_id, "account", "seven_day", .02, stamp(now + timedelta(days=7)),
                            ReadingLabel.PROVIDER, "wham", stamp(now)),)}

    def list_reset_credits(self, *args, **kwargs):
        raise AssertionError("an open lane is never a reset-credit candidate")


def test_c18_3_a_busy_lane_under_an_override_is_measured_by_its_read_and_takes_a_second_job(routing_state):  # noqa: F811
    """C-18.3, C-23.17, C-6.4, C-11.5: before the read the second job waits `no-slot` on an "eligible but
    unmeasured" lane; one busy read settles the override and admission places it beside the first."""
    service, harness = routing_state
    # Main's defaults, and the installed release's effective cap: the release line's are null (C-6.4),
    # and there an unmeasured lane is uncapped but still cannot be refused at its floor.
    service.policy["caps"].update(max_in_flight_per_lane=2, max_in_flight_unmeasured=1)
    confirmed = stamp(datetime.now(timezone.utc) - timedelta(minutes=1))
    service.store.add_reading(Reading("codex-1", "account", "seven_day", 1., stamp(datetime.now(timezone.utc) + timedelta(days=3)),
                                      ReadingLabel.PROVIDER, "wham", stamp(datetime.now(timezone.utc) - timedelta(hours=33))))
    service.store.add_action(action_id="credit-codex-1", kind="reset-credit", op_key="codex:fake:gift-1",
                             subject="codex-1", state="confirmed",
                             request_json=json.dumps({"lane_id": "codex-1", "account_key": "codex:fake"}),
                             result_json=json.dumps({"windows_reset": 1}), created_at=confirmed, updated_at=confirmed)
    assert service.timers.actions.confirmed_override("codex-1")

    first = service.dispatch("submit", harness.submit_args())["job_id"]
    second = service.dispatch("submit", harness.submit_args())["job_id"]
    service._admit()
    assert [row["lane_id"] for row in service.store.list_attempts(first)] == ["codex-1"]
    assert not service.store.list_attempts(second)
    held = service.dispatch("why", {"job_id": second})
    assert judged(held["decision"]) == {"status": "eligible but unmeasured", "measured": False,
                                        "reasons": ["no-slot"], "in_flight": 1}

    wham = Wham()
    service.timers.adapter_factory = lambda provider: wham
    leases = service.store.list_leases()
    service.timers.probe_cycle()
    assert wham.calls == ["codex-1"]
    assert service.store.list_leases() == leases              # the read took no slot
    assert service.timers.actions.confirmed_override("codex-1") is None

    service.store.update_job(second, next_check_at=utcnow())  # its capacity recheck is due (C-6.10)
    service._admit()
    assert [row["lane_id"] for row in service.store.list_attempts(second)] == ["codex-1"]
    placed = service.dispatch("why", {"job_id": second})["decision"]
    assert placed["chosen_lane"] == "codex-1"
    assert judged(placed) == {"status": "eligible", "measured": True, "reasons": None, "in_flight": 1}
    in_flight = service.store.query("SELECT COUNT(*) AS n FROM attempts WHERE lane_id='codex-1' AND state IN "
                                    "('reserved','starting','running','finalizing')")[0]["n"]
    assert in_flight == 2
