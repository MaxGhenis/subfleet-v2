"""D-WATCHDOG1 (d1031; C-12.4, C-26.11, C-4.5): every Claude launch runs with Claude
Code's retry watchdog off.

Under `CLAUDE_CODE_RETRY_WATCHDOG=1`, 429 and 529 responses retry without end, so a
lane that reaches its plan's ceiling waits out the window instead of exiting, and
C-4.5's `limited` failover cannot move the job on before the wall. A lane has no home
of its own, so the user's `settings.json` `env` reaches every launch that loads user
settings. Each launch therefore sets the variable to `0` in the settings it gives on
the command line.

Over every case `tests/lane_settings_cases.py` builds, each through its real builder:

* I1: the argv has exactly one `--settings`, whose settings carry
  `env.CLAUDE_CODE_RETRY_WATCHDOG == "0"`.
* I2: every other key those settings carried before is unchanged: `disableAllHooks`,
  `fastMode`, `ultracode` and the publish hook.
* I3: a user `settings.json` whose `env` sets the variable to "1" changes no argv, and
  no builder opens it.
* I4: the read-only flags (`--setting-sources ""`, `--safe-mode`, the tool list) are
  unchanged, and a read-only launch's settings name only `env`, and a fast turn's
  `fastMode`, which it had before.

The differential checks the whole argv: the new one with the override taken out is
the one 676906f85 built (`tests/fixtures/lane_settings/argv-676906f85.json`).
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet.adapters.claude import ClaudeAdapter
from subfleet.contracts import Sandbox
from tests import lane_settings_cases as lsc
from tests.conftest import FAKE_CLAUDE

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "subfleet"
RECORDING = REPO / "tests" / "fixtures" / "lane_settings" / "argv-676906f85.json"
BASE_COMMIT = "676906f854f5"
RECORDED = json.loads(RECORDING.read_text(encoding="utf-8"))["cases"]
CASES = lsc.cases()
READ_ONLY_CASES = [case for case in CASES if lsc.read_only(case)]
WATCHDOG, OFF = lsc.WATCHDOG, lsc.OFF
USER_ON = {"env": {WATCHDOG: "1"}}


def _seed(directory: Path, settings: dict | None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if settings is not None:
        (directory / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return directory


@pytest.fixture
def user_on(tmp_path, monkeypatch):
    """A user whose `~/.claude/settings.json` turns the watchdog on, as d1031's trip did."""
    home = tmp_path / "user-home"
    _seed(home / ".claude", USER_ON)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv(WATCHDOG, raising=False)
    return home


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> dict[str, list[str]]:
    """Every case's argv, each built once through its builder under a user whose
    `~/.claude/settings.json` turns the watchdog on; the tests below read them."""
    root = tmp_path_factory.mktemp("lane-settings")
    with pytest.MonkeyPatch.context() as patch:
        home = root / "user-home"
        _seed(home / ".claude", USER_ON)
        patch.setenv("HOME", str(home))
        patch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        patch.delenv(WATCHDOG, raising=False)
        return {lsc.case_id(case): lsc.build(case, root / f"case-{number:03d}")
                for number, case in enumerate(CASES)}


def _settings(argv) -> dict:
    payloads = lsc.settings_payloads(argv)
    assert len(payloads) == 1, f"{len(payloads)} --settings in {argv}"
    return json.loads(payloads[0])


# --- I1, I2 and the differential, on every case --------------------------------


def test_the_grid_is_the_recording():
    """The recording covers exactly the cases this module builds."""
    assert sorted(map(lsc.case_id, CASES)) == sorted(RECORDED)
    assert {case["path"] for case in CASES} == set(lsc.PATHS)


@pytest.mark.parametrize("case", CASES, ids=lsc.case_id)
def test_i1_every_launch_gives_one_settings_with_the_watchdog_off(case, built):
    """I1, C-12.4, C-26.11: one `--settings`, `env.CLAUDE_CODE_RETRY_WATCHDOG == "0"`."""
    settings = _settings(built[lsc.case_id(case)])
    assert settings["env"][WATCHDOG] == OFF


