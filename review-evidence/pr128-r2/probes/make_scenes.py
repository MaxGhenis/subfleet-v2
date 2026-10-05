"""Build approval scenes for the round-two review probe.

Each scene pairs the display summary the daemon emits in `approval.requested`
(codex_turn.py:674-688, claude_turn.py:547-563) with the stored request that
`approval.get` returns. `grant` lists the values that change what is granted,
chosen independently of the PR's own fixture lists.
"""
import json
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])
OUT = Path(sys.argv[2])


def codex(kind, method, params, options):
    if method.endswith("commandExecution/requestApproval"):
        display = {"command": params.get("command") or "", "cwd": params.get("cwd"), "reason": params.get("reason"),
                   "input_kind": params.get("kind"), "network": params.get("networkApprovalContext"),
                   "execpolicy_amendment": params.get("proposedExecpolicyAmendment"),
                   "network_amendments": params.get("proposedNetworkPolicyAmendments")}
    elif method.endswith("fileChange/requestApproval"):
        display = {"reason": params.get("reason"), "grant_root": params.get("grantRoot")}
    else:
        display = {"permissions": params.get("permissions"), "reason": params.get("reason"), "cwd": params.get("cwd")}
    return {"kind": kind, "display": display, "request": {"method": method, "params": params}, "options": options}


def claude(request, kind="tool", options=("allow", "deny", "cancel-turn")):
    name = request.get("tool_name")
    inp = request.get("input") or {}
    summary_input = "\n".join(
        (v if k == "command" else f"{k}: {v}") for k in ("command", "file_path", "path", "pattern", "url", "query", "description")
        if isinstance((v := inp.get(k)), str) and v)
    display = {"tool": name, "title": request.get("title") or request.get("display_name"),
               "description": request.get("description"), "input": summary_input or json.dumps(inp, sort_keys=True),
               "reason": request.get("decision_reason"), "blocked_path": request.get("blocked_path")}
    if kind == "question":
        display["questions"] = inp.get("questions")
    return {"kind": kind, "display": display, "request": request, "options": list(options)}


base = {"threadId": "thr_01a1", "turnId": "turn_01a1", "itemId": "call_01a1", "startedAtMs": 1791100000000}
scenes = {}
# Shapes the round-one review rendered (review-evidence/pr128/probes/ReviewScenes.swift), per the 0.153.3 schema.
scenes["r1-codex-command"] = codex("command", "item/commandExecution/requestApproval", {**base,
    "command": "rm -rf build && git clean -fdx", "cwd": "/Users/example/subfleet", "reason": "Clean the build outputs",
    "proposedExecpolicyAmendment": ["rm", "-rf"],
    "networkApprovalContext": {"host": "pypi.org", "protocol": "https"},
    "proposedNetworkPolicyAmendments": [{"host": "pypi.org", "action": "allow"}]},
    ["allow", "allow-session", "deny", "cancel-turn"])
scenes["r1-codex-command"]["grant"] = ["rm -rf build && git clean -fdx", "/Users/example/subfleet", "pypi.org", "https",
                                       "allow", "-rf"]
scenes["r1-codex-file-change"] = codex("file-change", "item/fileChange/requestApproval", {**base,
    "reason": "Write the release notes", "grantRoot": "/"}, ["allow", "allow-session", "deny", "cancel-turn"])
scenes["r1-codex-file-change"]["grant"] = ["grantRoot: /"]
scenes["r1-codex-permissions"] = codex("permissions", "item/permissions/requestApproval", {**base,
    "cwd": "/Users/example/subfleet", "reason": "Fetch the docs",
    "permissions": {"network": {"enabled": True}, "fileSystem": {"write": ["/Users/example"], "read": ["/etc"]}}},
    ["allow-turn", "deny"])
