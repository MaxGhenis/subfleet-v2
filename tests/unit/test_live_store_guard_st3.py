"""C-20.6 (D-ST3): a pytest process never resolves the account's live roots, whatever os.environ holds.

D-ST1's refusals read the test session from os.environ alone: the session
marker with a live sentinel, else PYTEST_CURRENT_TEST. In #222's fix round 5,
four unit tests replaced os.environ with a plain dict that named neither, so
`cmd_daemon_start` read as production and fell back to the person's
~/.subfleet. The process rule (`home_paths.pytest_loaded`) reads pytest's own
modules instead, and every refusal takes either rule.

Each test here replaces os.environ the same way, under spies that refuse,
before the call, any access beneath the account's home stores. The spies
compare spellings only; they never stat or open a live path to check.
"""
from __future__ import annotations

import ast
import builtins
import contextlib
import io
import json
import os
import pwd
import socket
import sqlite3
import subprocess
import sys
import textwrap
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import cli, client, compat, credentials, home_paths, host_push, prune_decisions, retention_git
from subfleet.adapters.claude_mcp import global_config_path
from subfleet.guard import preflight
from subfleet.home_paths import (
    PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID, SAFE_ENV_REMOVALS, SAFE_HOME_PATHS,
)
from subfleet.sessions import transcripts
from tests.live_store_trap import LIVE_STORES, PERSONAL
from tests.unit.test_live_store_guard import RESOLVERS

REPO = Path(__file__).resolve().parents[2]
STORES = (".subfleet", ".claude", ".codex", ".claude.json")
# A job's checkout can itself sit under ~/.subfleet/worktrees; its own files
# are the test's, not the store's. Beside the stores, every other personal
# default a resolver names: v1's install and state, the desktop app's log and
# LaunchAgents, v1's delegate state and the agent-secret helper.
CHECKOUT = REPO
FORBIDDEN_ROOTS = (*LIVE_STORES, PERSONAL / "chief-of-staff", PERSONAL / "Library" / "Logs" / "Claude",
                   PERSONAL / "Library" / "LaunchAgents", PERSONAL / ".local" / "state" / "delegate",
                   PERSONAL / "bin" / "agent-secret")
ENVIRONS = {"empty": {}, "path-only": {"PATH": "/usr/bin:/bin"}}
REFUSED = "Refusing"


def _spellings(path: str) -> list[str]:
    """`path` folded as the trap folds it, with its firmlink spelling."""
    folded = "/" + os.path.normpath(path).casefold().lstrip("/")
    data = "/system/volumes/data"
    return [folded, folded[len(data):] or "/"] if folded.startswith(data + "/") else [folded]


def _within(spelled: str, roots) -> bool:
    return any(spelled == root or spelled.startswith(root.rstrip("/") + "/") for root in roots)


FORBIDDEN = [spelled for root in FORBIDDEN_ROOTS for spelled in _spellings(str(root))]
# This process's own code: the checkout, and the interpreter and environment
# running it (a job's .venv also sits in a checkout under ~/.subfleet).
OWN = [spelled for root in {CHECKOUT, Path(sys.prefix), Path(sys.base_prefix), Path(sys.exec_prefix),
                            Path(pytest.__file__).parents[1]}
       for spelled in _spellings(str(root))]


# Looking up the checkout's own ancestors is how this process reads its files
# (realpath, inspect, linecache), so a metadata call on one is the test's too.
METADATA = frozenset({"os.stat", "os.lstat", "os.readlink"})


def reaches_live(path, dir_fd=None, api=None) -> bool:
    """Whether a call on `path` reaches a forbidden root, by spelling alone.

    The walk checks every prefix, so `root/..` still reaches the root. A path
    that ends inside this process's own code (the checkout, its interpreter
    and environment) is the test's own, wherever it lives, and so is a
    metadata lookup of one of their ancestors.
    """
    if isinstance(path, int) or path is None:
        return False
    try:
        spelled = os.fsdecode(path)
    except TypeError:
        return False
    if dir_fd is not None and not os.path.isabs(spelled):
        return False                     # relative to a descriptor this test opened
    absolute = os.path.join(os.getcwd(), spelled)
    finals = _spellings(absolute)
    if any(_within(final, OWN) for final in finals):
        return False
    if api in METADATA and any(_within(own, [final]) for own in OWN for final in finals):
        return False
    walked = ""
    for part in absolute.split("/"):
        if not part:
            continue
        walked = os.path.normpath(os.path.join(walked or "/", part))
        if any(_within(prefix, FORBIDDEN) for prefix in _spellings(walked)):
            return True
    return False


def _sqlite_target(database, uri=False):
    text = os.fsdecode(database) if not isinstance(database, int) else ""
    if uri or text.startswith("file:"):
        text = urllib.parse.unquote(urllib.parse.urlsplit(text).path or text[len("file:"):].split("?")[0])
    return text if text and text != ":memory:" else None


class Seal:
    """What the spies saw: every refused call, and every launch attempted."""

    def __init__(self):
        self.touched: list[tuple[str, str]] = []
        self.launched: list[object] = []

    def check(self, api, path, dir_fd=None):
        if reaches_live(path, dir_fd, api):
            self.touched.append((api, os.fsdecode(path)))
            pytest.fail(f"{api} touched {os.fsdecode(path)}")


