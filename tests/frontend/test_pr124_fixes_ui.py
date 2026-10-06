"""Review regressions through UIModel; all app/daemon storage is isolated."""
from datetime import datetime, timezone
import json
from pathlib import Path
import pytest

from subfleet.status_json import build_status
from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.swift import ROOT, compile_probe
from tests.frontend.test_app_cutover_start import MESSAGE, input_for, refused_journal, route_codex_hard, world  # noqa: F401
from tests.frontend.test_status_model import lane

pytestmark = needs_swift


@pytest.fixture(scope="session")
def review_probe(tmp_path_factory):
    return compile_probe(tmp_path_factory.mktemp("review-fixes") / "probe",
                         [ROOT / "tests/frontend/ReviewCutoverProbe.swift",
                          ROOT / "tests/frontend/CutoverViewScenarios.swift"], "SUBFLEET_UI_MODEL_TEST")


def data_for(tmp_path, world, **extra):
    return input_for(tmp_path, world, availability=world.call("capabilities"),
                     defaults={"providerChoice": "claude"}, **extra)


def run(probe, tmp_path, data, env=None):
    return run_probe(probe, write_json(tmp_path / "input.json", data), env=env)["snapshots"]


def save_composer(data, world):
    write_json(Path(data["root"]) / "support/new-conversation-draft.json",
               {"text": "my unsent idea", "attachments": [], "workspace": str(world.workspace),
                "provider": "claude", "providerChoice": "claude", "settings": world.settings(),
                "confirmWiden": False, "isPresented": False, "focusRevision": 0, "isSubmitting": False})


def test_retry_preserves_composer_and_original_message_identity(review_probe, tmp_path, world):
    data = data_for(tmp_path, world, steps=[{"action": "change-failure", "id": "app-second"},
                                          {"action": "open"}, {"action": "start"},
                                          {"action": "change-failure", "id": "app-second"},
                                          {"action": "folder", "path": str(world.workspace)},
                                          {"action": "start"}, {"action": "reload"}])
    mid, _ = refused_journal(Path(data["root"]) / "support/outbox.json", world.settings(permission="read-only"))
    save_composer(data, world)
    shots = run(review_probe, tmp_path, data)
    assert shots[1]["saved_text"] == "my unsent idea"
    assert shots[1]["recovering"] == "app-second"
    assert shots[2]["recovering"] is None and shots[2]["text"] == "my unsent idea"
    entries = json.loads((Path(data["root"]) / "support/outbox.json").read_text())["entries"]
    original = [e for e in entries if e.get("message", {}).get("text") == MESSAGE]
    assert len(original) == 1 and original[0]["key"] == mid
    assert len([e for e in entries if e.get("message", {}).get("text") == "my unsent idea"]) == 1


def test_switching_retries_and_relaunch_preserve_the_composer(review_probe, tmp_path, world):
    data = data_for(tmp_path, world, steps=[{"action": "change-failure", "id": "app-second"},
                                          {"action": "change-failure", "id": "app-first"},
                                          {"action": "reload"}, {"action": "open"}])
    refused_journal(Path(data["root"]) / "support/outbox.json")
    save_composer(data, world)
    shots = run(review_probe, tmp_path, data)
    assert all(shot["saved_text"] == "my unsent idea" for shot in shots)
    assert shots[-1]["text"] == "my unsent idea"


@pytest.mark.parametrize("claude,codex,expected", [(0, 0, "claude"), (0, 3, "codex"), (1, 3, "claude")])
def test_auto_requires_a_ready_codex_lane(review_probe, tmp_path, world, claude, codex, expected):
    route_codex_hard(world)
    state = tmp_path / "state"
    state.mkdir()
    payload = build_status({"lanes": [lane("codex"), lane("claude")], "offline": False}, now=datetime.now(timezone.utc))
    payload["claude"]["lanes"] = {"dispatchable_now": claude}
    payload["codex"]["fleet"]["dispatchable_now"] = codex
    write_json(state / "status.json", payload)
    data = input_for(tmp_path, world, availability=world.call("capabilities"),
                     defaults={"providerChoice": "auto", "lastModel.codex": "gpt-6-astra"}, steps=[{"action": "open"}])
    shot = run(review_probe, tmp_path, data, env={"SUBFLEET_HOME": str(state)})[-1]
    assert shot["provider"] == expected
    if expected == "codex":
        assert shot["model"] == "gpt-6.1-sol"


