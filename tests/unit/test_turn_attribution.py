"""C-24.4, C-24.7, C-24.8, C-26.5, C-26.6: which turn a provider's terminal event closes.

Resuming a session whose background task outlived its last process, Claude Code
runs a turn of its own for the task's notification and ends it with a `result`
while our message waits in its command queue. On 2026-09-28 that `result` ended
12 of 24 revived messages as complete before they had started; `after_result_s`
later their turns were stopped mid-work (job
20260928-152257-turn-cv-1790623376839-ca22af954212 is the traced case). The rows
here keep the shapes of that job's stdout (Claude Code 2.1.284), content replaced.

Invariants, property-tested below:

- I1 a turn never ends `complete` unless its message was acknowledged (C-24.4);
- I2 a turn has exactly one terminal outcome: one step carries it, it is never
  replaced, and exactly one `turn.completed` event records it;
- I3 a result that is not the message's (it names another message, or it closes
  a turn Claude Code began before the message was acknowledged) neither ends the
  turn nor writes a frame;
- I4 on a well-formed stream the outcome is the one the message's own result
  gives, as a position-based reference model reads it (a differential check);
- I5 a driver rebuilt from the same stdout emits the same events and frames (C-26.6).

The Codex driver keeps the same rule for `turn/started` and `turn/completed`.
"""

from __future__ import annotations

import json

import hypothesis
import hypothesis.strategies as st
import pytest

from subfleet.conversations import reconcile
from subfleet.conversations.claude_turn import ClaudeTurn
from subfleet.conversations.codex_turn import ID_THREAD, ID_TURN, CodexTurn
from tests.unit import test_codex_turn as codex
from tests.unit.test_claude_turn import INIT_OK, MID, SID, spec

OTHER = "3e1d1f7c-2222-4333-8444-555566667777"
MODEL = "claude-opus-5-5"


def row(**fields) -> str:
    return json.dumps(fields)


def lifecycle(state: str, uuid: str = MID) -> str:
    return row(type="command_lifecycle", command_uuid=uuid, state=state, session_id=SID)


NOTICE = row(type="system", subtype="task_notification", task_id="bior1vggp", status="stopped", output_file="",
             summary="Background shell command didn't finish before the previous session ended", session_id=SID)
HOOK = row(type="system", subtype="hook_started", hook_name="SessionStart:resume", hook_event="SessionStart")
SYS_INIT = row(type="system", subtype="init", model=MODEL, permissionMode="default", session_id=SID,
               capabilities=["interrupt_receipt_v1", "interrupt_cancel_queued_v1", "msg_lifecycle_v1"])
REQUESTING = row(type="system", subtype="status", status="requesting", session_id=SID)
ECHO = row(type="user", uuid=MID, isReplay=True, parent_tool_use_id=None, session_id=SID,
           message={"role": "user", "content": [{"type": "text", "text": "fix the bug"}]})
LIMIT = row(type="rate_limit_event", rate_limit_info={"status": "rejected", "rateLimitType": "five_hour",
                                                      "resetsAt": 1789000000})


def text(message_id: str, words: str) -> str:
    return row(type="assistant", parent_tool_use_id=None, session_id=SID,
               message={"id": message_id, "model": MODEL, "content": [{"type": "text", "text": words}]})


def notification_result(*, ok: bool = True, origin: str | None = "task-notification", index: int = 0,
                        words: str = "", subtype: str | None = None, **extra) -> str:
    """A turn Claude Code began itself: it names no message (91 of 91 of our results
    named ours on 2026-09-28; 50 of 50 notification turns named none)."""
    fields = dict(type="result", subtype=subtype or ("success" if ok else "error_during_execution"), is_error=not ok,
                  num_turns=0, result=words, stop_reason=None, session_id=SID, result_index=index,
                  permission_denials=[], errors=[])
    if origin:
        fields["origin"] = {"kind": origin}
    return row(**fields, **extra)


def our_result(*, ok: bool = True, subtype: str | None = None, named: bool = True, num_turns: int = 3,
               words: str = "Done.", names: tuple[str, ...] = ()) -> str:
    fields = dict(type="result", subtype=subtype or ("success" if ok else "error_during_execution"),
                  is_error=not ok, num_turns=num_turns, result=words, session_id=SID, permission_denials=[],
                  errors=[])
    if named:
        fields["user_message_uuid"] = (names or (MID,))[-1]
        fields["user_message_uuids"] = list(names or (MID,))
    return row(**fields)


def run(rows: list[str], turn: ClaudeTurn | None = None, *, interrupt_before: int | None = None):
    """Feed a fresh driver `rows` (INIT_OK among them sends the message); the driver
    and one step per row. `interrupt_before`: the person's stop lands before that row."""
    turn = turn or ClaudeTurn(spec(), read_bytes=lambda p: b"")
    steps = [turn.start()]
    offset = 0
    for index, line in enumerate(rows):
        if index == interrupt_before:
            steps.append(turn.interrupt())
        steps.append(turn.feed(line, offset))
        offset += len(line) + 1
    return turn, steps


