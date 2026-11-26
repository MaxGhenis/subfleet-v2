"""C-10.8: record provider/tool environments without process-ownership bypasses.

The real daemon/guardian integration also lives in tests/fake. This narrower
process seam works when the host sandbox denies the boot identity that the
daemon requires: it exercises real credential and launch builders and starts
the recording fake provider, without manufacturing guardian ownership.
"""

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.daemon import Daemon
from subfleet.provider_env import ProviderEnvironment
from tests.fake_adapter import FakeAdapter


@pytest.mark.parametrize("mode", ["default", "custom", "operator-file", "operator-dir", "null", "missing"])
def test_recording_provider_and_tool_use_daemon_launch_environment(tmp_path, monkeypatch, mode):
    monkeypatch.delenv("SUBFLEET_PROBE", raising=False)
    for key in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(key, raising=False)
    bundle = tmp_path / "ca.pem"
    bundle.write_bytes((Path(__file__).parents[1] / "fixtures/codex-ca.pem").read_bytes())
    policy = {"providers": {"codex": {"ca_bundle": str(bundle)}}}
    expected = {"SSL_CERT_FILE": str(bundle)}
    if mode == "default":
        policy = {}
        if not Path("/etc/ssl/cert.pem").is_file():
            pytest.skip("no system bundle; portable default selection is covered in unit tests")
        expected = {"SSL_CERT_FILE": "/etc/ssl/cert.pem"}
    elif mode.startswith("operator"):
        key = "SSL_CERT_FILE" if mode == "operator-file" else "SSL_CERT_DIR"
        expected = {key: str(tmp_path / "operator-choice")}
        monkeypatch.setenv(key, expected[key])
    elif mode in {"null", "missing"}:
        expected = {}
        policy["providers"]["codex"]["ca_bundle"] = None if mode == "null" else str(tmp_path / "missing.pem")
    home = tmp_path / "home"
    home.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text(json.dumps({"scenario": "ca-environment"}))
    lane = Lane("codex-1", "codex", "codex:fake", Credential("codex", str(home), "home"),
                str(home), LaneOwner.V2, False)
    daemon = Daemon.__new__(Daemon)
    daemon._provider_environment = ProviderEnvironment(policy)
    launch = FakeAdapter().build_launch(
        SimpleNamespace(workdir=str(tmp_path)), "fake/a1", tmp_path, lane,
        daemon._credential_environment(lane), "fake-model", None, prompt, None)
    env = daemon._launch_environment(lane, launch)
    with prompt.open() as stream:
        child = subprocess.run(launch.argv, env=env, cwd=launch.cwd, stdin=stream,
                               text=True, capture_output=True, timeout=60, check=True)
    assert json.loads(child.stdout) == {"provider": expected, "tool": expected}
