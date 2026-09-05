"""C-14.4 and C-12.8: what actually reaches a Claude child process.

These tests spawn the fake provider with the `Launch` the adapter built, applying
`env_remove` and `env_add` the way the guardian does (C-5.1), and then read what the
child saw. They prove three things end to end, offline: no API key survives into the
child, the credential does reach it, and the attempt's stdout, stderr, and prompt land
in the attempt directory.

They do not claim an OS sandbox. Claude Code has none: `read-only` is enforced by the
permission flags in the argv, so what is asserted is that those flags are present and
that the writing bypass is absent.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from subfleet.adapters.claude import ClaudeAdapter, link_raw_stream
from subfleet.contracts import HEADLESS_MARKER, Sandbox
from tests.conftest import FAKE_CLAUDE, FIXTURES, NOW, make_job, make_lane

pytestmark = pytest.mark.skipif(os.name != "posix", reason="posix process semantics")

SESSION_ID = "5eab1e00-0000-4000-8000-00000000dead"


def _adapter(tmp_path: Path) -> ClaudeAdapter:
    projects = tmp_path / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    return ClaudeAdapter(
        claude_bin=str(FAKE_CLAUDE),
        now=lambda: NOW,
        new_session_id=lambda: SESSION_ID,
        projects_dir=projects,
    )


def _build(tmp_path: Path, sandbox: Sandbox, *, prompt="Do the work.\n"):
    adapter = _adapter(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    job = make_job(str(workdir), str(prompt_path), sandbox=sandbox)
    launch = adapter.build_launch(
        job, "job-1/a1", tmp_path / "a1", make_lane(),
        {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-NOT-A-REAL-TOKEN"},
        "claude-opus-5", None, prompt_path, None,
    )
    return adapter, launch


def _spawn(launch, *, scenario: str, extra_env: dict[str, str] | None = None,
           parent_env: dict[str, str] | None = None) -> tuple[int, dict]:
    """Run the launch the way the guardian does (C-5.1): a sanitised parent
    environment, `env_remove` stripped, `env_add` merged, the prompt on stdin, and
    stdout and stderr redirected into the attempt directory."""
    env = dict(os.environ)
    env.update(parent_env or {})
    for key in launch.env_remove:
        env.pop(key, None)
    env.update(launch.env_add)
    env["SUBFLEET_FAKE_SCENARIO"] = scenario
    env["SUBFLEET_FAKE_FIXTURES"] = str(FIXTURES)
    # The daemon's own additions (C-5.1); the adapter must not have to add them.
    env["SUBFLEET_ATTEMPT"] = "job-1/a1"
    env["SUBFLEET_JOB"] = "job-1"
    env.update(extra_env or {})

    report = Path(launch.stdout_path).parent / "env-report.json"
    env["SUBFLEET_FAKE_ENV_REPORT"] = str(report)
    env["SUBFLEET_FAKE_STDIN_REPORT"] = str(
        Path(launch.stdout_path).parent / "stdin-seen"
    )

    with open(launch.stdin_path, "rb") as stdin, \
            open(launch.stdout_path, "wb") as stdout, \
            open(launch.stderr_path, "wb") as stderr:
        rc = subprocess.call(
            list(launch.argv), cwd=launch.cwd, env=env,
            stdin=stdin, stdout=stdout, stderr=stderr,
        )
    return rc, json.loads(report.read_text(encoding="utf-8"))


@pytest.mark.parametrize("sandbox", [Sandbox.READ_ONLY, Sandbox.WORKSPACE_WRITE])
def test_no_anthropic_api_key_reaches_the_child(tmp_path, sandbox):
    """C-14.4, C-12.4 a lane bills the subscription through its OAuth token. Even with
    `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` set in the parent, neither survives
    `env_remove` into the child."""
    _adapter_, launch = _build(tmp_path, sandbox)
    _rc, report = _spawn(
        launch, scenario="success-allowed",
        parent_env={
            "ANTHROPIC_API_KEY": "sk-ant-api03-MUST-NOT-LEAK",
            "ANTHROPIC_AUTH_TOKEN": "MUST-NOT-LEAK-EITHER",
        },
    )
    assert report["env_present"]["ANTHROPIC_API_KEY"] is False
    assert report["env_present"]["ANTHROPIC_AUTH_TOKEN"] is False
    assert "ANTHROPIC_API_KEY" not in report["env_keys"]
    assert "ANTHROPIC_AUTH_TOKEN" not in report["env_keys"]


def test_the_codex_and_openai_keys_are_irrelevant_but_recorded(tmp_path):
    """C-14.4 the isolation matrix covers all three keys. The Claude adapter removes
    the Anthropic pair; `CODEX_API_KEY` and `OPENAI_API_KEY` are the Codex adapter's
    to strip, and this test records which side each removal belongs to so a future
    change cannot quietly move one."""
    _adapter_, launch = _build(tmp_path, Sandbox.READ_ONLY)
    assert set(launch.env_remove) == {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}


def test_the_credential_reaches_the_child_and_appears_nowhere_else(tmp_path):
    """C-10.5, C-12.4 the token is in the child's environment and in no artifact: not
    in argv, not in the sent prompt, not in stdout, stderr, or the launch's notes."""
    _adapter_, launch = _build(tmp_path, Sandbox.READ_ONLY)
    _rc, report = _spawn(launch, scenario="success-allowed")
    assert report["env_present"]["CLAUDE_CODE_OAUTH_TOKEN"] is True

    secret = "sk-ant-oat01-NOT-A-REAL-TOKEN"
    assert secret not in " ".join(report["argv"])
    attempt_dir = Path(launch.stdout_path).parent
    for path in sorted(attempt_dir.iterdir()):
        if path.name == "env-report.json":
            continue
        assert secret.encode() not in path.read_bytes(), path
    assert secret not in json.dumps(launch.notes)


