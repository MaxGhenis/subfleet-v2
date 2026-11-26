"""C-11.10: a real stored blocking hold reaches CLI JSON through the socket."""

from __future__ import annotations

import json

import pytest

from subfleet import cli, protocol, task_holds
from tests.disk_floor_model import stamp
from tests.task_hold_support import fresh_daemon, hold, store_image


@pytest.mark.parametrize("action,now", [("hold", 60), ("release", 60), ("release", 601)],
                         ids=["hold-active", "release-active", "release-expired"])
def test_real_blocking_hold_survives_daemon_socket_and_cli_json(daemon, capsys, action, now):
    with fresh_daemon() as (service, harness, clock, signals):
        blocking = hold(service, "build", stamp(600), note=" booking: operator ")["task_hold"]
        assert blocking["note"] == "booking: operator"
        clock[0] = now
        before = {table: service.store.query(f"SELECT * FROM {table} ORDER BY rowid")
                  for table in ("leases", "jobs", "events")}

        def forward(request):
            try:
                # Match the real socket router: discovery belongs to the
                # conversation service, while tasks use the daemon dispatcher.
                if service.conversations.owns(request.op):
                    return service.conversations.handle(request.op, request.args, None)
                return service.dispatch(request.op, request.args)
            except protocol.ProtocolError as error:
                return protocol.refuse(request.id, error)

        server = daemon({"capabilities": forward, "tasks": forward})
        argv = ["tasks", action, "build", "--if-note-prefix", "battery-backoff:", "--json"]
        if action == "hold":
            argv.extend(["--until", stamp(1800), "--note", "battery-backoff: hot"])
        assert cli.main(argv) == 7
        captured = capsys.readouterr()
        response = json.loads(captured.out)  # Additional prose or JSON objects fail this parse.
        assert response == {"refused": "task-hold-condition", "task_hold": blocking}
        assert set(response["task_hold"]) == {"task", "until", "note", "since"}
        assert {key: type(value) for key, value in response["task_hold"].items()} == {
            key: type(value) for key, value in blocking.items()}
        assert "task-hold-condition" in captured.err
        for value in blocking.values():
            assert value in captured.err
        assert server.ops() == ["tasks"]
        assert server.args("tasks")["action"] == f"{action}-if"
        assert server.args("tasks")["if_note_prefix"] == "battery-backoff:"
        assert {table: service.store.query(f"SELECT * FROM {table} ORDER BY rowid") for table in before} == before


@pytest.mark.parametrize("action", ["hold", "release"])
@pytest.mark.parametrize("changed", [
    {"holder": task_holds.HOLDER + "booking-sk-" + "a" * 25},
    {"expires_at": "not-an-instant"},
], ids=["masked-note", "invalid-end"])
def test_unshowable_blocking_row_keeps_condition_json_and_store(daemon, capsys, action, changed):
    """Legacy hand-written rows may block a write without safe display details."""
    with fresh_daemon() as (service, harness, clock, signals):
        hold(service, "build", stamp(600), note="booking: operator")
        with service.store.transaction("test.legacy_task_hold") as tx:
            for column, value in changed.items():
                tx.execute(f"UPDATE leases SET {column}=? WHERE lease_key=?",
                           (value, task_holds.lease_key("build")))
        clock[0] = 60
        before = store_image(service)

        def forward(request):
            try:
                return service.dispatch(request.op, request.args)
            except protocol.ProtocolError as error:
                return protocol.refuse(request.id, error)

        server = daemon({"tasks": forward})
        argv = ["tasks", action, "build", "--if-note-prefix", "battery-backoff:", "--json"]
        if action == "hold":
            argv.extend(["--until", stamp(1800), "--note", "battery-backoff: hot"])
        assert cli.main(argv) == 7
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"refused": "task-hold-condition", "task_hold": None}
        assert "task-hold-condition" in captured.err
        assert "booking-sk-" not in captured.out + captured.err
        assert "not-an-instant" not in captured.out + captured.err
        assert server.ops() == ["tasks"]
        assert server.args("tasks")["action"] == f"{action}-if"
        assert store_image(service) == before
