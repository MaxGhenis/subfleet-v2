"""C-11.10 (D-RO-ADMIT1, fix r1 of #309): sandbox holds across CLI and daemon versions.

Review r1's probes p6 and p7 failed first on `AF_UNIX path too long`. These use the
short `/tmp` state root of `tests/unit/conftest.py`.

- p6, a new CLI and an older daemon: the CLI asks `capabilities` before it sends a
  sandbox target. Without `tasks.sandbox.v1` it refuses `task-hold-capability`
  (exit 6) naming that capability, and the daemon hears no `tasks` request.
- A task target is unchanged: no capabilities preflight, the old wire shape.
- A new CLI and a new daemon: the real advertisement lets the request through.
- p7 and issue #327: the older CLI's `?` hint follows task release rules with
  and without a policy task or hold row. C-11.10 documents those outcomes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from subfleet import cli, protocol
from subfleet.contracts import Exit
from subfleet.policy import load_policy
from tests.task_hold_support import NOTE, attempts, fresh_daemon, held_now, hold, job, refused, restart, store_image, submit


UNTIL = "2099-06-01T00:00:00Z"
ARGV = {"hold": ["tasks", "hold", "--sandbox", "workspace-write", "--until", UNTIL, "--note", "disk: brake"],
        "release": ["tasks", "release", "--sandbox", "workspace-write"]}
REFUSAL = {"refused": "task-hold-capability", "capability": "tasks.sandbox.v1"}
HINT = {"action": "release", "task": "?", "until": None, "note": None}
SANDBOX_ROW = ("SELECT holder, acquired_at, expires_at FROM leases "
               "WHERE lease_key='sandbox-hold:workspace-write'")
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


def test_an_older_clis_release_hint_is_refused_without_a_question_mark_task():
    """p7: the fixture policy has no task `?` and there is no `task-hold:?` row.
    The pre-#309 CLI's hint is refused `unknown-task` (exit 2) with no writes.
    """
    with fresh_daemon() as (service, harness, clock, signals):
        service.dispatch("tasks", {"action": "hold", "sandbox": "workspace-write", "until": UNTIL,
                                   "note": "disk brake"})
        before = store_image(service)
        error = refused(service, HINT)
        assert (error.code, error.reason) == (Exit.INVALID_INPUT, "unknown-task")
        assert store_image(service) == before


def with_question_mark_task(service, harness):
    path = harness.root / "policy.json"
    policy = json.loads(path.read_text())
    chains = policy["chains"]
    chains["?"] = list(chains["lookup"])
    path.write_text(json.dumps(policy, indent=2) + "\n")
    assert "?" in load_policy(path)["chains"]
    service = restart(service, harness)
    assert "?" in service.policy["chains"]
    return service


def test_documented_old_cli_hint_can_release_a_valid_question_mark_task_hold():
    with fresh_daemon() as (service, harness, clock, signals):
        service = with_question_mark_task(service, harness)
        qm = submit(service, harness, "?", "qm-job")
        service.dispatch("tasks", {"action": "hold", "sandbox": "workspace-write", "until": UNTIL,
                                   "note": "disk brake"})
        hold(service, "?", UNTIL)
        service._admit()
        waiting = job(service, qm)
        assert (waiting["state"], waiting["wait_reason"]) == ("waiting", "task-held")
        assert attempts(service, qm) == []
        sandbox_row = service.store.one(SANDBOX_ROW)
        assert sandbox_row is not None
        last_event = service.store.one("SELECT MAX(event_id) AS event_id FROM events")["event_id"]

        answer = service.dispatch("tasks", HINT)

        assert (answer["released"], answer["was_held"], answer["woken"]) == ("?", True, 1)
        assert service.store.one("SELECT 1 FROM leases WHERE lease_key='task-hold:?'") is None
        assert service.store.one(SANDBOX_ROW) == sandbox_row
        [sandbox_hold] = held_now(service)
        assert sandbox_hold["sandbox"] == "workspace-write"
        [event] = service.store.query("SELECT data_json FROM events WHERE event_id > ? AND kind='task.released'",
                                      (last_event,))
        data = json.loads(event["data_json"])
        assert data["task"] == "?"
        assert "sandbox" not in data
        queued = job(service, qm)
        assert (queued["state"], queued["wait_reason"], queued["next_check_at"]) == ("queued", None, None)

        service._admit()
        assert len(attempts(service, qm)) == 1


def test_the_older_cli_hint_releases_a_question_mark_row_the_policy_no_longer_routes():
    with fresh_daemon() as (service, harness, clock, signals):
        path = harness.root / "policy.json"
        original = path.read_text()
        service = with_question_mark_task(service, harness)
        service.dispatch("tasks", {"action": "hold", "sandbox": "workspace-write", "until": UNTIL,
                                   "note": "disk brake"})
        hold(service, "?", UNTIL)

        path.write_text(original)
        service = restart(service, harness)
        assert "?" not in service.policy["chains"]
        assert refused(service, {"action": "hold", "task": "?", "until": UNTIL, "note": NOTE}).reason == "unknown-task"
        sandbox_row = service.store.one(SANDBOX_ROW)
        assert sandbox_row is not None
        last_event = service.store.one("SELECT MAX(event_id) AS event_id FROM events")["event_id"]

        answer = service.dispatch("tasks", HINT)

        assert (answer["released"], answer["was_held"], answer["woken"]) == ("?", True, 0)
        assert service.store.one("SELECT 1 FROM leases WHERE lease_key='task-hold:?'") is None
        assert service.store.one(SANDBOX_ROW) == sandbox_row
        [event] = service.store.query("SELECT data_json FROM events WHERE event_id > ? AND kind='task.released'",
                                      (last_event,))
        data = json.loads(event["data_json"])
        assert data["task"] == "?"
        assert "sandbox" not in data

        before = store_image(service)
        changes = service.store.connection.total_changes
        error = refused(service, HINT)
        assert (error.code, error.reason) == (Exit.INVALID_INPUT, "unknown-task")
        assert service.store.connection.total_changes == changes
        assert store_image(service) == before


def test_the_older_cli_hint_with_a_question_mark_task_and_no_row_writes_nothing():
    with fresh_daemon() as (service, harness, clock, signals):
        service = with_question_mark_task(service, harness)
        service.dispatch("tasks", {"action": "hold", "sandbox": "workspace-write", "until": UNTIL,
                                   "note": "disk brake"})
        before = store_image(service)
        changes = service.store.connection.total_changes

        answer = service.dispatch("tasks", HINT)

        assert (answer["released"], answer["was_held"], answer["woken"]) == ("?", False, 0)
        assert service.store.connection.total_changes == changes
        assert store_image(service) == before


def test_c_11_10_states_what_the_older_cli_hint_releases():
    root = Path(__file__).resolve().parents[2]
    contract = (root / "docs" / "acceptance-contract.md").read_text()
    clause = next(line for line in contract.splitlines() if line.startswith("- **C-11.10**"))
    assert "which the daemon refuses `unknown-task` (exit 2), releasing nothing;" not in clause
    assert "never of the sandbox hold" in clause
    assert "Policy validation accepts any nonblank task name, `?` included" in clause
    assert "while a `task-hold:?` row is there" in clause

    older_entry = next(line for line in contract.splitlines() if line.startswith("- C-11.10 (2026-11-14, D-RO-ADMIT1"))
    assert "which releases nothing)" not in older_entry
    assert "which never reaches the sandbox hold but releases a hold on a task named" in older_entry

    report = "docs/reports/2026-11-26-release-hint-doc.md"
    assert any("issue #327" in line and report in line
               for line in contract.splitlines() if line.startswith("- C-11.10 ("))
    assert (root / report).is_file()
