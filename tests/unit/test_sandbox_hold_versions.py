"""C-11.10 (D-RO-ADMIT1, fix r1 of #309): sandbox holds across CLI and daemon versions.

Review r1's probes p6 and p7 failed first on `AF_UNIX path too long`. These use the
short `/tmp` state root of `tests/unit/conftest.py`.

- p6, a new CLI and an older daemon: the CLI asks `capabilities` before it sends a
  sandbox target. Without `tasks.sandbox.v1` it refuses `task-hold-capability`
  (exit 6) naming that capability, and the daemon hears no `tasks` request.
- A task target is unchanged: no capabilities preflight, the old wire shape.
- A new CLI and a new daemon: the real advertisement lets the request through.
"""

from __future__ import annotations

import json

import pytest

from subfleet import cli, protocol
from subfleet.contracts import Exit
from tests.task_hold_support import fresh_daemon


UNTIL = "2099-06-01T00:00:00Z"
ARGV = {"hold": ["tasks", "hold", "--sandbox", "workspace-write", "--until", UNTIL, "--note", "disk: brake"],
        "release": ["tasks", "release", "--sandbox", "workspace-write"]}
REFUSAL = {"refused": "task-hold-capability", "capability": "tasks.sandbox.v1"}
#: What an older daemon answers `capabilities` with: release/217 before #309 (both
#: condition capabilities), one that lists no tasks capability, and one older than the op.
OLDER = {
    "release-217-before-309": {"capabilities": lambda request: {"protocol": 1, "capabilities": [
        protocol.TASKS_IF_NOTE_PREFIX_CAPABILITY, protocol.TASKS_IF_NOTE_PREFIX_V2_CAPABILITY]}},
    "no-tasks-capability": {"capabilities": lambda request: {"protocol": 1, "capabilities": ["conversations.v1"]}},
    "no-capabilities-op": {},
}


def test_the_daemon_advertises_sandbox_holds():
    from subfleet.conversations import service
    assert protocol.TASKS_SANDBOX_CAPABILITY == "tasks.sandbox.v1"
    assert protocol.TASKS_SANDBOX_CAPABILITY in service.CAPABILITIES


@pytest.mark.parametrize("older", OLDER)
@pytest.mark.parametrize("prefix", [None, "disk:"], ids=["plain", "conditional"])
@pytest.mark.parametrize("action", ["hold", "release"])
@pytest.mark.parametrize("as_json", [True, False], ids=["json", "prose"])
def test_p6_a_new_cli_sends_no_sandbox_target_to_an_older_daemon(daemon, capsys, older, prefix, action, as_json):
    sent = []
    server = daemon({**OLDER[older], "tasks": lambda request: sent.append(request.args) or {}})
    argv = [*ARGV[action], *(["--if-note-prefix", prefix] if prefix else []), *(["--json"] if as_json else [])]
    assert cli.main(argv) == Exit.CLI_TOO_OLD == 6
    captured = capsys.readouterr()
    assert (json.loads(captured.out) if as_json else captured.out) == (REFUSAL if as_json else "")
    assert ("task-hold-capability: tasks " + action + " --sandbox needs daemon capability "
            "`tasks.sandbox.v1`; this daemon does not support sandbox holds") in captured.err
    assert "fix: subfleet daemon stop && subfleet daemon start" in captured.err
    assert "unknown task" not in captured.err
    assert server.ops() == ["capabilities"] and sent == []


@pytest.mark.parametrize("older", OLDER)
def test_a_task_target_still_sends_the_old_shape_without_a_preflight(daemon, capsys, older):
    server = daemon({**OLDER[older], "tasks": lambda request: {"released": "build", "was_held": False,
                                                               "woken": 0, "task_holds": []}})
    assert cli.main(["tasks", "release", "build"]) == 0
    assert server.ops() == ["tasks"]
    assert server.args("tasks") == {"action": "release", "task": "build", "until": None, "note": None}


@pytest.mark.parametrize("prefix", [None, "disk:"], ids=["plain", "conditional"])
def test_a_new_cli_and_daemon_hold_and_release_the_sandbox_through_the_socket(daemon, capsys, prefix):
    with fresh_daemon() as (service, harness, clock, signals):
        def forward(request):
            try:
                if service.conversations.owns(request.op):
                    return service.conversations.handle(request.op, request.args, None)
                return service.dispatch(request.op, request.args)
            except protocol.ProtocolError as error:
                return protocol.refuse(request.id, error)

        server = daemon({"capabilities": forward, "tasks": forward})
        condition = ["--if-note-prefix", prefix] if prefix else []
        assert cli.main([*ARGV["hold"], *condition, "--json"]) == 0
        held = json.loads(capsys.readouterr().out)
        assert held["task_hold"]["sandbox"] == "workspace-write" and held["task_hold"]["until"] == UNTIL
        assert service.store.one("SELECT expires_at FROM leases WHERE lease_key='sandbox-hold:workspace-write'")
        assert cli.main([*ARGV["release"], *condition, "--json"]) == 0
        released = json.loads(capsys.readouterr().out)
        assert (released["released"], released["was_held"]) == ("workspace-write", True)
        assert service.store.one("SELECT 1 FROM leases WHERE lease_key='sandbox-hold:workspace-write'") is None
        assert server.ops() == ["capabilities", "tasks", "capabilities", "tasks"]
        actions = [request.args["action"] for request in server.requests if request.op == "tasks"]
        assert actions == (["hold-if", "release-if"] if prefix else ["hold", "release"])
