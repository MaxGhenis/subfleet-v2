"""D-NF1: provider trust selection is shared and preserves operator choices."""

from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import daemon as daemon_module, policy as policy_module, provider_env
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.daemon import Daemon
from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError, load_policy
from subfleet.provider_env import CA_SOURCES, ProviderEnvironment

CERTIFICATE = Path(__file__).parents[1] / "fixtures/codex-ca.pem"


@pytest.fixture
def bundle(tmp_path):
    path = tmp_path / "ca.pem"
    path.write_bytes(CERTIFICATE.read_bytes())
    return str(path)


def selected(bundle):
    return {"providers": {"codex": {"ca_bundle": bundle}}}


def ca_values(env):
    return {key: env[key] for key in CA_SOURCES if key in env}


def test_default_codex_launch_environment(monkeypatch):
    seen = []
    def read(path):
        seen.append(path)
        return CERTIFICATE.read_bytes()
    monkeypatch.setattr(provider_env, "read_regular", read)
    environment = ProviderEnvironment({})
    additions = environment.additions("codex", {"CODEX_HOME": "/test/home"}, environ={})
    assert additions == {"CODEX_HOME": "/test/home", "SSL_CERT_FILE": "/etc/ssl/cert.pem"}
    assert environment.build("codex", additions, environ={}) == additions
    assert seen == ["/etc/ssl/cert.pem"] * 2


@pytest.mark.parametrize("operator", [
    {"SSL_CERT_FILE": "/operator/file.pem"}, {"SSL_CERT_DIR": "/operator/certs"},
    {"SSL_CERT_FILE": ""}, {"SSL_CERT_DIR": ""},
    {"SSL_CERT_FILE": "/operator/file.pem", "SSL_CERT_DIR": "/operator/certs"},
])
@pytest.mark.parametrize("configured", [None, "/missing/bundle.pem"])
def test_operator_ca_sources_pass_through_without_validation(operator, configured, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("an operator override must bypass bundle validation")
    monkeypatch.setattr(provider_env, "read_regular", forbidden)
    env = {"OTHER": "kept", **operator}
    result = ProviderEnvironment(selected(configured)).build("codex", environ=env)
    assert result == env
    assert env == {"OTHER": "kept", **operator}


def test_null_policy_adds_nothing(caplog):
    assert ProviderEnvironment(selected(None)).build("codex", environ={}) == {}
    assert not caplog.records


@pytest.mark.parametrize("kind", ["missing", "unreadable", "empty", "garbage", "invalid-pem", "directory", "fifo"])
def test_unusable_bundle_adds_nothing_and_logs_once(tmp_path, monkeypatch, caplog, kind):
    path = tmp_path / "bad.pem"
    if kind == "unreadable":
        path.write_bytes(CERTIFICATE.read_bytes())
        path.chmod(0)
        # chmod can still be read by root; model the open() permission error.
        def denied(*args, **kwargs):
            raise PermissionError("fixture bundle is unreadable")
        monkeypatch.setattr(provider_env, "read_regular", denied)
    elif kind == "empty":
        path.touch()
    elif kind == "garbage":
        path.write_text("this is not a certificate")
    elif kind == "invalid-pem":
        path.write_text("-----BEGIN CERTIFICATE-----\nYWJj\n-----END CERTIFICATE-----\n")
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        import os
        os.mkfifo(path)
    environment = ProviderEnvironment(selected(str(path)))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: environment.build("codex", environ={}), range(8)))
    assert results == [{}] * 8
    warnings = [record for record in caplog.records if "Codex CA bundle" in record.message]
    assert len(warnings) == 1 and str(path) in warnings[0].message


def test_bundle_is_rechecked_when_it_changes(bundle, caplog):
    environment = ProviderEnvironment(selected(bundle))
    assert environment.build("codex", environ={}) == {"SSL_CERT_FILE": bundle}
    Path(bundle).write_text("")
    assert environment.build("codex", environ={}) == {}
    Path(bundle).write_bytes(CERTIFICATE.read_bytes())
    assert environment.build("codex", environ={}) == {"SSL_CERT_FILE": bundle}
    assert len(caplog.records) == 1


