"""Resolve personal paths without letting pytest fall back to live stores."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

PYTEST_MARKER = "SUBFLEET_UNDER_PYTEST"
# Session descendants keep isolation only while this exact sentinel lives.
PYTEST_SESSION = "SUBFLEET_PYTEST_SESSION"
PYTEST_SESSION_ID = "SUBFLEET_PYTEST_SESSION_ID"
PYTEST_SENTINEL = ".pytest-session"
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
    "SUBFLEET_HOST_HOME": "host-home",
    "SUBFLEET_HOST_CONFIG": "host-config",
    "SUBFLEET_RESET_INHIBIT": "legacy/no-reset.json",
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
    if PYTEST_MARKER in os.environ:
        session = os.environ.get(PYTEST_SESSION)
        # Bare markers remain useful for standalone test harnesses. Configured
        # sessions always supply a lifetime, including in captured child envs.
        # A copied PYTEST_CURRENT_TEST must not revive an expired session either.
        if session is None:
            return True
        identity = os.environ.get(PYTEST_SESSION_ID)
        if not identity:
            return False
        try:
            with (Path(session) / PYTEST_SENTINEL).open(encoding="ascii") as sentinel:
                found = os.fstat(sentinel.fileno())
                token = sentinel.read(65)
                return identity == f"{found.st_dev}:{found.st_ino}:{token}"
        except (OSError, UnicodeError, ValueError):
            return False
    return "PYTEST_CURRENT_TEST" in os.environ


def home_path(variable: str, default: Callable[[], str | Path], *,
              env: Mapping[str, str] | None = None, accept_empty: bool = False,
              test_only: bool = False, expand_override: bool = False) -> str | Path:
    """Test-only guard against implicit access to the person's home stores.

    On 2026-10-12 a test's monkeypatch.undo() undid the desktop-store fixture,
    and the mirror rewrote the person's live Claude session copies. Session
    environment isolation is the first defense; the session marker covers
    configure, collection and teardown, when PYTEST_CURRENT_TEST is absent.
    A live session marker or a standalone test marker makes a missing override
    refuse the fallback before access. Cleanup expires captured session markers,
    including copied PYTEST_CURRENT_TEST values, by deleting their sentinel.
    Recreating a store directory cannot recreate that sentinel's identity.
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


def protected_provider_homes() -> list[Path]:
    """Protect test-provided HOME as well as isolated reader paths.

    Reader overrides must remain mandatory under pytest. They redirect access,
    but must not hide the provider homes checked by workspace admission. Only
    a HOME distinct from the account's home may add those implicit paths during
    tests; compare the home directories themselves without inspecting stores.
    """
    isolated = [Path(home_path("CLAUDE_CONFIG_DIR", lambda: Path.home() / ".claude",
                               test_only=True)), codex_app_home()]
    if not under_pytest():
        return isolated
    import pwd

    home = Path.home()
    personal = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if home == personal:
        return isolated
    try:
        if home.samefile(personal):
            return isolated
    except OSError:
        # An inaccessible synthetic home must still reach the spelling refusal.
        pass
    return [home / ".claude", home / ".codex", *isolated]


def desktop_login_path() -> Path:
    """Claude's desktop login, with a test-only isolation override."""
    return Path(home_path("SUBFLEET_DESKTOP_LOGIN", lambda: Path.home() / ".claude.json",
                          test_only=True))


def provider_home() -> Path:
    """Base of legacy provider homes, with a test-only isolation override."""
    return Path(home_path("SUBFLEET_PROVIDER_HOME", Path.home, test_only=True))
