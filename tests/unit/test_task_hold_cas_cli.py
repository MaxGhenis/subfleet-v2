"""D-TASKHOLD-CAS1: ownership conditions survive the CLI and daemon boundary.

The condition travels in actions that old daemons refuse before a write. Refusals
keep their structured hold on stdout in JSON mode, with prose confined to stderr,
and the two new outcomes have codes distinct from all pre-existing tasks outcomes.
"""

from __future__ import annotations

import json

import pytest

from subfleet import cli, protocol
from subfleet.client import DaemonError, DaemonUnavailable
from subfleet.contracts import Exit


CAPABILITY = "tasks.if_note_prefix.v2"
HOLD = {"task": "build", "until": "2026-11-07T12:10:00Z", "note": "booking: operator",
        "since": "2026-11-07T12:00:00Z"}
HOLD_ARGV = ["tasks", "hold", "build", "--until", HOLD["until"], "--note", "battery-backoff: hot"]
RELEASE_ARGV = ["tasks", "release", "build"]
CONDITION = ["--if-note-prefix", "battery-backoff:"]


def capabilities(request):
    return {"protocol": 1, "capabilities": [CAPABILITY]}


def refusal(request):
    return protocol.refuse(request.id, protocol.ProtocolError(
        "private diagnostic", Exit.REFUSED, reason="task-hold-condition", details={"task_hold": HOLD}))


def invalid_action(request):
    return protocol.refuse(request.id, protocol.ProtocolError("ignored", Exit.INVALID_INPUT, reason="tasks-action"))


@pytest.mark.parametrize("argv", [HOLD_ARGV, RELEASE_ARGV], ids=["hold", "release"])
@pytest.mark.parametrize("advertised", [None, [], [protocol.PUSH_CAPABILITY], ["tasks.if_note_prefix.v1"],
                                         "tasks.if_note_prefix.v2", [0, None, {}]],
                         ids=["absent", "empty", "unrelated", "v1-only", "string", "invalid"])
def test_refused_conditional_action_queries_capability_before_exit_6(daemon, capsys, argv, advertised):
    server = daemon({"capabilities": lambda request: {"capabilities": advertised}, "tasks": invalid_action})
    assert cli.main([*argv, *CONDITION, "--json"]) == 6
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"refused": "task-hold-capability", "capability": CAPABILITY}
    assert CAPABILITY in captured.err and "task-hold-capability" in captured.err
    assert "fix: subfleet daemon stop && subfleet daemon start" in captured.err
    assert server.ops() == ["tasks", "capabilities"]
    assert server.args("tasks")["action"] == f"{argv[1]}-if"
    assert server.args("tasks")["if_note_prefix"] == "battery-backoff:"


@pytest.mark.parametrize("argv", [HOLD_ARGV, RELEASE_ARGV], ids=["hold", "release"])
def test_daemon_predating_capabilities_refuses_conditional_action_first(daemon, capsys, argv):
    server = daemon({"tasks": invalid_action})
    assert cli.main([*argv, *CONDITION]) == 6
    captured = capsys.readouterr()
    assert captured.out == "" and CAPABILITY in captured.err
    assert server.ops() == ["tasks", "capabilities"]


@pytest.mark.parametrize("argv", [HOLD_ARGV, RELEASE_ARGV], ids=["hold", "release"])
def test_condition_uses_refusable_action_without_capability_preflight(daemon, capsys, argv):
    server = daemon({"tasks": lambda request: {"task_hold": HOLD}})
    assert cli.main([*argv, *CONDITION, "--json"]) == 0
    assert server.ops() == ["tasks"]
    assert server.args("tasks")["action"] == f"{argv[1]}-if"
    assert server.args("tasks")["if_note_prefix"] == "battery-backoff:"
    assert json.loads(capsys.readouterr().out) == {"task_hold": HOLD}