@contextlib.contextmanager
def sealing(patch):
    """Install the spies on `patch`: os path calls, open, sqlite3, sockets, Popen.

    Each refuses before the underlying call. pytest.fail raises a
    BaseException, so no `except Exception` in the code under test hides it.
    """
    seal = Seal()

    def spy(api, function, operands):
        def call(*args, **kwargs):
            for index, keyword, fd_keyword in operands:
                path = args[index] if len(args) > index else kwargs.get(keyword)
                seal.check(api, path, kwargs.get(fd_keyword))
            return function(*args, **kwargs)
        return call

    for name in ("chmod", "chown", "mkdir", "makedirs", "stat", "lstat", "open", "listdir", "scandir",
                 "unlink", "remove", "rmdir", "access", "utime", "readlink", "truncate", "chdir"):
        patch.setattr(os, name, spy(f"os.{name}", getattr(os, name), [(0, "path", "dir_fd")]))
    for name in ("rename", "replace", "link", "symlink"):
        patch.setattr(os, name, spy(f"os.{name}", getattr(os, name), [(0, "src", "src_dir_fd"),
                                                                       (1, "dst", "dst_dir_fd")]))
    patch.setattr(builtins, "open", spy("open", builtins.open, [(0, "file", "dir_fd")]))
    patch.setattr(io, "open", spy("io.open", io.open, [(0, "file", "dir_fd")]))
    patch.setattr(io, "FileIO", spy("io.FileIO", io.FileIO, [(0, "file", "dir_fd")]))
    connect = sqlite3.connect

    def sqlite_connect(database, *args, **kwargs):
        seal.check("sqlite3.connect", _sqlite_target(database, kwargs.get("uri", False)))
        return connect(database, *args, **kwargs)

    patch.setattr(sqlite3, "connect", sqlite_connect)
    socket_connect = socket.socket.connect

    def connect_socket(self, address):
        if isinstance(address, (str, bytes)):
            seal.check("socket.connect", address)
        return socket_connect(self, address)

    patch.setattr(socket.socket, "connect", connect_socket)

    def launch(*args, **kwargs):
        seal.launched.append(args[0] if args else kwargs.get("args"))
        pytest.fail(f"launched {args[0] if args else kwargs.get('args')!r}")

    patch.setattr(subprocess, "Popen", launch)
    yield seal


@pytest.fixture
def sealed(monkeypatch):
    with sealing(monkeypatch) as seal:
        yield seal


def replace_environ(patch, environ):
    """os.environ as the round-5 tests left it: a plain dict, no session names."""
    patch.setattr(os, "environ", dict(environ))


# --- the reported shape: `daemon start` under a plain environment ----------------------


@pytest.mark.parametrize("environ", ENVIRONS.values(), ids=ENVIRONS)
def test_daemon_start_refuses_the_live_root_before_any_access(environ, sealed, monkeypatch):
    """C-20.6 (D-ST3): #222 round 5's shape, `cmd_daemon_start` with os.environ a plain dict.

    At release it resolved ~/.subfleet and then ran chmod, read policy.json,
    lanes.json and the store, and opened daemon.log for append. Now `_root`
    refuses before the client exists, so no spy sees a call.
    """
    alive = []
    monkeypatch.setattr(cli, "_daemon_alive", lambda found: alive.append(found.root) or False)
    replace_environ(monkeypatch, environ)
    with pytest.raises(RuntimeError, match="SUBFLEET_HOME"):
        cli.cmd_daemon_start(SimpleNamespace(state_root=None))
    assert alive == [] and sealed.touched == [] and sealed.launched == []


# --- each guarded function ---------------------------------------------------------------


@pytest.mark.parametrize("environ", ENVIRONS.values(), ids=ENVIRONS)
def test_production_state_root_refuses(environ, sealed, monkeypatch):
    """C-20.6 (D-ST3): production_state_root refuses ~/.subfleet with no session names."""
    replace_environ(monkeypatch, environ)
    with pytest.raises(RuntimeError, match="~/.subfleet"):
        home_paths.production_state_root()
    assert sealed.touched == []


@pytest.mark.parametrize("environ", ENVIRONS.values(), ids=ENVIRONS)
@pytest.mark.parametrize("variable,resolver,suffix", RESOLVERS)
def test_every_resolver_refuses_its_default(environ, variable, resolver, suffix, sealed, monkeypatch):
    """C-20.6 (D-ST3): `home_path` with each production default; every D-ST1 resolver refuses."""
    replace_environ(monkeypatch, environ)
    with pytest.raises(RuntimeError, match=variable):
        resolver()
    assert sealed.touched == []


@pytest.mark.parametrize("environ", ENVIRONS.values(), ids=ENVIRONS)
@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("options", [{}, {"test_only": True}, {"accept_empty": True},
                                     {"expand_override": True}], ids=["plain", "test-only", "accept-empty",
                                                                      "expand"])
def test_home_path_refuses_each_store_default(environ, store, options, sealed, monkeypatch):
    """C-20.6 (D-ST3): each store's default is refused, read from os.environ or a given env."""
    replace_environ(monkeypatch, environ)

    def default():
        pytest.fail("the personal default was evaluated")

    with pytest.raises(RuntimeError, match="SUBFLEET_HOME"):
        home_paths.home_path("SUBFLEET_HOME", default, **options)
    with pytest.raises(RuntimeError, match="SUBFLEET_HOME"):
        home_paths.home_path("SUBFLEET_HOME", lambda: Path.home() / store, env={}, **options)
    assert sealed.touched == []


@pytest.mark.parametrize("environ", ENVIRONS.values(), ids=ENVIRONS)
@pytest.mark.parametrize("function,variable", [
    (home_paths.protected_provider_homes, "CLAUDE_CONFIG_DIR"),
    (home_paths.codex_app_home, "SUBFLEET_CODEX_APP_HOME"),
    (home_paths.desktop_login_path, "SUBFLEET_DESKTOP_LOGIN"),
    (home_paths.provider_home, "SUBFLEET_PROVIDER_HOME"),
    (lambda: client.state_root({}), "SUBFLEET_HOME"),
    (host_push._credential_paths, "SUBFLEET_HOST_HOME"),
    (host_push._environment, "SUBFLEET_HOST_HOME"),
    (lambda: credentials.keychain_command("claude-quota-fixture@example.test", runner=lambda *a, **k: None),
     "SUBFLEET_AGENT_SECRET"),
], ids=["protected-homes", "codex-app", "desktop-login", "provider-home", "state-root-env", "host-paths",
        "host-environment", "agent-secret"])
def test_each_guarded_function_refuses(environ, function, variable, sealed, monkeypatch):
    """C-20.6 (D-ST3): each other guarded function refuses with no session names."""
    replace_environ(monkeypatch, environ)
    with pytest.raises(RuntimeError, match=variable):
        function()
    assert sealed.touched == []


