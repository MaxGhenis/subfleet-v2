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
_LAUNCH_EVENTS = frozenset({"subprocess.Popen", "os.posix_spawn", "os.exec", "os.spawn"}) | {"os.system"}
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


def _fold_name(name: str) -> str:
    """Compare a bare name without resolving it against a mutable PATH."""
    folded = unicodedata.normalize("NFD", name).casefold()
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


def _launch_targets(event: str, details: tuple, *, _find_program=_find_program,
                    _fold_name=_fold_name) -> list[str | None]:
    # A later audit hook can mutate PATH after this hook returns. Never approve
    # the bare reader name; intentional test executables must name their path.
    if event == "os.system":
        return []  # Its shell is checked separately, without parsing shell text.
    program = details[1 if event == "os.spawn" else 0]
    name = os.fsdecode(program)
    if not os.path.dirname(name) and _fold_name(name) == "security":
        return ["security"]
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


# Wrapper children exec outside Python's audit machinery. Compare PATH strings
# before inspecting any executable, so a reset path cannot escape the recorder.
_DELEGATING_PROGRAMS = frozenset({
    "sh", "bash", "zsh", "dash", "env", "xargs", "nice", "nohup", "taskpolicy",
})
_SYSTEM_BINARY_DIRECTORIES = (
    "/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin", "/usr/gnu/bin",
)


def _env_search_path(argv: list[str], path: str | None) -> str | None:
    """env can clear, unset, assign or explicitly select the child's PATH.

    Parse options only before its utility; that utility's arguments are opaque.
    env -S splits another option/assignment prefix, without running a shell.
    """
    import shlex
    args = list(argv[1:])
    options = True
    while args:
        arg = args.pop(0)
        if options and arg == "--":
            options = False
        elif options and arg in ("-i", "--ignore-environment", "-"):
            path = None
        elif options and arg in ("-u", "--unset"):
            if args and args.pop(0) == "PATH":
                path = None
        elif options and (arg.startswith("--unset=") or arg.startswith("-u")):
            if arg.removeprefix("--unset=").removeprefix("-u") == "PATH":
                path = None
        elif options and arg in ("-P", "--path"):
            path = args.pop(0) if args else None
        elif options and arg.startswith("--path="):
            path = arg.partition("=")[2]
        elif options and (arg in ("-S", "--split-string")
                          or arg.startswith(("-S", "--split-string="))):
            if arg in ("-S", "--split-string"):
                split = args.pop(0) if args else ""
            else:
                split = arg[2:] if arg.startswith("-S") else arg.partition("=")[2]
            args[:0] = shlex.split(split)
        elif options and arg in ("-C", "--chdir"):
            if args:
                args.pop(0)
        elif options and arg.startswith("-"):
            # Combined env short flags, including -iv, retain -i's semantics.
            if not arg.startswith("--") and "i" in arg[1:]:
                path = None
        elif "=" in arg:
            if arg.startswith("PATH="):
                path = arg.partition("=")[2]
        else:
            break
    return path


def _unsafe_delegation(event: str, details: tuple, recorders, *,
                       system_dirs=_SYSTEM_BINARY_DIRECTORIES,
                       _fold_name=_fold_name, _env_path=_env_search_path, _family=_DELEGATING_PROGRAMS) -> bool:
    if event == "os.system":
        name, argv, env, cwd = "sh", (), None, None
    else:
        offset = 1 if event == "os.spawn" else 0
        name, argv = details[offset:offset + 2]
        env = details[3] if event == "subprocess.Popen" else details[offset + 2]
        cwd = details[2] if event == "subprocess.Popen" else None
        name = _fold_name(os.path.basename(os.fsdecode(name)))
    if name not in _family:
        return False
    # os.get_exec_path handles both string and bytes environments, and supplies
    # the OS default when PATH is absent. env's resets use that default too.
    path = os.pathsep.join(os.get_exec_path(env))
    if name == "env":
        path = _env_path([os.fsdecode(arg) for arg in argv], path)
    path = os.defpath if path is None else path

    def spelling(directory):
        directory = _fold_name(os.path.normpath(directory))
        return directory.removeprefix("/system/volumes/data") or "/"

    system = {spelling(directory) for directory in system_dirs}
    # Only wrappers need recorder identities. Direct Git/Python launches must
    # not reopen every previous nested session's sentinel.
    safe = {spelling(directory) for directory in (recorders() if callable(recorders) else recorders)}
    base = os.getcwd() if cwd is None else os.fsdecode(cwd)
    for directory in path.split(os.pathsep):
        directory = spelling(os.path.join(base, directory))
        if directory in system:
            return True
        if directory in safe:
            return False
    return False