def events(steps) -> list:
    return [event for step in steps for event in step.events]


def frames(steps) -> list[str]:
    return [frame.tag for step in steps for frame in step.frames]


#: Job 20260928-152257-turn-cv-1790623376839-ca22af954212, row for row (stream events left out).
TRACED = [HOOK, NOTICE, INIT_OK, lifecycle("queued"), SYS_INIT, notification_result(), lifecycle("started"),
          SYS_INIT, REQUESTING, ECHO, text("msg_1", "Re-checking state before acting."), our_result(num_turns=3)]


# --- the traced case and its neighbours -------------------------------------------------------


def test_c26_5_a_notification_turn_s_result_neither_ends_the_turn_nor_closes_stdin():
    """C-26.5, C-24.4: the traced case. The notification turn's `result` is recorded as
    `turn.other` and the message waits on; the message's own `result` ends the turn."""
    turn, steps = run(TRACED[:6])
    notified = steps[-1]
    assert notified.outcome is None and notified.frames == [] and turn.outcome is None
    assert [(e.kind, e.data["origin"], e.data["num_turns"]) for e in notified.events] == [
        ("turn.other", "task-notification", 0)]
    turn, steps = run(TRACED)
    assert (turn.outcome.state, turn.outcome.accepted, turn.outcome.ended_by) == ("complete", True, "provider")
    completed = [e for e in events(steps) if e.kind == "turn.completed"]
    assert len(completed) == 1 and completed[0].data["num_turns"] == 3
    assert frames(steps) == ["init", "user-message", "settings", "close"]
    kinds = [e.kind for e in events(steps)]
    assert kinds.index("turn.other") < kinds.index("accepted") < kinds.index("turn.completed")


def test_c26_5_two_notification_turns_before_the_message():
    """C-26.5 (job 20260928-152707-turn-cv-1790623627558-1dc98e893f90): a background agent's
    and a background shell's notifications, two turns, each with its own `result`."""
    rows = [NOTICE, NOTICE, INIT_OK, lifecycle("queued"), SYS_INIT, notification_result(index=0),
            SYS_INIT, notification_result(index=1), lifecycle("started"), ECHO, our_result()]
    turn, steps = run(rows)
    assert [e.data["result_index"] for e in events(steps) if e.kind == "turn.other"] == [0, 1]
    assert turn.outcome.state == "complete" and turn.outcome.accepted


def test_c26_5_a_result_before_initialize_is_answered_does_not_keep_the_message_back():
    """C-26.5: a turn Claude Code ends before it answers `initialize` is not the message's;
    the message is still sent when the answer comes."""
    turn, steps = run([NOTICE, notification_result(), INIT_OK, ECHO, our_result()])
    assert frames(steps) == ["init", "user-message", "settings", "close"]
    assert turn.outcome.state == "complete"


def test_c26_5_a_startup_failure_still_ends_the_turn():
    """C-26.5: a startup failure (`startup_failure_reason`, 2.1.284's zeroed result before
    exiting) is the session's, so it ends the turn at once, failed and not acknowledged."""
    failure = row(type="result", subtype="error_during_execution", is_error=True, num_turns=0,
                  startup_failure_reason="cwd_unavailable", errors=["The working directory was deleted"])
    turn, steps = run([failure])
    assert (turn.outcome.state, turn.outcome.reason, turn.outcome.accepted) == (
        "failed", "error_during_execution", False)
    assert frames(steps) == ["init", "close"]
    assert steps[-1].events[-1].data["session_failure"] == "cwd_unavailable"


def test_c26_5_an_unattributed_error_before_acknowledgement_ends_the_turn_and_a_limit_is_still_classified():
    """C-26.5, C-26.7: an error `result` that neither names a message nor says Claude Code
    began its turn, before the message was acknowledged, is taken as the session's failure
    and ends the turn as before, classified as a limit when one was reported."""
    turn, _ = run([INIT_OK, LIMIT, row(type="result", subtype="success", is_error=True, result="You've hit your limit")])
    assert (turn.outcome.state, turn.outcome.reason, turn.outcome.limited, turn.outcome.accepted) == (
        "failed", "limited", True, False)


def test_c26_5_a_notification_turn_that_hits_the_limit_leaves_the_message_to_its_own_result():
    """C-26.5, C-26.7: a notification turn refused for quota is not the message's; the
    message's own turn then meets the same limit and is classified by it."""
    rows = [INIT_OK, lifecycle("queued"), LIMIT,
            notification_result(ok=False, words="You've hit your session limit", subtype="success"),
            lifecycle("started"), ECHO, LIMIT, our_result(ok=False, subtype="success", words="You've hit your limit")]
    turn, steps = run(rows)
    assert steps[4].outcome is None
    assert (turn.outcome.state, turn.outcome.reason, turn.outcome.accepted) == ("failed", "limited", True)