@pytest.mark.parametrize("environ", ENVIRONS.values(), ids=ENVIRONS)
def test_the_v1_install_default_is_refused(environ, sealed, monkeypatch):
    """C-20.6 (D-ST3): step 3's fallback; `compat.v1_binary` defaulted to the person's v1 install."""
    replace_environ(monkeypatch, environ)
    with pytest.raises(RuntimeError, match=compat.V1_BIN_ENV):
        compat.v1_binary()
    assert sealed.touched == []


@pytest.mark.parametrize("environ", ENVIRONS.values(), ids=ENVIRONS)
def test_prune_decisions_state_root_default_is_refused(environ, sealed, monkeypatch):
    """C-20.6 (D-ST3): step 3's fallback; `python -m subfleet.prune_decisions` defaulted to ~/.subfleet."""
    replace_environ(monkeypatch, environ)
    with pytest.raises(RuntimeError, match="SUBFLEET_HOME"):
        prune_decisions.main(["--no-report"])
    assert sealed.touched == []


def test_prune_decisions_state_root_default_keeps_the_environment(tmp_path, monkeypatch):
    """C-20.6 (D-ST3): outside a refusal the default is unchanged: SUBFLEET_HOME, read once."""
    seen = []
    monkeypatch.setattr(prune_decisions, "prune", lambda root, **_: seen.append(root) or pytest.fail("stop"))
    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path / "chosen"))
    with pytest.raises(pytest.fail.Exception, match="stop"):
        prune_decisions.main(["--no-report"])
    assert seen == [str(tmp_path / "chosen")]


# --- the process rule ---------------------------------------------------------------------


@pytest.mark.parametrize("environ", ENVIRONS.values(), ids=ENVIRONS)
def test_the_process_rule_holds_whatever_the_environment(environ, monkeypatch):
    """C-20.6 (D-ST3): this process is a test process whatever os.environ holds; a child given it is not."""
    replace_environ(monkeypatch, environ)
    assert home_paths.pytest_loaded()
    assert home_paths.under_pytest()
    # A child given this environment has no pytest loaded: the environment rule alone.
    assert not home_paths.under_pytest(dict(environ))


PROCESS_PROBE = '''
import json, os, sys
if sys.argv[1] == "pytest":
    import pytest  # noqa: F401
from subfleet import home_paths
print(json.dumps({"loaded": home_paths.pytest_loaded(), "under": home_paths.under_pytest()}))
'''


@pytest.mark.parametrize("markers", ["live", "none"])
@pytest.mark.parametrize("imports", ["plain", "pytest"])
def test_a_child_keeps_the_environment_rule_unless_it_loads_pytest(tmp_path, markers, imports):
    """C-20.6 (D-ST3): children keep the marker rule; one that imports pytest is a test process."""
    env = {**os.environ, "PYTHONPATH": str(REPO), "HOME": str(tmp_path / "home")}
    if markers == "none":
        for name in (PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID, "PYTEST_CURRENT_TEST"):
            env.pop(name, None)
    done = subprocess.run([sys.executable, "-c", PROCESS_PROBE, imports], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.splitlines()[-1])
    assert out == {"loaded": imports == "pytest", "under": imports == "pytest" or markers == "live"}


def imports_pytest(name: str) -> bool:
    """Whether importing `name` loads pytest: pytest, _pytest, or a `pytest_*` plugin, which imports pytest."""
    top = name.split(".")[0]
    return top in ("pytest", "_pytest") or top.startswith("pytest_")


def pytest_imports(source: str) -> list[int]:
    """The lines of `source` whose import loads pytest (`imports_pytest`), found statically.

    `import`, absolute `from`, and `__import__` or `import_module` called with a
    constant name. A computed name (`'py' + 'test'`), an aliased `import_module`
    and `exec` are beyond a static check; `test_live_store_guard_st3_children.py`
    imports every module in a fresh child instead.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names = [node.module or ""]
        elif (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant)
              and isinstance(node.func, (ast.Name, ast.Attribute))
              and getattr(node.func, "id", getattr(node.func, "attr", "")) in ("import_module", "__import__")):
            names = [str(node.args[0].value)]
        found += [node.lineno for name in names if imports_pytest(name)]
    return found


@pytest.mark.parametrize("line", ["import pytest", "from _pytest import nodes", "import pytest_timeout",
                                  "from pytest_xdist import plugin", "import os, pytest_cov.plugin",
                                  "__import__('_pytest.x')", "importlib.import_module('pytest_timeout')"])
def test_the_import_check_flags_pytest_and_its_plugins(line):
    """C-20.6 (D-ST3, review r1 check 2): `import pytest_timeout` loads pytest as surely as `import pytest`."""
    assert pytest_imports(line) == [1]


@pytest.mark.parametrize("line", ["import pytestish", "from subfleet import pytest_like", "from . import pytest",
                                  "import importlib", "x = 'pytest'"])
def test_the_import_check_leaves_other_names(line):
    """C-20.6 (D-ST3): a name that merely contains pytest, or this package's own module, is not pytest."""
    assert pytest_imports(line) == []


def test_no_subfleet_module_imports_pytest():
    """C-20.6 (D-ST3): a production process could hold pytest's modules only by importing pytest."""
    found = [f"{path.relative_to(REPO)}:{line}" for path in sorted((REPO / "subfleet").rglob("*.py"))
             for line in pytest_imports(path.read_text(encoding="utf-8"))]
    assert found == []


# --- the one seam ----------------------------------------------------------------------------


