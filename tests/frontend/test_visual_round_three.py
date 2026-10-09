"""Round-three review regressions: real requests and unshown native views."""
import json
import re
import shutil

import pytest

from tests.frontend.conftest import needs_swift, run_probe
from tests.frontend.swift import ROOT, compile_probe
from tests.schema_check import errors

pytestmark = needs_swift
REQUESTS = json.loads((ROOT / "tests/fixtures/visual/approval-schema-requests.json").read_text())


@pytest.mark.parametrize("fixture", REQUESTS, ids=lambda row: row["id"])
def test_real_shaped_requests_retain_every_grant_field(r2_models, fixture):
    assert r2_models["schema_requests"][fixture["id"]] == fixture["expected"]


@pytest.mark.parametrize("fixture", REQUESTS[:3], ids=lambda row: row["id"])
def test_codex_regressions_use_schema_valid_requests(fixture):
    schema = json.loads((ROOT / "tests/fixtures/codex/app-server-0.153.3/ServerRequest.json").read_text())
    # CodexTurn persists method/params and stores the provider id separately.
    # Reattach that routing id to validate the original ServerRequest envelope.
    assert not errors({"id": "provider-fixture", **fixture["request"]}, schema, schema)
    assert set(fixture["request"]["params"]) <= set(schema["definitions"][fixture["definition"]]["properties"])


def test_metadata_filter_only_applies_at_its_schema_container(r2_models):
    assert r2_models["nested_metadata"] == {
        "futureGrant.approvalId": "grant-callback", "futureGrant.commandActions[0].path": "/future/root",
        "input.kind": "future-kind", "params.futureGrant.startedAtMs": "grant-value",
    }


@pytest.mark.parametrize("command,label", [
    ("cd app; rm -rf build && make", "rm -rf build"),
    ("cd app || exit 1\nbun test && bun run build", "bun test"),
    ("(cd app && swift build)", "swift build"),
    ("set -euo pipefail\ncd app && swift build", "swift build"),
    ("cd 'a;b' && rm -rf build", "rm -rf build"),
    ("cd app || rm -rf build", "cd app"),
    ("set -- dangerous; rm -rf build", "set -- dangerous"),
    ("cd app\nrm -rf build && make", "rm -rf build"),
])
def test_shell_preludes_keep_the_first_substantive_command(r2_models, command, label):
    assert r2_models["edge_labels"][command] == label


@pytest.fixture(scope="session")
def r3_views(tmp_path_factory):
    ocr = shutil.which("tesseract")
    assert ocr, "The review regression needs foreground OCR"
    probe = compile_probe(tmp_path_factory.mktemp("r3-view") / "probe",
                          ROOT / "tests/frontend/R3FixViewProbe.swift", "SUBFLEET_VIEW_TEST")
    return run_probe(probe, ROOT / "tests/fixtures/visual/approval-layout.json",
                     tmp_path_factory.mktemp("r3-render"), ROOT / "tests/fixtures/visual/approval-schema-requests.json",
                     timeout=180, env={"R3_TESSERACT": ocr})


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("focus", ["focused", "unfocused"])
def test_pending_count_contrasts_with_its_rendered_background(r3_views, mode, focus):
    assert r3_views["sidebar"][f"{mode}-{focus}"]["badge_contrast"] >= 4.5
    assert r3_views["visible_windows"] == 0


@pytest.mark.parametrize("state", ["answered", "withdrawn"])
def test_history_details_load_without_a_false_pending_error(r3_views, state):
    row = r3_views["history"][state]
    assert row["problem"] == "An unrelated notice"
    assert "subtype" in row["ocr"] and "can_use_tool" in row["ocr"]
    # The action resolver must still refuse stale cards.
    assert r3_views["history"][state + "-action"] == {"id": "nil", "problem": "That approval is no longer pending."}


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("kind", ["write", "codex"])
def test_largest_supported_text_keeps_request_and_actions_visible(r3_views, mode, kind):
    row = r3_views["large"][f"{kind}-{mode}"]
    assert row["height"] <= 720
    assert row["width"] <= 900
    assert row["scroll_heights"] and max(row["scroll_heights"]) >= 100
    text = " ".join(row["ocr"].split())
    for label in ["Allow", "Deny", "Cancel", "reviewed the masked values"]:
        assert label in text
    if kind == "codex":
        assert "Allow for this session" in text and "Deny and stop" in text
    assert set(row["action_bottoms"]) == {"Allow", "Deny", "Cancel"}
    assert all(row["height"] - bottom >= 12 for bottom in row["action_bottoms"].values())
    assert r3_views["visible_windows"] == 0


def test_linked_report_is_complete_and_uses_committed_sources():
    path = ROOT / "docs/reports/2026-10-03-visual-pass/R2.md"
    report = path.read_text()
    for target in re.findall(r"\]\(([^)]+)\)", report):
        if not target.startswith("https://"):
            assert (path.parent / target.split("#")[0]).resolve().is_file(), target
    assert "underway" not in report and "bb762633" not in report
    assert "761a62f2" not in report and "d6110252" not in report
    assert "round three" in report.lower() and "passed" in report.lower()
