"""D-ST1: admission still protects fake HOME; credential stubs remain usable."""

import json
import pwd
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import credentials, folders, home_paths
from subfleet.adapters import claude
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Credential
from subfleet.conversations.service import ConversationService


@pytest.mark.parametrize("alias", ["same", "symlink", "case"])
def test_workspace_checks_never_spell_account_stores(tmp_path, monkeypatch, alias):
    """Model the account home and aliases entirely with temporary directories."""
    personal = tmp_path / "Personal-Home"
    personal.mkdir()
    home = personal
    if alias == "symlink":
        home = tmp_path / "alias"
        home.symlink_to(personal, target_is_directory=True)
    elif alias == "case":
        home = tmp_path / "pERSONAL-hOME"
        if not home.is_dir():
            pytest.skip("needs a case-insensitive volume")
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(personal)))
    monkeypatch.setattr(Path, "home", lambda: home)
    seen = []

    def spell(path):
        # No lstat/open/listing of either implicit provider store is permitted.
        seen.append(Path(path))
        assert Path(path) not in (home / ".claude", home / ".codex")
        return str(path), None

    monkeypatch.setattr(folders, "spelling", spell)
    service = SimpleNamespace(root=tmp_path / "state",
                              daemon=SimpleNamespace(store=SimpleNamespace(lane_rows=lambda: [])))
    ConversationService._check_workspace(service, "claude", str(tmp_path / "workspace"),
                                         {"permission": "accept-edits"})
    assert seen == [tmp_path / "workspace", service.root,
                    Path(home_paths.home_path("CLAUDE_CONFIG_DIR", lambda: pytest.fail("fallback"))),
                    home_paths.codex_app_home()]


@pytest.mark.parametrize("variable", ["CLAUDE_CONFIG_DIR", "SUBFLEET_CODEX_APP_HOME"])
def test_fake_home_does_not_authorize_reader_fallback(tmp_path, monkeypatch, variable):
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.delenv(variable)
    monkeypatch.setattr(Path, "home", lambda: pytest.fail("HOME evaluated before guard"))
    with pytest.raises(RuntimeError, match=variable):
        home_paths.protected_provider_homes()


@pytest.mark.parametrize("reader", ["blob", "home-login", "token", "plan", "credential"])
def test_patched_subprocess_is_a_credential_standin(tmp_path, monkeypatch, reader):
    monkeypatch.delenv("CLAUDE_LANE_AGENT_SECRET", raising=False)
    oauth = {"accessToken": "fixture-token", "subscriptionType": "fixture-plan"}
    payload = json.dumps({"claudeAiOauth": oauth})
    calls = []

    def record(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, payload, "")

    monkeypatch.setattr(credentials.subprocess, "run", record)
    adapter = claude.ClaudeAdapter()
    reference = "fixture-service"
    if reader == "blob":
        assert claude._keychain_blob(reference) == payload
    elif reader == "home-login":
        home = tmp_path / "empty-home"
        reference = claude.keychain_service_for_home(home)
        assert claude.home_login(home) == oauth
    elif reader == "token":
        assert adapter._resolve_keychain_token(reference) == "fixture-token"
    elif reader == "plan":
        assert adapter._plan_from_keychain(reference) == "fixture-plan"
    else:
        assert credentials.resolve_credential(Credential("claude", reference, "keychain-token")) == {
            "CLAUDE_CODE_OAUTH_TOKEN": payload}
    assert calls == [["security", "find-generic-password", "-s", reference, "-w"]]


def test_adapter_guard_checks_captured_runner_after_subprocess_patch(monkeypatch):
    """Patching the module later cannot authorize an adapter's native runner."""
    calls = []

    def native(argv, **kwargs):
        calls.append(argv)
        pytest.fail("native backend reached")

    monkeypatch.setattr(credentials, "_NATIVE_RUNNER", native)
    monkeypatch.setattr(credentials.subprocess, "run", native)
    adapter = claude.ClaudeAdapter()
    monkeypatch.setattr(credentials.subprocess, "run", lambda *a, **k: pytest.fail("wrong runner"))
    with pytest.raises(AdapterError, match="refusing personal keychain"):
        adapter._resolve_keychain_token("fixture-service")
    assert adapter._plan_from_keychain("fixture-service") is None
    assert calls == []
