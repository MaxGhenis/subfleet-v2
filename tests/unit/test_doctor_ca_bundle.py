"""The ordinary doctor version probe uses the launch's root policy."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet.policy import DEFAULT_POLICY_PATH
from subfleet.provider_env import CA_SOURCES, ProviderEnvironment


@pytest.fixture
def bundle(tmp_path):
    path = tmp_path / "ca.pem"
    path.write_bytes((Path(__file__).parents[1] / "fixtures/codex-ca.pem").read_bytes())
    return str(path)


def selected(bundle):
    return {"providers": {"codex": {"ca_bundle": bundle}}}


def ca_values(env):
    return {key: env[key] for key in CA_SOURCES if key in env}


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_doctor_version_check_uses_root_policy_environment(tmp_path, bundle, monkeypatch, provider):
    from subfleet import doctor
    baseline = {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin", "LANG": "C",
                "SUBFLEET_FAKE_REPORT": str(tmp_path / "report")}
    monkeypatch.setattr(os, "environ", {
        **baseline, "LANE_METADATA_TOKEN": "dummy-authorized",
        "LANE_METADATA_TOKEN_EXTRA": "dummy-denied", "UNRELATED_TOKEN": "dummy-denied",
        "SUBFLEET_TEST_TOKEN": "dummy-denied", "SUBFLEET_ATTEMPT": "foreign/a1",
        # Round 3: only listed configuration and harness hooks pass (D-ENV1).
        "SUBFLEET_TEST_REPORT": str(tmp_path / "denied-report"),
    })
    root = tmp_path / "state"
    root.mkdir()
    data = json.loads(DEFAULT_POLICY_PATH.read_text())
    data["providers"] = selected(bundle)["providers"]
    data["env"] = {"pass": ["LANE_METADATA_TOKEN"]}
    (root / "policy.json").write_text(json.dumps(data))
    monkeypatch.setattr(doctor.shutil, "which", lambda binary: f"/fixture/{binary}")
    seen = []
    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="fixture version", stderr="")
    monkeypatch.setattr(doctor.subprocess, "run", run)
    # Exercise table wiring too: its Codex row must use this root's policy.
    rows = doctor.checks(root, settings=tmp_path / "settings.json")
    assert next(row for row in rows if row["check"] == f"{provider} --version")["status"] == doctor.PASS
    options = next(kwargs for argv, kwargs in seen if argv == [f"/fixture/{provider}", "--version"])
    expected = {**baseline, "LANE_METADATA_TOKEN": "dummy-authorized"}
    if provider == "codex":
        expected["SSL_CERT_FILE"] = bundle
    assert options["env"] == expected


@pytest.mark.parametrize("configured", ["custom", "null", "missing"])
@pytest.mark.parametrize("operator", [
    {}, {"SSL_CERT_FILE": ""}, {"SSL_CERT_DIR": ""},
    {"SSL_CERT_FILE": "/operator/ca.pem", "SSL_CERT_DIR": "/operator/certs"},
])
def test_doctor_codex_matches_launch_ca_selection(tmp_path, bundle, monkeypatch, configured, operator):
    from subfleet import doctor
    for key in CA_SOURCES:
        monkeypatch.delenv(key, raising=False)
    for key, value in operator.items():
        monkeypatch.setenv(key, value)
    policy = selected(bundle if configured == "custom" else None if configured == "null"
                      else str(tmp_path / "missing.pem"))
    root = tmp_path / "state"
    root.mkdir()
    data = json.loads(DEFAULT_POLICY_PATH.read_text())
    data["providers"] = policy["providers"]
    (root / "policy.json").write_text(json.dumps(data))
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/fixture/codex")
    seen = []
    def run(argv, **kwargs):
        seen.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="fixture version", stderr="")
    monkeypatch.setattr(doctor.subprocess, "run", run)
    assert doctor.check_provider("codex", root=root)["status"] == doctor.PASS
    expected = ca_values(ProviderEnvironment(policy).build("codex"))
    assert ca_values(seen[0]) == expected
    if operator:
        assert expected == operator


def test_doctor_version_reports_invalid_root_policy_without_spawning(tmp_path, monkeypatch):
    from subfleet import doctor
    (tmp_path / "policy.json").write_text('{"providers":{"codex":{"ca_bundle":"relative.pem"}}}')
    monkeypatch.setattr(doctor.shutil, "which", lambda binary: f"/fixture/{binary}")
    def forbidden(*args, **kwargs):
        pytest.fail("an invalid policy must be diagnosed before probing Codex")
    monkeypatch.setattr(doctor.subprocess, "run", forbidden)
    result = doctor.check_provider("codex", root=tmp_path)
    assert result["status"] == doctor.FAIL
    assert "policy.json" in result["fix"]


def test_doctor_codex_uses_shipped_policy_before_state_root_exists(tmp_path, bundle, monkeypatch):
    from subfleet import doctor, provider_env
    for key in CA_SOURCES:
        monkeypatch.delenv(key, raising=False)
    certificate = Path(bundle).read_bytes()
    seen = []
    def read(path, **kwargs):
        assert path == "/etc/ssl/cert.pem"
        return certificate
    monkeypatch.setattr(provider_env, "read_regular", read)
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/fixture/codex")
    def run(argv, **kwargs):
        seen.append(ca_values(kwargs["env"]))
        return SimpleNamespace(returncode=0, stdout="fixture version", stderr="")
    monkeypatch.setattr(doctor.subprocess, "run", run)
    assert doctor.check_provider("codex", root=tmp_path / "absent")["status"] == doctor.PASS
    assert seen == [{"SSL_CERT_FILE": "/etc/ssl/cert.pem"}]
