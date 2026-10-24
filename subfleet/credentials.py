"""Resolve credential references directly into launch environment additions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import os
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

from .launch_env import (
    ROSTER, STORE, build_environment, harness_name, inherited_name, lane_reference, refused_reference,
)
from .adapters.base import AdapterError
from .contracts import Credential, Lane
from .home_paths import (
    PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID, checked_override, home_path, session_alive, under_pytest,
)


def env_references(lanes: Iterable[Lane | Mapping[str, Any]]) -> frozenset[str]:
    """D-ENV1: the daemon variables env-kind lanes name as their credential, from
    lanes or roster rows.

    The daemon alone holds them. Each lane receives only its own, resolved into
    its credential; every launch withholds all of them by name. A refused
    variable (`launch_env.refused_reference`) is no lane's credential: its lane
    is held instead, and the daemon is not handed it by name.
    """
    variables = (found[1] for found in map(lane_reference, lanes) if found)
    return frozenset(ref for ref in variables if not refused_reference(ref))


def withheld_references(lanes: Iterable[Lane | Mapping[str, Any]]) -> frozenset[str]:
    """D-ENV1: the variables every launch withholds by name for the lanes.

    Each lane's credential variable, and a held lane's harness hook: production
    never reads one, so withholding it costs nothing and keeps whatever was put
    there from every other lane. A held lane's listed or session name is not
    withheld: every launch needs HOME or PATH, and a test's child its session.
    """
    variables = [found[1] for found in map(lane_reference, lanes) if found]
    return frozenset(ref for ref in variables if not refused_reference(ref) or harness_name(ref))


def reference_repair(stored: Lane | Mapping[str, Any], row: Mapping[str, Any]) -> str | None:
    """Review r4, P2-1: the variable a lanes.json row gives a stored lane, or None.

    Only a stored env-kind lane whose variable is refused takes one, and only
    from a row for the same lane id, provider and account whose own env-kind
    variable is not refused. Such a lane never held a credential this release
    could resolve, so this binds it for the first time: nothing else changes
    (C-1.3).
    """
    found, offered = lane_reference(stored), lane_reference(row)
    if not found or not offered or found[0] != offered[0] or not refused_reference(found[1]):
        return None
    provider = stored.get("provider") if isinstance(stored, Mapping) else stored.provider
    account = stored.get("account_key") if isinstance(stored, Mapping) else stored.account_key
    if (row.get("provider"), row.get("account_key")) != (provider, account) or refused_reference(offered[1]):
        return None
    return offered[1]


def startup_lanes(roster: Iterable[Mapping[str, Any]],
                  stored: Iterable[Lane | Mapping[str, Any]]) -> list[tuple[Any, str, str]]:
    """D-ENV1: each env-kind lane's (lane id, variable, source) as startup will
    hold them: every lanes.json row, then each stored lane a row does not repair."""
    roster = [row for row in roster if isinstance(row, Mapping)]
    lanes = [(*found, ROSTER) for found in map(lane_reference, roster) if found]
    for lane in stored:
        found = lane_reference(lane)
        if found and not any(reference_repair(lane, row) for row in roster):
            lanes.append((*found, STORE))
    return lanes


_NATIVE_RUNNER = subprocess.run


def native_runner(runner) -> bool:
    """D-ENV1: whether a reader starts its command itself, with no runner supplied
    or with `subprocess.run` passed as one. Only then is it handed the allowlisted
    environment; any other runner (D-ST1's stand-ins) builds its own."""
    return runner is None or runner is _NATIVE_RUNNER


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
        # Popen searches PATH only for a bare name: a path never reads env's PATH.
        return [_find_program(executable, None if os.path.dirname(name) else os.get_exec_path(env), cwd)]
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


# The shells, and the programs that run a command named by their operands through
# PATH. Their children exec outside Python's audit machinery. Compare PATH strings
# before inspecting any executable, so a reset path cannot escape the recorder.
# C-20.6 names exactly these; other PATH-searching programs are out of its scope.
_DELEGATING_PROGRAMS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh",
    "env", "xargs", "nice", "nohup", "taskpolicy", "time", "script", "caffeinate", "lockf", "arch",
    "stdbuf",
})
# What a listed wrapper other than a shell, env or arch runs: the first operand
# after its options, read with the getopt string its macOS binary holds, and
# after `files` operands (script's and lockf's file). It passes on the
# environment it was given.
_WRAPPER_OPTIONS = {
    "xargs": ("0E:I:J:L:n:oP:pR:S:s:rtx", 0), "nice": ("n:", 0), "nohup": ("", 0),
    "taskpolicy": ("xXbBd:g:c:t:l:p:asS:m:j:P:", 0), "time": ("ahlo:p", 0),
    "script": ("adeFkpqrT:t:", 1), "caffeinate": ("mdhisut:w:", 0), "lockf": ("knst:w", 1),
    "stdbuf": ("e:i:o:", 0),
}
# A listed wrapper whose command is another is judged again, as the first runs
# it. A chain of more listed wrappers than this is refused rather than followed.
_WRAPPER_DEPTH = 8
_SYSTEM_BINARY_DIRECTORIES = (
    "/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin", "/usr/gnu/bin",
)


