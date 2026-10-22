"""Resolve personal paths without letting pytest fall back to live stores."""

from __future__ import annotations

import os
import stat
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


# D-ST3: the account's live roots, each directly beneath its home. A test
# process resolves none of them, nor a path inside one, by any spelling.
LIVE_ROOTS = (".subfleet", ".claude", ".codex", ".claude.json")
# Links `names_live_root` follows before it refuses, as realpath's ELOOP would.
_MAX_LINKS = 40
# This process's own code: the tree this package runs from, and the interpreter
# and environment running it. A job's checkout can sit under ~/.subfleet/worktrees,
# and a path inside it is the process's own, not the store's.
_OWN_ROOTS = tuple(sorted({os.path.realpath(root) for root in (
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), sys.prefix, sys.base_prefix,
    sys.exec_prefix) if root}))


class PersonalPathRefused(RuntimeError):
    """A test process refused a personal path (D-ST1, D-ST3).

    A RuntimeError, as every such refusal always was, so existing handlers
    still catch it; `daemon.main` reports it in one line instead of a traceback.
    """


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
        return personal_home(Path.home())
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
    override that names the account's home or one of its live roots
    (`refuse_personal_override`). An empty override that `accept_empty` takes
    names the working directory, never a live root, and is returned as it is.
    """
    testing = under_pytest()
    if test_only and not testing:
        return default()
    values = os.environ if env is None else env
    override = values.get(variable)
    if override or (accept_empty and override is not None):
        if testing:
            refuse_personal_override(variable, override)
        return Path(override).expanduser() if expand_override else override
    if testing:
        raise PersonalPathRefused(f"Refusing personal path fallback under pytest; set {variable} "
                                  "to an isolated test path")
    return default()


def checked_override(variable: str, value, *, relative_to: str | Path | None = None):
    """`value`, read raw from path-valued `variable`, unless a test process refuses it.

    For a caller that reads such an override itself instead of through
    `home_path`: it takes the same refusals as an override there (D-ST3).
    Outside a test process `value` is returned untouched, without a check.
    """
    if value and under_pytest():
        refuse_personal_override(variable, value, relative_to=relative_to)
    return value


def refuse_personal_override(variable: str, value, *, relative_to: str | Path | None = None) -> None:
    """In a test process, refuse an override for `variable` that names the account's own paths.

    Two refusals, each before any access: a leading `~` or `~name` that
    expands to the account's home, whatever follows it (`_names_account_home`),
    and a value naming the account's home or a live root, or a path inside
    one, by any spelling (`names_live_root`).
    """
    if _names_account_home(value):
        raise PersonalPathRefused(f"Refusing personal path fallback under pytest; {variable} begins with ~, "
                                  "which expands to the account's home here; set it to an isolated test path")
    checked = value
    if relative_to is not None and isinstance(value, (str, bytes, os.PathLike)):
        expanded = os.path.expanduser(os.fsdecode(value))
        if not os.path.isabs(expanded):
            # Match the consumer's base without resolving or probing it first.
            checked = os.path.join(os.path.abspath(relative_to), expanded)
    if names_live_root(checked):
        raise PersonalPathRefused(f"Refusing personal path under pytest; {variable} names the account's home "
                                  "or one of its live stores; set it to an isolated test path")


def _names_account_home(value: str) -> bool:
    """Whether `value`'s leading `~` or `~name` expands to the account's home.

    Expansion reads HOME and, without one, pwd: the account's home. `~name`
    reads pwd for any name, the account's own included. Only the home a tilde
    names is judged, never a store beneath it, and as a HOME is judged
    (`personal_home`): a HOME inside a live root counts, and is never stat'ed.
    """
    if not isinstance(value, str) or not value.startswith("~"):
        return False
    head = value.split(os.sep, 1)[0]
    expanded = os.path.expanduser(head)
    return expanded != head and personal_home(Path(expanded))


def names_live_root(value) -> bool:
    """Whether path `value` names the account's home or a live root, or a path inside one.

    Judged by every spelling a caller could reach (D-ST3): after `~` expansion
    (through HOME, and through pwd when HOME is itself spelled with a `~`),
    lexically normalized against the working directory, and followed as
    `Path.resolve` follows its links. Neither a live root nor anything beneath
    one is stat'ed: the spelling is judged without a stat, and the link walk
    stops before its first call on or beneath one. A path inside this process's
    own code (`_OWN_ROOTS`) is the process's own. An empty value names the
    working directory and is left to its caller; a relative one under a
    working directory that is gone cannot be judged, so it is refused.
    """
    return _live_kind(value) is not None


def _live_kind(value) -> str | None:
    """`names_live_root`'s verdict: "store" for a live root or a path inside one,
    "home" for the account's home itself, else None."""
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if not isinstance(value, str) or not value:
        return None
    texts = [os.path.expanduser(value)]
    if texts[0].startswith("~"):
        named = _tilde_home(texts[0])
        if named is not None:
            texts.append(named)
    own = _own_roots()
    found = None
    for text in texts:
        if not os.path.isabs(text):
            try:
                text = os.path.join(os.getcwd(), text)
            except OSError:
                return "store"
        # A spelling that is lexically the account's home is that, without a
        # stat (`home/.subfleet/..` included); every caller refuses it or keeps
        # to isolated paths. Otherwise its links are followed too.
        kind = _spelled_kind(os.path.normpath(text), own)
        if kind is None:
            kind = _resolved_kind(text, own)
        if kind == "store":
            return kind
        found = found or kind
    return found