def test_the_seam_waives_the_process_rule_only_while_home_is_not_the_accounts(tmp_path, monkeypatch,
                                                                                production_process):
    """C-20.6 (D-ST3): the seam turns the process rule off for a test HOME, never the account's."""
    for name in (PYTEST_MARKER, "PYTEST_CURRENT_TEST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert home_paths.pytest_loaded()
    assert not home_paths.pytest_process() and not home_paths.under_pytest()
    for spelling in account_spellings():
        monkeypatch.setenv("HOME", spelling)
        assert home_paths.pytest_process(), spelling
        assert home_paths.under_pytest(), spelling
    monkeypatch.delenv("HOME")
    assert home_paths.pytest_process() and home_paths.under_pytest()
    replace_environ(monkeypatch, {"PATH": "/usr/bin:/bin"})
    assert home_paths.pytest_process() and home_paths.under_pytest()


def test_the_seam_keeps_the_live_root_refused_under_the_accounts_home(sealed, monkeypatch, production_process):
    """C-20.6 (D-ST3): with the account's HOME the waived rule stands, and nothing is touched."""
    replace_environ(monkeypatch, {"PATH": "/usr/bin:/bin", "HOME": str(PERSONAL)})
    with pytest.raises(RuntimeError, match="~/.subfleet"):
        home_paths.production_state_root()
    with pytest.raises(RuntimeError, match="SUBFLEET_CODEX_APP_HOME"):
        home_paths.codex_app_home()
    assert sealed.touched == []


def test_the_seam_restores_production_defaults_under_a_test_home(tmp_path, monkeypatch, production_process):
    """C-20.6 (D-ST3): what the seam is for, a test_only default outside a session under its own HOME."""
    for name in (PYTEST_MARKER, "PYTEST_CURRENT_TEST", "SUBFLEET_CODEX_APP_HOME", "SUBFLEET_DESKTOP_LOGIN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert home_paths.codex_app_home() == tmp_path / "home" / ".codex"
    assert home_paths.desktop_login_path() == tmp_path / "home" / ".claude.json"
    assert home_paths.production_state_root() == (tmp_path / "home" / ".subfleet").resolve()


# --- spellings of the account's home -------------------------------------------------------


def account_spellings() -> list[str]:
    """HOME values that name the account's home without being its pw_dir spelling.

    `missing/..` resolves lexically under Path.resolve, though no stat can
    follow it; `.subfleet/..` must be decided without a stat through the store;
    the firmlink and case spellings name it on macOS's data volume.
    """
    personal = str(PERSONAL)
    spellings = [personal, personal + "/", "/" + personal, personal + "/.",
                 personal + "/st3-missing-component/..", personal + "/st3-missing/../",
                 personal + "/" + STORES[0] + "/.."]
    if sys.platform == "darwin":
        spellings += ["/System/Volumes/Data" + personal, personal.upper(),
                      personal.upper() + "/st3-missing/.."]
    return spellings


@pytest.mark.parametrize("spelling", account_spellings())
def test_production_state_root_refuses_every_spelling_of_the_accounts_home(spelling, sealed, monkeypatch):
    """C-20.6 (D-ST3): at release `home/st3-missing/..` read as a test HOME; resolve() built ~/.subfleet."""
    monkeypatch.setenv("HOME", spelling)
    assert home_paths.is_account_home(Path(spelling))
    with pytest.raises(RuntimeError, match="~/.subfleet"):
        home_paths.production_state_root()
    assert home_paths.protected_provider_homes() == [Path(os.environ["CLAUDE_CONFIG_DIR"]),
                                                    Path(os.environ["SUBFLEET_CODEX_APP_HOME"])]
    assert sealed.touched == []


def test_a_home_spelled_through_a_link_past_a_missing_component_is_the_accounts(tmp_path, sealed, monkeypatch):
    """C-20.6 (D-ST3): no stat follows `missing/../link`, but Path.resolve builds on its target.

    At release, as a test HOME pointing at the account's home this way,
    production_state_root resolved the person's ~/.subfleet.
    """
    (tmp_path / "link").symlink_to(PERSONAL, target_is_directory=True)
    spelling = f"{tmp_path}/st3-missing/../link"
    monkeypatch.setenv("HOME", spelling)
    assert home_paths.is_account_home(Path(spelling))
    with pytest.raises(RuntimeError, match="~/.subfleet"):
        home_paths.production_state_root()
    assert sealed.touched == []


def test_a_home_spelled_through_a_store_is_decided_without_a_stat(monkeypatch):
    """C-20.6 (D-ST3): `home/.subfleet/..` is the account's home by its spelling alone.

    So no stat walks through the store to decide it. A stat or lstat that would
    is refused and recorded; the recorders are undone before the assertions.
    """
    stores = [spelled for store in STORES for spelled in _spellings(str(PERSONAL / store))]
    through = []

    def walks_a_store(path):
        walked = ""
        for part in os.path.join(os.getcwd(), os.fsdecode(path)).split("/"):
            if part:
                walked = os.path.normpath(os.path.join(walked or "/", part))
                if any(_within(prefix, stores) for prefix in _spellings(walked)):
                    return True
        return False

    with monkeypatch.context() as patch:
        for name in ("stat", "lstat"):
            def recorded(path, *args, _real=getattr(os, name), **kwargs):
                if not isinstance(path, int) and walks_a_store(path):
                    through.append(os.fsdecode(path))
                    raise PermissionError(f"st3: a stat through {os.fsdecode(path)}")
                return _real(path, *args, **kwargs)
            patch.setattr(os, name, recorded)
        decided = [home_paths.is_account_home(Path(f"{PERSONAL}/{store}/..")) for store in STORES]
    assert decided == [True] * len(STORES)
    assert through == []


# The account's own login name, from pwd, never written into the tests.
ACCOUNT = pwd.getpwuid(os.getuid()).pw_name
TILDES = {"tilde": "~/", "tilde-user": f"~{ACCOUNT}/"}
# `~/` names the account's home with HOME unset or the account's; `~<name>/`
# names it through pwd under any HOME, a test's own included.
TILDE_HOMES = [("tilde", "unset"), ("tilde", "account"), ("tilde-user", "unset"), ("tilde-user", "account"),
               ("tilde-user", "test")]


@pytest.mark.parametrize("variable", ["SUBFLEET_HOME", "SUBFLEET_CLAUDE_DIR", "SUBFLEET_CODEX_APP_HOME"])
@pytest.mark.parametrize("form,home", TILDE_HOMES)
@pytest.mark.parametrize("below", ["st3-not-a-store", STORES[0]])
def test_a_tilde_override_naming_the_accounts_home_is_refused(variable, form, home, below, tmp_path, sealed,
                                                              monkeypatch):
    """C-20.6 (D-ST3): `~/...` expands through HOME, or without one through pwd, to the account's home,
    and `~<name>/...` through pwd under any HOME.

    The tilde's refusal holds whatever follows it. Below a store the live-root
    refusal would refuse as well, so the non-store case is the one that pins
    the tilde rule (mutants `no-tilde-refusal` and `tilde-user-ignored`).
    """
    if home == "unset":
        monkeypatch.delenv("HOME")
    elif home == "account":
        monkeypatch.setenv("HOME", str(PERSONAL))
    else:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(variable, TILDES[form] + below)
    with pytest.raises(RuntimeError, match=f"{variable} begins with ~"):
        home_paths.home_path(variable, lambda: pytest.fail("default"), expand_override=True)
    assert sealed.touched == []


def test_a_tilde_override_under_a_test_home_is_the_tests(tmp_path, monkeypatch):
    """C-20.6 (D-ST3): a `~` override under a test HOME is the test's own and resolves there."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for chosen in ("~/chosen", "~/" + STORES[0]):
        monkeypatch.setenv("SUBFLEET_HOME", chosen)
        assert client.state_root() == tmp_path / "home" / chosen[2:]


# --- overrides naming a live root ------------------------------------------------------------


# Making a link touches nothing at its target, but a spy judges the target by
# its spelling, so test setup makes its links with the unspied call.
_make_link = os.symlink


def live_root_overrides(tmp_path: Path) -> dict[str, str]:
    """Overrides that name a live root, a path inside one, or the account's home, by other spellings.

    None begins with a `~` that names the account, so only the live-root
    refusal can catch them. The links are made under tmp_path.
    """
    store = PERSONAL / STORES[0]
    test_home = tmp_path / "home"
    _make_link(str(store), str(tmp_path / "link"), target_is_directory=True)
    _make_link(str(PERSONAL), str(tmp_path / "home-link"), target_is_directory=True)
    overrides = {
        "absolute": str(store), "inside": str(store / "state.sqlite3"), "trailing-slash": f"{store}/",
        "leading-slashes": f"//{store}", "dot": f"{store}/.", "missing-dot-dot": f"{PERSONAL}/st3-missing/../{STORES[0]}",
        "relative": os.path.relpath(store, tmp_path),
        # Review r1's: up through a test HOME and back down to the account's store.
        "test-home-dot-dot": "~/" + "../" * len(test_home.parts) + str(store).lstrip("/"),
        "link": str(tmp_path / "link"), "link-inside": str(tmp_path / "link" / "state.sqlite3"),
        "link-to-home": str(tmp_path / "home-link" / STORES[1]), "home": str(PERSONAL),
        **{f"store-{name}": str(PERSONAL / name) for name in STORES[1:]},
    }
    if sys.platform == "darwin":
        overrides.update({"firmlink": "/System/Volumes/Data" + str(store), "case": str(store).upper()})
    return overrides


LIVE_OVERRIDES = ["absolute", "inside", "trailing-slash", "leading-slashes", "dot", "missing-dot-dot", "relative",
                  "test-home-dot-dot", "link", "link-inside", "link-to-home", "home",
                  *[f"store-{name}" for name in STORES[1:]],
                  *(["firmlink", "case"] if sys.platform == "darwin" else [])]


@pytest.mark.parametrize("spelling", LIVE_OVERRIDES)
def test_an_override_naming_a_live_root_is_refused(spelling, tmp_path, sealed, monkeypatch):
    """C-20.6 (D-ST3, review r1): an override naming a live root, a path inside one, or the account's
    home, by any spelling, is refused before any access, even under a test HOME.

    At `aa74cd62a` `client.state_root()` returned the live ~/.subfleet for
    `SUBFLEET_HOME=<account home>/.subfleet`, and the `~/../..` spelling under a
    test HOME was not refused either: only the part before the first `/` was
    checked.
    """
    override = live_root_overrides(tmp_path)[spelling]
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUBFLEET_HOME", override)
    with pytest.raises(RuntimeError, match="SUBFLEET_HOME names the account's home or one of its live stores"):
        client.state_root()
    assert sealed.touched == []


@pytest.mark.parametrize("variable", sorted(SAFE_HOME_PATHS))
def test_every_override_takes_the_live_root_refusal(variable, tmp_path, sealed, monkeypatch):
    """C-20.6 (D-ST3): every registered override, through `home_path`, with and without `test_only`."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for value in (str(PERSONAL / STORES[0] / "x"), str(PERSONAL)):
        monkeypatch.setenv(variable, value)
        for options in ({}, {"test_only": True}, {"accept_empty": True}, {"expand_override": True}):
            with pytest.raises(RuntimeError, match=f"{variable} names the account's home"):
                home_paths.home_path(variable, lambda: pytest.fail("default"), **options)
    assert sealed.touched == []


@pytest.mark.parametrize("chosen", ["st3-not-a-store", "st3-not-a-store/.subfleet-like", ".subfleet-like"])
def test_an_override_beside_the_live_roots_is_the_tests(chosen, tmp_path, monkeypatch):
    """C-20.6 (D-ST3): a path under the account's home that is no live root is the test's own selection.

    Only spellings are compared: `client.state_root` returns the path without
    touching it.
    """
    monkeypatch.setenv("SUBFLEET_HOME", str(PERSONAL / chosen))
    assert client.state_root() == PERSONAL / chosen


def test_an_empty_override_names_the_working_directory(tmp_path, monkeypatch):
    """C-20.6 (D-ST3): `accept_empty` callers keep `""`, the working directory, as release does."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUBFLEET_HOME", "")
    assert home_paths.home_path("SUBFLEET_HOME", lambda: pytest.fail("default"), accept_empty=True) == ""
    assert preflight._state_root() == ""
    with pytest.raises(RuntimeError, match="SUBFLEET_HOME"):
        home_paths.home_path("SUBFLEET_HOME", lambda: pytest.fail("default"))


def test_this_process_s_own_code_is_its_own_inside_a_live_root(tmp_path, monkeypatch):
    """C-20.6 (D-ST3): a checkout under ~/.subfleet/worktrees is the process's own, never the store.

    With a stand-in account whose live root holds this process's code, a path
    inside that code is allowed; the store beside it, the root itself, and a
    path that leaves the code with `..` are refused. A link inside the code
    that leads back to the store is refused as well.
    """
    account = tmp_path / "account"
    own = account / STORES[0] / "worktrees" / "job"
    (own / "tests").mkdir(parents=True)
    (own / "back").symlink_to(account / STORES[0], target_is_directory=True)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(account), pw_name="st3"))
    monkeypatch.setattr(home_paths, "_OWN_ROOTS", (str(own),))
    assert not home_paths.names_live_root(str(own / "tests" / "state"))
    assert not home_paths.names_live_root(str(own))
    for refused in (account / STORES[0], account / STORES[0] / "state.sqlite3", own / ".." / ".." / "state.sqlite3",
                    own / "back", own / "back" / "state.sqlite3", account):
        assert home_paths.names_live_root(str(refused)), refused
    # An own root that holds a live root, or is one, exempts nothing.
    for wide in (account, account / STORES[0], tmp_path):
        monkeypatch.setattr(home_paths, "_OWN_ROOTS", (str(wide),))
        assert home_paths.names_live_root(str(account / STORES[0] / "state.sqlite3")), wide


# --- a HOME spelled `~<account>` ---------------------------------------------------------------


def test_a_tilde_spelled_home_is_the_accounts():
    """C-20.6 (D-ST3, review r1): `~<account>`, `~` and `~/` name the account's home through pwd;
    `~<no such user>` names no one's."""
    for spelling in (f"~{ACCOUNT}", f"~{ACCOUNT}/", f"~{ACCOUNT}/.", "~", "~/", f"~{ACCOUNT}/st3-missing/.."):
        assert home_paths.is_account_home(Path(spelling)), spelling
    for spelling in ("~st3-no-such-user-r1", "~st3-no-such-user-r1/x", f"~{ACCOUNT}/st3-not-a-store"):
        assert not home_paths.is_account_home(Path(spelling)), spelling


def test_a_tilde_spelled_account_home_keeps_the_process_rule(monkeypatch, production_process):
    """C-20.6 (D-ST3, review r1): under the seam, HOME `~<account>` is the account's: the rule stands.

    At `aa74cd62a` `is_account_home` was False for it, so the waiver held, and
    `codex_app_home()` returned `~<account>/.codex`, which expanduser makes live.
    """
    for name in (PYTEST_MARKER, "PYTEST_CURRENT_TEST"):
        monkeypatch.delenv(name, raising=False)
    for spelling in (f"~{ACCOUNT}", f"~{ACCOUNT}/", f"~{ACCOUNT}/.codex"):
        monkeypatch.setenv("HOME", spelling)
        assert home_paths.pytest_process() and home_paths.under_pytest(), spelling


@pytest.mark.parametrize("function,variable", [
    (home_paths.codex_app_home, "SUBFLEET_CODEX_APP_HOME"),
    (home_paths.desktop_login_path, "SUBFLEET_DESKTOP_LOGIN"),
    (transcripts.claude_dir, "SUBFLEET_CLAUDE_DIR"),
], ids=["codex-app", "desktop-login", "transcripts-home"])
def test_a_tilde_spelled_account_home_refuses_each_default(function, variable, sealed, monkeypatch,
                                                           production_process):
    """C-20.6 (D-ST3, review r1): with HOME `~<account>`, each default refuses, waiver or not."""
    for name in (PYTEST_MARKER, "PYTEST_CURRENT_TEST", variable):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", f"~{ACCOUNT}")
    with pytest.raises(RuntimeError, match=variable):
        function()
    assert sealed.touched == []


@pytest.mark.parametrize("spelling", [f"~{ACCOUNT}/{STORES[2]}", f"~{ACCOUNT}/{STORES[1]}/x", "~/" + STORES[2],
                                      str(PERSONAL / STORES[2]), str(PERSONAL / STORES[0] / "h")])
def test_a_home_inside_a_live_root_is_refused(spelling, sealed, monkeypatch):
    """C-20.6 (D-ST3, review r1): with HOME inside a live root, `protected_provider_homes` refuses
    rather than return a path inside it (at `aa74cd62a`: `~<account>/.codex/.claude`), and so does
    `production_state_root`."""
    monkeypatch.setenv("HOME", spelling)
    with pytest.raises(RuntimeError, match="HOME names one of the account's live stores"):
        home_paths.protected_provider_homes()
    with pytest.raises(RuntimeError, match="~/.subfleet"):
        home_paths.production_state_root()
    assert sealed.touched == []


# --- `compat.v1_binary`'s PATH fallback ----------------------------------------------------------


def test_the_v1_path_fallback_is_refused(tmp_path, sealed, monkeypatch):
    """C-20.6 (D-ST3, review r1): with the v1 setting at a missing file, PATH's `subfleet` is a default.

    At `aa74cd62a` it returned PATH's `subfleet`: on the person's PATH, minus
    this venv, a wrapper that runs the live release. Here PATH holds a stand-in.
    """
    standin = tmp_path / "bin" / "subfleet"
    standin.parent.mkdir()
    standin.write_text("#!/bin/sh\nexit 9\n")
    standin.chmod(0o755)
    searched = []
    which = compat.shutil.which
    monkeypatch.setattr(compat.shutil, "which", lambda name, *a, **k: searched.append(name) or which(name, *a, **k))
    monkeypatch.setenv(compat.V1_BIN_ENV, str(tmp_path / "missing" / "subfleet"))
    monkeypatch.setenv("PATH", str(standin.parent))
    with pytest.raises(RuntimeError, match=compat.V1_BIN_ENV):
        compat.v1_binary()
    assert searched == [] and sealed.touched == []


def test_a_named_v1_binary_is_still_found(tmp_path, monkeypatch):
    """C-20.6 (D-ST3): a test that names its own v1 binary gets it."""
    named = tmp_path / "v1" / "subfleet"
    named.parent.mkdir()
    named.write_text("#!/bin/sh\n")
    named.chmod(0o755)
    monkeypatch.setenv(compat.V1_BIN_ENV, str(named))
    assert compat.v1_binary() == str(named)


# --- the property ------------------------------------------------------------------------------


SESSION = dict(os.environ)
# A fixed universe, the same in every process (invariant 5): every name a
# guarded function reads (the round-1 sweep in the report), the session's
# markers and isolated paths, and decoys. A name the session holds may take the
# session's value. It was the session's own names in a set, whose order follows
# the per-process string hash, so a derandomized run drew different inputs
# under each PYTHONHASHSEED (review r1's P1).
NAMES = tuple(sorted({
    "HOME", "PATH", "TMPDIR", "USER", "LOGNAME", "SHELL", "PWD", "XDG_CONFIG_HOME", "CODEX_HOME", "AI_AGENT",
    "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "PYTHONPATH", "SSH_AUTH_SOCK", "SUBFLEET_GUARD_TRUST",
    "SUBFLEET_CODEX_GUARD_CACHE", "SUBFLEET_DAEMON_BIN", "SUBFLEET_HOOK_COMMAND", "PYTEST_CURRENT_TEST",
    PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID, *SAFE_HOME_PATHS, *SAFE_ENV_REMOVALS,
}))
JUNK = st.one_of(
    st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), max_size=10),
    st.sampled_from(["", "1", "0", "~", "~/", "~/" + STORES[0], "~/x/../" + STORES[1], "relative/dir",
                     f"~{PERSONAL.name}/{STORES[2]}", "/nonexistent/st3", "/", ".", "..",
                     "tests/unit/x.py::test (call)"]))
