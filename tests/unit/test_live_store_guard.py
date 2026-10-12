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
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.adapters.claude_mcp import global_config_path
from subfleet.guard.preflight import _state_root
from subfleet.sessions import desktop, mirror, revive, transcripts
from tests.conftest import SAFE_HOME_PATHS

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


@pytest.mark.parametrize("variable,resolver,suffix", RESOLVERS)
@pytest.mark.parametrize("patch", ["setenv", "delenv"])
def test_undo_restores_session_safe_path(request, tmp_path, monkeypatch, variable, resolver, suffix, patch):
    """C-23.28: the autouse fixture and a test share one undo stack."""
    per_test = os.environ[variable]
    if patch == "setenv":
        monkeypatch.setenv(variable, str(tmp_path / "test-override"))
    else:
        monkeypatch.delenv(variable)
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


@pytest.mark.parametrize("marker", ["", "synthetic-test (call)"])
def test_guard_never_evaluates_default(monkeypatch, marker):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", marker)
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
    ("timers-identity", "SUBFLEET_CODEX_APP_HOME"),
    ("daemon", "SUBFLEET_HOME"),
    ("importer", "SUBFLEET_HOME"),
    ("importer-home", "SUBFLEET_PROVIDER_HOME"),
    ("transfer-home", "SUBFLEET_PROVIDER_HOME"),
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
    for name, relative in SAFE_HOME_PATHS.items():
        monkeypatch.setenv(name, str(tmp_path / "isolated" / relative))
    monkeypatch.delenv(variable)
    empty_store = SimpleNamespace(query=lambda *args: [], lane_rows=lambda: [],
                                  one=lambda *args: {"provider": "codex", "account_key": "codex:fake", "owner": "v1"})
    calls = {
        "doctor": doctor.check_never_rules,
        "capacity-account": capacity.read_desktop_account,
        "capacity-identity": capacity.cached_desktop_identity,
        "catalog-build": lambda: catalog.build(tmp_path, lanes=[]),
        "catalog-session": lambda: catalog.native_session("codex", "fake", home=None, root=tmp_path, lanes=[]),
        "service-source": lambda: service.ConversationService._codex_source(
            SimpleNamespace(daemon=SimpleNamespace(store=empty_store)), None, "fake", None),
        "timers-identity": lambda: timers.Timers._identities(SimpleNamespace(store=empty_store)),
        "daemon": lambda: daemon.main([]),
        "importer": lambda: importer.main([]),
        "importer-home": lambda: importer.import_v1(tmp_path / "state", v1_state=tmp_path / "v1",
            delegate_state=tmp_path / "delegate", roster_dir=tmp_path / "roster"),
        "transfer-home": lambda: lanes_transfer.plan_transfer(empty_store, tmp_path / "state", "fake", "v2",
            roster_dir=tmp_path / "roster", agents_dir=tmp_path / "agents", v1_state=tmp_path / "v1"),
    }
    # If a mutation bypasses the guard, stop the entry point before any daemon
    # startup or legacy import. The changed exception still fails this test.
    monkeypatch.setattr(daemon.descriptors, "raise_open_file_limit", lambda: pytest.fail("daemon reached startup"))
    monkeypatch.setattr(importer, "import_legacy_cockpit", lambda *a, **k: pytest.fail("import reached files"))
    monkeypatch.setattr(importer, "_looks_like_v1_root", lambda *a: pytest.fail("import reached files"))
    with pytest.raises(RuntimeError, match=variable):
        calls[consumer]()


@pytest.mark.parametrize("mode", ["absent", "empty", "absolute", "relative", "tilde", "legacy-mcp"])
def test_outside_pytest_matches_release_217(tmp_path, mode):
    """C-23.28: execute the original release functions and ours in a child.

    No pytest marker, no real HOME, no Git/history dependency at test time.
    """
    env = {name: value for name, value in os.environ.items()
           if name not in SAFE_HOME_PATHS and name != "PYTEST_CURRENT_TEST"}
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    env["HOME"] = str(fake_home)
    env["PYTHONPATH"] = str(REPO)
    if mode != "absent" and mode != "legacy-mcp":
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
        compared = {}
        for name, source in SOURCES.items():
            module = importlib.import_module(name)
            namespace = module.__dict__.copy()
            exec("from __future__ import annotations\\n" + source, namespace)
            function = ast.parse(source).body[0].name
            args = (SimpleNamespace(_projects_dir=None),) if name.endswith("adapters.claude") else ()
            before = namespace[function](*args)
            current = (module.ClaudeAdapter._config_projects_dir if args else getattr(module, function))
            after = current(*args)
            assert after == before, (name, repr(before), repr(after))
            compared[name] = str(after)
        # Historically fixed homes must ignore the new test-only overrides in
        # production, even when the environment contains them.
        assert home_paths.codex_app_home() == Path.home() / ".codex"
        assert home_paths.desktop_login_path() == Path.home() / ".claude.json"
        assert home_paths.provider_home() == Path.home()
        print(json.dumps(compared))
    ''')
    result = subprocess.run([sys.executable, "-c", source], cwd=tmp_path, env=env,
                            text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(json.loads(result.stdout)) == 11
