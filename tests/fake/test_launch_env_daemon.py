"""D-ENV1: the actual daemon spawn sites cannot bypass environment selection."""

import json
import os

import pytest

from subfleet import daemon as daemon_module
from subfleet.provider_env import ProviderEnvironment
from tests.fake.test_probe_recovery import reserved_probe
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401
from tests.unit.test_launch_env_allowlist import ALLOWED, POLICY, POLLUTION


@pytest.mark.parametrize("mode", ["detached", "turn", "probe", "enrollment"])
def test_actual_daemon_spawn_rejects_pollution(routing_state, monkeypatch, mode):
    service, harness = routing_state
    parent = {"PATH": "/usr/bin:/bin", **POLLUTION}
    monkeypatch.setattr(os, "environ", parent)
    service._provider_environment = ProviderEnvironment(POLICY)
    seen = []
    def stopped(argv, **kwargs):
        seen.append((argv, kwargs["env"]))
        raise OSError("fake guardian spawn stopped")
    monkeypatch.setattr(daemon_module.subprocess, "Popen", stopped)
    job_id = service.dispatch("submit", harness.submit_args())["job_id"]
    if mode in {"detached", "turn"}:
        if mode == "turn":
            service.store.conn.execute("UPDATE jobs SET kind='turn' WHERE job_id=?", (job_id,))
            manifest_path = service.root / "jobs" / job_id / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["turn"] = {"fake": True}
            manifest_path.write_text(json.dumps(manifest))
            # Exercise the daemon's relay branch; turn builders have their own oracle.
            from tests.fake_adapter import FakeAdapter
            monkeypatch.setattr(service.conversations, "launch", lambda job, attempt, lane, creds, adir, model, guard:
                FakeAdapter().build_launch(service._spec(job), attempt["attempt_id"], adir, lane,
                                           creds, model, None, daemon_module.Path(job["prompt_path"]), None))
            monkeypatch.setattr(service, "_guard_override", lambda *args, **kwargs: None)
        service._admit()
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
    assert set(env) - ALLOWED - {"PYTHONPATH"} <= passed
