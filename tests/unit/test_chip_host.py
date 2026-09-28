"""C-31.1: real MCP framing, scoped tool surface, and turn launch isolation."""

from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from subfleet.client import DaemonError, DaemonUnavailable, ResponseLost
from subfleet.conversations.chip_host import ChipHost, MAX_LINE_BYTES, SERVER_NAME, TOOLS, mcp_config, serve
from subfleet.conversations.claude_turn import argv
from subfleet.conversations.turn import TurnSpec
from tests.unit.test_conversation_service import SETTINGS, conversation, svc  # noqa: F401


class Client:
    def __init__(self, failures=()):
        self.calls = []
        self.failures = list(failures)

    def call(self, op, args, **kwargs):
        self.calls.append((op, args, kwargs))
        if self.failures:
            raise self.failures.pop(0)
        return {"chip": {"chip_id": "task-1", "state": "pending" if op == "chip.spawn" else "dismissed",
                         "title": "Fix the tests"}}


def rpc(method, params=None, **extra):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}, **extra}


def host(client=None):
    value = ChipHost(client or Client(), conversation_id="parent", message_id="origin", token="secret-token")
    value.handle(rpc("initialize", {"protocolVersion": "2025-06-18"}))
    return value


def test_desktop_parameter_names_and_required_fields():
    schemas = {t["name"]: t["inputSchema"] for t in TOOLS}
    assert set(schemas) == {"spawn_task", "dismiss_task"}
    assert set(schemas["spawn_task"]["properties"]) == {"title", "prompt", "tldr", "cwd"}
    assert set(schemas["spawn_task"]["required"]) == {"title", "prompt", "tldr"}
    assert set(schemas["dismiss_task"]["properties"]) == {"task_id", "reason"}
    assert schemas["dismiss_task"]["required"] == ["task_id"]


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "future"])
def test_initialization_negotiates_an_implemented_version(version):
    value = host()
    result = value.handle(rpc("initialize", {"protocolVersion": version}))["result"]
    assert result["protocolVersion"] == ("2025-06-18" if version == "future" else version)
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert value.handle(rpc("tools/list"))["result"]["tools"] == TOOLS
    assert value.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_spawn_and_dismiss_bind_scope_and_return_desktop_task_id():
    value = host()
    args = {"title": "Fix the tests", "prompt": "  Exact\nmessage 🐈\n", "tldr": "Follow up.", "cwd": "/project"}
    result = value.handle(rpc("tools/call", {"name": "spawn_task", "arguments": args}))["result"]
    assert json.loads(result["content"][0]["text"])["task_id"] == "task-1"
    op, sent, _ = value.client.calls[0]
    assert op == "chip.spawn" and sent["prompt"] == args["prompt"]
    assert {k: sent[k] for k in value.binding} == value.binding
    assert sent["request_id"]
    assert "secret-token" not in json.dumps(result)
    value.handle(rpc("tools/call", {"name": "dismiss_task", "arguments": {"task_id": "task-1", "reason": "Merged"}}))
    assert value.client.calls[1][0:2] == ("chip.dismiss", {**value.binding, "chip_id": "task-1", "reason": "Merged"})


@pytest.mark.parametrize("params", [
    {"name": "chip.start", "arguments": {}},
    {"name": "spawn_task", "arguments": {"title": "missing fields"}},
    {"name": "spawn_task", "arguments": {"title": "x", "prompt": "x", "tldr": "x", "conversation_id": "other"}},
    {"name": "spawn_task", "arguments": {"title": "x", "prompt": "x", "tldr": "x", "cwd": None}},
    {"name": "dismiss_task", "arguments": {"task_id": 123}},
    {"name": "dismiss_task", "arguments": []},
])
def test_unknown_tools_and_bad_inputs_cannot_reach_daemon(params):
    value = host()
    assert value.handle(rpc("tools/call", params))["error"]["code"] == -32602
    assert not value.client.calls


def test_lost_spawn_response_retries_identical_idempotency_key():
    value = host(Client([ResponseLost("connection closed")]))
    result = value.handle(rpc("tools/call", {"name": "spawn_task", "arguments": {"title": "x", "prompt": "p", "tldr": "s"}}))
    assert not result["result"]["isError"]
    assert value.client.calls[0] == value.client.calls[1]


def test_busy_retry_does_not_claim_an_unanswered_spawn_failed():
    value = host(Client([ResponseLost("connection closed"), DaemonError(69, "busy, try again")]))
    result = value.handle(rpc("tools/call", {"name": "spawn_task", "arguments": {
        "title": "Follow up", "prompt": "Inspect files", "tldr": "Separate work"}}))["result"]
    assert result["isError"]
    assert "outcome is unknown" in result["content"][0]["text"]
    assert "inspect the conversation's chips" in result["content"][0]["text"]
    assert value.client.calls[0] == value.client.calls[1]


