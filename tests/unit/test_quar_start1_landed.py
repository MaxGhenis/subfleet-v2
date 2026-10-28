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
from tests.unit.test_kill4_fix2b import (CONT, KILL, MAX_S, START, TERM, abandon_seen_at_each_signal, publish,
                                         settle, starting)
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


# D-QUAR-START1 D4 (#274) beside the record: the extensions D4 admits are extensions like
# any other, so their landing is recorded once, and past the bound they end as C-5.6 says.

READER = 4243
STRANGER = 7000
D4_SHAPES = ["stranger-unread", "identity-reader", "both"]


def d4_shape(world, shape: str):
    """What D4 admits beside a running guardian: a stranger whose marker read is refused on
    every census, the guardian's own identity reader (an unmarked running child in its
    group), or both. Returns the reader, if any."""
    if shape in ("stranger-unread", "both"):
        world.add(STRANGER, 1, STRANGER)
        world.fail_reads[STRANGER] = 10 ** 6
    return world.add(READER, 4242, 4242) if shape in ("identity-reader", "both") else None


def extended_by_d4(daemon, clock, shape: str) -> None:
    """Start grace expires with D4's shape beside the guardian, and is extended for it."""
    extended(daemon, clock)
    granted = [data for row in daemon.store.list_events(JOB) if row["kind"] == "attempt.start_extended"
               if (data := json.loads(row["data_json"] or "{}"))]
    assert len(granted) == 1, granted
    assert granted[0].get("markers_unread") == ([STRANGER] if shape != "identity-reader" else None), granted
    assert granted[0].get("identity_readers") == ([READER] if shape != "stranger-unread" else None), granted


@pytest.mark.parametrize("shape", D4_SHAPES)
def test_a_start_d4_extended_that_lands_is_recorded_once(daemon, world, clock, shape):
    running_guardian(world)
    reader = d4_shape(world, shape)
    launched(daemon)
    extended_by_d4(daemon, clock, shape)
    clock[0] = LAUNCH + 15
    look(daemon)                                        # extended again, at the census's pacing
    assert attempt(daemon)["state"] == "starting" and not world.signals and not landed(daemon)
    if reader is not None:
        reader.alive = False
    clock[0] = LAUNCH + 20
    publish(daemon, "start.json", START)
    look(daemon)
    assert attempt(daemon)["state"] == "running" and not world.signals
    look(daemon)
    assert landed(daemon) == [record(20.0)], landed(daemon)


@pytest.mark.parametrize("shape", D4_SHAPES)
def test_a_start_d4_extended_has_its_group_killed_at_once_at_the_bound_and_records_nothing(
        daemon, world, clock, monkeypatch, shape):
    """At exactly `START_GRACE_MAX_S` from the launch, the census still meets D4's shape;
    the extension ends there, the abandon is written first and the group KILLed at once,
    with no TERM (C-5.6). The kill protocol's own censuses read the stranger whole, as
    measured, so its end verifies. No `start.json` landed, so no record."""
    guardian = running_guardian(world)
    d4_shape(world, shape)
    launched(daemon)
    extended_by_d4(daemon, clock, shape)
    contain, met = daemon._contain, []

    def at_the_bound(a):
        census = contain(a)
        if not met:
            met.append(census)
            world.fail_reads.clear()
        return census
    daemon._contain = at_the_bound
    clock[0] = LAUNCH + MAX_S
    seen = abandon_seen_at_each_signal(daemon, world, monkeypatch)
    a = settle(daemon)
    assert met and (STRANGER in met[0].markers_unread) == (shape != "identity-reader")
    assert seen and all(seen), seen
    assert world.signals[0] == ("group", 4242, KILL), world.signals
    assert not [s for s in world.signals if s[2] in (TERM, CONT)], world.signals
    assert not guardian.alive and STRANGER not in world.signalled()
    assert (a["state"], a["outcome_detail"]) == ("failed", "starting-no-receipt"), a
    assert not landed(daemon)


@pytest.mark.parametrize("shape", D4_SHAPES)
def test_a_start_d4_extended_found_by_the_abandons_last_read_is_recorded_once(daemon, world, clock, shape):
    running_guardian(world)
    d4_shape(world, shape)
    launched(daemon)
    extended_by_d4(daemon, clock, shape)
    clock[0] = LAUNCH + MAX_S + 1
    publish(daemon, "start.json", START)
    for _ in range(2):
        assert daemon._abandon_start(attempt(daemon), "starting-no-receipt") is False
    assert landed(daemon) == [record(MAX_S + 1)], landed(daemon)
