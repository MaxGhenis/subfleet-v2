"""Launch/environment wiring with a fake, not a proof of OS sandbox enforcement.

The daemon/guardian lane owns enforcement of C-14.4's write-isolation matrix.
These tests exercise the Codex launch under both sandbox requests and the
credential sanitation required at the process boundary, without importing it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

import pytest

from subfleet.adapters.codex import CodexAdapter
from subfleet.contracts import Credential, JobSpec, Lane, LaneOwner, Sandbox


TESTS = Path(__file__).resolve().parents[1]
FAKE_CODEX = TESTS / "bin" / "codex"
FIXTURES = TESTS / "fixtures" / "codex"
API_KEYS = ("CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
SCENARIOS = (
    "success", "limit-with-clock", "limit-no-clock", "credits-rejection", "auth-401",
    "refresh-token-revoked", "cli-too-old", "content-filter", "content-cyber-flag", "stream-disconnect",
    "model-at-capacity", "spawn-fail", "model-scoped-limit",
)


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


def _fake_environment(launch, scenario, **extra_env):
    # A caller must merge additions over its already sanitized environment.
    # The Codex adapter's own env_remove deliberately names its two API keys;
    # the guardian boundary strips the third provider's key as well.
    env = {name: value for name, value in os.environ.items() if name not in API_KEYS}
    env.update(launch.env_add)
    env.update(SUBFLEET_FAKE_SCENARIO=scenario, **extra_env)
    for name in launch.env_remove:
        env.pop(name, None)
    return env


def _spawn_fake(launch, scenario, **extra_env):
    env = _fake_environment(launch, scenario, **extra_env)
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


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_fake_replays_exact_fixture_streams_and_exit_code(tmp_path, scenario):
    """C-12.7 C-12.8 The Python fake reproduces redacted fixture stdout, stderr and raw rc exactly."""
    launch = _launch(tmp_path)
    completed = _spawn_fake(launch, scenario)
    fixture = FIXTURES / scenario
    assert completed.returncode == int((fixture / "rc").read_text())
    assert Path(launch.raw_stream_path).read_bytes() == (fixture / "stdout").read_bytes()
    assert Path(launch.stderr_path).read_bytes() == (fixture / "stderr").read_bytes()
    assert (Path(launch.stderr_path).parent / "last.md").exists() is (scenario == "success")


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


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_fixture_provenance_and_redaction(scenario):
    """C-12.7 Every fixture declares its provenance and omits credentials and personal absolute paths."""
    fixture = FIXTURES / scenario
    expected = json.loads((fixture / "expected.json").read_text())
    assert {"class", "scope", "clock_source", "closure", "native_session_id"} <= expected.keys()
    assert isinstance(expected["synthetic"], bool)
    provenance = expected["provenance"]
    if expected["synthetic"]:
        assert provenance["source"] == "synthetic"
        assert provenance["reason"]
    elif provenance["source"] == "v1":
        assert provenance["run"]
        assert {"meta.json", "err.log", "out.md"} <= set(provenance["files"])
        assert provenance["normalization"]  # The source launcher did not emit JSONL.
    else:
        # A v2 attempt's own JSONL stream, events kept verbatim.
        assert provenance["source"] == "v2"
        assert provenance["run"]
        assert {"a1/stream.jsonl", "a1/stderr"} <= set(provenance["files"])
        assert provenance["normalization"]
    for path in fixture.iterdir():
        text = path.read_text()
        assert not re.search(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+", text), path.name
        assert not re.search(r"\bsk-[A-Za-z0-9_-]{12,}", text), path.name
        assert not re.search(r"(?i)\b(?:authorization|cookie|set-cookie)\s*[:=]", text), path.name
        assert not re.search(r"/(?:Users|home)/[^/\s]+/", text), path.name
    if scenario == "auth-401":
        assert "max@policyengine.org" in (fixture / "stderr").read_text()


def test_nested_setsid_fake_starts_detached_grandchild(tmp_path):
    """C-12.8 A fake grandchild leads its own session and survives the provider's termination."""
    launch = _launch(tmp_path)
    diagnostics_path = Path(launch.stderr_path).parent / "fake-diagnostics.json"
    env = _fake_environment(
        launch, "nested-setsid", SUBFLEET_FAKE_DIAGNOSTICS_PATH=str(diagnostics_path),
    )
    grandchild_pid = None
    with (
        open(launch.stdin_path, "rb") as stdin,
        open(launch.raw_stream_path, "wb") as stdout,
        open(launch.stderr_path, "wb") as stderr,
        subprocess.Popen(
            launch.argv, cwd=launch.cwd, env=env, stdin=stdin, stdout=stdout,
            stderr=stderr, start_new_session=True,
        ) as child,
    ):
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    grandchild_pid = json.loads(diagnostics_path.read_text())["nested_grandchild_pid"]
                    break
                except (FileNotFoundError, json.JSONDecodeError):
                    assert child.poll() is None, Path(launch.stderr_path).read_text()
                    time.sleep(0.01)
            assert grandchild_pid is not None, "fake did not publish its detached grandchild"
            assert os.getpgid(child.pid) == child.pid
            assert os.getpgid(grandchild_pid) == grandchild_pid
            assert os.getsid(grandchild_pid) == grandchild_pid
            child.terminate()
            child.wait(timeout=2)
            os.kill(grandchild_pid, 0)
        finally:
            if grandchild_pid is not None:
                try:
                    os.kill(grandchild_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
