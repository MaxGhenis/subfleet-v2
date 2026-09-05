"""Launch/environment wiring with a fake, not a proof of OS sandbox enforcement.

The daemon/guardian lane owns enforcement of C-14.4's write-isolation matrix.
These tests exercise the Codex launch under both sandbox requests and the
credential sanitation required at the process boundary, without importing it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from subfleet.adapters.codex import CodexAdapter
from subfleet.contracts import Credential, JobSpec, Lane, LaneOwner, Sandbox


TESTS = Path(__file__).resolve().parents[1]
FAKE_CODEX = TESTS / "bin" / "codex"
FIXTURES = TESTS / "fixtures" / "codex"
API_KEYS = ("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def _launch(tmp_path, sandbox=Sandbox.READ_ONLY):
    workdir = tmp_path / "workdir"
    attempt = tmp_path / "job" / "a1"
    home = tmp_path / "codex-home"
    for directory in (workdir, attempt, home):
        directory.mkdir(parents=True)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Return the fixture's final message.\n")
    job = JobSpec(
        request_id="process-test", kind="dispatch", workdir=str(workdir),
        prompt_path=str(prompt), task=None, tier=None, pinned_model="gpt-6-astra",
        pinned_lane="codex-4", sandbox=sandbox, out_path=None, name="isolation",
    )
    lane = Lane("codex-4", "codex", "codex:account-4", Credential("codex", str(home), "home"),
                str(home), LaneOwner.V2, False)
    return CodexAdapter(codex_bin=str(FAKE_CODEX)).build_launch(
        job, "job/a1", attempt, lane,
        {"CODEX_HOME": str(home), "SUBFLEET_ATTEMPT": "job/a1", "SUBFLEET_JOB": "job"},
        "gpt-6-astra", "high", prompt, 'hooks={PreToolUse=[]}',
    )


def _spawn_fake(launch, scenario, **extra_env):
    # A caller must merge additions over its already sanitized environment.
    # The Codex adapter's own env_remove deliberately names its two API keys;
    # the guardian boundary strips the third provider's key as well.
    env = {name: value for name, value in os.environ.items() if name not in API_KEYS}
    env.update(launch.env_add)
    env.update(SUBFLEET_FAKE_SCENARIO=scenario, **extra_env)
    for name in launch.env_remove:
        env.pop(name, None)
    stream_path = Path(launch.raw_stream_path)
    with open(launch.stdin_path, "rb") as stdin, stream_path.open("wb") as stdout, open(launch.stderr_path, "wb") as stderr:
        return subprocess.run(launch.argv, cwd=launch.cwd, env=env, stdin=stdin,
                              stdout=stdout, stderr=stderr, timeout=5, check=False)


@pytest.mark.parametrize("sandbox", list(Sandbox))
def test_fake_child_receives_sandbox_stdin_and_sanitized_environment(tmp_path, monkeypatch, sandbox):
    """C-5.1 C-12.3 C-14.4 Both sandbox requests reach a child with stdin and no provider API keys."""
    for key in API_KEYS:
        monkeypatch.setenv(key, f"test-secret-{key}")
    launch = _launch(tmp_path, sandbox)
    diagnostics_path = Path(launch.stderr_path).parent / "fake-diagnostics.json"
    completed = _spawn_fake(launch, "success", SUBFLEET_FAKE_DIAGNOSTICS_PATH=str(diagnostics_path))
    assert completed.returncode == 0
    diagnostics = json.loads(diagnostics_path.read_text())
    assert diagnostics["argv"][diagnostics["argv"].index("--sandbox") + 1] == sandbox.value
    assert diagnostics["cwd"] == launch.cwd
    assert diagnostics["stdin"] == Path(launch.stdin_path).read_text()
    for key in API_KEYS:
        assert diagnostics["env"][key] is None
        assert os.environ[key] == f"test-secret-{key}"
    for key in ("CODEX_HOME", "SUBFLEET_JOB", "SUBFLEET_ATTEMPT"):
        assert diagnostics["env"][key] == launch.env_add[key]
    assert Path(launch.raw_stream_path).parent == Path(launch.stderr_path).parent
    assert Path(launch.raw_stream_path).read_bytes() == (FIXTURES / "success" / "stdout").read_bytes()
    assert (Path(launch.stderr_path).parent / "last.md").read_bytes()


@pytest.mark.parametrize("scenario", [
    "success", "limit-with-clock", "limit-no-clock", "credits-rejection", "auth-401",
    "refresh-token-revoked", "cli-too-old", "content-filter", "stream-disconnect",
    "model-at-capacity", "spawn-fail", "model-scoped-limit",
])
def test_fake_replays_exact_fixture_streams_and_exit_code(tmp_path, scenario):
    """C-12.7 C-12.8 The Python fake reproduces redacted fixture stdout, stderr and raw rc exactly."""
    launch = _launch(tmp_path)
    completed = _spawn_fake(launch, scenario)
    fixture = FIXTURES / scenario
    assert completed.returncode == int((fixture / "rc").read_text())
    assert Path(launch.raw_stream_path).read_bytes() == (fixture / "stdout").read_bytes()
    assert Path(launch.stderr_path).read_bytes() == (fixture / "stderr").read_bytes()


def test_fake_honors_delay_and_output_last_message(tmp_path):
    """C-12.8 The fake honors its bounded delay and writes the final agent message to the requested file."""
    launch = _launch(tmp_path)
    started = time.monotonic()
    completed = _spawn_fake(launch, "success", SUBFLEET_FAKE_DELAY_S="0.05")
    elapsed = time.monotonic() - started
    assert completed.returncode == 0
    assert elapsed >= 0.05
    events = [json.loads(line) for line in (FIXTURES / "success" / "stdout").read_text().splitlines() if line.strip()]
    messages = [event["item"]["text"] for event in events if event.get("type") == "item.completed"
                and event.get("item", {}).get("type") == "agent_message"]
    assert (Path(launch.stderr_path).parent / "last.md").read_text() == messages[-1]
