"""Behavioral display projection and contrast checks using the production Swift code."""
import json

import pytest

from tests.frontend.conftest import needs_swift, run_probe
from tests.frontend.swift import ROOT, compile_probe

pytestmark = needs_swift


@pytest.fixture(scope="session")
def visual_probe(tmp_path_factory):
    return compile_probe(tmp_path_factory.mktemp("visual") / "probe", ROOT / "tests/frontend/VisualProbe.swift",
                         "SUBFLEET_MODEL_TEST")


def test_every_text_token_meets_contrast_on_every_surface(visual_probe):
    values = run_probe(visual_probe, "contrast")
    assert len(values) == 40
    for value in values:
        assert value["contrast"] >= value["target"], value


def test_recorded_stream_groups_live_tools_between_prose_and_hides_empty_thinking(visual_probe):
    fixture = ROOT / "tests/fixtures/visual/progress.json"
    events = json.loads(fixture.read_text())
    assert sum(e["kind"] == "tool.started" for e in events) == 15
    thinking = [e for e in events if e["kind"] == "thinking"]
    assert len(thinking) == 6 and sum(not e["data"]["text"] for e in thinking) == 4
    out = run_probe(visual_probe, fixture)
    live = out["live"]
    assert len(live["text"]) == 3
    assert [g["count"] for g in live["groups"]] == [5, 5, 5]
    assert [g["failed"] for g in live["groups"]] == [1, 1, 0]
    assert sum(g["items"] for g in live["groups"]) == 17
    assert live["groups"][-1]["running"] == "Render the final snapshots"
    assert all("git show" not in label for label in live["tools"])
    assert out["read"] == "Read UIWindow.swift"
    assert out["hidden"] == "Credential access (hidden)"
    assert out["edit"] == "Edited 1 file"


def test_finished_history_collapses_once_and_keeps_all_prose(visual_probe):
    out = run_probe(visual_probe, ROOT / "tests/fixtures/visual/progress.json")
    assert out["finished"]["text"] == out["live"]["text"]
    assert len(out["finished"]["groups"]) == 1
    group = out["finished"]["groups"][0]
    assert group["label"] == "Worked for 3m 12s · 15 steps"
    assert group["count"] == 15 and group["failed"] == 2 and group["items"] == 17


def test_account_usage_matches_serving_lane_and_hides_stale_or_unmatched_usage(visual_probe, tmp_path):
    data = {"generated_at": "2026-10-04T10:00:00Z",
            "codex": {"homes": [], "fleet": {"total_homes": 0, "dispatchable_now": 0}},
            "claude": {"accounts": [{"lane_id": "claude-2", "email": "max@example.com", "active": False,
                        "enrolled": True, "probe": {"five_hour": {"used_percent": 37, "status": "provider"},
                                                    "seven_day": {"used_percent": 62, "status": "provider"}}}]}}
    path = tmp_path / "status.json"
    path.write_text(json.dumps(data))
    out = run_probe(visual_probe, "account", path)
    assert out["fresh"] == "max@example.com · 5h 37% · week 62%"
    assert out["stale"] == "max@example.com · 5h — · week —"
    assert out["other"] == "other · 5h — · week —"
    assert out["mismatch"] == "max@example.com · 5h — · week —"


def test_review_presentation_keeps_masked_command_and_serving_facts_in_their_place(visual_probe):
    out = run_probe(visual_probe, "review")
    assert out["headline"] == "Run the frontend tests in this checkout"
    assert out["command"] == "echo [MASKED]"
    assert out["fallback"] == "provider command"
    assert out["noCommand"] is None, "Do not replace a loaded request with provider input"
    assert not any(out["acknowledgments"][s] for s in
                   ("waiting", "starting", "running", "approval-needed"))
    assert out["acknowledgments"]["complete"]
    assert all(out["acknowledgments"][s] for s in ("queued", "steering", "steered", "delivery-unknown", "unknown", "sending"))
    assert "max@example.com" in out["tooltip"] and "claude-opus-5-5" in out["tooltip"]
    assert out["home"] == "~/project" and out["outside"] == "/Users/examples/project"
