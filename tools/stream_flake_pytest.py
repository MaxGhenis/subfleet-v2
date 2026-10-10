"""Repeat the D-F5 e2e in one pytest process and record complete failing traces.

Load with ``pytest -p tools.stream_flake_pytest --stream-loops=50
--stream-results=/path/to/results.jsonl <test node id>``.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest


def pytest_addoption(parser):
    parser.addoption("--stream-loops", type=int, default=1)
    parser.addoption("--stream-results", type=Path)


def pytest_collection_modifyitems(config, items):
    loops = config.getoption("--stream-loops")
    if loops < 1:
        raise pytest.UsageError("--stream-loops must be positive")
    if loops == 1:
        return
    if any(hasattr(item, "callspec") for item in items):
        raise pytest.UsageError("--stream-loops requires unparametrized tests")
    originals = list(items)
    items[:] = [pytest.Function.from_parent(item.parent, name=f"{item.name}[loop-{i + 1:03d}]",
                                          callobj=item.obj, originalname=item.originalname)
                for i in range(loops) for item in originals]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    path = item.config.getoption("--stream-results")
    if path is None or (report.when != "call" and report.passed):
        return
    row = {"test": item.nodeid, "phase": report.when, "outcome": report.outcome,
           "duration": report.duration}
    if report.failed:
        row["failure"] = str(report.longrepr)
        conv = item.funcargs.get("conv")
        if conv is not None:
            try:
                with sqlite3.connect(f"file:{conv.e2e.root / 'conversations.sqlite3'}?mode=ro", uri=True) as db:
                    db.row_factory = sqlite3.Row
                    row["events"] = [dict(event) for event in db.execute(
                        "SELECT seq,message_id,source,position,ordinal,kind,data_json FROM events ORDER BY seq")]
            except sqlite3.Error as error:
                row["event_capture_error"] = str(error)
    if report.skipped:
        row["reason"] = str(report.longrepr)
    with path.open("a") as output:
        output.write(json.dumps(row) + "\n")
