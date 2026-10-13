"""Round-four marker lifetime regressions, using only synthetic home stores."""

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from subfleet import cli, home_paths
from tests import conftest

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("capture_current_test", [False, True], ids=["collection", "test-call"])
def test_settings_write_cannot_revive_expired_session(tmp_path, monkeypatch, capture_current_test):
    """Replay configure/capture/cleanup/ordinary-child hook installation."""
    home = tmp_path / "ordinary-home"
    home.mkdir()
    (home / ".claude.json").write_text(
        '{"oauthAccount":{"emailAddress":"ordinary@example.test"}}')
    with monkeypatch.context() as ambient:
        ambient.setenv("HOME", str(home))
        ambient.delenv("PYTEST_CURRENT_TEST", raising=False)
        config = SimpleNamespace()
        try:
            conftest.pytest_configure(config)
            assert home_paths.under_pytest()
            captured = cli.daemon_env(tmp_path / "daemon")
            if capture_current_test:
                captured["PYTEST_CURRENT_TEST"] = "captured fixture (call)"
            base = config._subfleet_safe_home_base
        finally:
            conftest.pytest_unconfigure(config)
    assert not base.exists()
    captured["PYTHONPATH"] = str(REPO)
    probe = '''
import os
from pathlib import Path
from subfleet import capacity, home_paths, hooks
from subfleet.sessions import mirror
login = Path.home() / ".claude.json"
assert not home_paths.under_pytest()
assert home_paths.desktop_login_path() == login
assert capacity.read_desktop_account() == "ordinary@example.test"
written = hooks.apply(command="fixture-subfleet hook")
assert written["written"], written
assert written["path"] == os.environ["SUBFLEET_CLAUDE_SETTINGS"]
assert Path(written["path"]).is_file()
assert Path(os.environ[home_paths.PYTEST_SESSION]).is_dir()
assert not home_paths.under_pytest(), "settings write revived an expired session"
assert home_paths.desktop_login_path() == login
assert capacity.read_desktop_account() == "ordinary@example.test"
os.environ.pop("SUBFLEET_SESSION_STORE", None)
assert mirror.store_dir().is_relative_to(Path.home())
'''
    try:
        child = subprocess.run([sys.executable, "-c", probe], env=captured, cwd=home,
                               capture_output=True, text=True, timeout=30)
        assert child.returncode == 0, child.stdout + child.stderr
    finally:
        # The input deliberately recreates the synthetic session directory.
        conftest.pytest_unconfigure(config)
