"""A failed test's state root is kept whether or not the harness's teardown raises (D-KR1, #238 review r1 finding 2).

r1 saw `harness.close()` raise `TimeoutExpired` stopping `tests.fake.run_daemon`: the keep code ran after it in
the same `finally`, so the copy never ran and `TemporaryDirectory` deleted the root. Each case here runs the real
`daemon` (tests/fake) or `e2e` (tests/e2e) fixture in a scratch pytest session, over every pairing of a test that
fails or passes with a `close()` that raises or returns:
- a failed test's root is kept, and the copy is taken after `close()` has run (it holds close()'s marker);
- a `close()` that raised keeps the root too, as the live app test's `dev_daemon` does;
- close()'s own error is still the teardown's error;
- a passing test whose `close()` returned keeps nothing.
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

SCRATCH = '''
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

    kept = {path.name for path in (tmp_path / "kept").glob("*/*")}
    assert kept == {failed_keep.failed_keep_dir(name).name
                    for name in ("test_fails[close_raises]", "test_fails[close_returns]", "test_passes[close_raises]")}
    for name in ("test_fails[close_raises]", "test_fails[close_returns]", "test_passes[close_raises]"):
        keep = failed_keep.failed_keep_dir(name)
        assert (keep / "evidence.txt").read_text() == "written by the test"
        # Taken after close(): a copy made before it (keep code moved above close()) has no marker.
        assert (keep / "closed.txt").read_text() == "close ran"
