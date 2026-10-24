"""A failed test's state root is kept whether or not the harness's teardown raises (D-KR1, #238 review r1 finding 2).

r1 saw `harness.close()` raise `TimeoutExpired` stopping `tests.fake.run_daemon`: the keep code ran after it in
the same `finally`, so the copy never ran and `TemporaryDirectory` deleted the root. Each case here runs the real
`daemon` (tests/fake) or `e2e` (tests/e2e) fixture in a scratch pytest session, over every pairing of a test that
fails or passes with a `close()` that raises or returns:
- a failed test's root is kept, and the copy is taken after `close()` has run (it holds close()'s marker);
- a `close()` that raised keeps the root too, as the live app test's `dev_daemon` does;
- close()'s own error is still the teardown's error;
- a passing test whose `close()` returned keeps nothing.
A test whose setup fails keeps its root as well (#247 review r1, P3-2): a fixture built on the harness that raises,
as `conv`'s daemon start does when its 10 s wait times out under load, ends the test before its call, so a keep
decided by the call report alone kept nothing. A setup that skips keeps nothing unless `close()` raised.
The kept roots go to a scratch folder, never the real one CI uploads.
"""
from __future__ import annotations

import pytest

from tests import failed_keep

pytest_plugins = ["pytester"]

#: Each harness's fixture with what it needs and nothing that reads ps or compiles the app's stand-in:
#: these tests never start a daemon.
CONFTESTS = {
    "daemon": '''
import pytest
from tests.fake.conftest import daemon, pytest_runtest_makereport  # noqa: F401  the harness under test

@pytest.fixture
def process_inspection_available():
    """The real one reads ps; nothing here starts a daemon."""
''',
    "e2e": '''
import pytest
from tests.e2e.conftest import e2e, pytest_runtest_makereport  # noqa: F401  the harness under test

@pytest.fixture
def e2e_process_inspection():
    """The real one reads ps; nothing here starts a daemon."""

@pytest.fixture
def dev_app():
    return "no development app in this scratch session"
''',
}

HELPERS = '''
import subprocess
import pytest

CASES = pytest.mark.parametrize("raises", [True, False], ids=["close_raises", "close_returns"])


def closing(harness, raises):
    """close() leaves a marker in the root, runs the real close(), then raises if asked."""
    original = harness.close
    def close():
        (harness.root / "closed.txt").write_text("close ran")
        original()
        if raises:
            raise subprocess.TimeoutExpired(["python", "-m", "tests.fake.run_daemon"], 2)
    harness.close = close
    (harness.root / "evidence.txt").write_text("written by the test")
'''

SCRATCH = HELPERS + '''

@CASES
def test_fails(HARNESS, raises):
    closing(HARNESS, raises)
    assert False, "deliberate failure"


@CASES
def test_passes(HARNESS, raises):
    closing(HARNESS, raises)
'''


@pytest.mark.parametrize("harness", sorted(CONFTESTS))
def test_a_failed_test_keeps_its_root_whether_or_not_close_raises(pytester, monkeypatch, tmp_path, harness):
    monkeypatch.setattr(failed_keep, "ROOT", tmp_path / "kept")
    pytester.makeconftest(CONFTESTS[harness])
    pytester.makepyfile(test_scratch_teardown=SCRATCH.replace("HARNESS", harness))
    run = pytester.inline_run("-p", "no:cacheprovider", "-q")
    reports = {(report.nodeid.split("::")[-1], report.when): report
               for report in run.getreports("pytest_runtest_logreport")}

    def outcome(test, when):
        return reports[(test, when)].outcome

    assert outcome("test_fails[close_raises]", "call") == outcome("test_fails[close_returns]", "call") == "failed"
    assert outcome("test_passes[close_raises]", "call") == outcome("test_passes[close_returns]", "call") == "passed"
    for test in ("test_fails[close_raises]", "test_passes[close_raises]"):
        # close()'s error is the teardown's error: the keep ran in a nested finally and did not replace it.
        assert outcome(test, "teardown") == "failed"
        assert "TimeoutExpired" in str(reports[(test, "teardown")].longrepr)
        assert "tests.fake.run_daemon" in str(reports[(test, "teardown")].longrepr)
    for test in ("test_fails[close_returns]", "test_passes[close_returns]"):
        assert outcome(test, "teardown") == "passed"

    for name in ("test_fails[close_raises]", "test_fails[close_returns]", "test_passes[close_raises]"):
        keep = failed_keep.failed_keep_dir(name)
        assert (keep / "evidence.txt").read_text() == "written by the test", f"{name} kept nothing"
        # Taken after close(): a copy made before it (keep code moved above close()) has no marker.
        assert (keep / "closed.txt").exists(), f"{name}'s root was copied before close() ran"
        assert (keep / "closed.txt").read_text() == "close ran"
    kept = {path.name for path in (tmp_path / "kept").glob("*/*")}
    assert kept == {failed_keep.failed_keep_dir(name).name
                    for name in ("test_fails[close_raises]", "test_fails[close_returns]", "test_passes[close_raises]")}