GUARDED = {
    **{case.id: case.values[1] for case in RESOLVERS},
    "production-state-root": home_paths.production_state_root,
    "protected-provider-homes": home_paths.protected_provider_homes,
    "state-root-env": lambda: client.state_root(dict(os.environ)),
    "host-credential-paths": host_push._credential_paths,
    "agent-secret": lambda: credentials.keychain_command("claude-quota-fixture@example.test",
                                                         runner=lambda *a, **k: None)[0],
    "v1-binary": compat.v1_binary,
    "guard-paths": preflight.guard_paths,
    "guard-cache": preflight.cache_dir,
    "lane-codex-home": lambda: preflight._lane_home(None),
    "temp-roots": lambda: sorted(retention_git.temp_roots()),
}


def live_spellings() -> list[str]:
    """Values that name the account's home, a live root or a path inside one, by `~` or otherwise."""
    spellings = ["~", "~/", "~/" + STORES[0], f"~{ACCOUNT}", f"~{ACCOUNT}/{STORES[1]}", f"~{ACCOUNT}/st3-elsewhere",
                 str(PERSONAL), str(PERSONAL / STORES[2]), str(PERSONAL / STORES[0] / "state.sqlite3"),
                 f"{PERSONAL}/st3-missing/../{STORES[3]}", f"//{PERSONAL}/{STORES[1]}/x"]
    if sys.platform == "darwin":
        spellings += ["/System/Volumes/Data" + str(PERSONAL / STORES[0]), str(PERSONAL / STORES[1]).upper()]
    return spellings


