"""D-RO-ADMIT1: sandbox holds keep the tasks CLI's wire and display contracts."""

from __future__ import annotations

import json

import pytest

from subfleet import cli, protocol
from subfleet.client import DaemonError


UNTIL = "2026-11-12T18:00:00Z"
HOLD = {"sandbox": "workspace-write", "until": UNTIL, "note": "disk: preserve room",
        "since": "2026-11-12T17:00:00Z"}
HOLD_ARGV = ["tasks", "hold", "--sandbox", "workspace-write", "--until", UNTIL,
             "--note", HOLD["note"]]
RELEASE_ARGV = ["tasks", "release", "--sandbox", "workspace-write"]


def client_for(monkeypatch, answer, *, capabilities=()):
    calls = []

    class Client:
        def call(self, op, args):
            calls.append((op, args))
            if op == "capabilities":
                return {"capabilities": list(capabilities)}
            if isinstance(answer, BaseException):
                raise answer
            return answer

    monkeypatch.setattr(cli, "_client", lambda args: Client())
    return calls


@pytest.mark.parametrize("argv", [HOLD_ARGV, RELEASE_ARGV], ids=["hold", "release"])
def test_sandbox_target_parses_to_tasks_handler(argv):
    args = cli.build_parser().parse_args(argv)
    assert args.handler is cli.cmd_tasks
    assert args.task is None
    assert args.sandbox == "workspace-write"


@pytest.mark.parametrize("action", ["hold", "release"])
@pytest.mark.parametrize("target", [[], ["build", "--sandbox", "workspace-write"],
                                    ["--sandbox", "read-only"]], ids=["missing", "both", "unsupported"])
def test_task_or_supported_sandbox_is_required_before_connecting(monkeypatch, capsys, action, target):
    calls = client_for(monkeypatch, {})
    assert cli.main(["tasks", action, *target]) == 2
    assert calls == []
    assert capsys.readouterr().err


@pytest.mark.parametrize("argv,expected", [
    (HOLD_ARGV, {"action": "hold", "task": None, "sandbox": "workspace-write", "until": UNTIL,
                 "note": HOLD["note"]}),
    (RELEASE_ARGV, {"action": "release", "task": None, "sandbox": "workspace-write", "until": None,
                    "note": None}),
])
def test_sandbox_hold_and_release_send_one_explicit_target(monkeypatch, capsys, argv, expected):
    calls = client_for(monkeypatch, {"task_hold": HOLD})
    assert cli.main([*argv, "--json"]) == 0
    assert calls == [("tasks", expected)]
    assert json.loads(capsys.readouterr().out) == {"task_hold": HOLD}


def test_hold_success_and_both_list_targets_are_named(monkeypatch, capsys):
    client_for(monkeypatch, {"task_hold": HOLD})
    assert cli.main(HOLD_ARGV) == 0
    assert capsys.readouterr().out == f"held: sandbox workspace-write until {UNTIL} ({HOLD['note']})\n"
    task_hold = {"task": "review", "until": UNTIL, "note": "manual", "since": HOLD["since"]}
    client_for(monkeypatch, {"task_holds": [task_hold, HOLD]})
    assert cli.main(["tasks", "list"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"review until {UNTIL}: manual",
                                                  f"sandbox workspace-write until {UNTIL}: {HOLD['note']}"]


@pytest.mark.parametrize("argv", [HOLD_ARGV, RELEASE_ARGV], ids=["hold", "release"])
def test_sandbox_condition_reuses_note_prefix_capability(monkeypatch, capsys, argv):
    calls = client_for(monkeypatch, {"task_hold": HOLD},
                       capabilities=[protocol.TASKS_IF_NOTE_PREFIX_CAPABILITY])
    assert cli.main([*argv, "--if-note-prefix", " disk: ", "--json"]) == 0
    assert [op for op, _ in calls] == ["capabilities", "tasks"]
    assert calls[-1][1]["sandbox"] == "workspace-write"
    assert calls[-1][1]["if_note_prefix"] == "disk:"
    assert json.loads(capsys.readouterr().out) == {"task_hold": HOLD}


def test_sandbox_condition_refusal_keeps_structured_target(monkeypatch, capsys):
    error = DaemonError(7, "operator owns this hold", reason="task-hold-condition", details={"task_hold": HOLD})
    client_for(monkeypatch, error, capabilities=[protocol.TASKS_IF_NOTE_PREFIX_CAPABILITY])
    assert cli.main([*RELEASE_ARGV, "--if-note-prefix", "other:", "--json"]) == 7
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"refused": "task-hold-condition", "task_hold": HOLD}
    assert "task-hold-condition" in captured.err


@pytest.mark.parametrize("argv", [["tasks", "--help"], ["tasks", "hold", "--help"],
                                  ["tasks", "release", "--help"]])
def test_help_states_sandbox_target_and_jobs_never_held(capsys, argv):
    assert cli.main(argv) == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "--sandbox workspace-write" in text
    assert "Conversation turns and jobs with no task are never held" in text
    assert "running attempts are never touched" in text


def test_status_names_sandbox_and_counts_its_stored_sandboxes():
    jobs = [{"job_id": str(i), "kind": kind, "task": task, "sandbox": sandbox,
             "state": "waiting", "wait_reason": "task-held"}
            for i, (kind, task, sandbox) in enumerate([
                ("dispatch", "review", "workspace-write"),
                ("dispatch", "build", ""),
                ("dispatch", "build", "read-only"),
                ("turn", "review", "workspace-write"),
                ("dispatch", None, "workspace-write"),
            ])]
    data = {"lanes": [], "readings": [], "closures": [], "task_holds": [HOLD], "jobs": jobs}
    assert f"sandbox workspace-write until {UNTIL} (2 waiting): {HOLD['note']}" in cli.format_status(data)
    assert "(19 waiting)" in cli.format_status({**data, "task_holds": [{**HOLD, "waiting": 19}]})
