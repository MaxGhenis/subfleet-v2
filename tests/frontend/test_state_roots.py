"""Frontend state-root cleanup and failed-test evidence in scratch pytest sessions.

The service harness roots live under /tmp to fit the macOS AF_UNIX path limit.
Passing tests remove their roots. A failed setup or call keeps a copy of the
root with sockets omitted, scoped to that test and pytest process. The scratch
sessions send their copies to each outer test's own temporary directory.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
import shutil
from types import SimpleNamespace
import uuid

import pytest

from tests import failed_keep

pytest_plugins = ["pytester"]

#: Every fixture here that makes a state root: (module, fixture, root prefix).
FIXTURES = [
    ("test_core_protocol", "harness", "sf-app-h-"),
    ("test_core_diff", "harness", "sf-app-d-"),
    ("test_core_timeline", "harness", "sf-tl-"),
    ("test_core_store", "harness", "sf-st-"),
    ("test_core_outbox", "daemon", "sf-ob-"),
    ("test_core_client", "service", "sf-wl-"),
    ("test_core_steer", "harness", "sf-steer-"),
    ("test_core_steer", "daemon", "sf-steer-ob-"),
    ("test_core_suggestions", "harness", "sf-app-sg-"),
    ("test_core_suggestions_r1b", "harness", "sf-app-sg-r1b-"),
    ("test_core_notifications", "harness", "sf-nt-"),
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
import unittest

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


@pytest.fixture
def skipping(request):
    with state_root(request, "sf-app-x-") as root:
        RECORD.joinpath("setup-skipped").write_text(str(root))
        pytest.skip("skips on purpose")
        yield root


@pytest.fixture
def skipping_after(request):
    with state_root(request, "sf-app-x-") as root:
        RECORD.joinpath("teardown-skipped").write_text(str(root))
        (root / "evidence.txt").write_text("kept")
        yield root
        pytest.skip("skips on purpose while tearing down")


def test_setup_fails_{token}(broken):
    pass


def test_setup_skips_{token}(skipping):
    pass


def test_fails_then_skips_while_tearing_down_{token}(skipping_after):
    assert False, "fails on purpose"


def test_inline_passes_{token}(request):
    with state_root(request, "sf-app-x-") as root:
        RECORD.joinpath("inline-passed").write_text(str(root))


def test_inline_fails_{token}(request):
    with state_root(request, "sf-app-x-") as root:
        RECORD.joinpath("inline-failed").write_text(str(root))
        (root / "evidence.txt").write_text("kept")
        assert False, "fails on purpose"


def test_inline_skips_{token}(request):
    with state_root(request, "sf-app-x-") as root:
        RECORD.joinpath("inline-skipped").write_text(str(root))
        pytest.skip("skips on purpose")


def test_inline_xfails_{token}(request):
    with state_root(request, "sf-app-x-") as root:
        RECORD.joinpath("inline-xfailed").write_text(str(root))
        pytest.xfail("xfails on purpose")


def test_inline_unittest_skips_{token}(request):
    with state_root(request, "sf-app-x-") as root:
        RECORD.joinpath("inline-unittest-skipped").write_text(str(root))
        raise unittest.SkipTest("skips on purpose")
"""

LEFTOVER_TESTS = """
from pathlib import Path
import tempfile

RECORD = Path({record!r})


def test_leaves_one_{token}():
    RECORD.joinpath("left").write_text(tempfile.mkdtemp(prefix="sf-left-", dir=RECORD.parent))


def test_cleans_up_{token}():
    with tempfile.TemporaryDirectory(prefix="sf-left-", dir=RECORD.parent) as directory:
        RECORD.joinpath("cleaned").write_text(directory)


def test_fails_and_leaves_one_{token}():
    RECORD.joinpath("failed").write_text(tempfile.mkdtemp(prefix="sf-left-", dir=RECORD.parent))
    assert False, "fails on purpose"
"""


DEPENDENT_SETUP_TEST = """
from pathlib import Path

import pytest

from tests.frontend.conftest import state_root

RECORD = Path({record!r})


@pytest.fixture
def with_root(request):
    with state_root(request, "sf-app-x-") as root:
        RECORD.joinpath("dependent-setup").write_text(str(root))
        (root / "evidence.txt").write_text("fixture started")
        yield root


@pytest.fixture
def broken(with_root):
    raise RuntimeError("dependent fixture fails on purpose")


def test_dependent_setup_fails_{token}(broken):
    pass
"""

SUBPROCESS_TEST = """
import os
from pathlib import Path

from tests.failed_keep import failed_keep_dir
from tests.frontend.conftest import state_root


def test_same_name(request):
    record = Path(os.environ["SF_STATE_ROOT_RECORD"])
    record.write_text(str(failed_keep_dir(request.node.name)))
    with state_root(request, "sf-app-x-") as root:
        (root / "evidence.txt").write_text(record.name)
        assert False, "fails on purpose"
"""

COPY_FAILURE_PREFIX = "sf-app-x-copy-"
COPY_FAILURE_TEST = """
from pathlib import Path

from tests.frontend.conftest import state_root

RECORD = Path({record!r})


def test_copy_fails_{token}(request):
    with state_root(request, {prefix!r}) as root:
        RECORD.joinpath("copy-failure").write_text(str(root))
        (root / "evidence.txt").write_text("left in place")
        assert False, "fails on purpose"
"""