@pytest.mark.parametrize("harness", sorted(CONFTESTS))
def test_a_copy_that_fails_never_replaces_close_s_error(pytester, monkeypatch, tmp_path, harness):
    (tmp_path / "kept").write_text("a file where the kept-roots folder should be\n")
    monkeypatch.setattr(failed_keep, "ROOT", tmp_path / "kept")
    pytester.makeconftest(CONFTESTS[harness])
    pytester.makepyfile(test_scratch_teardown=SCRATCH.replace("HARNESS", harness))
    run = pytester.inline_run("-p", "no:cacheprovider", "-q", "-k", "close_raises")
    teardowns = {report.nodeid.split("::")[-1]: report for report in run.getreports("pytest_runtest_logreport")
                 if report.when == "teardown"}
    assert sorted(teardowns) == ["test_fails[close_raises]", "test_passes[close_raises]"]
    for report in teardowns.values():
        assert report.failed
        assert report.longrepr.reprcrash.message.startswith("subprocess.TimeoutExpired"), report.longrepr.reprcrash
        assert "could not keep the state root" in report.capstderr
    assert (tmp_path / "kept").read_text() == "a file where the kept-roots folder should be\n"


#: Fixtures built on the harness that end its setup, as `conv`, `hooked` and `dispatching` in
#: tests/e2e/test_conversations.py can: each starts the daemon during setup, so a start whose wait times out
#: under load fails the test's setup, and its call never runs.
SETUP_SCRATCH = HELPERS + '''

@pytest.fixture
def started(HARNESS, raises):
    closing(HARNESS, raises)
    HARNESS.until(lambda: False, timeout=0)      # the harness's own wait, timing out as a slow daemon start's does


@pytest.fixture
def skipped(HARNESS, raises):
    closing(HARNESS, raises)
    pytest.skip("deliberate skip")


@CASES
def test_setup_fails(started, raises):
    pass


@CASES
def test_setup_skips(skipped, raises):
    pass
'''


@pytest.mark.parametrize("harness", sorted(CONFTESTS))
def test_a_test_whose_setup_fails_keeps_its_root(pytester, monkeypatch, tmp_path, harness):
    monkeypatch.setattr(failed_keep, "ROOT", tmp_path / "kept")
    pytester.makeconftest(CONFTESTS[harness])
    pytester.makepyfile(test_scratch_setup=SETUP_SCRATCH.replace("HARNESS", harness))
    run = pytester.inline_run("-p", "no:cacheprovider", "-q")
    reports = {(report.nodeid.split("::")[-1], report.when): report
               for report in run.getreports("pytest_runtest_logreport")}
    tests = [f"test_setup_{end}[close_{close}]" for end in ("fails", "skips") for close in ("raises", "returns")]
    assert sorted(reports) == sorted((test, when) for test in tests for when in ("setup", "teardown")), \
        "every test ends in setup: none has a call report"
    for test in tests:
        assert reports[(test, "setup")].outcome == ("failed" if "fails" in test else "skipped")
        assert reports[(test, "teardown")].outcome == ("failed" if "close_raises" in test else "passed")
    for test in ("test_setup_fails[close_raises]", "test_setup_fails[close_returns]"):
        assert "condition timed out" in str(reports[(test, "setup")].longrepr)

    # A failed setup keeps the root whether or not close() raised; a skipped one only when close() raised.
    kept_tests = ("test_setup_fails[close_raises]", "test_setup_fails[close_returns]", "test_setup_skips[close_raises]")
    for name in kept_tests:
        keep = failed_keep.failed_keep_dir(name)
        assert (keep / "evidence.txt").is_file(), f"{name} kept nothing"
        assert (keep / "evidence.txt").read_text() == "written by the test"
        assert (keep / "closed.txt").is_file(), f"{name}'s root was copied before close() ran"
    kept = {path.name for path in (tmp_path / "kept").glob("*/*")}
    assert kept == {failed_keep.failed_keep_dir(name).name for name in kept_tests}
