"""C-6.9 end to end: a real admission pass agrees with the hold rule on generated queues.

Each example builds a fresh daemon with three measured Claude lanes, some of them
closed, a few older Fable jobs waiting on capacity (each pinned to one lane or to
none), and one later Fable job, then runs one admission pass. The expected result
comes from a reference model written from the contract's words (C-6.9, C-6.11),
not from `scheduler.tier_hold`, so the two are a differential check:

- the later job is never placed on a lane an older competing waiter is pinned to;
- an older waiter with no lane pin holds it back entirely (unpinned FIFO);
- two jobs pinned to different lanes never compete;
- otherwise it runs on a lane nobody keeps, or reports `behind-older-job`, naming
  the oldest job keeping a lane that would take it, only when every lane that
  would take it is kept;
- its stored exclusions never change, and no older job is placed.
"""

import json
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet import daemon as daemon_module
from subfleet.adapters import registry
from subfleet.contracts import (ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, Reading,
                                ReadingLabel)
from subfleet.daemon import Daemon, after, utcnow
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter

LANES = ("claude-a", "claude-b", "claude-c")
pins = st.sampled_from((None,) + LANES)


@pytest.fixture
def offline(monkeypatch):
    """What `routing_state` patches, once for every example: no process table, no desktop login."""
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
    monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
    monkeypatch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})


def expected(olders, later_pin, closed):
    """C-6.9 and C-6.11 in the contract's terms: (lanes it may run on, or the hold it gets)."""
    could = set(LANES) if later_pin is None else {later_pin}
    kept = {}
    for older, pin in olders:
        if pin is not None and later_pin is not None and pin != later_pin:
            continue                                            # pinned to different lanes: never compete
        if pin is None:
            return {"reason": "behind-older-job", "behind": older}          # could use any lane
        kept.setdefault(pin, older)                             # the oldest waiter pinned there keeps it
        if later_pin is not None and could <= kept.keys():
            return {"reason": "behind-older-job", "behind": older}          # confined to kept lanes
    open_lanes = could - kept.keys() - closed
    if open_lanes:
        return open_lanes
    kept_only = {lane: kept[lane] for lane in could & kept.keys() - closed}
    if kept_only:
        order = [older for older, _ in olders]
        return {"reason": "behind-older-job", "kept": kept_only,
                "behind": min(kept_only.values(), key=order.index)}
    return {"reason": "closed:account"}


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(pins, max_size=3), pins, st.sets(st.sampled_from(LANES)))
def test_c6_9_one_admission_pass_matches_the_hold_rule(offline, older_pins, later_pin, closed):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "state"
        root.mkdir()
        harness = Harness(root)
        service = Daemon(root)
        try:
            for lane_id in LANES:
                service.store.put_lane(Lane(lane_id, "claude", f"claude:{lane_id}@example.invalid",
                                            Credential("claude", f"/fake/{lane_id}", "home"), f"/fake/{lane_id}",
                                            LaneOwner.V2, False))
                service.store.add_reading(Reading(lane_id, "account", "seven_day", .2, after(86400),
                                                  ReadingLabel.PROVIDER, "fixture", utcnow()))
            for lane_id in closed:
                service.store.add_closure(Closure(lane_id, "account", after(3600), ClosureReason.PROVIDER_LIMIT,
                                                  ClockSource.REPORTED, "fixture"))
            olders = []
            for pin in older_pins:
                older = service.dispatch("submit", harness.submit_args(pinned_model="fable", pinned_lane=pin))["job_id"]
                service.store.update_job(older, state="waiting", wait_reason="capacity", next_check_at=after(3600))
                olders.append((older, pin))
            later = service.dispatch("submit", harness.submit_args(pinned_model="fable", pinned_lane=later_pin))["job_id"]
            stored = service.store.get_job(later)["exclusions"]
            service._admit()
            want = expected(olders, later_pin, closed)
            placed = [row["lane_id"] for row in service.store.list_attempts(later)]
            hold = service._holds.get(later)
            if isinstance(want, set):
                assert len(placed) == 1 and placed[0] in want, hold
            else:
                assert not placed
                assert {key: hold.get(key) for key in want} == want, hold
            assert all(not service.store.list_attempts(older) for older, _ in olders)
            assert service.store.get_job(later)["exclusions"] == stored
            if placed:
                kept = {pin for _, pin in olders if pin is not None and later_pin is None}
                assert placed[0] not in kept
                decision = json.loads(service.store.list_decisions(later)[-1]["decision_json"])
                assert "kept:" not in json.dumps(decision) or later_pin is None
        finally:
            service.close()