@pytest.mark.parametrize("operator", [{}, {"SSL_CERT_FILE": "operator.pem"}, {"SSL_CERT_DIR": "operator-dir"}])
def test_claude_environment_is_unchanged(bundle, monkeypatch, operator):
    def forbidden(*args, **kwargs):
        pytest.fail("Claude must not read the Codex CA bundle")
    monkeypatch.setattr(provider_env, "read_regular", forbidden)
    base = {"PATH": "/test/bin", **operator}
    additions = {"CLAUDE_CONFIG_DIR": "/test/claude"}
    environment = ProviderEnvironment(selected(bundle))
    assert environment.additions("claude", additions, environ=base) == additions
    assert environment.build("claude", additions, environ=base) == {**base, **additions}


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_daemon_credentials_guardian_and_recovery_share_environment(bundle, tmp_path, monkeypatch, provider):
    for key in CA_SOURCES:
        monkeypatch.delenv(key, raising=False)
    lane = Lane(f"{provider}-1", provider, f"{provider}:test", Credential(provider, str(tmp_path), "home"),
                str(tmp_path), LaneOwner.V2, False)
    credential = {"CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR": str(tmp_path)}
    monkeypatch.setattr(daemon_module, "resolve_credential", lambda _: credential.copy())
    daemon = Daemon.__new__(Daemon)
    daemon.log = logging.getLogger(__name__)
    daemon._provider_environment = ProviderEnvironment(selected(bundle), log=daemon.log)
    daemon.store = SimpleNamespace(get_lane=lambda _: lane)
    additions = daemon._credential_environment(lane)
    launch = SimpleNamespace(env_add=additions, env_remove=("OPENAI_API_KEY",))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-child")
    guardian_env = daemon._launch_environment(lane, launch)
    expected = {"SSL_CERT_FILE": bundle} if provider == "codex" else {}
    assert ca_values(additions) == ca_values(guardian_env) == ca_values(daemon._relaunch_env(lane.lane_id)) == expected
    assert "OPENAI_API_KEY" not in guardian_env
    assert additions != credential if provider == "codex" else additions == credential


@pytest.mark.parametrize("value,error_key", [
    ([], "providers"), ({"codex": None}, "providers.codex"),
    ({"codex": {"ca_bundle": False}}, "providers.codex.ca_bundle"),
    ({"codex": {"ca_bundle": []}}, "providers.codex.ca_bundle"),
    ({"codex": {"ca_bundle": ""}}, "providers.codex.ca_bundle"),
    ({"codex": {"ca_bundle": "relative.pem"}}, "providers.codex.ca_bundle"),
    ({"codex": {"ca_bundle": "/bad\0path"}}, "providers.codex.ca_bundle"),
])
def test_policy_loader_validates_ca_setting(tmp_path, value, error_key):
    data = json.loads(DEFAULT_POLICY_PATH.read_text())
    data["providers"] = value
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data))
    with pytest.raises(PolicyError) as error:
        load_policy(path)
    assert error.value.key == error_key


@pytest.mark.parametrize("configured", ["omitted", None, "/missing/bundle.pem"])
def test_policy_loader_defaults_and_accepts_opt_out_or_unavailable_path(tmp_path, configured):
    data = json.loads(DEFAULT_POLICY_PATH.read_text())
    if configured == "omitted":
        data.pop("providers")
    else:
        data["providers"] = selected(configured)["providers"]
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data))
    actual = load_policy(path)
    assert policy_module.codex_ca_bundle(actual) == ("/etc/ssl/cert.pem" if configured == "omitted" else configured)


@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@given(environ=st.dictionaries(st.one_of(st.sampled_from(CA_SOURCES), st.text(min_size=1, max_size=30)),
                               st.text(max_size=80), max_size=10),
       choice=st.one_of(st.none(), st.sampled_from(["valid", "missing"]),
                        st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789/_-", min_size=1, max_size=60)))
def test_property_ca_selection_never_overrides_or_adds_a_second_source(bundle, tmp_path, environ, choice):
    configured = bundle if choice == "valid" else (None if choice is None else str(tmp_path / choice.lstrip("/")))
    before = dict(environ)
    result = ProviderEnvironment(selected(configured)).build("codex", environ=environ)
    operator = ca_values(environ)
    assert ca_values(result) == operator if operator else len(ca_values(result)) <= 1
    assert all(result[key] == value for key, value in environ.items())
    assert environ == before
    if not operator:
        assert ca_values(result) == ({"SSL_CERT_FILE": bundle} if configured == bundle else {})
