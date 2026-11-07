"""Resumable suite runs for r3 attempt a3 (not part of the repository).

A whole-suite shard doesn't fit in one 600 s tool call at this host's load, so a shard
runs as a series of calls, each under the shared suite lock:

- every finished test goes to $SUITE_DONE (JSONL, one object per test) as soon as its
  teardown report reaches the controller, so a cut run loses only the tests it was
  running;
- a run deselects every test already in $SUITE_DONE;
- after $SUITE_DEADLINE (epoch seconds) a worker starts no new test: it claims the test's
  protocol without running it and reports nothing, so the next run picks it up;
- reports that arrive after $SUITE_HARD (when the driver interrupts the run) are not
  recorded, so an interrupted test runs again rather than counting as a failure;
- each worker writes the shard's collected and remaining counts to $SUITE_DONE.collect.
"""
from __future__ import annotations

import json
import os
import time

import pytest

DONE = os.environ["SUITE_DONE"]
DEADLINE = float(os.environ["SUITE_DEADLINE"])
HARD = float(os.environ["SUITE_HARD"])
TAIL = 4000

_config = None
_state: dict[str, dict] = {}


def _done_ids() -> set[str]:
    ids = set()
    try:
        with open(DONE, encoding="utf-8") as done:
            for line in done:
                try:
                    ids.add(json.loads(line)["nodeid"])
                except (ValueError, KeyError):
                    pass
    except FileNotFoundError:
        pass
    return ids


def _is_worker() -> bool:
    return _config is not None and hasattr(_config, "workerinput")


def pytest_configure(config):
    global _config
    _config = config


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    done = _done_ids()
    keep = [item for item in items if item.nodeid not in done]
    dropped = [item for item in items if item.nodeid in done]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = keep
    worker = getattr(config, "workerinput", {}).get("workerid", "main")
    # Each worker writes its own file and renames it into place, so no two writers interleave.
    temporary = f"{DONE}.collect.{worker}"
    with open(temporary, "w", encoding="utf-8") as out:
        json.dump({"worker": worker, "collected": len(keep) + len(dropped), "done_before": len(dropped),
                   "remaining": len(keep), "at": time.time()}, out)
    os.replace(temporary, DONE + ".collect")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    if time.time() > DEADLINE:
        return True     # not run, nothing reported: the next run starts it


def _text(report) -> str:
    try:
        text = report.longreprtext
    except Exception:  # noqa: BLE001 - a report we can't render still counts
        text = repr(report.longrepr)
    return text[-TAIL:]


def _write(record: dict) -> None:
    with open(DONE, "a", encoding="utf-8") as done:
        done.write(json.dumps(record) + "\n")


def pytest_runtest_logreport(report):
    if _is_worker() or time.time() > HARD:
        return
    state = _state.setdefault(report.nodeid, {"nodeid": report.nodeid, "outcome": None, "seconds": 0.0})
    state["seconds"] = round(state["seconds"] + (getattr(report, "duration", 0.0) or 0.0), 3)
    xfail = hasattr(report, "wasxfail")
    if report.when == "setup":
        if report.failed:
            state["outcome"], state["detail"] = "error", _text(report)
        elif report.skipped:
            state["outcome"] = "xfailed" if xfail else "skipped"
            state["detail"] = _text(report)[-300:]
    elif report.when == "call":
        if report.passed:
            state["outcome"] = "xpassed" if xfail else "passed"
        elif report.failed:
            state["outcome"], state["detail"] = "failed", _text(report)
        elif report.skipped:
            state["outcome"] = "xfailed" if xfail else "skipped"
    elif report.when == "teardown":
        if report.failed:
            state["teardown_error"] = _text(report)
        if state["outcome"] is None:
            state["outcome"] = "error"
        state["at"] = round(time.time(), 1)
        _write(_state.pop(report.nodeid))
    else:                # xdist's report for a crashed worker ("???")
        state["outcome"], state["detail"] = "crashed", _text(report)
        state["at"] = round(time.time(), 1)
        _write(_state.pop(report.nodeid))