# Each name a guarded function reads as a path may also take a live spelling.
PATH_NAMES = frozenset({*SAFE_HOME_PATHS, "CODEX_HOME", "XDG_CONFIG_HOME", "TMPDIR", "SUBFLEET_GUARD_TRUST",
                        "SUBFLEET_CODEX_GUARD_CACHE", "SUBFLEET_DAEMON_BIN", "CLAUDE_LANE_AGENT_SECRET",
                        "SUBFLEET_DEV_APP_EXECUTABLE"})
# HOME may also be spelled `~<account>` or lie inside a live root.
HOME_SPELLINGS = [f"~{ACCOUNT}", f"~{ACCOUNT}/{STORES[2]}", str(PERSONAL / STORES[2]), str(PERSONAL / STORES[1] / "x")]


@st.composite
def environments(draw, scratch: Path):
    """Any subset of NAMES plus junk names, each a session or junk value.

    The same choices draw the same environment in every process: the names
    are drawn as a list from a fixed tuple, never as a set, and every name has
    the same options whatever the session holds (`""` stands in for a value
    the session lacks), so only a session value itself differs.
    """
    env = {}
    for name in draw(st.lists(st.sampled_from(NAMES), unique=True, max_size=len(NAMES))):
        options = [JUNK, st.just(SESSION.get(name, ""))]
        if name == "HOME":
            options += [st.sampled_from(account_spellings() + HOME_SPELLINGS), st.just(str(scratch / "home"))]
        if name in SAFE_HOME_PATHS:
            options.append(st.just(str(scratch / "chosen" / name)))
        if name in PATH_NAMES:
            options.append(st.sampled_from(live_spellings()))
        env[name] = draw(st.one_of(*options))
    for name, value in draw(st.dictionaries(st.from_regex(r"[A-Z_][A-Z0-9_]{0,8}", fullmatch=True), JUNK,
                                            max_size=3)).items():
        env.setdefault(name, value)
    return env


