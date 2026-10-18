"""Shared fixtures for the app's frontend probes: one compile of the core probe per
session, and state roots under /tmp that a passing test leaves nothing of."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import pytest

from tests.e2e.conftest import _sockets
from tests.frontend.swift import ROOT, compile_probe

CORE_PROBE = [ROOT / "tests/frontend/CoreProbe.swift", ROOT / "tests/frontend/CoreProbeScenarios.swift",
              ROOT / "tests/frontend/CoreProbeLive.swift"]
needs_swift = pytest.mark.skipif(sys.platform != "darwin" or shutil.which("xcrun") is None,
                                 reason="native Swift frontend validation requires macOS developer tools")
# The ⌘K palette's matching and the text scale (C-29.12, C-29.13).
CORE_PROBE.append(ROOT / "tests/frontend/CoreProbeSearch.swift")
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


# --- state roots ----------------------------------------------------------------

#: Where a failed test's state root is kept, as the e2e and fake-daemon harnesses keep theirs.
FAILED = Path("/tmp/sf-failed")


@contextmanager
def state_root(request, prefix: str):
    """A directory under /tmp for one test's state, removed when the test is done.

    Under /tmp because a macOS AF_UNIX path holds at most 103 bytes, which pytest's
    tmp_path overruns. A failed test's root is first copied, sockets left out, to
    /tmp/sf-failed/<test name>, as the e2e and fake-daemon harnesses keep theirs; so
    is a root whose own code raised (a fixture's setup or teardown, or a test body
    that uses this directly). Pytest never raises a test's failure into its
    fixtures, so a fixture learns of one only from the call's report
    (`pytest_runtest_makereport` above): a flag set after its `yield` is set
    whether the test passed or failed. A skip or xfail raised inside the block is
    judged by that report too, so it keeps the root only of a test whose call
    failed. A root that cannot be copied stays where it is.
    """
    def call_failed() -> bool:
        report = getattr(request.node, "rep_call", None)
        return report is not None and report.failed

    root = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    failed = True
    try:
        yield root
        failed = call_failed()
    except (pytest.skip.Exception, pytest.xfail.Exception, unittest.SkipTest):
        failed = call_failed()
        raise
    finally:
        if failed:
            keep = FAILED / re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)
            shutil.rmtree(keep, ignore_errors=True)
            shutil.copytree(root, keep, symlinks=True, ignore_dangling_symlinks=True, ignore=_sockets)
            print(f"\n[frontend harness] kept state root at {keep}", file=sys.stderr)
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
