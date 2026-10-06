"""Execute the fake fixture itself without daemon process inspection."""

import json
import os
from pathlib import Path
import subprocess

import pytest

from subfleet.contracts import Credential, ExitInfo, JobSpec, Lane, LaneOwner, Sandbox
from tests import waits
from tests.fake_adapter import FakeAdapter


@pytest.mark.parametrize("scenario,rc,cls", [("ok", 0, "ok"),
                                            ("rc4-limit-with-clock", 4, "limited"),
                                            ("rc1-crash-after-output", 1, "unknown")])
def test_c12_8_fake_provider_replays_outcomes_and_deliverable(tmp_path, scenario, rc, cls):
    """C-12.1, C-12.8 fake provider scenarios produce adapter outcomes and exact stdout bytes."""
    prompt = tmp_path / "prompt.md"
    prompt.write_text(json.dumps({"scenario": scenario}))
    credential = Credential("codex", str(tmp_path / "home"), "home")
    lane = Lane("codex-1", "codex", "codex:fake", credential, credential.ref, LaneOwner.V2, False)
    spec = JobSpec("fixture", "dispatch", str(tmp_path), str(prompt), None, None,
                   "astra", "codex-1", Sandbox.READ_ONLY, None, "fake")
    adapter = FakeAdapter()
    launch = adapter.build_launch(spec, "fixture/a1", tmp_path, lane, {}, "fake-model", None,
                                  prompt, None)
    env = {key: value for key, value in os.environ.items() if key not in launch.env_remove}
    env.update(launch.env_add)
    result = waits.run(launch.argv, input=prompt.read_bytes(), cwd=launch.cwd,
                       capture_output=True, env=env, check=False, timeout=3)
    assert result.returncode == rc
    Path(launch.stdout_path).write_bytes(result.stdout)
    Path(launch.stderr_path).write_bytes(result.stderr)
    outcome = adapter.classify(tmp_path, launch, ExitInfo(rc, None, 0, None))
    assert outcome.cls.value == cls
    assert adapter.deliverable(tmp_path, launch, outcome) == result.stdout
    if rc == 4:
        clock = json.loads(result.stderr)["resets_at"]
        assert outcome.closure.clock_source.value == "reported"
        assert outcome.closure.scope == "account"
        assert outcome.evidence["quota"]["resets_at"] == clock


def test_c5_2_fake_spawn_failure_uses_a_missing_executable(tmp_path):
    """C-5.2, C-12.8 spawn-fail supplies a nonexistent argv executable to the guardian."""
    prompt = tmp_path / "prompt.md"
    prompt.write_text('{"scenario":"spawn-fail"}')
    credential = Credential("codex", str(tmp_path / "home"), "home")
    lane = Lane("codex-1", "codex", "codex:fake", credential, credential.ref, LaneOwner.V2, False)
    spec = JobSpec("fixture", "dispatch", str(tmp_path), str(prompt), None, None,
                   "astra", "codex-1", Sandbox.READ_ONLY, None, "fake")
    launch = FakeAdapter().build_launch(spec, "fixture/a1", tmp_path, lane, {}, "fake-model", None,
                                        prompt, None)
    assert not Path(launch.argv[0]).exists()
    with pytest.raises(FileNotFoundError):
        subprocess.run(launch.argv, cwd=launch.cwd, check=False)


@pytest.mark.parametrize("scenario", ["nested-setsid", "ignore-sigterm", "rc4-limit-with-clock", "spawn-fail"])
@pytest.mark.parametrize("indent", [None, 2], ids=["single-line", "multiline"])
def test_c6_7_fake_scenario_survives_preambles_and_checkpoint_suffix(tmp_path, scenario, indent):
    """C-6.7, C-12.8, C-13.3 write/headless preambles and retry checkpoints preserve the fake scenario."""
    from subfleet.daemon import HEADLESS_PREAMBLE, WRITE_PREAMBLE
    prompt = tmp_path / "prompt.md"
    marker = str(tmp_path / "escaped.pid")
    settings = {"scenario": scenario, "delay_s": .25, "marker": marker}
    sent = (WRITE_PREAMBLE + HEADLESS_PREAMBLE + json.dumps(settings, indent=indent)
            + "\n\nContinue from checkpoint abcdef. Preserved snapshots: fixture-ref.\n")
    prompt.write_text(sent)
    credential = Credential("codex", str(tmp_path / "home"), "home")
    lane = Lane("codex-1", "codex", "codex:fake", credential, credential.ref, LaneOwner.V2, False)
    spec = JobSpec("fixture", "dispatch", str(tmp_path), str(prompt), None, None,
                   "astra", "codex-1", Sandbox.WORKSPACE_WRITE, None, "fake")
    launch = FakeAdapter().build_launch(spec, "fixture/a2", tmp_path, lane, {}, "fake-model", None,
                                        prompt, None)
    assert launch.env_add["SUBFLEET_FAKE_SCENARIO"] == scenario
    assert launch.env_add["SUBFLEET_FAKE_DELAY_S"] == "0.25"
    assert launch.env_add["SUBFLEET_FAKE_MARKER"] == marker
    assert prompt.read_text() == sent and launch.stdin_path == str(prompt)
    if scenario == "spawn-fail":
        assert not Path(launch.argv[0]).exists()


def test_c12_8_fake_adapter_preserves_plain_prompt_without_settings(tmp_path):
    """C-12.8 plain prompts and inline JSON examples remain unchanged without a fake scenario override."""
    prompt = tmp_path / "prompt.md"
    sent = 'Explain the inline example {"scenario":"nested-setsid"}.\n{"unrelated":true}\n'
    prompt.write_text(sent)
    credential = Credential("codex", str(tmp_path / "home"), "home")
    lane = Lane("codex-1", "codex", "codex:fake", credential, credential.ref, LaneOwner.V2, False)
    spec = JobSpec("fixture", "dispatch", str(tmp_path), str(prompt), None, None,
                   "astra", "codex-1", Sandbox.READ_ONLY, None, "fake")
    launch = FakeAdapter().build_launch(spec, "fixture/a1", tmp_path, lane, {}, "fake-model", None,
                                        prompt, None)
    assert "SUBFLEET_FAKE_SCENARIO" not in launch.env_add
    assert prompt.read_text() == sent and launch.stdin_path == str(prompt)