def _fold(path: str) -> str:
    """`path` as the file system compares it: case folded, slashes and dots
    collapsed, without macOS's `/System/Volumes/Data` firmlink."""
    folded = "/" + os.path.normpath(path).casefold().lstrip("/")
    data = "/system/volumes/data"
    return folded[len(data):] if folded.startswith(data + "/") else folded


def _spelled_kind(path: str, own) -> str | None:
    """`_live_kind` of normalized absolute `path` by its spelling alone, without a stat."""
    if _within(path, own):
        return None
    personal = _fold(_account_home())
    parts = _fold(path).split("/")
    for index in range(1, len(parts)):
        if parts[index] in LIVE_ROOTS and ("/".join(parts[:index]) or "/") == personal:
            return "store"
    return "home" if _fold(path) == personal else None


def _resolved_kind(path: str, own) -> str | None:
    """`_live_kind` of absolute `path` followed as os.path.realpath follows it.

    Each component is looked up with lstat, and a link's target spliced in, as
    realpath does; a missing one is kept as spelled. A component that enters a
    live root, judged by its parent (`is_account_home`, a stat of the parent
    alone), ends the walk before any call on it, unless it is this process's own
    code. A path spelled under an own root is walked from that root
    (`_walk_start`), so nothing above it is stat'ed, a live root holding it
    included.
    """
    resolved, pending, inside = _walk_start(path, own)
    links = 0
    while pending:
        name = pending.pop()
        if name in ("", "."):
            continue
        if name == "..":
            resolved = os.path.dirname(resolved)
            inside = _spelled_kind(resolved, ()) == "store" or _inside_by_identity(resolved)
            continue
        candidate = os.path.join(resolved, name)
        entering = inside or (name.casefold() in LIVE_ROOTS and is_account_home(Path(resolved)))
        if entering and not _within(candidate, own):
            return "store"
        try:
            mode = os.lstat(candidate).st_mode
        except (OSError, ValueError):
            mode = 0
        if not stat.S_ISLNK(mode):
            resolved, inside = candidate, entering
            continue
        links += 1
        if links > _MAX_LINKS:
            return "store"
        try:
            target = os.readlink(candidate)
        except (OSError, ValueError):
            resolved, inside = candidate, entering
            continue
        if target.startswith("/"):
            resolved, pending, inside = _walk_start("/".join([target, *reversed(pending)]), own)
            continue
        pending.extend(target.split("/")[::-1])
    if _within(resolved, own):
        return None
    if inside:
        return "store"
    return "home" if is_account_home(Path(resolved)) else None


def _walk_start(path: str, own) -> tuple[str, list[str], bool]:
    """Where `_resolved_kind`'s walk of absolute `path` begins: the directory, the
    components left (last first, as the walk pops them), and whether it is inside
    a live root.

    An own root is a realpath, so a walk from `/` would reach it unchanged; a path
    spelled under one, past empty and `.` components, begins there without a call.
    Any other path begins at `/`. A job's checkout under ~/.subfleet/worktrees is
    thus walked without a stat of ~/.subfleet; one spelled otherwise (through `..`,
    a firmlink or another case) enters the live root and is refused.
    """
    parts = [part for part in path.split("/") if part not in ("", ".")]
    for root in own:
        head = [part for part in root.split("/") if part]
        if parts[:len(head)] == head:
            return root, parts[len(head):][::-1], _spelled_kind(root, ()) == "store"
    return "/", parts[::-1], False