@pytest.mark.parametrize("failures", [
    [DaemonError(2, "bad cwd")], [DaemonUnavailable("no socket")],
    [ResponseLost("lost"), ResponseLost("lost again")],
])
def test_tool_failures_are_visible_without_claiming_a_start(failures):
    value = host(Client(failures))
    result = value.handle(rpc("tools/call", {"name": "dismiss_task", "arguments": {"task_id": "task-1"}}))["result"]
    assert result["isError"] and result["content"][0]["text"]


def test_stdio_handles_parse_errors_notifications_and_bounds():
    rows = [b"broken\n", b"[]\n", json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode() + b"\n",
            json.dumps(rpc("ping")).encode() + b"\n", b"x" * (MAX_LINE_BYTES + 1)]
    output = io.StringIO()
    serve(host(), io.BytesIO(b"".join(rows)), output)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [r.get("error", {}).get("code") for r in replies] == [-32700, -32600, None, -32600]


def test_tools_require_initialization_and_unknown_methods_are_refused():
    value = ChipHost(Client(), conversation_id="parent", message_id="origin", token="token")
    assert value.handle(rpc("tools/list"))["error"]["code"] == -32000
    assert host().handle(rpc("resources/list"))["error"]["code"] == -32601


@pytest.mark.parametrize("permission", ["ask", "accept-edits", "bypass", "read-only"])
def test_launch_adds_only_chip_tools_without_weakening_read_only(permission):
    spec = TurnSpec(provider="claude", message_id="m", text="prompt", model_id="opus", permission=permission,
                    native_session_id="session")
    config = mcp_config({"root": "/state", "conversation_id": "parent", "message_id": "m", "token": "token"})
    read_only = ("--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}')
    command = argv(spec, chip_mcp_config=config, read_only_flags=read_only)
    actual = json.loads(command[command.index("--mcp-config") + 1])
    if permission == "read-only":
        assert actual == {"mcpServers": {}}
        assert "--allowedTools" not in command and command.count("--mcp-config") == 1
    else:
        assert actual == config
        assert "--strict-mcp-config" not in command
        assert command[command.index("--allowedTools") + 1].split(",") == [
            f"mcp__{SERVER_NAME}__spawn_task", f"mcp__{SERVER_NAME}__dismiss_task"]


def test_configured_command_runs_from_an_unrelated_directory(tmp_path):
    config = mcp_config({"root": str(tmp_path / "unused"), "conversation_id": "parent", "message_id": "m", "token": "token"})
    server = config["mcpServers"][SERVER_NAME]
    assert Path(server["command"]).is_absolute() and server["command"] == sys.executable
    result = subprocess.run([server["command"], *server["args"]], cwd=tmp_path,
                            input=json.dumps(rpc("initialize", {"protocolVersion": "2025-06-18"})) + "\n" +
                                  json.dumps(rpc("tools/list", id="list")) + "\n",
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert replies[0]["result"]["serverInfo"]["name"] == SERVER_NAME
    assert replies[1]["id"] == "list" and len(replies[1]["result"]["tools"]) == 2
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("permission", ["ask", "read-only"])
@pytest.mark.parametrize("resumed", [False, True])
def test_service_launch_binds_host_only_to_writable_turns(svc, monkeypatch, permission, resumed):
    from subfleet.conversations import service as service_module
    settings = {**SETTINGS, "permission": permission}
    cid = conversation(svc, settings=settings)
    mid = "acee2400-422f-4c89-815a-5fa7302efacf"
    svc.store.submit_message(conversation_id=cid, message_id=mid, after_message_id=None,
                             text="Propose follow-up work", attachments=[], settings=settings)
    turn = {"conversation_id": cid, "message_id": mid, "settings": settings,
            "native_session_id": "native" if resumed else None}
    monkeypatch.setattr(service_module, "_read_json", lambda _: {"turn": turn})
    monkeypatch.setattr(service_module, "claude_launch", lambda supplied, **kwargs: (supplied, kwargs))
    supplied, launch = svc.launch({"job_id": "job"}, {"attempt_id": "job/a1"},
                                  SimpleNamespace(provider="claude"), {}, svc.root / "attempt", "opus")
    assert supplied == turn
    if permission == "read-only":
        assert launch["chip_host"] is None
        assert svc.store.query("SELECT * FROM conversation_chip_hosts") == []
    else:
        binding = launch["chip_host"]
        assert binding["root"] == str(svc.root)
        assert (binding["conversation_id"], binding["message_id"]) == (cid, mid)
        result = svc.chips.spawn({"conversation_id": cid, "message_id": mid, "host_token": binding["token"],
                                 "request_id": "request", "title": "Follow up", "tldr": "Separate work.",
                                 "prompt": "Read the files."})
        assert result["chip"]["parent_conversation_id"] == cid
