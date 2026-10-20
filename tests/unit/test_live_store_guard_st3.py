"""D-ST3: a pytest process never resolves the account's live roots, whatever os.environ holds.

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

from subfleet import cli, client, compat, credentials, home_paths, host_push, prune_decisions
from subfleet.home_paths import PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID, SAFE_HOME_PATHS
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
    """#222 round 5's shape: `cmd_daemon_start` with os.environ a plain dict.

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
    replace_environ(monkeypatch, environ)
    with pytest.raises(RuntimeError, match="~/.subfleet"):
        home_paths.production_state_root()
    assert sealed.touched == []


@pytest.mark.parametrize("environ", ENVIRONS.values(), ids=ENVIRONS)
@pytest.mark.parametrize("variable,resolver,suffix", RESOLVERS)
def test_every_resolver_refuses_its_default(environ, variable, resolver, suffix, sealed, monkeypatch):
    """`home_path` with each production default: every D-ST1 resolver refuses."""
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
    replace_environ(monkeypatch, environ)
    with pytest.raises(RuntimeError, match=variable):
        function()
    assert sealed.touched == []


@pytest.mark.parametrize("environ", ENVIRONS.values(), ids=ENVIRONS)
def test_the_v1_install_default_is_refused(environ, sealed, monkeypatch):
    """Step 3's fallback: `compat.v1_binary` defaulted to the person's v1 install."""
    replace_environ(monkeypatch, environ)
    with pytest.raises(RuntimeError, match=compat.V1_BIN_ENV):
        compat.v1_binary()
    assert sealed.touched == []


@pytest.mark.parametrize("environ", ENVIRONS.values(), ids=ENVIRONS)
def test_prune_decisions_state_root_default_is_refused(environ, sealed, monkeypatch):
    """Step 3's fallback: `python -m subfleet.prune_decisions` defaulted to ~/.subfleet."""
    replace_environ(monkeypatch, environ)
    with pytest.raises(RuntimeError, match="SUBFLEET_HOME"):
        prune_decisions.main(["--no-report"])
    assert sealed.touched == []


def test_prune_decisions_state_root_default_keeps_the_environment(tmp_path, monkeypatch):
    """Outside a refusal the default is unchanged: SUBFLEET_HOME, read once."""
    seen = []
    monkeypatch.setattr(prune_decisions, "prune", lambda root, **_: seen.append(root) or pytest.fail("stop"))
    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path / "chosen"))
    with pytest.raises(pytest.fail.Exception, match="stop"):
        prune_decisions.main(["--no-report"])
    assert seen == [str(tmp_path / "chosen")]


# --- the process rule ---------------------------------------------------------------------


@pytest.mark.parametrize("environ", ENVIRONS.values(), ids=ENVIRONS)
def test_the_process_rule_holds_whatever_the_environment(environ, monkeypatch):
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
    """Subprocesses keep today's marker rule; one that imports pytest is a test process."""
    env = {**os.environ, "PYTHONPATH": str(REPO), "HOME": str(tmp_path / "home")}
    if markers == "none":
        for name in (PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID, "PYTEST_CURRENT_TEST"):
            env.pop(name, None)
    done = subprocess.run([sys.executable, "-c", PROCESS_PROBE, imports], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.splitlines()[-1])
    assert out == {"loaded": imports == "pytest", "under": imports == "pytest" or markers == "live"}


def test_no_subfleet_module_imports_pytest():
    """A production process could hold pytest's modules only by importing pytest."""
    found = []
    for path in sorted((REPO / "subfleet").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
            elif (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant)
                  and isinstance(node.func, (ast.Name, ast.Attribute))
                  and getattr(node.func, "id", getattr(node.func, "attr", "")) in ("import_module", "__import__")):
                names = [str(node.args[0].value)]
            found += [f"{path.relative_to(REPO)}:{node.lineno}" for name in names
                      if name.split(".")[0] in ("pytest", "_pytest")]
    assert found == []


# --- the one seam ----------------------------------------------------------------------------


def test_the_seam_waives_the_process_rule_only_while_home_is_not_the_accounts(tmp_path, monkeypatch,
                                                                                production_process):
    """The seam turns the process rule off for a test HOME and never for the account's."""
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
    replace_environ(monkeypatch, {"PATH": "/usr/bin:/bin", "HOME": str(PERSONAL)})
    with pytest.raises(RuntimeError, match="~/.subfleet"):
        home_paths.production_state_root()
    with pytest.raises(RuntimeError, match="SUBFLEET_CODEX_APP_HOME"):
        home_paths.codex_app_home()
    assert sealed.touched == []