# macOS env's getopt string is "-0C:iP:S:u:v". Its "-" option clears like -i, so
# a lone "-", a "-" inside a group and the "-" that starts any "--name" do too.
# GNU env's short options are a subset.
_ENV_FLAGS = "-0iv"
_ENV_VALUE_OPTIONS = "CPSu"
# env -S splits on these. It also substitutes `${NAME}`, reads `\` escapes (`\_`
# is a space, `\c` ends the string) and stops at `#`; those are not emulated.
_ENV_SPLIT_SPACES = " \t\n\v\f\r"
_ENV_SPLIT_REFUSED = "$\\#"


def _env_split(string: str) -> list[str]:
    """env -S's words, for a string without substitutions, escapes or comments."""
    import shlex
    if any(character in string for character in _ENV_SPLIT_REFUSED):
        raise ValueError("env -S string with a substitution, escape or comment")
    lexer = shlex.shlex(string, posix=True)
    lexer.whitespace, lexer.whitespace_split, lexer.commenters = _ENV_SPLIT_SPACES, True, ""
    return list(lexer)


def _env_child(argv: list[str], path: str | None, *, variable: str = "PATH", _split=_env_split):
    """What env does before it runs its utility: (the child's `variable`, -P path,
    -C directory, the utility's argv).

    Options are walked as env's getopt walks them: grouped or not, a value
    attached or in the next argument, and -S's words in place of the string.
    NAME=value assignments follow, until the utility, whose own arguments env
    passes on. None is an absent variable. An option env does not have, or a
    missing value, raises: env would not run, and the check refuses rather
    than guess.
    """
    args, altpath, chdir = list(argv[1:]), None, None
    while args and args[0].startswith("-"):
        arg = args.pop(0)
        if arg == "--":
            break
        group = arg[1:] or "-"
        for index, flag in enumerate(group):
            if flag in _ENV_FLAGS:
                if flag in "-i":
                    path = None
                continue
            if flag not in _ENV_VALUE_OPTIONS:
                raise ValueError(f"env has no option -{flag}")
            value = group[index + 1:] or (args.pop(0) if args else None)
            if value is None:
                raise ValueError(f"env -{flag} needs a value")
            if flag == "u" and value == variable:
                path = None
            elif flag == "P":
                altpath = value
            elif flag == "C":
                chdir = value
            elif flag == "S":
                args[:0] = _split(value)
            break
    while args and "=" in args[0]:
        name, _, value = args.pop(0).partition("=")
        if name == variable:
            path = value
    return path, altpath, chdir, args


def _arch_child(argv: list[str], path: str | None, *, variable: str = "PATH"):
    """What arch gives its program: (its `variable`, the program's argv). -c
    clears, -d unsets and -e sets, in order.

    arch reads every other "-name" before the program as an architecture.
    """
    args = list(argv[1:])
    while args and args[0].startswith("-"):
        arg = args.pop(0)
        if arg == "-c":
            path = None
        elif arg in ("-arch", "-d", "-e"):
            if not args:
                raise ValueError(f"arch {arg} needs a value")
            value = args.pop(0)
            if arg == "-d" and value == variable:
                path = None
            elif arg == "-e" and value.partition("=")[0] == variable:
                if "=" not in value:
                    raise ValueError("arch -e needs NAME=value")
                path = value.partition("=")[2]
    return path, args


def _delegated_searches(name: str, argv: list[str], path: str | None, *,
                        _env=_env_child, _arch=_arch_child):
    """The PATHs a wrapper's command is found through, and the directory it runs in.

    A wrapper searches the PATH it was given and passes it on. env finds its
    utility through -P's path, if given, and its child searches the PATH env
    leaves; -C moves the base of relative entries. arch searches its own PATH,
    and its program the one arch's options leave.
    """
    if name == "env":
        child, altpath, chdir, _command = _env(argv, path)
        return ([child] if altpath is None else [altpath, child]), chdir
    if name == "arch":
        return [path, _arch(argv, path)[0]], None
    return [path], None


