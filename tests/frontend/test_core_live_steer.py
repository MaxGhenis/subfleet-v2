"""Step 14 of the live probe (`CoreProbeLive.swift`) against the daemon's own steer op,
with the running turn unable to take a steer at the first try (D-STEER-PUMP1).

`test_core_live.py` cannot put the daemon in that state on purpose, and cannot run under
a Subfleet lane at all (the person-only peer check refuses a caller under a guardian), so
these run the step's outbox sequence through the core probe's `outbox` steps against the
real `ConversationService` and a real `TurnRunner` (`daemon_harness.py`). The runner is
made unready the way load leaves one: still catching up with the provider's output
(`replay_caught_up`), or its driver not yet told the provider's capabilities (no
`system init` read, which `SUBFLEET_FAKE_LATE_INIT_S` makes the fake Claude hold back).
The daemon then answers `not-steerable` (C-24.9, C-25.2).
"""

from __future__ import annotations

import pytest

from tests.frontend.conftest import needs_swift
from tests.frontend.daemon_harness import STEER_CAPS, after_submitted, go_live_when_submitted, steer_frames, steer_requests
from tests.frontend.test_core_steer import create_step, daemon, ids, run_steps  # noqa: F401 (daemon is a fixture)

pytestmark = needs_swift

CAUSES = ("catching-up", "no-system-init-yet")


def unready(runner, cause: str, ready: bool) -> None:
    """Make the host's runner able, or unable, to take a steer, as `cause` says."""
    if cause == "catching-up":
        runner.replay_caught_up = ready
    else:
        runner.driver.capabilities = set(STEER_CAPS["capabilities"]) if ready else set()


def lagging_host(harness, host: str, steered: str, cause: str, tries: int) -> dict:
    """`host` runs as a live turn whose runner cannot take a steer for the first `tries`
    tries of `steered`'s steer, and can from then on. Returns the runners."""
    runners = go_live_when_submitted(harness, host)
    after_submitted(harness, steered, lambda receipt: unready(runners[host], cause, tries == 0))
    real, asked = harness.service.op_message_steer, []

    def steer(args, peer):
        try:
            return real(args, peer)
        finally:
            asked.append(args["message_id"])
            unready(runners[host], cause, len(asked) >= tries)
    harness.service.op_message_steer = steer
    return runners


def taken(steer: dict) -> bool:
    """Step 14's check: the outbox has the daemon's receipt, steering or steered."""
    return steer["state"] == "acknowledged" and (steer["receipt"] or {}).get("state") in ("steering", "steered")


@pytest.mark.xfail(strict=True, reason="D-STEER-PUMP1: step 14 steers without `into` and reads it after one pump")
@pytest.mark.parametrize("cause", CAUSES)
def test_step_14_takes_a_steer_the_running_turn_could_not_take_at_first(core_probe, tmp_path, daemon, cause):
    """The turn is running and accepted, and can take a steer from the second try on, as a
    runner that lagged under load can. Step 14 still has the daemon take the steer."""
    harness, server = daemon
    host, steered = ids(2)
    runners = lagging_host(harness, host, steered, cause, tries=1)
    out = run_steps(core_probe, tmp_path, server.path, [
        create_step(harness, "req-live"),
        {"do": "submit", "conversation": "@draft:req-live", "message_id": host, "text": "steer into me [fake:slow]"},
        {"do": "engine-pump"},
        # Step 14 as 946ea9f96 runs it: the steer journaled with no `into`, one pump, then read.
        {"do": "submit", "conversation": "@conv:req-live", "message_id": steered, "text": "also say steered",
         "steer": True},
        {"do": "engine-pump"},
    ])
    (steer,) = out["steers"]
    assert taken(steer), steer
    assert steer_requests(server) == [steered] * steer["attempts"]
    assert steer_frames(runners[host]) == [steered]
