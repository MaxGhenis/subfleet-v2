"""The queue tray's Withdraw through the app's engine, and the store state
around the tray (C-29.7; design §12).

Withdraw on a queued row is `message.cancel`. The daemon answers `dispatching`
while the dispatcher binds the message to its turn job, and the engine asks
again after 0.1, 0.25, 0.5 and 1 s before it shows the refusal. It answers
`too-late` once the message has left the queue; then `message.status` says
where it is, and only a message that is its own live turn (waiting, starting,
running, approval-needed) is stopped, with `turn.interrupt`. Any other state
is answered with its receipt: a second Withdraw, or one after the turn ended,
is not an error and stops nothing. A row with no receipt yet is withdrawn
through the outbox (D-22), which asks the daemon first when it may have it.

`queue-engine` runs `ConversationEngine.stop` and `withdrawSend` against a
scripted daemon (each op answers from its own queue of results and refusals)
and against the daemon's own conversation service on a socket
(`daemon_harness.ServiceServer`). The engine's pauses are recorded, never
slept. The property tests state the rule's invariants over generated answer
sequences, and compare the engine with a reference written from the rule.

`queue-state` folds receipts, local sends, withdrawals and event pages into
`ConversationStoreState` as the app does, and reads the focused conversation's
layout and what its view does with it.
"""

from __future__ import annotations

from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Callable
import uuid

from hypothesis import HealthCheck, event, given, settings, strategies as st
import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import ServiceHarness, ServiceServer
from tests.frontend.test_core_approval_reach import Log, ask

pytestmark = needs_swift