def test_c26_7_a_session_that_ends_without_a_result_after_a_limit_records_it():
    """C-26.7: stdout ended with no result after the session reported a limit, in whichever
    turn: the account's refusal is the best evidence of why, so finalization records the
    closure (the message itself is reconciled, C-24.6)."""
    turn, _ = run([INIT_OK, lifecycle("queued"), LIMIT])
    outcome = turn.eof(999).outcome
    assert (outcome.state, outcome.reason, outcome.limited, outcome.ended_by) == (
        "failed", "ended-without-result", True, "eof")
    turn, _ = run([INIT_OK, lifecycle("queued")])
    assert turn.eof(999).outcome.limited is False


def test_c24_4_a_result_naming_the_message_acknowledges_it():
    """C-24.4: a `result` whose `user_message_uuids` holds the message is the provider saying
    it consumed it, even with no echo or lifecycle before it."""
    turn, steps = run([INIT_OK, our_result()])
    assert [(e.kind, e.data.get("by")) for e in steps[-1].events][:1] == [("accepted", "result")]
    assert turn.outcome.state == "complete" and turn.outcome.accepted


def test_c26_5_a_result_naming_other_messages_is_not_ours_even_after_ours_started():
    """C-26.5: a `result` that names other messages only is another turn's, whenever it comes."""
    turn, steps = run([INIT_OK, ECHO, our_result(names=(OTHER,)), our_result()])
    assert steps[-2].outcome is None and steps[-2].events[0].kind == "turn.other"
    assert turn.outcome.state == "complete"


def test_c24_7_a_stop_while_the_message_waits_takes_it_out_of_the_queue():
    """C-24.7, C-26.5: the interrupt carries `cancel_queued`; the notification turn it
    aborts is not the message's; the queue's `cancelled` is the provider's last word on
    the message, which ends stopped, not acknowledged, and leaves the conversation free."""
    rows = [INIT_OK, lifecycle("queued"), SYS_INIT, REQUESTING,
            notification_result(ok=False, terminal_reason="aborted_streaming"), lifecycle("cancelled")]
    turn, steps = run(rows, interrupt_before=3)
    interrupt = json.loads(next(f.line for s in steps for f in s.frames if f.tag == "interrupt"))
    assert interrupt["request"] == {"subtype": "interrupt", "cancel_queued": True}
    assert steps[-2].outcome is None
    outcome = turn.outcome
    assert (outcome.state, outcome.reason, outcome.accepted, outcome.ended_by, outcome.not_run) == (
        "interrupted", "stopped", False, "provider", "cancelled")
    # C-24.6: reconciled by the transcript; with no record of it, stopped, the conversation free.
    absent = reconcile.Evidence(acknowledged=False, frame="written", process_gone=True, native="absent")
    settled = reconcile.settle({**outcome.__dict__, "stop_reason": "stopped"}, provider="claude", turn_seq=0,
                               gather=lambda: absent)
    assert (settled.state, settled.reason, settled.block) == ("interrupted", "stopped", None)


def test_c24_7_the_interrupt_s_receipt_listing_the_message_as_cancelled_ends_it_too():
    """C-26.5: `interrupt_receipt_v1` lists what `cancel_queued` took out of the queue."""
    receipt = row(type="control_response", response={"subtype": "success", "request_id": "subfleet-interrupt",
                                                      "response": {"still_queued": [], "cancelled": [MID]}})
    turn, _ = run([INIT_OK, lifecycle("queued"), receipt], interrupt_before=2)
    assert (turn.outcome.state, turn.outcome.not_run, turn.outcome.accepted) == ("interrupted", "cancelled", False)
    kept = row(type="control_response", response={"subtype": "success", "request_id": "subfleet-interrupt",
                                                   "response": {"still_queued": [MID]}})
    turn, _ = run([INIT_OK, lifecycle("queued"), kept], interrupt_before=2)
    assert turn.outcome is None                          # an older CLI kept it: the stop escalates


def test_c26_5_a_session_failure_before_acknowledgement_takes_the_waiting_message_out_first():
    """C-26.5 (review of 6f2b54e4, finding 3): closing stdin cancels nothing queued, so a
    session failure that ends the turn before the message started sends the interrupt
    with `cancel_queued` ahead of the close; the message never runs unwatched after it."""
    failure = row(type="result", subtype="error_during_execution", is_error=True, num_turns=0, errors=["boom"])
    turn, steps = run([INIT_OK, lifecycle("queued"), failure])
    assert frames(steps) == ["init", "user-message", "settings", "interrupt", "close"]
    assert json.loads(steps[-1].frames[0].line)["request"]["cancel_queued"] is True
    assert (turn.outcome.state, turn.outcome.reason) == ("failed", "error_during_execution")
    turn, steps = run([failure])                         # nothing was sent yet: nothing to take out
    assert frames(steps) == ["init", "close"]


