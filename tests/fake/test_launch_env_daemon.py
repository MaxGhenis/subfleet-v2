"""D-ENV1: the actual daemon spawn sites cannot bypass environment selection."""

import json
import os
from types import SimpleNamespace

import pytest

from subfleet import daemon as daemon_module
from subfleet.adapters import registry
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.policy import PolicyError
from subfleet.provider_env import ProviderEnvironment
from tests.claude_code import claude_code_active
from tests.fake.conftest import Harness
from tests.fake.test_probe_recovery import reserved_probe
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401
from tests.fake_adapter import FakeAdapter
from tests.unit.test_launch_env_allowlist import ALLOWED, POLICY, POLLUTION


def daemon_spawn_environment(routing_state, monkeypatch, mode, *, pythonpath=None, extra=None):
    service, harness = routing_state
    parent = {"PATH": "/usr/bin:/bin", **POLLUTION, **(extra or {})}
    if pythonpath is not None:
        parent["PYTHONPATH"] = pythonpath
    monkeypatch.setattr(os, "environ", parent)
    policy = {**POLICY, "env": {"pass": ["PYTHONPATH"]}} if pythonpath is not None else POLICY
    service._provider_environment = ProviderEnvironment(policy)
    seen = []
    original_popen = daemon_module.subprocess.Popen
    def stopped(argv, **kwargs):
        if "subfleet.guardian" not in argv:
            return original_popen(argv, **kwargs)
        seen.append((argv, kwargs["env"]))
        raise OSError("fake guardian spawn stopped")
    monkeypatch.setattr(daemon_module.subprocess, "Popen", stopped)
    job_id = service.dispatch("submit", harness.submit_args())["job_id"]
    if mode in {"detached", "turn"}:
        service._admit()
        attempt = service.store.list_attempts(job_id)[0]
        if mode == "turn":
            service.store.conn.execute("UPDATE jobs SET kind='turn' WHERE job_id=?", (job_id,))
            manifest_path = service.root / "jobs" / job_id / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["turn"] = {"conversation_id": "fake-conversation", "message_id": "fake-message",
                                "provider": "codex", "cwd": str(harness.workdir),
                                "settings": {"model": "astra", "permission": "read-only"}}
            manifest_path.write_text(json.dumps(manifest))
            # Exercise the daemon's relay branch; turn builders have their own oracle.
            from tests.fake_adapter import FakeAdapter
            monkeypatch.setattr(service.conversations, "launch", lambda job, attempt, lane, creds, adir, model, guard:
                FakeAdapter().build_launch(service._spec(job), attempt["attempt_id"], adir, lane,
                                           creds, model, None, daemon_module.Path(job["prompt_path"]), None))
            monkeypatch.setattr(service, "_guard_override", lambda *args, **kwargs: None)
        monkeypatch.setattr(service, "_launch_failure", lambda *args, **kwargs: None)
        service._launch(attempt)
    elif mode == "probe":
        record = reserved_probe(service, job_id, state="reserved")
        service._execute_probe(service._job(job_id), service.store.get_lane("codex-1"),
                               service.policy["models"]["astra"], record["holder"])
    else:
        with pytest.raises(OSError):
            service._enrollment_turn("codex-1", "enroll:fake", ["fake-provider"],
                cwd=str(service.root), env={"PATH": "/usr/bin:/bin", "CLAUDE_CONFIG_DIR": "/fake"}, timeout=1)
    assert seen, "daemon did not reach its guardian spawn"
    argv, env = seen[0]
    assert not (set(env) & set(POLLUTION))
    # Guardian authorization carries names only and covers launch-owned variables.
    passed = {argv[i + 1] for i, item in enumerate(argv) if item == "--env-pass"}
    bootstrap = {"PYTHONPATH", "SUBFLEET_EXIT_SETTLE_S"}
    assert set(env) - ALLOWED - bootstrap <= passed
    return argv, env


