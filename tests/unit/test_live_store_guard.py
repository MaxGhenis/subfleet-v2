"""C-23.28: undoing test patches can never expose personal home stores."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from subfleet import client, home_paths, hooks, notify_push, cli, credentials
from subfleet import importer, lanes_transfer
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.adapters.claude_mcp import global_config_path
from subfleet.guard.preflight import _state_root
from subfleet.sessions import desktop, mirror, revive, transcripts
from subfleet.home_paths import (
    KEYCHAIN_READERS, PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID,
    SAFE_ENV_REMOVALS, SAFE_HOME_PATHS,
)
from tests.conftest import pytest_configure, pytest_unconfigure

REPO = Path(__file__).resolve().parents[2]
RESOLVERS = [
    pytest.param("SUBFLEET_SESSION_STORE", mirror.store_dir, "", id="mirror-store"),
    pytest.param("SUBFLEET_SESSION_STORE", revive.session_store_dir, "", id="revive-store"),
    pytest.param("SUBFLEET_DESKTOP_LOG", desktop.log_path, "", id="desktop-log"),
    pytest.param("SUBFLEET_CLAUDE_DIR", transcripts.claude_dir, "", id="transcripts-home"),
    pytest.param("SUBFLEET_CLAUDE_DIR", transcripts.projects_dir, "projects", id="transcripts-projects"),
    pytest.param("SUBFLEET_CLAUDE_DIR", mirror.projects_dir, "projects", id="mirror-projects"),
    pytest.param("SUBFLEET_CLAUDE_DIR", notify_push.claude_dir, "", id="notify-home"),
    pytest.param("SUBFLEET_CLAUDE_DIR", notify_push.sessions_dir, "sessions", id="notify-sessions"),
    pytest.param("SUBFLEET_HOME", client.state_root, "", id="client-state"),
    pytest.param("SUBFLEET_HOME", _state_root, "", id="preflight-state"),
    pytest.param("CLAUDE_CONFIG_DIR", lambda: ClaudeAdapter()._config_projects_dir(),
                 "projects", id="adapter-projects"),
    pytest.param("CLAUDE_CONFIG_DIR", global_config_path, ".claude.json", id="mcp-config"),
    pytest.param("SUBFLEET_CLAUDE_SETTINGS", hooks.settings_path, "", id="hook-settings"),
    pytest.param("SUBFLEET_CODEX_APP_HOME", home_paths.codex_app_home, "", id="codex-app"),
    pytest.param("SUBFLEET_DESKTOP_LOGIN", home_paths.desktop_login_path, "", id="desktop-login"),
    pytest.param("SUBFLEET_PROVIDER_HOME", home_paths.provider_home, "", id="legacy-provider-home"),
    pytest.param("SUBFLEET_LAUNCHD_PLIST", cli.launchd_plist_path, "", id="launchd-plist"),
]

# The original import-time constants remain production defaults. Test seams
# are selected at the read sites, before any legacy directory is inspected.
RESOLVERS += [
    pytest.param(variable, lambda variable=variable, default=default:
                 Path(home_paths.home_path(variable, lambda: default, test_only=True)), "", id=name)
    for variable, default, name in [
        ("SUBFLEET_V1_STATE", importer.V1_STATE, "legacy-state"),
        ("SUBFLEET_DELEGATE_STATE", importer.DELEGATE_STATE, "legacy-delegate"),
        ("SUBFLEET_V1_ROSTER_DIR", importer.V1_ROSTER_DIR, "legacy-roster"),
        ("SUBFLEET_V1_LAUNCH_AGENTS", lanes_transfer.LAUNCH_AGENTS, "legacy-agents"),
        ("SUBFLEET_HOST_HOME", Path("/nonexistent"), "host-home"),
        ("SUBFLEET_HOST_CONFIG", Path("/nonexistent"), "host-config"),
        ("SUBFLEET_RESET_INHIBIT", Path("/nonexistent"), "reset-hold"),
    ]
]


def outcome(call):
    try:
        return ("value", call())
    except Exception as error:
        return ("exception", type(error).__name__, str(error))


def guard_calls(source, module):
    """Resolve Python import spellings before inspecting guard calls."""
    tree = ast.parse(source)
    package = module.rpartition(".")[0]
    bindings = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            imported = node.module or ""
            if node.level:
                imported = importlib.util.resolve_name("." * node.level + imported, package)
            for alias in node.names:
                bindings[alias.asname or alias.name] = imported + "." + alias.name
    # Calls within home_paths itself have no import binding.
    if module == "subfleet.home_paths":
        bindings["home_path"] = module + ".home_path"
    if module == "subfleet.credentials":
        bindings["keychain_command"] = module + ".keychain_command"

    def qualified(node):
        if isinstance(node, ast.Name):
            return bindings.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return qualified(node.value) + "." + node.attr
        return ""

    constants = {node.targets[0].id: node.value.value for node in tree.body
                 if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                 and isinstance(node.value, ast.Constant)}
    variables = set()
    readers = set()

    def visit(node, owner):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            owner += "." + node.name
        if isinstance(node, ast.Call):
            name = qualified(node.func)
            if name == "subfleet.home_paths.home_path":
                argument = node.args[0] if node.args else next(
                    keyword.value for keyword in node.keywords if keyword.arg == "variable")
                variable = argument.value if isinstance(argument, ast.Constant) else constants[argument.id]
                assert variable in SAFE_HOME_PATHS, (module, node.lineno, variable)
                variables.add(variable)
            elif name == "subfleet.credentials.keychain_command":
                readers.add(owner)
        for child in ast.iter_child_nodes(node):
            visit(child, owner)

    visit(tree, module)
    return variables, readers


def test_registry_covers_every_production_guard():
    """Adding a guard variable must also isolate it in fixtures and children."""
    variables, readers = set(), set()
    for path in (REPO / "subfleet").rglob("*.py"):
        module = ".".join(path.relative_to(REPO).with_suffix("").parts)
        found_variables, found_readers = guard_calls(path.read_text(), module)
        variables.update(found_variables)
        readers.update(found_readers)
    assert variables == set(SAFE_HOME_PATHS)
    assert readers == KEYCHAIN_READERS


@pytest.mark.parametrize("import_line,call", [
    ("from subfleet.home_paths import home_path", "home_path"),
    ("from subfleet import home_paths", "home_paths.home_path"),
    ("from subfleet.home_paths import home_path as guarded_path", "guarded_path"),
    ("from .home_paths import home_path as hp", "hp"),
    ("import subfleet.home_paths as paths", "paths.home_path"),
    ("import subfleet.home_paths", "subfleet.home_paths.home_path"),
    ("from . import home_paths as paths", "paths.home_path"),
])
def test_registry_rejects_unregistered_import_spellings(import_line, call):
    """Reviewer's direct, qualified and aliased scratch inputs use the real check."""
    source = (import_line + '\ndef new_resolver():\n    return ' + call +
              '("SUBFLEET_REVIEW_UNREGISTERED", lambda: "/fake-personal-store")\n')
    with pytest.raises(AssertionError, match="SUBFLEET_REVIEW_UNREGISTERED"):
        guard_calls(source, "subfleet.review_scratch")