def test_the_daemons_own_variables_reach_the_child(tmp_path):
    """C-5.1 `SUBFLEET_ATTEMPT` and `SUBFLEET_JOB` are the daemon's to add — the
    adapter must not duplicate them — and containment (C-5.5) finds a process by them."""
    _adapter_, launch = _build(tmp_path, Sandbox.READ_ONLY)
    assert "SUBFLEET_ATTEMPT" not in launch.env_add
    assert "SUBFLEET_JOB" not in launch.env_add
    _rc, report = _spawn(launch, scenario="success-allowed")
    assert report["env_present"]["SUBFLEET_ATTEMPT"] is True
    assert report["env_present"]["SUBFLEET_JOB"] is True


def test_stdout_stderr_and_the_sent_prompt_land_in_the_attempt_directory(tmp_path):
    """C-2.3, C-6.7 every artifact of the attempt is inside `a<seq>/`, and stdin was
    the prompt the adapter wrote, headless block and all."""
    _adapter_, launch = _build(tmp_path, Sandbox.READ_ONLY)
    attempt_dir = tmp_path / "a1"
    rc, _report = _spawn(launch, scenario="success-allowed")

    assert rc == 0
    assert Path(launch.stdout_path).parent == attempt_dir
    assert Path(launch.stderr_path).parent == attempt_dir
    assert Path(launch.stdin_path).parent == attempt_dir
    stream = Path(launch.stdout_path).read_text(encoding="utf-8")
    assert '"type":"rate_limit_event"' in stream
    seen = (attempt_dir / "stdin-seen").read_bytes().decode("utf-8")
    assert seen.startswith(HEADLESS_MARKER)
    assert seen.endswith("Do the work.\n")
    assert seen == Path(launch.stdin_path).read_text(encoding="utf-8")


def test_the_run_classifies_end_to_end_from_the_files_it_wrote(tmp_path):
    """C-9.8, C-12.2 a launch, a real child process, and then classification off the
    files that child produced — the whole path with no store and no daemon."""
    from subfleet.contracts import ExitInfo, OutcomeClass, ReadingLabel

    adapter, launch = _build(tmp_path, Sandbox.READ_ONLY)
    rc, _report = _spawn(launch, scenario="success-allowed")
    attempt_dir = tmp_path / "a1"

    # stream-json writes the stream to stdout; publishing it as its own artifact is a
    # link, not a copy (C-8.2).
    stream = link_raw_stream(attempt_dir, launch)
    assert stream is not None and stream.exists()
    assert stream.read_bytes() == Path(launch.stdout_path).read_bytes()

    outcome = adapter.classify(
        attempt_dir, launch,
        ExitInfo(rc=rc, signal=None, wall_s=0.1, child_pid=None),
    )
    assert outcome.cls is OutcomeClass.OK
    assert {r.window: r.utilization for r in outcome.readings} == {
        "five_hour": 0.05, "seven_day": 0.25
    }
    assert all(r.label is ReadingLabel.PROVIDER for r in outcome.readings)
    assert adapter.deliverable(attempt_dir, launch, outcome) == b"ok"