def _getopt_operands(argv: list[str], options: str) -> list[str]:
    """argv's operands, as BSD getopt leaves them for the option string `options`.

    Options end at the first operand, a lone "-" or "--". An option the
    program does not have, or a missing value, raises: the program would not
    run, and the check refuses rather than guess at its command.
    """
    args = list(argv[1:])
    while args and args[0].startswith("-") and args[0] != "-":
        arg = args.pop(0)
        if arg == "--":
            break
        for end, flag in enumerate(arg[1:], 2):
            if flag == ":" or flag not in options:
                raise ValueError(f"no option -{flag}")
            if options[options.index(flag) + 1:][:1] == ":":
                if end == len(arg):  # The value is the next argument.
                    if not args:
                        raise ValueError(f"-{flag} needs a value")
                    args.pop(0)
                break
    return args


def _wrapper_command(name: str, argv: list[str], path: str | None, shell: str | None, *,
                     _env=_env_child, _arch=_arch_child, _options=_WRAPPER_OPTIONS,
                     _operands=_getopt_operands):
    """What a listed wrapper runs: (its command's argv, the PATH and SHELL it gets).

    An empty argv is a wrapper given no command, or a shell, whose operands are
    a script or a command string. nice reads an obsolete `-<number>` as
    `-n <number>`. script given no command runs `$SHELL -i`, by path and not
    through PATH, or `/bin/sh -i` when SHELL is unset.
    """
    if name == "env":
        child, _altpath, _chdir, command = _env(argv, path)
        return command, child, _env(argv, shell, variable="SHELL")[0]
    if name == "arch":
        child, command = _arch(argv, path)
        return command, child, _arch(argv, shell, variable="SHELL")[0]
    if name not in _options:
        return [], path, shell
    options, files = _options[name]
    args = list(argv)
    if name == "nice" and len(args) > 1 and args[1] != "--" and args[1][:1] == "-" \
            and args[1][1:2] in tuple("-0123456789"):
        args[1] = "-n" + args[1][1:]
    command = _operands(args, options)[files:]
    if name == "script" and not command and shell != "":
        command = [os.path.join(".", "/bin/sh" if shell is None else shell), "-i"]
    return command, path, shell


def _launch_paths(env, name: bytes = b"PATH") -> list[str | None]:
    """Each value of `name` the child receives, or None when it has none.

    Popen and os.posix_spawn encode every key, so a text and a bytes key of one
    name both reach the child, which reads one of its own choosing: judge each.
    """
    if env is None:
        return [os.environ.get(os.fsdecode(name))]
    found = [os.fsdecode(value) for key, value in env.items() if os.fsencode(key) == name]
    return found or [None]


def _directory_spelling(directory: str, *, _fold_name=_fold_name) -> str:
    """A directory as C-20.6 compares it, by spelling only.

    Leading slashes collapse (normpath keeps a POSIX `//`), dot segments resolve
    lexically, case and Unicode fold, and the Data volume's firmlink prefix
    goes. Symlinks and other aliases are not resolved.
    """
    if directory.startswith("/"):
        directory = "/" + directory.lstrip("/")
    folded = _fold_name(os.path.normpath(directory))
    data = "/system/volumes/data"
    if folded == data or folded.startswith(data + "/"):
        return folded[len(data):] or "/"
    return folded


def _searches_system_first(search: str | None, base: str, system, safe, *,
                           _spelling=_directory_spelling) -> bool:
    """Whether a search of `search` from `base` meets a system directory before a
    recorder. None is an absent PATH, which the wrapper's default replaces."""
    for entry in (os.defpath if search is None else search).split(os.pathsep):
        if entry.startswith("~"):
            raise ValueError("bash expands ~ in a PATH entry")
        directory = os.path.join(base, entry)
        if not os.path.isabs(directory):
            # Only a relative entry reads the working directory.
            directory = os.path.join(os.getcwd(), directory)
        directory = _spelling(directory)
        if directory in system:
            return True
        if directory in safe:
            return False
    return False