@pytest.mark.parametrize("import_line,call", [
    ("from subfleet import home_paths", "home_paths.home_path"),
    ("from .home_paths import home_path as hp", "hp"),
])
def test_registry_counts_registered_import_spellings(import_line, call):
    source = (import_line + '\ndef resolver():\n    return ' + call +
              '("SUBFLEET_HOME", lambda: "/fake-personal-store")\n')
    assert guard_calls(source, "subfleet.review_scratch") == ({"SUBFLEET_HOME"}, set())


# This executes during actual pytest collection, before any autouse fixture or
# PYTEST_CURRENT_TEST exists. Record both the installed baseline and refusal.
COLLECTION_OUTCOMES = {}
for case in RESOLVERS:
    variable, resolver, suffix = case.values
    override = os.environ.get(variable)
    resolved = outcome(lambda: Path(resolver()))
    try:
        os.environ.pop(variable, None)
        missing = outcome(resolver)
    finally:
        if override is not None:
            os.environ[variable] = override
    COLLECTION_OUTCOMES[case.id] = (override, resolved, missing,
                                     "PYTEST_CURRENT_TEST" in os.environ,
                                     PYTEST_MARKER in os.environ)


@pytest.mark.parametrize("variable,resolver,suffix", RESOLVERS)
def test_collection_phase_is_isolated(request, variable, resolver, suffix):
    case_id = request.node.callspec.id
    override, resolved, missing, current_test, session_marker = COLLECTION_OUTCOMES[case_id]
    expected = request.config._subfleet_safe_home_base / SAFE_HOME_PATHS[variable]
    assert not current_test
    assert session_marker
    assert override == str(expected)
    assert resolved == ("value", expected / suffix)
    assert missing[0:2] == ("exception", "RuntimeError")
    assert variable in missing[2]


