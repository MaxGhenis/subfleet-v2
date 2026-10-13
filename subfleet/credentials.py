"""Resolve credential references directly into launch environment additions."""

from __future__ import annotations

import os
import subprocess
import sys
import unicodedata
from pathlib import Path

from .adapters.base import AdapterError
from .contracts import Credential
from .home_paths import (
    PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID, home_path, session_alive, under_pytest,
)


_NATIVE_RUNNER = subprocess.run
# macOS's login-keychain reader. Under pytest no process may start it, whichever
# runner, wrapper or spelling reaches it.
KEYCHAIN_BINARY = "/usr/bin/security"
# Recording stand-ins a test registers here are refused as the real binary would
# be, so a probe can show the refusal without ever reaching the keychain.
_STANDIN_KEYCHAIN_BINARIES: tuple[str, ...] = ()
# The audit events that start a program. `os.spawn` is native only on Windows;
# elsewhere os.spawn* forks and the child's exec is audited instead.
_LAUNCH_EVENTS = frozenset({"subprocess.Popen", "os.posix_spawn", "os.exec", "os.spawn"})
# The installed guard's session pins live on `sys`, so the guard is per process.
_EXEC_GUARD = "_subfleet_keychain_exec_guard"
_REFUSAL = "refusing personal keychain read under pytest"
_REFUSAL_FIX = "provide an isolated credential runner or executable"


class KeychainExecRefused(AdapterError):
    """The exec guard's refusal. Readers report it exactly as the early refusal."""


def _fold(path: str) -> str:
    """A path as the volume compares it: symlinks and dot segments resolved, and
    case and Unicode folded. APFS starts `/usr/bin/ſecurity` as the same file."""
    folded = unicodedata.normalize("NFD", os.path.realpath(path)).casefold()
    return unicodedata.normalize("NFD", folded)


def _identity(path) -> tuple[int, int] | None:
    try:
        found = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    return found.st_dev, found.st_ino


def _native_keychain_binary() -> tuple[str, tuple[int, int] | None]:
    """The real reader by folded spelling and file identity. The identity catches
    aliases no spelling reveals, such as `/.vol/<device>/<inode>`."""
    folded = unicodedata.normalize("NFD", KEYCHAIN_BINARY).casefold()
    return unicodedata.normalize("NFD", folded), _identity(KEYCHAIN_BINARY)


def _is_keychain_binary(target: str, native: tuple[str, tuple[int, int] | None], *,
                        _fold=_fold, _identity=_identity) -> bool:
    """Whether `target` is the native reader, or a registered stand-in, by any path."""
    names, identities = {native[0]}, {native[1]}
    try:
        for standin in _STANDIN_KEYCHAIN_BINARIES:
            names.add(_fold(os.fspath(standin)))
            identities.add(_identity(standin))
    except (OSError, TypeError, ValueError):
        pass  # A malformed stand-in never weakens the native rule.
    identities.discard(None)
    try:
        if _fold(target) in names:
            return True
    except (OSError, ValueError):
        pass
    found = _identity(target)
    return found is not None and found in identities


