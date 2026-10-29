"""Independent #278 r1 checks; all providers are recording seams or tree fakes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from subfleet.provider_env import ProviderEnvironment
from tests import lane_settings_cases as lsc
from tests.unit import test_lane_settings as subject


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            'def new_turn(claude_bin):\n    return [claude_bin, "-p", "hello"]\n',
            id="literal-control",
        ),
        pytest.param(
            'def new_turn(claude_bin):\n    argv = [claude_bin]\n'
            '    argv += ["-p", "hello"]\n    return argv\n',
            id="assembled-in-pieces",
            marks=pytest.mark.xfail(strict=True, reason="r1: coverage guard misses split argv"),
        ),
        pytest.param(
            'import shlex\ndef new_turn(claude_bin):\n'
            '    return shlex.split(shlex.quote(claude_bin) + " -p hello")\n',
            id="shlex",
            marks=pytest.mark.xfail(strict=True, reason="r1: coverage guard misses shlex"),
        ),
        pytest.param(
            'def helper(executable):\n    return [executable, "-p", "hello"]\n'
            'def new_turn(claude_bin):\n    return helper(claude_bin)\n',
            id="helper-in-another-module",
            marks=pytest.mark.xfail(strict=True, reason="r1: guard requires a claude_bin name"),
        ),
    ],
)
def test_coverage_guard_rejects_an_uncovered_turn(source, tmp_path, monkeypatch):
    """Execute the new builder without spawning, then run the existing guard.

    Keep the six known sites in a scratch package and add a seventh. The three
    expected failures document false negatives, rather than claiming coverage.
    """
    package = tmp_path / "subfleet"
    for relative, _ in subject.BUILDERS:
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((subject.PACKAGE / relative).read_text(encoding="utf-8"), encoding="utf-8")
    (package / "new_turn.py").write_text(source, encoding="utf-8")
    namespace = {}
    exec(compile(source, "new_turn.py", "exec"), namespace)
    assert namespace["new_turn"]("claude") == ["claude", "-p", "hello"]
    monkeypatch.setattr(subject, "PACKAGE", package)
    with pytest.raises(AssertionError):
        subject.test_every_claude_p_builder_in_the_package_is_a_case_path()


def test_lane_home_settings_are_never_opened_and_change_no_builder_argv(tmp_path, monkeypatch):
    """Extend I3 to a seeded CLAUDE_CONFIG_DIR and the enrol/heal home shapes."""
    baseline = {}
    opened = []
    watching = False

    def audit(event, args):
        if watching and event == "open" and args and isinstance(args[0], (str, bytes, Path)):
            name = str(args[0])
            if name.endswith(("settings.json", "settings.local.json")):
                opened.append(name)

    sys.addaudithook(audit)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv(lsc.WATCHDOG, raising=False)
    for index, case in enumerate(lsc.cases()):
        baseline[lsc.case_id(case)] = lsc.build(case, tmp_path / "baseline" / str(index))
    config = tmp_path / "lane-home"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps(subject.USER_ON), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    for index, case in enumerate(lsc.cases()):
        work = tmp_path / "seeded" / str(index)
        for relative in ("lane-home", "login"):
            folder = work / relative
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "settings.json").write_text(json.dumps(subject.USER_ON), encoding="utf-8")
        watching = True
        try:
            argv = lsc.build(case, work)
        finally:
            watching = False
        assert argv == baseline[lsc.case_id(case)]
        assert subject._settings(argv)["env"][lsc.WATCHDOG] == "0"
    assert opened == []


@pytest.mark.parametrize("sandbox", lsc.SANDBOXES)
def test_explicit_env_pass_reaches_the_process_alongside_the_settings_pin(sandbox, tmp_path):
    """No claim about real Claude's ordering of process env and settings env."""
    parent = {lsc.WATCHDOG: "1"}
    assert lsc.WATCHDOG not in ProviderEnvironment({}).build("claude", environ=parent)
    environment = ProviderEnvironment({"env": {"pass": [lsc.WATCHDOG]}})
    credentials = environment.additions("claude", {"CLAUDE_CODE_OAUTH_TOKEN": lsc.TOKEN}, environ=parent)
    case = dict(path="job", sandbox=sandbox, task="build", isolated=False, tmp=False, mcp=False, effort=None)
    job = lsc._job(tmp_path, case)
    launch = lsc._adapter(tmp_path).build_launch(
        job, "job/a1", tmp_path / "attempt", lsc._lane(), credentials, lsc.MODEL, None,
        Path(job.prompt_path), None,
    )
    assert environment.assemble(launch.env_add, launch.env_remove, environ=parent, provider="claude")[lsc.WATCHDOG] == "1"
    assert subject._settings(launch.argv)["env"][lsc.WATCHDOG] == "0"