def test_remembered_astra_cannot_override_the_daemon_hard_default(review_probe, tmp_path, world):
    route_codex_hard(world)
    data = input_for(tmp_path, world, availability=world.call("capabilities"),
                     defaults={"providerChoice": "codex", "lastModel.codex": "gpt-6-astra"}, steps=[{"action": "open"}])
    assert data["models"]["codex"]["models"][0]["id"] == "gpt-6-astra"
    assert run(review_probe, tmp_path, data)[-1]["model"] == "gpt-6.1-sol"


@pytest.mark.parametrize("default", [None, "unpublished-model"])
def test_codex_default_falls_back_to_first_non_retired_model(review_probe, tmp_path, world, default):
    data = input_for(tmp_path, world, defaults={"providerChoice": "codex"}, steps=[{"action": "open"}])
    codex = data["models"]["codex"]
    template = codex["models"][0]
    codex["models"] = [dict(template, short=short, id=short, value=short, values=[short], retired=retired)
                       for short, retired in [("retired-model", True), ("first-active", False), ("second-active", False)]]
    codex["default_models"] = {"codex": default} if default else {}
    assert run(review_probe, tmp_path, data)[-1]["model"] == "first-active"


def test_older_daemon_skips_optional_check_at_open_and_start(review_probe, tmp_path, world):
    caps = world.call("capabilities")
    caps["capabilities"].remove("workspace.check.v1")
    data = input_for(tmp_path, world, availability=caps, unknown_op_checks=True,
                     defaults={"providerChoice": "claude"}, steps=[{"action": "open"},
                     {"action": "type", "text": "hello"}, {"action": "start"}])
    shots = run(review_probe, tmp_path, data)
    assert shots[2]["can_start"] is True
    assert shots[-1]["check_calls"] == 0
    entries = json.loads((Path(data["root"]) / "support/outbox.json").read_text())["entries"]
    assert [e["kind"] for e in entries] == ["conversation.create", "message.submit"]


def test_unanswered_check_keeps_folder_and_rechecks_on_reconcile(review_probe, tmp_path, world):
    data = data_for(tmp_path, world, fail_checks=1, steps=[{"action": "open"},
                    {"action": "type", "text": "hello"}, {"action": "reconcile"}])
    save_composer(data, world)
    shots = run(review_probe, tmp_path, data)
    assert shots[1]["saved_workspace"] == str(world.workspace)
    assert shots[-1]["check_calls"] == 2 and shots[-1]["can_start"] is True


def test_same_folder_can_retry_a_transport_failure(review_probe, tmp_path, world):
    data = data_for(tmp_path, world, fail_checks=1, steps=[{"action": "open"},
                    {"action": "folder", "path": str(world.workspace)}, {"action": "type", "text": "hello"}])
    save_composer(data, world)
    shot = run(review_probe, tmp_path, data)[-1]
    assert shot["check_calls"] == 2 and shot["can_start"] is True


def test_last_successful_retry_clears_only_its_notice(review_probe, tmp_path, world):
    data = data_for(tmp_path, world, steps=[{"action": "change-failure", "id": "app-second"},
                    {"action": "folder", "path": str(world.workspace)}, {"action": "start"}])
    support = Path(data["root"]) / "support"
    settings = world.settings(permission="read-only")
    mid, journal = refused_journal(support / "outbox.json", settings)
    journal["entries"] = [e for e in journal["entries"] if e["key"] != "app-first"]
    write_json(support / "outbox.json", journal)
    created = world.call("conversation.create", request_id="app-second", provider="codex",
                         workspace=str(world.workspace), settings=settings)
    receipt = world.call("message.submit", conversation_id=created["conversation"]["conversation_id"], message_id=mid,
                         after_message_id=None, text=MESSAGE, attachments=[], settings=settings)
    data["answers"] = {"conversation.create": created, "message.submit": receipt}
    shot = run(review_probe, tmp_path, data)[-1]
    assert shot["failed"] == [] and shot["footer"] is None


