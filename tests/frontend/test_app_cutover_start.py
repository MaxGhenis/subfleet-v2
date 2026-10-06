"""First use through the real UI model and outbox, with isolated app storage."""
import json
import re
import uuid

import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import ServiceHarness
from tests.frontend.test_core_app_cutover_reading import conversation

pytestmark = needs_swift
MESSAGE = "im in the subfleet app now instead of claude code - can we resume stuff that was running"


@pytest.fixture
def world(tmp_path):
    harness = ServiceHarness(tmp_path / "daemon")
    try:
        yield harness
    finally:
        harness.close()


def input_for(tmp_path, world, **extra):
    root = tmp_path / "app"
    root.joinpath("support").mkdir(parents=True, exist_ok=True)
    return {
        "root": str(root), "list": {"conversations": []},
        "models": {p: world.call("models.list", provider=p) for p in ("claude", "codex")},
        "checks": {}, "fallback_check": world.call("workspace.check", workspace=str(world.workspace)),
        "steps": [], "availability": world.call("capabilities"), **extra,
    }


def show(probe, tmp_path, data):
    return run_probe(probe, write_json(tmp_path / "input.json", data))


def route_codex_hard(world, short="sol61", model_id="gpt-6.1-sol"):
    """A loaded-policy stand-in; keep Astra available for explicit pins."""
    policy = world.daemon.policy
    policy["models"][short] = {"provider": "codex", "id": model_id}
    hard = policy["tiers"].index("hard")
    for chain in policy["chains"].values():
        if policy["models"][chain[hard]]["provider"] == "codex":
            chain[hard] = short


@pytest.mark.parametrize("short,model_id", [("sol61", "gpt-6.1-sol"), ("other-hard", "custom-codex-hard")])
def test_codex_draft_uses_the_loaded_policy_default(cutover_model_probe, tmp_path, world, short, model_id):
    route_codex_hard(world, short, model_id)
    data = input_for(tmp_path, world, steps=[{"action": "provider", "provider": "codex"}, {"action": "open"}])
    hard_model = next(m for m in data["models"]["codex"]["models"] if m["short"] == short)
    hard_model.update(value="observed-hard-value", values=["observed-hard-value"])
    assert show(cutover_model_probe, tmp_path, data)["snapshots"][-1]["draft"]["settings"]["model"] == "observed-hard-value"


def refused_journal(path, settings=None):
    """The live outbox's orders 16–18, with fresh ids and an isolated folder."""
    settings = settings or {"model": "gpt-6-astra", "permission": "ask", "effort": None, "fast": False, "auto_continue": True}
    failure = {"code": 7, "reason": "protected-workspace", "message": "protected-workspace: home contains state",
               "retryable": False}
    entries = []
    for order, key in [(16, "app-first"), (17, "app-second")]:
        entries.append({"kind": "conversation.create", "key": key, "order": order, "conversation": "draft:" + key,
                        "create": {"request_id": key, "provider": "codex", "workspace": "/refused/home",
                                   "workspace_kind": "in-place", "settings": settings},
                        "state": "failed", "attempts": 1, "failure": failure,
                        "waitingForPredecessor": False, "createdAt": "2026-10-03T12:45:28.112Z"})
    mid = str(uuid.uuid4())
    entries.append({"kind": "message.submit", "key": mid, "order": 18, "conversation": "draft:app-second",
                    "message": {"text": MESSAGE, "attachments": [], "settings": settings},
                    "state": "queued", "attempts": 0, "waitingForPredecessor": False,
                    "createdAt": "2026-10-03T12:45:56.132Z"})
    journal = {"version": 1, "nextOrder": 19, "entries": entries, "chains": {}}
    write_json(path, journal)
    return mid, journal


