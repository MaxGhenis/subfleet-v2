"""Record pytest's exact node IDs, phase outcomes and test durations for the CI retry wrapper."""

import json
import os
from pathlib import Path


def session_path(outcomes: Path) -> Path:
    """Where a session's end is recorded, beside its outcomes: first.jsonl -> first.session.json."""
    return outcomes.with_suffix(".session.json")


class OutcomeRecorder:
    def __init__(self, path: Path):
        self.path = path

    def _record(self, report, **extra):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "nodeid": report.nodeid,
                "when": report.when,
                "outcome": report.outcome,
                "xfail": bool(getattr(report, "wasxfail", None)),
                **extra,
            }) + "\n")

    def pytest_runtest_logreport(self, report):
        # The phase's seconds; tools/ci_shard_durations.py sums them per test file.
        self._record(report, duration=report.duration)

    def pytest_collectreport(self, report):
        if report.failed:
            self._record(report)

    # A cancelled or interrupted job still uploads what it recorded. The session record says
    # whether the outcomes are whole, so tools/ci_shard_durations.py never takes a partial
    # file's time for its full time.
    def pytest_sessionstart(self, session):
        session_path(self.path).unlink(missing_ok=True)   # an earlier session's record vouches for nothing here

    def pytest_sessionfinish(self, session, exitstatus):
        session_path(self.path).write_text(
            json.dumps({"exitstatus": int(exitstatus), "collected": session.testscollected}) + "\n",
            encoding="utf-8")


def pytest_configure(config):
    # Resolve before any fixture can replace environ or change cwd. Each pytest
    # configuration owns its recorder, including pytester's nested sessions.
    path = Path(os.environ["SUBFLEET_PYTEST_OUTCOMES"]).resolve()
    config.pluginmanager.register(OutcomeRecorder(path), "subfleet-outcomes-recorder")