def test_a_send_does_not_wait_for_folder_git_checks(review_probe, tmp_path, world):
    from tests.frontend.test_core_app_cutover_reading import conversation
    data = data_for(tmp_path, world, check_delay_ms=2000,
                    list={"conversations": [conversation("existing", "2026-10-03T12:45:00Z", workspace=str(world.workspace))]},
                    steps=[{"action": "open-send", "id": "existing", "settings": world.settings()}])
    shot = run(review_probe, tmp_path, data)[-1]
    assert 0 <= shot["send_wait_ms"] < 1000


def test_start_rechecks_a_folder_and_journals_nothing_if_it_changed(review_probe, tmp_path, world):
    ok = world.call("workspace.check", workspace=str(world.workspace))
    refused = world.call("workspace.check", workspace=str(world.workspace / "deleted"))
    data = data_for(tmp_path, world, check_sequence=[ok, refused, refused], steps=[{"action": "open"},
                    {"action": "type", "text": "hello"}, {"action": "start"}])
    shot = run(review_probe, tmp_path, data)[-1]
    assert not (Path(data["root"]) / "support/outbox.json").exists()
    assert shot["text"] == "hello" and not shot["can_start"]


def test_relaunch_after_journaling_never_offers_the_same_words_again(review_probe, tmp_path, world):
    data = data_for(tmp_path, world, steps=[{"action": "open"}, {"action": "start"}])
    save_composer(data, world)
    mid, journal = refused_journal(Path(data["root"]) / "support/outbox.json", world.settings())
    journal["entries"][-1]["message"]["text"] = "my unsent idea"
    write_json(Path(data["root"]) / "support/outbox.json", journal)
    saved_path = Path(data["root"]) / "support/new-conversation-draft.json"
    saved = json.loads(saved_path.read_text())
    saved.update(requestID="app-second", messageID=mid)
    write_json(saved_path, saved)
    shots = run(review_probe, tmp_path, data)
    assert all(shot["text"] == "" for shot in shots)
    assert len(json.loads((Path(data["root"]) / "support/outbox.json").read_text())["entries"]) == 3


def test_create_already_acknowledged_after_a_crash_receives_its_original_message(review_probe, tmp_path, world):
    data = data_for(tmp_path, world, steps=[{"action": "open"}, {"action": "start"}])
    save_composer(data, world)
    mid, journal = refused_journal(Path(data["root"]) / "support/outbox.json", world.settings())
    created = world.call("conversation.create", request_id="app-second", provider="claude",
                         workspace=str(world.workspace), settings=world.settings())
    cid = created["conversation"]["conversation_id"]
    entry = journal["entries"][1]
    entry.update(state="acknowledged", conversationID=cid)
    entry["create"].update(provider="claude", workspace=str(world.workspace))
    journal["entries"] = [entry]
    journal["chains"] = {cid: {"lastPersonMessageID": None}}
    write_json(Path(data["root"]) / "support/outbox.json", journal)
    saved_path = Path(data["root"]) / "support/new-conversation-draft.json"
    saved = json.loads(saved_path.read_text())
    saved.update(requestID="app-second", messageID=mid)
    write_json(saved_path, saved)
    run(review_probe, tmp_path, data)
    entries = json.loads((Path(data["root"]) / "support/outbox.json").read_text())["entries"]
    assert len(entries) == 2 and entries[-1]["key"] == mid and entries[-1]["conversation"] == cid


def test_successful_retry_keeps_an_unrelated_footer_problem(review_probe, tmp_path, world):
    data = data_for(tmp_path, world, steps=[{"action": "problem", "text": "another problem"},
                                           {"action": "relaunch-pump"}])
    assert run(review_probe, tmp_path, data)[-1]["footer"] == "another problem"
