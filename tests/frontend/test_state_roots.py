"""The app core's state roots under /tmp: removed after a passing test, kept after a failed one.

The service harness fixtures put each test's state root under /tmp, where a
macOS AF_UNIX path fits, and until 2026-09-28 removed none: one machine held
783 protocol and 194 Changes-pane roots, and about 5,600 more from the
timeline, store, outbox and watch fixtures. They now make their roots with
`conftest.state_root`, which removes a root after a passing test and, after a
failed one, first copies it without its sockets to /tmp/sf-failed/<test name>,
as the e2e and fake-daemon harnesses keep theirs. Pytest never raises a test's
failure into its fixtures, so only the call's report can tell a fixture that
its test failed; each case here therefore runs the real fixture in an inner
pytest session, with a test that passes and one that fails.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
import shutil
from types import SimpleNamespace
import uuid

import pytest

from tests.frontend.conftest import FAILED

pytest_plugins = ["pytester"]

#: Every fixture here that makes a state root: (module, fixture, root prefix).
FIXTURES = [
    ("test_core_protocol", "harness", "sf-app-h-"),
    ("test_core_diff", "harness", "sf-app-d-"),
    ("test_core_timeline", "harness", "sf-tl-"),
    ("test_core_store", "harness", "sf-st-"),
    ("test_core_outbox", "daemon", "sf-ob-"),
    ("test_core_client", "service", "sf-wl-"),
]

#: The inner session gets the frontend conftest's report hook and leftover check.
CONFTEST = """
from tests.frontend.conftest import no_leftover_temporary_directories, pytest_runtest_makereport
"""

FIXTURE_TESTS = """
from pathlib import Path
import socket

from tests.frontend.{module} import {fixture}

RECORD = Path({record!r})


def root_of(value):
    return (value[0] if isinstance(value, tuple) else value).root


def test_passes_{token}({fixture}):
    RECORD.joinpath("passed").write_text(str(root_of({fixture})))


def test_fails_{token}({fixture}):
    root = root_of({fixture})
    RECORD.joinpath("failed").write_text(str(root))
    (root / "evidence.txt").write_text("kept")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(root / "left.sock"))
    finally:
        listener.close()
    RECORD.joinpath("socket").write_text(str(root / "left.sock"))
    assert False, "fails on purpose"
"""

STATE_ROOT_TESTS = """
from pathlib import Path

import pytest

from tests.frontend.conftest import state_root

RECORD = Path({record!r})


@pytest.fixture
def broken(request):
    with state_root(request, "sf-app-x-") as root:
        RECORD.joinpath("setup").write_text(str(root))
        (root / "evidence.txt").write_text("kept")
        raise RuntimeError("setup fails on purpose")
        yield root


def test_setup_fails_{token}(broken):
    pass


def test_inline_passes_{token}(request):
    with state_root(request, "sf-app-x-") as root:
        RECORD.joinpath("inline-passed").write_text(str(root))


def test_inline_fails_{token}(request):
    with state_root(request, "sf-app-x-") as root:
        RECORD.joinpath("inline-failed").write_text(str(root))
        (root / "evidence.txt").write_text("kept")
        assert False, "fails on purpose"
"""

LEFTOVER_TESTS = """
from pathlib import Path
import tempfile

RECORD = Path({record!r})


def test_leaves_one_{token}():
    RECORD.joinpath("left").write_text(tempfile.mkdtemp(prefix="sf-left-", dir="/tmp"))


def test_cleans_up_{token}():
    with tempfile.TemporaryDirectory(prefix="sf-left-", dir="/tmp") as directory:
        RECORD.joinpath("cleaned").write_text(directory)


def test_fails_and_leaves_one_{token}():
    RECORD.joinpath("failed").write_text(tempfile.mkdtemp(prefix="sf-left-", dir="/tmp"))
    assert False, "fails on purpose"
"""


@pytest.fixture
def inner(pytester, tmp_path):
    """An inner pytest session over one generated test file, with unique test names,
    so what it keeps under /tmp/sf-failed is its own; all of that is removed after
    (and /tmp/sf-failed too, when this made it and nothing else is in it)."""
    token = uuid.uuid4().hex[:12]
    record = tmp_path / "record"
    record.mkdir()
    pytester.makeconftest(CONFTEST)
    existed = FAILED.exists()

    def run(template: str, **fields):
        pytester.makepyfile(**{f"test_inner_{token}": template.format(token=token, record=str(record), **fields)})
        return pytester.runpytest_inprocess("-p", "no:cacheprovider")

    yield SimpleNamespace(token=token, record=record, run=run)
    for kept in FAILED.glob(f"*_{token}"):
        shutil.rmtree(kept, ignore_errors=True)
    if not existed:
        with suppress(OSError):
            FAILED.rmdir()


def recorded(inner, name: str) -> Path:
    return Path((inner.record / name).read_text())


def test_failed_roots_are_kept_where_the_other_harnesses_keep_theirs():
    """docs/lanes/README.md names this directory, and CI uploads it."""
    assert FAILED == Path("/tmp/sf-failed")


@pytest.mark.parametrize(("module", "fixture", "prefix"), FIXTURES, ids=[module for module, _, _ in FIXTURES])
def test_a_fixture_removes_its_root_after_a_pass_and_keeps_it_after_a_failure(inner, module, fixture, prefix):
    result = inner.run(FIXTURE_TESTS, module=module, fixture=fixture)
    result.assert_outcomes(passed=1, failed=1)
    passed, failed = recorded(inner, "passed"), recorded(inner, "failed")
    assert passed.parent == failed.parent == Path("/tmp")
    assert passed.name.startswith(prefix) and failed.name.startswith(prefix)
    # Nothing is left in /tmp either way.
    assert not passed.exists() and not failed.exists()
    # A pass keeps nothing; a failure keeps the root, without its sockets.
    assert not (FAILED / f"test_passes_{inner.token}").exists()
    kept = FAILED / f"test_fails_{inner.token}"
    assert (kept / "evidence.txt").read_text() == "kept"
    assert (kept / "work").is_dir()
    assert recorded(inner, "socket") == failed / "left.sock"
    assert not (kept / "left.sock").exists()


def test_a_root_whose_fixture_raised_or_whose_test_body_failed_is_kept(inner):
    """`state_root` used by a fixture whose setup raises, and inline in a test body."""
    result = inner.run(STATE_ROOT_TESTS)
    result.assert_outcomes(passed=1, failed=1, errors=1)
    for name in ("setup", "inline-passed", "inline-failed"):
        assert not recorded(inner, name).exists(), name
    assert (FAILED / f"test_setup_fails_{inner.token}" / "evidence.txt").read_text() == "kept"
    assert (FAILED / f"test_inline_fails_{inner.token}" / "evidence.txt").read_text() == "kept"
    assert not (FAILED / f"test_inline_passes_{inner.token}").exists()


def test_a_passing_test_that_leaves_a_directory_behind_errors(inner):
    """The leftover check: a passing test that leaves a directory it made is an error
    naming it; one that removes its directories passes; a failed test is only failed."""
    try:
        result = inner.run(LEFTOVER_TESTS)
        left = recorded(inner, "left")
        result.assert_outcomes(passed=2, failed=1, errors=1)
        result.stdout.fnmatch_lines([f"*test_leaves_one_{inner.token} passed but left behind directories it made: {left}*"])
        assert not recorded(inner, "cleaned").exists()
    finally:
        for name in ("left", "failed"):
            with suppress(FileNotFoundError):
                shutil.rmtree(recorded(inner, name))
