"""D-ENV1: the actual daemon spawn sites cannot bypass environment selection."""

import json
import os

import pytest

from subfleet import daemon as daemon_module
from subfleet.provider_env import ProviderEnvironment
from tests.fake.test_probe_recovery import reserved_probe
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401
from tests.unit.test_launch_env_allowlist import ALLOWED, POLICY, POLLUTION


def daemon_spawn_environment(routing_state, monkeypatch, mode, *, pythonpath=None):
    service, harness = routing_state
    parent = {"PATH": "/usr/bin:/bin", **POLLUTION}
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
    assert status["environment"] == {"extra_names": sorted(POLLUTION)}
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
