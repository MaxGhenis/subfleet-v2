"""Step 14 of the live probe (`CoreProbeLive.swift`) against the daemon's own steer op,
with the running turn unable to take a steer at first (D-STEER-PUMP1).

`test_core_live.py` cannot put the daemon in that state on purpose, and cannot run under
a Subfleet lane at all (the person-only peer check refuses a caller under a guardian), so
these run the step through the core probe's `outbox` steps against the real
`ConversationService` and a real `TurnRunner` (`daemon_harness.py`). `settle-steer` is the
step's own wait and check (`settleSteer`, `steerTaken`), on the probe's clock. The runner
is made unready the way load leaves one: still catching up with the provider's output
(`replay_caught_up`), or its driver not yet told the provider's capabilities (no
`system init` read, which `SUBFLEET_FAKE_LATE_INIT_S` makes the fake Claude hold back).
The daemon then answers `not-steerable` (C-24.9, C-25.2), and a steer that names its turn
is asked again at 0.5 s doubling, five tries in all (C-29.7).
"""

from __future__ import annotations

import json

import pytest

from tests.frontend.conftest import needs_swift
from tests.frontend.daemon_harness import (
    STEER_CAPS, after_submitted, go_live_when_submitted, record_state, steer_frames, steer_requests,
)
from tests.frontend.test_core_steer import create_step, daemon, ids, run_steps  # noqa: F401 (daemon is a fixture)

pytestmark = needs_swift

CAUSES = ("catching-up", "no-system-init-yet")
TRIES = 5                                   # Outbox.steerAttempts


def unready(runner, cause: str, ready: bool) -> None:
    """Make the host's runner able, or unable, to take a steer, as `cause` says."""
    if cause == "catching-up":
        runner.replay_caught_up = ready
    else:
        runner.driver.capabilities = set(STEER_CAPS["capabilities"]) if ready else set()


def lagging_host(harness, host: str, steered: str, cause: str, tries: int,
                 after_first=None) -> dict:
    """`host` runs as a live turn whose runner cannot take a steer for the first `tries`
    tries of `steered`'s steer, and can from then on; `after_first()` runs after the first
    try. Returns the runners."""
    runners = go_live_when_submitted(harness, host)
    after_submitted(harness, steered, lambda receipt: unready(runners[host], cause, tries == 0))
    real, asked = harness.service.op_message_steer, []

    def steer(args, peer):
        try:
            return real(args, peer)
        finally:
            asked.append(args["message_id"])
            unready(runners[host], cause, len(asked) >= tries)
            if after_first is not None and len(asked) == 1:
                after_first()
    harness.service.op_message_steer = steer
    return runners


def step_14(core_probe, tmp_path, harness, server, host: str, steered: str, *, into: bool = True,
            deadline: float | None = None) -> dict:
    """Step 14's outbox sequence: the host's turn, the steer journaled with its submit
    (naming the host, as the probe and the app send it, unless `into` is false), then the
    step's wait and check. Returns that step's result."""
    settle = {"do": "settle-steer", "key": steered, **({"deadline": deadline} if deadline is not None else {})}
    out = run_steps(core_probe, tmp_path, server.path, [
        create_step(harness, "req-live"),
        {"do": "submit", "conversation": "@draft:req-live", "message_id": host, "text": "steer into me [fake:slow]"},
        {"do": "engine-pump"},
        {"do": "submit", "conversation": "@conv:req-live", "message_id": steered, "text": "also say steered",
         "steer": True, **({"into": host} if into else {})},
        settle,
    ])
    result = out["results"][-1]
    assert result["do"] == "settle-steer" and "error" not in result, json.dumps(result, sort_keys=True)
    return result


def backoffs(tries: int) -> float:
    """The outbox's waits before the second to the (tries + 1)th try: 0.5 s doubling."""
    return sum(0.5 * 2 ** n for n in range(tries))


@pytest.mark.parametrize("cause", CAUSES)
def test_step_14_takes_a_steer_the_running_turn_could_not_take_at_first(core_probe, tmp_path, daemon, cause):
    """The turn is running and accepted, and can take a steer from the second try on, as a
    runner that lagged under load can. Step 14 still has the daemon take the steer: asked
    again 0.5 s later, once more, and written to the provider once. (As 946ea9f96 ran the
    step, without `into` and judged after one pump, it read the steer refused, receipt null.)"""
    harness, server = daemon
    host, steered = ids(2)
    runners = lagging_host(harness, host, steered, cause, tries=1)
    result = step_14(core_probe, tmp_path, harness, server, host, steered)
    steer = result["steer"]
    assert result["taken"] is True, json.dumps(result, sort_keys=True)
    assert (steer["state"], steer["attempts"], steer["receipt"]["state"]) == ("acknowledged", 2, "steering")
    assert (result["pumps"], result["waited_s"]) == (2, 0.5)
    assert steer_requests(server) == [steered] * 2
    assert steer_frames(runners[host]) == [steered]
    assert harness.store.message(steered)["state"] == "steering"