@pytest.mark.parametrize("argv", [HOLD_ARGV, RELEASE_ARGV], ids=["hold", "release"])
@pytest.mark.parametrize("reason", ["tasks-action", "task-hold-note", "unknown-task"])
def test_invalid_conditional_request_keeps_original_refusal_when_v2_is_advertised(daemon, capsys, argv, reason):
    def invalid_request(request):
        return protocol.refuse(request.id, protocol.ProtocolError("ignored", Exit.INVALID_INPUT, reason=reason))

    server = daemon({"capabilities": capabilities, "tasks": invalid_request})
    assert cli.main([*argv, *CONDITION, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "task-hold-capability" not in captured.err
    assert server.ops() == ["tasks", "capabilities"]


@pytest.mark.parametrize("argv,expected", [
    (["tasks"], {"action": "list", "task": None, "until": None, "note": None}),
    (["tasks", "list"], {"action": "list", "task": None, "until": None, "note": None}),
    (HOLD_ARGV, {"action": "hold", "task": "build", "until": HOLD["until"], "note": "battery-backoff: hot"}),
    (RELEASE_ARGV, {"action": "release", "task": "build", "until": None, "note": None}),
])
def test_unconditional_requests_keep_old_payload_without_capability_query(daemon, capsys, argv, expected):
    server = daemon({"tasks": lambda request: {"task_hold": HOLD}})
    assert cli.main([*argv, "--json"]) == 0
    assert server.ops() == ["tasks"]
    assert server.args("tasks") == expected
    assert json.loads(capsys.readouterr().out) == {"task_hold": HOLD}


@pytest.mark.parametrize("argv", [HOLD_ARGV, RELEASE_ARGV], ids=["hold", "release"])
@pytest.mark.parametrize("prefix", ["", "   ", "x\ny", "\nbattery-backoff:\n", "a\tb", "\x7f", "\u2028",
                                     "x" * 161, "sk-ant-oat01-" + "Q" * 40,
                                     " battery-backoff:", "battery-backoff: ", "  battery-backoff:  "])
def test_bad_prefix_is_task_hold_note_before_any_request(daemon, capsys, argv, prefix):
    server = daemon({"capabilities": capabilities, "tasks": lambda request: {}})
    assert cli.main([*argv, "--if-note-prefix", prefix]) == 2
    assert "task-hold-note" in capsys.readouterr().err
    assert server.ops() == []


@pytest.mark.parametrize("new_note,prefix", [
    ("booking: manual", "battery-backoff:"),
    ("Battery-backoff: hot", "battery-backoff:"),
    ("some battery-backoff: hot", "battery-backoff:"),
])
def test_new_hold_note_must_start_with_prefix_before_any_request(daemon, capsys, new_note, prefix):
    server = daemon({"capabilities": capabilities, "tasks": lambda request: {}})
    argv = ["tasks", "hold", "build", "--until", HOLD["until"], "--note", new_note,
            "--if-note-prefix", prefix]
    assert cli.main(argv) == 2
    assert "task-hold-note" in capsys.readouterr().err
    assert server.ops() == []


def test_new_hold_note_is_checked_after_note_trimming(daemon, capsys):
    server = daemon({"capabilities": capabilities, "tasks": lambda request: {"task_hold": HOLD}})
    argv = ["tasks", "hold", "build", "--until", HOLD["until"], "--note", " battery-backoff: hot ",
            "--if-note-prefix", "battery-backoff:", "--json"]
    assert cli.main(argv) == 0
    assert server.args("tasks")["if_note_prefix"] == "battery-backoff:"
    capsys.readouterr()


@pytest.mark.parametrize("argv", [HOLD_ARGV, RELEASE_ARGV], ids=["hold", "release"])
@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_condition_refusal_names_all_blocking_hold_fields_and_exit_7(daemon, capsys, argv, json_mode):
    server = daemon({"capabilities": capabilities, "tasks": refusal})
    assert cli.main([*argv, *CONDITION, *(["--json"] if json_mode else [])]) == 7
    captured = capsys.readouterr()
    assert "task-hold-condition" in captured.err
    for value in HOLD.values():
        assert value in captured.err
    if json_mode:
        result = json.loads(captured.out)
        assert result == {"refused": "task-hold-condition", "task_hold": HOLD}
        assert set(result["task_hold"]) == {"task", "until", "note", "since"}
        assert {key: type(value) for key, value in result["task_hold"].items()} == {
            key: type(value) for key, value in HOLD.items()}
    else:
        assert captured.out == ""
    assert server.ops() == ["tasks"]


def test_json_refusal_uses_structured_details_instead_of_message_parsing(daemon, capsys):
    def opaque_message(request):
        response = refusal(request)
        response.error.message = "diagnostic words without a hold"
        return response

    daemon({"capabilities": capabilities, "tasks": opaque_message})
    assert cli.main([*RELEASE_ARGV, *CONDITION, "--json"]) == 7
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"refused": "task-hold-condition", "task_hold": HOLD}
    assert "diagnostic words" in captured.err


def test_a_blocking_note_containing_unknown_op_still_returns_condition_code(daemon, capsys):
    def unknown_op_note(request):
        return protocol.refuse(request.id, protocol.ProtocolError(
            "private diagnostic", Exit.REFUSED, reason="task-hold-condition",
            details={"task_hold": {**HOLD, "note": "booking: unknown op"}}))

    daemon({"capabilities": capabilities, "tasks": unknown_op_note})
    assert cli.main([*RELEASE_ARGV, *CONDITION, "--json"]) == 7
    captured = capsys.readouterr()
    assert json.loads(captured.out)["task_hold"]["note"] == "booking: unknown op"
    assert "booking: unknown op" in captured.err


def test_tasks_outcome_codes_are_distinct_by_outcome_category(monkeypatch, capsys):
    """Existing tasks paths use 0, 1, 2 and 69; ownership and skew use 7 and 6."""
    outcomes = [
        ("list-success", ["tasks"], {"task_holds": []}, 0),
        ("hold-success", HOLD_ARGV, {"task_hold": HOLD}, 0),
        ("release-success", RELEASE_ARGV, {"released": "build", "was_held": True}, 0),
        ("release-absent", RELEASE_ARGV, {"released": "build", "was_held": False}, 0),
        ("cli-invalid-input", ["tasks", "hold", "build"], {}, 2),
        ("daemon-invalid-input", RELEASE_ARGV, DaemonError(2, "tasks: unknown task builds"), 2),
        ("malformed-response", RELEASE_ARGV, protocol.ProtocolError("bad response", Exit.OPERATIONAL), 1),
        ("daemon-unavailable", RELEASE_ARGV, DaemonUnavailable("fixture daemon unavailable"), 69),
        ("daemon-predates-tasks", RELEASE_ARGV, DaemonError(2, protocol.UNKNOWN_OP), 69),
        ("condition-failed", [*RELEASE_ARGV, *CONDITION],
         DaemonError(7, "task-hold-condition", reason="task-hold-condition", details={"task_hold": HOLD}), 7),
        ("capability-missing", [*RELEASE_ARGV, *CONDITION], DaemonError(2, "invalid tasks action"), 6),
    ]
    observed = {}
    for name, argv, answer, expected in outcomes:
        class StubClient:
            def call(self, op, args):
                if op == "capabilities":
                    return {"capabilities": [] if name == "capability-missing" else [CAPABILITY]}
                if isinstance(answer, BaseException):
                    raise answer
                return answer

        monkeypatch.setattr(cli, "_client", lambda args: StubClient())
        observed[name] = cli.main(argv)
        assert observed[name] == expected, name
        capsys.readouterr()
    old_codes = {code for name, code in observed.items() if name not in {"condition-failed", "capability-missing"}}
    assert old_codes == {0, 1, 2, 69}
    assert observed["condition-failed"] not in old_codes
    assert observed["capability-missing"] not in old_codes | {observed["condition-failed"]}


@pytest.mark.parametrize("argv", [["tasks", "--help"], ["tasks", "hold", "--help"],
                                  ["tasks", "release", "--help"]])
def test_tasks_help_names_condition_exit_codes_and_json_contract(capsys, argv):
    assert cli.main(argv) == 0
    text = capsys.readouterr().out
    assert "--if-note-prefix" in text
    assert "Exit 7" in text and "exit 6" in text
    assert CAPABILITY in text and "stdout" in text and "stderr" in text
    assert "surrounding spaces" in text


@pytest.mark.parametrize("argv", [HOLD_ARGV, RELEASE_ARGV], ids=["hold", "release"])
def test_json_condition_refusal_without_details_prints_null(daemon, capsys, argv):
    def no_details(request):
        return protocol.refuse(request.id, protocol.ProtocolError(
            "private diagnostic", Exit.REFUSED, reason="task-hold-condition"))

    server = daemon({"tasks": no_details})
    assert cli.main([*argv, *CONDITION, "--json"]) == 7
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"refused": "task-hold-condition", "task_hold": None}
    assert "task-hold-condition" in captured.err and "private diagnostic" not in captured.err
    assert server.ops() == ["tasks"]