def test_default_skips_refused_recent_folders_and_does_not_remember_home(cutover_model_probe, tmp_path, world):
    refused = str(world.root)
    allowed = str(world.workspace)
    data = input_for(tmp_path, world, list={"conversations": [
        conversation("protected", "2026-10-03T12:45:00Z", workspace=refused),
        conversation("project", "2026-10-03T11:00:00Z", workspace=allowed),
    ]}, checks={p: world.call("workspace.check", workspace=p, permission="accept-edits")
                for p in (refused, allowed)}, steps=[{"action": "open", "text": "Hello", "confirm": True}])
    out = show(cutover_model_probe, tmp_path, data)
    draft = out["snapshots"][-1]["draft"]
    assert draft["workspace"] == allowed and draft["can_start"]
    assert out["remembered_workspace"] is None
    assert [c["args"]["workspace"] for c in out["calls"]] == [refused, allowed]


def test_no_recent_folder_reserves_scratch_without_creating_it(cutover_model_probe, tmp_path, world):
    data = input_for(tmp_path, world, steps=[{"action": "open", "text": "Hello", "confirm": True}])
    out = show(cutover_model_probe, tmp_path, data)
    draft = out["snapshots"][-1]["draft"]
    assert draft["workspace"] is None and draft["can_start"] and not draft["exists"]
    assert re.search(r"/support/scratch/\d{4}-\d{2}-\d{2}-[0-9a-f]{6}$", draft["scratchWorkspace"])
    assert out["calls"][0]["op"] == "workspace.check"


def test_start_creates_and_checks_scratch_before_journaling(cutover_model_probe, tmp_path, world):
    data = input_for(tmp_path, world, steps=[{"action": "open", "text": "Hello", "confirm": True}, {"action": "start"}])
    out = show(cutover_model_probe, tmp_path, data)
    reserved = out["snapshots"][1]["draft"]["scratchWorkspace"]
    journal = json.loads((tmp_path / "app/support/outbox.json").read_text())
    assert journal["entries"][0]["create"]["workspace"] == reserved
    assert journal["entries"][1]["message"]["text"] == "Hello"
    assert any(c["args"]["workspace"] == reserved for c in out["calls"])
    from pathlib import Path
    assert Path(reserved).is_dir() and Path(reserved).stat().st_mode & 0o777 == 0o700


def test_folder_refusal_disables_start_and_scratch_recovers(cutover_model_probe, tmp_path, world):
    refused = str(world.root)
    data = input_for(tmp_path, world, checks={refused: world.call(
        "workspace.check", workspace=refused, permission="accept-edits")}, steps=[
        {"action": "open", "text": "Hello", "confirm": True}, {"action": "folder", "path": refused},
    ])
    out = show(cutover_model_probe, tmp_path, data)
    draft = out["snapshots"][-1]["draft"]
    assert not draft["can_start"] and "protected-workspace" in draft["workspaceCheck"]["reason"]
    assert draft["workspaceCheck"]["fix"]
    saved = json.loads((tmp_path / "app/support/new-conversation-draft.json").read_text())
    assert saved.get("workspace") is None
    data["steps"].append({"action": "scratch"})
    assert show(cutover_model_probe, tmp_path, data)["snapshots"][-1]["draft"]["can_start"]


@pytest.mark.parametrize("remembered", [None, "claude-sonnet-5"])
def test_model_uses_provider_memory_or_daemon_opus_default(cutover_model_probe, tmp_path, world, remembered):
    data = input_for(tmp_path, world, remembered={"claude": remembered} if remembered else {},
                     steps=[{"action": "open"}])
    draft = show(cutover_model_probe, tmp_path, data)["snapshots"][-1]["draft"]
    expected = remembered or "claude-opus-5-5"
    assert draft["settings"]["model"] == expected and draft["resolution"] == "Claude · " + expected


def test_daemon_default_selects_its_observed_model_value(cutover_model_probe, tmp_path, world):
    data = input_for(tmp_path, world, steps=[{"action": "open"}])
    opus = next(m for m in data["models"]["claude"]["models"] if m["short"] == "opus")
    opus.update(value="opus[1m]", values=["opus[1m]"])
    assert show(cutover_model_probe, tmp_path, data)["snapshots"][-1]["draft"]["settings"]["model"] == "opus[1m]"


