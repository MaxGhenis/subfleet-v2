"""C-18.7 hold and release checks from the adversarial review of d04f1d08 (MaxGhenis/subfleet-v2#182)."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from subfleet.contracts import ClockSource, Closure, ClosureReason, LaneOwner
from subfleet.daemon import Daemon
from subfleet.protocol import LanesArgs
from subfleet.timers import iso
from tests.unit.test_timers_touch import hold, rig


@pytest.mark.parametrize("changes", [
    {"enabled": 0}, {"desktop": 1}, {"owner": LaneOwner.V1},
    {"identity_status": "mismatch"},
])
def test_review182_state_changed_at_lease_grant_is_rechecked(rig, monkeypatch, changes):
    timer, store, clock, wham, enroll, turns, *_ = rig
    lane = enroll()
    claim = timer._claim

    def reserve_then_change(current, purpose):
        holder = claim(current, purpose)
        store.update_lane(current.lane_id, **changes)
        return holder

    monkeypatch.setattr(timer, "_claim", reserve_then_change)
    item = timer._touch_lane(lane, {"reason": "forced"}, mode="operator", request_id="review", wait_s=0)
    timer._publish_touch(item, "operator")
    assert item["record"]["status"].startswith("skipped-")
    assert turns == [] and store.list_leases() == []


def test_review182_release_only_ends_the_requested_hold_and_keeps_provider_limit(rig):
    timer, store, clock, wham, enroll, turns, *_ = rig
    first, second = enroll("codex-1"), enroll("codex-2")
    store.add_closure(Closure(first.lane_id, "account", iso(clock() + timedelta(hours=3)),
                              ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "wham"))
    hold(store, first.lane_id, clock() + timedelta(hours=1))
    hold(store, second.lane_id, clock() + timedelta(hours=2))
    service = SimpleNamespace(store=store, _notify=lambda: None, _desktop_identity=lambda: None,
                              _capacity_view=lambda _: timer.snapshot())
    Daemon._hold_lane(service, LanesArgs(action="release", lane_id=second.lane_id))
    assert timer.operator_holds() == {first.lane_id: iso(clock() + timedelta(hours=1))}
    Daemon._hold_lane(service, LanesArgs(action="release", lane_id=first.lane_id))
    assert timer.operator_holds() == {}
    [closure] = store.list_closures(first.lane_id)
    assert closure["reason"] == "provider-limit" and closure["released_at"] is None
