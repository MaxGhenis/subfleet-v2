"""2.1.10 first-use checks use the real create path and indexed activity."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import hypothesis
from hypothesis import given, strategies as st
import pytest

from subfleet import protocol
from subfleet.conversations import catalog
from subfleet.conversations.service import ConversationService
from subfleet.conversations.store import ConversationError
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy, resolve_model
from tests.unit.test_conversation_service import FakeDaemon, SETTINGS, conversation, svc, write_catalog  # noqa: F401


def create_args(workspace, permission="accept-edits", provider="claude", **extra):
    return {"request_id": str(uuid.uuid4()), "provider": provider, "workspace": str(workspace),
            "settings": {**SETTINGS, "permission": permission}, "confirm_widen": True, **extra}


def check_args(args):
    return {"workspace": args["workspace"], "provider": args["provider"],
            "permission": args["settings"]["permission"],
            **{k: args[k] for k in ("workspace_kind", "allow_main") if k in args}}


def accepted(service, args):
    """Differential oracle: the real public create operation, never its helper."""
    try:
        service.handle("conversation.create", args, None)
    except ConversationError as exc:
        return False, f"{exc.reason}: {exc}", exc.fix
    return True, None, None


def test_workspace_check_is_advertised_and_uses_the_file_pool(svc):
    assert "workspace.check" in protocol.CONVERSATION_OPS
    assert "workspace.check.v1" in svc.handle("capabilities", {}, None)["capabilities"]
    assert svc.pool_for("workspace.check") is svc.files


@pytest.mark.parametrize("permission", ["accept-edits", "bypass"])
def test_workspace_check_refuses_protected_home_without_writing(svc, tmp_path, monkeypatch, permission):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    before = list(svc.store.list_conversations())
    out = svc.handle("workspace.check", {"workspace": str(home), "permission": permission}, None)
    assert out["ok"] is False and out["reason"].startswith("protected-workspace:")
    assert ".claude" in out["reason"] and out["fix"]
    assert svc.store.list_conversations() == before


def test_read_only_home_is_allowed_by_check_and_real_create(svc, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    args = create_args(home, "read-only")
    assert svc.handle("workspace.check", check_args(args), None)["ok"] is True
    assert accepted(svc, args) == (True, None, None)


@pytest.mark.parametrize("name", ["main", "master"])
def test_workspace_check_and_create_refuse_writable_protected_branch(svc, tmp_path, monkeypatch, name):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", name, str(repo)], check=True)
    monkeypatch.setattr(svc, "_person", lambda *args: None)
    args = create_args(repo)
    check = svc.handle("workspace.check", check_args(args), None)
    assert check["ok"] is False and check["reason"].startswith("protected-branch:")
    assert (check["ok"], check["reason"], check["fix"]) == accepted(svc, args)
    for permission, allow_main in (("read-only", False), ("accept-edits", True)):
        permitted = create_args(repo, permission, allow_main=allow_main)
        assert svc.handle("workspace.check", check_args(permitted), None)["ok"] is True
        assert accepted(svc, permitted)[0] is True


def test_workspace_check_and_create_agree_on_invalid_folder_and_worktree(svc, monkeypatch):
    monkeypatch.setattr(svc, "_person", lambda *args: None)
    for args in (create_args(Path(svc.test_workspace) / "missing"),
                 create_args(svc.test_workspace, workspace_kind="worktree")):
        check = svc.handle("workspace.check", check_args(args), None)
        assert (check["ok"], check["reason"], check["fix"]) == accepted(svc, args)


@hypothesis.settings(max_examples=100, deadline=None)
@hypothesis.example(target="home", permission="accept-edits", provider="claude", spelling="plain")
@hypothesis.example(target="state-parent", permission="bypass", provider="claude", spelling="symlink")
@hypothesis.example(target="claude", permission="accept-edits", provider="claude", spelling="case")
@hypothesis.example(target="codex", permission="ask", provider="codex", spelling="symlink")
@hypothesis.example(target="project", permission="accept-edits", provider="claude", spelling="plain")
@given(target=st.sampled_from(["home", "state-parent", "state", "claude", "codex", "lane", "project"]),
       permission=st.sampled_from(["read-only", "ask", "accept-edits", "bypass"]),
       provider=st.sampled_from(["claude", "codex"]), spelling=st.sampled_from(["plain", "symlink", "case"]))
def test_generated_workspace_check_matches_real_create(target, permission, provider, spelling):
    """Home, state ancestors, provider homes, links, APFS case variants and projects.

    On case-sensitive filesystems a missing case variant is refused by both;
    on case-insensitive APFS its actual inode is protected by both. Only peer
    identity and Path.home are supplied by the fixture; create validation,
    canonicalization and its store writes are all real.
    """
    with tempfile.TemporaryDirectory(prefix="sf-cutover-property-", dir="/private/tmp") as raw:
        base = Path(raw)
        home = base / "Home"
        root = home / ".subfleet" / "state"
        targets = {"home": home, "state-parent": root.parent, "state": root,
                   "claude": home / ".claude", "codex": home / ".codex",
                   "lane": home / ".codex" / "lane", "project": home / "Projects" / "project"}
        for path in targets.values():
            path.mkdir(parents=True, exist_ok=True)
        workspace = targets[target]
        if spelling == "symlink":
            alias = base / "alias"
            alias.symlink_to(workspace, target_is_directory=True)
            workspace = alias
        elif spelling == "case":
            workspace = Path(str(workspace).replace("Home", "hOME").replace(".claude", ".CLAUDE")
                             .replace(".codex", ".CODEX").replace("Projects", "pROJECTS"))
        daemon = FakeDaemon(root)
        service = ConversationService(daemon)
        try:
            (root / "conversations" / "codex-writable-verified.json").write_text("{}")
            with mock.patch.object(Path, "home", return_value=home), mock.patch.object(service, "_person"):
                args = create_args(workspace, permission, provider)
                check = service.handle("workspace.check", check_args(args), None)
                assert (check["ok"], check["reason"], check["fix"]) == accepted(service, args)
        finally:
            service.close()
            daemon.store.close()


def test_case_insensitive_protected_alias_is_refused_when_the_filesystem_accepts_it(svc, tmp_path, monkeypatch):
    home = tmp_path / "Home"
    (home / ".claude").mkdir(parents=True)
    alias = tmp_path / "hOME"
    if not alias.is_dir():
        pytest.skip("this filesystem is case-sensitive")
    monkeypatch.setattr(Path, "home", lambda: home)
    out = svc.handle("workspace.check", {"workspace": str(alias), "permission": "accept-edits"}, None)
    assert out["ok"] is False and out["reason"].startswith("protected-workspace:")


def test_last_activity_uses_the_last_catalog_run_before_sorting_and_limiting(svc, monkeypatch):
    sid = "96de7576-0000-4000-8000-000000000000"
    old = conversation(svc, native_session_id=sid.upper(), origin="native")
    newer = conversation(svc)
    svc.store.update_conversation(old, updated_at="2026-09-28T12:00:00Z")
    svc.store.update_conversation(newer, updated_at="2026-10-01T12:00:00Z")
    mtime = datetime(2026, 10, 3, 12, 45, tzinfo=UTC).timestamp()
    write_catalog(svc.root, "2026-10-03T12:45:00Z", [{"provider": "claude", "native_session_id": sid, "mtime": mtime}])
    with mock.patch.object(catalog, "build", side_effect=AssertionError("list scanned the projects tree")), \
            mock.patch.object(Path, "rglob", side_effect=AssertionError("list walked the projects tree")):
        rows = svc.handle("conversation.list", {"limit": 1, "include_catalog": False}, None)["conversations"]
    assert [row["conversation_id"] for row in rows] == [old]
    assert rows[0]["last_activity"] == "2026-10-03T12:45:00.000Z"
    assert rows[0]["updated_at"] == "2026-09-28T12:00:00Z"


def test_last_activity_keeps_newer_row_and_falls_back_without_catalog(svc):
    cid = conversation(svc, native_session_id="s-1", origin="native")
    svc.store.update_conversation(cid, updated_at="2026-10-03T13:00:00Z")
    write_catalog(svc.root, "2026-09-28T12:00:00Z", [{"provider": "claude", "native_session_id": "s-1", "mtime": 1.0}])
    assert svc.handle("conversation.list", {}, None)["conversations"][0]["last_activity"] == "2026-10-03T13:00:00.000Z"
    (svc.root / "catalog.json").write_text("broken")
    assert svc.handle("conversation.list", {}, None)["conversations"][0]["last_activity"] == "2026-10-03T13:00:00.000Z"


def test_activity_times_keeps_all_provider_bound_entries_and_ignores_bad_mtimes(tmp_path):
    rows = [{"provider": "claude", "native_session_id": f"s-{n}", "mtime": float(n)} for n in range(205)]
    rows += [{"provider": "codex", "native_session_id": "s-1", "mtime": 500},
             {"provider": "claude", "native_session_id": "s-1", "mtime": 600},
             {"provider": "claude", "native_session_id": "bad", "mtime": float("nan")},
             {"provider": "claude", "native_session_id": "bool", "mtime": True}, "bad"]
    write_catalog(tmp_path, "2026-09-28T12:00:00Z", rows)
    activity = catalog.activity_times(tmp_path)
    assert len(activity) == 206 and activity[("claude", "s-204")] == 204
    assert activity[("claude", "s-1")] == 600 and activity[("codex", "s-1")] == 500


def test_models_publish_claude_opus_and_sol_defaults_and_route_no_chain_to_astra(svc):
    svc.daemon.policy = load_policy(DEFAULT_POLICY_PATH)
    out = svc.handle("models.list", {}, None)
    assert out["default_models"] == {"claude": "claude-opus-5-5", "codex": "gpt-6.1-sol"}
    claude = svc.handle("models.list", {"provider": "claude"}, None)
    assert claude["default_models"] == {"claude": "claude-opus-5-5"}
    assert {row["id"] for row in claude["models"]} == {"claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"}
    assert all("astra" not in chain for chain in svc.daemon.policy["chains"].values())
    assert resolve_model(svc.daemon.policy, "sol") == "sol"
    assert resolve_model(svc.daemon.policy, "gpt-6-astra") == "astra"