@pytest.mark.parametrize("tries", range(TRIES + 2))
def test_step_14_waits_out_each_retry_and_judges_the_last_answer(core_probe, tmp_path, daemon, tries):
    """Every number of tries the turn cannot take the steer for, 0 to past the outbox's five:
    one pump per try, waiting exactly the outbox's backoff before each (never spinning).
    Under five, the steer is taken at the try after; from five on, it is refused
    `not-steerable` after five tries, the check fails, and the wait ends there, long before
    its deadline."""
    harness, server = daemon
    host, steered = ids(2)
    runners = lagging_host(harness, host, steered, "catching-up", tries=tries)
    result = step_14(core_probe, tmp_path, harness, server, host, steered)
    steer, asked = result["steer"], min(tries + 1, TRIES)
    assert result["pumps"] == steer["attempts"] == asked, json.dumps(result, sort_keys=True)
    assert result["waited_s"] == backoffs(asked - 1)
    assert steer_requests(server) == [steered] * asked
    assert all(r["args"] == {"message_id": steered, "into": host} for r in server.requests if r["op"] == "message.steer")
    if tries < TRIES:
        assert result["taken"] is True and steer["state"] == "acknowledged"
        assert steer_frames(runners[host]) == [steered]
        assert harness.store.message(steered)["state"] == "steering"
    else:
        assert result["taken"] is False and steer["state"] == "refused"
        assert steer["failure"]["reason"] == "not-steerable" and steer["failure"]["retryable"] is False
        assert result["deadline_s"] == 7.5 + TRIES * 3 + 7.5 and result["waited_s"] < result["deadline_s"]
        assert steer_frames(runners[host]) == []
        assert harness.store.message(steered)["state"] == "queued"


def test_a_steer_without_into_is_refused_at_once_and_no_wait_takes_it(core_probe, tmp_path, daemon):
    """Why step 14 names its turn: without `into`, the outbox closes a `not-steerable` steer
    at once (C-29.7 asks again only for a steer that names its turn), so waiting cannot
    help. This is how the step failed on #262 (run 40273357312): its detail, the receipt,
    read null."""
    harness, server = daemon
    host, steered = ids(2)
    runners = lagging_host(harness, host, steered, "catching-up", tries=1)
    result = step_14(core_probe, tmp_path, harness, server, host, steered, into=False)
    steer = result["steer"]
    assert result["taken"] is False, json.dumps(result, sort_keys=True)
    assert (steer["state"], steer["attempts"], steer["receipt"], steer["into"]) == ("refused", 1, None, None)
    assert steer["failure"] == {"code": 7, "reason": "not-steerable", "retryable": False,
                                "message": "not-steerable: the provider cannot take a steer now"}
    assert (result["pumps"], result["waited_s"]) == (1, 0)
    assert steer_requests(server) == [steered] and steer_frames(runners[host]) == []


def test_a_steer_refused_for_good_fails_the_check_however_long_step_14_waits(core_probe, tmp_path, daemon):
    """The turn cannot take the steer at the first try and has ended by the second: the
    daemon refuses the retry `no-live-turn`, the steer closes, and the check fails at once."""
    harness, server = daemon
    host, steered = ids(2)
    lagging_host(harness, host, steered, "catching-up", tries=1,
                 after_first=lambda: record_state(harness, host, "complete"))
    result = step_14(core_probe, tmp_path, harness, server, host, steered)
    steer = result["steer"]
    assert result["taken"] is False, json.dumps(result, sort_keys=True)
    assert (steer["state"], steer["attempts"], steer["failure"]["reason"]) == ("refused", 2, "no-live-turn")
    assert (result["pumps"], result["waited_s"]) == (2, 0.5)
    assert harness.store.message(steered)["state"] == "queued"


def test_step_14_stops_at_its_deadline_and_judges_a_steer_still_waiting(core_probe, tmp_path, daemon):
    """A deadline shorter than the outbox's tries ends the wait there, with the steer still
    waiting to be asked again: the check fails and says so, after one pump per retry time
    and one at the deadline."""
    harness, server = daemon
    host, steered = ids(2)
    lagging_host(harness, host, steered, "catching-up", tries=TRIES)
    result = step_14(core_probe, tmp_path, harness, server, host, steered, deadline=1.0)
    steer = result["steer"]
    assert result["taken"] is False, json.dumps(result, sort_keys=True)
    assert (steer["state"], steer["attempts"], steer["failure"]["retryable"]) == ("queued", 2, True)
    assert (result["pumps"], result["waited_s"], result["deadline_s"]) == (3, 1.0, 1.0)
    assert steer_requests(server) == [steered] * 2