@pytest.mark.parametrize("variable,resolver,suffix", RESOLVERS)
@pytest.mark.parametrize("missing", [False, True], ids=["override", "refusal"])
def test_configure_phase_is_isolated(monkeypatch, variable, resolver, suffix, missing):
    from types import SimpleNamespace
    config = SimpleNamespace()
    with monkeypatch.context() as phase:
        # Track all direct hook writes so this simulation restores the enclosing
        # session's baseline. No test-current marker exists during configure.
        for name in SAFE_HOME_PATHS:
            phase.setenv(name, os.environ[name])
        phase.delenv("PYTEST_CURRENT_TEST", raising=False)
        phase.delenv(PYTEST_MARKER, raising=False)
        phase.setattr(Path, "home", lambda: pytest.fail("personal HOME was evaluated"))
        try:
            pytest_configure(config)
            assert PYTEST_MARKER in os.environ
            if missing:
                phase.delenv(variable)
                with pytest.raises(RuntimeError, match=variable):
                    resolver()
            else:
                expected = config._subfleet_safe_home_base / SAFE_HOME_PATHS[variable]
                assert Path(resolver()) == expected / suffix
        finally:
            pytest_unconfigure(config)


@pytest.mark.parametrize("variable,resolver,suffix", RESOLVERS)
@pytest.mark.parametrize("patch", ["plain", "setenv", "delenv", "delenv-missing"])
def test_undo_restores_session_safe_path(request, tmp_path, monkeypatch, variable, resolver, suffix, patch):
    """C-23.28: the autouse fixture and a test share one undo stack."""
    per_test = os.environ[variable]
    if patch == "setenv":
        monkeypatch.setenv(variable, str(tmp_path / "test-override"))
    elif patch == "delenv":
        monkeypatch.delenv(variable)
    elif patch == "delenv-missing":
        monkeypatch.delenv(variable, raising=False)
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.undo()
    base = request.config._subfleet_safe_home_base
    expected = base / SAFE_HOME_PATHS[variable]
    # Assert the environment first: even a broken first layer cannot make this
    # regression read a real MCP config before it fails.
    assert os.environ.get(variable) == str(expected)
    assert per_test != str(expected)
    path = Path(resolver())
    assert path == expected / suffix
    assert path.is_relative_to(base)
    assert not path.is_relative_to(Path.home())
    assert not path.exists()


