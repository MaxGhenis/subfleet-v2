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
import uuid
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
    A resume case is recorded without an effort (its builder took none), so a
    named effort's `--effort` goes where a fresh launch puts it: right after
    `--verbose`, before the permission flags.
    """
    if case["path"] == "resume":
        argv = list(PINNED[lsc.case_id({key: value for key, value in case.items() if key != "effort"})])
        if case["effort"]:
            after_verbose = argv.index("--verbose") + 1
            argv[after_verbose:after_verbose] = ["--effort", case["effort"]]
    else:
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
        # A job or resume merges lane settings before installing its publish
        # hook; turns merge their already assembled settings at the end.
        if case["path"] in ("job", "resume"):
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


RESUME_CASES = st.fixed_dictionaries({
    "path": st.just("resume"),
    "sandbox": st.sampled_from(lsc.SANDBOXES),
    "task": st.sampled_from(lsc.TASKS),
    "tmp": st.booleans(),
    "mcp": st.booleans(),
    "model": st.booleans(),
    "effort": st.sampled_from(EFFORTS),
})


def _resume_launch(scratch: Path, case: dict, effort: str | None):
    job = lsc._job(scratch, case)
    return lsc._adapter(scratch).resume_launch(
        job, "job-1/a2", scratch / "attempt", lsc._lane(), lsc._credentials(scratch, case["tmp"]),
        lsc.SESSION, Path(job.prompt_path), None,
        model_id=lsc.MODEL if case["model"] else None, effort=effort,
    )


@PROPERTIES
@example(case=dict(path="resume", sandbox="workspace-write", task="review", tmp=True, mcp=True,
                   model=True, effort="medium"))
@example(case=dict(path="resume", sandbox="read-only", task=None, tmp=False, mcp=False,
                   model=False, effort=None))
@given(case=RESUME_CASES)
def test_property_resume_effort_pins_settings_and_preserves_argv(case, tmp_path):
    """I1-I4 on the resume path (fix r1, finding 4): `resume_launch` pins a named
    effort as `build_launch` does, and a resume naming none keeps 676906f85's argv
    with only the watchdog restored. The daemon revives attempts through this path."""
    scratch = Path(tempfile.mkdtemp(dir=tmp_path))
    launch = _resume_launch(scratch, case, case["effort"])
    assert launch.env_add.get(EFFORT_ENV) is None  # the pin belongs only in settings
    _assert_effort_and_differential(case, lsc.normalize(launch.argv, scratch))


@pytest.mark.parametrize("task", lsc.TASKS)
@pytest.mark.parametrize("sandbox", lsc.SANDBOXES)
def test_resumed_attempt_pins_the_effort_its_fresh_launch_pins(sandbox, task, tmp_path):
    """Review r1's probe P2, both sandboxes: a fresh launch naming `medium` carries
    `--effort medium` and the `medium` pin, and so does the resume of that attempt,
    at the same place, with the same `--settings` payload byte for byte."""
    case = dict(path="resume", sandbox=sandbox, task=task, tmp=True, mcp=True, model=True, effort="medium")
    fresh_scratch, resume_scratch = (Path(tempfile.mkdtemp(dir=tmp_path)) for _ in range(2))
    job = lsc._job(fresh_scratch, case)
    fresh = lsc.normalize(lsc._adapter(fresh_scratch).build_launch(
        job, "job-1/a1", fresh_scratch / "attempt", lsc._lane(), lsc._credentials(fresh_scratch, True),
        lsc.MODEL, "medium", Path(job.prompt_path), None).argv, fresh_scratch)
    resumed = lsc.normalize(_resume_launch(resume_scratch, case, "medium").argv, resume_scratch)
    for argv in (fresh, resumed):
        index, payload = _one_settings(argv)
        assert payload["env"][EFFORT_ENV] == "medium"
        assert argv.count("--effort") == 1 and argv[argv.index("--effort") + 1] == "medium"
        assert argv[argv.index("--effort") - 1] == "--verbose"
    assert _one_settings(fresh)[1] == _one_settings(resumed)[1]
    assert fresh[fresh.index("--effort"):] == resumed[resumed.index("--effort"):]


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


def test_an_empty_effort_pins_nothing_on_any_path(tmp_path):
    """Review r1's mutant MH: an empty effort is no effort. `lane_settings` adds no
    variable for it, and a job, a resume and a turn naming `""` build the same argv
    as naming None. No input reaches this today (the policy refuses a blank effort and
    the conversation service replaces an empty one); this pins the rule anyway."""
    assert EFFORT_ENV not in lane_settings(effort="")["env"]
    assert lane_settings({"env": {"KEEP": "1"}, "x": [1]}, effort="") == lane_settings({"env": {"KEEP": "1"}, "x": [1]})
    assert lane_settings_args(effort="") == lane_settings_args()
    for path, extra in (("job", dict(isolated=False)), ("resume", dict(model=True))):
        for sandbox in lsc.SANDBOXES:
            case = dict(path=path, sandbox=sandbox, task="review", tmp=True, mcp=False, **extra)
            built = {}
            for effort in ("", None):
                scratch = Path(tempfile.mkdtemp(dir=tmp_path))
                if path == "job":
                    job = lsc._job(scratch, case)
                    launch = lsc._adapter(scratch).build_launch(
                        job, "job-1/a1", scratch / "attempt", lsc._lane(), lsc._credentials(scratch, True),
                        lsc.MODEL, effort, Path(job.prompt_path), None)
                else:
                    launch = _resume_launch(scratch, case, effort)
                built[effort] = lsc.normalize(launch.argv, scratch)
            assert built[""] == built[None], (path, sandbox)
            assert EFFORT_ENV not in _one_settings(built[""])[1]["env"]
    from subfleet.conversations import claude_turn
    from subfleet.conversations.turn import TurnSpec
    for permission in lsc.PERMISSIONS:
        turns = [claude_turn.argv(TurnSpec(provider="claude", message_id="m", text="t", model_id=lsc.MODEL,
                                           permission=permission, native_session_id=None,
                                           new_session_id=lsc.SESSION, effort=effort))
                 for effort in ("", None)]
        assert turns[0] == turns[1], permission


def test_v1_source_parity_runs_by_default_where_the_v1_runner_exists(tmp_path, monkeypatch):
    """Review r1 finding 1: with no `SUBFLEET_V1_RUNNER`, the v1 source-parity tests
    look for v1's runner in the user's own v1 tree and run whenever it is there, as
    at b51f08f96; the variable only overrides where to look. The user's home is a
    scratch stand-in here, so nothing reads or stats the real v1 tree."""
    import importlib.util
    import pwd
    from types import SimpleNamespace

    home = tmp_path / "home"
    runner = home / "chief-of-staff" / "subfleet" / "bin" / "subfleet-claude"
    real_getpwuid = pwd.getpwuid
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home))
                        if uid == os.getuid() else real_getpwuid(uid))

    def load(name: str):
        spec = importlib.util.spec_from_file_location(
            f"_v1_parity_{name}", REPO / "tests/unit/test_claude_v1_parity.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def skipped(module) -> bool:
        marks = module.pytestmark if isinstance(module.pytestmark, list) else [module.pytestmark]
        return any(mark.name == "skipif" and bool(mark.args[0]) for mark in marks)

    monkeypatch.delenv("SUBFLEET_V1_RUNNER", raising=False)
    absent = load("absent")
    assert absent.V1_RUNNER == runner and skipped(absent)          # no v1 tree, as in CI
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    present = load("present")
    assert present.V1_RUNNER == runner and not skipped(present)    # this Mac: they run
    monkeypatch.setenv("SUBFLEET_V1_RUNNER", "")
    assert not skipped(load("empty-override"))                     # empty means unset
    elsewhere = tmp_path / "elsewhere" / "subfleet-claude"
    monkeypatch.setenv("SUBFLEET_V1_RUNNER", str(elsewhere))
    missing = load("missing-override")
    assert missing.V1_RUNNER == elsewhere and skipped(missing)    # the override wins
    elsewhere.parent.mkdir()
    elsewhere.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    assert not skipped(load("override"))


REAL_CLAUDE = pytest.mark.skipif(os.environ.get("SUBFLEET_REAL_CLAUDE") != "1",
                                 reason="opt in with SUBFLEET_REAL_CLAUDE=1; no API request is sent")


def _real_claude_binary() -> str:
    binary = os.environ.get("SUBFLEET_CLAUDE_BIN") or shutil.which("claude")
    assert binary, "a real Claude CLI is required for the opted-in premise test"
    binary = str(Path(binary).absolute())
    assert Path(binary).resolve() != (REPO / "tests/bin/claude").resolve(), "select a real SUBFLEET_CLAUDE_BIN"
    return binary


def _real_applied(tmp_path: Path, name: str, arguments: list[str], user_effort: str | None,
                  child_effort: str | None = None) -> dict:
    """Run the real CLI with `arguments` in a fresh HOME/config/cwd, ask only
    `get_settings` (no message, no credential) and return its `applied` answer."""
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
    command += [_real_claude_binary(), *arguments]
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
    return matching[0]["response"]["applied"]


@REAL_CLAUDE
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
    arguments = ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
                 "--verbose", "--effort", "high"]
    if settings_effort is not None:
        arguments += ["--settings", json.dumps({"env": {EFFORT_ENV: settings_effort}}, separators=(",", ":"))]
    assert _real_applied(tmp_path, name, arguments, user_effort, child_effort)["effort"] == expected


@REAL_CLAUDE
@pytest.mark.parametrize("name,permission,pinned,user_effort,expected", [
    ("ultracode-bypass", "bypass", True, None, "xhigh"),
    ("ultracode-bypass-user-low", "bypass", True, "low", "xhigh"),
    ("ultracode-ask-user-low", "ask", True, "low", "xhigh"),
    # The control: the base's turn (no pin) lets the user's `low` win, ultracode
    # still reported on. It shows this probe can tell the pin from its absence.
    ("ultracode-base-user-low", "bypass", False, "low", "low"),
])
def test_real_claude_ultracode_turn_keeps_ultracode_with_the_effort_pin(
        name, permission, pinned, user_effort, expected, tmp_path):
    """C-26.8, review r1 finding 3: a writable ultracode turn's own argv, as
    `claude_turn.argv` builds it on an Opus model (`{"ultracode":true,"env":{...,
    "CLAUDE_CODE_EFFORT_LEVEL":"xhigh"}}` beside `--effort xhigh`), still gets
    `applied.ultracode: true` from `get_settings`, at xhigh, whatever the user's
    settings say. SUBFLEET_REAL_CLAUDE_OPUS names another Opus model id."""
    from subfleet.conversations import claude_turn
    from subfleet.conversations.turn import TurnSpec
    model = os.environ.get("SUBFLEET_REAL_CLAUDE_OPUS") or lsc.MODEL
    argv = claude_turn.argv(TurnSpec(provider="claude", message_id="m", text="t", model_id=model,
                                     permission=permission, native_session_id=None,
                                     new_session_id=str(uuid.uuid4()), effort="ultracode"))
    index, payload = _one_settings(argv)
    assert payload["ultracode"] is True and payload["env"][EFFORT_ENV] == "xhigh"
    assert argv[argv.index("--effort") + 1] == "xhigh"
    if not pinned:
        del payload["env"][EFFORT_ENV]
        argv = argv[:index + 1] + [json.dumps(payload, separators=(",", ":"))] + argv[index + 2:]
    applied = _real_applied(tmp_path, name, argv[1:], user_effort)
    assert applied["ultracode"] is True, applied
    assert applied["effort"] == expected, applied
    assert applied["model"] == model, applied