def _guard_active(pins: list[tuple[str, str]], *, under_pytest=under_pytest,
                  session_alive=session_alive) -> bool:
    return under_pytest() or any(session_alive(*pin) for pin in list(pins))


def _exec_guard(pins: list[tuple[str, str]], *, system_dirs=_SYSTEM_BINARY_DIRECTORIES,
                recorder_dirs: tuple[str, ...] = ()):
    native = _native_keychain_binary()

    # Every helper is bound by default argument when defined, so replacing this
    # module's attributes later cannot change the installed hook. Only the
    # stand-in registry and live session pins are read at launch; neither can
    # remove the native rule.
    def guard(event, details, *, _events=_LAUNCH_EVENTS, _targets=_launch_targets,
              _matches=_is_keychain_binary, _active=_guard_active, _native=native,
              _unsafe=_unsafe_delegation, _alive=session_alive,
              _system=system_dirs, _recorders=recorder_dirs):
        if event not in _events:
            return
        try:
            def recorders():
                return _recorders + tuple(os.path.join(base, "keychain-bin")
                                           for base, token in list(pins) if _alive(base, token))
            refused = _active(pins) and _unsafe(event, details, recorders, system_dirs=_system)
            starts = [] if refused else [target for target in _targets(event, details) if target is not None]
        except (OSError, TypeError, ValueError):
            return  # Arguments the launch itself rejects before starting anything.
        if (refused or any(target == "security" or _matches(target, _native)
                           for target in starts)) and _active(pins):
            raise KeychainExecRefused(_REFUSAL, code=7, fix=_REFUSAL_FIX, reason="keychain-test-guard")

    return guard


def install_keychain_exec_guard() -> bool:
    """Refuse, under pytest, any launch that would start the login keychain's reader.

    Bare `security` names (NFD/casefold/NFD) are refused independently of PATH,
    including PATH
    changes by later audit hooks. Explicit paths retain folded-path and identity
    checks. The test session puts a recording `security` first on PATH. Shells
    and env-family wrappers are refused when their child PATH reaches a system
    binary directory before a live session recorder, including absent PATH and
    env resets. This check compares strings without probing system directories.
    Deliberate absolute reader paths inside shell strings are outside C-20.6
    (D-ST1); production readers use argv.

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
    if not os.path.dirname(security_bin) and _fold_name(security_bin) == "security":
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
        raise AdapterError(_REFUSAL, code=7, fix=_REFUSAL_FIX, reason="keychain-test-guard")
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
                               fix=f"set {credential.ref} in the daemon environment to the lane OAuth token",
                               reason="credential-env")
        return {"CLAUDE_CODE_OAUTH_TOKEN": token}
    if credential.kind != "keychain-token" or credential.provider != "claude":
        raise AdapterError("unsupported credential kind for provider", code=7, reason="credential-kind")
    execute = runner or subprocess.run
    try:
        result = execute(keychain_command(credential.ref, runner=execute),
                         capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        raise AdapterError("could not resolve lane keychain credential", code=7,
                           fix="unlock the configured credential store and verify the lane reference with agent-secret",
                           reason="credential-keychain") from None
    if result.returncode or not result.stdout.strip():
        raise AdapterError("could not resolve lane keychain credential", code=7,
                           fix="unlock the configured credential store and verify the lane reference with agent-secret",
                           reason="credential-keychain")
    return {"CLAUDE_CODE_OAUTH_TOKEN": result.stdout.rstrip("\r\n")}


resolve = resolve_credential


# Python children of a test session inherit its markers; guard them from import.
if PYTEST_MARKER in os.environ or "PYTEST_CURRENT_TEST" in os.environ:
    install_keychain_exec_guard()