def test_c26_5_a_startup_failure_ends_the_turn_whatever_else_the_result_says():
    """C-26.5 (review of 6f2b54e4, finding 6): `startup_failure_reason` is checked first,
    after acknowledgement and on a result that calls itself a success."""
    odd = row(type="result", subtype="success", is_error=False, num_turns=0, startup_failure_reason="bypass_root",
              user_message_uuid=MID)
    turn, steps = run([INIT_OK, ECHO, odd])
    assert (turn.outcome.state, turn.outcome.reason) == ("failed", "success")
    assert steps[-1].events[-1].data["session_failure"] == "bypass_root"


def test_c26_7_a_limit_another_turn_met_does_not_classify_the_message():
    """C-26.7 (review of 6f2b54e4, finding 1): a notification turn refused for quota
    before the message started does not make the message `limited`: not when the queue
    then discards it, and not when its own turn fails for another reason."""
    turn, _ = run([INIT_OK, lifecycle("queued"), LIMIT,
                   notification_result(ok=False, subtype="success", words="You've hit your session limit"),
                   lifecycle("discarded")])
    assert (turn.outcome.reason, turn.outcome.limited, turn.outcome.not_run) == ("provider-discarded", False, "discarded")
    turn, _ = run([INIT_OK, lifecycle("queued"), LIMIT, notification_result(ok=False, subtype="success"),
                   lifecycle("started"), ECHO, our_result(ok=False)])
    assert (turn.outcome.reason, turn.outcome.limited) == ("error_during_execution", False)
    # A result that names the message with nothing before it: the limit in its turn is its own.
    turn, _ = run([INIT_OK, LIMIT, our_result(ok=False, subtype="success")])
    assert (turn.outcome.reason, turn.outcome.limited) == ("limited", True)


@pytest.mark.parametrize("state", ["refused", "discarded"])
def test_c26_5_a_message_the_queue_will_not_run_fails_without_waiting(state):
    """C-26.5: `refused` and `discarded` before the message started mean it will not run in
    this session; no `result` would come for it."""
    turn, _ = run([INIT_OK, lifecycle(state)])
    assert (turn.outcome.state, turn.outcome.reason, turn.outcome.accepted) == ("failed", f"provider-{state}", False)


def test_c26_5_lifecycle_states_after_the_message_started_leave_the_end_to_its_result():
    """C-26.5: `cancelled` after `started` (a turn that consumed the message was aborted)
    and every other command's lifecycle change nothing; the result decides."""
    turn, steps = run([INIT_OK, lifecycle("queued"), lifecycle("started"), lifecycle("cancelled"),
                       lifecycle("refused", OTHER), row(type="result", subtype="error_during_execution",
                                                        is_error=True, num_turns=2)])
    assert all(step.outcome is None for step in steps[:-1])
    assert (turn.outcome.state, turn.outcome.reason, turn.outcome.accepted) == (
        "failed", "error_during_execution", True)


def test_c24_8_a_notification_turn_after_a_driver_stop_is_not_the_provider_finishing():
    """C-24.8: after the driver's own stop (a model mismatch) only the message's `result`
    says the provider finished its turn; a notification turn's after it does not."""
    wrong = row(type="system", subtype="init", model="claude-haiku-4-5", session_id=SID)
    turn, _ = run([INIT_OK, ECHO, wrong, notification_result()])
    assert turn.outcome.reason == "model-mismatch" and not turn.terminal_after_end
    turn.feed(row(type="result", subtype="error_during_execution", is_error=True, user_message_uuid=MID), 900)
    assert turn.terminal_after_end


def test_c26_5_another_turn_s_output_is_shown_but_answers_nothing():
    """C-26.5: what a notification turn writes is session output the person sees, but it
    is no answer to the message: `answered` counts only output after acknowledgement,
    whether streamed, whole, or a tool call."""
    tool = row(type="assistant", parent_tool_use_id=None, message={"id": "msg_t", "model": MODEL, "content": [
        {"type": "tool_use", "id": "tu_n", "name": "Bash", "input": {"command": "ls"}}]})
    delta = row(type="stream_event", event={"type": "content_block_delta", "index": 0,
                                            "delta": {"type": "text_delta", "text": "Looking\n"}})
    turn, steps = run([INIT_OK, lifecycle("queued"), delta, tool, text("msg_n", "The background build finished."),
                       notification_result(words="The background build finished.")])
    assert [e.kind for e in events(steps)].count("text") == 1 and not turn.answered
    turn, _ = run([ECHO, text("msg_1", "Fixed."), our_result()], turn)
    assert turn.outcome.answered