@pytest.mark.parametrize("mode", ["detached", "turn", "probe", "enrollment"])
def test_actual_daemon_spawn_rejects_pollution(routing_state, monkeypatch, mode):
    daemon_spawn_environment(routing_state, monkeypatch, mode)


def test_daemon_status_and_doctor_expose_only_extra_names(routing_state, monkeypatch):
    from subfleet import doctor
    service, _ = routing_state
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", **POLLUTION})
    status = service.dispatch("daemon.status", {})
    # Round 4 (review r3, P3-2): the lanes' own variables are listed apart; this
    # roster has no env-kind lane.
    assert status["environment"] == {"extra_names": sorted(POLLUTION), "lane_credentials": []}
    assert "dummy" not in json.dumps(status["environment"])
    monkeypatch.setattr(doctor.Client, "call", lambda *args, **kwargs: status)
    output = doctor.render([doctor.check_daemon_environment(service.root)])
    assert all(name in output for name in POLLUTION)
    assert "dummy" not in output


@pytest.mark.parametrize("mode", ["detached", "turn", "probe", "enrollment"])
@pytest.mark.parametrize("relay", [False, True], ids=["stdin", "relay"])
@pytest.mark.parametrize("pythonpath", [None, "", "/dummy/policy-python"])
def test_guardian_bootstrap_preserves_provider_pythonpath(routing_state, monkeypatch, tmp_path, mode, relay, pythonpath):
    """Run each real daemon launch through the guardian's actual provider seam."""
    from subfleet import guardian
    with monkeypatch.context() as patch:
        argv, environment = daemon_spawn_environment(
            routing_state, patch, mode, pythonpath=pythonpath)
    passed = tuple(argv[i + 1] for i, item in enumerate(argv) if item == "--env-pass")
    # Even exact policy authority cannot export the guardian's private setting.
    passed += ("SUBFLEET_EXIT_SETTLE_S",)
    seen = []
    monkeypatch.setattr(guardian.os, "environ", environment)
    monkeypatch.setattr(guardian.os, "setsid", lambda: None)
    monkeypatch.setattr(guardian.signal, "signal", lambda *args: None)
    monkeypatch.setattr(guardian, "boot_id", lambda: "fake")
    monkeypatch.setattr(guardian, "proc_start", lambda pid: "fake")
    monkeypatch.setattr(guardian, "linger", lambda: None)
    monkeypatch.setattr(guardian.subprocess, "Popen", lambda *args, **kwargs:
                        seen.append(kwargs["env"]) or type("Child", (), {"pid": 123, "wait": lambda self: 0})())
    if relay:
        from subfleet import relay as relay_module
        from types import SimpleNamespace
        monkeypatch.setattr(relay_module, "RelayServer", lambda *args, **kwargs: SimpleNamespace(
            bind=lambda: None, serve=lambda fd, **kwargs: os.close(fd), stop=lambda: None))
    previous = os.umask(0o077)
    try:
        guardian.run_guardian(["fake-provider"], attempt_dir=tmp_path, cwd=str(tmp_path), stdin_path=None,
                              stdout_path=str(tmp_path / "stdout"), stderr_path=str(tmp_path / "stderr"),
                              env_pass=passed, control_socket=str(tmp_path / "relay.sock") if relay else None)
    finally:
        os.umask(previous)
    if pythonpath is None:
        assert "PYTHONPATH" not in seen[0]
    else:
        assert seen[0]["PYTHONPATH"] == pythonpath
    assert "SUBFLEET_EXIT_SETTLE_S" not in seen[0]
    assert not any(name.startswith("_SUBFLEET_") for name in seen[0])


