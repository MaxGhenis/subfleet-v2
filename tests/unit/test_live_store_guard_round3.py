"""Round-three executed review inputs, using only synthetic personal stores."""

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from subfleet import cli, home_paths, host_push, importer
from tests import conftest

REPO = Path(__file__).resolve().parents[2]


@contextmanager
def configured_session(tmp_path, monkeypatch):
    """Configure above removable fixture patches, just as the reviewer did."""
    with monkeypatch.context() as ambient:
        home = tmp_path / "person-home"
        home.mkdir()
        config_home = home / ".config"
        (config_home / "git").mkdir(parents=True)
        (home / ".gitconfig").write_text("[credential]\nhelper = osxkeychain\n")
        (config_home / "git/config").write_text("[credential]\nuseHttpPath = true\n")
        (home / ".claude.json").write_text(
            '{"oauthAccount":{"emailAddress":"fixture@example.test"}}')
        hold = home / "no-reset.json"
        hold.write_text('{"review-sentinel":"personal-reset-hold"}')
        ambient.setenv("HOME", str(home))
        ambient.setenv("XDG_CONFIG_HOME", str(config_home))
        ambient.setenv("SSH_AUTH_SOCK", str(tmp_path / "person-agent.sock"))
        ambient.setenv("SUBFLEET_RESET_INHIBIT", str(hold))
        ambient.delenv("PYTEST_CURRENT_TEST", raising=False)
        config = SimpleNamespace()
        try:
            conftest.pytest_configure(config)
            removable = pytest.MonkeyPatch()
            conftest.no_desktop_store.__wrapped__(
                SimpleNamespace(mktemp=lambda _: tmp_path / "per-test"), removable)
            removable.setenv("HOME", str(tmp_path / "per-test-home"))
            removable.setenv("XDG_CONFIG_HOME", str(tmp_path / "per-test-config"))
            removable.setenv("SSH_AUTH_SOCK", str(tmp_path / "per-test-agent.sock"))
            removable.undo()
            assert os.environ["HOME"] == str(config._subfleet_safe_home_base / "home")
            assert home_paths.under_pytest()
            yield home, hold, config
        finally:
            conftest.pytest_unconfigure(config)


@pytest.mark.parametrize("check", ["credentials", "agent"])
def test_host_credentials_after_configure_and_fixture_undo(tmp_path, monkeypatch, check):
    with configured_session(tmp_path, monkeypatch):
        repo = tmp_path / "repo.git"
        repo.mkdir()
        # Real local Git parses fake files only; no helper or transport is run.
        settings = host_push.host_credentials(repo)
        if check == "credentials":
            assert settings == ()
        else:
            launches = []

            def intercept(argv, **kwargs):
                launches.append((argv, kwargs["env"]))
                raise OSError("intercepted before any helper, socket or remote")

            monkeypatch.setattr(host_push.subprocess, "Popen", intercept)
            # The session removed the agent socket it inherited (D-PRR1); one
            # set during the session must still not reach git.
            monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "person-agent.sock"))
            with pytest.raises(host_push.PushError):
                host_push.git(repo, "ls-remote", "https://example.invalid/review",
                              credentials=settings)
            assert len(launches) == 1
            argv, env = launches[0]
            assert "SSH_AUTH_SOCK" not in env
            assert "credential.helper=osxkeychain" not in argv
            assert env["HOME"] != os.environ["HOME"]
            assert env["XDG_CONFIG_HOME"] != os.environ["XDG_CONFIG_HOME"]
            assert env["GIT_CONFIG_GLOBAL"] == os.devnull
            assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_inherited_reset_hold_after_configure_and_fixture_undo(tmp_path, monkeypatch):
    with configured_session(tmp_path, monkeypatch) as (_, hold, config):
        reads = []
        native_read = Path.read_text

        def record(path, *args, **kwargs):
            if path == hold:
                reads.append(path)
            return native_read(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", record)
        state = tmp_path / "state"
        importer.import_v1(state, write_report=False)
        assert reads == []
        assert not (state / importer.INHIBIT_MARKER).exists()
        assert Path(os.environ["SUBFLEET_RESET_INHIBIT"]).is_relative_to(
            config._subfleet_safe_home_base)


@pytest.mark.parametrize("remove_overrides", [False, True], ids=["captured", "missing-paths"])
@pytest.mark.parametrize("capture_current_test", [False, True], ids=["collection", "test-call"])
def test_captured_daemon_environment_expires_after_cleanup(
        tmp_path, monkeypatch, remove_overrides, capture_current_test):
    with configured_session(tmp_path, monkeypatch) as (home, _, config):
        captured = cli.daemon_env(tmp_path / "daemon")
        if capture_current_test:
            captured["PYTEST_CURRENT_TEST"] = "synthetic-test (call)"
        base = config._subfleet_safe_home_base
    assert not base.exists()
    # The captured session HOME is isolated. An ordinary child may explicitly
    # choose this synthetic personal home after the session has expired.
    captured["HOME"] = str(home)
    if remove_overrides:
        for variable in home_paths.SAFE_HOME_PATHS:
            captured.pop(variable, None)
    captured["PYTHONPATH"] = str(REPO)
    probe = '''
from pathlib import Path
from subfleet import capacity, home_paths
assert not home_paths.under_pytest()
assert home_paths.codex_app_home() == Path.home() / ".codex"
assert home_paths.desktop_login_path() == Path.home() / ".claude.json"
assert capacity.read_desktop_account() == "fixture@example.test"
'''
    child = subprocess.run([sys.executable, "-c", probe], env=captured, cwd=home,
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stdout + child.stderr


@pytest.mark.parametrize("variable", ["SUBFLEET_HOST_HOME", "SUBFLEET_HOST_CONFIG"])
@pytest.mark.parametrize("empty", [False, True], ids=["missing", "empty"])
@pytest.mark.parametrize("consumer", ["discovery", "execution"])
def test_host_credentials_refuse_unisolated_paths(tmp_path, monkeypatch, variable, empty, consumer):
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    if empty:
        monkeypatch.setenv(variable, "")
    else:
        monkeypatch.delenv(variable)
    monkeypatch.setattr(host_push.subprocess, "Popen",
                        lambda *a, **kw: pytest.fail("unisolated Git launch"))
    with pytest.raises(RuntimeError, match=variable):
        if consumer == "discovery":
            host_push.host_credentials(tmp_path)
        else:
            host_push.git(tmp_path, "status")


def test_host_credentials_accept_explicit_fixture_configs(tmp_path, monkeypatch):
    home, xdg = tmp_path / "fixture-home", tmp_path / "fixture-config"
    home.mkdir()
    (xdg / "git").mkdir(parents=True)
    (home / ".gitconfig").write_text("[credential]\nhelper = fixture-helper\n")
    (xdg / "git/config").write_text("[credential]\nuseHttpPath = true\n")
    monkeypatch.setenv("SUBFLEET_HOST_HOME", str(home))
    monkeypatch.setenv("SUBFLEET_HOST_CONFIG", str(xdg))
    # The session holds no inherited agent socket (D-PRR1); set one to drop.
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "agent.sock"))
    settings = host_push.host_credentials(tmp_path)
    assert settings == ("-c", "credential.usehttppath=true", "-c", "credential.helper=fixture-helper")
    env = host_push._environment()
    assert env["HOME"] == str(home)
    assert env["XDG_CONFIG_HOME"] == str(xdg)
    assert "SSH_AUTH_SOCK" not in env


