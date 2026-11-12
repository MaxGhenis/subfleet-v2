"""D-EFFORT-ENV1: a named Claude effort wins in the one settings source.

The whole-argv oracle is the existing pre-watchdog recording, with only the
already shipped watchdog override restored. It does not call the current
settings merger, so a change to an unnamed launch cannot bless itself.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet.adapters.claude import LANE_SETTINGS_ENV, lane_settings, lane_settings_args
from tests import lane_settings_cases as lsc


EFFORT_ENV = "CLAUDE_CODE_EFFORT_LEVEL"
EFFORTS = (None, "low", "medium", "high", "xhigh", "max")
TURN_EFFORTS = (*EFFORTS, "ultracode")
PRE_EXISTING_ENV = {"CLAUDE_CODE_RETRY_WATCHDOG": "0"}
REPO = Path(__file__).resolve().parents[2]
PINNED = json.loads((REPO / "tests/fixtures/lane_settings/argv-676906f85.json").read_text())["cases"]


def _one_settings(argv: list[str]) -> tuple[int, dict]:
    assert len(lsc.settings_payloads(argv)) == 1, argv
    index = argv.index("--settings")
    return index, json.loads(argv[index + 1])


def _applied_effort(effort: str | None) -> str | None:
    return "xhigh" if effort == "ultracode" else effort


def _before_effort(case: dict) -> list[str]:
    """Rebuild b51f08f's argv from the older recording, independently of the merge.

    The old grid pins None/high/ultracode. Other named efforts have the same
    argv shape as high; replace only that argument with the generated effort.
    """
    reference = dict(case)
    if case["effort"] not in (None, "ultracode"):
        reference["effort"] = "high"
    argv = list(PINNED[lsc.case_id(reference)])
    if case["effort"]:
        argv[argv.index("--effort") + 1] = _applied_effort(case["effort"])
    if "--settings" in argv:
        index = argv.index("--settings")
        payload = json.loads(argv[index + 1])
        restored_env = {**dict(payload.get("env") or {}), **PRE_EXISTING_ENV}
        # A job merges lane settings before installing its publish hook;
        # turns merge their already assembled settings at the end.
        if case["path"] == "job":
            payload = {"env": restored_env, **payload}
        else:
            payload["env"] = restored_env
        argv[index + 1] = json.dumps(payload, separators=(",", ":"))
    else:
        argv += ["--settings", json.dumps({"env": PRE_EXISTING_ENV}, separators=(",", ":"))]
    return argv


def _assert_effort_and_differential(case: dict, argv: list[str]) -> None:
    index, payload = _one_settings(argv)
    effort = _applied_effort(case["effort"])
    baseline = _before_effort(case)
    if effort is None:
        assert EFFORT_ENV not in payload["env"]
        assert "--effort" not in argv
        assert argv == baseline  # including the exact settings JSON bytes
    else:
        assert payload["env"][EFFORT_ENV] == effort
        assert argv[argv.index("--effort") + 1] == effort
        assert argv.count("--effort") == 1
        del payload["env"][EFFORT_ENV]
        without_pin = argv[:index + 1] + [json.dumps(payload, separators=(",", ":"))] + argv[index + 2:]
        assert without_pin == baseline  # every other setting, env key and flag
    assert payload["env"] == PRE_EXISTING_ENV
    assert dict(LANE_SETTINGS_ENV) == PRE_EXISTING_ENV


JOB_CASES = st.fixed_dictionaries({
    "path": st.just("job"),
    "sandbox": st.sampled_from(lsc.SANDBOXES),
    "task": st.sampled_from(lsc.TASKS),
    "isolated": st.booleans(),
    "effort": st.sampled_from(EFFORTS),
    "tmp": st.booleans(),
    "mcp": st.booleans(),
}).filter(lambda case: not case["isolated"] or case["sandbox"] == "read-only")

TURN_CASES = st.fixed_dictionaries({
    "path": st.just("turn"),
    "permission": st.sampled_from(lsc.PERMISSIONS),
    "fast": st.booleans(),
    "effort": st.sampled_from(TURN_EFFORTS),
    "resume": st.booleans(),
    "tmp": st.booleans(),
})

PROPERTIES = settings(max_examples=120, deadline=None, derandomize=True, database=None,
                      suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])


@PROPERTIES
@example(case=dict(path="job", sandbox="workspace-write", task="review", isolated=False,
                   effort="high", tmp=True, mcp=True))
@example(case=dict(path="job", sandbox="read-only", task="review", isolated=True,
                   effort=None, tmp=False, mcp=False))
@given(case=JOB_CASES)
def test_property_job_effort_pins_settings_and_preserves_argv(case, tmp_path):
    """I1-I4: named jobs pin both flags; unnamed jobs and every other byte stay put.

    Both sandboxes, valid isolated reviews, the publish hook, MCP and process
    credential/TMPDIR environments are generated through the real job builder.
    """
    scratch = Path(tempfile.mkdtemp(dir=tmp_path))
    job = lsc._job(scratch, case)
    credentials = lsc._credentials(scratch, case["tmp"])
    launch = lsc._adapter(scratch).build_launch(
        job, "job-1/a1", scratch / "attempt", lsc._lane(), credentials,
        lsc.MODEL, case["effort"], Path(job.prompt_path), None,
    )
    expected_env = dict(credentials)
    if case["task"] == "review":
        expected_env.update({
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_CONFIG_PARAMETERS": "'url.subfleet-publish-denied::.pushInsteadOf'='' "
                                     "'protocol.subfleet-publish-denied.allow'='never'",
            "SUBFLEET_PUBLISH_DENIED": "1",
            "PATH": str(scratch / "attempt/publish-bin") + os.pathsep + os.environ.get("PATH", os.defpath),
        })
    assert launch.env_add == expected_env  # the effort pin belongs only in settings
    _assert_effort_and_differential(case, lsc.normalize(launch.argv, scratch))


@PROPERTIES
@example(case=dict(path="turn", permission="bypass", fast=True, effort="ultracode", resume=True, tmp=True))
@example(case=dict(path="turn", permission="read-only", fast=False, effort="ultracode", resume=False, tmp=False))
@example(case=dict(path="turn", permission="ask", fast=False, effort=None, resume=True, tmp=False))
@given(case=TURN_CASES)
def test_property_turn_effort_pins_settings_and_preserves_argv(case, tmp_path):
    """I1-I4: all permissions pin named turns, including ultracode's xhigh.

    The whole-argv differential also preserves disableAllHooks, fastMode,
    ultracode, resume/session-id and the existing read-only restrictions.
    """
    scratch = Path(tempfile.mkdtemp(dir=tmp_path))
    _assert_effort_and_differential(case, lsc.build(case, scratch))


JSON_SCALARS = st.one_of(st.none(), st.booleans(), st.integers(-10, 10), st.text(max_size=10))
JSON_VALUES = st.recursive(JSON_SCALARS, lambda inner: st.one_of(
    st.lists(inner, max_size=3), st.dictionaries(st.text(max_size=10), inner, max_size=3)), max_leaves=10)
ENV_KEYS = st.one_of(st.text(min_size=1, max_size=16), st.sampled_from([EFFORT_ENV, *PRE_EXISTING_ENV]))


@settings(max_examples=180, deadline=None, derandomize=True, database=None)
@given(extra=st.dictionaries(st.text(min_size=1, max_size=16).filter(lambda key: key != "env"),
                             JSON_VALUES, max_size=6),
       env=st.one_of(st.none(), st.dictionaries(ENV_KEYS, st.text(max_size=12), max_size=6)),
       effort=st.sampled_from(EFFORTS))
def test_property_settings_merge_preserves_other_values(extra, env, effort):
    """I2-I4: arbitrary settings retain every other key and input is never mutated.

    An existing effort env value is preserved when no effort is named; naming
    one replaces only that value, alongside the pre-existing watchdog merge.
    """
    original = {**extra, **({"env": env} if env is not None else {})}
    before = copy.deepcopy(original)
    expected_env = {**(env or {}), **PRE_EXISTING_ENV}
    if effort is not None:
        expected_env[EFFORT_ENV] = effort
    expected = {**extra, "env": expected_env}
    assert lane_settings(original, effort=effort) == expected
    flags = lane_settings_args(original, effort=effort)
    _, payload = _one_settings(flags)
    assert payload == expected
    assert flags == ["--settings", json.dumps(expected, separators=(",", ":"))]
    assert original == before
    assert dict(LANE_SETTINGS_ENV) == PRE_EXISTING_ENV


@pytest.mark.skipif(os.environ.get("SUBFLEET_REAL_CLAUDE") != "1",
                    reason="opt in with SUBFLEET_REAL_CLAUDE=1; no API request is sent")
@pytest.mark.parametrize("name,user_effort,settings_effort,child_effort,expected", [
    ("user-low-cli-high", "low", None, None, "low"),
    ("settings-medium", "low", "medium", None, "medium"),
    ("user-without-effort", None, None, None, "high"),
    ("child-env-max", "low", "medium", "max", "medium"),
])
def test_real_claude_settings_effort_precedence(name, user_effort, settings_effort,
                                               child_effort, expected, tmp_path):
    """I5: get_settings resolves all four cases without a message or credential.

    SUBFLEET_CLAUDE_BIN can select the adapter/daemon binary explicitly when
    pytest's safe PATH contains a Claude stand-in. Only fresh HOME/config/cwd
    directories are used, and env -u removes inherited credential variables.
    """
    binary = os.environ.get("SUBFLEET_CLAUDE_BIN") or shutil.which("claude")
    assert binary, "a real Claude CLI is required for the opted-in premise test"
    binary = str(Path(binary).absolute())
    assert Path(binary).resolve() != (REPO / "tests/bin/claude").resolve(), "select a real SUBFLEET_CLAUDE_BIN"
    home, config, work = (tmp_path / part for part in ("home", "config", "work"))
    for path in (home, config, work):
        path.mkdir()
    user_env = {} if user_effort is None else {EFFORT_ENV: user_effort}
    (config / "settings.json").write_text(json.dumps({"env": user_env}))
    remove = sorted({key for key in os.environ if key.startswith(("CLAUDE_CODE_", "ANTHROPIC_"))} | {
        EFFORT_ENV, "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR", "CLAUDE_CONFIG_DIR_READONLY",
        "CLAUDE_CONFIG_JSON", "CLAUDE_CREDENTIAL_FILE", "CLAUDE_LANE_AGENT_SECRET", "SUBFLEET_AGENT_SECRET",
    })
    command = ["env"]
    for key in remove:
        command += ["-u", key]
    command += [f"HOME={home}", f"CLAUDE_CONFIG_DIR={config}", f"TMPDIR={tmp_path}",
                "DISABLE_AUTOUPDATER=1", "DISABLE_TELEMETRY=1", "DISABLE_ERROR_REPORTING=1"]
    if child_effort is not None:
        command += [f"{EFFORT_ENV}={child_effort}"]
    command += [binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--effort", "high"]
    if settings_effort is not None:
        command += ["--settings", json.dumps({"env": {EFFORT_ENV: settings_effort}}, separators=(",", ":"))]
    request_id = f"sf-eff-b1-{name}"
    request = {"type": "control_request", "request_id": request_id, "request": {"subtype": "get_settings"}}
    done = subprocess.run(command, input=json.dumps(request) + "\n", cwd=work, capture_output=True,
                          text=True, timeout=45)
    assert done.returncode == 0, done.stderr[-2000:]
    responses = [json.loads(line) for line in done.stdout.splitlines() if line.startswith("{")]
    matching = [row["response"] for row in responses if row.get("type") == "control_response"
                and row.get("response", {}).get("request_id") == request_id]
    assert len(matching) == 1, done.stdout[-4000:]
    assert matching[0]["subtype"] == "success", matching
    assert matching[0]["response"]["applied"]["effort"] == expected