@pytest.mark.parametrize("case", CASES, ids=lsc.case_id)
def test_i2_every_other_setting_is_what_the_launch_had_before(case, built):
    """I2: the override adds `env` and nothing else; `disableAllHooks`, `fastMode`,
    `ultracode` and the publish hook are where and what they were, and a writable
    review still passes `--setting-sources user` just before its one `--settings`."""
    argv = built[lsc.case_id(case)]
    settings = _settings(argv)
    assert settings.pop("env") == {WATCHDOG: OFF}
    hooks = settings.pop("hooks", None)
    assert (hooks is not None) is lsc.publish_hook(case)
    if hooks is not None:
        assert hooks == {"PreToolUse": [{"hooks": [{
            "type": "command", "command": f"{lsc.PLACEHOLDER}/attempt/publish-bin/review-publish-hook",
            "timeout": 30}]}]}
        at = argv.index("--settings")
        assert argv[at - 2:at] == ["--setting-sources", "user"] and argv.count("--setting-sources") == 1
    assert settings == lsc.expected_settings(case)


@pytest.mark.parametrize("case", CASES, ids=lsc.case_id)
def test_differential_the_override_is_the_only_change_from_676906f85(case, built):
    """The new argv, the override taken back out of its one `--settings`, is the argv
    676906f85's builders made for the same inputs, flag for flag."""
    argv = built[lsc.case_id(case)]
    assert lsc.without_override(argv) == RECORDED[lsc.case_id(case)]


# --- I3 -------------------------------------------------------------------------


def test_i3_the_users_settings_file_changes_no_argv_and_is_never_opened(tmp_path, monkeypatch):
    """I3: every case built under a user whose settings turn the watchdog on, and again
    under one with no settings at all, gives the same argv; no builder opens a
    `settings.json` or `settings.local.json` anywhere (an audit hook watches)."""
    opened: list[str] = []
    watching = [False]

    def audit(event, args):
        if watching[0] and event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            name = os.fsdecode(args[0])
            if name.endswith(("settings.json", "settings.local.json")):
                opened.append(name)
    sys.addaudithook(audit)
    built = {}
    for label, settings in (("on", USER_ON), ("none", None)):
        home = tmp_path / f"home-{label}"
        _seed(home / ".claude", settings)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        watching[0] = True
        try:
            built[label] = {lsc.case_id(case): lsc.build(case, tmp_path / label / f"case-{number:03d}")
                            for number, case in enumerate(CASES)}
        finally:
            watching[0] = False
    assert opened == []
    assert built["on"] == built["none"]
    assert all(_settings(argv)["env"] == {WATCHDOG: OFF} for argv in built["on"].values())


# --- I4 -------------------------------------------------------------------------


@pytest.mark.parametrize("case", READ_ONLY_CASES, ids=lsc.case_id)
def test_i4_read_only_flags_are_unchanged_and_the_settings_reopen_nothing(case, built):
    """I4, C-12.4, C-23.2: a read-only launch keeps the adapter's read-only set, flag for
    flag (`--setting-sources ""` once, `--safe-mode`, the tool list), and its one
    `--settings` names only `env` (and a fast turn's `fastMode`, which it carried
    before), so it brings back no hook, permission, MCP server or settings source."""
    argv = built[lsc.case_id(case)]
    start, end = argv.index("--permission-mode"), argv.index("--disable-slash-commands") + 1
    isolated = case["path"] == "gate" or bool(case.get("isolated"))
    flags = list(ClaudeAdapter.permission_args(Sandbox.READ_ONLY, isolated=isolated))
    assert argv[start:end] == flags
    assert argv.count("--setting-sources") == 1 and argv[argv.index("--setting-sources") + 1] == ""
    assert "--safe-mode" in argv and "--dangerously-skip-permissions" not in argv
    settings = _settings(argv)
    allowed = {"env", "fastMode"} if case["path"] == "turn" and case["fast"] else {"env"}
    assert set(settings) == allowed and settings["env"] == {WATCHDOG: OFF}


# --- the properties ---------------------------------------------------------------


EFFORTS_ALL = (None, "low", "medium", "high", "xhigh", "max")


def _case(path, **dimensions):
    return st.fixed_dictionaries({"path": st.just(path), **{k: st.sampled_from(v) for k, v in dimensions.items()}})


