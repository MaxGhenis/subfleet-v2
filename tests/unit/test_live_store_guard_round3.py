"""Round-three executed review inputs, using only synthetic personal stores."""

from contextlib import contextmanager
import json
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
            assert os.environ["HOME"] == str(home)
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
def test_captured_daemon_environment_expires_after_cleanup(tmp_path, monkeypatch, remove_overrides):
    with configured_session(tmp_path, monkeypatch) as (home, _, config):
        captured = cli.daemon_env(tmp_path / "daemon")
        base = config._subfleet_safe_home_base
    assert not base.exists()
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
