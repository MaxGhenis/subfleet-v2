"""Resolve personal paths without letting pytest fall back to live stores."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path


def home_path(variable: str, default: Callable[[], str | Path], *,
              env: Mapping[str, str] | None = None, accept_empty: bool = False,
              test_only: bool = False, expand_override: bool = False) -> str | Path:
    """Test-only guard against implicit access to the person's home stores.

    On 2026-10-12 a test's monkeypatch.undo() undid the desktop-store fixture,
    and the mirror rewrote the person's live Claude session copies. Session
    environment isolation is the first defense; while PYTEST_CURRENT_TEST is
    set, a missing override refuses the fallback here, before any file access.
    test_only supplies an isolation seam for defaults that historically had no
    environment override, preserving their behavior outside pytest. Optional
    expansion applies only to overrides, never to an already constructed default;
    raw overrides let other callers retain their expansion and empty handling.
    """
    testing = "PYTEST_CURRENT_TEST" in os.environ
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