def _unsafe_delegation(event: str, details: tuple, recorders, *, native=None,
                       system_dirs=_SYSTEM_BINARY_DIRECTORIES, _fold_name=_fold_name,
                       _family=_DELEGATING_PROGRAMS, _paths=_launch_paths,
                       _searches=_delegated_searches, _first=_searches_system_first,
                       _spelling=_directory_spelling, _command=_wrapper_command,
                       _matches=_is_keychain_binary, _reader=_native_keychain_binary,
                       _depth=_WRAPPER_DEPTH) -> bool:
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
    try:
        system = {_spelling(directory) for directory in system_dirs}
        # Only wrappers need recorder identities. Direct Git/Python launches must
        # not reopen every previous nested session's sentinel.
        safe = {_spelling(directory) for directory in (recorders() if callable(recorders) else recorders)}
        reader = _reader() if native is None else native
        argv = [os.fsdecode(arg) for arg in argv]

        def reaches(name, argv, path, shell, start, depth):
            # A search the wrapper makes meets a system directory first, its
            # command is the reader by path, or its command is a listed wrapper
            # that does, run with the PATH, SHELL and directory this one leaves.
            if depth > _depth:
                raise ValueError("listed wrappers nested too deep to judge")
            searches, chdir = _searches(name, argv, path)
            base = start if chdir is None else os.path.join(start, chdir)
            if any(_first(search, base, system, safe) for search in searches):
                return True
            command, path, shell = _command(name, argv, path, shell)
            if not command:
                return False
            word = command[0]
            if os.path.dirname(word) and _matches(os.path.join(base, word), reader):
                return True
            inner = _fold_name(os.path.basename(word))
            return inner in _family and reaches(inner, command, path, shell, base, depth + 1)

        start = os.fsdecode(cwd) if cwd is not None else ""
        return any(reaches(name, argv, path, shell, start, 1)
                   for path in _paths(env) for shell in _paths(env, b"SHELL"))
    except Exception:
        # A wrapper launch whose check cannot finish is refused (C-20.6).
        return True


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
        if event not in _events or not _active(pins):
            return
        try:
            def recorders():
                return _recorders + tuple(os.path.join(base, "keychain-bin")
                                           for base, token in list(pins) if _alive(base, token))
            refused = (_unsafe(event, details, recorders, system_dirs=_system, native=_native)
                       or any(target == "security" or _matches(target, _native)
                              for target in _targets(event, details) if target is not None))
        except Exception:
            # A launch whose check cannot finish is refused (C-20.6). The launch
            # may reject the same arguments, but a later audit hook can change
            # them first, as it can change PATH.
            refused = True
        if refused:
            raise KeychainExecRefused(_REFUSAL, code=7, fix=_REFUSAL_FIX, reason="keychain-test-guard")

    return guard


def install_keychain_exec_guard() -> bool:
    """Refuse, under pytest, any launch that would start the login keychain's reader.

    Bare `security` names (NFD/casefold/NFD) are refused independently of PATH,
    including PATH changes by later audit hooks. Explicit paths retain
    folded-path and identity checks. A launch searches PATH only for a bare
    name, as Popen does. The test session puts a recording `security` first on
    PATH.

    It also refuses a launch of sh, bash, zsh, dash, ksh, csh, tcsh, env, xargs,
    nice, nohup, taskpolicy, time, script, caffeinate, lockf, arch or stdbuf
    whose child can search a system binary directory before a live session
    recorder. It judges every PATH key the child receives (an absent PATH is the
    wrapper's default), env's options as env's getopt reads them, with -P's
    path, the child's PATH and -C's directory, and arch's -c, -d and -e. It
    compares spellings without probing a directory: leading slashes collapsed,
    dot segments resolved, case and Unicode folded. A listed wrapper other than
    a shell is also judged by the command it runs, the first operand after its
    options as the wrapper reads them (after env's assignments and after
    script's and lockf's file; script given none runs `$SHELL -i`). That
    command, given by path, is refused if it is the reader or a registered
    stand-in; a command that is itself a listed wrapper is judged again with
    the PATH, SHELL and directory the outer one leaves, up to 8 listed wrappers
    deep. A `~` entry, an env -S string using `$`, `\\` or `#`, a deeper chain,
    an option a wrapper does not have, and any other launch whose check cannot
    finish are refused. Outside C-20.6 (D-ST1), requiring isolated inputs:
    other PATH-searching programs (`find -exec`, which nothing in this
    repository launches; `sudo`, `sandbox-exec`, `xcrun`, interpreters); the
    arguments xargs reads from its input; a shell's script operand; a wrapper
    run under another name; path aliases (a symlink to `/usr/bin`, or a
    symlink to the reader that a wrapper's bare command finds through PATH);
    PATH changes made inside the shell (a shell string, a login shell's
    profile, `BASH_ENV`, `ENV` or `ZDOTDIR`); deliberate shell strings naming
    the reader's absolute path; `DirEntry.stat`; and native file access
    through `sqlite3`. Production readers use argv.

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
        helper = checked_override("CLAUDE_LANE_AGENT_SECRET", os.environ.get("CLAUDE_LANE_AGENT_SECRET"))
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
        if inherited_name(credential.ref):
            # D-ENV1: every launch inherits an allowlisted name, so it cannot be one lane's.
            raise AdapterError("lane environment credential names a variable every launch inherits", code=7,
                               fix=f"store the lane OAuth token under a daemon variable other than {credential.ref}",
                               reason="credential-env-inherited")
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
        # D-ENV1: the native reader gets the allowlisted environment; a supplied
        # runner (D-ST1's stand-ins under pytest) builds its own.
        result = execute(keychain_command(credential.ref, runner=execute),
                         capture_output=True, text=True, timeout=10, check=False,
                         **({"env": build_environment()} if native_runner(runner) else {}))
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
