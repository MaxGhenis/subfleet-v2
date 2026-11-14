import json
import os
from pathlib import Path
import pytest

phases = []
collection_errors = []
imported_sources = {}

@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session):
    import subfleet.store
    import subfleet.retention_notices
    import tests.failed_keep
    root = Path.cwd().resolve()
    # Keep the shared test harness's pruning and failure artifacts in scratch.
    tests.failed_keep.ROOT = Path(os.environ["NOTICE_KEEP_FAILED_ROOT"])
    for module in (subfleet.store, subfleet.retention_notices):
        path = Path(module.__file__).resolve()
        assert path.is_relative_to(root), f"source escaped temporary checkout: {path}"
        imported_sources[module.__name__] = str(path)

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    phases.append({"nodeid": report.nodeid, "when": report.when, "outcome": report.outcome,
                   "exception": call.excinfo.typename if call.excinfo else None,
                   "assertion": bool(call.excinfo and call.excinfo.errisinstance(AssertionError)),
                   "failure": str(report.longrepr) if report.failed else None})

def pytest_collectreport(report):
    if report.failed:
        collection_errors.append(str(report.longrepr))

def pytest_sessionfinish(session, exitstatus):
    Path(os.environ["NOTICE_KEEP_PHASES"]).write_text(json.dumps(
        {"exitstatus": int(exitstatus), "phases": phases, "collection_errors": collection_errors,
         "imported_sources": imported_sources}, indent=2) + "\n")