def names_an_account_store(path) -> bool:
    """Whether `path`, as its caller would expand it, is at or under an account store.

    Walks from the root and judges a component named like a store by its parent
    alone: that parent's spelling, else its identity. It stops at the first
    match, so it never stats beneath a store.
    """
    text = os.path.expanduser(os.fsdecode(path))
    if any(_within(folded, OWN) for folded in _spellings(os.path.abspath(text))):
        return False                     # this process's own code, such as its .venv's scripts
    parts = os.path.abspath(text).split("/")
    personal = os.stat(PERSONAL)
    for index, part in enumerate(parts):
        if part.casefold() not in STORES:
            continue
        parent = "/".join(parts[:index]) or "/"
        if set(_spellings(parent)) & set(_spellings(str(PERSONAL))):
            return True
        try:
            found = os.stat(parent)
        except (OSError, ValueError):
            continue
        if (found.st_dev, found.st_ino) == (personal.st_dev, personal.st_ino):
            return True
    return False


def resolved(function):
    """The paths a guarded function returns, or [] when it refuses or fails."""
    try:
        value = function()
    except Exception:          # a refusal, or junk the function rejects; spies raise BaseException
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    return [item for item in values if isinstance(item, (str, os.PathLike))]


PROPERTY = settings(max_examples=int(os.environ.get("SUBFLEET_ST3_EXAMPLES", "150")), deadline=None,
                    database=None, derandomize=True,
                    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])


def assert_no_store(environ, scratch: Path) -> None:
    """With os.environ `environ`, no guarded function returns a path at or under an account store."""
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(scratch)         # a relative junk value is judged under the test's own tree
        replace_environ(patch, environ)
        for name, function in GUARDED.items():
            for path in resolved(function):
                assert not names_an_account_store(path), (name, sorted(environ), os.fspath(path))


