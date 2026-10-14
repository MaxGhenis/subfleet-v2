"""Mutation kills require a named pytest regression; runner failures stop the tool."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "capacity_steer_mutations.py"
spec = importlib.util.spec_from_file_location("capacity_steer_mutations", SCRIPT)
mutations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mutations)


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for path in (mutations.DAEMON, mutations.SCHEDULER, mutations.CODEX):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original rule\n")
    monkeypatch.setattr(mutations, "MUTATIONS", {
        "one regression": ([(mutations.SCHEDULER, "original rule", "mutated rule")],
                           (mutations.ROUTE,)),
    })
    return tmp_path


def test_a_broken_lock_path_exits_nonzero_without_reporting_kills(tmp_path):
    for path in (mutations.DAEMON, mutations.SCHEDULER, mutations.CODEX):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, target)
    originals = {path: (tmp_path / path).read_bytes()
                 for path in (mutations.DAEMON, mutations.SCHEDULER, mutations.CODEX)}
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path,
                            env={**os.environ, "SUBFLEET_SUITE_LOCK": str(tmp_path / "missing" / "lock")},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "killed" not in result.stdout
    assert "infrastructure" in result.stderr.lower()
    assert {path: (tmp_path / path).read_bytes() for path in originals} == originals


@pytest.mark.parametrize("returncode,stdout,stderr", [
    (73, "", "lockf: cannot open lock"),
    (2, "ERROR collecting tests/unit/test_capacity_steer_route.py", ""),
    (1, "no tests ran", ""),
    (-9, "", "terminated"),
    (1, "FAILED tests/unrelated.py::test_other - AssertionError", ""),
])
def test_runner_failures_are_not_kills(runner, monkeypatch, capsys, returncode, stdout, stderr):
    monkeypatch.setattr(mutations, "pytest", lambda _: subprocess.CompletedProcess([], returncode, stdout, stderr))
    assert mutations.main() != 0
    captured = capsys.readouterr()
    assert "killed" not in captured.out
    assert "infrastructure" in captured.err.lower()
    assert mutations.SCHEDULER.read_text() == "original rule\n"


@pytest.mark.parametrize("failed", [False, True])
def test_only_a_named_selected_regression_counts_as_a_kill(runner, monkeypatch, capsys, failed):
    node = mutations.ROUTE + "::test_c9_5_waits"
    stdout = f"FAILED {node} - AssertionError\n1 failed" if failed else "1 passed"
    monkeypatch.setattr(mutations, "pytest", lambda _: subprocess.CompletedProcess([], int(failed), stdout, ""))
    assert mutations.main() == (0 if failed else 1)
    assert f"{int(failed)} of 1 mutations killed" in capsys.readouterr().out
    assert mutations.SCHEDULER.read_text() == "original rule\n"