def _inside_by_identity(resolved: str) -> bool:
    """Whether resolved, link-free `resolved` lies in a live root, by each store-named
    component's parent (`is_account_home`), checked from the root down."""
    parts = resolved.split("/")
    return any(parts[index].casefold() in LIVE_ROOTS and is_account_home(Path("/".join(parts[:index]) or "/"))
               for index in range(1, len(parts)))


def _account_home() -> str:
    import pwd

    return pwd.getpwuid(os.getuid()).pw_dir


def _own_roots() -> tuple[str, ...]:
    """`_OWN_ROOTS` that hold no live root: none is the account's home, a live
    root, or a directory above one, so the exemption never covers a store."""
    personal = _fold(_account_home())
    stores = [personal.rstrip("/") + "/" + name for name in LIVE_ROOTS]
    return tuple(root for root in _OWN_ROOTS
                 if not any(_fold(root) == store or store.startswith(_fold(root).rstrip("/") + "/")
                            for store in [personal, *stores]))


def _within(path: str, roots) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def _tilde_home(spelled: str) -> str | None:
    """`spelled` with its leading `~` or `~name` replaced by that user's home from
    pwd, or None when `~name` names no user."""
    import pwd

    head, sep, rest = spelled.partition(os.sep)
    try:
        entry = pwd.getpwuid(os.getuid()) if head == "~" else pwd.getpwnam(head[1:])
    except (KeyError, ValueError):
        return None
    return entry.pw_dir + sep + rest


def personal_home(home: Path) -> bool:
    """Whether HOME `home` would resolve the account's stores (D-ST3).

    It does when it is the account's home by any spelling, or names a live
    root or a path inside one. A HOME spelled with a leading `~` or `~name` is
    judged as the home it names through pwd; one naming no user is neither.
    """
    return _home_kind(home) is not None


def _home_kind(home: Path) -> str | None:
    """`_live_kind` of a HOME. A leading `~` or `~name` is read through pwd, never
    through HOME itself; the stat-free store check comes before any stat."""
    spelled = os.fspath(home)
    if spelled.startswith("~"):
        spelled = _tilde_home(spelled)
        if spelled is None:
            return None
    kind = _live_kind(spelled)
    if kind is None and is_account_home(Path(spelled)):
        kind = "home"
    return kind


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
    kind = _home_kind(home)
    if kind == "home":
        return isolated
    if kind == "store":
        # D-ST3: HOME/.claude would be a path inside the account's live root.
        raise PersonalPathRefused("Refusing provider homes under pytest; HOME names one of the account's "
                                  "live stores; set HOME to an isolated test home")
    return [home / ".claude", home / ".codex", *isolated]


def is_account_home(home: Path) -> bool:
    """Whether `home` is the account's own home directory, by any spelling.

    Equal spellings decide without a stat, lexically normalized ones too.
    Otherwise the home directories themselves are compared, never a store
    beneath them: as `home` reads, and as `Path.resolve` would build on it.
    The second catches `home/missing/..`, which no stat can follow but which
    resolves to the account's home (D-ST3). A home spelled with a leading `~`
    or `~name`, as a HOME can be, is the home it names through pwd, as a
    consumer's `os.path.expanduser` reaches it; one naming no user is not the
    account's.
    """
    import pwd

    personal = Path(pwd.getpwuid(os.getuid()).pw_dir)
    tilde_spelled = os.fspath(home)
    if tilde_spelled.startswith("~"):
        named = _tilde_home(tilde_spelled)
        if named is None:
            return False
        home = Path(named)
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
    if under_pytest() and personal_home(home):
        raise PersonalPathRefused("Refusing to resolve the personal state root ~/.subfleet under pytest; "
                                  "set HOME to an isolated test home")
    return (home / ".subfleet").resolve()


def desktop_login_path() -> Path:
    """Claude's desktop login, with a test-only isolation override."""
    return Path(home_path("SUBFLEET_DESKTOP_LOGIN", lambda: Path.home() / ".claude.json",
                          test_only=True))


def provider_home() -> Path:
    """Base of legacy provider homes, with a test-only isolation override."""
    return Path(home_path("SUBFLEET_PROVIDER_HOME", Path.home, test_only=True))