INPUTS = st.one_of(
    _case("job", sandbox=lsc.SANDBOXES, task=lsc.TASKS, isolated=(False, True), effort=EFFORTS_ALL,
          tmp=(False, True), mcp=(False, True)).filter(lambda c: not c["isolated"] or c["sandbox"] == "read-only"),
    _case("resume", sandbox=lsc.SANDBOXES, task=lsc.TASKS, tmp=(False, True), mcp=(False, True),
          model=(False, True)),
    _case("probe", sandbox=lsc.SANDBOXES, task=("build", "review"), effort=EFFORTS_ALL),
    _case("timer", purpose=("keepalive", "heal"), effort=EFFORTS_ALL),
    _case("gate", gate=("plan", "pr"), effort=EFFORTS_ALL),
    _case("turn", permission=lsc.PERMISSIONS, fast=(False, True), effort=(*EFFORTS_ALL, "ultracode"),
          resume=(False, True), tmp=(False, True)),
    _case("enroll"), _case("probe-turn", model=(lsc.HAIKU, lsc.MODEL, "claude-sonnet-5")),
    _case("probe-outcome"), _case("heal"),
)


@settings(max_examples=200, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(case=INPUTS)
def test_property_i1_i2_i4_over_the_builders_inputs(case, tmp_path, user_on):
    """I1, I2 and I4 for inputs drawn over every builder's dimensions, efforts beyond
    the recorded grid included: one `--settings` with the watchdog off, every other
    key as `expected_settings` states it, and the read-only set intact."""
    argv = lsc.build(case, Path(tempfile.mkdtemp(dir=tmp_path)))
    settings = _settings(argv)
    assert settings.pop("env") == {WATCHDOG: OFF}                          # I1
    assert ("hooks" in settings) is lsc.publish_hook(case)                 # I2
    settings.pop("hooks", None)
    assert settings == lsc.expected_settings(case)
    if lsc.read_only(case):                                                # I4
        assert argv[argv.index("--setting-sources") + 1] == "" and argv.count("--setting-sources") == 1
        assert "--safe-mode" in argv
        assert argv[argv.index("--tools") + 1] == argv[argv.index("--allowedTools") + 1]
    if lsc.case_id(case) in RECORDED:                                      # the differential, where recorded
        assert lsc.without_override(argv) == RECORDED[lsc.case_id(case)]


JSON_SCALARS = st.one_of(st.none(), st.booleans(), st.integers(-5, 5), st.text(max_size=6))
JSON_VALUES = st.recursive(JSON_SCALARS, lambda inner: st.one_of(
    st.lists(inner, max_size=3), st.dictionaries(st.text(max_size=6), inner, max_size=3)), max_leaves=8)


@settings(max_examples=300, deadline=None, derandomize=True, database=None)
@given(extra=st.dictionaries(st.text(min_size=1, max_size=12).filter(lambda k: k != "env"), JSON_VALUES,
                             max_size=6),
       env=st.one_of(st.none(), st.dictionaries(st.text(min_size=1, max_size=12), st.text(max_size=8),
                                                max_size=4)),
       user_value=st.sampled_from([None, "1", "0", "true", ""]))
def test_property_the_merge_keeps_every_other_key_and_turns_the_watchdog_off(extra, env, user_value):
    """I1/I2 at the merge itself: whatever settings a launch already passes, and
    whatever its `env` says of the variable, the merged settings carry every key and
    every other variable unchanged, the variable `"0"`, and the input is not mutated."""
    from subfleet.adapters.claude import LANE_SETTINGS_ENV, lane_settings
    given_settings = dict(extra)
    if env is not None:
        given_settings["env"] = {**env, **({WATCHDOG: user_value} if user_value is not None else {})}
    before = json.dumps(given_settings, sort_keys=True)
    merged = lane_settings(given_settings)
    assert json.dumps(given_settings, sort_keys=True) == before
    assert dict(LANE_SETTINGS_ENV) == {WATCHDOG: OFF}
    assert merged["env"][WATCHDOG] == OFF
    assert {k: v for k, v in merged.items() if k != "env"} == extra
    assert {k: v for k, v in merged["env"].items() if k != WATCHDOG} == {
        k: v for k, v in (given_settings.get("env") or {}).items() if k != WATCHDOG}
    assert json.loads(json.dumps(merged)) == merged                       # it is a settings payload


# --- the recording, against the commit itself -------------------------------------


def test_the_recording_is_what_676906f85_builds(tmp_path):
    """The differential's oracle is 676906f85's own builders: its `subfleet` package,
    taken from git and run through the same cases in a separate interpreter, builds
    exactly the recorded argv. Skipped where the clone lacks the commit."""
    found = subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"{BASE_COMMIT}^{{commit}}"],
                           capture_output=True)
    if found.returncode != 0:
        pytest.skip(f"{BASE_COMMIT} is not in this clone")
    archive = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", BASE_COMMIT, "subfleet"],
                             capture_output=True, check=True).stdout
    old = tmp_path / "old"
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(old, filter="data")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(_seed(tmp_path / "home" / ".claude", USER_ON).parent),
           "TMPDIR": str(tmp_path)}
    done = subprocess.run([sys.executable, "-I", "-B", str(Path(lsc.__file__)), str(old), str(tmp_path / "cases")],
                          capture_output=True, text=True, env=env, timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout)["cases"] == RECORDED


