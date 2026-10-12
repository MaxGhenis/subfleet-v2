"""C-18.7 regressions from the adversarial review of d04f1d08 (MaxGhenis/subfleet-v2#182). No provider calls."""
import json

import pytest

from subfleet.policy import DEFAULT_POLICY_PATH, load_policy, touch_model
from subfleet.timers import iso
from tests.unit.test_timers_touch import rig, weekly


@pytest.mark.parametrize("window", ["1440", "20160"])
def test_nonweekly_window_never_starts_weekly_touch(rig, window):
    timer, store, clock, wham, enroll, turns, *_ = rig
    lane = enroll()
    wham.overrides[lane.lane_id] = {
        "status": "ok", "limit_reached": False, "allowed": True,
        "account_key": lane.account_key,
        "readings": (weekly(clock(), window=window),),
    }
    timer.probe_cycle()
    assert turns == [], "non-weekly reading caused a supervised weekly-clock touch"


def test_finished_job_between_plan_and_reservation_is_not_touched(rig, monkeypatch):
    timer, store, clock, wham, enroll, turns, *_ = rig
    lane = enroll()
    wham.lag_s = 600
    for reading in wham.probe_status(lane, {})["readings"]:
        store.add_reading(reading)
    plan = timer.touch_plan(timer.snapshot())
    [entry] = [entry for entry in plan if entry["action"] == "touch"]
    original_claim = timer._claim

    def claim_after_finished_job(current, purpose):
        # A short job won the lane after the plan, opened its window and left
        # before this touch obtained the reservation.
        store.add_job(job_id="intervening", request_id="req", payload_digest="digest", kind="dispatch",
                      state="succeeded", workdir=str(timer.root), prompt_path="/prompt", sandbox="read-only")
        store.add_attempt(attempt_id="intervening/a1", job_id="intervening", seq=1, lane_id=lane.lane_id,
                          rc=0, model_requested="gpt-6-luna", state="succeeded", started_at=iso(clock()),
                          native_session_id="thread")
        wham.start(lane.lane_id)
        return original_claim(current, purpose)

    monkeypatch.setattr(timer, "_claim", claim_after_finished_job)
    item = timer._touch_lane(lane, entry, mode="operator", request_id="review182", wait_s=0)
    timer._publish_touch(item, "operator")
    assert turns == [], "--all touched a weekly clock opened by an intervening short job"


def test_nonzero_short_window_use_never_starts_weekly_touch(rig):
    timer, store, clock, wham, enroll, turns, *_ = rig
    lane = enroll()
    wham.overrides[lane.lane_id] = {
        "status": "ok", "limit_reached": False, "allowed": True,
        "account_key": lane.account_key,
        "readings": (weekly(clock()), weekly(clock(), window="five_hour", utilization=.01)),
    }
    timer.probe_cycle()
    assert turns == [], "nonzero provider-observed use caused a weekly-clock touch"


def test_implicit_retired_luna_uses_fixed_default_id(tmp_path):
    raw = json.loads(DEFAULT_POLICY_PATH.read_text())
    raw["models"].pop("luna")
    raw["timers"].pop("touch_model", None)
    raw["retired"]["luna"] = "astra"
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    policy = load_policy(path)
    assert touch_model(policy)["id"] == "gpt-6-luna"
