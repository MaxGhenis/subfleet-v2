"""Round-five review checks for explicit null grants (PR #128).

1. Differential property: for generated Claude/Codex requests, the loaded card's
   rows equal a reference projection of the stated rule: every leaf, including
   null and empty containers, under its exact path, except the named plumbing
   keys in their own container. Keys must be unique (the view uses them as ids).
2. Regression: the history summary built by the real adapters must not show
   adapter placeholders (absent fields spelled None) as `null` grant rows.

Round-five fix (the summary path drops top-level nulls only):

3. Property over generated requests fed through the real adapters: summary
   rows equal the reference rule (no top-level null, nested nulls kept), and
   every summary `null` is an explicit null in the raw request (no phantoms).
4. The Notion removal and Codex's nullable permission profiles keep their
   nested nulls in history.
5. Native history cards: a placeholder null adds no row and no height.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.swift import ROOT, compile_probe

PROBE = ROOT / "tests/frontend/R5PropertyProbe.swift"
HIDDEN_REQUEST = {"agent_id", "classifier_approvable", "subtype", "tool_use_id", "tool_name", "display_name", "title",
                  "description", "decision_reason", "decision_reason_type", "suppress_always_allow_rule",
                  "requires_user_interaction", "method"}
HIDDEN_PARAMS = {"threadId", "turnId", "itemId", "startedAtMs", "reason", "approvalId", "commandActions"}
HIDDEN_DISPLAY = {"tool", "title", "description", "reason"}
EMPTY_AMENDMENTS = {"permission_suggestions", "execpolicy_amendment", "network_amendments",
                    "proposedExecpolicyAmendment", "proposedNetworkPolicyAmendments"}


def child(path: str, key: str) -> str:
    if key == "" or any(c in key for c in ".[]"):
        return path + "[" + json.dumps(key, ensure_ascii=False) + "]"
    return key if path == "" else path + "." + key


def leaf(value) -> str:
    if value is None:
        return "null"
    if value == "":
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return "{}" if isinstance(value, dict) else "[]"
    return str(value)


def reference(kind: str, display_tool: str | None, request) -> list[tuple[str, str]]:
    tool = request.get("tool_name") if isinstance(request.get("tool_name"), str) else display_tool
    rows: list[tuple[str, str]] = []

    def walk(value, path):
        if isinstance(value, dict) and value:
            for key in sorted(value):
                hidden = (HIDDEN_REQUEST | HIDDEN_DISPLAY | HIDDEN_PARAMS if path == ""
                          else HIDDEN_PARAMS if path == "params"
                          else {"description"} if path == "input" and tool == "Bash" else set())
                if key in hidden:
                    continue
                if path in ("", "params") and key in EMPTY_AMENDMENTS and value[key] == []:
                    continue
                if kind == "question" and path in ("", "input") and key in ("questions", "answers"):
                    continue
                walk(value[key], child(path, key))
        elif isinstance(value, list) and value:
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
        else:
            rows.append((path, leaf(value)))

    walk(request, "")
    return rows


KEYS = st.sampled_from(["data_source_id", "properties", "Salary", "Owner", "a.b", "x[0]", "", "method", "description",
                        "approvalId", "commandActions", "reason", "threadId", "agent_id", "title", "kind", "cwd",
                        "grantRoot", "environmentId", "permissions", "fileSystem", "write", "network", "enabled",
                        "blocked_path", "permission_suggestions", "futureGrant", "content", "command"])
LEAVES = st.none() | st.booleans() | st.integers(-5, 5) | st.sampled_from(["", "/repo", "make", "null", "0"])
JSONS = st.recursive(LEAVES, lambda inner: st.lists(inner, max_size=3) | st.dictionaries(KEYS, inner, max_size=4),
                     max_leaves=12)


@st.composite
def scenes(draw):
    if draw(st.booleans()):
        tool = draw(st.sampled_from(["Bash", "Write", "mcp__notion__API-update-a-data-source", "AskUserQuestion"]))
        request = {"subtype": "can_use_tool", "tool_name": tool, "tool_use_id": "toolu-p",
                   "input": draw(st.dictionaries(KEYS, JSONS, max_size=5))}
        request.update(draw(st.dictionaries(KEYS, JSONS, max_size=4)))
        request["tool_name"] = tool
        return {"kind": "question" if tool == "AskUserQuestion" else "tool", "display": {"tool": tool},
                "request": request}
    method = draw(st.sampled_from(["item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                                   "item/permissions/requestApproval"]))
    params = {"threadId": "thr-1", "turnId": "turn-1", "itemId": "i", "startedAtMs": 1}
    params.update(draw(st.dictionaries(KEYS, JSONS, max_size=6)))
    kind = {"item/commandExecution/requestApproval": "command", "item/fileChange/requestApproval": "file-change",
            "item/permissions/requestApproval": "permissions"}[method]
    return {"kind": kind, "display": {}, "request": {"method": method, "params": params}}


@pytest.fixture(scope="session")
def property_probe(tmp_path_factory):
    return compile_probe(tmp_path_factory.mktemp("r5-property") / "probe", PROBE, "SUBFLEET_MODEL_TEST")


@needs_swift
@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture,
                                                               HealthCheck.too_slow, HealthCheck.data_too_large])
@given(batch=st.lists(scenes(), min_size=25, max_size=25))
def test_loaded_rows_match_the_reference_projection(property_probe, tmp_path, batch):
    path = tmp_path / "scenes.json"
    path.write_text(json.dumps(batch))
    output = run_probe(property_probe, path)
    for scene, result in zip(batch, output):
        actual = [tuple(row) for row in result["rows"]]
        keys = [key for key, _ in actual]
        assert len(keys) == len(set(keys)), (scene, actual)
        expected = reference(scene["kind"], scene["display"].get("tool"), scene["request"])
        assert sorted(actual) == sorted(expected), (scene, actual, expected)


@needs_swift
def test_history_summaries_from_real_adapters_show_no_placeholder_nulls(property_probe, tmp_path):
    from tests.unit.test_claude_turn import ClaudeTurn, line, spec as claude_spec, started
    from tests.unit.test_codex_turn import CodexTurn, spec as codex_spec, to_running

    claude = {
        "bash": {"subtype": "can_use_tool", "tool_name": "Bash", "tool_use_id": "t1",
                 "input": {"command": "make test", "description": "Run the tests"}},
        "question": {"subtype": "can_use_tool", "tool_name": "AskUserQuestion", "tool_use_id": "t2",
                     "input": {"questions": [{"question": "Which first?", "header": "Order", "multiSelect": False,
                                              "options": [{"label": "Notes", "description": "n"}]}]}},
    }
    codex = {
        "command": {"method": "item/commandExecution/requestApproval",
                    "params": {"kind": "command", "threadId": "thr-1", "turnId": "turn-1", "itemId": "i",
                               "startedAtMs": 1, "environmentId": None, "command": "make test", "cwd": "/repo"}},
        "file-change": {"method": "item/fileChange/requestApproval",
                        "params": {"threadId": "thr-1", "turnId": "turn-1", "itemId": "i", "startedAtMs": 1}},
    }
    batch, names = [], []
    for name, request in claude.items():
        turn = ClaudeTurn(claude_spec(), read_bytes=lambda p: b"")
        started(turn)
        step = turn.feed(line(type="control_request", request_id="r-" + name, request=request), 80)
        names.append(name)
        batch.append(step)
    for name, request in codex.items():
        turn = CodexTurn(codex_spec())
        to_running(turn)
        names.append(name)
        batch.append(turn.feed(json.dumps({"id": "r-" + name, **request}), 20))
    scenes_ = []
    for step in batch:
        event = json.loads(json.dumps(next(e for e in step.events if e.kind == "approval.requested").data))
        kind = event.pop("kind")
        event.pop("options"), event.pop("request_id")
        scenes_.append({"kind": kind, "display": event, "request": json.loads(json.dumps(step.approvals[0].request))})
    path = tmp_path / "scenes.json"
    path.write_text(json.dumps(scenes_))
    output = run_probe(property_probe, path)
    for name, scene, result in zip(names, scenes_, output):
        placeholders = [key for key, value in result["summary"] if value == "null" and "." not in key
                        and "[" not in key and scene["display"].get(key, 0) is None]
        assert not placeholders, (name, result["summary"])


# --- Round-five fix -----------------------------------------------------------

NOTION = json.loads((ROOT / "tests/fixtures/visual/approval-null-requests.json").read_text())[0]["request"]
CODEX_PARAMS = {"threadId": "thr-1", "turnId": "turn-1", "itemId": "item-1", "startedAtMs": 1791100000000}
# Real-shaped requests from the round-five review (Claude 2.1.284 omits an
# absent blocked_path; Codex 0.159 omits optional fields, but environmentId is
# a required nullable key).
HISTORY = {
    "claude-bash": {"subtype": "can_use_tool", "tool_name": "Bash", "display_name": "Bash", "tool_use_id": "toolu-bash",
                    "input": {"command": "make test", "description": "Run the tests"}, "description": "Run the tests"},
    "claude-question": {"subtype": "can_use_tool", "tool_name": "AskUserQuestion", "display_name": "AskUserQuestion",
                        "tool_use_id": "toolu-question",
                        "input": {"questions": [{"question": "Which release task should go first?", "header": "Order",
                                                 "multiSelect": False,
                                                 "options": [{"label": "Notes", "description": "Write notes"},
                                                             {"label": "Tag", "description": "Cut the tag"}]}]}},
    "claude-notion": NOTION,
    "codex-command": {"method": "item/commandExecution/requestApproval",
                      "params": {**CODEX_PARAMS, "kind": "command", "environmentId": None,
                                 "command": "/bin/zsh -lc 'make test'", "cwd": "/repo",
                                 "commandActions": [{"type": "unknown", "command": "make test"}]}},
    "codex-file-change": {"method": "item/fileChange/requestApproval", "params": dict(CODEX_PARAMS)},
    "codex-permissions": {"method": "item/permissions/requestApproval",
                          "params": {**CODEX_PARAMS, "environmentId": None, "cwd": "/repo", "reason": None,
                                     "permissions": {"network": {"enabled": None}, "fileSystem": None}}},
}
# codex_turn._server_request copies these params into the summary under its own names.
CODEX_SUMMARY_SOURCES = {"cwd": "cwd", "input_kind": "kind", "network": "networkApprovalContext",
                         "execpolicy_amendment": "proposedExecpolicyAmendment",
                         "network_amendments": "proposedNetworkPolicyAmendments", "grant_root": "grantRoot",
                         "permissions": "permissions"}


def adapter_scene(request: dict) -> dict:
    """One raw provider request fed through its real adapter: the `approval.requested`
    data as the store keeps it (Timeline removes request_id, kind and options)."""
    from tests.unit.test_claude_turn import ClaudeTurn, line, spec as claude_spec, started
    from tests.unit.test_codex_turn import CodexTurn, spec as codex_spec, to_running

    if "method" in request:
        turn = CodexTurn(codex_spec())
        to_running(turn)
        step = turn.feed(json.dumps({"id": "r-1", **request}), 20)
    else:
        turn = ClaudeTurn(claude_spec(), read_bytes=lambda path: b"")
        started(turn)
        step = turn.feed(line(type="control_request", request_id="r-1", request=request), 80)
    display = json.loads(json.dumps(next(e for e in step.events if e.kind == "approval.requested").data))
    kind, options = display.pop("kind"), display.pop("options")
    display.pop("request_id")
    return {"kind": kind, "display": display, "options": options,
            "request": json.loads(json.dumps(step.approvals[0].request))}


def summary_reference(scene: dict) -> list[tuple[str, str]]:
    """The summary rule: the display fields without top-level nulls, a JSON-object
    input string decoded (a question's other string input is dropped), then the
    same projection as a loaded request; nothing left means no rows."""
    fields = {key: value for key, value in scene["display"].items() if value is not None}
    if isinstance(fields.get("input"), str):
        try:
            decoded = json.loads(fields["input"])
        except ValueError:
            decoded = None
        if isinstance(decoded, dict):
            fields["input"] = decoded
        elif scene["kind"] == "question":
            del fields["input"]
    if not fields:
        return []                   # placeholders alone are not an empty `{}` value
    tool = scene["display"].get("tool")
    return reference(scene["kind"], tool if isinstance(tool, str) else None, fields)


def request_nulls(scene: dict) -> set[str]:
    """Every null row the raw request itself supports, under the summary's names.
    Only keys present in the request are copied, so an absent field has none."""
    request = scene["request"]
    if "params" in request:
        params = request["params"]
        copied = {key: params[name] for key, name in CODEX_SUMMARY_SOURCES.items() if name in params}
    else:
        copied = {key: request[key] for key in ("blocked_path", "input") if key in request}
    return {key for key, value in reference(scene["kind"], None, copied) if value == "null"}


# Without the string "null", a rendered `null` can only be a JSON null (a Bash
# command spelled null is summarised as that text, and renders the same).
ADAPTER_JSONS = st.recursive(st.none() | st.booleans() | st.integers(-5, 5) | st.sampled_from(["", "/repo", "make", "0"]),
                             lambda inner: st.lists(inner, max_size=3) | st.dictionaries(KEYS, inner, max_size=4),
                             max_leaves=12)


@st.composite
def raw_requests(draw):
    if draw(st.booleans()):
        tool = draw(st.sampled_from(["Bash", "Write", "mcp__notion__API-update-a-data-source", "AskUserQuestion"]))
        request = draw(st.dictionaries(st.sampled_from(["blocked_path", "title", "display_name", "description",
                                                        "decision_reason", "agent_id", "futureGrant"]),
                                       ADAPTER_JSONS, max_size=3))
        tool_input = draw(st.dictionaries(KEYS, ADAPTER_JSONS, max_size=5))
        if tool == "AskUserQuestion" and draw(st.booleans()):
            tool_input["questions"] = [{"question": "Which first?", "options": [{"label": "Notes"}]}]
        # Claude always sends an input object (an absent one is not a can_use_tool request).
        request.update(subtype="can_use_tool", tool_name=tool, tool_use_id="toolu-p", input=tool_input)
        return request
    method = draw(st.sampled_from(["item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                                   "item/permissions/requestApproval"]))
    params = draw(st.dictionaries(st.sampled_from(sorted(set(CODEX_SUMMARY_SOURCES.values()) | {
        "command", "reason", "environmentId", "approvalId", "commandActions", "futureGrant"})), ADAPTER_JSONS, max_size=6))
    params.update(CODEX_PARAMS)     # the adapter only answers its own thread
    return {"method": method, "params": params}


@needs_swift
@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture,
                                                               HealthCheck.too_slow, HealthCheck.data_too_large])
@given(batch=st.lists(raw_requests(), min_size=25, max_size=25))
def test_adapter_summaries_match_the_rule_and_never_invent_a_null(property_probe, tmp_path, batch):
    scenes_ = [adapter_scene(request) for request in batch]
    output = run_probe(property_probe, write_json(tmp_path / "scenes.json", scenes_))
    for scene, result in zip(scenes_, output):
        summary = [tuple(row) for row in result["summary"]]
        assert sorted(summary) == sorted(summary_reference(scene)), (scene, summary)
        assert all(key for key, _ in summary), (scene, summary)     # never the bare root
        invented = {key for key, value in summary if value == "null"} - request_nulls(scene)
        assert not invented, (scene, summary)
        # Loading the request still shows every null it holds, top-level ones included.
        tool = scene["display"].get("tool")
        loaded = sorted(tuple(row) for row in result["rows"])
        assert loaded == sorted(reference(scene["kind"], tool if isinstance(tool, str) else None, scene["request"]))


@needs_swift
def test_history_summaries_keep_nested_null_grants(property_probe, tmp_path):
    names = ["claude-notion", "codex-permissions", "codex-command", "claude-question", "codex-file-change"]
    scenes_ = [adapter_scene(HISTORY[name]) for name in names]
    output = run_probe(property_probe, write_json(tmp_path / "scenes.json", scenes_))
    summary = {name: dict(map(tuple, result["summary"])) for name, result in zip(names, output)}
    assert summary["claude-notion"] == {"input.data_source_id": "f336d0bc-b841-465b-8045-024475c079dd",
                                        "input.properties.Owner": "null", "input.properties.Salary": "null"}
    assert summary["codex-permissions"] == {"cwd": "/repo", "permissions.fileSystem": "null",
                                            "permissions.network.enabled": "null"}
    assert summary["codex-command"] == {"command": "/bin/zsh -lc 'make test'", "cwd": "/repo", "input_kind": "command"}
    assert summary["claude-question"] == summary["codex-file-change"] == {}


@needs_swift
def test_placeholder_only_summary_has_no_empty_root_row(property_probe, tmp_path):
    scene = {"kind": "permissions", "display": {"permissions": None, "futureGrant": None},
             "request": {"futureGrant": None}}
    result, = run_probe(property_probe, write_json(tmp_path / "scenes.json", [scene]))
    assert result["summary"] == []
    assert result["rows"] == [["futureGrant", "null"]]


@pytest.fixture(scope="session")
def history_views(tmp_path_factory):
    ocr = shutil.which("tesseract")
    if ocr is None:
        pytest.skip("rendered-text validation requires tesseract")
    folder = tmp_path_factory.mktemp("r5-history")
    probe = compile_probe(folder / "probe", ROOT / "tests/frontend/R2ApprovalViewProbe.swift", "SUBFLEET_VIEW_TEST")
    scenes_ = {name: adapter_scene(HISTORY[name]) for name in ("claude-question", "claude-notion", "codex-command")}
    # Controls: the same cards with the placeholders left out, as base rendered them.
    for name in ("claude-question", "codex-command"):
        control = json.loads(json.dumps(scenes_[name]))
        control["display"] = {key: value for key, value in control["display"].items() if value is not None}
        scenes_[name + "-control"] = control
    result = run_probe(probe, write_json(folder / "scenes.json", scenes_), folder / "renders", timeout=540,
                       env={"R2_TESSERACT": ocr, "R2_PARTS": "approvals"})
    if path := os.environ.get("SF_R5_RESULT"):
        Path(path).write_text(json.dumps(result, indent=2) + "\n")
    return result


@needs_swift
@pytest.mark.parametrize("name", ["claude-question", "codex-command"])
def test_history_cards_gain_no_row_or_height_from_placeholders(history_views, name):
    card, control = history_views["approvals"][name], history_views["approvals"][name + "-control"]
    assert card["fields_before_load"] == control["fields_before_load"]
    assert not [row for row in card["fields_before_load"] if row.endswith(": null")]
    assert card["answered_card_height"] == control["answered_card_height"]
    assert card["answered_card_height"] == {"claude-question": 132, "codex-command": 168}[name]
    assert history_views["visible_windows"] == 0


@needs_swift
def test_answered_question_history_card_shows_no_placeholder(history_views):
    text = " ".join(history_views["approvals"]["claude-question"]["answered_card_ocr"].split())
    assert "Which release task should go first?" in text and "Answer: Notes" in text
    assert "blocked_path" not in text and "null" not in text


@needs_swift
def test_notion_removals_show_on_card_sheet_and_history(history_views):
    notion = history_views["approvals"]["claude-notion"]
    for mode in ("light", "dark"):
        for surface in ("card", "sheet"):
            text = notion[mode][surface + "_ocr"]
            assert "Salary" in text and "Owner" in text and "null" in text, (mode, surface, text)
    assert "input.properties.Owner: null" in notion["fields_before_load"]
    assert "input.properties.Salary: null" in notion["fields_before_load"]
    assert not [row for row in notion["fields_before_load"] if row.startswith("blocked_path")]
