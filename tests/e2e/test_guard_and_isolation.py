"""Guard refusal and provider environment isolation through the real CLI and daemon."""

import json
from pathlib import Path

import pytest


TRUST = Path(__file__).resolve().parents[2] / "subfleet" / "guard" / "TRUST"
API_KEYS = ("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def test_changed_guard_trust_refuses_codex_with_the_fix(e2e):
    """C-14.2, C-17.3: a mismatched TRUST refuses the real CLI before provider launch."""
    trust = json.loads(TRUST.read_text())
    trust["hook_sha256"] = "0" * 64
    wrong_trust = e2e.root / "wrong-TRUST"
    wrong_trust.write_text(json.dumps(trust))
    e2e.start(env={"SUBFLEET_GUARD_TRUST": str(wrong_trust)})

    refused = e2e.cli(*e2e.run_args("astra", "--wait"))
    assert refused.rc == 7, refused
    assert "SHA-256" in refused.stderr
    assert "fix:" in refused.stderr
    assert "subfleet doctor" in refused.stderr
    assert not (e2e.root / "codex-env.json").exists()
    for attempt in e2e.rows("SELECT * FROM attempts"):
        assert attempt["guardian_pid"] is None
        assert attempt["child_pid"] is None


@pytest.mark.parametrize("model,scenario,provider", [
    ("astra", "success", "codex"),
    ("haiku", "success-allowed", "claude"),
])
@pytest.mark.parametrize("sandbox", ["read-only", "workspace-write"])
def test_provider_environment_isolated_in_each_sandbox(e2e, model, scenario, provider, sandbox):
    """C-5.1, C-12.3, C-12.4, C-14.4: both sandboxes strip API keys and tag the child."""
    e2e.start(scenario=scenario)
    result = e2e.cli(*e2e.run_args(model, "-s", sandbox, "--in-place", "--wait"))
    assert result.rc == 0, result
    job_id = result.stdout.strip()
    attempt, = e2e.attempts(job_id)
    assert attempt["state"] == "succeeded"
    report = json.loads((e2e.root / f"{provider}-env.json").read_text())

    if provider == "codex":
        for name in API_KEYS:
            assert report["env"].get(name) is None
        assert report["env"]["SUBFLEET_JOB"] == job_id
        assert report["env"]["SUBFLEET_ATTEMPT"] == attempt["attempt_id"]
        assert report["env"]["CODEX_HOME"]
        assert report["argv"][report["argv"].index("--sandbox") + 1] == sandbox
        if sandbox == "workspace-write":
            assert any(arg.startswith("hooks={") for arg in report["argv"])
    else:
        for name in API_KEYS:
            assert not report["env_present"][name]
            assert name not in report["env_keys"]
        assert report["env_present"]["SUBFLEET_JOB"]
        assert report["env_present"]["SUBFLEET_ATTEMPT"]
        assert report["env_present"]["CLAUDE_CODE_OAUTH_TOKEN"]
        if sandbox == "read-only":
            assert report["argv"][report["argv"].index("--permission-mode") + 1] == "plan"
            assert "--safe-mode" in report["argv"]
        else:
            assert "--dangerously-skip-permissions" in report["argv"]

    assert Path(report["cwd"]).resolve() == e2e.workdir.resolve()