scenes["r1-codex-permissions"]["grant"] = ["enabled: true", "/Users/example", "/etc", "/Users/example/subfleet"]
scenes["r1-claude-write"] = claude({"subtype": "can_use_tool", "tool_name": "Write", "display_name": "Write",
    "tool_use_id": "toolu_01", "blocked_path": "/etc/hosts",
    "input": {"file_path": "/etc/hosts", "content": "0.0.0.0 example.com"}})
scenes["r1-claude-write"]["grant"] = ["/etc/hosts", "0.0.0.0 example.com"]
# The field set a real stored Claude Bash request carries (~/.subfleet approvals, read-only copy; values synthetic).
long_command = ("cd /Users/example/project && " + " && ".join(f"python -m tool step{i} --input data/part{i}.csv --out out/part{i}.json"
                                                              for i in range(24)))
scenes["real-claude-bash"] = claude({"agent_id": "agent_0123456789", "classifier_approvable": True,
    "decision_reason": "Bash command contains a cd followed by a write; requires approval under the ask policy.",
    "decision_reason_type": "rule_based", "description": "Rebuild every partition", "display_name": "Bash",
    "input": {"command": long_command, "description": "Rebuild every partition"}, "permission_suggestions": [],
    "subtype": "can_use_tool", "suppress_always_allow_rule": False, "tool_name": "Bash", "tool_use_id": "toolu_01ABCDEF"})
scenes["real-claude-bash"]["grant"] = ["step23 --input data/part23.csv"]
# A Write of an ordinary 120-line file: is the card bounded, and can the sheet's buttons be reached?
content = "\n".join(f"line {i:03d}: export const value{i} = compute({i}, 'payload');" for i in range(120))
scenes["claude-write-120-lines"] = claude({"subtype": "can_use_tool", "tool_name": "Write", "display_name": "Write",
    "tool_use_id": "toolu_02", "input": {"file_path": "/Users/example/project/src/values.ts", "content": content}})
scenes["claude-write-120-lines"]["grant"] = ["/Users/example/project/src/values.ts", "line 119"]
# A real-shaped AskUserQuestion: two questions, three options each, long descriptions.
qs = [{"header": "Scope", "multiSelect": False, "question": "Which part of the release should I prepare first? " * 3,
       "options": [{"label": f"Option {j}", "description": f"Description {j}: " + "covers a longer explanation of the trade-off. " * 3}
                   for j in range(3)]},
      {"header": "Timing", "multiSelect": True, "question": "When should the release notes go out?",
       "options": [{"label": f"Window {j}", "description": f"Window description {j}"} for j in range(3)]}]
scenes["real-claude-question"] = claude({"display_name": "AskUserQuestion", "input": {"questions": qs},
    "requires_user_interaction": True, "subtype": "can_use_tool", "tool_name": "AskUserQuestion",
    "tool_use_id": "toolu_03"}, kind="question", options=("answer", "deny", "cancel-turn"))
scenes["real-claude-question"]["grant"] = ["Which part of the release", "Option 2"]

# Every fixture the PR added, with the display the daemon would emit for that request.
for fixture in json.loads((ROOT / "tests/fixtures/visual/approvals.json").read_text()):
    request = fixture["request"]
    if "method" in request:
        options = ["allow-turn", "deny"] if fixture["kind"] == "permissions" else ["allow", "allow-session", "deny", "cancel-turn"]
        scene = codex(fixture["kind"], request["method"], request["params"], options)
    elif "subtype" in request:
        scene = claude(request, kind=fixture["kind"],
                       options=("answer", "deny", "cancel-turn") if fixture["kind"] == "question" else ("allow", "deny", "cancel-turn"))
    else:
        scene = {"kind": fixture["kind"], "display": {k: v for k, v in request.items()}, "request": request,
                 "options": ["allow", "allow-session", "deny", "cancel-turn"]}
    scene["display"]["description"] = fixture["headline"]
    scene["grant"] = fixture["visible"]
    scenes["fixture-" + fixture["id"]] = scene

OUT.write_text(json.dumps(scenes, indent=1))
print(len(scenes), "scenes")
