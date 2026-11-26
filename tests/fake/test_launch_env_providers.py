"""D-ENV1: recording fake providers and their tools see no ambient secrets."""

import os
import subprocess
import sys

import pytest

from subfleet.daemon import Daemon
from subfleet.provider_env import ProviderEnvironment
from tests.unit.test_launch_env_allowlist import POLICY, POLLUTION, launch_for


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("mode", ["detached", "turn", "probe"])
def test_fake_provider_and_tool_reject_shell_secrets(tmp_path, monkeypatch, provider, mode):
    parent = {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", **POLLUTION}
    monkeypatch.setattr(os, "environ", parent)
    lane, launch = launch_for(provider, mode, tmp_path, monkeypatch)
    daemon = Daemon.__new__(Daemon)
    daemon._provider_environment = ProviderEnvironment(POLICY)
    env = daemon._launch_environment(lane, launch)
    # No environment dump, including on failure: stdout is only a fixed word.
    checker = "import os; assert not any(n in os.environ for n in " + repr(tuple(POLLUTION)) + ")"
    provider_code = checker + "; import subprocess,sys; subprocess.run([sys.executable,'-c'," + repr(checker) + "],check=True); print('OK')"
    child = subprocess.run([sys.executable, "-c", provider_code], env=env, cwd=tmp_path,
                           capture_output=True, text=True, timeout=10)
    assert child.returncode == 0, "fake provider or tool inherited pollution"
    assert child.stdout.strip() == "OK"
