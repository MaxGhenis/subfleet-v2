"""Explicit null request values are grants, including MCP property removals."""
import json
import shutil

import pytest

from tests.frontend.conftest import needs_swift, run_probe
from tests.frontend.swift import ROOT, compile_probe
from tests.schema_check import errors

REQUESTS = json.loads((ROOT / "tests/fixtures/visual/approval-null-requests.json").read_text())


@pytest.fixture(scope="session")
def null_models(tmp_path_factory):
    probe = compile_probe(tmp_path_factory.mktemp("null-models") / "probe",
                          ROOT / "tests/frontend/R4NullPresentationProbe.swift", "SUBFLEET_MODEL_TEST")
    return run_probe(probe, ROOT / "tests/fixtures/visual/approval-null-requests.json")


@needs_swift
@pytest.mark.parametrize("fixture", REQUESTS, ids=lambda row: row["id"])
@pytest.mark.parametrize("source", ["loaded", "summary"])
def test_explicit_null_grants_survive_loaded_and_summary_projection(null_models, fixture, source):
    expected = fixture["expected"]
    if source == "summary":
        expected = {key.removeprefix("params."): value for key, value in expected.items()}
        # Only input is supplied by Claude's display summary.
        if "input" in fixture["request"]:
            expected = {key: value for key, value in expected.items() if key.startswith("input.")}
        # The adapters also write absent fields as top-level None, so a summary
        # cannot show a top-level null; nested ones remain (round five, P2).
        top_level_nulls = {key for key, value in fixture["request"].get("params", {}).items() if value is None}
        expected = {key: value for key, value in expected.items() if key not in top_level_nulls}
    assert null_models[fixture["id"]][source] == expected


@pytest.mark.parametrize("fixture", [row for row in REQUESTS if row["id"].startswith("codex-")], ids=lambda row: row["id"])
def test_nullable_codex_requests_fit_the_provider_schema(fixture):
    schema = json.loads((ROOT / "tests/fixtures/codex/app-server-0.153.3/ServerRequest.json").read_text())
    assert not errors({"id": "null-grant", **fixture["request"]}, schema, schema)


def test_claude_mcp_request_and_allow_reply_preserve_removals():
    from tests.unit.test_claude_turn import ClaudeTurn, line, spec, started
    request = REQUESTS[0]["request"]
    turn = ClaudeTurn(spec(), read_bytes=lambda path: b"")
    started(turn)
    step = turn.feed(line(type="control_request", request_id="null-grant", request=request), 80)
    assert step.approvals[0].request == request
    reply = json.loads(turn.respond("null-grant", "allow").frames[0].line)
    assert reply["response"]["response"]["updatedInput"]["properties"] == {"Salary": None, "Owner": None}


@pytest.mark.parametrize("fixture", [row for row in REQUESTS if row["id"].startswith("codex-")], ids=lambda row: row["id"])
def test_codex_adapter_preserves_explicit_null_grants(fixture):
    from tests.unit.test_codex_turn import CodexTurn, spec, to_running
    turn = CodexTurn(spec())
    to_running(turn)
    step = turn.feed(json.dumps({"id": "null-grant", **fixture["request"]}), 20)
    assert step.approvals[0].request == fixture["request"]
    if fixture["kind"] == "permissions":
        reply = json.loads(turn.respond("null-grant", "allow-turn").frames[0].line)
        assert reply["result"]["permissions"] == fixture["request"]["params"]["permissions"]


@pytest.fixture(scope="session")
def null_views(tmp_path_factory):
    folder = tmp_path_factory.mktemp("null-views")
    probe = compile_probe(folder / "probe", ROOT / "tests/frontend/R2ApprovalViewProbe.swift", "SUBFLEET_VIEW_TEST")
    ocr = shutil.which("tesseract")
    assert ocr, "The null grant regression requires foreground OCR"
    path = folder / "requests.json"
    path.write_text(json.dumps({row["id"]: row for row in REQUESTS[:2]}))
    return run_probe(probe, path, folder / "renders", timeout=540,
                     env={"R2_TESSERACT": ocr, "R2_PARTS": "approvals"})


@needs_swift
@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("surface", ["card", "sheet"])
@pytest.mark.parametrize("scenario,names", [
    ("claude-null-property-delete", ["Salary", "Owner"]),
    ("claude-null-description", ["description", "labels"]),
])
def test_null_removals_are_visible_before_allowing(null_views, mode, surface, scenario, names):
    row = null_views["approvals"][scenario][mode]
    text = row[surface + "_ocr"]
    assert all(name in text for name in names), text
    assert "null" in text, text
    assert "Allow" in text and "Deny" in text
    assert row[surface + "_height"] <= 400
    assert null_views["visible_windows"] == 0
