"""API edges and defaults from the PR 124 review, using public operations."""
import copy
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


def test_default_codex_policy_routes_hard_to_sol_and_retires_astra(svc):
    policy = load_policy(DEFAULT_POLICY_PATH)
    svc.daemon.policy = policy
    hard = policy["tiers"].index("hard")
    assert {policy["models"][chain[hard]]["id"] for chain in policy["chains"].values()
            if policy["models"][chain[hard]]["provider"] == "codex"} == {"gpt-6.1-sol"}
    assert "gpt-6-astra" not in {entry["id"] for entry in policy["models"].values()}
    assert svc.handle("models.list", {"provider": "codex"}, None)["default_models"] == {"codex": "gpt-6.1-sol"}


def test_codex_default_follows_custom_hard_tier_instead_of_a_model_alias(svc):
    policy = copy.deepcopy(load_policy(DEFAULT_POLICY_PATH))
    policy["models"]["custom-hard"] = {"provider": "codex", "id": "custom-codex-hard"}
    hard = policy["tiers"].index("hard")
    for chain in policy["chains"].values():
        if policy["models"][chain[hard]]["provider"] == "codex":
            chain[hard] = "custom-hard"
    svc.daemon.policy = policy
    assert svc.handle("models.list", {"provider": "codex"}, None)["default_models"] == {"codex": "custom-codex-hard"}


def test_retired_astra_is_never_a_default_even_in_an_old_custom_policy(svc):
    svc.daemon.policy = {"models": {"old": {"provider": "codex", "id": "gpt-6-astra"},
                                   "current": {"provider": "codex", "id": "custom-current"}}}
    assert svc.handle("models.list", {}, None)["default_models"] == {"codex": "custom-current"}


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
