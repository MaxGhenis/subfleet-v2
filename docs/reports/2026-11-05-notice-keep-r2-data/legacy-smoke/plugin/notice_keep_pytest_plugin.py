"""Temporary phase-recording plugin for tools.notice_keep_mutants."""
import json
import os
from pathlib import Path
import pytest
from tools.notice_keep_mutants import install_mutant

phases = []
collection_errors = []
patch = None

def pytest_addoption(parser):
    parser.addoption("--notice-keep-mutant", default=None)

def pytest_configure(config):
    global patch
    name = config.getoption("--notice-keep-mutant")
    if name:
        patch = install_mutant(name)

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
        {"exitstatus": int(exitstatus), "phases": phases, "collection_errors": collection_errors},
        indent=2) + "\n")

def pytest_unconfigure(config):
    if patch is not None:
        patch.undo()