def _find_program(name, search: list[str] | None, cwd=None) -> str | None:
    """The file a launch of `name` starts, found as the launch finds it.

    A name with a directory, or a launch that does not search, is taken relative
    to the child's working directory. Otherwise, as `_posixsubprocess` and
    `posix_spawnp` do, the first executable file on the search path runs.
    """
    name = os.fsdecode(name)
    base = "" if cwd is None else os.fsdecode(cwd)
    if search is None or os.path.dirname(name):
        return os.path.join(base, name)
    for directory in search:
        candidate = os.path.join(base, directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _launch_targets(event: str, details: tuple, *, _find_program=_find_program) -> list[str | None]:
    if event == "subprocess.Popen":
        executable, _argv, cwd, env = details
        return [_find_program(executable, os.get_exec_path(env), cwd)]
    if event == "os.posix_spawn":
        # One event serves both: posix_spawnp searches the caller's PATH, and
        # posix_spawn takes the path as given.
        path = details[0]
        return [_find_program(path, os.get_exec_path()), _find_program(path, None)]
    if event == "os.exec":
        return [_find_program(details[0], None)]
    if event == "os.spawn":
        return [_find_program(details[1], None)]
    return []


def _guard_active(pins: list[tuple[str, str]], *, under_pytest=under_pytest,
                  session_alive=session_alive) -> bool:
    return under_pytest() or any(session_alive(*pin) for pin in list(pins))


def _exec_guard(pins: list[tuple[str, str]]):
    native = _native_keychain_binary()

    # Every helper is bound by default argument when defined, so replacing this
    # module's attributes later cannot change the installed hook. Only the
    # stand-in registry is read when a launch is audited; it can only add.
    def guard(event, details, *, _events=_LAUNCH_EVENTS, _targets=_launch_targets,
              _matches=_is_keychain_binary, _active=_guard_active, _native=native):
        if event not in _events:
            return
        try:
            starts = [target for target in _targets(event, details) if target is not None]
        except (OSError, TypeError, ValueError):
            return  # Arguments the launch itself rejects before starting anything.
        if any(_matches(target, _native) for target in starts) and _active(pins):
            raise KeychainExecRefused(_REFUSAL, code=7, fix=_REFUSAL_FIX)

    return guard


def install_keychain_exec_guard() -> bool:
    """Refuse, under pytest, any launch that would start the login keychain's reader.

    No runner identity can tell a stub from a wrapper around the real
    `subprocess.run`, so this judges the file each launch would start: through
    the launch's PATH and working directory, then by folded path and file
    identity. A stub never launches, so it still works. An audit hook lasts as
    long as the process, and fixture undo cannot remove it. The hook pins the
    session live at installation, so a test that strips the markers from this
    process stays refused until cleanup deletes that session's sentinel. A
    refusal is the reader's AdapterError (code 7), never a hang. Installing again
    adds no second hook. Outside pytest nothing installs it, and once installed
    it is inert without a live session.
    """
    pins = getattr(sys, _EXEC_GUARD, None)
    installed = pins is None
    if installed:
        pins = []
        sys.addaudithook(_exec_guard(pins))
        setattr(sys, _EXEC_GUARD, pins)
    session = (os.environ.get(PYTEST_SESSION), os.environ.get(PYTEST_SESSION_ID))
    if all(session) and session not in pins and session_alive(*session):
        pins.append(session)
    return installed


def _starts_keychain_binary(security_bin: str) -> bool:
    if security_bin == "security":
        return True
    target = _find_program(security_bin, os.get_exec_path())
    return target is not None and _is_keychain_binary(target, _native_keychain_binary())


def keychain_command(reference: str, security_bin: str = "security", *,
                     runner=None) -> list[str]:
    """Keep v1's dedicated agent keychain when its helper is installed.

    A failed helper read must not fall through to a same-named, stale credential
    in the login keychain. Hosts without the helper retain the native backend.
    """
    # Provider-owned desktop/home OAuth blobs stay in their native keychain.
    # Only the explicit per-lane setup-token namespace belongs to agent-secret.
    if reference.startswith("claude-quota-"):
        helper = os.environ.get("CLAUDE_LANE_AGENT_SECRET")
        if not helper:
            default = Path(home_path("SUBFLEET_AGENT_SECRET",
                                     lambda: Path.home() / "bin" / "agent-secret",
                                     test_only=True))
            helper = str(default) if default.is_file() else None
        if helper:
            return [helper, "get", reference]
    # A reference or fake HOME does not isolate macOS's login keychain. Fixture
    # undo cannot remove this guard. Only a stand-in runner or executable
    # may issue a credential command while either marker is present. This is
    # the early refusal that explains itself; the exec guard below is the rule.
    if (under_pytest() and (runner is None or runner is _NATIVE_RUNNER)
            and _starts_keychain_binary(security_bin)):
        raise AdapterError(_REFUSAL, code=7, fix=_REFUSAL_FIX)
    return [security_bin, "find-generic-password", "-s", reference, "-w"]


def resolve_credential(credential: Credential, *, runner=None) -> dict[str, str]:
    """C-10.5: credential values appear only in the returned child environment."""
    if credential.kind == "home" and credential.provider in ("codex", "claude"):
        key = "CODEX_HOME" if credential.provider == "codex" else "CLAUDE_CONFIG_DIR"
        return {key: str(Path(credential.ref).expanduser().resolve())}
    if credential.kind == "env" and credential.provider == "claude":
        token = os.environ.get(credential.ref)
        if not token or not token.strip():
            raise AdapterError("could not resolve lane environment credential", code=7,
                               fix=f"set {credential.ref} in the daemon environment to the lane OAuth token")
        return {"CLAUDE_CODE_OAUTH_TOKEN": token}
    if credential.kind != "keychain-token" or credential.provider != "claude":
        raise AdapterError("unsupported credential kind for provider", code=7)
    execute = runner or subprocess.run
    try:
        result = execute(keychain_command(credential.ref, runner=execute),
                         capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        raise AdapterError("could not resolve lane keychain credential", code=7,
                           fix="unlock the configured credential store and verify the lane reference with agent-secret") from None
    if result.returncode or not result.stdout.strip():
        raise AdapterError("could not resolve lane keychain credential", code=7,
                           fix="unlock the configured credential store and verify the lane reference with agent-secret")
    return {"CLAUDE_CODE_OAUTH_TOKEN": result.stdout.rstrip("\r\n")}


resolve = resolve_credential


# Python children of a test session inherit its markers; guard them from import.
if PYTEST_MARKER in os.environ or "PYTEST_CURRENT_TEST" in os.environ:
    install_keychain_exec_guard()
