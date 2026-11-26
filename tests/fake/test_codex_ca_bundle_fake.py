"""A real daemon and guardian pass C-10.8's CA environment to tools too."""

import json
from pathlib import Path

import pytest

from subfleet.contracts import attempt_dir


@pytest.mark.parametrize("mode", ["default", "custom", "operator-file", "operator-dir", "null", "missing"])
def test_fake_codex_and_its_tool_inherit_ca_environment(daemon, monkeypatch, mode):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    policy_path = daemon.root / "policy.json"
    policy = json.loads(policy_path.read_text())
    bundle = daemon.root / "ca.pem"
    bundle.write_bytes((Path(__file__).parents[1] / "fixtures/codex-ca.pem").read_bytes())
    expected = {"SSL_CERT_FILE": str(bundle)}
    if mode == "default":
        if not Path("/etc/ssl/cert.pem").is_file():
            pytest.skip("system bundle is unavailable; portable default coverage is in unit tests")
        policy.pop("providers", None)
        expected = {"SSL_CERT_FILE": "/etc/ssl/cert.pem"}
    elif mode in {"operator-file", "operator-dir"}:
        key = "SSL_CERT_FILE" if mode == "operator-file" else "SSL_CERT_DIR"
        # Operators may even choose an unavailable path; Subfleet passes it on.
        expected = {key: str(daemon.root / "operator-choice")}
        monkeypatch.setenv(key, expected[key])
    elif mode in {"null", "missing"}:
        expected = {}
    if mode != "default":
        policy["providers"] = {"codex": {"ca_bundle": None if mode == "null" else
                                          str(daemon.root / "missing.pem") if mode == "missing" else str(bundle)}}
    policy_path.write_text(json.dumps(policy))
    # Give only this test's daemon time to start under host load.
    original_until = daemon.until
    monkeypatch.setattr(daemon, "until", lambda predicate, timeout=5: original_until(predicate, timeout=max(60, timeout)))
    daemon.start()
    job_id = daemon.submit("ca-environment")
    job = daemon.until(lambda: (row if (row := daemon.job(job_id))["state"] in
                                {"succeeded", "failed", "cancelled", "lost"} else None), timeout=90)
    assert job["state"] == "succeeded", daemon.log_text()
    report = json.loads((attempt_dir(daemon.root, job_id, 1) / "stdout").read_text())
    assert report == {"provider": expected, "tool": expected}
    if mode == "missing":
        assert (daemon.root / "daemon.log").read_text().count("Codex CA bundle") == 1