def test_read_only_carries_the_flags_that_fail_closed(tmp_path):
    """C-14.3, C-12.4 Claude has no OS sandbox: `read-only` is the argv. Plan mode, a
    named tool surface, no settings sources, no MCP, no slash commands — and never the
    bypass flag."""
    _adapter_, launch = _build(tmp_path, Sandbox.READ_ONLY)
    _rc, report = _spawn(launch, scenario="success-allowed")
    argv = report["argv"]
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert "--safe-mode" in argv and "--strict-mcp-config" in argv
    assert "Bash" not in argv[argv.index("--tools") + 1]
    assert "Write" not in argv[argv.index("--allowedTools") + 1]


def test_workspace_write_carries_the_bypass_and_nothing_else(tmp_path):
    """C-14.3 a writable Claude launch relies on the global never-rules hook in
    `~/.claude/settings.json`; there is no per-launch guard override to pass."""
    _adapter_, launch = _build(tmp_path, Sandbox.WORKSPACE_WRITE)
    _rc, report = _spawn(launch, scenario="success-allowed")
    assert report["argv"][-1] == "--dangerously-skip-permissions"
    assert "--permission-mode" not in report["argv"]


def test_a_guard_override_is_accepted_and_never_reaches_the_argv(tmp_path):
    """C-14.3 the Codex guard override has no Claude equivalent; passing one must not
    smuggle an unknown flag into the launch line."""
    adapter = _adapter(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("x\n", encoding="utf-8")
    job = make_job(str(workdir), str(prompt_path), sandbox=Sandbox.WORKSPACE_WRITE)
    launch = adapter.build_launch(
        job, "job-1/a1", tmp_path / "a1", make_lane(), {}, "claude-opus-5", None,
        prompt_path, "hooks=/tmp/guard.json",
    )
    assert "hooks=/tmp/guard.json" not in launch.argv
    assert "--settings" not in launch.argv


def test_the_child_writes_the_transcript_the_launch_predicted(tmp_path):
    """C-12.5 the transcript path recorded at launch is where the session's transcript
    actually appears, so attestation finds exactly one file."""
    from subfleet.contracts import ExitInfo

    adapter, launch = _build(tmp_path, Sandbox.READ_ONLY)
    projects = tmp_path / "projects"
    rc, _report = _spawn(
        launch, scenario="model-downgrade",
        extra_env={"CLAUDE_FAKE_PROJECTS_DIR": str(projects)},
    )
    predicted = Path(launch.notes["transcript_path"])
    assert predicted.exists(), sorted(p for p in projects.rglob("*.jsonl"))
    assert predicted.parent.parent == projects

    outcome = adapter.classify(
        tmp_path / "a1", launch, ExitInfo(rc=rc, signal=None, wall_s=0.1, child_pid=None)
    )
    # The transcript the child wrote records claude-opus-5, which is what this launch
    # asked for: attested, from exactly one file at the predicted path.
    served = adapter.attest(tmp_path / "a1", launch, outcome, "claude-opus-5")
    assert served.status.value == "attested"
    assert served.served_model == "claude-opus-5"
    assert str(predicted) in served.evidence
    # The same transcript against a different request is the downgrade verdict.
    downgraded = adapter.attest(tmp_path / "a1", launch, outcome, "claude-fable-5-1")
    assert downgraded.status.value == "mismatch"
    assert downgraded.served_model == "claude-opus-5"


def test_the_fake_honours_the_scenario_delay(tmp_path):
    """C-12.8 `SUBFLEET_FAKE_DELAY_S` makes a fake run take measurable time, so a
    timeout or kill test has something to interrupt."""
    _adapter_, launch = _build(tmp_path, Sandbox.READ_ONLY)
    started = time.monotonic()
    _rc, _report = _spawn(
        launch, scenario="success-allowed", extra_env={"SUBFLEET_FAKE_DELAY_S": "0.4"}
    )
    assert time.monotonic() - started >= 0.4


@pytest.mark.parametrize(
    "scenario,expected_rc", [("success-allowed", 0), ("rejected-credits-fable", 1),
                             ("spawn-failure", 127)])
def test_the_fake_replays_each_fixtures_exit_status(tmp_path, scenario, expected_rc):
    """C-12.8 the fake replays a fixture's stdout, stderr, and rc exactly."""
    _adapter_, launch = _build(tmp_path, Sandbox.READ_ONLY)
    rc, _report = _spawn(launch, scenario=scenario)
    assert rc == expected_rc
    assert Path(launch.stderr_path).read_bytes() == (
        FIXTURES / scenario / "stderr"
    ).read_bytes()
