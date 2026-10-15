"""Record pytest's exact node IDs, phase outcomes and test durations for the CI retry wrapper."""

import json
import os
from pathlib import Path

import pytest

_SESSION = pytest.StashKey[Path]()


def session_path(outcomes: Path) -> Path:
    """Where a session's end is recorded, beside its outcomes: first.jsonl -> first.session.json."""
    return outcomes.with_suffix(".session.json")


def _record(report, **extra):
    with Path(os.environ["SUBFLEET_PYTEST_OUTCOMES"]).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "xfail": bool(getattr(report, "wasxfail", None)),
            **extra,
        }) + "\n")


def pytest_runtest_logreport(report):
    # The phase's seconds; tools/ci_shard_durations.py sums them per test file.
    _record(report, duration=report.duration)


def pytest_collectreport(report):
    if report.failed:
        _record(report)


# A cancelled or interrupted job still uploads what it recorded. The session record says
# whether the outcomes are whole, so tools/ci_shard_durations.py never takes a partial
# file's time for its full time.
def pytest_sessionstart(session):
    path = session_path(Path(os.environ["SUBFLEET_PYTEST_OUTCOMES"]))
    path.unlink(missing_ok=True)          # an earlier session's record vouches for nothing here
    session.config.stash[_SESSION] = path


def pytest_sessionfinish(session, exitstatus):
    path = session.config.stash.get(_SESSION, None)
    if path is not None:
        path.write_text(json.dumps({"exitstatus": int(exitstatus), "collected": session.testscollected}) + "\n",
                        encoding="utf-8")
