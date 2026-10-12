"""C-23.28: undoing test patches can never expose personal home stores."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from subfleet import client, home_paths, hooks, notify_push
from subfleet import importer, lanes_transfer
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.adapters.claude_mcp import global_config_path
from subfleet.guard.preflight import _state_root
from subfleet.sessions import desktop, mirror, revive, transcripts
from subfleet.home_paths import PYTEST_MARKER, SAFE_HOME_PATHS
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
    ]
]


def outcome(call):
    try:
        return ("value", call())
    except Exception as error:
        return ("exception", type(error).__name__, str(error))


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
