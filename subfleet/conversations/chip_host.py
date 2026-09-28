"""The CLI-launched stdio MCP server for task suggestions (C-31.1).

Only the daemon root and parent supplied at launch are reachable. This server
never starts a daemon, edits a store, or exposes Start as a model tool.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any
import uuid

# An absolute script path works from every proposed cwd and from an editable
# install, without depending on the provider's PYTHONPATH or shell activation.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from subfleet.client import Client, DaemonError, DaemonUnavailable, ResponseLost

SERVER_NAME = "subfleet_chips"
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", PROTOCOL_VERSION)
MAX_LINE_BYTES = 262_144
TOOLS = [
    {
        "name": "spawn_task",
        "description": "Suggest a separate task as a chip the person can Start. Nothing runs until they start it. "
                       "Supply all context in prompt; the child gets that exact first message and no parent brief.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short imperative title, ideally under 60 characters."},
                "prompt": {"type": "string", "description": "Self-contained initial message for the new session."},
                "tldr": {"type": "string", "description": "One or two plain-English sentences describing the task."},
                "cwd": {"type": "string", "description": "Optional existing absolute directory; defaults to this project."},
            },
            "required": ["title", "prompt", "tldr"],
        },
    },
    {
        "name": "dismiss_task",
        "description": "Withdraw a pending task chip from this conversation. A started task cannot be withdrawn.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task_id returned by spawn_task."},
                "reason": {"type": "string", "description": "Optional reason for withdrawing the suggestion."},
            },
            "required": ["task_id"],
        },
    },
]


def mcp_config(host: dict[str, str]) -> dict:
    """Only daemon-provided binding goes into the launch configuration."""
    return {"mcpServers": {SERVER_NAME: {
        "type": "stdio", "command": sys.executable,
        "args": ["-I", str(Path(__file__).resolve()), "--root", host["root"],
                 "--conversation-id", host["conversation_id"], "--message-id", host["message_id"],
                 "--token", host["token"]],
    }}}


def error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class ChipHost:
    def __init__(self, client: Client, *, conversation_id: str, message_id: str, token: str):
        self.client = client
        self.binding = {"conversation_id": conversation_id, "message_id": message_id, "host_token": token}
        self.initialized = False

    def handle(self, row: Any) -> dict | None:
        if not isinstance(row, dict) or row.get("jsonrpc") != "2.0" or not isinstance(row.get("method"), str):
            return error(row.get("id") if isinstance(row, dict) else None, -32600, "Invalid Request")
        # JSON-RPC notifications never get responses, including cancellation.
        if "id" not in row:
            return None
        request_id = row["id"]
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            return error(None, -32600, "Invalid request id")
        params = row.get("params", {})
        if not isinstance(params, dict):
            return error(request_id, -32602, "params must be an object")
        method = row["method"]
        if method == "initialize":
            version = params.get("protocolVersion")
            self.initialized = True
            result = {"protocolVersion": version if version in SUPPORTED_VERSIONS else PROTOCOL_VERSION,
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": SERVER_NAME, "version": "1.0.0"},
                      "instructions": "Use spawn_task for useful separate follow-up work. The person decides when to start it."}
        elif method == "ping":
            result = {}
        elif not self.initialized:
            return error(request_id, -32000, "Initialize the server before using tools")
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments", {})
            tool = next((tool for tool in TOOLS if tool["name"] == name), None)
            if tool is None:
                return error(request_id, -32602, "Unknown tool")
            schema = tool["inputSchema"]
            if (not isinstance(arguments, dict)
                    or any(key not in arguments for key in schema["required"])
                    or any(key not in schema["properties"] or not isinstance(value, str)
                           for key, value in arguments.items())):
                return error(request_id, -32602, "Invalid tool arguments")
            result = self.call_tool(name, arguments)
        else:
            return error(request_id, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def call_tool(self, name: str, arguments: dict) -> dict:
        request_id = str(uuid.uuid4())
        if name == "spawn_task":
            op = "chip.spawn"
            args = {**arguments, **self.binding, "request_id": request_id}
        else:
            op = "chip.dismiss"
            args = {**self.binding, "chip_id": arguments["task_id"]}
            if "reason" in arguments:
                args["reason"] = arguments["reason"]
        response_lost = False
        try:
            try:
                result = self.client.call(op, args, request_id=request_id)
            except ResponseLost:
                response_lost = True
                # Both ops are idempotent. Reuse the identical request, including
                # its spawn key, if the commit's response was lost.
                result = self.client.call(op, args, request_id=request_id)
            chip = result["chip"]
            body = {"task_id": chip["chip_id"], "state": chip["state"], "title": chip["title"]}
            return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}], "isError": False}
        except (DaemonError, DaemonUnavailable, ResponseLost) as exc:
            # A lost response is explicitly uncertain; never imply it did not commit.
            detail = str(exc).replace(self.binding["host_token"], "[host token]")
            if response_lost:
                detail += "; the earlier request's outcome is unknown"
                detail += "; inspect the conversation's chips before proposing the same task again"
            return {"content": [{"type": "text", "text": detail}], "isError": True}


def serve(host: ChipHost, source, output) -> None:
    while raw := source.readline(MAX_LINE_BYTES + 1):
        if len(raw) > MAX_LINE_BYTES:
            response = error(None, -32600, "Request exceeds the size limit")
        else:
            try:
                response = host.handle(json.loads(raw))
            except (ValueError, UnicodeDecodeError):
                response = error(None, -32700, "Parse error")
        if response is not None:
            output.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            output.flush()
        if len(raw) > MAX_LINE_BYTES:
            return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    root = Path(args.root)
    if not root.is_absolute():
        parser.error("--root must be absolute")
    host = ChipHost(Client(root), conversation_id=args.conversation_id, message_id=args.message_id, token=args.token)
    serve(host, sys.stdin.buffer, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
