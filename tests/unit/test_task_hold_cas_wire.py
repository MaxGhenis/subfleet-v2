"""C-11.10: a condition refusal carries a checked hold, alongside legacy error text."""

from __future__ import annotations

import json

import pytest

from subfleet import protocol, refusals
from subfleet.client import Client, DaemonError
from subfleet.contracts import Exit
from subfleet.conversations.service import CAPABILITIES
from subfleet.sessions.handoff import scrub_secrets
from tests.unit.test_protocol_secrecy import wire_reply

HOLD = {"task": "build", "until": "2026-11-07T12:10:00Z", "note": "operator: booking",
        "since": "2026-11-07T12:00:00Z"}


def condition(hold=HOLD, **extra):
    return protocol.ProtocolError("untrusted exception words", Exit.REFUSED,
                                  fix="untrusted repair", reason="task-hold-condition",
                                  details={"task_hold": hold, **extra})


def test_this_daemon_advertises_transactional_task_note_conditions():
    assert protocol.TASKS_IF_NOTE_PREFIX_CAPABILITY == "tasks.if_note_prefix.v1"
    assert protocol.TASKS_IF_NOTE_PREFIX_V2_CAPABILITY == "tasks.if_note_prefix.v2"
    assert protocol.TASKS_IF_NOTE_PREFIX_CAPABILITY in CAPABILITIES
    assert protocol.TASKS_IF_NOTE_PREFIX_V2_CAPABILITY in CAPABILITIES
    args = protocol.coerce_args(protocol.TasksArgs, {"if_note_prefix": "battery-backoff:", "unknown": True})
    assert args.if_note_prefix == "battery-backoff:"


def test_condition_refusal_reaches_the_client_as_structured_checked_hold(daemon, root):
    daemon({"tasks": lambda request: protocol.refuse(request.id, condition())})
    with pytest.raises(DaemonError) as caught:
        Client(root).call("tasks", {"action": "release", "task": "build", "if_note_prefix": "battery-backoff:"})
    error = caught.value
    assert error.code == 7 and error.reason == "task-hold-condition"
    assert error.details == {"task_hold": HOLD}
    assert "untrusted" not in str(error) and error.fix is None
    for value in HOLD.values():
        assert value in str(error)


def test_condition_wire_error_is_additive_and_an_older_decoder_can_still_read_it():
    response = wire_reply(op="tasks", error=condition())
    error = response["error"]
    assert error["code"] == 7
    assert error["reason"] == "task-hold-condition" and error["details"] == {"task_hold": HOLD}
    assert set(error) == {"code", "message", "fix", "reason", "details"}
    # a8624a489's decoder projects these three fields, dropping unknown ones.
    old_error = {key: error.get(key) for key in ("code", "message", "fix")}
    assert old_error["code"] == 7 and "operator: booking" in old_error["message"]
    assert old_error["fix"] is None


def test_existing_refusals_keep_their_exact_wire_error_shape():
    response = wire_reply(op="tasks", error=protocol.ProtocolError(
        "ignored", reason="task-hold-until", details={"task_hold": HOLD}))
    assert response["error"] == {"code": 2, "message": "tasks hold: --until must be a later ISO 8601 instant",
                                  "fix": None}
    decoded = protocol.decode_response(json.dumps(response))
    assert decoded.error.reason is None and decoded.error.details is None


@pytest.mark.parametrize("changed", [
    {"task": 3}, {"until": 3}, {"until": "diagnostic words"}, {"note": ["untrusted"]},
    {"note": "untrusted\nwords"}, {"note": "é" * 161}, {"since": 3}, {"since": "diagnostic words"},
    {"extra": "diagnostic words"},
])
def test_invalid_hold_details_are_not_a_generic_refusal_reflection_channel(changed):
    response = wire_reply(op="tasks", error=condition({**HOLD, **changed}))
    assert response["error"] == {"code": 7, "message": refusals.HANDLER_MESSAGES["task-hold-condition"][0],
                                  "fix": None, "reason": "task-hold-condition"}


def test_incomplete_or_extra_details_are_not_exposed():
    incomplete = {key: value for key, value in HOLD.items() if key != "since"}
    for error in (condition(incomplete), condition(extra="untrusted words")):
        shown = wire_reply(op="tasks", error=error)["error"]
        assert "details" not in shown and shown["reason"] == "task-hold-condition"
        assert "untrusted" not in shown["message"]


def test_nullable_since_keeps_the_hold_reply_value_type():
    hold = {**HOLD, "since": None}
    shown = wire_reply(op="tasks", error=condition(hold))["error"]
    assert shown["details"] == {"task_hold": hold}
    assert "since" not in shown["message"] and "None" not in shown["message"]


def test_legacy_credential_note_is_scrubbed_equally_in_text_and_structured_details():
    secret = "ghp_" + "A" * 36
    note = "operator booking " + secret
    clean, count = scrub_secrets(note, whole=True, strip_reminders=True)
    assert count > 0
    shown = wire_reply(op="tasks", error=condition({**HOLD, "note": note}))["error"]
    assert shown["details"]["task_hold"]["note"] == clean
    assert clean in shown["message"] and secret not in json.dumps(shown)


def test_client_does_not_trust_unchecked_arbitrary_structured_details():
    error = DaemonError(7, "opaque older message", reason="task-hold-condition",
                        details={"task_hold": {**HOLD, "since": "untrusted words"}})
    assert error.reason == "task-hold-condition" and error.details is None


def test_a_legacy_note_with_an_embedded_joiner_credential_is_never_reflected():
    secret = "sk-" + "a" * 25
    shown = wire_reply(op="tasks", error=condition({**HOLD, "note": "booking-" + secret}))["error"]
    assert secret not in json.dumps(shown)
    assert shown["reason"] == "task-hold-condition" and "details" not in shown
