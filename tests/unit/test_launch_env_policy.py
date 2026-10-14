"""C-11.1: environment pass-through is an explicit list of exact names."""

import json

import pytest

from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError, load_policy


@pytest.mark.parametrize("section,key", [
    (None, "env"), ([], "env"), ("FOO", "env"),
    ({"pass": None}, "env.pass"), ({"pass": "FOO"}, "env.pass"),
    ({"pass": {}}, "env.pass"), ({"pass": ["FOO", 2]}, "env.pass[1]"),
    ({"pass": [""]}, "env.pass[0]"), ({"pass": ["1FOO"]}, "env.pass[0]"),
    ({"pass": ["FOO*"]}, "env.pass[0]"), ({"pass": ["LC_*"]}, "env.pass[0]"),
    ({"pass": ["FOO=bar"]}, "env.pass[0]"), ({"pass": [" FOO"]}, "env.pass[0]"),
    ({"pass": ["FOO\n"]}, "env.pass[0]"), ({"pass": ["FÖÖ"]}, "env.pass[0]"),
])
def test_invalid_env_pass_names_exact_policy_key(tmp_path, section, key):
    policy = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    policy["env"] = section
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    with pytest.raises(PolicyError) as error:
        load_policy(path)
    assert error.value.key == key


@pytest.mark.parametrize("section", [{}, {"pass": []}, {"pass": ["FOO_API_KEY", "_TOKEN", "mixedCase2"]}])
def test_valid_env_pass_preserves_exact_policy_names(tmp_path, section):
    policy = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    policy["env"] = section
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    assert load_policy(path)["env"] == section


def test_passed_secret_is_exact_and_survives_daemon_start(tmp_path, monkeypatch):
    from subfleet import cli
    from subfleet.provider_env import ProviderEnvironment
    policy = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    policy["env"] = {"pass": ["FOO_API_KEY"]}
    policy["providers"] = {"codex": {"ca_bundle": None}}
    (tmp_path / "policy.json").write_text(json.dumps(policy))
    monkeypatch.setattr("os.environ", {"FOO_API_KEY": "dummy", "FOO_API_KEY_OTHER": "dummy",
                                       "OTHER_TOKEN": "dummy", "PATH": "/usr/bin:/bin"})
    started = cli.daemon_env(tmp_path)
    assert set(started) == {"PATH", "SUBFLEET_HOME", "FOO_API_KEY"}
    child = ProviderEnvironment(load_policy(tmp_path / "policy.json")).build("codex", environ=started)
    assert set(child) == set(started)
    assert child["FOO_API_KEY"] == "dummy"
