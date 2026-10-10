"""Record pytest's exact node IDs and phase outcomes for the CI retry wrapper."""

import json
import os
from pathlib import Path


def _record(report):
    with Path(os.environ["SUBFLEET_PYTEST_OUTCOMES"]).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "xfail": bool(getattr(report, "wasxfail", None)),
        }) + "\n")


def pytest_runtest_logreport(report):
    _record(report)


def pytest_collectreport(report):
    if report.failed:
        _record(report)
