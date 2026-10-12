"""Resolve personal paths without letting pytest fall back to live stores."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

PYTEST_MARKER = "SUBFLEET_UNDER_PYTEST"
# An inherited helper may query the person's dedicated keychain. Tests must
# explicitly install their own stand-in instead of inheriting this executable.
SAFE_ENV_REMOVALS = ("CLAUDE_LANE_AGENT_SECRET",)

# One registry for the session fixture and every subprocess harness. Values are
# relative to a temporary base, and must never name the operator's home stores.
SAFE_HOME_PATHS = {
    "SUBFLEET_SESSION_STORE": "claude-code-sessions",
    "SUBFLEET_DESKTOP_LOG": "logs/main.log",
    "SUBFLEET_CLAUDE_DIR": "claude",
    "SUBFLEET_HOME": "state",
    "CLAUDE_CONFIG_DIR": "claude-config",
    "SUBFLEET_CLAUDE_SETTINGS": "claude/settings.json",
    "SUBFLEET_CODEX_APP_HOME": "codex",
    "SUBFLEET_DESKTOP_LOGIN": "claude.json",
    "SUBFLEET_PROVIDER_HOME": "providers",
    "SUBFLEET_V1_STATE": "legacy/state",
    "SUBFLEET_DELEGATE_STATE": "legacy/delegate",
    "SUBFLEET_V1_ROSTER_DIR": "legacy/roster",
    "SUBFLEET_V1_LAUNCH_AGENTS": "legacy/agents",
    "SUBFLEET_LAUNCHD_PLIST": "agents/com.subfleet.daemon.plist",
    "SUBFLEET_AGENT_SECRET": "bin/agent-secret",
}


# Every credential command execution must go through credentials.keychain_command.
# These readers accept explicit runners/binaries; session baselines never grant
# permission to query the person's login keychain.
KEYCHAIN_READERS = {
    "subfleet.adapters.claude._keychain_blob",
    "subfleet.adapters.claude.ClaudeAdapter._resolve_keychain_token",
    "subfleet.adapters.claude.ClaudeAdapter._plan_from_keychain",
    "subfleet.credentials.resolve_credential",
}


def under_pytest() -> bool:
    return PYTEST_MARKER in os.environ or "PYTEST_CURRENT_TEST" in os.environ


def home_path(variable: str, default: Callable[[], str | Path], *,
              env: Mapping[str, str] | None = None, accept_empty: bool = False,
              test_only: bool = False, expand_override: bool = False) -> str | Path:
    """Test-only guard against implicit access to the person's home stores.

    On 2026-10-12 a test's monkeypatch.undo() undid the desktop-store fixture,
    and the mirror rewrote the person's live Claude session copies. Session
    environment isolation is the first defense; the session marker covers
    configure, collection and teardown, when PYTEST_CURRENT_TEST is absent.
    Either marker makes a missing override refuse the fallback before access.
    test_only supplies an isolation seam for defaults that historically had no
    environment override, preserving their behavior outside pytest. Optional
    expansion applies only to overrides, never to an already constructed default;
    raw overrides let other callers retain their expansion and empty handling.
    """
    testing = under_pytest()
    if test_only and not testing:
        return default()
    values = os.environ if env is None else env
    override = values.get(variable)
    if override or (accept_empty and override is not None):
        return Path(override).expanduser() if expand_override else override
    if testing:
        raise RuntimeError(f"Refusing personal path fallback under pytest; set {variable} "
                           "to an isolated test path")
    return default()


def codex_app_home() -> Path:
    """The desktop Codex home, with a test-only isolation override."""
    return Path(home_path("SUBFLEET_CODEX_APP_HOME", lambda: Path.home() / ".codex",
                          test_only=True))


def desktop_login_path() -> Path:
    """Claude's desktop login, with a test-only isolation override."""
    return Path(home_path("SUBFLEET_DESKTOP_LOGIN", lambda: Path.home() / ".claude.json",
                          test_only=True))


def provider_home() -> Path:
    """Base of legacy provider homes, with a test-only isolation override."""
    return Path(home_path("SUBFLEET_PROVIDER_HOME", Path.home, test_only=True))
