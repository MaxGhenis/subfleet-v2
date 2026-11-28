"""Round-two correctness regressions through the real UIModel and durable outbox."""
import json
from pathlib import Path

import pytest

from tests.frontend.conftest import needs_swift, write_json
from tests.frontend.test_core_app_cutover_reading import conversation
from tests.frontend.test_pr124_fixes_ui import data_for, review_probe, run, save_composer  # noqa: F401
from tests.frontend.test_app_cutover_start import refused_journal, world  # noqa: F401

pytestmark = needs_swift
UNREADY = ["unknown", "down", "busy", "incompatible", "refused"]


@pytest.mark.parametrize("status", UNREADY)
@pytest.mark.parametrize("previous_refusal", [False, True])
def test_unready_daemon_disables_start_with_reason(review_probe, tmp_path, world, status, previous_refusal):
    data = data_for(tmp_path, world)
    save_composer(data, world)
    refusal = {"ok": False, "reason": "protected-branch", "fix": "Choose another branch"}
    data.update(check_sequence=[refusal] if previous_refusal else [],
                fail_checks=0, steps=[{"action": "open"},
                                    {"action": "availability", "status": status}, {"action": "start"}])
    shots = run(review_probe, tmp_path, data)
    assert not shots[2]["can_start"]
    assert shots[2]["check_ok"] is False and shots[2]["check_reason"]
    assert shots[2]["check_calls"] == shots[1]["check_calls"]
    assert not (Path(data["root"]) / "support/outbox.json").exists()
    assert shots[-1]["text"] == "my unsent idea"


@pytest.mark.parametrize("status", UNREADY)
def test_start_rejects_stale_acceptance_after_losing_readiness(review_probe, tmp_path, world, status):
    data = data_for(tmp_path, world, steps=[{"action": "open"}, {"action": "lose-ready", "status": status},
                                           {"action": "start"}])
    save_composer(data, world)
    shots = run(review_probe, tmp_path, data)
    assert shots[1]["can_start"]
    assert not (Path(data["root"]) / "support/outbox.json").exists()
    assert shots[-1]["text"] == "my unsent idea"
    assert not shots[-1]["can_start"] and shots[-1]["check_reason"]


def test_unready_initial_default_waits_and_rechecks_recent_folders(review_probe, tmp_path, world):
    refused = str(world.root)
    allowed = str(world.workspace)
    data = data_for(tmp_path, world, initial_status="unknown",
                    list={"conversations": [conversation("refused", "2026-10-04T12:00:00Z", workspace=refused),
                                            conversation("allowed", "2026-10-03T12:00:00Z", workspace=allowed)]},
                    checks={refused: {"ok": False, "reason": "protected-branch", "fix": "Choose another folder"}},
                    steps=[{"action": "type", "text": "keep me"}, {"action": "open"},
                           {"action": "start"}, {"action": "availability", "status": "ready"}])
    shots = run(review_probe, tmp_path, data)
    assert not shots[2]["can_start"] and shots[2]["check_reason"]
    assert shots[2]["workspace"] != refused and shots[2]["check_calls"] == 0
    assert not (Path(data["root"]) / "support/outbox.json").exists()
    assert shots[-1]["workspace"] == allowed and shots[-1]["can_start"]
    assert shots[-1]["check_calls"] == 2


@pytest.mark.parametrize("provider,model", [("claude", "claude-sonnet-5"), ("codex", "gpt-5.6-terra")])
def test_saved_model_survives_opening_before_catalog_load(review_probe, tmp_path, world, provider, model):
    data = data_for(tmp_path, world, steps=[{"action": "open"}, {"action": "models"}, {"action": "reload"}])
    data["delayed_models"] = data["models"]
    data["models"] = {}
    save_composer(data, world)
    saved_path = Path(data["root"]) / "support/new-conversation-draft.json"
    saved = json.loads(saved_path.read_text())
    saved.update(provider=provider, providerChoice=provider, settings=world.settings(model=model, permission="read-only"))
    write_json(saved_path, saved)
    shots = run(review_probe, tmp_path, data)
    assert all(shot["model"] == model for shot in shots)
    assert all(shot["saved_model"] == model for shot in shots)


def test_scratch_start_resets_its_composer_when_retry_opens_during_check(review_probe, tmp_path, world):
    data = data_for(tmp_path, world, check_delay_ms=400, steps=[
        {"action": "type", "text": "scratch message"}, {"action": "open"},
        {"action": "start-switch-retry", "id": "app-second"}, {"action": "wait", "ms": 1000},
        {"action": "open"}, {"action": "type", "text": "second scratch message"}, {"action": "start"}])
    refused_journal(Path(data["root"]) / "support/outbox.json", world.settings(permission="read-only"))
    shots = run(review_probe, tmp_path, data)
    first_scratch = shots[2]["scratch_workspace"]
    assert shots[4]["recovering"] == "app-second" and shots[4]["workspace"] == "/refused/home"
    assert shots[5]["text"] == "" and shots[5]["scratch_workspace"] != first_scratch
    entries = json.loads((Path(data["root"]) / "support/outbox.json").read_text())["entries"]
    new_creates = [entry["create"]["workspace"] for entry in entries
                   if entry["kind"] == "conversation.create" and entry["key"] not in ("app-first", "app-second")]
    assert len(new_creates) == 2 and len(set(new_creates)) == 2