def test_review_r2_real_case_reaches_no_codex_guardian(routing_state, monkeypatch):
    """Review r2: the env-kind example set in the daemon's shell reached a Codex lane."""
    service, _ = routing_state
    argv, env = daemon_spawn_environment(routing_state, monkeypatch, "detached",
                                         extra={"SUBFLEET_TEST_OAUTH": "dummy-oauth"})
    assert "SUBFLEET_TEST_OAUTH" not in set(env) | set(argv)
    assert not any(value == "dummy-oauth" for value in env.values())
    assert "SUBFLEET_TEST_OAUTH" in service.dispatch("daemon.status", {})["environment"]["extra_names"]


# Each way a lane's reference reached other launches before round 3.
CHANNELS = {
    # Round 2's e2e policy passed both references. Validation now refuses that
    # policy; this sets it beneath validation, where the launch still withholds them.
    "policy": ("E2E_LANE_ONE_TOKEN", "E2E_LANE_TWO_TOKEN"),
    # Round 2's prefix rule admitted these to every launch.
    "prefix": ("SUBFLEET_TEST_OAUTH", "SUBFLEET_LANE_TWO_OAUTH"),
    # A harness name every launch inherits can never be one lane's credential.
    "harness": ("SUBFLEET_FAKE_LANE_ONE", "SUBFLEET_FAKE_LANE_TWO"),
}


@pytest.mark.parametrize("channel", sorted(CHANNELS))
def test_env_kind_lane_credentials_reach_only_their_own_lane(routing_state, monkeypatch, channel):
    """Review r2 P3: lane 1's launch holds neither lane 2's name nor value; Codex holds neither."""
    service, harness = routing_state
    references, values = CHANNELS[channel], ("dummy-lane-one", "dummy-lane-two")
    lanes = [Lane(f"claude-{index + 1}", "claude", f"claude:lane-{index + 1}",
                  Credential("claude", reference, "env"), None, LaneOwner.V2, False)
             for index, reference in enumerate(references)]
    for lane in lanes:
        service.store.put_lane(lane)
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", **dict(zip(references, values))})
    if channel == "policy":
        service._provider_environment.pass_names = references
    prompt = harness.workdir / "prompt.md"
    prompt.write_text("Reply OK")

    def launched(lane):
        """The daemon's own seams: credential, launch and the relaunch after a restart."""
        credential = service._credential_environment(lane)
        launch = FakeAdapter().build_launch(SimpleNamespace(workdir=str(harness.workdir)), "fake/a1",
                                            harness.workdir, lane, credential, "fake", None, prompt, None)
        return service._launch_environment(lane, launch), service._relaunch_env(lane.lane_id)

    for env in launched(service.store.get_lane("codex-1")):
        assert not set(references) & set(env)
        assert not any(value in values for value in env.values())
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in set(env)
    for own, lane in enumerate(lanes):
        if channel == "harness":
            with pytest.raises(AdapterError) as error:
                service._credential_environment(lane)
            assert error.value.reason == "credential-env-inherited"
            continue
        for env in launched(lane):
            assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == values[own], "its own credential"
            assert not set(references) & set(env)
            assert not any(value == values[1 - own] for value in env.values())


def test_daemon_refuses_a_policy_passing_a_lane_credential(tmp_path, monkeypatch):
    """Policy validation at startup, after the roster is seeded; the lock is released."""
    root = tmp_path / "state"
    root.mkdir()
    Harness(root)
    # As `routing_state` starts its daemon: no host identity, keychain or provider.
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
    monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
    monkeypatch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
    claude_code_active(monkeypatch, tmp_path / "claude")
    roster = json.loads((root / "lanes.json").read_text())
    roster.append({"lane_id": "claude-1", "provider": "claude", "account_key": "claude:lane-1",
                   "credential_kind": "env", "credential_ref": "E2E_LANE_ONE_TOKEN", "owner": "v2"})
    (root / "lanes.json").write_text(json.dumps(roster))
    policy = json.loads((root / "policy.json").read_text())
    policy["env"] = {"pass": ["E2E_LANE_ONE_TOKEN"]}
    (root / "policy.json").write_text(json.dumps(policy))
    try:
        started = daemon_module.Daemon(root)
    except PolicyError as exc:
        assert exc.key == "env.pass[0]"
    else:
        started.close()
        pytest.fail("a policy passing a lane credential started the daemon")
    policy["env"] = {"pass": []}
    (root / "policy.json").write_text(json.dumps(policy))
    daemon_module.Daemon(root).close()