@pytest.mark.parametrize("variable,resolver,suffix", RESOLVERS)
def test_missing_override_refuses_fallback(tmp_path, monkeypatch, variable, resolver, suffix):
    """C-23.28: a missing override fails before the personal default is used.

    A fake HOME also keeps mutation runs harmless when this guard is removed.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.delenv(variable)
    with pytest.raises(RuntimeError, match=variable):
        resolver()


@pytest.mark.parametrize("variable,resolver,suffix", RESOLVERS)
def test_test_override_wins(tmp_path, monkeypatch, variable, resolver, suffix):
    """C-23.28: a test can explicitly select its own isolated tree."""
    chosen = tmp_path / "chosen"
    monkeypatch.setenv(variable, str(chosen))
    assert Path(resolver()) == chosen / suffix


@pytest.mark.parametrize("variable,resolver,suffix", [case for case in RESOLVERS
                                                    if case.values[1] is not _state_root])
def test_empty_override_refuses_implicit_fallback(tmp_path, monkeypatch, variable, resolver, suffix):
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv(variable, "")
    with pytest.raises(RuntimeError, match=variable):
        resolver()


@pytest.mark.parametrize("resolver,variable", [
    (lambda: client.state_root({}), "SUBFLEET_HOME"),
    (lambda: global_config_path({}), "CLAUDE_CONFIG_DIR"),
])
def test_explicit_empty_environment_also_refuses(monkeypatch, tmp_path, resolver, variable):
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    with pytest.raises(RuntimeError, match=variable):
        resolver()


@pytest.mark.parametrize("marker_variable", [PYTEST_MARKER, "PYTEST_CURRENT_TEST"])
@pytest.mark.parametrize("marker", ["", "synthetic-test (call)"])
def test_guard_never_evaluates_default(monkeypatch, marker_variable, marker):
    monkeypatch.delenv(PYTEST_MARKER, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv(marker_variable, marker)
    monkeypatch.delenv("SUBFLEET_SESSION_STORE")

    def forbidden():
        pytest.fail("the personal default was evaluated")

    with pytest.raises(RuntimeError, match="SUBFLEET_SESSION_STORE"):
        home_paths.home_path("SUBFLEET_SESSION_STORE", forbidden)


@pytest.mark.parametrize("consumer,variable", [
    ("doctor", "SUBFLEET_CLAUDE_SETTINGS"),
    ("capacity-account", "SUBFLEET_DESKTOP_LOGIN"),
    ("capacity-identity", "SUBFLEET_DESKTOP_LOGIN"),
    ("catalog-build", "SUBFLEET_CODEX_APP_HOME"),
    ("catalog-session", "SUBFLEET_CODEX_APP_HOME"),
    ("service-source", "SUBFLEET_CODEX_APP_HOME"),
    ("service-workspace-claude", "CLAUDE_CONFIG_DIR"),
    ("service-workspace-codex", "SUBFLEET_CODEX_APP_HOME"),
    ("service-app-executables", "SUBFLEET_PROVIDER_HOME"),
    ("timers-identity", "SUBFLEET_CODEX_APP_HOME"),
    ("daemon", "SUBFLEET_HOME"),
    ("importer", "SUBFLEET_HOME"),
    ("importer-home", "SUBFLEET_PROVIDER_HOME"),
    ("transfer-home", "SUBFLEET_PROVIDER_HOME"),
    ("importer-state", "SUBFLEET_V1_STATE"),
    ("importer-delegate", "SUBFLEET_DELEGATE_STATE"),
    ("importer-roster", "SUBFLEET_V1_ROSTER_DIR"),
    ("cockpit-state", "SUBFLEET_V1_STATE"),
    ("importer-cli-state", "SUBFLEET_V1_STATE"),
    ("importer-cli-delegate", "SUBFLEET_DELEGATE_STATE"),
    ("importer-cli-roster", "SUBFLEET_V1_ROSTER_DIR"),
    ("transfer-state", "SUBFLEET_V1_STATE"),
    ("transfer-roster", "SUBFLEET_V1_ROSTER_DIR"),
    ("transfer-agents", "SUBFLEET_V1_LAUNCH_AGENTS"),
])
def test_consumers_cannot_bypass_refusal(tmp_path, monkeypatch, consumer, variable):
    """C-23.28: real readers and entry points must use the guarded resolver."""
    from types import SimpleNamespace
    from subfleet import capacity, daemon, doctor, importer, lanes_transfer, timers
    from subfleet.conversations import catalog, service

    # Undo identity-reader stubs, then set every seam with a fresh undo stack.
    # This is independent of the session baseline so mutations remain safe.
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("SUBFLEET_DEV_APP_EXECUTABLE", str(tmp_path / "fake-app"))
    for name, relative in SAFE_HOME_PATHS.items():
        monkeypatch.setenv(name, str(tmp_path / "isolated" / relative))
    monkeypatch.delenv(variable)
    empty_store = SimpleNamespace(query=lambda *args: [], lane_rows=lambda: [],
                                  one=lambda *args: {"provider": "codex", "account_key": "codex:fake", "owner": "v1"})
    claude_store = SimpleNamespace(one=lambda *args: {"provider": "claude", "account_key": "claude:fake",
                                                     "owner": "v1"})
    calls = {
        "doctor": doctor.check_never_rules,
        "capacity-account": capacity.read_desktop_account,
        "capacity-identity": capacity.cached_desktop_identity,
        "catalog-build": lambda: catalog.build(tmp_path, lanes=[]),
        "catalog-session": lambda: catalog.native_session("codex", "fake", home=None, root=tmp_path, lanes=[]),
        "service-source": lambda: service.ConversationService._codex_source(
            SimpleNamespace(daemon=SimpleNamespace(store=empty_store)), None, "fake", None),
        "service-workspace-claude": lambda: service.ConversationService._check_workspace(
            SimpleNamespace(root=tmp_path, daemon=SimpleNamespace(store=empty_store)),
            "claude", str(tmp_path / "workspace"), {"permission": "accept-edits"}),
        "service-workspace-codex": lambda: service.ConversationService._check_workspace(
            SimpleNamespace(root=tmp_path, daemon=SimpleNamespace(store=empty_store)),
            "claude", str(tmp_path / "workspace"), {"permission": "accept-edits"}),
        "service-app-executables": lambda: service.ConversationService._app_executables(
            SimpleNamespace(root=tmp_path)),
        "timers-identity": lambda: timers.Timers._identities(SimpleNamespace(store=empty_store)),
        "daemon": lambda: daemon.main([]),
        "importer": lambda: importer.main([]),
        "importer-home": lambda: importer.import_v1(tmp_path / "state", v1_state=tmp_path / "v1",
            delegate_state=tmp_path / "delegate", roster_dir=tmp_path / "roster"),
        "transfer-home": lambda: lanes_transfer.plan_transfer(empty_store, tmp_path / "state", "fake", "v2",
            roster_dir=tmp_path / "roster", agents_dir=tmp_path / "agents", v1_state=tmp_path / "v1"),
        **{name: lambda: importer.import_v1(tmp_path / "state", dry_run=True, write_report=False)
           for name in ("importer-state", "importer-delegate", "importer-roster")},
        "cockpit-state": lambda: importer.import_legacy_cockpit(tmp_path / "state", dry_run=True),
        **{name: lambda: importer.main(["--state-root", str(tmp_path / "state"), "--dry-run"])
           for name in ("importer-cli-state", "importer-cli-delegate", "importer-cli-roster")},
        **{name: lambda: lanes_transfer.plan_transfer(claude_store, tmp_path / "state", "fake", "v2")
           for name in ("transfer-state", "transfer-roster")},
        "transfer-agents": lambda: lanes_transfer.v1_codex_scope(tmp_path / "providers", tmp_path / "roster"),
    }
    # If a mutation bypasses the guard, stop the entry point before any daemon
    # startup or legacy import. The changed exception still fails this test.
    monkeypatch.setattr(daemon.descriptors, "raise_open_file_limit", lambda: pytest.fail("daemon reached startup"))
    if consumer == "importer":
        monkeypatch.setattr(importer, "import_legacy_cockpit", lambda *a, **k: pytest.fail("import reached files"))
    monkeypatch.setattr(importer, "_looks_like_v1_root", lambda *a: pytest.fail("import reached files"))
    with pytest.raises(RuntimeError, match=variable):
        calls[consumer]()


def test_legacy_defaults_use_session_paths_after_undo(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    # Redirect independently of the session layer, including mutation runs.
    for name, relative in SAFE_HOME_PATHS.items():
        monkeypatch.setenv(name, str(tmp_path / "isolated" / relative))
    report = importer.import_v1(tmp_path / "state", dry_run=True, write_report=False)
    assert report.v1_state == os.environ["SUBFLEET_V1_STATE"]
    assert report.delegate_state == os.environ["SUBFLEET_DELEGATE_STATE"]
    assert report.roster_dir == os.environ["SUBFLEET_V1_ROSTER_DIR"]
    roster = Path(os.environ["SUBFLEET_V1_ROSTER_DIR"])
    roster.mkdir(parents=True)
    (roster / "claude-accounts.json").write_text('{"enrolled":{"fixture@example.test":"fixture"}}')
    row = {"lane_id": "fixture", "provider": "claude", "account_key": "claude:fixture@example.test",
           "credential_ref": "fixture", "credential_kind": "env", "credential_epoch": 1,
           "home": None, "owner": "v1", "desktop": False, "enabled": True}
    store = SimpleNamespace(one=lambda *args: row)
    plan = lanes_transfer.plan_transfer(store, tmp_path / "state", "fixture", "v2")
    assert plan.edits[-1].path == roster / "claude-accounts.json"
    assert not plan.live_v1_runs


def test_e2e_child_has_every_guard_variable(tmp_path, monkeypatch):
    from tests.e2e.conftest import E2E
    monkeypatch.setattr(E2E, "init_repo", lambda self: None)
    harness = E2E(tmp_path)
    assert set(SAFE_HOME_PATHS) <= harness.env.keys()
    assert harness.env[PYTEST_MARKER] == "1"
    assert harness.env[PYTEST_SESSION] == os.environ[PYTEST_SESSION]
    assert harness.env[PYTEST_SESSION_ID] == os.environ[PYTEST_SESSION_ID]
    for name in SAFE_HOME_PATHS:
        assert Path(harness.env[name]).is_relative_to(tmp_path)
    source = '''
from subfleet import capacity, home_paths
from subfleet.adapters.claude import ClaudeAdapter
from pathlib import Path
import os
assert set(home_paths.SAFE_HOME_PATHS) <= os.environ.keys()
assert capacity.read_desktop_account() is None
assert home_paths.desktop_login_path() == home_paths.Path(os.environ["SUBFLEET_DESKTOP_LOGIN"])
assert home_paths.codex_app_home() == home_paths.Path(os.environ["SUBFLEET_CODEX_APP_HOME"])
assert ClaudeAdapter()._config_projects_dir() == Path(os.environ["CLAUDE_FAKE_PROJECTS_DIR"])
'''
    result = subprocess.run([sys.executable, "-c", source], env=harness.env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["absent", "empty", "absolute", "relative", "tilde", "legacy-mcp",
                                  "literal-tilde-home"])
@pytest.mark.parametrize("home_name", ["fake-home", "fake home with spaces", "~/fake-home"])
def test_outside_pytest_matches_release_217(tmp_path, mode, home_name):
    """C-23.28: execute the original release functions and ours in a child.

    No pytest marker, no real HOME, no Git/history dependency at test time.
    """
    env = {name: value for name, value in os.environ.items()
           if name not in SAFE_HOME_PATHS and name not in ("PYTEST_CURRENT_TEST", PYTEST_MARKER)}
    fake_home = tmp_path / home_name
    fake_home.mkdir(parents=True)
    env["HOME"] = home_name if home_name.startswith("~") else str(fake_home)
    env["PYTHONPATH"] = str(REPO)
    if mode == "literal-tilde-home":
        # A literal tilde in HOME must not be expanded a second time. All
        # resulting relative paths remain beneath this child's temporary cwd.
        (tmp_path / "~" / "fake-home").mkdir(parents=True, exist_ok=True)
        env["HOME"] = "~/fake-home"
    if mode not in ("absent", "legacy-mcp", "literal-tilde-home"):
        chosen = {"empty": "", "absolute": str(tmp_path / "configured"),
                  "relative": "relative-config", "tilde": "~/configured"}[mode]
        env.update({name: chosen for name in SAFE_HOME_PATHS})
    if mode == "legacy-mcp":
        legacy = fake_home / ".claude" / ".config.json"
        legacy.parent.mkdir()
        legacy.write_text("{}")
    source = textwrap.dedent('''
        import ast, importlib, json, os
        from pathlib import Path
        from types import SimpleNamespace
        from tests.fixtures.home_resolvers_release_217 import SOURCES
        from subfleet import home_paths
        assert "PYTEST_CURRENT_TEST" not in os.environ
        assert home_paths.PYTEST_MARKER not in os.environ
        def outcome(call):
            try:
                return ("value", call())
            except Exception as error:
                return ("exception", type(error).__name__, str(error))
        compared = {}
        for name, source in SOURCES.items():
            module = importlib.import_module(name)
            namespace = module.__dict__.copy()
            exec("from __future__ import annotations\\n" + source, namespace)
            function = ast.parse(source).body[0].name
            args = (SimpleNamespace(_projects_dir=None),) if name.endswith("adapters.claude") else ()
            before = outcome(lambda: namespace[function](*args))
            current = (module.ClaudeAdapter._config_projects_dir if args else getattr(module, function))
            after = outcome(lambda: current(*args))
            assert after == before, (name, repr(before), repr(after))
            compared[name] = str(after)
        # Historically fixed homes must ignore the new test-only overrides in
        # production, even when the environment contains them.
        for current, original in [
            (home_paths.codex_app_home, lambda: Path.home() / ".codex"),
            (home_paths.desktop_login_path, lambda: Path.home() / ".claude.json"),
            (home_paths.provider_home, Path.home),
        ]:
            assert outcome(current) == outcome(original)
        # The additional legacy seams also retain the import-time release
        # defaults, including their exceptions for a literal-tilde HOME.
        for module_name, constant, variable, spelling in [
            ("subfleet.importer", "V1_STATE", "SUBFLEET_V1_STATE", "~/chief-of-staff/state/subfleet"),
            ("subfleet.importer", "DELEGATE_STATE", "SUBFLEET_DELEGATE_STATE", "~/.local/state/delegate"),
            ("subfleet.importer", "V1_ROSTER_DIR", "SUBFLEET_V1_ROSTER_DIR", "~/chief-of-staff/subfleet"),
            ("subfleet.lanes_transfer", "V1_STATE_DIR", "SUBFLEET_V1_STATE", "~/chief-of-staff/state/subfleet"),
            ("subfleet.lanes_transfer", "V1_ROSTER_DIR", "SUBFLEET_V1_ROSTER_DIR", "~/chief-of-staff/subfleet"),
            ("subfleet.lanes_transfer", "LAUNCH_AGENTS", "SUBFLEET_V1_LAUNCH_AGENTS", "~/Library/LaunchAgents"),
        ]:
            def legacy_default():
                module = importlib.import_module(module_name)
                return Path(home_paths.home_path(variable, lambda: getattr(module, constant), test_only=True))
            assert outcome(legacy_default) == outcome(lambda: Path(spelling).expanduser())
        print(json.dumps(compared))
    ''')
    result = subprocess.run([sys.executable, "-c", source], cwd=tmp_path, env=env,
                            text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(json.loads(result.stdout)) == 11


@pytest.mark.parametrize("marker", [PYTEST_MARKER, "PYTEST_CURRENT_TEST"])
def test_keychain_after_undo_never_requests_native_command(tmp_path, monkeypatch, marker):
    """Replay the review's default desktop reader with a recording backend."""
    from subfleet.adapters import claude
    from subfleet.adapters.claude import PROFILE_UNAVAILABLE
    from tests.conftest import no_desktop_login
    monkeypatch.undo()
    with pytest.MonkeyPatch.context() as phase:
        phase.delenv(PYTEST_MARKER, raising=False)
        phase.delenv("PYTEST_CURRENT_TEST", raising=False)
        phase.setenv(marker, "1")
        phase.setenv("HOME", str(tmp_path / "fake-home"))
        calls = []

        def record(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "fixture unavailable")

        phase.setattr(claude.subprocess, "run", record)
        removable = pytest.MonkeyPatch()
        no_desktop_login.__wrapped__(removable)
        removable.undo()
        result = ClaudeAdapter().probe_desktop_profile()
        assert result.status == PROFILE_UNAVAILABLE
        assert calls == []


