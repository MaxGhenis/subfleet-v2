"""Resolve personal paths without letting pytest fall back to live stores."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

PYTEST_MARKER = "SUBFLEET_UNDER_PYTEST"
# Session descendants keep isolation only while this exact sentinel lives.
PYTEST_SESSION = "SUBFLEET_PYTEST_SESSION"
PYTEST_SESSION_ID = "SUBFLEET_PYTEST_SESSION_ID"
PYTEST_SENTINEL = ".pytest-session"
# An inherited helper may query the person's dedicated keychain. Tests must
# explicitly install their own stand-in instead of inheriting this executable.
# An inherited development build would widen the person boundary (D-21) for
# every test service, and make each one ask where the person's state root is;
# a test that means a development build names its own.
SAFE_ENV_REMOVALS = ("CLAUDE_LANE_AGENT_SECRET", "SUBFLEET_DEV_APP_EXECUTABLE")

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
    "SUBFLEET_V1_BIN": "legacy/bin/subfleet",
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


def under_pytest(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this process, or a child given `environ`, is in a test session.

    This process is when either rule says so: the process rule
    (`pytest_process`), whatever os.environ holds, or the environment rule
    below. A child given `environ` has no pytest loaded, so the environment
    rule alone decides for it (D-ST3).
    """
    if environ is None:
        return pytest_process() or _session_environment(os.environ)
    return _session_environment(environ)


def _session_environment(values: Mapping[str, str]) -> bool:
    """The environment rule: a live session marker, a bare marker, or
    PYTEST_CURRENT_TEST."""
    if PYTEST_MARKER in values:
        session = values.get(PYTEST_SESSION)
        # Bare markers remain useful for standalone test harnesses. Configured
        # sessions always supply a lifetime, including in captured child envs.
        # A copied PYTEST_CURRENT_TEST must not revive an expired session either.
        if session is None:
            return True
        return session_alive(session, values.get(PYTEST_SESSION_ID))
    return "PYTEST_CURRENT_TEST" in values


def pytest_loaded() -> bool:
    """Whether pytest runs in this process, read from its modules, never os.environ.

    pytest's entry point imports `_pytest` and `pytest` before it loads any
    conftest, plugin or test module, and they stay for the life of the
    process: whichever conftest ran, however a test replaced os.environ, and
    across a reload of this module. No module under subfleet/ imports pytest
    (tests/unit/test_live_store_guard_st3.py checks), so a production process
    holds them only if something imports pytest into it, and then it refuses
    more, never less (D-ST3).
    """
    return "_pytest" in sys.modules or "pytest" in sys.modules


def _process_rule_waived() -> bool:
    """D-ST3's one seam. Only `tests.conftest.waive_process_rule` replaces it."""
    return False


def pytest_process() -> bool:
    """The process rule: pytest is loaded here.

    A test of production behaviour may waive it (the `production_process`
    fixture), and the waiver holds only while HOME is not the account's own,
    read again at every call: with the account's HOME by any spelling, or
    with none, which `Path.home()` reads as the account's, the rule stands.
    """
    if not pytest_loaded():
        return False
    if not _process_rule_waived():
        return True
    try:
        return is_account_home(Path.home())
    except (OSError, RuntimeError, KeyError, ValueError):
        return True


def session_alive(session: str | os.PathLike, identity: str | None) -> bool:
    """Whether this session's own sentinel still exists. Cleanup deletes it, and
    a recreated file never matches: the identity includes its inode and token."""
    if not identity:
        return False
    try:
        with (Path(session) / PYTEST_SENTINEL).open(encoding="ascii") as sentinel:
            found = os.fstat(sentinel.fileno())
            token = sentinel.read(65)
            return identity == f"{found.st_dev}:{found.st_ino}:{token}"
    except (OSError, UnicodeError, ValueError):
        return False


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
    A pytest process refuses whatever os.environ holds (D-ST3), and refuses an
    override beginning `~` that would expand to the account's own home.
    """
    testing = under_pytest()
    if test_only and not testing:
        return default()
    values = os.environ if env is None else env
    override = values.get(variable)
    if override or (accept_empty and override is not None):
        if testing and _names_account_home(override):
            raise RuntimeError(f"Refusing personal path fallback under pytest; {variable} begins with ~, "
                               "which expands to the account's home here; set it to an isolated test path")
        return Path(override).expanduser() if expand_override else override
    if testing:
        raise RuntimeError(f"Refusing personal path fallback under pytest; set {variable} "
                           "to an isolated test path")
    return default()


def _names_account_home(value: str) -> bool:
    """Whether `value`'s leading `~` or `~name` expands to the account's home.

    Expansion reads HOME and, without one, pwd: the account's home. Only the
    home a tilde names is compared, never a store beneath it.
    """
    if not isinstance(value, str) or not value.startswith("~"):
        return False
    head = value.split(os.sep, 1)[0]
    expanded = os.path.expanduser(head)
    return expanded != head and is_account_home(Path(expanded))


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
    home = Path.home()
    if is_account_home(home):
        return isolated
    return [home / ".claude", home / ".codex", *isolated]


def is_account_home(home: Path) -> bool:
    """Whether `home` is the account's own home directory, by any spelling.

    Equal spellings decide without a stat, lexically normalized ones too.
    Otherwise the home directories themselves are compared, never a store
    beneath them: as `home` reads, and as `Path.resolve` would build on it.
    The second catches `home/missing/..`, which no stat can follow but which
    resolves to the account's home (D-ST3).
    """
    import pwd

    personal = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if home == personal or Path(os.path.abspath(home)) == personal:
        return True
    return _same_directory(home, personal) or _same_directory(Path(os.path.realpath(home)), personal)


def _same_directory(path: Path, other: Path) -> bool:
    try:
        return path.samefile(other)
    except (OSError, ValueError):
        # An inaccessible synthetic home must still reach the spelling refusal.
        return False


def production_state_root() -> Path:
    """D-21: the person's production state root, `~/.subfleet`, resolved as
    release/217 resolves it.

    Under pytest only a test's own HOME may name it: the D-21 tests set HOME and
    expect `HOME/.subfleet` to be the production root. The provider-home seam is
    not this root, because pytest points it at the session's own directory, so
    every test root would count as a development root. A HOME that is still the
    account's own would resolve the person's live store, so it is refused before
    anything beneath it is touched (D-ST1).
    """
    home = Path.home()
    if under_pytest() and is_account_home(home):
        raise RuntimeError("Refusing to resolve the personal state root ~/.subfleet under pytest; "
                           "set HOME to an isolated test home")
    return (home / ".subfleet").resolve()


def desktop_login_path() -> Path:
    """Claude's desktop login, with a test-only isolation override."""
    return Path(home_path("SUBFLEET_DESKTOP_LOGIN", lambda: Path.home() / ".claude.json",
                          test_only=True))


def provider_home() -> Path:
    """Base of legacy provider homes, with a test-only isolation override."""
    return Path(home_path("SUBFLEET_PROVIDER_HOME", Path.home, test_only=True))