def startup_root(tmp_path, monkeypatch, reference, passed=()):
    """A seeded root whose roster adds one env-kind Claude lane, as `routing_state` starts it."""
    root = tmp_path / "state"
    root.mkdir()
    Harness(root)
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
    monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
    monkeypatch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
    claude_code_active(monkeypatch, tmp_path / "claude")
    roster = json.loads((root / "lanes.json").read_text())
    roster.append({"lane_id": "claude-9", "provider": "claude", "account_key": "claude:lane-9",
                   "credential_kind": "env", "credential_ref": reference, "owner": "v2"})
    (root / "lanes.json").write_text(json.dumps(roster))
    policy = json.loads((root / "policy.json").read_text())
    policy["env"] = {"pass": list(passed)}
    (root / "policy.json").write_text(json.dumps(policy))
    return root, policy


@pytest.mark.parametrize("reference", ["HOME", "SUBFLEET_FAKE_LANE_NINE", "SUBFLEET_HOST_HOME"])
def test_daemon_startup_refuses_a_lane_variable_every_launch_inherits(tmp_path, monkeypatch, reference):
    """Review r3, P3-1: the start check is the first line of defence, at daemon startup too."""
    root, policy = startup_root(tmp_path, monkeypatch, reference)
    with pytest.raises(PolicyError) as error:
        daemon_module.Daemon(root).close()
    assert error.value.key == "credential_ref" and reference in str(error.value)
    # The refusal released the lock: a second start reaches the same check
    # (from the store now), not "already running".
    with pytest.raises(PolicyError) as again:
        daemon_module.Daemon(root).close()
    assert again.value.key == "credential_ref"


@pytest.mark.parametrize("case", ["env-pass", "inherited"])
def test_daemon_main_exits_two_on_a_refused_startup(tmp_path, monkeypatch, capsys, case):
    """Review r3, P3-3 (C-11.1): exit 2 with the message, never a traceback in daemon.log."""
    if case == "env-pass":
        root, _ = startup_root(tmp_path, monkeypatch, "E2E_LANE_NINE_TOKEN",
                               passed=["GIT_CONFIG_GLOBAL", "E2E_LANE_NINE_TOKEN"])
        key = "env.pass[1]"
    else:
        root, _ = startup_root(tmp_path, monkeypatch, "LANG")
        key = "credential_ref"
    monkeypatch.setenv("E2E_LANE_NINE_TOKEN", "dummy-nine")
    monkeypatch.setattr(daemon_module.descriptors, "raise_open_file_limit", lambda: (256, 256, 256))
    monkeypatch.setattr(daemon_module.Daemon, "serve_forever",
                        lambda self: pytest.fail("a refused startup served"))
    assert daemon_module.main(["--state-root", str(root)]) == 2
    err = capsys.readouterr().err
    assert key in err and "Traceback" not in err and "dummy-nine" not in err


def test_daemon_status_lists_a_lane_variable_apart(tmp_path, monkeypatch):
    """Review r3, P3-2: the daemon holds its lane's variable, and says so apart from extras."""
    root, _ = startup_root(tmp_path, monkeypatch, "SUBFLEET_TEST_OAUTH")
    service = daemon_module.Daemon(root)
    try:
        monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", "SUBFLEET_TEST_OAUTH": "dummy-oauth",
                                           "GH_TOKEN": "dummy-gh"})
        status = service.dispatch("daemon.status", {})["environment"]
    finally:
        service.close()
    assert status == {"extra_names": ["GH_TOKEN"], "lane_credentials": ["SUBFLEET_TEST_OAUTH"]}
    assert "dummy" not in json.dumps(status)
