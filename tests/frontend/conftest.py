"""Shared fixtures for the app's frontend probes: one compile of the core probe per session."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tests.frontend.swift import ROOT, compile_probe

CORE_PROBE = [ROOT / "tests/frontend/CoreProbe.swift", ROOT / "tests/frontend/CoreProbeScenarios.swift",
              ROOT / "tests/frontend/CoreProbeLive.swift"]
needs_swift = pytest.mark.skipif(sys.platform != "darwin" or shutil.which("xcrun") is None,
                                 reason="native Swift frontend validation requires macOS developer tools")


@pytest.fixture(scope="session")
def core_probe(tmp_path_factory) -> Path:
    """app/Sources compiled with SUBFLEET_MODEL_TEST (no AppKit, no SwiftUI) and the core probe."""
    if sys.platform != "darwin" or shutil.which("xcrun") is None:
        pytest.skip("native Swift frontend validation requires macOS developer tools")
    return compile_probe(tmp_path_factory.mktemp("subfleet-core") / "probe", CORE_PROBE, "SUBFLEET_MODEL_TEST")


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, "rep_" + report.when, report)


def run_probe(probe: Path, *args, env: dict | None = None, timeout: float = 60, raw: bool = False):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("SUBFLEET_")}
    environment.update(env or {})
    result = subprocess.run([str(probe), *map(str, args)], capture_output=True, text=True, timeout=timeout,
                            env=environment)
    assert result.returncode == 0, (result.returncode, result.stdout[-4000:], result.stderr[-4000:])
    return result.stdout if raw else json.loads(result.stdout)


def write_json(path: Path, value) -> Path:
    path.write_text(json.dumps(value))
    return path
