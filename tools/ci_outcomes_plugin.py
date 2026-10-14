"""Record pytest's exact node IDs, phase outcomes and test durations for the CI retry wrapper."""

import json
import os
from pathlib import Path


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