def test_c24_4_a_message_known_only_by_its_result_s_name_keeps_its_answer():
    """C-24.4, C-26.5 (review of 6f2b54e4, correctness finding 2): with no echo or
    lifecycle before it, what the message's own turn wrote counts once its result names
    it; what another turn wrote before that turn's result does not."""
    turn, _ = run([INIT_OK, text("msg_n", "Background noted."), notification_result(),
                   text("msg_1", "Fixed."), our_result()])
    assert turn.outcome.state == "complete" and turn.outcome.answered
    turn, _ = run([INIT_OK, text("msg_n", "Background noted."), notification_result(), our_result()])
    assert turn.outcome.state == "complete" and not turn.outcome.answered


def test_c26_5_after_the_message_started_the_next_result_is_its_turn_s():
    """C-26.5 (review of 6f2b54e4, correctness finding 5): `command_lifecycle started` says
    the message drained into the one running turn, a fold into a turn Claude Code began
    included, so the next result is that turn's even if it names nobody and gives that
    origin; the message never waits on a result that named nobody."""
    turn, steps = run([INIT_OK, lifecycle("queued"), lifecycle("started"), notification_result()])
    assert (turn.outcome.state, turn.outcome.accepted) == ("complete", True)
    turn, steps = run([INIT_OK, ECHO, notification_result()])       # the echo alone says less
    assert turn.outcome is None


def test_c24_7_cancelling_another_turn_s_request_before_the_message_started_stops_the_message():
    """C-24.7, C-26.5 (review of 6f2b54e4, correctness finding 4): a tool request from a
    turn the session ran before the message started shows on the message; the person's
    cancel-turn there also takes the message out of the queue."""
    ask = row(type="control_request", request_id="r1", request={"subtype": "can_use_tool", "tool_name": "Bash",
                                                                "input": {"command": "ls"}, "tool_use_id": "tu1"})
    turn, _ = run([INIT_OK, lifecycle("queued"), ask])
    step = turn.respond("r1", "cancel-turn")
    assert [f.tag for f in step.frames] == ["approval:r1", "interrupt"]
    assert json.loads(step.frames[1].line)["request"]["cancel_queued"] is True
    turn.feed(notification_result(ok=False, terminal_reason="aborted_tools"), 900)
    turn.feed(lifecycle("cancelled"), 1000)
    assert (turn.outcome.state, turn.outcome.reason, turn.outcome.not_run) == ("interrupted", "stopped", "cancelled")
    turn, _ = run([INIT_OK, ECHO, ask])                                # the message's own request: no second stop
    assert [f.tag for f in turn.respond("r1", "cancel-turn").frames] == ["approval:r1"]


def test_c24_6_a_stop_the_driver_sent_itself_that_swept_the_message_is_recorded():
    """C-24.6, C-26.8 (review of 6f2b54e4, correctness finding 3): a model mismatch before
    the message started ends the turn with an interrupt that carries `cancel_queued`; the
    queue's `cancelled` after that is recorded, so reconciliation knows it will not run."""
    wrong = row(type="system", subtype="init", model="claude-haiku-4-5", session_id=SID)
    turn, _ = run([INIT_OK, lifecycle("queued"), wrong, notification_result(ok=False), lifecycle("cancelled")])
    assert (turn.outcome.reason, turn.not_run, turn.outcome.not_run) == ("model-mismatch", "cancelled", None)
    settled = reconcile.settle({**turn.outcome.__dict__, "not_run": turn.not_run}, provider="claude", turn_seq=0,
                               gather=lambda: reconcile.Evidence(acknowledged=False, frame="written",
                                                                 process_gone=True, native="absent"))
    assert (settled.state, settled.reason, settled.block) == ("failed", "model-mismatch", None)


def test_c24_6_a_session_failure_is_reconciled_not_taken_as_the_message_s_end():
    """C-24.6, C-26.5 (review of 6f2b54e4, correctness finding 6): the session's failure is
    no terminal event of the message's, so the driver ends the turn and its delivery is
    reconciled: before the message was written, not delivered."""
    failure = row(type="result", subtype="error_during_execution", is_error=True, num_turns=0,
                  startup_failure_reason="cwd_unavailable")
    turn, _ = run([failure])
    assert turn.outcome.ended_by == "driver"
    gone = reconcile.Evidence(acknowledged=False, frame="absent", process_gone=True, native="absent")
    settled = reconcile.settle(turn.outcome.__dict__, provider="claude", turn_seq=0, gather=lambda: gone)
    assert (settled.state, settled.reason, settled.block) == ("failed", "not-delivered: error_during_execution", None)


def test_c26_6_a_replay_of_the_traced_case_is_identical():
    """C-26.6: rebuilt from the same stdout, the driver emits the same events and frames."""
    _, first = run(TRACED)
    _, second = run(TRACED)
    assert [(e.kind, e.source, e.data) for e in events(first)] == [(e.kind, e.source, e.data) for e in events(second)]
    assert frames(first) == frames(second)


