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

from subfleet import protocol
from tests.frontend.conftest import needs_swift, write_json
from tests.frontend.daemon_harness import (
    STEER_CAPS, after_submitted, go_live_when_submitted, record_state, steer_frames, steer_requests,
)
from tests.frontend.test_core_steer import (  # noqa: F401 (daemon and harness are fixtures)
    create_step, daemon, harness, ids, run_steps,
)

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
        # The step's bound at the harness's 3 s answers: prerequisite, submit, the tries, margin.
        assert result["deadline_s"] == 3 + 3 + 7.5 + TRIES * 3 + 7.5 and result["waited_s"] < result["deadline_s"]
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
    before the deadline. The wait before the third try is cut to the 0.5 s left, and no pump
    runs at the deadline (#265 review r1: it is checked before every pump)."""
    harness, server = daemon
    host, steered = ids(2)
    lagging_host(harness, host, steered, "catching-up", tries=TRIES)
    result = step_14(core_probe, tmp_path, harness, server, host, steered, deadline=1.0)
    steer = result["steer"]
    assert result["taken"] is False, json.dumps(result, sort_keys=True)
    assert (steer["state"], steer["attempts"], steer["failure"]["retryable"]) == ("queued", 2, True)
    assert (result["pumps"], result["waited_s"], result["deadline_s"]) == (2, 1.0, 1.0)
    assert result["pauses"] == [0.5, 0.5]
    assert steer_requests(server) == [steered] * 2


# --- step 14's deadline is an elapsed bound from the step's start (#265 review r1, P3) ----------
#
# On the probe's clock, with the client's 15 s answer deadline: a scripted daemon whose
# answers take time on that clock (`cost`), or never come (`timeout`, which takes the whole
# answer deadline the client gave the exchange). `step-start` marks where step 14 began:
# before the host's prerequisite, which `advance` stands for here.

ANSWER_S = 15.0                             # DaemonClient.baseTimeout
STEP_DEADLINE_S = 120.0                     # CoreProbeLive.swift `steerStepDeadline`, at 15 s


def ok(result: dict) -> dict:
    return json.loads(protocol.encode(protocol.ok("", result)))


def clocked_step_14(core_probe, tmp_path, harness, *, prerequisite: float, submit_cost: float,
                    taken: bool = False) -> dict:
    """Step 14 on the probe's clock: the step begins, the prerequisite takes `prerequisite`
    seconds, the steered message is journaled with its steer (naming the host), and the
    step's wait runs. The daemon answers the submit after `submit_cost` seconds; it takes
    the steer at once when `taken`, else each try waits out the whole answer deadline.
    Returns the wait's result."""
    host, steered = ids(2)
    cid = harness.create()["conversation_id"]
    receipt = harness.submit(cid, "also say steered", message_id=steered)
    steering = {**receipt, "state": "steering", "state_reason": f"steer:{host}"}
    answers = [{"op": "message.submit", "answer": ok(receipt), "cost": submit_cost},
               *([{"op": "message.steer", "answer": ok(steering)}] if taken
                 else [{"op": "message.steer", "timeout": True}] * TRIES)]
    script = "script:" + str(write_json(tmp_path / "clocked.json", answers))
    out = run_steps(core_probe, tmp_path, script, [
        {"do": "know_chain", "conversation": cid, "last": None},
        {"do": "client-timeout", "seconds": ANSWER_S},
        {"do": "step-start"},
        {"do": "advance", "seconds": prerequisite},
        {"do": "submit", "conversation": cid, "message_id": steered, "text": "also say steered",
         "steer": True, "into": host},
        {"do": "settle-steer", "key": steered},
    ])
    result = out["results"][-1]
    assert result["do"] == "settle-steer" and "error" not in result, json.dumps(out, sort_keys=True)
    return result


#: (prerequisite, the submit's answer time, then what the step's pumps and waits come to)
CLOCKED = {
    # The review's virtual-time input: a 15 s answer ahead of the steer, then five steer
    # tries that each wait out the 15 s answer deadline (97.5 s past 90 s at 44d4c8b49).
    # Inside 120 s the outbox gets all five tries and gives the steer up itself.
    "review-input": (0.0, 15.0, {"step_s": 97.5, "pumps": 5, "pauses": [0.5, 1.0, 2.0, 4.0],
                                 "state": "refused", "attempts": 5}),
    # Time spent inside a pump: the fifth try starts at 107.5 s and its exchange may wait
    # only the 12.5 s left, so the step ends at its deadline exactly, not 2.5 s past it.
    "time-in-pumps": (40.0, 0.0, {"step_s": 120.0, "pumps": 5, "pauses": [0.5, 1.0, 2.0, 4.0],
                                  "state": "refused", "attempts": 5}),
    # The deadline falls in a backoff: the fourth try ends at 117.5 s, the wait before the
    # fifth is cut to the 2.5 s left, and no pump runs at or after the deadline.
    "deadline-in-backoff": (54.0, 0.0, {"step_s": 120.0, "pumps": 4, "pauses": [0.5, 1.0, 2.0, 2.5],
                                        "state": "queued", "attempts": 4}),
}


@pytest.mark.parametrize("case", CLOCKED)
def test_step_14_judges_its_steer_by_its_deadline_counted_from_the_step_start(core_probe, tmp_path, harness,
                                                                              case):
    """C-29.7 (#265 review r1, P3): step 14's deadline counts from the step's start, its
    prerequisite and the steered message's submit included. It is checked before every pump
    and every wait, each wait is cut to the time left, and each exchange a pump makes waits
    at most until the deadline: the step never runs past it, however its time is spent."""
    prerequisite, submit_cost, expect = CLOCKED[case]
    result = clocked_step_14(core_probe, tmp_path, harness, prerequisite=prerequisite, submit_cost=submit_cost)
    detail = json.dumps(result, sort_keys=True)
    assert result["step_s"] <= result["deadline_s"], detail
    assert result["deadline_s"] == STEP_DEADLINE_S, detail
    assert result["step_s"] == pytest.approx(expect["step_s"]), detail
    assert (result["pumps"], result["pauses"]) == (expect["pumps"], pytest.approx(expect["pauses"])), detail
    steer = result["steer"]
    assert (steer["state"], steer["attempts"]) == (expect["state"], expect["attempts"]), detail
    assert result["taken"] is False and steer["receipt"] is None, detail
    assert steer["failure"]["reason"] == ("no-answer" if expect["state"] == "refused" else None), detail


def test_step_14_takes_an_answered_steer_with_one_pump_and_no_wait(core_probe, tmp_path, harness):
    """A turn that takes the steer at once: one pump sends the submit and the steer, the
    steer is acknowledged and taken, and the step waits for nothing."""
    result = clocked_step_14(core_probe, tmp_path, harness, prerequisite=0.0, submit_cost=0.0, taken=True)
    detail = json.dumps(result, sort_keys=True)
    assert result["taken"] is True, detail
    assert (result["pumps"], result["pauses"], result["waited_s"], result["step_s"]) == (1, [], 0, 0), detail
    assert (result["steer"]["state"], result["steer"]["attempts"]) == ("acknowledged", 1), detail
