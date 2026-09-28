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

import pytest

from tests.e2e.conftest import _sockets
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
    not a failure. A root that cannot be copied stays where it is.
    """
    root = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    failed = True
    try:
        yield root
        report = getattr(request.node, "rep_call", None)
        failed = report is not None and report.failed
    except (pytest.skip.Exception, pytest.xfail.Exception):
        failed = False
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
    more from the timeline, store, outbox and watch fixtures. The directories a
    test makes are recorded, not globbed, so another run's roots in /tmp are
    never taken for this test's. A failed test reports its own failure, and its
    roots are under /tmp/sf-failed.
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