# --- properties --------------------------------------------------------------------------------

ORIGINS = st.sampled_from(["task-notification", "peer", "channel"])


@st.composite
def other_turn(draw, index: int) -> list[str]:
    """One turn Claude Code runs for something else while the message waits."""
    rows = []
    if draw(st.booleans()):
        rows.append(SYS_INIT)
    if draw(st.booleans()):
        rows.append(text(f"msg_other_{index}", "Background work noted."))
    if draw(st.booleans()):
        rows.append(LIMIT)
    ok = draw(st.booleans())
    if draw(st.booleans()):
        # Named: another client's message (a peer's) that this session ran.
        rows.append(our_result(ok=ok, names=(OTHER,), num_turns=1, words="other"))
    else:
        rows.append(notification_result(ok=ok, origin=draw(ORIGINS), index=index))
    return rows


@st.composite
def well_formed(draw) -> tuple[list[str], dict]:
    """A stream the CLI could write for our message: other turns first (perhaps), our
    message acknowledged by `started`, its echo, both, or only its result's name, our
    turn, our `result`, and other turns' results after it. Returns the rows and what a
    position-based reading says the outcome is."""
    rows = [draw(st.sampled_from([HOOK, NOTICE]))] if draw(st.booleans()) else []
    if draw(st.booleans()):
        rows.append(notification_result(index=0))            # before `initialize` is answered
    rows.append(INIT_OK)
    if draw(st.booleans()):
        rows.append(lifecycle("queued"))
    for index in range(draw(st.integers(0, 3))):
        rows += draw(other_turn(index + 1))
    ack = draw(st.sampled_from(["started", "echo", "both", "name-only"]))
    if ack in ("started", "both"):
        rows.append(lifecycle("started"))
    if ack in ("echo", "both"):
        rows.append(ECHO)
    for n in range(draw(st.integers(0, 2))):
        rows.append(text(f"msg_ours_{n}", f"step {n}"))
    ours_limited = draw(st.booleans())
    if ours_limited:
        rows.append(LIMIT)
    ok = draw(st.booleans())
    num_turns = draw(st.integers(1, 50))
    named = True if ack == "name-only" else draw(st.booleans())
    ours = our_result(ok=ok, named=named, num_turns=num_turns,
                      subtype=None if ok else draw(st.sampled_from(["error_during_execution", "success"])))
    ours_at = len(rows)
    rows.append(ours)
    for index in range(draw(st.integers(0, 2))):
        rows.append(notification_result(index=10 + index))
    limited = ours_limited                                  # its own turn's; others' limits were theirs
    body = json.loads(ours)
    if ok:
        expected = ("complete", None)
    elif limited:
        expected = ("failed", "limited")
    else:
        expected = ("failed", body["subtype"])
    return rows, {"ours_at": ours_at, "state": expected, "num_turns": num_turns, "ack": ack}


def terminal_steps(steps) -> list:
    return [step for step in steps if step.outcome is not None]


def check_one_outcome(turn: ClaudeTurn, steps) -> None:
    """I2: one step carries the outcome; exactly one `turn.completed` records it."""
    carriers = terminal_steps(steps)
    completed = [e for e in events(steps) if e.kind == "turn.completed"]
    if turn.outcome is None:
        assert carriers == [] and completed == []
    else:
        assert len(carriers) == 1 and carriers[0].outcome is turn.outcome
        assert len(completed) == 1 and completed[0].data["state"] == turn.outcome.state
    sources = [e.source for e in events(steps)]
    assert len(sources) == len(set(sources))


@hypothesis.given(well_formed())
@hypothesis.settings(max_examples=300, deadline=None)
def test_property_the_message_s_own_result_decides_its_turn(case):
    """I1 to I5 on well-formed streams, against the position-based reference."""
    rows, want = case
    turn, steps = run(rows)
    check_one_outcome(turn, steps)                                           # I2
    assert (turn.outcome.state, turn.outcome.reason) == want["state"]       # I4
    assert turn.outcome.accepted and turn.outcome.ended_by == "provider"
    before = rows[:want["ours_at"]]                                          # I1, from the stream itself
    assert lifecycle("started") in before or ECHO in before or want["ack"] == "name-only"
    carrier = terminal_steps(steps)[0]
    assert steps.index(carrier) == want["ours_at"] + 1                       # ends on our own row (I3)
    assert carrier.events[-1].data["num_turns"] == want["num_turns"]
    assert "close" not in frames(steps[:want["ours_at"] + 1])                # nothing closed before it
    replayed, again = run(rows)                                              # I5
    assert [(e.kind, e.source, e.data) for e in events(steps)] == [(e.kind, e.source, e.data) for e in events(again)]
    assert frames(steps) == frames(again) and replayed.outcome == turn.outcome


