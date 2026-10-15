"""Record pytest's exact node IDs and phase outcomes for the CI retry wrapper."""

import json
import os
from pathlib import Path


class OutcomeRecorder:
    def __init__(self, path: Path):
        self.path = path

    def _record(self, report):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "nodeid": report.nodeid,
                "when": report.when,
                "outcome": report.outcome,
                "xfail": bool(getattr(report, "wasxfail", None)),
            }) + "\n")

    def pytest_runtest_logreport(self, report):
        self._record(report)

    def pytest_collectreport(self, report):
        if report.failed:
            self._record(report)


def pytest_configure(config):
    # Resolve before any fixture can replace environ or change cwd. Each pytest
    # configuration owns its recorder, including pytester's nested sessions.
    path = Path(os.environ["SUBFLEET_PYTEST_OUTCOMES"]).resolve()
    config.pluginmanager.register(OutcomeRecorder(path), "subfleet-outcomes-recorder")
