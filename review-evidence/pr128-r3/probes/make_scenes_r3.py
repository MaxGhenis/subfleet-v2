"""Round-three review scenes for #128 (review job only, not for the PR branch).

Field sets follow what was read this session:
- Codex 0.159 `codex app-server generate-json-schema`: CommandExecutionRequestApprovalParams
  (threadId, turnId, itemId, startedAtMs, approvalId, reason, command, cwd, commandActions,
  kind = command | writeStdin, environmentId, networkApprovalContext, proposed amendments),
  PermissionsRequestApprovalParams.
- Claude CLI 2.1.284's can_use_tool request object (tool_name, display_name, input, description,
  permission_suggestions, blocked_path, decision_reason, decision_reason_type, decision_reason_code,
  classifier_approvable, tool_use_id, agent_id, requires_user_interaction).
- The daemon summaries: codex_turn.py:674-688 and claude_turn.py:547-563.
Values (paths, text, reasons) are constructed and labelled as such; only the key sets are real.

usage: make_scenes_r3.py OUT.json BUILDER_LAYOUT.json R2_SCENES.json
"""
import json
import sys

out, layout_path, r2_path = sys.argv[1:4]
scenes = {}

# The builder's own five scenes, unchanged, for a like-for-like re-measure.
for key, scene in json.load(open(layout_path)).items():
    scenes["builder-" + key] = scene
# Round two's daemon-shaped scenes.
r2 = json.load(open(r2_path))
for key in ("real-claude-bash", "real-claude-question", "r1-codex-permissions"):
    scenes["r2-" + key] = r2[key]

CONTENT = "".join(f"line {i:03d}: export const value{i} = compute({i}, 'payload');\n" for i in range(120))
LONG_SCRIPT = ("cd /Users/example/subfleet && PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'\n" +
               "".join(f"print('step {i}: checking partition {i} of the release inputs')\n" for i in range(40)) + "PY")
LONG_COMMAND = "/bin/zsh -lc " + "'" + LONG_SCRIPT.replace("'", "'\\''") + "'"


def codex_command(command, kind="command", approval_id=None, reason=None, actions=None):
    params = {"threadId": "thr_01a1", "turnId": "turn_01a1", "itemId": "call_01a1", "startedAtMs": 1791100000000,
              "approvalId": approval_id, "reason": reason, "command": command, "cwd": "/Users/example/subfleet",
              "commandActions": actions if actions is not None else [{"type": "unknown", "command": command}],
              "kind": kind, "environmentId": None, "networkApprovalContext": None,
              "proposedExecpolicyAmendment": None, "proposedNetworkPolicyAmendments": None}
    display = {"command": command, "cwd": params["cwd"], "reason": reason, "input_kind": kind, "network": None,
               "execpolicy_amendment": None, "network_amendments": None}
    return {"kind": "command", "display": display, "options": ["allow", "allow-session", "deny", "cancel-turn"],
            "request": {"method": "item/commandExecution/requestApproval", "params": params}}


def claude_tool(name, tool_input, description, summary_input):
    request = {"subtype": "can_use_tool", "tool_name": name, "display_name": name, "input": tool_input,
               "description": description, "permission_suggestions": [], "blocked_path": None,
               "decision_reason": "constructed: asks every time", "decision_reason_type": "mode",
               "decision_reason_code": "constructed-code", "classifier_approvable": False,
               "tool_use_id": "toolu_r3", "agent_id": None}
    display = {"tool": name, "title": name, "description": description, "input": summary_input,
               "reason": request["decision_reason"], "blocked_path": None}
    return {"kind": "tool", "display": display, "options": ["allow", "deny", "cancel-turn"], "request": request}


# P1.1: the four sizes the brief names, with real key sets.
scenes["r3-claude-write-120"] = claude_tool(
    "Write", {"file_path": "/Users/example/project/src/values.ts", "content": CONTENT},
    "Writing values.ts", "file_path: /Users/example/project/src/values.ts")
scenes["r3-codex-long-command"] = codex_command(LONG_COMMAND, reason="Check the release inputs before packaging")
masked = codex_command("/bin/zsh -lc 'curl -H \"Authorization: Bearer [MASKED]\" https://api.example.com/v1/items'",
                       reason="Fetch the item list")
masked["masked"] = [{"path": "params.command", "rule": "bearer", "length": 40, "sha256": "constructed"}]
scenes["r3-codex-masked"] = masked
scenes["r3-claude-short-bash"] = claude_tool(
    "Bash", {"command": "git status --short", "description": "Show working tree status"},
    "Show working tree status", "git status --short\ndescription: Show working tree status")
masked_write = claude_tool("Write", {"file_path": "/Users/example/project/.env", "content": CONTENT},
                           "Writing .env", "file_path: /Users/example/project/.env")
masked_write["masked"] = [{"path": "input.content", "rule": "token", "length": 30, "sha256": "constructed"}]
scenes["r3-claude-masked-write-120"] = masked_write

# P2.2: kind = writeStdin is "input sent to an existing terminal" (Codex 0.159 schema).
scenes["r3-codex-write-stdin"] = codex_command("/bin/zsh -lc 'python3 manage.py migrate'", kind="writeStdin",
                                               approval_id="7b0c6a52-0000-4000-8000-000000000001")
# The same, before approval.get has loaded (history and first paint read the daemon summary).
# Ordering: a chained command whose parsed actions pass ten entries.
chain = " && ".join(f"sed -n '{i}0,{i}9p' part{i}.txt" for i in range(12))
scenes["r3-codex-12-actions"] = codex_command(
    "/bin/zsh -lc " + "\"" + chain + "\"",
    actions=[{"type": "read", "command": f"sed -n '{i}0,{i}9p' part{i}.txt", "name": f"part{i}.txt",
              "path": f"/Users/example/subfleet/part{i}.txt"} for i in range(12)])
perm = {"threadId": "thr_01a1", "turnId": "turn_01a1", "itemId": "call_01a1", "startedAtMs": 1791100000000,
        "cwd": "/Users/example/subfleet", "reason": "Write the release outputs", "environmentId": None,
        "permissions": {"fileSystem": {"write": [f"/Users/example/out/{i:02d}" for i in range(12)]}}}
scenes["r3-codex-permissions-12"] = {
    "kind": "permissions", "options": ["allow-turn", "deny"],
    "display": {"permissions": perm["permissions"], "reason": perm["reason"], "cwd": perm["cwd"]},
    "request": {"method": "item/permissions/requestApproval", "params": perm}}
# Long headline: a Codex reason the provider wrote at length.
long_reason = codex_command("/bin/zsh -lc 'git push origin HEAD'", reason=(
    "Push the release branch so the hub can open the pull request; this needs network access to github.com "
    "and write access to the remote, which the sandbox does not grant by default, so I am asking first. ") * 2)
long_reason["masked"] = [{"path": "params.command", "rule": "token", "length": 12, "sha256": "constructed"}]
scenes["r3-codex-long-reason-masked"] = long_reason

json.dump(scenes, open(out, "w"), indent=1)
print(len(scenes), "scenes")
