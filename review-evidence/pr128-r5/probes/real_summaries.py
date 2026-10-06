"""Build approval scenes from the real Claude and Codex turn drivers.

Each raw request is fed through the production adapter (ClaudeTurn/CodexTurn).
The scene's `display` is the `approval.requested` event data with the three
fields Timeline.swift removes (request_id, kind, options), JSON round-tripped
as the store and protocol do. `request` is the adapter's retained request.
"""
import json
import sys

from tests.unit.test_claude_turn import ClaudeTurn, line, spec as claude_spec, started
from tests.unit.test_codex_turn import CodexTurn, spec as codex_spec, to_running

NOTION = json.loads(open("tests/fixtures/visual/approval-null-requests.json").read())[0]["request"]

CLAUDE = {
    # Claude 2.1.284 builds can_use_tool with undefined (omitted) blocked_path and
    # agent_id unless set; see the binary excerpt in output/claude-can-use-tool.txt.
    "claude-bash": {"subtype": "can_use_tool", "tool_name": "Bash", "display_name": "Bash",
                    "input": {"command": "make test", "description": "Run the tests"},
                    "description": "Run the tests", "permission_suggestions": [
                        {"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "make test"}],
                         "behavior": "allow", "destination": "localSettings"}],
                    "decision_reason_type": "rule", "tool_use_id": "toolu-bash"},
    "claude-question": {"subtype": "can_use_tool", "tool_name": "AskUserQuestion", "display_name": "AskUserQuestion",
                        "input": {"questions": [{"question": "Which release task should go first?", "header": "Order",
                                                 "multiSelect": False,
                                                 "options": [{"label": "Notes", "description": "Write notes"},
                                                             {"label": "Tag", "description": "Cut the tag"}]}]},
                        "tool_use_id": "toolu-question"},
    "claude-notion": NOTION,
}

CODEX = {
    # Codex 0.159 (generated TS): environmentId is a required nullable key; the
    # other optional fields are omitted when absent (`?:`).
    "codex-command": {"method": "item/commandExecution/requestApproval",
                      "params": {"kind": "command", "threadId": "thr-1", "turnId": "turn-1", "itemId": "item-1",
                                 "startedAtMs": 1791100000000, "environmentId": None,
                                 "command": "/bin/zsh -lc 'make test'", "cwd": "/repo",
                                 "commandActions": [{"type": "unknown", "command": "make test"}]}},
    "codex-file-change": {"method": "item/fileChange/requestApproval",
                          "params": {"threadId": "thr-1", "turnId": "turn-1", "itemId": "item-2",
                                     "startedAtMs": 1791100000000}},
    "codex-permissions": {"method": "item/permissions/requestApproval",
                          "params": {"threadId": "thr-1", "turnId": "turn-1", "itemId": "item-3",
                                     "environmentId": None, "startedAtMs": 1791100000000, "cwd": "/repo",
                                     "reason": None,
                                     "permissions": {"network": {"enabled": True}, "fileSystem": None}}},
}


def scene(approval, event):
    data = json.loads(json.dumps(event.data))
    kind = data.pop("kind")
    options = data.pop("options")
    data.pop("request_id")
    return {"kind": kind, "display": data, "options": options,
            "request": json.loads(json.dumps(approval.request))}


def main(out):
    scenes = {}
    for name, request in CLAUDE.items():
        turn = ClaudeTurn(claude_spec(), read_bytes=lambda path: b"")
        started(turn)
        step = turn.feed(line(type="control_request", request_id="req-" + name, request=request), 80)
        event = next(e for e in step.events if e.kind == "approval.requested")
        scenes[name] = scene(step.approvals[0], event)
    for name, request in CODEX.items():
        turn = CodexTurn(codex_spec())
        to_running(turn)
        step = turn.feed(json.dumps({"id": "req-" + name, **request}), 20)
        event = next(e for e in step.events if e.kind == "approval.requested")
        scenes[name] = scene(step.approvals[0], event)
    with open(out, "w") as handle:
        json.dump(scenes, handle, indent=2, sort_keys=True)
    for name, row in scenes.items():
        print(name, "display nulls:", sorted(k for k, v in row["display"].items() if v is None))


if __name__ == "__main__":
    main(sys.argv[1])