@pytest.mark.parametrize("reader", ["blob", "home-login", "token", "plan", "credential"])
@pytest.mark.parametrize("marker", [PYTEST_MARKER, "PYTEST_CURRENT_TEST"])
def test_every_keychain_reader_refuses_default_backend(tmp_path, monkeypatch, reader, marker):
    from subfleet.adapters import claude
    from subfleet.adapters.base import AdapterError
    from subfleet.contracts import Credential
    monkeypatch.delenv(PYTEST_MARKER, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv(marker, "1")
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.delenv("CLAUDE_LANE_AGENT_SECRET", raising=False)
    calls = []

    def record(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "fixture unavailable")

    monkeypatch.setattr(claude.subprocess, "run", record)
    adapter = ClaudeAdapter()
    if reader == "blob":
        assert claude._keychain_blob("Claude Code-credentials") is None
    elif reader == "home-login":
        assert claude.home_login(tmp_path / "empty-home") is None
    elif reader == "plan":
        assert adapter._plan_from_keychain("claude-quota-fixture") is None
    else:
        with pytest.raises(AdapterError, match="refusing personal keychain"):
            if reader == "token":
                adapter._resolve_keychain_token("claude-quota-fixture")
            else:
                credentials.resolve_credential(Credential("claude", "claude-quota-fixture", "keychain-token"))
    assert calls == []


def test_keychain_explicit_runner_is_a_standin(monkeypatch):
    from subfleet.adapters import claude
    from subfleet.contracts import Credential
    monkeypatch.delenv("CLAUDE_LANE_AGENT_SECRET", raising=False)
    calls = []

    def record(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "fixture-token", "")

    assert claude._keychain_blob("fixture-service", runner=record) == "fixture-token"
    credential = Credential("claude", "fixture-service", "keychain-token")
    assert credentials.resolve_credential(credential, runner=record)["CLAUDE_CODE_OAUTH_TOKEN"] == "fixture-token"
    assert ClaudeAdapter(runner=record).credential_env(credential)["CLAUDE_CODE_OAUTH_TOKEN"] == "fixture-token"
    assert len(calls) == 3


@pytest.mark.parametrize("security_bin", ["security", "/usr/bin/security"])
@pytest.mark.parametrize("explicit_native_runner", [False, True])
def test_native_keychain_command_is_never_a_standin(security_bin, explicit_native_runner):
    from subfleet.adapters.base import AdapterError
    runner = credentials._NATIVE_RUNNER if explicit_native_runner else None
    with pytest.raises(AdapterError, match="refusing personal keychain"):
        credentials.keychain_command("Claude Code-credentials", security_bin, runner=runner)


def test_implicit_agent_helper_lookup_is_isolated(monkeypatch):
    from subfleet.adapters.base import AdapterError
    monkeypatch.delenv("CLAUDE_LANE_AGENT_SECRET", raising=False)
    monkeypatch.setattr(Path, "home", lambda: pytest.fail("personal helper HOME was evaluated"))
    with pytest.raises(AdapterError, match="refusing personal keychain"):
        credentials.keychain_command("claude-quota-fixture")
    monkeypatch.delenv("SUBFLEET_AGENT_SECRET")
    with pytest.raises(RuntimeError, match="SUBFLEET_AGENT_SECRET"):
        credentials.keychain_command("claude-quota-fixture")


def test_doctor_default_plist_is_isolated(monkeypatch):
    from subfleet import doctor
    monkeypatch.setattr(Path, "home", lambda: pytest.fail("personal plist HOME was evaluated"))
    assert doctor.check_launchd_limit()["status"] == doctor.UNKNOWN
    monkeypatch.delenv("SUBFLEET_LAUNCHD_PLIST")
    with pytest.raises(RuntimeError, match="SUBFLEET_LAUNCHD_PLIST"):
        doctor.check_launchd_limit()


@pytest.mark.parametrize("prior", ["missing", "empty", "configured"])
def test_unconfigure_restores_environment_and_expires_captured_child(tmp_path, monkeypatch, prior):
    """The review's copied daemon_env becomes ordinary after session cleanup."""
    from types import SimpleNamespace
    config = SimpleNamespace()
    tracked = (PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID,
               *SAFE_HOME_PATHS, *SAFE_ENV_REMOVALS)
    with monkeypatch.context() as phase:
        phase.delenv("PYTEST_CURRENT_TEST", raising=False)
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        (fake_home / ".claude.json").write_text(
            '{"oauthAccount":{"emailAddress":"fixture@example.test"}}')
        phase.setenv("HOME", str(fake_home))
        for name in tracked:
            if prior == "missing":
                phase.delenv(name, raising=False)
            else:
                phase.setenv(name, "" if prior == "empty" else str(tmp_path / name))
        original = {name: os.environ.get(name) for name in tracked}
        try:
            pytest_configure(config)
            base = config._subfleet_safe_home_base
            captured = cli.daemon_env(tmp_path / "daemon")
            assert all(name not in captured for name in SAFE_ENV_REMOVALS)
        finally:
            pytest_unconfigure(config)
        assert {name: os.environ.get(name) for name in tracked} == original
        assert not base.exists()
        pytest_unconfigure(config)  # repeated cleanup must not restore a second time
        assert {name: os.environ.get(name) for name in tracked} == original
        assert captured[PYTEST_MARKER] == "1"
        assert captured[PYTEST_SESSION] == str(base)
        assert all(name in captured for name in SAFE_HOME_PATHS)
        captured["PYTHONPATH"] = str(REPO)
        probe = '''
from subfleet import home_paths, capacity
from pathlib import Path
import os
assert os.environ[home_paths.PYTEST_MARKER] == "1"
assert not Path(os.environ["SUBFLEET_DESKTOP_LOGIN"]).parent.exists()
assert not home_paths.under_pytest()
assert home_paths.desktop_login_path() == Path.home() / ".claude.json"
assert home_paths.codex_app_home() == Path.home() / ".codex"
assert capacity.read_desktop_account() == "fixture@example.test"
'''
        result = subprocess.run([sys.executable, "-c", probe], env=captured,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        if prior == "missing":
            # A newly captured ordinary child resolves release defaults under a
            # fake HOME, after the session has removed its marker and overrides.
            fresh = cli.daemon_env(tmp_path / "ordinary-daemon")
            fresh["PYTHONPATH"] = str(REPO)
            probe = '''
from subfleet import home_paths, capacity
from pathlib import Path
import os
assert home_paths.PYTEST_MARKER not in os.environ
assert capacity.read_desktop_account() == "fixture@example.test"
assert home_paths.desktop_login_path() == Path.home() / ".claude.json"
assert home_paths.codex_app_home() == Path.home() / ".codex"
assert home_paths.home_path("SUBFLEET_CLAUDE_DIR", lambda: Path.home() / ".claude") == Path.home() / ".claude"
'''
            result = subprocess.run([sys.executable, "-c", probe], env=fresh,
                                    capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stdout + result.stderr
