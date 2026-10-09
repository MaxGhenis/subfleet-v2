"""Exercise CI policy against actual pytest/JUnit reports from tiny projects."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import ci_retry_known_flakes as retry_tool

pytest_plugins = ["pytester"]
SCRIPT = Path(__file__).resolve().parents[2] / "tools/ci_retry_known_flakes.py"
WARNING = "::warning title=Known flake passed on retry::"
RECORDER = """
import json
from pathlib import Path

def pytest_sessionstart(session):
    path = Path('attempt')
    path.write_text(str(int(path.read_text()) + 1 if path.exists() else 1))

def pytest_collection_finish(session):
    with Path('runs.jsonl').open('a') as stream:
        stream.write(json.dumps([item.nodeid for item in session.items]) + '\\n')
"""
PREAMBLE = """
import pytest
from pathlib import Path
def first():
    return Path('attempt').read_text() == '1'
"""


@pytest.fixture
def project(tmp_path, pytester, monkeypatch, capsys):
    def invoke(source, allowed=(), *, conftest="", config="", files=None, cli=False):
        (tmp_path / "pytest.ini").write_text("[pytest]\n" + config, encoding="utf-8")
        (tmp_path / "conftest.py").write_text(
            textwrap.dedent(RECORDER) + textwrap.dedent(conftest), encoding="utf-8")
        (tmp_path / "test_sample.py").write_text(
            textwrap.dedent(PREAMBLE) + textwrap.dedent(source), encoding="utf-8")
        for name, content in (files or {}).items():
            (tmp_path / name).write_text(textwrap.dedent(content), encoding="utf-8")
        allowlist = tmp_path / "allowlist.txt"
        allowlist.write_text("".join(f"{node}  # D-TEST: Generated flake.\n" for node in allowed))
        argv = [str(SCRIPT), "--allowlist", str(allowlist),
                "--reports-dir", str(tmp_path / "reports")]
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "SUBFLEET_LIVE": "0",
               "PYTEST_ADDOPTS": "--tb=line", "PY_COLORS": "0"}
        if cli:
            result = subprocess.run([sys.executable, *argv], cwd=tmp_path,
                                    env={**os.environ, **env}, capture_output=True,
                                    text=True, timeout=240)
        else:
            # Real pytest runs and real JUnit, with pytester's module isolation.
            # Reuse the interpreter so this policy matrix stays fast under load.
            commands = []
            def run_inline(command):
                assert command[:3] == [sys.executable, "-m", "pytest"]
                commands.append(command)
                return SimpleNamespace(returncode=pytester.runpytest_inprocess(*command[3:]).ret)

            with monkeypatch.context() as patch:
                patch.chdir(tmp_path)
                for name, value in env.items():
                    patch.setenv(name, value)
                patch.setattr(sys, "argv", argv)
                patch.setattr(retry_tool.subprocess, "run", run_inline)
                capsys.readouterr()
                code = retry_tool.main()
                output = capsys.readouterr()
                result = SimpleNamespace(returncode=code, stdout=output.out, stderr=output.err)
            assert len(commands) <= 2
            for retry in commands[1:]:
                assert 1 <= len(retry[8:]) <= 5
                assert set(retry[8:]) <= set(allowed)
        runs_file = tmp_path / "runs.jsonl"
        runs = [json.loads(line) for line in runs_file.read_text().splitlines()] if runs_file.exists() else []
        # Invariant: every invocation after the first selects only <=5 eligible IDs.
        assert len(runs) <= 2, result.stdout + result.stderr
        for retry in runs[1:]:
            assert len(retry) <= 5
            assert set(retry) <= set(allowed)
        return result, runs
    return invoke


def test_all_pass_runs_once(project):
    result, runs = project("def test_ok(): pass")
    assert result.returncode == 0, result.stdout + result.stderr
    assert runs == [["test_sample.py::test_ok"]]
    assert WARNING not in result.stdout


def test_cli_runs_the_real_subprocesses(project):
    node = "test_sample.py::test_flake"
    result, runs = project("def test_flake(): assert not first()", [node], cli=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert runs == [[node], [node]]
    assert f"{WARNING}{node} (D-TEST)" in result.stdout


@pytest.mark.parametrize("count", range(1, 6))
def test_only_allowlisted_failures_retry_and_emit_each_defect(project, count):
    nodes = [f"test_sample.py::test_flake_{index}" for index in range(count)]
    source = "def test_stable(): pass\n" + "\n".join(
        f"def test_flake_{index}(): assert not first()" for index in range(count))
    result, runs = project(source, nodes)
    assert result.returncode == 0, result.stdout + result.stderr
    assert runs == [["test_sample.py::test_stable", *nodes], nodes]
    assert result.stdout.count(WARNING) == count
    for node in nodes:
        assert f"{WARNING}{node} (D-TEST)" in result.stdout
    assert f"{count} known flake(s) passed on retry" in result.stdout


def test_non_allowlisted_order_dependent_failure_always_fails(project):
    result, runs = project("""
        state = []
        def test_contaminate(): state.append('contamination')
        def test_regression(): assert not state
    """)
    assert result.returncode == 1
    assert len(runs) == 1
    assert "Failure outside the known-flake allowlist" in result.stdout


def test_one_unknown_failure_blocks_even_an_allowlisted_flake(project):
    result, runs = project("""
        def test_flake(): assert not first()
        def test_unknown(): assert not first()
    """, ["test_sample.py::test_flake"])
    assert result.returncode == 1
    assert len(runs) == 1
    assert WARNING not in result.stdout


@pytest.mark.parametrize("outcome", ["failure", "skip", "xfail", "setup-error", "teardown-error", "teardown-skip"])
def test_retry_must_pass_every_phase(project, outcome):
    action = {
        "failure": "assert False",
        "skip": "pytest.skip('unavailable')",
        "xfail": "pytest.xfail('unfixed')",
        "setup-error": "pass",
        "teardown-error": "pass",
        "teardown-skip": "pass",
    }[outcome]
    fixture = """
        @pytest.fixture
        def phase():
            if not first() and OUTCOME == 'setup-error':
                raise RuntimeError('setup failure')
            yield
            if not first() and OUTCOME == 'teardown-error':
                raise RuntimeError('teardown failure')
            if not first() and OUTCOME == 'teardown-skip':
                pytest.skip('teardown unavailable')
    """.replace("OUTCOME", repr(outcome))
    source = textwrap.dedent(fixture) + "\ndef test_flake(phase):\n    assert not first()\n    " + action
    result, runs = project(source, ["test_sample.py::test_flake"])
    assert result.returncode == 1, result.stdout + result.stderr
    assert len(runs) == 2
    assert WARNING not in result.stdout


@pytest.mark.parametrize("phase", ["setup", "teardown"])
def test_first_pass_fixture_errors_are_retry_candidates(project, phase):
    result, runs = project(f"""
        @pytest.fixture
        def flaky_fixture():
            if '{phase}' == 'setup': assert not first()
            yield
            if '{phase}' == 'teardown': assert not first()
        def test_flake(flaky_fixture): pass
    """, ["test_sample.py::test_flake"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(runs) == 2
    assert WARNING in result.stdout


def test_more_than_five_failures_never_retry(project):
    nodes = [f"test_sample.py::test_flake_{index}" for index in range(6)]
    source = "\n".join(f"def test_flake_{index}(): assert not first()" for index in range(6))
    result, runs = project(source, nodes)
    assert result.returncode == 1
    assert len(runs) == 1
    assert "6 failed tests" in result.stdout


def test_collection_error_never_retries(project):
    result, runs = project("def test_flake(): assert not first()", ["test_sample.py::test_flake"],
                           files={"test_broken.py": "raise RuntimeError('collection error')"})
    assert result.returncode == 2
    assert len(runs) == 1
    assert WARNING not in result.stdout


def test_collection_error_on_retry_fails(project):
    result, runs = project("""
        if not first(): raise RuntimeError('retry collection error')
        def test_flake(): assert not first()
    """, ["test_sample.py::test_flake"])
    # Pytest reports a collection error for an explicitly selected node as 4
    # ("found no collectors"); retain that nonzero status rather than retrying.
    assert result.returncode == 4
    assert WARNING not in result.stdout


@pytest.mark.parametrize("case, exit_code", [("internal", 3), ("usage", 4), ("empty", 5)])
def test_actual_pytest_errors_are_preserved(project, case, exit_code):
    result, runs = project(
        "def helper(): pass" if case == "empty" else "def test_ok(): pass",
        config="addopts = --nonexistent-ci-option\n" if case == "usage" else "",
        conftest="""
            def pytest_collection_modifyitems(items):
                raise RuntimeError('internal error')
        """ if case == "internal" else "",
    )
    assert result.returncode == exit_code
    assert len(runs) <= 1
    assert WARNING not in result.stdout


@pytest.mark.parametrize("exit_code", [2, 3, 4, 5])
@pytest.mark.parametrize("on_retry", [False, True])
def test_exit_codes_two_to_five_are_preserved(project, exit_code, on_retry):
    result, runs = project("def test_flake(): assert not first()", ["test_sample.py::test_flake"],
                           conftest=f"""
        def pytest_sessionfinish(session, exitstatus):
            if Path('attempt').read_text() == '{2 if on_retry else 1}':
                session.exitstatus = {exit_code}
    """)
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert len(runs) == (2 if on_retry else 1)
    assert WARNING not in result.stdout


@pytest.mark.parametrize("allow_persistent", [False, True])
def test_cache_omission_cannot_hide_failure(project, tmp_path, allow_persistent):
    nodes = ["test_sample.py::test_flake"]
    if allow_persistent:
        nodes.append("test_sample.py::test_persistent")
    result, runs = project("""
        @pytest.fixture
        def skipping_teardown():
            yield
            pytest.skip('inspection unavailable')
        def test_persistent(skipping_teardown): assert False
        def test_flake(): assert not first()
    """, nodes)
    cache = json.loads((tmp_path / ".pytest_cache/v/cache/lastfailed").read_text())
    assert "test_sample.py::test_persistent" not in cache  # Reproduce the reviewer's omission.
    first = ET.parse(tmp_path / "reports/first.xml")
    assert sum(case.find("failure") is not None for case in first.iter("testcase")) == 2
    assert result.returncode == 1
    assert len(runs) == (2 if allow_persistent else 1)
    assert WARNING not in result.stdout


def test_call_and_teardown_failures_count_as_one_node(project):
    result, runs = project("""
        @pytest.fixture
        def phase():
            yield
            assert not first()
        def test_flake(phase): assert not first()
    """, ["test_sample.py::test_flake"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert runs[1] == ["test_sample.py::test_flake"]
    assert result.stdout.count(WARNING) == 1


def test_class_and_parameter_node_ids_are_retried_exactly(project):
    node = "test_sample.py::TestGroup::test_flake[a.b::c/&<%]"
    result, runs = project("""
        class TestGroup:
            @pytest.mark.parametrize('value', [1], ids=['a.b::c/&<%'])
            def test_flake(self, value): assert not first()
    """, [node], config="junit_family = xunit2\njunit_logging = all\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert runs == [[node], [node]]
    assert f"{WARNING}{node.replace('%', '%25')} (D-TEST)" in result.stdout


def test_absent_retry_node_fails_even_when_pytest_exits_zero(project):
    nodes = ["test_sample.py::test_flake", "test_sample.py::test_other"]
    result, runs = project("""
        def test_flake(): assert not first()
        def test_other(): assert not first()
    """, nodes, conftest="""
        def pytest_collection_modifyitems(items):
            if Path('attempt').read_text() == '2':
                items[:] = items[:1]
    """)
    assert result.returncode == 1
    assert len(runs) == 2
    assert "Every requested retry must be present and passed" in result.stdout
    assert WARNING not in result.stdout


@pytest.mark.parametrize("report", [None, "<broken", "<testsuites />"])
@pytest.mark.parametrize("on_retry", [False, True])
def test_unverifiable_junit_fails_closed(project, report, on_retry):
    # Replace/remove a real pytest report after its session has written it.
    result, runs = project("def test_flake(): assert not first()", ["test_sample.py::test_flake"],
                           conftest=f"""
        def pytest_unconfigure(config):
            if Path('attempt').read_text() == '{2 if on_retry else 1}':
                path = Path(config.option.xmlpath)
                if {report is None!r}:
                    path.unlink()
                else:
                    path.write_text({report!r})
    """)
    assert result.returncode == 1
    assert len(runs) == (2 if on_retry else 1)
    assert WARNING not in result.stdout
