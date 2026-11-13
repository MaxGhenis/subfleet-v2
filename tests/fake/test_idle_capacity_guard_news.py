"""D-ADMIT-IDLE1 r1: temporary door holds must not spend capacity news."""

from copy import deepcopy

import pytest

from subfleet import scheduler
from subfleet.contracts import ClosureReason
from subfleet.daemon import utcnow
from subfleet.disk import GB
from tests.fake.test_admission_latency import submit_turn
from tests.fake.test_capacity_wait_backoff import backoff, make_due
from tests.fake.test_idle_capacity_events import clock, held_fleet, long_wait, release  # noqa: F401
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401
from tests.machine_guard_reference import LEGACY_GUARD


def door(service, monkeypatch, guard):
    """Use the real door decisions with synthetic machine/disk/start readings."""
    admission = service.policy["admission"]
    admission["machine_guard"] = deepcopy(LEGACY_GUARD)
    admission["disk"]["enabled"] = guard == "disk"
    admission["ramp"] = {"enabled": guard == "ramp", "per_minute": 2, "window_s": 30}
    machine = {"load1": 0, "load5": 0, "cpus": 18, "memory_pressure": 1}
    disk = [100]
    starts = []
    monkeypatch.setattr(service, "_machine_reading", lambda: dict(machine))
    monkeypatch.setattr("subfleet.disk.free_bytes", lambda path: int(disk[0] * GB))
    monkeypatch.setattr(service, "_ramp_rows", lambda since, **options: list(starts))

    def hold():
        if guard == "machine":
            machine.update(load1=400, load5=400, memory_pressure=4)
        elif guard == "disk":
            disk[0] = 39
        else:
            starts.append({"attempt_id": "synthetic-start", "kind": "dispatch", "reserved_at": utcnow()})

    def clear():
        machine.update(load1=0, load5=0, memory_pressure=1)
        disk[0] = 100
        # A ramp's real 30-second rolling window clears when the clock advances.

    return hold, clear


@pytest.mark.parametrize("guard", ["machine", "disk", "ramp"])
@pytest.mark.parametrize("control", ["long", "short-operator", "short-provider"])
def test_release_news_survives_temporary_door_hold(held_fleet, monkeypatch, guard, control):
    service, harness, closure, advance = held_fleet
    hold, clear = door(service, monkeypatch, guard)
    if control == "short-operator":
        monkeypatch.setattr(scheduler, "capacity_recheck_ceiling", lambda decision: 30)
    elif control == "short-provider":
        with service.store.transaction("fixture.provider_limit") as tx:
            tx.execute("UPDATE closures SET reason=? WHERE closure_id=?",
                       (ClosureReason.PROVIDER_LIMIT.value, closure))
    job = long_wait(service, harness)
    assert backoff(service, job) == (600 if control == "long" else 30)
    clock_before = service.store.get_job(job)["next_check_at"]
    checked_before = service._capacity_waits[job]["checked_at"]
    hold()
    release(service, closure)
    service._admit()
    expected = "machine-busy" if guard == "machine" else guard
    assert service._holds[job]["reason"] == expected
    assert service.store.get_job(job)["next_check_at"] == clock_before
    assert service._capacity_waits[job]["checked_at"] == checked_before
    service._admit()  # another held pass must retain the coalesced news too
    clear()
    advance(31)
    service._admit()
    assert [row["state"] for row in service.store.list_attempts(job)] == ["reserved"]


def test_release_news_survives_temporary_conversation_hold(held_fleet, monkeypatch):
    service, harness, closure, advance = held_fleet
    blocked = [False]
    monkeypatch.setattr(service.conversations, "admission_hold", lambda job:
                        {"reason": "conversation-blocked"} if blocked[0] else None)
    job = submit_turn(service, harness, 1)
    service._admit_turns()
    service._capacity_waits[job] = {**service._capacity_waits[job], "rechecks": 9}
    make_due(service, job)
    service._admit_turns()
    assert backoff(service, job) == 600
    blocked[0] = True
    release(service, closure)
    service._admit_turns()
    assert service._holds[job]["reason"] == "conversation-blocked"
    blocked[0] = False
    advance(31)
    service._admit_turns()
    assert [row["state"] for row in service.store.list_attempts(job)] == ["reserved"]


def test_release_news_survives_temporary_workspace_reaper_skip(held_fleet, monkeypatch):
    service, harness, closure, advance = held_fleet
    job = long_wait(service, harness)
    skipped = [{job}]
    monkeypatch.setattr(service, "_reap_push_workspaces", lambda holds: skipped[0])
    release(service, closure)
    service._admit()
    skipped[0] = set()
    advance(31)
    service._admit()
    assert [row["state"] for row in service.store.list_attempts(job)] == ["reserved"]


@pytest.mark.parametrize("finish", ["consume", "end", "different-wait", "probe-wait"])
def test_saved_news_coalesces_and_clears_with_its_wait(held_fleet, monkeypatch, finish):
    service, harness, closure, advance = held_fleet
    hold, clear = door(service, monkeypatch, "machine")
    job = long_wait(service, harness)
    hold()
    release(service, closure)
    for _ in range(7):
        service.policy_digest += ":change"
        service._admit()
        assert service._capacity_news == {job: True}
    if finish == "end":
        service.store.update_job(job, state="cancelled")
    elif finish == "different-wait":
        service.store.update_job(job, wait_reason="route")
    elif finish == "probe-wait":
        service._capacity_waits[job] = {**service._capacity_waits[job], "expedite": False}
    else:
        clear()
        advance(31)
    service._admit()
    assert not service._capacity_news
    if finish == "consume":
        assert [row["state"] for row in service.store.list_attempts(job)] == ["reserved"]


def test_release_news_survives_ramp_recheck_after_clock_moves_back(held_fleet, monkeypatch):
    """A start expired at the door can return to the window before reservation."""
    service, harness, closure, advance = held_fleet
    hold, _ = door(service, monkeypatch, "ramp")
    job = long_wait(service, harness)
    clock_before = service.store.get_job(job)["next_check_at"]
    hold()
    advance(31)
    workspace = service._workspace
    jump = [True]

    def backwards_during_preparation(candidate):
        result = workspace(candidate)
        if candidate["job_id"] == job and jump[0]:
            jump[0] = False
            advance(-20)
        return result

    monkeypatch.setattr(service, "_workspace", backwards_during_preparation)
    release(service, closure)
    service._admit()
    assert service._holds[job]["reason"] == "ramp"
    assert service.store.get_job(job)["next_check_at"] == clock_before
    advance(31)
    service._admit()
    assert [row["state"] for row in service.store.list_attempts(job)] == ["reserved"]
