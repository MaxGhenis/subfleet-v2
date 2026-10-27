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


@pytest.mark.parametrize("name", ["FOO_API_KEY", "CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                                  "SUBFLEET_CUSTOM_SECRET", "LC_CUSTOM_PASSWORD"])
def test_mandatory_removal_beats_exact_policy_authority(name):
    from subfleet.provider_env import ProviderEnvironment
    environment = ProviderEnvironment({"env": {"pass": [name]}, "providers": {"codex": {"ca_bundle": None}}})
    parent = {name: "dummy", name + "_OTHER": "dummy"}
    assert set(environment.build("codex", environ=parent)) == {name}
    assert environment.build("codex", env_remove=(name,), environ=parent) == {}


def test_daemon_start_refuses_invalid_env_policy_before_spawn(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from subfleet import cli
    policy = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    policy["env"] = {"pass": ["FOO*"]}
    (tmp_path / "policy.json").write_text(json.dumps(policy))
    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_daemon_alive", lambda *args, **kwargs: False)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("invalid policy spawned daemon"))
    assert cli.cmd_daemon_start(SimpleNamespace(state_root=str(tmp_path))) == 2
    assert "env.pass[0]" in capsys.readouterr().err