def test_the_seam_restores_production_defaults_under_a_test_home(tmp_path, monkeypatch, production_process):
    """What the seam is for: a test_only default outside a session, under its own HOME."""
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
    """`home/st3-missing/..` read as a test HOME at release: resolve() then built ~/.subfleet."""
    monkeypatch.setenv("HOME", spelling)
    assert home_paths.is_account_home(Path(spelling))
    with pytest.raises(RuntimeError, match="~/.subfleet"):
        home_paths.production_state_root()
    assert home_paths.protected_provider_homes() == [Path(os.environ["CLAUDE_CONFIG_DIR"]),
                                                    Path(os.environ["SUBFLEET_CODEX_APP_HOME"])]
    assert sealed.touched == []


def test_a_home_spelled_through_a_link_past_a_missing_component_is_the_accounts(tmp_path, sealed, monkeypatch):
    """No stat follows `missing/../link`, but Path.resolve builds on the link's target.

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


@pytest.mark.parametrize("variable", ["SUBFLEET_HOME", "SUBFLEET_CLAUDE_DIR", "SUBFLEET_CODEX_APP_HOME"])
@pytest.mark.parametrize("home", ["unset", "account"])
def test_a_tilde_override_naming_the_accounts_home_is_refused(variable, home, sealed, monkeypatch):
    """`~/...` expands through HOME, and without one through pwd, to the account's home."""
    if home == "unset":
        monkeypatch.delenv("HOME")
    else:
        monkeypatch.setenv("HOME", str(PERSONAL))
    monkeypatch.setenv(variable, "~/" + STORES[0])
    with pytest.raises(RuntimeError, match=variable):
        home_paths.home_path(variable, lambda: pytest.fail("default"), expand_override=True)
    assert sealed.touched == []


def test_a_tilde_override_under_a_test_home_is_the_tests(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SUBFLEET_HOME", "~/chosen")
    assert client.state_root() == tmp_path / "home" / "chosen"


# --- the property ------------------------------------------------------------------------------


SESSION = dict(os.environ)
NAMES = sorted({*SESSION, "HOME", "PATH", "PYTEST_CURRENT_TEST", PYTEST_MARKER, PYTEST_SESSION,
                PYTEST_SESSION_ID, *SAFE_HOME_PATHS})
JUNK = st.one_of(
    st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), max_size=10),
    st.sampled_from(["", "1", "0", "~", "~/", "~/" + STORES[0], "~/x/../" + STORES[1], "relative/dir",
                     "/nonexistent/st3", "/", ".", "..", "tests/unit/x.py::test (call)"]))
GUARDED = {
    **{case.id: case.values[1] for case in RESOLVERS},
    "production-state-root": home_paths.production_state_root,
    "protected-provider-homes": home_paths.protected_provider_homes,
    "state-root-env": lambda: client.state_root(dict(os.environ)),
    "host-credential-paths": host_push._credential_paths,
    "agent-secret": lambda: credentials.keychain_command("claude-quota-fixture@example.test",
                                                         runner=lambda *a, **k: None)[0],
    "v1-binary": compat.v1_binary,
}


@st.composite
def environments(draw, scratch: Path):
    """Any subset of the session's names plus junk names, each a session or junk value."""
    env = {}
    for name in draw(st.sets(st.sampled_from(NAMES), max_size=len(NAMES))):
        options = [JUNK]
        if name in SESSION:
            options.append(st.just(SESSION[name]))
        if name == "HOME":
            options += [st.sampled_from(account_spellings()), st.just(str(scratch / "home"))]
        if name in SAFE_HOME_PATHS:
            options.append(st.just(str(scratch / "chosen" / name)))
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


@PROPERTY
@given(data=st.data())
def test_no_guarded_function_returns_an_account_store_whatever_the_environment(tmp_path, sealed, data):
    environ = data.draw(environments(tmp_path))
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)        # a relative junk value is judged under the test's own tree
        replace_environ(patch, environ)
        for name, function in GUARDED.items():
            for path in resolved(function):
                assert not names_an_account_store(path), (name, sorted(environ), os.fspath(path))
    assert sealed.touched == []


def test_the_property_oracle_finds_the_account_stores(tmp_path):
    """The oracle is not vacuous: it names each store by each spelling, and nothing else."""
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
    """The spies are not vacuous: each refuses a live spelling before its call."""
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