def test_model_used_in_existing_conversation_becomes_new_conversation_default(cutover_model_probe, tmp_path, world):
    data = input_for(tmp_path, world, list={"conversations": [conversation("existing", "2026-10-03T12:45:00Z")]},
                     steps=[{"action": "send-existing", "id": "existing", "settings": world.settings(model="claude-sonnet-5")},
                            {"action": "open"}])
    assert show(cutover_model_probe, tmp_path, data)["snapshots"][-1]["draft"]["settings"]["model"] == "claude-sonnet-5"


def test_migration_exposes_both_failed_creates_and_footer_selects_the_saved_message(cutover_model_probe, tmp_path, world):
    data = input_for(tmp_path, world, steps=[{"action": "select-failure"},
                                            {"action": "change-failure", "id": "app-second"}])
    mid, original = refused_journal(tmp_path / "app/support/outbox.json")
    out = show(cutover_model_probe, tmp_path, data)
    initial, selected, restored = out["snapshots"]
    assert [d["id"] for d in initial["failed"]] == ["app-second", "app-first"]
    assert initial["failed"][0]["text"] == MESSAGE and initial["failed"][0]["message_ids"] == [mid]
    assert initial["footer"] and selected["selected"] == "app-second"
    assert restored["draft"]["text"] == MESSAGE
    assert restored["draft"]["workspace"] == "/refused/home"
    assert restored["draft"]["settings"]["model"] == "gpt-6-astra"
    assert restored["draft"]["settings"]["permission"] == "read-only"
    migrated = json.loads((tmp_path / "app/support/outbox.json").read_text())
    assert migrated["draftRecoveryVersion"] == 1 and migrated["entries"] == original["entries"]
    assert out["visible_windows"] == 0


def test_discard_withdraws_only_the_selected_draft_and_queued_message(cutover_model_probe, tmp_path, world):
    data = input_for(tmp_path, world, steps=[{"action": "discard", "id": "app-second"}])
    refused_journal(tmp_path / "app/support/outbox.json")
    out = show(cutover_model_probe, tmp_path, data)
    assert [d["id"] for d in out["snapshots"][-1]["failed"]] == ["app-first"]
    entries = json.loads((tmp_path / "app/support/outbox.json").read_text())["entries"]
    assert [e["state"] for e in entries] == ["failed", "withdrawn", "withdrawn"]


def test_retry_preserves_ids_and_sends_the_first_message_after_fixed_create(core_probe, tmp_path, world):
    journal_path = tmp_path / "outbox.json"
    settings = world.settings()
    mid, original = refused_journal(journal_path, settings)
    created = world.call("conversation.create", request_id="app-second", provider="codex",
                         workspace=str(world.workspace), settings={**settings, "permission": "read-only"})
    cid = created["conversation"]["conversation_id"]
    receipt = world.call("message.submit", conversation_id=cid, message_id=mid, after_message_id=None,
                         text=MESSAGE, attachments=[], settings={**settings, "permission": "read-only"})
    script = write_json(tmp_path / "script.json", [
        {"op": op, "answer": {"id": "fixture", "ok": True, "result": result}}
        for op, result in [("conversation.create", created), ("message.submit", receipt)]
    ])
    steps = write_json(tmp_path / "steps.json", [
        {"do": "retry-draft", "key": "app-second", "workspace": str(world.workspace), "text": MESSAGE,
         "settings": {**settings, "permission": "read-only"}}, {"do": "pump"},
    ])
    out = run_probe(core_probe, "outbox", "script:" + str(script), journal_path, steps)
    assert out["results"][-1]["report"]["acknowledged"] == ["app-second", mid]
    assert out["calls"] == ["conversation.create app-second answered", f"message.submit {mid} after=null answered"]
    persisted = json.loads(journal_path.read_text())["entries"]
    assert [e["key"] for e in persisted] == [e["key"] for e in original["entries"]]
    assert persisted[-1]["message"]["text"] == MESSAGE and persisted[-1]["conversation"] == cid