#: Every row shape the driver reads that bears on which turn a result closes, each tagged
#: by hand with what it is, so the oracle below never asks the driver (review of
#: 6f2b54e4, finding 4). Results: `startup` (a startup failure), `named-ours`,
#: `named-other`, `began` (unnamed, Claude Code began the turn: an origin other than
#: human), `plain` (unnamed, no such origin). Other rows: `ack` (the echo acknowledges
#: the message), `started` (its lifecycle does, and it drained into a turn), `not-run`
#: (the queue will not run it), `none`.
START = row(type="stream_event", event={"type": "message_start", "message": {"id": "msg_s"}})
DELTA = row(type="stream_event", event={"type": "content_block_delta", "index": 0,
                                        "delta": {"type": "text_delta", "text": "partial"}})
TAGGED = [
    *((line, "none") for line in (HOOK, NOTICE, SYS_INIT, REQUESTING, LIMIT, text("msg_a", "a"),
                                  text("msg_b", "b"), START, DELTA, lifecycle("queued"), lifecycle("completed"),
                                  lifecycle("queued", OTHER), lifecycle("started", OTHER),
                                  lifecycle("cancelled", OTHER))),
    (ECHO, "ack"), (lifecycle("started"), "started"),
    *((lifecycle(state), "not-run") for state in ("cancelled", "discarded", "refused")),
    (notification_result(), "began"), (notification_result(ok=False), "began"),
    (notification_result(ok=False, origin="peer"), "began"), (notification_result(origin="channel"), "began"),
    (notification_result(origin=None), "plain"), (notification_result(ok=False, origin=None), "plain"),
    (notification_result(origin="human"), "plain"), (notification_result(ok=False, origin="human"), "plain"),
    (our_result(named=False), "plain"), (our_result(ok=False, named=False), "plain"),
    (our_result(), "named-ours"), (our_result(ok=False), "named-ours"), (our_result(names=(OTHER, MID)), "named-ours"),
    (our_result(ok=False, subtype="success"), "named-ours"), (our_result(names=(OTHER,)), "named-other"),
    (row(type="result", subtype="error_during_execution", is_error=True, num_turns=0,
         startup_failure_reason="temp_dir_unusable"), "startup"),
    (row(type="result", subtype="success", is_error=False, num_turns=0, startup_failure_reason="bypass_root",
         user_message_uuid=MID), "startup"),
]


def oracle(tagged: list[tuple[str, str]], stop: int | None) -> tuple[int | None, list[int], dict[int, bool]]:
    """The documented rule, read off the tags: the row index the turn ends at (-1: the
    stop before the message was sent), the indexes of results that are another turn's,
    and whether the message was acknowledged by each row (by its stream, never the driver)."""
    acked = started = False
    others, acked_at = [], {}
    for index, (line, tag) in enumerate(tagged):
        if stop == 0 and index == 0:
            return -1, others, acked_at                  # stopped while `initialize` was unanswered
        body = json.loads(line)
        ok = body.get("is_error") is False and body.get("subtype") == "success"
        if tag in ("ack", "started") and not acked:
            acked, started = True, tag == "started"
        acked_at[index] = acked or tag == "named-ours"
        if tag == "not-run" and not acked:
            return index, others, acked_at
        if tag in ("began", "plain") and started:
            return index, others, acked_at               # the one turn it drained into
        if tag in ("named-other", "began") or (tag == "plain" and not acked and ok):
            others.append(index)
        elif tag in ("startup", "named-ours", "plain"):
            return index, others, acked_at
    return None, others, acked_at


@hypothesis.given(st.lists(st.sampled_from(TAGGED), max_size=14), st.integers(0, 15), st.booleans())
@hypothesis.settings(max_examples=800, deadline=None)
def test_property_no_stream_completes_an_unacknowledged_message_or_ends_it_on_another_s_result(tagged, cut, stop):
    """I1, I2, I3, I5 on any sequence of those rows, the message sent first (INIT_OK), a
    stop landing anywhere or nowhere, against the tag oracle."""
    tagged = [(INIT_OK, "none"), *tagged]
    rows = [line for line, _ in tagged]
    stop_at = cut if stop else None
    turn, steps = run(rows, interrupt_before=stop_at)
    check_one_outcome(turn, steps)                                           # I2
    want_end, others, acked_at = oracle(tagged, stop_at)
    probe = ClaudeTurn(spec(), read_bytes=lambda p: b"")
    probe.start()
    offset, ended_at = 0, None
    for index, line in enumerate(rows):
        if index == stop_at:
            if probe.interrupt().outcome is not None and ended_at is None:
                ended_at = -1
        step = probe.feed(line, offset)
        offset += len(line) + 1
        if step.outcome is not None and ended_at is None:
            ended_at = index
        if index in others and (want_end is None or index < want_end):          # I3
            kinds = [e.kind for e in step.events]
            assert step.outcome is None and step.frames == [] and kinds.count("turn.other") == 1
    assert ended_at == want_end, (ended_at, want_end)
    if turn.outcome is not None and turn.outcome.state == "complete":
        assert want_end is not None and acked_at[want_end]                   # I1, by the stream's own evidence
    assert probe.outcome == turn.outcome                                      # I5
    if turn.outcome is None:
        assert "close" not in frames(steps)