# --- the session's view, through the fake provider ---------------------------------


def _one_of_each() -> list[dict]:
    """A case per distinct launch shape: path, read-only or not, publish hook or not,
    and each turn permission with and without Fast."""
    seen, out = set(), []
    for case in CASES:
        key = (case["path"], lsc.read_only(case), lsc.publish_hook(case), case.get("permission"), case.get("fast"))
        if key not in seen:
            seen.add(key)
            out.append(case)
    return out


@pytest.mark.parametrize("where", ["home", "config-dir"])
@pytest.mark.parametrize("case", _one_of_each(), ids=lsc.case_id)
def test_the_fake_session_sees_the_watchdog_off_when_the_users_file_turns_it_on(case, where, built, tmp_path):
    """The tree's fake `claude`, run with the argv each builder made, reads the user's
    file (`~/.claude/settings.json`, or a lane home's under `CLAUDE_CONFIG_DIR`) that
    says "1" and the launch's command-line settings, and reports "0": the value the
    session applies, under the layering `tests/bin/claude`'s `settings_env` assumes
    (command-line settings over the files). Whether real Claude Code layers them so
    is the 2026-10-22 live probe's evidence, not this test's."""
    argv = built[lsc.case_id(case)]
    report = tmp_path / "report.json"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "user-home"),
           "SUBFLEET_FAKE_SETTINGS_REPORT": str(report), "SUBFLEET_FAKE_SETTINGS_NAMES": WATCHDOG}
    if where == "home":
        _seed(tmp_path / "user-home" / ".claude", USER_ON)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(_seed(tmp_path / "lane-home", USER_ON))
    work = tmp_path / "cwd"
    work.mkdir()
    done = subprocess.run([sys.executable, "-I", "-S", str(FAKE_CLAUDE), *argv[1:]], cwd=work, env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    seen = json.loads(report.read_text(encoding="utf-8"))
    assert seen["settings_flags"] == 1
    assert seen["seen"][WATCHDOG] == OFF, seen


# --- no `claude -p` builder outside the cases --------------------------------------


#: Every function in `subfleet/` whose code spells a `claude -p` command line, and the
#: case paths that build through it. A new site fails here until it is listed, which
#: means until a case covers it.
BUILDERS = {
    ("adapters/claude.py", "_turn_argv"): {"enroll", "probe-turn", "probe-outcome"},
    ("adapters/claude.py", "build_launch"): {"job", "probe", "timer", "gate"},
    ("adapters/claude.py", "resume_launch"): {"resume"},
    ("adapters/claude.py", "reconstruct_v1_argv"): set(),          # the v1 parity reference, not a launch
    ("conversations/claude_turn.py", "argv"): {"turn"},
    ("claude_cards.py", "heal_turn"): {"heal"},
}


def _claude_p_sites() -> set[tuple[str, str]]:
    sites = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) >= 2:
                    first, second = node.elts[0], node.elts[1]
                    name = (first.attr if isinstance(first, ast.Attribute)
                            else first.id if isinstance(first, ast.Name) else "")
                    if "claude_bin" in name.lower() and isinstance(second, ast.Constant) and second.value == "-p":
                        sites.add((path.relative_to(PACKAGE).as_posix(), function.name))
    return sites


def test_every_claude_p_builder_in_the_package_is_a_case_path():
    """Decision 1's "anything else that runs `claude -p`": the package's `claude -p`
    command lines are exactly `BUILDERS`, and every launch path covers one."""
    assert _claude_p_sites() == set(BUILDERS)
    assert set().union(*BUILDERS.values()) == set(lsc.PATHS)