@pytest.fixture
def inner(pytester, tmp_path, monkeypatch):
    """Run one generated test module with a dedicated folder for kept roots."""
    monkeypatch.setattr(failed_keep, "ROOT", tmp_path)
    token = uuid.uuid4().hex[:12]
    record = tmp_path / "record"
    record.mkdir()
    pytester.makeconftest(CONFTEST)

    def run(template: str, **fields):
        pytester.makepyfile(**{f"test_inner_{token}": template.format(token=token, record=str(record), **fields)})
        return pytester.runpytest_inprocess("-p", "no:cacheprovider")

    return SimpleNamespace(token=token, record=record, run=run)


def recorded(inner, name: str) -> Path:
    return Path((inner.record / name).read_text())


@pytest.mark.parametrize(("module", "fixture", "prefix"), FIXTURES,
                         ids=[f"{module}-{fixture}" for module, fixture, _ in FIXTURES])
def test_a_fixture_removes_its_root_after_a_pass_and_keeps_it_after_a_failure(inner, module, fixture, prefix):
    result = inner.run(FIXTURE_TESTS, module=module, fixture=fixture)
    result.assert_outcomes(passed=1, failed=1)
    passed, failed = recorded(inner, "passed"), recorded(inner, "failed")
    assert passed.parent == failed.parent == Path("/tmp")
    assert passed.name.startswith(prefix) and failed.name.startswith(prefix)
    assert not passed.exists() and not failed.exists()
    assert not failed_keep.failed_keep_dir(f"test_passes_{inner.token}").exists()
    kept = failed_keep.failed_keep_dir(f"test_fails_{inner.token}")
    assert (kept / "evidence.txt").is_file(), f"missing evidence at {kept}"
    assert (kept / "evidence.txt").read_text() == "kept"
    assert (kept / "work").is_dir()
    assert recorded(inner, "socket") == failed / "left.sock"
    assert not (kept / "left.sock").exists()


def test_a_root_whose_fixture_raised_or_whose_test_body_failed_is_kept(inner):
    """`state_root` used by a fixture whose setup raises or skips, or that skips while
    tearing down after a failed test, and inline in a test body that passes, fails,
    skips (pytest's or unittest's) or xfails: only the raise and the failures are kept."""
    result = inner.run(STATE_ROOT_TESTS)
    result.assert_outcomes(passed=1, failed=2, errors=1, skipped=4, xfailed=1)
    for name in ("setup", "setup-skipped", "teardown-skipped", "inline-passed", "inline-failed",
                 "inline-skipped", "inline-xfailed", "inline-unittest-skipped"):
        assert not recorded(inner, name).exists(), name
    names = (f"test_fails_then_skips_while_tearing_down_{inner.token}", f"test_inline_fails_{inner.token}",
             f"test_setup_fails_{inner.token}")
    assert set(failed_keep.ROOT.glob(f"*/*_{inner.token}")) == {
        failed_keep.failed_keep_dir(name) for name in names
    }
    for name in names:
        assert (failed_keep.failed_keep_dir(name) / "evidence.txt").read_text() == "kept", name


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


def test_a_failed_dependent_fixture_setup_keeps_the_root(inner):
    result = inner.run(DEPENDENT_SETUP_TEST)
    result.assert_outcomes(errors=1)
    reports = result.reprec.getreports("pytest_runtest_logreport")
    assert [(report.when, report.outcome) for report in reports] == [
        ("setup", "failed"),
        ("teardown", "passed"),
    ]
    assert "dependent fixture fails on purpose" in str(reports[0].longrepr)
    assert not recorded(inner, "dependent-setup").exists()
    keep = failed_keep.failed_keep_dir(f"test_dependent_setup_fails_{inner.token}")
    assert (keep / "evidence.txt").is_file(), f"missing evidence at {keep}"
    assert (keep / "evidence.txt").read_text() == "fixture started"


def test_two_pytest_processes_keep_their_own_evidence(pytester, monkeypatch, tmp_path):
    pytester.makeconftest(f"""{CONFTEST}
from pathlib import Path
from tests import failed_keep

failed_keep.ROOT = Path({str(tmp_path)!r})
""")
    pytester.makepyfile(test_shared_name=SUBPROCESS_TEST)

    expected = {}
    for label in ("first", "second"):
        record = tmp_path / label
        monkeypatch.setenv("SF_STATE_ROOT_RECORD", str(record))
        result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-q")
        result.assert_outcomes(failed=1)
        expected[Path(record.read_text())] = label

    assert len({path.parent.name for path in expected}) == 2
    assert set(tmp_path.glob("*/test_same_name")) == set(expected)
    for keep, evidence in expected.items():
        assert keep.parent.name.isdecimal()
        assert (keep / "evidence.txt").read_text() == evidence


def test_a_copy_failure_preserves_the_test_error_and_the_root(inner, monkeypatch):
    kept_file = inner.record / "kept"
    kept_file.write_text("a file where the kept-roots folder should be")
    monkeypatch.setattr(failed_keep, "ROOT", kept_file)

    try:
        result = inner.run(COPY_FAILURE_TEST, prefix=COPY_FAILURE_PREFIX)
        result.assert_outcomes(failed=1)
        reports = result.reprec.getreports("pytest_runtest_logreport")
        [call] = [report for report in reports if report.when == "call"]
        assert call.longrepr.reprcrash.message.startswith("AssertionError: fails on purpose")
        root = recorded(inner, "copy-failure")
        assert (root / "evidence.txt").read_text() == "left in place"
        assert str(root) in call.capstderr
    finally:
        if (inner.record / "copy-failure").exists():
            root = recorded(inner, "copy-failure")
            assert root.parent == Path("/tmp")
            assert root.name.startswith(COPY_FAILURE_PREFIX)
            with suppress(FileNotFoundError):
                shutil.rmtree(root)