@PROPERTY
@given(data=st.data())
def test_no_guarded_function_returns_an_account_store_whatever_the_environment(tmp_path, sealed, data):
    """C-20.6 (D-ST3): over environments of session names and junk, no guarded function names a store."""
    assert_no_store(data.draw(environments(tmp_path)), tmp_path)
    assert sealed.touched == []


# Review r1's P1: the property failed on each at `aa74cd62a`, depending on the
# hash seed, because `global_config_path` read CLAUDE_CONFIG_DIR raw.
REVIEW_MCP_INPUTS = {
    "config-dir-tilde": {"CLAUDE_CONFIG_DIR": "~"},
    "with-ai-agent": {"CLAUDE_CONFIG_DIR": "~", "AI_AGENT": "claude-code"},
    "with-account-home": {"CLAUDE_CONFIG_DIR": "~", "HOME": str(PERSONAL)},
    "tilde-slash-with-path": {"CLAUDE_CONFIG_DIR": "~/", "PATH": "/usr/bin:/bin"},
}


@pytest.mark.parametrize("environ", REVIEW_MCP_INPUTS.values(), ids=REVIEW_MCP_INPUTS)
def test_the_reviews_mcp_config_inputs_name_no_store(environ, tmp_path, sealed, monkeypatch):
    """C-20.6 (D-ST3, review r1's P1): `global_config_path` refuses a `~` CLAUDE_CONFIG_DIR.

    It read the override raw, so `~` skipped the tilde refusal and it returned
    `~/.claude.json`, which `os.path.expanduser` makes the live file.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        replace_environ(patch, environ)
        with pytest.raises(RuntimeError, match="CLAUDE_CONFIG_DIR begins with ~"):
            global_config_path()
        with pytest.raises(RuntimeError, match="CLAUDE_CONFIG_DIR begins with ~"):
            global_config_path(dict(environ))
    assert_no_store(environ, tmp_path)
    assert sealed.touched == []


def _app_executables(root):
    from subfleet.conversations.service import ConversationService
    return ConversationService._app_executables(SimpleNamespace(root=root))


# Every raw read of a path-valued variable the round-1 sweep routed through
# `home_paths.checked_override` (the report's resolver table).
ROUTED_READS = {
    "CLAUDE_CONFIG_DIR": lambda tmp: global_config_path(),
    "SUBFLEET_GUARD_TRUST": lambda tmp: preflight.guard_paths(tmp),
    "SUBFLEET_CODEX_GUARD_CACHE": lambda tmp: preflight.cache_dir(tmp),
    "CODEX_HOME": lambda tmp: preflight._lane_home(None),
    "CLAUDE_LANE_AGENT_SECRET": lambda tmp: credentials.keychain_command("claude-quota-fixture@example.test",
                                                                         runner=lambda *a, **k: None),
    "SUBFLEET_DAEMON_BIN": lambda tmp: cli._daemond_argv(tmp),
    "SUBFLEET_DEV_APP_EXECUTABLE": _app_executables,
    "TMPDIR": lambda tmp: retention_git.temp_roots(),
}


@pytest.mark.parametrize("variable", ROUTED_READS)
@pytest.mark.parametrize("value", ["tilde-user", "live-root", "home"])
def test_each_raw_path_read_takes_the_override_refusals(variable, value, tmp_path, sealed, monkeypatch):
    """C-20.6 (D-ST3, review r1's P1 swept): each raw read refuses as a `home_path` override would."""
    spelled = {"tilde-user": f"~{ACCOUNT}/{STORES[0]}", "live-root": str(PERSONAL / STORES[2] / "x"),
               "home": str(PERSONAL)}[value]
    monkeypatch.setenv(variable, spelled)
    with pytest.raises(RuntimeError, match=variable):
        ROUTED_READS[variable](tmp_path)
    assert sealed.touched == []


@pytest.mark.parametrize("variable", ROUTED_READS)
def test_each_raw_path_read_keeps_a_tests_own_path(variable, tmp_path, monkeypatch):
    """C-20.6 (D-ST3): the same reads take a test's own path, as they did."""
    monkeypatch.setenv(variable, str(tmp_path / "own"))
    ROUTED_READS[variable](tmp_path)


def test_the_property_oracle_finds_the_account_stores(tmp_path):
    """C-20.6 (D-ST3): the oracle is not vacuous; it names each store by each spelling, nothing else."""
    for spelling in account_spellings():
        for store in STORES:
            assert names_an_account_store(f"{spelling}/{store}"), (spelling, store)
            assert names_an_account_store(f"{spelling}/{store}/inner"), (spelling, store)
    assert not names_an_account_store(tmp_path / ".subfleet")
    assert not names_an_account_store(PERSONAL / "not-a-store")
    assert not names_an_account_store(PERSONAL / "x" / ".subfleet")


def connect_unix(path):
    with socket.socket(socket.AF_UNIX) as unix:
        unix.connect(str(path))


def test_the_spies_refuse_before_the_call(monkeypatch):
    """C-20.6 (D-ST3): the spies are not vacuous; each refuses a live spelling before its call."""
    target = PERSONAL / STORES[0] / "state.sqlite3"
    calls = [lambda: os.stat(target), lambda: os.chmod(target.parent, 0o700), lambda: open(target, "ab"),
             lambda: sqlite3.connect(target.as_uri() + "?mode=ro", uri=True),
             lambda: sqlite3.connect(str(target)), lambda: os.mkdir(target.parent),
             lambda: (PERSONAL / STORES[1] / "projects").resolve(), lambda: os.stat(PERSONAL / "x" / ".." / STORES[2]),
             lambda: connect_unix(target.parent / "daemon.sock"),
             lambda: subprocess.Popen(["/usr/bin/true"])]
    with sealing(monkeypatch) as seal:
        for call in calls:
            with pytest.raises(pytest.fail.Exception):
                call()
        assert len(seal.touched) == len(calls) - 1 and len(seal.launched) == 1
        assert not reaches_live(REPO / "pyproject.toml")

