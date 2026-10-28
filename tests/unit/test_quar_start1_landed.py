"""D-QUAR-START1 (a)'s bound gets a production record (review r3 of #250, check 5).

`START_GRACE_MAX_S` (60 s) rests on one incident: a guardian that took about 31 s to its
provider under load 60 to 100 (2026-10-26). The 39 samples measured for it never came
near that. So when an attempt that start grace extended (`attempt.start_extended`) later
has its `start.json` found, the daemon records `attempt.start_landed` on that attempt,
once: `seconds` from the deadline's start to the read that found it, and
`deadline_start`, which is `launch`, or `restart` when the deadline began at a restarted
daemon's first look (it lives in daemon memory). The daemon keeps no count of
extensions, so the event has none. An attempt never extended records nothing, and
nothing the daemon does changes.

On the simulated host (`tests/kill4_world.py`) and a fake clock: the daemon's start grace,
census and abandon run unchanged.
"""

from __future__ import annotations

import json

import pytest

from subfleet import daemon as daemon_module
from subfleet.contracts import START_GRACE_S
from tests.unit.test_daemon_settle import ATTEMPT, JOB, STARTED, attempt, daemon  # noqa: F401
from tests.unit.test_kill4_fix2b import MAX_S, START, publish, starting
from tests.unit.test_kill4_review_r3 import restart, running_guardian
from tests.unit.test_kill4_terminal_containment import kinds, world  # noqa: F401

LAUNCH = 1000.0


def landed(daemon) -> list[dict]:
    """The records, without `add_event`'s bare audit row of the same kind."""
    return [data for row in daemon.store.list_events(JOB) if row["kind"] == "attempt.start_landed"
            if (data := json.loads(row["data_json"] or "{}"))]


def record(seconds: float, deadline_start: str = "launch") -> dict:
    return {"seconds": seconds, "deadline_start": deadline_start, "start_grace_s": START_GRACE_S,
            "start_grace_max_s": MAX_S}


@pytest.fixture
def clock(monkeypatch) -> list[float]:
    """The daemon's monotonic clock, set by the test; the attempt launched at `LAUNCH`."""
    now = [LAUNCH]
    monkeypatch.setattr(daemon_module.time, "monotonic", lambda: now[0])
    return now


def launched(daemon) -> None:
    """A `starting` attempt launched at `LAUNCH`: its deadline is start grace away."""
    starting(daemon, expired_s=-START_GRACE_S)
    assert daemon._starting_deadlines[ATTEMPT] == LAUNCH + START_GRACE_S


def look(daemon) -> None:
    """One pass over the attempt, due."""
    daemon._inspect_next.pop(ATTEMPT, None)
    daemon.__dict__.setdefault("_cwd_wait_next", {}).pop(ATTEMPT, None)
    daemon._process_attempt(ATTEMPT)


def extended(daemon, clock) -> None:
    """Start grace expires 1 s ago with the guardian alive and running: it is extended."""
    clock[0] = LAUNCH + START_GRACE_S + 1
    look(daemon)
    assert attempt(daemon)["state"] == "starting" and "attempt.start_extended" in kinds(daemon)
    assert not landed(daemon)


@pytest.mark.parametrize("read", ["next-look", "after-the-census"])
def test_a_start_that_lands_after_an_extension_is_recorded_once_with_its_seconds(daemon, world, clock, read):
    """The guardian publishes `start.json` 20 s after its launch, 10 s past start grace.
    `next-look`: the pass's first read finds it. `after-the-census`: it lands while start
    grace's census runs, and the read after that census finds it; the next pass's first
    read finds it again, and the attempt is recorded once."""
    running_guardian(world)
    launched(daemon)
    extended(daemon, clock)
    clock[0] = LAUNCH + 20
    if read == "next-look":
        publish(daemon, "start.json", START)
    else:
        contain = daemon._contain

        def landing(a):
            census = contain(a)
            publish(daemon, "start.json", START)
            return census
        daemon._contain = landing
    look(daemon)
    if read == "after-the-census":
        assert attempt(daemon)["state"] == "starting"           # taken from the top next tick
        daemon._contain = contain
        clock[0] = LAUNCH + 21
        look(daemon)
    assert attempt(daemon)["state"] == "running" and not world.signals
    look(daemon)
    assert landed(daemon) == [record(20.0)], landed(daemon)


def test_a_start_that_lands_within_start_grace_records_nothing(daemon, world, clock):
    running_guardian(world)
    launched(daemon)
    clock[0] = LAUNCH + 5
    look(daemon)
    publish(daemon, "start.json", START)
    clock[0] = LAUNCH + 6
    look(daemon)
    assert attempt(daemon)["state"] == "running"
    assert "attempt.start_extended" not in kinds(daemon) and not landed(daemon)


@pytest.mark.parametrize("landing", ["at-its-first-look", "after-its-first-look"])
def test_a_restart_between_the_extension_and_the_landing_records_the_restarts_first_look(daemon, world, clock,
                                                                                        landing):
    """The deadline lives in daemon memory: a restarted daemon's starts at its first look,
    at `LAUNCH + 30` here. Found there, `start.json` landed 0 s after it; found 7 s later,
    7 s. Either way the record says the deadline began at the restart, not the launch."""
    running_guardian(world)
    launched(daemon)
    extended(daemon, clock)
    restart(daemon)
    clock[0] = LAUNCH + 30
    if landing == "at-its-first-look":
        publish(daemon, "start.json", START)
    look(daemon)
    if landing == "after-its-first-look":
        assert attempt(daemon)["state"] == "starting" and not landed(daemon)
        assert daemon._starting_deadlines[ATTEMPT] == LAUNCH + 30 + START_GRACE_S
        clock[0] = LAUNCH + 37
        publish(daemon, "start.json", START)
        look(daemon)
    assert attempt(daemon)["state"] == "running" and not world.signals
    seconds = 0.0 if landing == "at-its-first-look" else 7.0
    assert landed(daemon) == [record(seconds, "restart")], landed(daemon)


def test_a_start_json_the_abandons_last_read_finds_after_an_extension_is_recorded(daemon, world, clock):
    """Past the bound, the abandon is written and `start.json` read for the last time
    (`_abandon_start`); found there, 61 s after the launch, it is a landing past the bound:
    the record the bound most needs."""
    running_guardian(world)
    launched(daemon)
    extended(daemon, clock)
    clock[0] = LAUNCH + MAX_S + 1
    publish(daemon, "start.json", START)
    assert daemon._abandon_start(attempt(daemon), "starting-no-receipt") is False
    assert landed(daemon) == [record(MAX_S + 1)], landed(daemon)


def test_a_start_json_the_abandons_last_read_finds_without_an_extension_records_nothing(daemon, world, clock):
    """A stopped guardian is never extended: its last read finding `start.json` is no landing
    of an extended start."""
    running_guardian(world).stopped = True
    launched(daemon)
    clock[0] = LAUNCH + START_GRACE_S + 1
    publish(daemon, "start.json", START)
    assert daemon._abandon_start(attempt(daemon), "starting-no-receipt") is False
    assert "attempt.start_extended" not in kinds(daemon) and not landed(daemon)