# --- Codex --------------------------------------------------------------------------------------


def codex_sent(turn: CodexTurn) -> None:
    """Through the thread answer: `turn/start` is written, its answer not yet read."""
    codex.to_thread(turn)
    turn.feed(codex.resp(ID_THREAD, {"thread": {"id": "thr-1", "status": {"type": "idle"}}, "model": "gpt-6-astra"}), 3)


def test_c26_5_a_codex_turn_announced_before_turn_start_was_sent_is_not_the_message_s():
    """C-26.5, C-24.4: none has been seen (5 of 5 Codex turn attempts on 2026-09-29 ended on
    their own turn), but a `turn/started` or `turn/completed` before `turn/start` was written
    can be no answer to it: it neither acknowledges nor ends the message's turn."""
    turn = CodexTurn(codex.spec(native_session_id="thr-1"))     # a resumed thread: its id is known
    codex.to_thread(turn)
    started = turn.feed(codex.note("turn/started", threadId="thr-1", turn={"id": "turn-0", "status": "inProgress"}), 3)
    done = turn.feed(codex.note("turn/completed", threadId="thr-1", turn={"id": "turn-0", "status": "completed"}), 4)
    assert [e.kind for e in started.events + done.events] == ["turn.other", "turn.other"]
    assert not turn.accepted and turn.outcome is None and done.frames == []


def test_c26_5_a_codex_turn_under_another_id_neither_acknowledges_nor_ends_the_message_s():
    turn = CodexTurn(codex.spec())
    codex.to_running(turn)
    other = turn.feed(codex.note("turn/completed", threadId="thr-1", turn={"id": "turn-9", "status": "completed"}), 5)
    assert other.outcome is None and other.events[0].kind == "turn.other"
    ours = turn.feed(codex.note("turn/completed", threadId="thr-1", turn={"id": "turn-1", "status": "completed"}), 6)
    assert ours.outcome.state == "complete" and turn.turn_id == "turn-1"


def test_c26_5_a_codex_turn_completed_before_turn_start_is_answered_is_still_the_message_s():
    """C-24.4: once `turn/start` is written, a turn the server announces before it answers
    is the one it started (the answer can lag the notification)."""
    turn = CodexTurn(codex.spec())
    codex_sent(turn)
    done = turn.feed(codex.note("turn/completed", threadId="thr-1", turn={"id": "turn-1", "status": "completed"}), 4)
    assert done.outcome.state == "complete" and turn.accepted and turn.turn_id == "turn-1"


CODEX_NOTES = st.sampled_from([
    codex.note(method, threadId="thr-1", turn={"id": turn_id, "status": status})
    for method in ("turn/started", "turn/completed")
    for turn_id in ("turn-1", "turn-0", "turn-9")
    for status in ("completed", "inProgress", "failed")
])


@hypothesis.given(st.lists(CODEX_NOTES, max_size=8), st.integers(0, 8), st.booleans())
@hypothesis.settings(max_examples=300, deadline=None)
def test_property_a_codex_turn_ends_once_and_after_its_answer_only_on_its_own_turn(notes, at, answered):
    """I1, I2, I3 for Codex. Whatever turns are announced around `turn/start`'s answer
    (`turn-1`): one outcome at most, never complete unacknowledged, and once the answer is
    read, the turn it names is the only one that acknowledges or ends the message."""
    turn = CodexTurn(codex.spec())
    codex_sent(turn)
    answer = codex.resp(ID_TURN, {"turn": {"id": "turn-1", "status": "inProgress", "items": []}})
    at = min(at, len(notes))
    stream = [*notes[:at], answer, *notes[at:]] if answered else list(notes)
    steps = [turn.feed(line, offset) for offset, line in enumerate(stream, start=10)]
    carriers = terminal_steps(steps)
    assert len(carriers) <= 1                                                     # I2
    assert [e.kind for e in events(steps)].count("turn.completed") == len(carriers)
    if turn.outcome is not None and turn.outcome.state == "complete":
        assert turn.accepted                                                       # I1
    if answered and not terminal_steps(steps[:at]):
        # Read while the turn was live, the answer names the message's turn (a turn the
        # server announced before answering is taken as the one it started: C-24.4).
        assert turn.turn_id == "turn-1"
        for line, step in zip(stream[at + 1:], steps[at + 1:]):                     # I3, after the answer
            if json.loads(line)["params"]["turn"]["id"] != "turn-1":
                assert step.outcome is None and step.frames == []
                assert [e.kind for e in step.events] in ([], ["turn.other"])
