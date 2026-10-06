"""API edges and defaults from the PR 124 review, using public operations."""
import copy
import json
from datetime import datetime, timezone

import pytest

from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from tests.unit.test_app_cutover_daemon import accepted, create_args
from tests.unit.test_conversation_service import svc, conversation, write_catalog  # noqa: F401


@pytest.mark.parametrize("workspace", ["", None])
def test_no_folder_check_and_create_agree_without_check_writes(svc, workspace):
    args = create_args(workspace, "read-only")
    args["workspace"] = workspace
    before = list(svc.root.rglob("*"))
    check = svc.handle("workspace.check", {"workspace": workspace, "provider": "claude", "permission": "read-only"}, None)
    assert list(svc.root.rglob("*")) == before
    assert (check["ok"], check["reason"], check["fix"]) == accepted(svc, args)


def test_missing_provider_check_and_create_use_claude(svc, tmp_path):
    args = create_args(tmp_path, "read-only")
    del args["provider"]
    check = svc.handle("workspace.check", {"workspace": str(tmp_path), "permission": "read-only"}, None)
    assert (check["ok"], check["reason"], check["fix"]) == accepted(svc, args) == (True, None, None)


@pytest.mark.parametrize("short,model_id", [("sol61", "gpt-6.1-sol"), ("custom-hard", "custom-codex-hard")])
def test_codex_default_follows_loaded_hard_tier_policy(svc, tmp_path, short, model_id):
    policy = copy.deepcopy(load_policy(DEFAULT_POLICY_PATH))
    policy["models"][short] = {"provider": "codex", "id": model_id}
    hard = policy["tiers"].index("hard")
    for chain in policy["chains"].values():
        if policy["models"][chain[hard]]["provider"] == "codex":
            chain[hard] = short
    path = tmp_path / "loaded-policy.json"
    path.write_text(json.dumps(policy))
    svc.daemon.policy = load_policy(path)
    # Publishing must use what was loaded, even if the disk changes afterward.
    path.write_text(DEFAULT_POLICY_PATH.read_text())
    for args in ({}, {"provider": "codex"}):
        result = svc.handle("models.list", args, None)
        assert result["default_models"]["codex"] == model_id
        assert result["default_models"]["codex"] != "gpt-6-astra"
        astra = next(m for m in result["models"] if m["short"] == "astra")
        assert astra["id"] == "gpt-6-astra" and astra["retired"] is False


def test_codex_default_uses_first_active_unscoped_model_without_hard_routing(svc):
    svc.daemon.policy = load_policy(DEFAULT_POLICY_PATH)
    assert svc.handle("models.list", {"provider": "codex"}, None)["default_models"] == {"codex": "gpt-6-astra"}
    for chain in svc.daemon.policy["chains"].values():
        chain[svc.daemon.policy["tiers"].index("hard")] = "opus"
    assert svc.handle("models.list", {"provider": "codex"}, None)["default_models"] == {"codex": "gpt-6-astra"}


def test_active_astra_alone_without_hard_routing_is_the_published_default(svc):
    svc.daemon.policy = {"models": {"astra": {"provider": "codex", "id": "gpt-6-astra"}}}
    assert svc.handle("models.list", {"provider": "codex"}, None)["default_models"] == {"codex": "gpt-6-astra"}


def test_retired_astra_is_never_a_default_even_in_an_old_custom_policy(svc):
    svc.daemon.policy = {"models": {"old": {"provider": "codex", "id": "gpt-6-astra"},
                                   "current": {"provider": "codex", "id": "custom-current"}},
                         "tiers": ["hard"], "chains": {"build": ["old"]},
                         "retired": {"gpt-6-astra": "current"}}
    result = svc.handle("models.list", {}, None)
    assert result["default_models"] == {"codex": "custom-current"}
    assert result["models"][0]["retired"] is True


def test_future_activity_cannot_pin_an_older_conversation(svc, monkeypatch):
    now = datetime.now(timezone.utc).timestamp()
    cid = conversation(svc, native_session_id="future", origin="native")
    other = conversation(svc)
    with svc.store.transaction() as db:
        db.execute("UPDATE conversations SET updated_at=? WHERE conversation_id=?", ("2026-10-01T12:00:00Z", cid))
        db.execute("UPDATE conversations SET updated_at=? WHERE conversation_id=?", ("2026-10-02T12:00:00Z", other))
    write_catalog(svc.root, "2026-10-03T12:00:00Z", [
        {"provider": "claude", "native_session_id": "future", "mtime": now + 86400}])
    rows = svc.handle("conversation.list", {"include_catalog": False}, None)["conversations"]
    assert [row["conversation_id"] for row in rows] == [other, cid]
    assert rows[-1]["last_activity"].startswith("2026-10-01")