MID = "0b9f7c52-5d0e-4c61-9a57-3f0e1c2d4a10"
CID = "cv-queue-engine"
RETRIES = [0.1, 0.25, 0.5, 1]
# The states in which Withdraw's `too-late` path interrupts: the message is its own live turn.
LIVE = ["waiting", "starting", "running", "approval-needed"]
# Every other state `message.status` can report (`unknown`: an id the daemon has no message by).
NOT_LIVE = ["queued", "complete", "failed", "interrupted", "cancelled", "delivery-unknown", "unknown"]
PROPERTY = settings(max_examples=120, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


# MARK: - Scripted answers


def refusal(reason: str, text: str, fix: str | None = None, code: int = 2) -> dict:
    """A refusal as the daemon words it (service.py `ConversationError`: exit 2, `"<reason>: <text>"`)."""
    return {"error": {"code": code, "message": f"{reason}: {text}", "fix": fix}}


DISPATCHING = refusal("dispatching", "the message is being handed to its turn job", "send the cancel again in a moment")
TOO_LATE = refusal("too-late", "the provider may already have this message", "use turn.interrupt")
UNKNOWN_MESSAGE = refusal("unknown-message", "cancel of an unknown message needs its conversation_id")
TIMEOUT = {"timeout": True}


def not_running(state: str) -> dict:
    return refusal("not-running", f"the message is {state}")


def receipt(state: str, mid: str = MID, **extra) -> dict:
    return {"message_id": mid, "conversation_id": CID, "seq": 1, "origin": "person", "state": state, **extra}


def ok(result) -> dict:
    return {"result": result}


def withdrawn(mid: str = MID) -> dict:
    return ok(receipt("cancelled", mid, state_reason="withdrawn", stop_requested=True))


def status(state: str, mid: str = MID) -> dict:
    return ok({"messages": [receipt(state, mid)]})


def stopped(state: str = "running", mid: str = MID) -> dict:
    return ok(receipt(state, mid, stop_requested=True))


def cancel(mid: str = MID) -> dict:
    return {"do": "stop", "action": {"action": "cancel", "message_id": mid}}


def interrupt(mid: str = MID) -> dict:
    return {"do": "stop", "action": {"action": "interrupt", "message_id": mid}}


def engine(core_probe, steps: list[dict], answers: dict | None = None, **extra) -> dict:
    with tempfile.TemporaryDirectory(prefix="sf-qe-") as scratch:
        path = write_json(Path(scratch) / f"{uuid.uuid4().hex}.json",
                          {"answers": answers or {}, "steps": steps, **extra})
        return run_probe(core_probe, "queue-engine", path)


def one(core_probe, step: dict, answers: dict, **extra) -> dict:
    """One stop against the scripted daemon: its result, with the run's unused answers."""
    out = engine(core_probe, [step], answers, **extra)
    return {**out["results"][0], "unused": out["unused"]}


def ops(result: dict) -> list[str]:
    return [call.split(" ")[0] for call in result["calls"]]


# MARK: - message.cancel and its refusals


def test_c29_7_withdraw_cancels_a_queued_message_in_one_call(core_probe):
    """C-29.7 Withdraw of a queued message is one `message.cancel`; its receipt says withdrawn."""
    result = one(core_probe, cancel(), {"message.cancel": [withdrawn()]})
    assert "error" not in result
    assert result["action"] == {"action": "cancel", "message_id": MID} and result["label"] == "Withdraw"
    assert result["calls"] == [f"message.cancel {MID}"] and result["pauses"] == []
    assert result["receipt"] == receipt("cancelled", state_reason="withdrawn", stop_requested=True)
    assert result["unused"] == {}


def test_c29_7_withdraw_asks_again_while_the_dispatcher_binds_the_message(core_probe):
    """C-29.7 `dispatching` twice, then withdrawn: three cancels, after pauses of 0.1 and 0.25 s."""
    result = one(core_probe, cancel(), {"message.cancel": [DISPATCHING, DISPATCHING, withdrawn()]})
    assert "error" not in result
    assert result["calls"] == [f"message.cancel {MID}"] * 3
    assert result["pauses"] == [0.1, 0.25]
    assert result["receipt"]["state"] == "cancelled" and result["receipt"]["state_reason"] == "withdrawn"


def test_c29_7_withdraw_shows_dispatching_after_its_last_retry(core_probe):
    """C-29.7 `dispatching` every time: 1 + 4 cancels after pauses 0.1, 0.25, 0.5 and 1 s,
    then the daemon's refusal, unchanged; a sixth answer is never asked for."""
    result = one(core_probe, cancel(), {"message.cancel": [DISPATCHING] * 6})
    assert result["calls"] == [f"message.cancel {MID}"] * 5
    assert result["pauses"] == RETRIES
    assert result["error"] == {"kind": "daemon", "code": 2, "reason": "dispatching",
                               "message": DISPATCHING["error"]["message"],
                               "detail": "the message is being handed to its turn job",
                               "fix": "send the cancel again in a moment"}
    assert "receipt" not in result
    assert result["unused"] == {"message.cancel": 1}


@pytest.mark.parametrize("retries", [[], [0.2], [0.05, 3.0]])
def test_c29_7_the_dispatching_retries_are_the_engines_to_set(core_probe, retries):
    """C-29.7 One cancel per retry plus the first, each after its own pause, and no more."""
    result = one(core_probe, cancel(), {"message.cancel": [DISPATCHING] * 6}, dispatching_retries=retries)
    assert result["calls"] == [f"message.cancel {MID}"] * (1 + len(retries))
    assert result["pauses"] == retries
    assert result["error"]["reason"] == "dispatching"


@pytest.mark.parametrize("state", LIVE)
def test_c29_7_a_message_that_became_its_own_live_turn_is_interrupted(core_probe, state):
    """C-29.7 `too-late`, and `message.status` says the message is its own live turn:
    Withdraw stops it with `turn.interrupt` (the daemon's fix for that refusal)."""
    result = one(core_probe, cancel(), {"message.cancel": [TOO_LATE], "message.status": [status(state)],
                                        "turn.interrupt": [stopped(state)]})
    assert "error" not in result
    assert result["calls"] == [f"message.cancel {MID}", f"message.status {MID}", f"turn.interrupt {MID}"]
    assert result["receipt"] == receipt(state, stop_requested=True)
    assert result["pauses"] == [] and result["unused"] == {}


@pytest.mark.parametrize("state", NOT_LIVE)
def test_c29_7_a_message_that_left_the_queue_otherwise_is_not_interrupted(core_probe, state):
    """C-29.7 `too-late`, and the message has ended, was withdrawn already, or is in a
    state the app does not stop: its receipt comes back, nothing is interrupted, no error."""
    result = one(core_probe, cancel(), {"message.cancel": [TOO_LATE], "message.status": [status(state)],
                                        "turn.interrupt": [stopped()]})
    assert "error" not in result
    assert result["calls"] == [f"message.cancel {MID}", f"message.status {MID}"]
    assert result["receipt"] == receipt(state)
    assert result["unused"] == {"turn.interrupt": 1}


@pytest.mark.parametrize("listed", [[receipt("complete")], [receipt("running", "another-message"), receipt("complete")]],
                         ids=["alone", "after-another"])
def test_c29_7_a_turn_that_ends_before_the_interrupt_returns_how_it_ended(core_probe, listed):
    """C-29.7 Running when asked, ended when stopped: `not-running`, and the receipt
    `message.status` then gives for this message is returned, not an error, wherever
    the answer lists it."""
    result = one(core_probe, cancel(), {
        "message.cancel": [TOO_LATE], "message.status": [status("running"), ok({"messages": listed})],
        "turn.interrupt": [not_running("complete")]})
    assert "error" not in result
    assert ops(result) == ["message.cancel", "message.status", "turn.interrupt", "message.status"]
    assert result["receipt"] == receipt("complete")


@pytest.mark.parametrize("messages", [[], [receipt("running", "another-message")]], ids=["empty", "another"])
def test_c29_7_a_status_that_leaves_out_the_message_is_malformed(core_probe, messages):
    """C-29.7 After `too-late`, a `message.status` answer without the message is an
    unexpected answer, and nothing is interrupted."""
    result = one(core_probe, cancel(), {"message.cancel": [TOO_LATE], "message.status": [ok({"messages": messages})],
                                        "turn.interrupt": [stopped()]})
    assert result["error"] == {"kind": "malformed", "message": f"message.status left out {MID}"}
    assert ops(result) == ["message.cancel", "message.status"]


def test_c29_7_stop_on_a_running_turn_is_one_interrupt(core_probe):
    """C-29.7 Stop (a live turn) is `turn.interrupt` alone: no cancel, no status."""
    result = one(core_probe, interrupt(), {"turn.interrupt": [stopped()], "message.cancel": [withdrawn()]})
    assert "error" not in result
    assert result["action"] == {"action": "interrupt", "message_id": MID} and result["label"] == "Stop"
    assert result["calls"] == [f"turn.interrupt {MID}"]
    assert result["receipt"] == receipt("running", stop_requested=True)


@pytest.mark.parametrize("answers, calls", [
    ({"message.cancel": [UNKNOWN_MESSAGE]}, ["message.cancel"]),
    ({"message.cancel": [refusal("person-only", "only a person may do this", code=7)]}, ["message.cancel"]),
    ({"message.cancel": [refusal("internal", "the store is closed", code=1)]}, ["message.cancel"]),
    ({"message.cancel": [TOO_LATE], "message.status": [refusal("internal", "the store is closed", code=1)]},
     ["message.cancel", "message.status"]),
    ({"message.cancel": [TOO_LATE], "message.status": [status("running")],
      "turn.interrupt": [refusal("internal", "the store is closed", code=1)]},
     ["message.cancel", "message.status", "turn.interrupt"]),
], ids=["unknown-message", "person-only", "cancel-internal", "status-internal", "interrupt-internal"])
def test_c29_7_other_refusals_are_shown_unchanged(core_probe, answers, calls):
    """C-29.7 A refusal other than `dispatching`, `too-late` and an interrupt's
    `not-running` reaches the person as the daemon worded it; nothing is asked again."""
    result = one(core_probe, cancel(), answers)
    last = [answer for values in answers.values() for answer in values][-1]["error"]
    assert result["error"]["kind"] == "daemon"
    assert (result["error"]["code"], result["error"]["message"]) == (last["code"], last["message"])
    assert ops(result) == calls and result["pauses"] == []


@pytest.mark.parametrize("answers", [{"message.cancel": [TIMEOUT]},
                                     {"message.cancel": [TOO_LATE], "message.status": [TIMEOUT]}],
                         ids=["cancel", "status"])
def test_c29_7_a_call_with_no_answer_is_shown_and_not_retried(core_probe, answers):
    """C-29.7 No answer in time is the client's timeout, shown as it is: only
    `dispatching` is asked again."""
    result = one(core_probe, cancel(), answers)
    assert result["error"]["kind"] == "timedOut"
    assert len(result["calls"]) == sum(len(values) for values in answers.values()) and result["pauses"] == []


# MARK: - The action the app picks, and the outbox's Withdraw (D-22)


def test_c29_7_the_control_follows_the_state_the_app_saw(core_probe):
    """C-29.7 Withdraw for a queued message (`message.cancel`), Stop for a live one
    (`turn.interrupt`), nothing for one that ended; and a message journaled but not
    acknowledged is withdrawn through the outbox whatever state the app last saw."""
    journaled = str(uuid.uuid4())
    out = engine(core_probe, [
        {"do": "stop", "message_id": "q1", "state": "queued"},
        {"do": "stop", "message_id": "r1", "state": "approval-needed"},
        {"do": "stop", "message_id": "d1", "state": "complete"},
        {"do": "journal", "conversation": CID, "message_id": journaled, "text": "not sent yet"},
        {"do": "stop", "message_id": journaled, "state": "queued"},
    ], {"message.cancel": [withdrawn("q1")], "turn.interrupt": [stopped("approval-needed", "r1")]})
    queued, live, ended, _, local = out["results"]
    assert (queued["action"], queued["label"], queued["calls"]) == (
        {"action": "cancel", "message_id": "q1"}, "Withdraw", ["message.cancel q1"])
    assert (live["action"], live["label"], live["calls"]) == (
        {"action": "interrupt", "message_id": "r1"}, "Stop", ["turn.interrupt r1"])
    assert (ended["action"], ended["label"], ended["calls"], ended["receipt"]) == ({"action": "none"}, None, [], None)
    assert (local["action"], local["label"], local["calls"], local["receipt"]) == (
        {"action": "withdraw", "message_id": journaled}, "Withdraw", [], None)
    assert [entry["state"] for entry in out["entries"]] == ["withdrawn"]


# The control under a message for each state the app can show, and the daemon op
# it then makes. The app's controls (the timeline's status line, the live turn's
# strip, the composer's Stop) ask `stopAction` with no outbox entry, so the state
# alone decides: `sending` (no receipt yet) is withdrawn through the outbox.
CONTROLS = [
    ("sending", "withdraw", "Withdraw", []),
    ("queued", "cancel", "Withdraw", ["message.cancel"]),
    *[(state, "interrupt", "Stop", ["turn.interrupt"]) for state in LIVE],
    *[(state, "none", None, []) for state in NOT_LIVE if state != "queued"],
    (None, "none", None, []),
]


@pytest.mark.parametrize("state, action, label, called", CONTROLS, ids=[str(c[0]) for c in CONTROLS])
def test_c29_7_the_control_the_app_shows_follows_the_state_alone(core_probe, state, action, label, called):
    """C-29.7 Asked as the app's controls ask it (no outbox entry), for a message the
    outbox still holds: Withdraw for a row with no receipt (the outbox, no daemon
    call) and for a queued one (`message.cancel`), Stop for the message's own live
    turn (`turn.interrupt`), and no control for any other state. The daemon offers
    both a cancel and an interrupt answer; only the one the control calls for is used."""
    answers = {"message.cancel": [withdrawn()], "turn.interrupt": [stopped(state or "running")]}
    out = engine(core_probe, [
        {"do": "journal", "conversation": CID, "message_id": MID, "text": "the row under its control"},
        {"do": "stop", "message_id": MID, "state": state, "outbox_entry": False},
    ], answers)
    result = out["results"][1]
    assert "error" not in result, result
    expected_action = {"action": action} if action == "none" else {"action": action, "message_id": MID}
    assert (result["action"], result["label"]) == (expected_action, label)
    assert result["calls"] == [f"{op} {MID}" for op in called] and result["pauses"] == []
    assert out["unused"] == {op: 1 for op in answers if op not in called}
    assert result["receipt"] == (answers[called[0]][0]["result"] if called else None)
    # Only Withdraw of a row with no receipt closes its outbox entry.
    assert (out["entries"][0]["state"] == "withdrawn") == (action == "withdraw")


def test_c29_7_withdraw_send_before_the_daemon_could_have_it(core_probe):
    """C-29.7, D-22 A row with no receipt: never sent, it is withdrawn here with no
    call; while its send is under way, Withdraw waits for the answer."""
    never, going = str(uuid.uuid4()), str(uuid.uuid4())
    out = engine(core_probe, [
        {"do": "journal", "conversation": CID, "message_id": never, "text": "one"},
        {"do": "journal", "conversation": CID, "message_id": going, "text": "two"},
        {"do": "withdraw_send", "message_id": never},
        {"do": "begin", "message_id": going},
        {"do": "withdraw_send", "message_id": going},
    ])
    assert out["results"][2]["result"] == {"outcome": "withdrawn", "receipt": None}
    assert out["results"][4]["result"] == {"outcome": "in-flight"}
    assert out["calls"] == []
    assert [(entry["key"], entry["state"]) for entry in out["entries"]] == [(never, "withdrawn"), (going, "sending")]


@pytest.mark.parametrize("statuses, answers, outcome, calls", [
    # The daemon never had it: a tombstone, named with its conversation, stops a late copy.
    (["unknown"], {"message.cancel": [ok(receipt("cancelled", origin="tombstone",
                                                 state_reason="withdrawn-before-receipt"))]},
     ("withdrawn", "cancelled"), ["message.status", "message.cancel"]),
    # It has it, still queued: cancelled there.
    (["queued"], {"message.cancel": [withdrawn()]}, ("stopped", "cancelled"), ["message.status", "message.cancel"]),
    # It has it, and it left the queue before the cancel: interrupted once status says it runs.
    (["queued", "running"], {"message.cancel": [TOO_LATE], "turn.interrupt": [stopped()]},
     ("stopped", "running"), ["message.status", "message.cancel", "message.status", "turn.interrupt"]),
    # It has it, and it is running already: interrupted.
    (["running"], {"turn.interrupt": [stopped()]}, ("stopped", "running"), ["message.status", "turn.interrupt"]),
    # It has it, and it has ended: nothing is stopped.
    (["complete"], {}, ("stopped", None), ["message.status"]),
], ids=["never-received", "queued", "moved-on", "running", "ended"])
def test_c29_7_withdraw_send_after_a_lost_answer_asks_the_daemon_first(core_probe, statuses, answers, outcome, calls):
    """C-29.7, D-22 A send whose answer was lost: `message.status` first; a tombstone
    when the daemon never had it, otherwise Withdraw acts where the daemon has it."""
    mid = str(uuid.uuid4())
    answers = {**answers, "message.status": [status(state, mid) for state in statuses]}
    for key in ("message.cancel", "turn.interrupt"):
        answers[key] = [{**answer, "result": {**answer["result"], "message_id": mid}} if "result" in answer else answer
                        for answer in answers.get(key, [])]
    out = engine(core_probe, [
        {"do": "journal", "conversation": CID, "message_id": mid, "text": "lost"},
        {"do": "lose_answer", "message_id": mid},
        {"do": "withdraw_send", "message_id": mid},
    ], answers)
    result = out["results"][2]
    assert "error" not in result, result
    assert (result["result"]["outcome"], (result["result"]["receipt"] or {}).get("state")) == outcome
    assert ops(result) == calls and out["unused"] == {}
    if outcome[0] == "withdrawn":
        assert result["calls"][-1] == f"message.cancel {mid} conversation={CID}"
    else:
        assert all(" conversation=" not in call for call in result["calls"])
    assert out["entries"][0]["state"] == ("withdrawn" if outcome[0] == "withdrawn" else "acknowledged")


TOMBSTONE = ok(receipt("cancelled", origin="tombstone", state_reason="withdrawn-before-receipt"))


@pytest.mark.parametrize("before, answers", [
    ([], {}),
    (["begin"], {}),
    (["lose_answer"], {"message.status": [status("unknown")], "message.cancel": [TOMBSTONE]}),
    (["lose_answer"], {"message.status": [status("queued")], "message.cancel": [withdrawn()]}),
    (["lose_answer"], {"message.status": [status("running")], "turn.interrupt": [stopped()]}),
    (["lose_answer"], {"message.status": [status("complete")]}),
], ids=["never-sent", "in-flight", "never-received", "queued", "running", "ended"])
def test_c29_7_stop_withdraw_gives_what_withdraw_send_gives(core_probe, before, answers):
    """C-29.7, D-22 `engine.stop` of a Withdraw is `withdrawSend` with its receipt:
    the same calls, the same outbox entry afterwards, and the receipt `withdrawSend`
    returned, or none. A send under way is no receipt and no error (the app's
    `UIModel.stop` asks `withdrawSend` itself, so it can tell the person to try again)."""
    steps = [{"do": "journal", "conversation": CID, "message_id": MID, "text": "withdraw me"},
             *[{"do": step, "message_id": MID} for step in before]]
    runs = [{"answers": answers, "steps": [*steps, last]} for last in (
        {"do": "withdraw_send", "message_id": MID},
        {"do": "stop", "action": {"action": "withdraw", "message_id": MID}})]
    with tempfile.TemporaryDirectory(prefix="sf-qe-") as scratch:
        send, stop = run_probe(core_probe, "queue-engine", write_json(Path(scratch) / "runs.json", {"runs": runs}))["runs"]
    sent, stopped_ = send["results"][-1], stop["results"][-1]
    assert "error" not in sent and "error" not in stopped_, (sent, stopped_)
    assert stopped_["receipt"] == sent["result"].get("receipt")
    assert stopped_["calls"] == sent["calls"] and stopped_["pauses"] == sent["pauses"] == []
    assert stop["entries"] == send["entries"] and stop["unused"] == send["unused"] == {}
    if before == ["begin"]:
        assert sent["result"] == {"outcome": "in-flight"} and stopped_["receipt"] is None and stopped_["calls"] == []


# MARK: - Properties over scripted answers


def reason_of(message: str) -> str | None:
    """`DaemonError.reason`: the lowercase slug before ": "."""
    match = re.match(r"([a-z][a-z0-9-]*): ", message)
    return match.group(1) if match else None


class Failure(Exception):
    def __init__(self, kind: str, detail: str | None = None):
        super().__init__(kind, detail)
        self.kind, self.detail = kind, detail


class Script:
    """The scripted daemon again: each op answers from its own queue, in order."""

    def __init__(self, answers: dict[str, list[dict]]):
        self.queues = {op: list(values) for op, values in answers.items()}
        self.calls: list[str] = []

    def call(self, op: str):
        self.calls.append(op)
        queue = self.queues.get(op) or []
        if not queue:
            raise Failure("unavailable")
        answer = queue.pop(0)
        if "error" in answer:
            raise Failure("daemon", reason_of(answer["error"]["message"]))
        if answer.get("timeout"):
            raise Failure("timedOut", op)
        return answer["result"]


def reference(action: str, retries: list[float],
              answers: dict[str, list[dict]]) -> tuple[list[str], list[float], tuple]:
    """Stop, written from the rule in the module docstring: the calls, the pauses,
    and the outcome (`("receipt", <receipt or None>)` or `("error", kind, detail)`)."""
    script, pauses = Script(answers), []

    def mine(result) -> dict | None:
        return next((r for r in result["messages"] if r["message_id"] == MID), None)

    def stop_if_running():
        current = mine(script.call("message.status"))
        if current is None:
            raise Failure("malformed")
        if current["state"] not in LIVE:
            return current
        try:
            return script.call("turn.interrupt")
        except Failure as failure:
            if (failure.kind, failure.detail) != ("daemon", "not-running"):
                raise
            return mine(script.call("message.status"))

    def run():
        if action == "interrupt":
            return script.call("turn.interrupt")
        waits = list(retries)
        while True:
            try:
                return script.call("message.cancel")
            except Failure as failure:
                if (failure.kind, failure.detail) == ("daemon", "dispatching") and waits:
                    pauses.append(waits.pop(0))
                    continue
                if (failure.kind, failure.detail) == ("daemon", "too-late"):
                    return stop_if_running()
                raise

    try:
        outcome = ("receipt", run())
    except Failure as failure:
        outcome = ("error", failure.kind, failure.detail)
    return script.calls, pauses, outcome


def outcome_of(result: dict) -> tuple:
    error = result.get("error")
    if error is None:
        return ("receipt", result["receipt"])
    detail = {"daemon": error.get("reason"), "timedOut": error.get("op")}.get(error["kind"])
    return ("error", error["kind"], detail)


def answered(calls: list[str], answers: dict[str, list[dict]]) -> list[tuple[str, dict | None]]:
    """Each call with the answer it got: every op takes its own answers in order."""
    used: dict[str, int] = {}
    out = []
    for op in calls:
        index = used.get(op, 0)
        used[op] = index + 1
        values = answers.get(op, [])
        out.append((op, values[index] if index < len(values) else None))
    return out


def said(answer: dict | None) -> str:
    """What an answer said, in one word: a refusal's reason, `timeout`, `missing`, or the result."""
    if answer is None:
        return "missing"
    if "error" in answer:
        return reason_of(answer["error"]["message"]) or "refused"
    if answer.get("timeout"):
        return "timeout"
    return "result"


def status_state(answer: dict | None) -> str | None:
    """The state a `message.status` answer gave for the message, if it gave one."""
    if said(answer) != "result":
        return None
    return next((r["state"] for r in answer["result"]["messages"] if r["message_id"] == MID), None)


CANCEL_ANSWERS = {"withdrawn": withdrawn(), "dispatching": DISPATCHING, "too-late": TOO_LATE,
                  "unknown-message": UNKNOWN_MESSAGE, "timeout": TIMEOUT}
STATUS_ANSWERS = {**{state: status(state) for state in LIVE + NOT_LIVE}, "omitted": ok({"messages": []}),
                  "another": status("running", "another-message"),
                  "internal": refusal("internal", "the store is closed", code=1), "timeout": TIMEOUT}
INTERRUPT_ANSWERS = {"stopped": stopped(), "not-running": not_running("complete"),
                     "internal": refusal("internal", "the store is closed", code=1), "timeout": TIMEOUT}


@st.composite
def scripts(draw):
    """A Stop (a cancel four times in five) against a daemon whose answers are drawn.
    The cancels: a run of `dispatching` (none, some, or one more than the retries),
    then the answer that decides (`too-late` most often), then any answers. The first
    status: a live state, another state (twice as often: there are more), or a
    failure. Every op has more answers than the rule lets the engine use, so an
    engine that asked too often would get them."""
    action = draw(st.sampled_from(["cancel"] * 4 + ["interrupt"]))
    retries = draw(st.lists(st.sampled_from([0.1, 0.25, 0.5, 1.0, 2.0]), max_size=4))
    total = len(retries) + 3
    lead = draw(st.integers(0, len(retries) + 1))
    decisive = draw(st.sampled_from(["too-late"] * 4 + ["withdrawn", "unknown-message", "timeout"]))
    cancels = ["dispatching"] * lead + [decisive]
    cancels += draw(st.lists(st.sampled_from(sorted(CANCEL_ANSWERS)), min_size=total - len(cancels),
                             max_size=total - len(cancels)))
    first = draw(st.sampled_from(["live", "not-live", "not-live", "failure"]))
    statuses = [draw(st.sampled_from(LIVE if first == "live" else NOT_LIVE if first == "not-live"
                                     else ["omitted", "another", "internal", "timeout"]))]
    statuses += draw(st.lists(st.sampled_from(sorted(STATUS_ANSWERS)), min_size=2, max_size=2))
    interrupts = draw(st.lists(st.sampled_from(["stopped", "stopped", "not-running", "not-running", "internal",
                                                "timeout"]), min_size=2, max_size=2))
    answers = {"message.cancel": [CANCEL_ANSWERS[name] for name in cancels],
               "message.status": [STATUS_ANSWERS[name] for name in statuses],
               "turn.interrupt": [INTERRUPT_ANSWERS[name] for name in interrupts]}
    return action, retries, answers


def check_stop(action: str, retries: list[float], answers: dict[str, list[dict]], result: dict) -> None:
    """The rule's invariants for one Stop against one scripted daemon, and the reference."""
    calls = ops(result)
    pairs = answered(calls, answers)
    outcome = outcome_of(result)
    ended = f"error {outcome[2] or outcome[1]}" if outcome[0] == "error" else "receipt"
    event(f"{action}: {' '.join(calls)} -> {ended}")

    assert all(call.split(" ")[1] == MID for call in result["calls"])
    assert "unavailable" not in str(outcome), "the engine asked for more answers than the rule allows"
    # turn.interrupt: the Stop itself, or right after a status that said the message runs.
    for index, op in enumerate(calls):
        if op == "turn.interrupt":
            if action == "interrupt":
                assert index == 0
            else:
                assert index > 0 and calls[index - 1] == "message.status"
                assert status_state(pairs[index - 1][1]) in LIVE
    assert calls.count("turn.interrupt") <= 1 and calls.count("message.status") <= 2
    # Cancels and pauses.
    cancels = calls.count("message.cancel")
    assert cancels <= 1 + len(retries)
    assert result["pauses"] == retries[:max(0, cancels - 1)]
    # A dispatching refusal: another cancel next, or the step ends with it.
    for index, (op, answer) in enumerate(pairs):
        if op == "message.cancel" and said(answer) == "dispatching":
            if index + 1 < len(calls):
                assert calls[index + 1] == "message.cancel"
            else:
                assert outcome == ("error", "daemon", "dispatching") and cancels == 1 + len(retries)
    # A too-late ends in an error only when what came after it failed.
    if any(op == "message.cancel" and said(answer) == "too-late" for op, answer in pairs):
        if outcome[0] == "error":
            first_status = pairs[calls.index("message.status")][1]
            assert (said(first_status) != "result" or status_state(first_status) is None
                    or any(op == "turn.interrupt" and said(answer) not in ("result", "not-running")
                           for op, answer in pairs)
                    or (calls.count("message.status") == 2 and said(pairs[-1][1]) != "result"))
    # The whole behaviour, against the reference.
    expected_calls, expected_pauses, expected_outcome = reference(action, retries, answers)
    assert (calls, result["pauses"], outcome) == (expected_calls, expected_pauses, expected_outcome)


@PROPERTY
@given(batch=st.lists(scripts(), min_size=1, max_size=8))
def test_c29_7_property_withdraw_interrupts_only_what_status_says_runs(core_probe, batch):
    """C-29.7 For every scripted daemon: `turn.interrupt` follows only a
    `message.status` that said the message is its own live turn (or is the Stop of a
    live turn itself); at most 1 + len(retries) cancels, one pause before each cancel
    after the first, the pauses the retries in order; a `dispatching` refusal is
    followed by another cancel or by nothing; a `too-late` ends in an error only when
    a status or the interrupt failed or a status left the message out; every call
    names the message; and the engine agrees with the reference. Each example runs
    up to eight daemons, each with its own engine, in one probe."""
    runs = [{"answers": answers, "dispatching_retries": retries,
             "steps": [cancel() if action == "cancel" else interrupt()]} for action, retries, answers in batch]
    with tempfile.TemporaryDirectory(prefix="sf-qe-") as scratch:
        out = run_probe(core_probe, "queue-engine", write_json(Path(scratch) / "runs.json", {"runs": runs}))
    assert len(out["runs"]) == len(batch)
    for (action, retries, answers), run in zip(batch, out["runs"]):
        check_stop(action, retries, answers, run["results"][0])


# MARK: - The daemon's own service, end to end


@pytest.fixture
def daemon():
    harness = ServiceHarness(Path(tempfile.mkdtemp(prefix="sf-qe-", dir="/tmp")))
    server = ServiceServer(harness)
    yield harness, server
    server.close()
    harness.close()


def live(core_probe, server, steps: list[dict], **extra) -> dict:
    with tempfile.TemporaryDirectory(prefix="sf-qe-") as scratch:
        path = write_json(Path(scratch) / f"{uuid.uuid4().hex}.json",
                          {"socket": str(server.path), "steps": steps, **extra})
        return run_probe(core_probe, "queue-engine", path, timeout=120)


def queued_message(harness) -> tuple[str, str]:
    cid = harness.create()["conversation_id"]
    return cid, harness.submit(cid, "waits its turn")["message_id"]


def sent_ops(server) -> list[str]:
    return [request["op"] for request in server.requests]


def test_c29_7_real_daemon_a_second_withdraw_is_answered_with_the_receipt(core_probe, daemon):
    """C-29.7 The daemon's cancel is not idempotent: a second Withdraw of a message
    already withdrawn is `too-late`, and the engine answers it with the message's
    receipt (`message.status`: cancelled), sends no `turn.interrupt`, and shows no error."""
    harness, server = daemon
    _, mid = queued_message(harness)
    out = live(core_probe, server, [cancel(mid), cancel(mid)])
    first, second = out["results"]
    assert "error" not in first and "error" not in second, out["results"]
    assert first["calls"] == [f"message.cancel {mid}"]
    assert (first["receipt"]["state"], first["receipt"]["state_reason"]) == ("cancelled", "withdrawn")
    assert second["calls"] == [f"message.cancel {mid}", f"message.status {mid}"]
    assert (second["receipt"]["state"], second["receipt"]["state_reason"]) == ("cancelled", "withdrawn")
    assert "turn.interrupt" not in sent_ops(server)
    assert harness.store.message(mid)["state"] == "cancelled"


# A message that left the queue for `starting` before Withdraw (cancel, status,
# interrupt) is test_core_outbox.py::test_design_12_stop_cancels_while_queued_and_interrupts_once_it_moved.


@pytest.mark.parametrize("state", ["complete", "failed", "interrupted", "delivery-unknown"])
def test_c29_7_real_daemon_withdraw_after_the_turn_ended_stops_nothing(core_probe, daemon, state):
    """C-29.7 A message that ran and ended (or awaits the person's ruling on its
    delivery) before Withdraw arrived: `too-late`, then its receipt; no
    `turn.interrupt`, no stop recorded, no error."""
    harness, server = daemon
    _, mid = queued_message(harness)
    assert harness.store.set_state(mid, "running", expect=("queued",))
    assert harness.store.set_state(mid, state, expect=("running",))
    out = live(core_probe, server, [cancel(mid)])
    result = out["results"][0]
    assert "error" not in result, result
    assert result["calls"] == [f"message.cancel {mid}", f"message.status {mid}"]
    assert result["receipt"]["state"] == state
    assert "turn.interrupt" not in sent_ops(server)
    assert harness.store.message(mid)["stop_requested_at"] is None


def test_c29_7_real_daemon_withdraw_of_a_waiting_message_with_no_job(core_probe, daemon):
    """C-29.7 A message waiting to be sent again, with no job bound (a deferral or a
    re-admission), is still withdrawn by the cancel: no provider has it (C-24.7)."""
    harness, server = daemon
    _, mid = queued_message(harness)
    assert harness.store.set_state(mid, "waiting", reason="deferred: no lane", expect=("queued",))
    out = live(core_probe, server, [cancel(mid)])
    result = out["results"][0]
    assert result["calls"] == [f"message.cancel {mid}"]
    assert (result["receipt"]["state"], result["receipt"]["state_reason"]) == ("cancelled", "withdrawn")


def test_c29_7_real_daemon_withdraw_of_an_unknown_message_shows_the_refusal(core_probe, daemon):
    """C-29.7 The tray's cancel names no conversation, so the daemon leaves no
    tombstone for an id it has no message by: `unknown-message`, shown unchanged."""
    harness, server = daemon
    harness.create()
    mid = str(uuid.uuid4())
    out = live(core_probe, server, [cancel(mid)])
    result = out["results"][0]
    assert result["calls"] == [f"message.cancel {mid}"]
    assert result["error"]["kind"] == "daemon" and result["error"]["reason"] == "unknown-message"
    assert harness.store.find_message(mid) is None


def claim(harness, mid: str) -> None:
    """A state in which the daemon's `_cancel` answers `dispatching` (service.py): the
    message `waiting`, a job id bound, and no turn job in the job store (the harness's
    has none). `_cancel` withdraws a waiting message with no job id bound, as the
    dispatcher's claim leaves it, so the harness binds one."""
    assert harness.store.set_state(mid, "waiting", reason="dispatching", expect=("queued",))
    harness.store.update_message(mid, job_id="job-being-created")


def test_c29_7_real_daemon_dispatching_is_asked_again_then_shown(core_probe, daemon):
    """C-29.7 While the dispatcher binds the message, every cancel is `dispatching`:
    the engine asks four more times, pausing 0.1, 0.25, 0.5 and 1 s, then shows the
    daemon's refusal with its fix. A refused cancel records no stop."""
    harness, server = daemon
    _, mid = queued_message(harness)
    claim(harness, mid)
    out = live(core_probe, server, [cancel(mid)])
    result = out["results"][0]
    assert result["calls"] == [f"message.cancel {mid}"] * 5
    assert result["pauses"] == RETRIES
    assert result["error"]["reason"] == "dispatching" and result["error"]["fix"] == "send the cancel again in a moment"
    assert sent_ops(server) == ["message.cancel"] * 5
    message = harness.store.message(mid)
    assert message["state"] == "waiting" and message["stop_requested_at"] is None


def release_pauses(gate: Path, actions: dict[int, Callable[[], None]], done: threading.Event,
                   errors: list[BaseException]) -> threading.Thread:
    """Lets the engine's pauses go one by one (`holdAtGate`), running the test's
    change to the daemon's state first where it has one."""
    def run() -> None:
        pause = 1
        while not done.is_set():
            if (gate / f"paused-{pause}").exists():
                try:
                    if pause in actions:
                        actions[pause]()
                except BaseException as exc:          # reported by the test
                    errors.append(exc)
                (gate / f"go-{pause}").touch()
                pause += 1
            else:
                time.sleep(0.005)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def gated(core_probe, server, steps: list[dict], actions: dict[int, Callable[[], None]]) -> dict:
    with tempfile.TemporaryDirectory(prefix="sf-qe-gate-") as gate:
        done, errors = threading.Event(), []
        thread = release_pauses(Path(gate), actions, done, errors)
        try:
            out = live(core_probe, server, steps, gate=gate)
        finally:
            done.set()
            thread.join(timeout=5)
        assert not errors, errors
        return out


def test_c29_7_real_daemon_dispatching_then_started_is_interrupted(core_probe, daemon):
    """C-29.7 `dispatching`, and during the pause the dispatcher starts the turn: the
    next cancel is `too-late`, status says `starting`, and Withdraw interrupts it."""
    harness, server = daemon
    _, mid = queued_message(harness)
    claim(harness, mid)

    def start() -> None:
        assert harness.store.set_state(mid, "starting", expect=("waiting",))

    out = gated(core_probe, server, [cancel(mid)], {1: start})
    result = out["results"][0]
    assert "error" not in result, result
    assert result["calls"] == [f"message.cancel {mid}"] * 2 + [f"message.status {mid}", f"turn.interrupt {mid}"]
    assert result["pauses"] == [0.1]
    assert result["receipt"]["state"] == "starting" and result["receipt"]["stop_requested"] is True
    assert harness.store.message(mid)["stop_requested_at"]


def test_c29_7_real_daemon_dispatching_then_released_is_withdrawn(core_probe, daemon):
    """C-29.7 `dispatching`, and during the pause the dispatcher gives the message
    back (its submit was refused; service.py puts a claim back to `queued`): the next
    cancel withdraws it."""
    harness, server = daemon
    _, mid = queued_message(harness)
    claim(harness, mid)

    def release() -> None:
        harness.store.update_message(mid, job_id=None)
        assert harness.store.set_state(mid, "queued", expect=("waiting",), unbound=True)

    out = gated(core_probe, server, [cancel(mid)], {1: release})
    result = out["results"][0]
    assert "error" not in result, result
    assert result["calls"] == [f"message.cancel {mid}"] * 2 and result["pauses"] == [0.1]
    assert (result["receipt"]["state"], result["receipt"]["state_reason"]) == ("cancelled", "withdrawn")
    assert "turn.interrupt" not in sent_ops(server)


@pytest.mark.parametrize("received", [False, True], ids=["never-received", "received"])
def test_c29_7_real_daemon_withdraw_send_after_a_lost_answer(core_probe, daemon, received):
    """C-29.7, D-22 The tray's Withdraw of a row whose send lost its answer: the daemon
    that never had it keeps a tombstone (a late copy cannot land); one that has it,
    still queued, cancels it."""
    harness, server = daemon
    cid = harness.create()["conversation_id"]
    mid = str(uuid.uuid4())
    if received:
        harness.submit(cid, "lost answer", message_id=mid)
    out = live(core_probe, server, [
        {"do": "journal", "conversation": cid, "message_id": mid, "text": "lost answer"},
        {"do": "lose_answer", "message_id": mid},
        {"do": "withdraw_send", "message_id": mid},
    ])
    result = out["results"][2]
    assert "error" not in result, result
    stored = harness.store.message(mid)
    if received:
        assert result["result"]["outcome"] == "stopped"
        assert result["calls"] == [f"message.status {mid}", f"message.cancel {mid}"]
        assert (stored["state"], stored["state_reason"], stored["origin"]) == ("cancelled", "withdrawn", "person")
        assert out["entries"][0]["state"] == "acknowledged"
    else:
        assert result["result"]["outcome"] == "withdrawn"
        assert result["calls"] == [f"message.status {mid}", f"message.cancel {mid} conversation={cid}"]
        assert (stored["state"], stored["state_reason"], stored["origin"]) == (
            "cancelled", "withdrawn-before-receipt", "tombstone")
        assert out["entries"][0]["state"] == "withdrawn"


# MARK: - The store state around the tray


def store(core_probe, steps: list[dict]) -> dict:
    with tempfile.TemporaryDirectory(prefix="sf-qs-") as scratch:
        return run_probe(core_probe, "queue-state", write_json(Path(scratch) / f"{uuid.uuid4().hex}.json",
                                                               {"steps": steps}))


def state_receipt(cid: str, mid: str, seq: int, state: str) -> dict:
    return {"message_id": mid, "conversation_id": cid, "seq": seq, "origin": "person", "state": state,
            "text": f"message {seq}"}


def test_c29_7_withdraw_local_marks_the_message_in_whichever_timeline_holds_it(core_probe):
    """C-29.7 A message withdrawn before the daemon had it is cancelled
    (`withdrawn-before-receipt`) in the timeline that holds it, focused or not; it
    leaves the tray, and the timeline shows it as one line in place of its bubble."""
    out = store(core_probe, [
        {"focus": "cv-a"},
        {"receipts": [state_receipt("cv-a", "a1", 1, "running")]},
        {"local": {"conversation_id": "cv-a", "message_id": "a2", "text": "behind the\n running turn"}},
        {"local": {"conversation_id": "cv-b", "message_id": "b1", "text": "elsewhere"}},
        {"withdraw_local": "b1"},
        {"withdraw_local": "a2"},
        {"focus": "cv-b"},
    ])
    steps = out["steps"]
    assert steps[2]["tray"] == ["a2"] and "person:a2" not in steps[2]["items"]     # behind the live turn
    assert steps[4]["tray"] == ["a2"]                                              # b1 is not in this conversation
    assert steps[5]["tray"] == [] and steps[5]["items"] == ["person:a1", "person:a2"]
    assert steps[6]["focused"] == "cv-b" and steps[6]["items"] == ["person:b1"] and steps[6]["tray"] == []
    for cid, mid, text in (("cv-a", "a2", "behind the running turn"), ("cv-b", "b1", "elsewhere")):
        timeline = out["timelines"][cid]
        assert timeline["turns"][mid] == {"state": "cancelled", "state_reason": "withdrawn-before-receipt"}
        row = next(item for item in timeline["layout"]["items"] if item["id"] == f"person:{mid}")
        assert (row["type"], row["text"]) == ("notice", f"Withdrawn before it was sent: {text}")
        assert timeline["layout"]["tray"] == []
    assert out["timelines"]["cv-a"]["turns"]["a1"] == {"state": "running", "state_reason": None}


def test_c29_7_withdraw_local_of_an_unknown_message_changes_nothing(core_probe):
    """C-29.7 Withdrawing an id no timeline holds adds nothing anywhere."""
    out = store(core_probe, [{"focus": "cv-a"}, {"local": {"conversation_id": "cv-a", "message_id": "a1", "text": "x"}},
                             {"withdraw_local": "nowhere"}])
    assert set(out["timelines"]) == {"cv-a"}
    assert out["timelines"]["cv-a"]["turns"] == {"a1": {"state": "sending", "state_reason": None}}


def test_c29_7_focusing_again_reads_the_log_before_following(core_probe):
    """C-29.7, C-27.5 A conversation focused again reads its log from the cursor:
    `caught_up` is false until a page adds nothing, even though its timeline had read
    the log before. Until then the view lands at the end without scrolling and the
    approval follower waits; after it, the pending card is brought into view once."""
    log = Log()
    log.add("m1", "accepted")
    card = ask(log, "m1", "perm-1")
    first, empty = log.page(), log.page()
    log.add("m1", "text", block="0", text="while you were away")
    later, empty_again = log.page(), log.page()
    out = store(core_probe, [
        {"focus": "cv-a"},
        {"receipts": [state_receipt("cv-a", "m1", 1, "approval-needed")]},
        {**first, "conversation_id": "cv-a"},
        {**empty, "conversation_id": "cv-a"},
        {"focus": "cv-b"},
        {"focus": "cv-a"},
        {**later, "conversation_id": "cv-a"},
        {**empty_again, "conversation_id": "cv-a"},
    ])
    steps = out["steps"]
    assert [step["caught_up"] for step in steps] == [False, False, False, True, False, False, False, True]
    assert steps[3]["scroll"] == card                            # read to its end: the card, once
    assert steps[4]["focused"] == "cv-b" and steps[4]["move"] == "appear"
    # Back to cv-a: a new view, which neither follows nor scrolls to the card until the log is read.
    assert steps[5]["move"] == "appear" and steps[5]["scroll"] is None
    assert steps[6]["did"] == "applied:1" and steps[6]["move"] == "jump" and steps[6]["scroll"] is None
    assert steps[7]["did"] == "applied:0" and steps[7]["move"] == "jump" and steps[7]["scroll"] == card
    assert out["timelines"]["cv-a"]["caught_up"] is True


def test_c29_7_focusing_a_new_conversation_creates_its_timeline(core_probe):
    """C-29.7 Focusing a conversation the app has no timeline for creates an empty
    one that has not read its log; focusing none leaves every timeline as it was."""
    out = store(core_probe, [
        {"focus": "cv-new"},
        {"page": {"events": [], "next": 0, "reset": False}, "conversation_id": "cv-new"},
        {"focus": None},
    ])
    first, read, none = out["steps"]
    assert (first["focused"], first["caught_up"], first["items"], first["tray"], first["title"]) == (
        "cv-new", False, [], [], None)
    assert read["did"] == "applied:0" and read["caught_up"] is True
    assert none["focused"] is None and "caught_up" not in none
    assert set(out["timelines"]) == {"cv-new"} and out["timelines"]["cv-new"]["caught_up"] is True
