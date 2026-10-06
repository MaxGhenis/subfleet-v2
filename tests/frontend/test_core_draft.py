"""The inline draft survives navigation and persists until its first send is journaled."""
from pathlib import Path

import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.swift import ROOT, compile_probe

pytestmark = needs_swift


@pytest.fixture(scope="session")
def draft_probe(tmp_path_factory):
    return compile_probe(tmp_path_factory.mktemp("subfleet-draft") / "probe",
                         ROOT / "tests/frontend/DraftProbe.swift", "SUBFLEET_MODEL_TEST")


def draft(draft_probe, tmp_path: Path, steps, remembered_workspace=None):
    return run_probe(draft_probe, write_json(tmp_path / "draft.json", {
        "remembered_workspace": remembered_workspace, "steps": steps,
    }))


def settings(**overrides):
    return {"model": "opus", "permission": "ask", **overrides}


def model(provider="claude", value="opus", efforts=None):
    return {"short": value, "id": value, "provider": provider, "value": value, "values": [value],
            "efforts": efforts, "fast": {"supported": False, "billing": "usage credits"}}


def test_new_and_switching_preserve_draft_and_request_focus(draft_probe, tmp_path):
    snapshots = draft(draft_probe, tmp_path, [
        {"action": "open", "text": "Investigate parser.py", "settings": settings(effort="high")},
        {"action": "leave"}, {"action": "open"}, {"action": "open"},
    ], "/work/parser")
    assert [s["isPresented"] for s in snapshots] == [True, False, True, True]
    assert [s["focusRevision"] for s in snapshots] == [1, 1, 2, 3]
    assert all(s["text"] == "Investigate parser.py" for s in snapshots)
    assert all(s["workspace"] == "/work/parser" for s in snapshots)
    assert all(s["settings"]["effort"] == "high" for s in snapshots)


def test_no_folder_is_a_persistent_explicit_choice(draft_probe, tmp_path):
    snapshots = draft(draft_probe, tmp_path, [
        {"action": "open"}, {"workspace": None}, {"action": "restore"}, {"action": "open"},
    ], "/work/remembered")
    assert snapshots[0]["workspace"] == "/work/remembered"
    assert all(s["workspace"] is None for s in snapshots[1:])


def test_send_requires_content_and_model_but_no_folder(draft_probe, tmp_path):
    snapshots = draft(draft_probe, tmp_path, [
        {"settings": settings()}, {"text": " \n "}, {"text": "Hello"},
        {"settings": settings(model="")},
    ])
    assert [s["can_send"] for s in snapshots] == [False, False, True, False]


def test_draft_keeps_unsent_content_until_journaled(draft_probe, tmp_path):
    image = {"path": "/cache/image.png", "sha256": "abc", "media_type": "image/png", "bytes": 42}
    snapshots = draft(draft_probe, tmp_path, [
        {"action": "open", "settings": settings(), "attachments": [image]},
        {"submitting": True}, {"action": "journaled"},
    ], "/work")
    assert snapshots[0]["can_send"] is True
    assert snapshots[1]["can_send"] is False and snapshots[1]["attachments"] == [image]
    assert snapshots[2]["attachments"] == [] and snapshots[2]["text"] == ""
    assert snapshots[2]["workspace"] == "/work" and snapshots[2]["settings"]["model"] == "opus"
    assert snapshots[2]["isPresented"] is True and snapshots[2]["focusRevision"] == 2


def test_restart_restores_editable_text_and_settings(draft_probe, tmp_path):
    snapshots = draft(draft_probe, tmp_path, [
        {"action": "open", "text": "Still unsent", "settings": settings(effort="high"), "submitting": True},
        {"action": "restore"}, {"action": "open"},
    ])
    assert snapshots[1]["text"] == "Still unsent" and snapshots[1]["can_send"] is True
    assert snapshots[1]["isPresented"] is False and snapshots[1]["isSubmitting"] is False
    assert snapshots[2]["settings"]["effort"] == "high"


def test_wider_permission_needs_explicit_confirmation(draft_probe, tmp_path):
    snapshots = draft(draft_probe, tmp_path, [
        {"text": "Edit it", "settings": settings(permission="bypass")}, {"confirm_widen": True},
    ])
    assert [s["can_send"] for s in snapshots] == [False, True]


def test_provider_and_model_changes_reconcile_effort_and_permissions(draft_probe, tmp_path):
    snapshots = draft(draft_probe, tmp_path, [
        {"settings": settings(effort="high"), "models": [model(efforts=["low", "high"])]},
        {"models": [model(value="haiku", efforts=[])]},
        {"provider": "codex", "models": [model(provider="codex", value="gpt-6.1-sol", efforts=["ultra"])]},
    ])
    assert snapshots[0]["settings"]["effort"] == "high"
    assert snapshots[1]["settings"]["model"] == "haiku" and snapshots[1]["settings"]["effort"] is None
    assert snapshots[2]["settings"]["model"] == "gpt-6.1-sol"
    assert snapshots[2]["settings"]["permission"] == "read-only"
