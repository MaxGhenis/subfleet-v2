"""Shared fixtures for the app's frontend probes: one compile of the core probe per session."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import pytest

from tests.failed_keep import failed_keep_dir, setup_or_call_failed
from tests.frontend.swift import ROOT, compile_probe

CORE_PROBE = [ROOT / "tests/frontend/CoreProbe.swift", ROOT / "tests/frontend/CoreProbeScenarios.swift",
              ROOT / "tests/frontend/CoreProbeLive.swift"]
needs_swift = pytest.mark.skipif(sys.platform != "darwin" or shutil.which("xcrun") is None,
                                 reason="native Swift frontend validation requires macOS developer tools")
# The ⌘K palette's matching and the text scale (C-29.12, C-29.13).
CORE_PROBE.append(ROOT / "tests/frontend/CoreProbeSearch.swift")
CORE_PROBE.extend([ROOT / "tests/frontend/CoreProbeQueue.swift", ROOT / "tests/frontend/CoreProbeQueueEngine.swift"])
# Suggested conversations: the Suggested section and the timeline's card (C-29.14).
CORE_PROBE.append(ROOT / "tests/frontend/CoreProbeSuggestions.swift")


@pytest.fixture(scope="session")
def core_probe(tmp_path_factory) -> Path:
    """app/Sources compiled with SUBFLEET_MODEL_TEST (no AppKit, no SwiftUI) and the core probe."""
    if sys.platform != "darwin" or shutil.which("xcrun") is None:
        pytest.skip("native Swift frontend validation requires macOS developer tools")
    return compile_probe(tmp_path_factory.mktemp("subfleet-core") / "probe", CORE_PROBE, "SUBFLEET_MODEL_TEST")


# A test's failure is not raised into its fixtures: a fixture that keeps a failed run's
# state (test_core_live.py's `dev_daemon`) reads the phase's report from the item, as the
# e2e and fake harnesses' fixtures do (tests/e2e/conftest.py, tests/fake/conftest.py).
@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, "rep_" + report.when, report)


@pytest.fixture(scope="session")
def conversation_view_probe(tmp_path_factory) -> Path:
    return compile_probe(tmp_path_factory.mktemp("subfleet-conversation-view") / "probe",
                         ROOT / "tests/frontend/ConversationViewProbe.swift", "SUBFLEET_VIEW_TEST")


@pytest.fixture(scope="session")
def cutover_model_probe(tmp_path_factory) -> Path:
    return compile_probe(tmp_path_factory.mktemp("subfleet-cutover-model") / "probe",
                         [ROOT / "tests/frontend/CutoverModelProbe.swift",
                          ROOT / "tests/frontend/CutoverViewScenarios.swift"], "SUBFLEET_UI_MODEL_TEST")


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


@pytest.fixture(scope="session")
def r2_models(tmp_path_factory):
    probe = compile_probe(tmp_path_factory.mktemp("r2-models") / "probe",
                          ROOT / "tests/frontend/R2PresentationProbe.swift", "SUBFLEET_MODEL_TEST")
    return run_probe(probe, ROOT / "tests/fixtures/visual/codex-commands.json",
                     ROOT / "tests/fixtures/visual/approval-schema-requests.json")


@pytest.fixture(scope="session")
def queue_tray_view_probe(tmp_path_factory) -> Path:
    return compile_probe(tmp_path_factory.mktemp("subfleet-queue-view") / "probe",
                         ROOT / "tests/frontend/QueueTrayViewProbe.swift", "SUBFLEET_VIEW_TEST")


@contextmanager
def state_root(request, prefix: str):
    """Own one service state root under /tmp so it fits the macOS AF_UNIX path limit.

    A fixture learns that its test failed from the setup and call reports.
    An exception inside this block counts even before those reports exist.
    Skips and xfails use the reports, including a skip during teardown
    after a failed call. A failed root is copied without sockets, and its
    original stays in place if the copy raises an OSError.
    """
    from tests.e2e.conftest import _sockets

    root = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    failed = True
    try:
        yield root
        failed = setup_or_call_failed(request.node)
    except (pytest.skip.Exception, pytest.xfail.Exception, unittest.SkipTest):
        failed = setup_or_call_failed(request.node)
        raise
    finally:
        remove = True
        if failed:
            keep = failed_keep_dir(request.node.name)
            try:
                shutil.rmtree(keep, ignore_errors=True)
                shutil.copytree(root, keep, symlinks=True, ignore_dangling_symlinks=True, ignore=_sockets)
            except OSError as error:
                remove = False
                print(f"\n[frontend harness] could not keep the state root ({error}). Left at {root}", file=sys.stderr)
            else:
                print(f"\n[frontend harness] kept state root at {keep}", file=sys.stderr)
        if remove:
            shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def no_leftover_temporary_directories(request):
    """A test that passes leaves behind no directory it made with `tempfile.mkdtemp`
    (which `TemporaryDirectory` uses too).

    The service harness fixtures here removed none of their roots: by 2026-09-28
    one machine held 783 /tmp/sf-app-h-*, 194 /tmp/sf-app-d-* and about 5,600
    more from the timeline, store, outbox and watch fixtures, and the steer tests
    added later left one per test (one per example of a Hypothesis test). The
    directories a test makes are recorded, not globbed, so another run's roots in
    /tmp are never taken for this test's. A failed test reports its own failure,
    and its roots are under /tmp/sf-failed.
    """
    made: list[str] = []
    mkdtemp = tempfile.mkdtemp

    def recording(*args, **kwargs):
        path = mkdtemp(*args, **kwargs)
        made.append(path)
        return path

    tempfile.mkdtemp = recording
    try:
        yield
    finally:
        tempfile.mkdtemp = mkdtemp
    report = getattr(request.node, "rep_call", None)
    left = [path for path in made if os.path.lexists(path)]
    if report is not None and report.passed and left:
        pytest.fail(f"{request.node.name} passed but left behind directories it made: {', '.join(left)}",
                    pytrace=False)
