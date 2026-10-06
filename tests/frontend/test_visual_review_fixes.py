"""PR #128 regressions, using production models and offscreen native views."""
import json
import os
from pathlib import Path
import shutil

import pytest

from tests.frontend.conftest import needs_swift, run_probe
from tests.frontend.swift import ROOT, compile_probe

pytestmark = needs_swift
FIXTURES = ROOT / "tests/fixtures/visual/approvals.json"


@pytest.fixture(scope="session")
def review_models(tmp_path_factory):
    probe = compile_probe(tmp_path_factory.mktemp("review-models") / "probe",
                          ROOT / "tests/frontend/ReviewFixProbe.swift", "SUBFLEET_MODEL_TEST")
    return run_probe(probe, FIXTURES, ROOT / "tests/fixtures/visual/codex-commands.json")


@pytest.mark.parametrize("id,command", [("codex-command", "rm -rf build && git clean -fdx"),
    ("claude-bash", "echo [MASKED]"), ("flat-command", "git status")])
def test_loaded_approval_retains_exact_masked_command(review_models, id, command):
    assert next(a for a in review_models["approvals"] if a["id"] == id)["command"] == command


@pytest.mark.parametrize("state,reason,text", [("failed", "model-mismatch", "Failed: model-mismatch"),
    ("failed", "continued-elsewhere", "Failed: continued-elsewhere"), ("interrupted", "person-stopped", "Stopped"),
    ("cancelled", "withdrawn", "Withdrawn"), ("complete", "stop-too-late", "Completed before the stop took effect"),
    ("complete", "", "Completed")])
def test_settled_turn_keeps_its_outcome(review_models, state, reason, text):
    row = next(o for o in review_models["outcomes"] if (o["state"], o["reason"]) == (state, reason))
    assert row["visible"] and row["text"] == text


@pytest.mark.parametrize("index,label", [(0, "nl -ba subfleet/daemon.py"), (1, "git show 02c63203"),
    (2, "Search the web for SwiftUI keyboard navigation"), (3, "Search src for TODO"),
    (4, "Find files matching **/*.swift"), (5, "Fetch https://example.com/docs"),
    (6, "Edit 2 files (a.swift, b.swift)"), (7, "Run a command"), (8, "Write the notes")])
def test_tool_labels_keep_provider_and_input_detail(review_models, index, label):
    assert review_models["tools"][index] == label


def test_codex_commands_are_grouped_as_commands(review_models):
    assert review_models["command_group"] == "Ran 1 command"


def test_failed_work_group_never_uses_the_success_label(review_models):
    assert review_models["failed_groups"][0].startswith("Failed")


@pytest.fixture(scope="session")
def review_views(tmp_path_factory):
    ocr = shutil.which("tesseract")
    if ocr is None:
        pytest.skip("offscreen rendered-text validation requires tesseract")
    probe = compile_probe(tmp_path_factory.mktemp("review-views") / "probe",
                          ROOT / "tests/frontend/ReviewFixViewProbe.swift", "SUBFLEET_VIEW_TEST")
    render = Path(os.environ.get("SF_REVIEW_RENDER", tmp_path_factory.mktemp("review-render")))
    render.mkdir(parents=True, exist_ok=True)
    out = run_probe(probe, FIXTURES, render, timeout=180,
                    env={"SF_REVIEW_TESSERACT": ocr})
    if path := os.environ.get("SF_REVIEW_VIEW_RESULT"):
        Path(path).write_text(json.dumps(out, indent=2) + "\n")
    return out


@pytest.mark.parametrize("fixture", json.loads(FIXTURES.read_text()), ids=lambda f: f["id"])
def test_every_loaded_grant_field_is_visible_on_card_and_sheet(review_views, fixture):
    # Questions are answered inline; reviewOpensRequestSheet excludes them.
    for surface in (("card",) if fixture["kind"] == "question" else ("card", "sheet")):
        text = review_views["approvals"][fixture["id"]][surface]
        for value in fixture["visible"]:
            # Wrapped paths can acquire OCR whitespace after a slash.
            shown = value.replace(" ", "") in text.replace(" ", "") if value.startswith("/") else value in text
            assert shown, (surface, fixture["id"], value, text)
        assert "unmasked provider fallback" not in text


@pytest.mark.parametrize("state", ["complete", "running", "failed", "interrupted", "cancelled", "failover", "unblock-note"])
def test_every_turn_has_visible_serving_facts_and_fast_warning(review_views, state):
    text = review_views["turns"][state]
    for value in ("max@example.com", "gpt-6-astra", "high", "Fast was asked for", "standard speed"):
        assert value in text, (state, value, text)


@pytest.mark.parametrize("state,text", [("complete", "Completed"), ("failed", "Failed: model-mismatch"),
    ("interrupted", "Stopped"), ("cancelled", "Withdrawn"), ("stop-too-late", "Completed before the stop took effect")])
def test_real_settled_turn_shows_outcome_without_an_error_event(review_views, state, text):
    assert text in review_views["turns"][state]


def test_sidebar_uses_native_keyboard_selection_and_separate_badge(review_views):
    assert review_views["sidebar"]["native_selection"]
    assert review_views["sidebar"]["focus_shortcut_handled"]
    assert review_views["sidebar"]["focus_shortcut_reached_list"]
    assert review_views["sidebar"]["arrow_changed_selection"]
    assert set(review_views["sidebar"]["arrow_path"]) == {"cv:c", "cv:second", "cv:earlier"}
    assert review_views["sidebar"]["badge_clicked"], review_views["sidebar"]
    assert review_views["sidebar"]["badge_hit_is_control"]
    assert review_views["sidebar"]["badge_is_independent"]
    assert review_views["visible_windows"] == 0


def test_active_provider_filter_is_visible(review_views):
    assert "Codex" in review_views["filter"]


def test_disabled_quiet_send_looks_disabled(review_views):
    assert review_views["disabled_pixel_difference"] > 0.001


def test_permission_amber_has_text_contrast_in_both_modes(review_views):
    assert min(review_views["amber_contrast"]) >= 4.5