@pytest.mark.parametrize("empty", [False, True], ids=["missing", "empty"])
def test_importer_refuses_unisolated_reset_hold(tmp_path, monkeypatch, empty):
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    if empty:
        monkeypatch.setenv("SUBFLEET_RESET_INHIBIT", "")
    else:
        monkeypatch.delenv("SUBFLEET_RESET_INHIBIT")
    monkeypatch.setattr(importer, "_looks_like_v1_root",
                        lambda *a: pytest.fail("import reached state before hold refusal"))
    with pytest.raises(RuntimeError, match="SUBFLEET_RESET_INHIBIT"):
        importer.import_v1(tmp_path / "state", write_report=False)


@pytest.mark.parametrize("choice", ["environment", "explicit", "explicit-home"])
def test_reset_hold_explicit_test_paths_keep_precedence(tmp_path, monkeypatch, choice):
    hold = tmp_path / "fixture-hold"
    explicit = tmp_path / "explicit-hold"
    home = tmp_path / "fixture-home"
    options = {}
    monkeypatch.setenv("SUBFLEET_RESET_INHIBIT", str(hold))
    if choice == "explicit":
        options["v1_reset_hold"] = explicit
    elif choice == "explicit-home":
        monkeypatch.delenv("SUBFLEET_RESET_INHIBIT")
        options["home"] = home
    selected = []
    monkeypatch.setattr(importer, "import_reset_settings",
                        lambda *a, v1_hold, **kw: selected.append(v1_hold))
    importer.import_v1(tmp_path / "state", write_report=False, **options)
    expected = {"environment": hold, "explicit": explicit,
                "explicit-home": home / importer.V1_RESET_HOLD}[choice]
    assert selected == [expected]


def test_captured_daemon_environment_guards_a_live_session_child(tmp_path, monkeypatch):
    with configured_session(tmp_path, monkeypatch):
        captured = cli.daemon_env(tmp_path / "daemon")
        captured["PYTHONPATH"] = str(REPO)
        captured.pop("SUBFLEET_HOST_HOME")
        probe = '''
from subfleet import home_paths, host_push
assert home_paths.under_pytest()
try:
    host_push._environment()
except RuntimeError as error:
    assert "SUBFLEET_HOST_HOME" in str(error)
else:
    raise AssertionError("active child bypassed credential isolation")
'''
        child = subprocess.run([sys.executable, "-c", probe], env=captured,
                               capture_output=True, text=True, timeout=30)
        assert child.returncode == 0, child.stdout + child.stderr
