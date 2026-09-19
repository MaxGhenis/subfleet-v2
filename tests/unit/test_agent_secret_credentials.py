"""The dedicated v1 credential store remains the authority after cutover."""

import subprocess

import pytest

from subfleet.adapters.base import AdapterError
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.contracts import Credential
from subfleet.credentials import keychain_command, resolve_credential


@pytest.mark.parametrize("override", [False, True])
def test_daemon_and_enrollment_use_same_agent_helper(monkeypatch, tmp_path, override):
    monkeypatch.setenv("HOME", str(tmp_path))
    helper = tmp_path / ("custom helper" if override else "bin/agent-secret")
    helper.parent.mkdir(exist_ok=True)
    helper.touch()
    if override:
        monkeypatch.setenv("CLAUDE_LANE_AGENT_SECRET", str(helper))
    else:
        monkeypatch.delenv("CLAUDE_LANE_AGENT_SECRET", raising=False)
    reference = "claude-quota-test@example.test"
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["capture_output"] and kwargs["timeout"] <= 15
        return subprocess.CompletedProcess(argv, 0, "test-private-value\n", "")

    monkeypatch.setattr("subfleet.credentials.subprocess.run", run)
    credential = Credential("claude", reference, "keychain-token")
    assert resolve_credential(credential) == {"CLAUDE_CODE_OAUTH_TOKEN": "test-private-value"}
    assert ClaudeAdapter(runner=run).credential_env(credential) == {
        "CLAUDE_CODE_OAUTH_TOKEN": "test-private-value"}
    assert calls == [[str(helper), "get", reference]] * 2


@pytest.mark.parametrize("failure", ["returncode", "timeout"])
def test_failed_helper_never_falls_back_or_discloses_output(monkeypatch, failure):
    monkeypatch.setenv("CLAUDE_LANE_AGENT_SECRET", "/synthetic/agent-secret")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 10, output="test-private-value")
        return subprocess.CompletedProcess(argv, 1, "test-private-value", "test-private-value")

    monkeypatch.setattr("subfleet.credentials.subprocess.run", run)
    credential = Credential("claude", "claude-quota-test-reference", "keychain-token")
    for resolve in (resolve_credential, ClaudeAdapter(runner=run).credential_env):
        with pytest.raises(AdapterError) as error:
            resolve(credential)
        assert "test-private-value" not in str(error.value)
        assert error.value.__cause__ is None
    assert calls == [["/synthetic/agent-secret", "get", "claude-quota-test-reference"]] * 2


def test_host_without_agent_helper_retains_native_keychain(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_LANE_AGENT_SECRET", raising=False)
    assert keychain_command("claude-quota-reference", "/native/security") == [
        "/native/security", "find-generic-password", "-s", "claude-quota-reference", "-w"]


@pytest.mark.parametrize("reference", ["Claude Code-credentials", "Claude Code-credentials-12345678"])
def test_provider_owned_oauth_blobs_stay_in_native_keychain(monkeypatch, reference):
    monkeypatch.setenv("CLAUDE_LANE_AGENT_SECRET", "/synthetic/agent-secret")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0,
            '{"claudeAiOauth":{"accessToken":"native-test-token"}}', "")

    adapter = ClaudeAdapter(runner=run)
    assert adapter.credential_env(Credential("claude", reference, "keychain-token")) == {
        "CLAUDE_CODE_OAUTH_TOKEN": "native-test-token"}
    assert calls == [["security", "find-generic-password", "-s", reference, "-w"]]
